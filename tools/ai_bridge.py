#!/usr/bin/env python3
"""
danran AIサポート bridge — Claude Code CLI（Max プラン）をチャットルームに接続する。

仕組み:
  - Supabase の全ルームを数秒ごとに監視（AIサポート＝常時 / 他ルーム＝@AI 呼びかけ時）
  - 新しいユーザー発言が来たら、ローカルの `claude -p`（ヘッドレス）で返信を生成
  - その返信をボット（🤖 アシスタント）として Supabase に投稿

Claude Code との協調（実装ループ）:
  - claude の返信末尾に「TASK: yes/no」を自己申告させ、yes（＝コード変更が要る依頼）なら
    その発言を Supabase の共有キュー public.ai_tasks に status='pending' で積む。
  - まさとの Claude Code（cron）が pending を拾って実装→push→その部屋に「✅実装しました」を投稿し
    タスクを done に更新する。bridge は受付＆トリアージ、Claude Code は実装担当、という分業。
  - bridge 自身はコードを変更しない（会話のみ）。TASK 行は家族には表示せず除去する。

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
import urllib.error
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
    "- 返信テキストだけを出力する。ファイル編集やコマンド実行はしない（添付画像を確認する"
    "ための読み取りだけは可）。\n"
    "- 返信は数行で簡潔に、絵文字は控えめに。\n"
    "バグ報告や機能の要望は受け止めて、必要なら『どの画面で・何をしたら・どうなったか』を1つだけ簡潔に質問してください。\n"
    "実装が必要なバグ修正・機能追加は、まさとのClaude Codeが引き継いで対応し、できたらこの部屋でお知らせします"
    "（あなた自身はコードを変更しません。安心させる一言を添えてOK）。"
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


def fetch_all_recent(n=80):
    """全ルームの直近メッセージ（新しい順）。"""
    q = ("messages?select=id,room_name,user_id,user_name,content,image_url,created_at"
         "&order=created_at.desc&limit=" + str(n))
    return api("GET", q) or []


def enqueue_task(msg):
    """実装が要る依頼を ai_tasks キューに積む（Claude Code が拾って実装する）。
    source_message_id 一意制約で二重登録は弾く（409 は握りつぶす）。"""
    try:
        api("POST", "ai_tasks", {
            "room_name":         msg.get("room_name", ""),
            "source_message_id": msg.get("id"),
            "requester":         msg.get("user_name", ""),
            "request_text":      (msg.get("content") or "")[:2000],
            "status":            "pending",
        })
        print(f"[danran-bridge] 🧩 タスク登録 → ai_tasks: {(msg.get('content') or '')[:40]}")
    except urllib.error.HTTPError as e:
        if e.code != 409:    # 409=既に登録済み（二重防止）→ 無視
            print("[danran-bridge] enqueue err:", e)
    except Exception as e:
        print("[danran-bridge] enqueue err:", e)


def split_task_flag(text):
    """claude 返信末尾の `TASK: yes/no` 行を取り出して本文から除去。
    戻り: (家族に見せる本文, 実装が必要か bool)。"""
    is_task = False
    lines = (text or "").rstrip().split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        m = re.match(r"\s*task\s*[:：]\s*(yes|no|はい|いいえ)\s*$", lines[-1], re.I)
        if m:
            is_task = m.group(1).lower() in ("yes", "はい")
            lines.pop()
    return ("\n".join(lines).strip(), is_task)

def mentions_ai(text):
    """本文に @AI / ＠AI（大小文字無視）が含まれるか。"""
    t = (text or "").lower()
    return ("@ai" in t) or ("＠ai" in t)


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


def post_reply(text, room=ROOM):
    api("POST", "messages", {
        "room_name": room, "user_id": BOT_UID,
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


def build_prompt(msgs, ai_room=True):
    lines = []
    for m in msgs[-MAX_HIST:]:
        c = (m.get("content") or "").strip() or ("（画像を送信）" if m.get("image_url") else "")
        if not c:
            continue
        who = "アシスタント" if m.get("user_id") == BOT_UID else (m.get("user_name") or "家族")
        lines.append(f"{who}: {c}")
    convo = "\n".join(lines)
    # 末尾に必ず付ける「実装要否」の自己申告（Claude Code への引き継ぎ判定に使う）
    task_rule = (
        "\n\n--- 最後に必ず ---\n"
        "返信本文の後、最終行に1行だけ次の形式で実装要否を書いてください（家族には表示しません）:\n"
        "TASK: yes  ← danran のコード変更（バグ修正・UI改善・機能追加）が必要な依頼のとき\n"
        "TASK: no   ← 使い方の質問・雑談・既に直っている等、コード変更が不要なとき"
    )
    if ai_room:
        return (SYS + "\n\n--- これまでの会話 ---\n" + convo +
                "\n\n--- 指示 ---\n上の最後の発言に対する、サポートAIとしての返信だけを出力してください。" +
                task_rule)
    # 通常ルームで @AI 呼びかけに答える場合
    guest = (
        "あなたは家族チャットアプリ danran の AI アシスタントです。家族の会話の中で誰かが"
        "「@AI」と呼びかけました。直近の会話の流れを踏まえて、その呼びかけに日本語で簡潔・"
        "親しみやすく答えてください。アプリ名は半角『danran』。マークダウン記法は使わない。"
        "実装が必要な依頼なら『まさとのClaude Codeが対応して、できたらこの部屋でお知らせします』と"
        "一言添えてください（あなた自身はコードを変更しません）。返信テキストだけを出力してください。"
    )
    return (guest + "\n\n--- 会話 ---\n" + convo +
            "\n\n--- 指示 ---\n@AI への呼びかけに答えてください。" + task_rule)


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

AI_TMP = os.path.join(REPO_DIR, "tools", ".ai_tmp")   # 画像の一時保存（gitignore）

def download_images(msgs, max_n=3):
    """直近メッセージの画像を最大 max_n 枚 tools/.ai_tmp に落とし、相対パス一覧を返す。"""
    rels = []
    try:
        os.makedirs(AI_TMP, exist_ok=True)
        urls = []
        for m in reversed(msgs):       # 新しい順に集める
            u = m.get("image_url")
            if u:
                urls.append(u)
            if len(urls) >= max_n:
                break
        for i, u in enumerate(reversed(urls)):   # 会話順（古→新）に保存
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "danran-bridge"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                ext = ".png" if u.lower().split("?")[0].endswith(".png") else ".jpg"
                p = os.path.join(AI_TMP, f"img_{i}{ext}")
                with open(p, "wb") as f:
                    f.write(data)
                rels.append(os.path.relpath(p, REPO_DIR))
            except Exception:
                pass
    except Exception:
        pass
    return rels

def cleanup_tmp():
    try:
        for f in glob.glob(os.path.join(AI_TMP, "*")):
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception:
        pass

def run_claude(prompt, has_images=False):
    try:
        args = [CLAUDE_BIN, "-p", prompt, "--max-turns", "3" if has_images else "1"]
        if has_images:
            args += ["--allowedTools", "Read"]   # 画像を Read で見られるように
        r = subprocess.run(args, cwd=WORKDIR, capture_output=True, text=True, timeout=240)
        return (r.stdout or "").strip()
    except Exception as e:
        print("  claude error:", e)
        return ""


def _group_by_room(rows):
    """desc 取得の rows を room_name → 昇順メッセージ列 にまとめる。"""
    by = {}
    for m in rows:
        by.setdefault(m.get("room_name", ""), []).append(m)
    for rn in by:
        by[rn] = list(reversed(by[rn]))   # 昇順
    return by

def main():
    print(f"[danran-bridge] 起動。全ルームを {POLL_SEC}s ごとに監視（AIサポート＝常時 / 他＝@AI 呼びかけ時）")
    # 起動時の各ルーム最新時刻＝バックログ無視の基準
    last_by_room = {}
    for rn, msgs in _group_by_room(fetch_all_recent()).items():
        if msgs:
            last_by_room[rn] = parse_ts(msgs[-1].get("created_at"))
    print(f"[danran-bridge] 既存はスキップ。新着を待機中…（Ctrl+C で停止）")
    while True:
        try:
            heartbeat()
            for rn, msgs in _group_by_room(fetch_all_recent()).items():
                if not msgs:
                    continue
                newest = msgs[-1]
                nts = parse_ts(newest.get("created_at"))
                if nts <= last_by_room.get(rn, 0):
                    continue
                # ボット自身の発言 → 既読扱いにして次へ
                if newest.get("user_id") == BOT_UID:
                    last_by_room[rn] = nts
                    continue
                # 連投が落ち着くまで待つ（まだ待つなら last は更新しない＝次ループで再判定）
                if (time.time() - nts) <= SETTLE_SEC:
                    continue
                is_ai_room = (rn == ROOM)
                # 通常ルームは @AI 呼びかけ時のみ反応
                if not is_ai_room and not mentions_ai(newest.get("content")):
                    last_by_room[rn] = nts
                    continue
                print(f"[danran-bridge] 新着 ← [{rn}] {newest.get('user_name')}: "
                      f"{(newest.get('content') or '(画像)')[:50]}")
                prompt = build_prompt(msgs, is_ai_room)
                imgs = download_images(msgs)   # 直近の画像があれば落として Vision で見せる
                if imgs:
                    prompt += ("\n\n--- 添付画像 ---\n次の画像ファイルを Read ツールで開いて"
                               "内容を確認し、回答に反映してください: " + ", ".join(imgs))
                reply = run_claude(prompt, has_images=bool(imgs))
                cleanup_tmp()
                # 末尾の TASK: yes/no を取り出し、本文からは除去して家族に見せる
                clean, is_task = split_task_flag(reply)
                post_reply(clean or "⚠️ うまく応答できませんでした。もう一度試してください。", rn)
                if is_task:
                    enqueue_task(newest)   # 実装が要る依頼 → Claude Code 用キューへ
                print(f"[danran-bridge] 返信 → [{rn}]{' [img]' if imgs else ''}"
                      f"{' [task]' if is_task else ''} {(clean or '(エラー)')[:60]}")
                last_by_room[rn] = nts
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
