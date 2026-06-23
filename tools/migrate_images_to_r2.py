#!/usr/bin/env python3
"""
既存の Supabase Storage 画像（chat-images / avatars）を R2 にコピーし、
DB の参照URL（messages.image_url / messages.user_avatar / users.avatar /
users.cover_url / rooms.icon）を R2 の絶対URLに差し替える。

- Supabase の原本は削除しない（残す）。R2 にコピーして配信だけ R2 に移す＝egress 無料化。
- 重複URLは1回だけアップロード（user_avatar スナップショットは同一URLが大量にあるため）。
- 冪等: 既に /media/ を指すURLはスキップ。再実行しても二重コピーしない（URL→key マップを
  ~/.danran_img_migrated.json に保存）。

使い方:  python3 tools/migrate_images_to_r2.py            # dry-run（変更しない）
        python3 tools/migrate_images_to_r2.py --apply    # 実行
"""
import json
import os
import socket
import sys
import time
import tomllib
import urllib.parse
import urllib.request

# IPv4 固定（mini の IPv6 経路で SSL handshake がハングするため。bridge と同流儀）
_orig_gai = socket.getaddrinfo
socket.getaddrinfo = lambda h, *a, **k: (
    [r for r in _orig_gai(h, *a, **k) if r[0] == socket.AF_INET] or _orig_gai(h, *a, **k))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_sec = tomllib.load(open(os.path.join(REPO, ".streamlit", "secrets.toml"), "rb"))
SB_URL = _sec["supabase"]["url"].rstrip("/")
SB_KEY = _sec["supabase"]["anon_key"]
WORKER = "https://danran-chat.kinakonism.workers.dev"
HDR = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json"}
MAP_PATH = os.path.expanduser("~/.danran_img_migrated.json")
APPLY = "--apply" in sys.argv

SB_MARK = "/storage/v1/object/public/"   # これを含む＝Supabase Storage 由来


def rest(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SB_URL + "/rest/v1/" + path, data=data, headers=HDR, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def is_sb(url):
    return bool(url) and SB_MARK in url and ("/chat-images/" in url or "/avatars/" in url)


def load_map():
    try:
        return json.load(open(MAP_PATH))
    except Exception:
        return {}


def save_map(m):
    json.dump(m, open(MAP_PATH, "w"), ensure_ascii=False)


def _get(req, tries=4):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(), r.headers
        except Exception as e:
            last = e; time.sleep(1.5 * (i + 1))
    raise last


def migrate_url(url, cache):
    """Supabase URL → R2 絶対URL。アップロード済みは cache を使う。"""
    if url in cache and cache[url] not in ("(dry-run)",):
        return cache[url]
    # ダウンロード（公開オブジェクト・リトライ付き）
    data, hdrs = _get(urllib.request.Request(url.split("?")[0], headers={"apikey": SB_KEY}))
    ctype = hdrs.get("Content-Type", "image/jpeg").split(";")[0]
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"
    if not APPLY:
        cache[url] = "(dry-run)"; return cache[url]
    up = urllib.request.Request(WORKER + "/media/upload", data=data, method="POST",
                                headers={"Content-Type": ctype, "x-danran-auth": SB_KEY,
                                         "User-Agent": "danran-migrate/1.0"})
    body, _ = _get(up)
    j = json.loads(body)
    new = WORKER + j["url"]
    cache[url] = new
    save_map(cache)
    return new


def main():
    print("モード:", "★ 実行(--apply)" if APPLY else "dry-run（--apply で実行）")
    cache = load_map()
    n_up = 0

    # 1) 一意なURLを全テーブルから収集
    urls = set()
    rows_msg = rest("GET", "messages?select=id,image_url,user_avatar") or []
    for m in rows_msg:
        for c in ("image_url", "user_avatar"):
            if is_sb(m.get(c)):
                urls.add(m[c])
    rows_usr = rest("GET", "users?select=id,avatar,cover_url") or []
    for u in rows_usr:
        for c in ("avatar", "cover_url"):
            if is_sb(u.get(c)):
                urls.add(u[c])
    rows_room = rest("GET", "rooms?select=id,icon") or []
    for r in rows_room:
        if is_sb(r.get("icon")):
            urls.add(r["icon"])
    print(f"移行対象の一意URL: {len(urls)} 件")

    # 2) アップロード（重複排除）
    for u in sorted(urls):
        try:
            new = migrate_url(u, cache)
            if APPLY:
                n_up += 1
            print(f"  {'UP' if APPLY else 'would'} {u.split('/')[-1][:40]} -> {new.split('/')[-1][:40]}")
        except Exception as e:
            print("  ERR", u[:60], e)

    if not APPLY:
        print("\ndry-run 完了。--apply で R2 コピー＋URL差し替えを実行します。")
        return

    # 3) DB 参照を差し替え（id 単位）
    def patch(table, rowid, col, oldv):
        new = cache.get(oldv)
        if not new or new == "(dry-run)":
            return 0
        rest("PATCH", f"{table}?id=eq.{rowid}", {col: new})
        return 1

    c_msg = c_usr = c_room = 0
    for m in rows_msg:
        for col in ("image_url", "user_avatar"):
            if is_sb(m.get(col)):
                c_msg += patch("messages", m["id"], col, m[col])
    for u in rows_usr:
        for col in ("avatar", "cover_url"):
            if is_sb(u.get(col)):
                c_usr += patch("users", u["id"], col, u[col])
    for r in rows_room:
        if is_sb(r.get("icon")):
            c_room += patch("rooms", r["id"], "icon", r["icon"])
    print(f"\n差し替え: messages={c_msg} / users={c_usr} / rooms={c_room}  (uploaded {n_up})")
    print("Supabase 原本は残しています（削除なし）。")


if __name__ == "__main__":
    main()
