#!/usr/bin/env python3
"""
danran AIサポート bridge — Claude Code CLI（Max プラン）をチャットルームに接続する。

仕組み:
  - Supabase の「🤖 AIサポート」ルームを数秒ごとに監視
  - 新しいユーザー発言が来たら、ローカルの `claude -p`（ヘッドレス）で返信を生成
  - その返信をボット（🤖 アシスタント）として Supabase に投稿

使い方（この Mac で常駐させる。Claude Code が Max でログイン済みであること）:
  cd ~/danran
  python3 tools/ai_bridge.py
  （止めるときは Ctrl+C。寝かせる/閉じると返信は止まります）

メモ:
  - API 課金なし（Max プランの claude CLI を使う）。ただし「サブスクの自動化」は
    Anthropic 規約的にグレーなので、家族・低頻度の私的利用にとどめること。
  - クラウド側(app.py)は secrets[ai].api_key が未設定なら返信しないので、bridge と二重返信しない。
    → bridge を使う場合は [ai] api_key を設定しないこと。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime

# ── 設定 ──────────────────────────────────────────────
ROOM       = "🤖 AIサポート"
BOT_UID    = "00000000-0000-0000-0000-0000000000a1"
BOT_NAME   = "🤖 アシスタント"
BOT_AVATAR = "🤖"
REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# claude を動かす作業ディレクトリ。REPO_DIR にすると danran のコード/CLAUDE.md を読んで
# 正確に答えられる（その分ファイルにアクセスできる）。安全重視なら中立なフォルダに変える。
WORKDIR    = REPO_DIR
POLL_SEC   = 4
SETTLE_SEC = 2.5    # 連投が落ち着くまで待ってから1回だけ返信
MAX_HIST   = 20

SYS = (
    "あなたは家族専用チャットアプリ「danran」のサポートAIです。"
    "ここは家族みんなが見る『AIサポート』ルーム。使い方の質問やバグ報告に、日本語で"
    "簡潔・やさしく答えます。\n"
    "【書き方のルール（重要）】\n"
    "- アプリ名は必ず半角で『danran』と書く（『danラン』『ダンラン』などにしない）。\n"
    "- マークダウン記法は使わない。`**`（太字）や`#`（見出し）、`・**…**`のような装飾を出さない。"
    "チャットでは記号がそのまま表示されて読みにくくなるため、プレーンな文章で書く。箇条書きは行頭『・』だけでよい。\n"
    "- 返信テキストだけを出力し、ファイル編集・コマンド実行などのツールは使わない。\n"
    "- 返信は数行で簡潔に、絵文字は控えめに。\n"
    "バグ報告は受け止めて、必要なら『どの画面で・何をしたら・どうなったか』を1つだけ簡潔に質問してください"
    "（開発者のまさともこのルームを見ます）。"
)

# ── Supabase 認証情報（.streamlit/secrets.toml から）──
_sec = tomllib.load(open(os.path.join(REPO_DIR, ".streamlit", "secrets.toml"), "rb"))
URL  = _sec["supabase"]["url"].rstrip("/")
KEY  = _sec["supabase"]["anon_key"]
HDR  = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"}


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + "/rest/v1/" + path, data=data, headers=HDR, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def fetch_recent(n=25):
    q = ("messages?select=user_id,user_name,content,image_url,created_at"
         "&room_name=eq." + urllib.parse.quote(ROOM) +
         "&order=created_at.desc&limit=" + str(n))
    rows = api("GET", q) or []
    return list(reversed(rows))


def parse_ts(s):
    if not s:
        return 0.0
    s = s.replace(" ", "T")
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(re.sub(r"([+-]\d{2})$", r"\1:00", s)).timestamp()
    except Exception:
        return 0.0


def post_reply(text):
    api("POST", "messages", {
        "room_name": ROOM, "user_id": BOT_UID,
        "user_name": BOT_NAME, "user_avatar": BOT_AVATAR,
        "content": (text or "")[:4000],
    })


def heartbeat():
    """生存記録。アプリ側はこの更新が最近なら『AI オンライン🟢』を表示する。"""
    try:
        from datetime import datetime, timezone
        api("PATCH", "ai_status?id=eq.1", {"updated_at": datetime.now(timezone.utc).isoformat()})
    except Exception:
        pass


def build_prompt(msgs):
    lines = []
    for m in msgs[-MAX_HIST:]:
        c = (m.get("content") or "").strip() or ("（画像を送信）" if m.get("image_url") else "")
        if not c:
            continue
        who = "アシスタント" if m.get("user_id") == BOT_UID else (m.get("user_name") or "家族")
        lines.append(f"{who}: {c}")
    return (SYS + "\n\n--- これまでの会話 ---\n" + "\n".join(lines) +
            "\n\n--- 指示 ---\n上の最後の発言に対する、アシスタントとしての返信だけを出力してください。")


def _claude_bin():
    """launchd 等の最小 PATH でも claude を見つけられるよう実体パスを解決。"""
    p = shutil.which("claude")
    if p:
        return p
    cands = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/claude")), reverse=True)
    cands += ["/opt/homebrew/bin/claude", "/usr/local/bin/claude",
              os.path.expanduser("~/.claude/local/claude"),
              os.path.expanduser("~/.local/bin/claude")]
    for c in cands:
        if os.path.exists(c):
            return c
    return "claude"

CLAUDE_BIN = _claude_bin()

def run_claude(prompt):
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--max-turns", "1"],
            cwd=WORKDIR, capture_output=True, text=True, timeout=180,
        )
        return (r.stdout or "").strip()
    except Exception as e:
        print("  claude error:", e)
        return ""


def main():
    print(f"[danran-bridge] 起動。ルーム『{ROOM}』を {POLL_SEC}s ごとに監視します。")
    msgs = fetch_recent()
    last_ts = parse_ts(msgs[-1]["created_at"]) if msgs else 0.0
    print(f"[danran-bridge] 既存 {len(msgs)} 件はスキップ。新着を待機中…（Ctrl+C で停止）")
    while True:
        try:
            heartbeat()   # 生存記録（オンラインランプ用）
            msgs = fetch_recent()
            if msgs:
                newest = msgs[-1]
                nts = parse_ts(newest.get("created_at"))
                if (newest.get("user_id") != BOT_UID and nts > last_ts
                        and (time.time() - nts) > SETTLE_SEC):
                    print(f"[danran-bridge] 新着 ← {newest.get('user_name')}: "
                          f"{(newest.get('content') or '(画像)')[:50]}")
                    reply = run_claude(build_prompt(msgs))
                    post_reply(reply or "⚠️ うまく応答できませんでした。もう一度試してください。")
                    print(f"[danran-bridge] 返信 → {(reply or '(エラー)')[:60]}")
                    last_ts = nts
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("[danran-bridge] err:", e)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[danran-bridge] 停止しました。")
