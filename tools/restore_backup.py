#!/usr/bin/env python3
"""
danran バックアップ復元 — bridge の日次バックアップ（~/danran_backups/danran-*.json.gz）を
Supabase に upsert で書き戻す。

使い方（mini 上で）:
  python3 tools/restore_backup.py                          # 最新のバックアップを dry-run（書き込まない）
  python3 tools/restore_backup.py --apply                  # 最新のバックアップを実際に復元
  python3 tools/restore_backup.py --table messages --apply # 1テーブルだけ復元
  python3 tools/restore_backup.py ~/danran_backups/danran-2026-06-10.json.gz --apply

仕組み:
  - upsert（Prefer: resolution=merge-duplicates）なので既存行は上書き・無い行は復活。
    バックアップ後に送られた新しいメッセージは消えない（マージであり置換ではない）。
  - FK 順（users → rooms → … → reactions）で投入する。
  - メディア本体（写真・動画）は ~/danran_backups/media/ にあり、Storage へ手で戻す
    （URL が変わらなければ messages からの参照はそのまま生きる）。

★ 既定は dry-run。--apply を付けない限り一切書き込まない。
"""
import gzip
import json
import os
import sys
import tomllib
import urllib.parse
import urllib.request

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_sec = tomllib.load(open(os.path.join(REPO_DIR, ".streamlit", "secrets.toml"), "rb"))
URL = _sec["supabase"]["url"].rstrip("/")
KEY = _sec["supabase"]["anon_key"]

# 復元順（FK: reactions.message_id → messages.id があるため messages が先）と
# upsert の競合キー（PostgREST on_conflict）。
TABLES = (
    ("users",              "id"),
    ("rooms",              "id"),
    ("room_members",       "id"),
    ("messages",           "id"),
    ("reactions",          "id"),
    ("last_read",          "user_id,room_name"),
    ("push_subscriptions", "id"),
    ("ai_tasks",           "id"),
    ("ai_reminders",       "id"),
    ("events",             "id"),
)
BATCH = 500


def upsert(table, conflict, rows):
    """rows を BATCH 件ずつ upsert。戻り値は投入行数。"""
    n = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        req = urllib.request.Request(
            URL + "/rest/v1/" + table + "?on_conflict=" + urllib.parse.quote(conflict),
            data=json.dumps(chunk).encode(),
            headers={"apikey": KEY, "Authorization": "Bearer " + KEY,
                     "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            method="POST")
        with urllib.request.urlopen(req, timeout=120):
            pass
        n += len(chunk)
    return n


def main():
    args = sys.argv[1:]
    apply_ = "--apply" in args
    only = args[args.index("--table") + 1] if "--table" in args else ""
    files = [a for a in args if not a.startswith("--") and a != only]
    if files:
        path = os.path.expanduser(files[0])
    else:
        import glob
        gens = sorted(glob.glob(os.path.expanduser("~/danran_backups/danran-*.json.gz")))
        if not gens:
            print("バックアップが見つかりません（~/danran_backups/）"); sys.exit(1)
        path = gens[-1]

    dump = json.load(gzip.open(path, "rt", encoding="utf-8"))
    print(f"バックアップ: {path}")
    print(f"取得日時: {dump.get('_meta', {}).get('taken_at', '?')}")
    print(f"モード: {'★ 復元実行' if apply_ else 'dry-run（--apply で実行）'}\n")

    for table, conflict in TABLES:
        if only and table != only:
            continue
        rows = dump.get(table) or []
        if not rows:
            print(f"  {table:<20} 0 行（スキップ）")
            continue
        if apply_:
            try:
                n = upsert(table, conflict, rows)
                print(f"  {table:<20} {n} 行を upsert ✅")
            except Exception as e:
                print(f"  {table:<20} ❌ 失敗: {e}")
        else:
            print(f"  {table:<20} {len(rows)} 行（upsert予定 / 競合キー: {conflict}）")

    if not apply_:
        print("\n何も書き込んでいません。実行するには --apply を付けてください。")


if __name__ == "__main__":
    main()
