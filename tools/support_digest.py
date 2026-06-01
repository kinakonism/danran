#!/usr/bin/env python3
"""
danran サポートダイジェスト — 「🤖 AIサポート」ルームの会話を読み、
Claude（Max の claude CLI）で「改善のためのダイジェスト」を生成する。

使い方:
  cd ~/danran
  python3 tools/support_digest.py            # 直近14日分
  python3 tools/support_digest.py 30         # 直近30日分

出力: 標準出力 ＋ tools/support_digest_YYYYMMDD.md に保存。
得られた TODO を Claude Code（このリポジトリ）に投げると修正まで進められる。
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOM     = "🤖 AIサポート"
BOT_UID  = "00000000-0000-0000-0000-0000000000a1"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAYS     = int(sys.argv[1]) if len(sys.argv) > 1 else 14

_sec = tomllib.load(open(os.path.join(REPO_DIR, ".streamlit", "secrets.toml"), "rb"))
URL  = _sec["supabase"]["url"].rstrip("/")
KEY  = _sec["supabase"]["anon_key"]
HDR  = {"apikey": KEY, "Authorization": "Bearer " + KEY}


def fetch_messages():
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
    q = ("messages?select=user_id,user_name,content,image_url,created_at"
         "&room_name=eq." + urllib.parse.quote(ROOM) +
         "&created_at=gte." + urllib.parse.quote(since) +
         "&order=created_at.asc&limit=1000")
    req = urllib.request.Request(URL + "/rest/v1/" + q, headers=HDR)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def claude_bin():
    p = shutil.which("claude")
    if p:
        return p
    for c in sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/claude")), reverse=True) + \
             ["/opt/homebrew/bin/claude", "/usr/local/bin/claude"]:
        if os.path.exists(c):
            return c
    return "claude"


PROMPT_HEAD = (
    "以下は家族チャットアプリ「danran」の『AIサポート』ルームの会話ログです。"
    "家族からのバグ報告・要望・質問を読み、開発者（まさと）向けの改善ダイジェストを"
    "日本語・プレーンテキスト（マークダウン記法なし）で作ってください。次の構成で簡潔に:\n"
    "1) バグ報告（症状 / 再現条件 / 推定原因）\n"
    "2) 機能要望・改善案\n"
    "3) よくある質問 → FAQ や UI 改善の候補\n"
    "4) すぐ直せそうな TODO（優先度: 高/中/低 を付ける）\n"
    "会話が少なければ無理に埋めず、その旨を書いてOK。\n\n"
    "=== 会話ログ ===\n"
)


def main():
    msgs = fetch_messages()
    lines = []
    for m in msgs:
        c = (m.get("content") or "").strip() or ("（画像を送信）" if m.get("image_url") else "")
        if not c:
            continue
        who = "アシスタント(AI)" if m.get("user_id") == BOT_UID else (m.get("user_name") or "家族")
        ts = (m.get("created_at") or "")[:16].replace("T", " ")
        lines.append(f"[{ts}] {who}: {c}")
    if not lines:
        print(f"直近{DAYS}日のサポート会話はありません。")
        return
    print(f"[digest] 直近{DAYS}日 / {len(lines)} 発言を要約中…（claude）")
    r = subprocess.run(
        [claude_bin(), "-p", PROMPT_HEAD + "\n".join(lines), "--max-turns", "1"],
        cwd=REPO_DIR, capture_output=True, text=True, timeout=240,
    )
    out = (r.stdout or "").strip() or "（生成に失敗しました）"
    print("\n" + "=" * 48 + "\n" + out + "\n" + "=" * 48)
    fn = os.path.join(REPO_DIR, "tools", "support_digest_" + datetime.now().strftime("%Y%m%d") + ".md")
    with open(fn, "w") as f:
        f.write(f"# danran サポートダイジェスト（直近{DAYS}日 / {datetime.now():%Y-%m-%d %H:%M}）\n\n{out}\n")
    print(f"\n保存: {fn}")


if __name__ == "__main__":
    main()
