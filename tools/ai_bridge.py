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
import socket
import ssl
import subprocess
import sys
import threading
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

# ── イベント駆動の自動実装ループ ────────────────────────────────────────
OWNER_NAME      = "まさと"          # 破壊的変更の承認者・通知先（オーナー）
IMPLEMENT_ON    = True              # 自動実装ループの有効/無効スイッチ
IMPL_TIMEOUT    = 600              # 実装役 claude の最大実行秒
IMPL_LOCK       = threading.Lock()  # 同時に走る実装は1つだけ
STUCK_MIN       = 12                # implementing がこの分数を超えたら failed に回収
# 安全網: デプロイ後ヘルスチェック先（Streamlit の health エンドポイント）
APP_HEALTH_URL  = "https://danran-dhawa6nhapcwnq6lrjqzhw.streamlit.app/_stcore/health"
# 実装役が触ってよいファイル（破壊的変更の防波堤の一つ）
EDIT_ALLOW      = "app.py / components/longpress/index.html / sw.js / manifest.json / .streamlit/config.toml"
# 依頼者の「進めていい？」への合図（肯定/否定）
_AFFIRM = ("ok", "okay", "おk", "おけ", "おけー", "はい", "うん", "ええ", "いいよ", "いいね",
           "進めて", "すすめて", "おねがい", "お願い", "やって", "やろう", "ゴー", "go",
           "頼む", "たのむ", "よろしく", "よろ", "👍", "🙆", "🙏", "🆗", "✅", "💯")
_NEGATE = ("いや", "やめ", "だめ", "ダメ", "no", "キャンセル", "ちがう", "違う", "まだ", "保留")

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

# ── IPv4 優先（mini の SSL handshake timeout 対策）─────────────────────────
#   macOS で IPv6 経路が張れず TLS handshake がストールすることがある。bridge プロセスの
#   getaddrinfo を IPv4 だけに絞る（claude はサブプロセス＝別プロセスなので影響しない）。
_USE_IPV4_ONLY = True
if _USE_IPV4_ONLY:
    _orig_getaddrinfo = socket.getaddrinfo
    def _getaddrinfo_v4(host, *a, **kw):
        res = _orig_getaddrinfo(host, *a, **kw)
        v4 = [r for r in res if r[0] == socket.AF_INET]
        return v4 or res
    socket.getaddrinfo = _getaddrinfo_v4

# リトライ対象のネットワーク例外（HTTPError は別扱い＝4xx は再試行しない）
_RETRYABLE = (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError, urllib.error.URLError)


def api(method, path, body=None, tries=3):
    """Supabase REST 呼び出し。一時的なネットワーク/SSL エラーはバックオフ付きで再試行。
    HTTP 4xx（409=重複 等）は確定的なので再試行せず投げる。5xx は一時的とみて再試行。"""
    data = json.dumps(body).encode() if body is not None else None
    last_err = None
    for attempt in range(tries):
        req = urllib.request.Request(URL + "/rest/v1/" + path, data=data, headers=HDR, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600 and attempt < tries - 1:
                last_err = e; time.sleep(0.6 * (attempt + 1)); continue
            raise   # 4xx は即投げる（enqueue の 409 判定等が依存）
        except _RETRYABLE as e:
            last_err = e
            if attempt < tries - 1:
                time.sleep(0.6 * (attempt + 1))   # 0.6s → 1.2s バックオフ
                continue
            raise
    if last_err:
        raise last_err


def fetch_all_recent(n=80):
    """全ルームの直近メッセージ（新しい順）。"""
    q = ("messages?select=id,room_name,user_id,user_name,content,image_url,created_at"
         "&order=created_at.desc&limit=" + str(n))
    return api("GET", q) or []


def enqueue_task(msg, status="proposed", result=""):
    """依頼を ai_tasks に登録。status='proposed'（依頼者の合図待ち）/'needs_review'（破壊的→まさと確認）。
    source_message_id 一意制約で二重登録は弾く（409 は握りつぶす）。"""
    try:
        api("POST", "ai_tasks", {
            "room_name":         msg.get("room_name", ""),
            "source_message_id": msg.get("id"),
            "requester":         msg.get("user_name", ""),
            "request_text":      (msg.get("content") or "")[:2000],
            "status":            status,
            "result":            result or "",
        })
        print(f"[danran-bridge] 🧩 タスク登録({status}): {(msg.get('content') or '')[:40]}")
    except urllib.error.HTTPError as e:
        if e.code != 409:    # 409=既に登録済み（二重防止）→ 無視
            print("[danran-bridge] enqueue err:", e)
    except Exception as e:
        print("[danran-bridge] enqueue err:", e)


def set_task_status(task_id, status, result=None):
    try:
        body = {"status": status, "updated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat()}
        if result is not None:
            body["result"] = result[:2000]
        api("PATCH", "ai_tasks?id=eq." + str(task_id), body)
    except Exception as e:
        print("[danran-bridge] set_task_status err:", e)


def get_proposed_task(room):
    """その部屋で『合図待ち(proposed)』のタスク（最新1件・30分以内）を返す。無ければ None。"""
    try:
        q = ("ai_tasks?select=id,requester,request_text,created_at&room_name=eq."
             + urllib.parse.quote(room) + "&status=eq.proposed&order=created_at.desc&limit=1")
        rows = api("GET", q) or []
        if not rows:
            return None
        t = rows[0]
        if (time.time() - parse_ts(t.get("created_at"))) > 1800:   # 30分超は無効
            return None
        return t
    except Exception:
        return None


# ── オーナー（まさと）への通知: Web Push ＋ AIサポートへの記録 ──
_owner_uid_cache = {"v": None}
def get_owner_uid():
    if _owner_uid_cache["v"] is None:
        try:
            rows = api("GET", "users?select=id&name=eq." + urllib.parse.quote(OWNER_NAME) + "&limit=1") or []
            _owner_uid_cache["v"] = rows[0]["id"] if rows else ""
        except Exception:
            _owner_uid_cache["v"] = ""
    return _owner_uid_cache["v"]

def _vapid():
    p = _sec.get("push", {}) or {}
    return p.get("vapid_private_key", ""), p.get("vapid_subject", "")

def push_to_owner(title, body):
    """まさとの全購読に Web Push を送る（pywebpush は venv に導入済み）。"""
    try:
        from pywebpush import webpush, WebPushException
        priv, subj = _vapid()
        uid = get_owner_uid()
        if not (priv and subj and uid):
            return
        subs = api("GET", "push_subscriptions?select=endpoint,p256dh,auth&user_id=eq." + uid) or []
        payload = json.dumps({"title": title, "body": body[:160], "room": ROOM, "url": "/"},
                             ensure_ascii=False)
        for s in subs:
            try:
                webpush(subscription_info={"endpoint": s["endpoint"],
                                           "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                        data=payload, vapid_private_key=priv, vapid_claims={"sub": subj})
            except Exception:
                pass
    except Exception as e:
        print("[danran-bridge] push_to_owner err:", e)

def notify_owner(title, body, to_support=True):
    """まさとに通知: Web Push ＋（任意で）AIサポートルームに記録。"""
    push_to_owner(title, body)
    if to_support:
        try:
            post_reply(f"🔔 まさとへ: {body}", room=ROOM)
        except Exception:
            pass
    print(f"[danran-bridge] 🔔 notify_owner: {title} / {body[:60]}")


def split_flags(text):
    """claude 返信末尾の `TASK:` / `DESTRUCTIVE:` 行を取り出して本文から除去。
    戻り: (家族に見せる本文, is_task bool, is_destructive bool)。"""
    is_task = False
    is_destr = False
    lines = (text or "").rstrip().split("\n")
    # 末尾から最大2行ぶんのフラグを剥がす
    for _ in range(2):
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            break
        last = lines[-1]
        m = re.match(r"\s*task\s*[:：]\s*(yes|no|はい|いいえ)\s*$", last, re.I)
        if m:
            is_task = m.group(1).lower() in ("yes", "はい"); lines.pop(); continue
        m = re.match(r"\s*destructive\s*[:：]\s*(yes|no|はい|いいえ)\s*$", last, re.I)
        if m:
            is_destr = m.group(1).lower() in ("yes", "はい"); lines.pop(); continue
        break
    return ("\n".join(lines).strip(), is_task, is_destr)

def is_affirmative(text):
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(k in t for k in _AFFIRM)

def is_negative(text):
    t = (text or "").strip().lower()
    return any(k in t for k in _NEGATE)

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
    # 末尾に必ず付けるフラグ（家族には非表示で除去する）
    flag_rule = (
        "\n\n--- 最後に必ず（家族には表示されません）---\n"
        "返信本文の後に、次の2行をこの順で必ず付けてください:\n"
        "TASK: yes/no        ← danran のコード変更（バグ修正・UI改善・機能追加）が必要なら yes\n"
        "DESTRUCTIVE: yes/no ← 破壊的/要注意（DB削除・スキーマ変更・認証/パスワード/課金/通知鍵まわり・"
        "ファイル削除・大規模リファクタ・依存追加 等）なら yes。単純なUI/文言/小バグ/小機能なら no"
    )
    # 実装依頼への振る舞い: yes かつ非破壊なら「こう直す、進めていい？」と確認を促す。
    impl_rule = (
        "\n\n--- 実装依頼への返し方 ---\n"
        "コード変更が必要そうな依頼には、何をどう直すかを1〜2文で具体的に示し、最後に"
        "『この内容で進めていい？OKか👍で教えてね』と確認を促してください（あなたが直接コードを書くのではなく、"
        "OKをもらってから自動で実装されます）。破壊的/要注意な依頼は『これは念のためまさとに確認するね』と伝えてください。"
    )
    if ai_room:
        return (SYS + impl_rule + "\n\n--- これまでの会話 ---\n" + convo +
                "\n\n--- 指示 ---\n上の最後の発言への、サポートAIとしての返信だけを出力してください。" +
                flag_rule)
    # 通常ルームで @AI 呼びかけに答える場合
    guest = (
        "あなたは家族チャットアプリ danran の AI アシスタントです。家族の会話の中で誰かが"
        "「@AI」と呼びかけました。直近の会話の流れを踏まえて、日本語で簡潔・親しみやすく答えてください。"
        "アプリ名は半角『danran』。マークダウン記法は使わない。返信テキストだけを出力してください。"
    )
    return (guest + impl_rule + "\n\n--- 会話 ---\n" + convo +
            "\n\n--- 指示 ---\n@AI への呼びかけに答えてください。" + flag_rule)


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


# ── 実装役 claude（ツール使用・編集→構文チェック→commit→push）──────────────
IMPL_SYS = (
    "あなたは家族チャットアプリ danran のリポジトリ（カレントディレクトリ）で作業する実装エージェントです。"
    "次の依頼を、最小限・低リスクに実装してください。\n\n"
    "【厳守ルール】\n"
    "1. 触ってよいファイルは " + EDIT_ALLOW + " のみ。tools/ や .git 設定や .streamlit/secrets.toml は触らない。\n"
    "2. 破壊的・要注意な操作は禁止: DBスキーマ/データ変更, 認証・セッション・パスワード・課金・"
    "通知(VAPID)鍵まわりの変更, ファイル削除, 大規模リファクタ, 依存パッケージ追加, rm 等。"
    "これらが必要だと判明したら、コードを変更せず最終行に『NEEDS_OWNER: 理由』とだけ書いて終了する。\n"
    "3. 変更後は必ず構文チェック: python3 -c \"import ast; ast.parse(open('app.py').read())\"。"
    "components/longpress/index.html を変更した場合は <script> を取り出して node --check も通し、"
    "さらに app.py の declare_component 名 danran_lp_vNN の数字を必ず +1 する。\n"
    "4. チェックが通ったら git add -A して git commit（日本語で簡潔なメッセージ）し、git push origin main する。\n"
    "5. シークレットや鍵を絶対に出力・コミットしない。\n"
    "6. 最後に、家族向けに『何をどう変えたか』を1〜2文・日本語・プレーンテキスト（記号装飾なし）で要約して出力する。"
)

IMPL_ALLOWED = ("Edit,Write,Read,Glob,Grep,"
                "Bash(git add:*),Bash(git commit:*),Bash(git push:*),Bash(git status:*),"
                "Bash(git diff:*),Bash(git log:*),Bash(git rev-parse:*),Bash(git fetch:*),"
                "Bash(python3:*),Bash(node:*),Bash(ls:*),Bash(cat:*),Bash(grep:*),Bash(sed:*),Bash(rg:*)")

def _git(*a):
    return subprocess.run(["git", *a], cwd=REPO_DIR, capture_output=True, text=True, timeout=60)

def _compile_guard():
    """push 済みツリーの app.py がコンパイル（構文）OK か。実装役の自己チェックすり抜け対策。"""
    try:
        r = subprocess.run([sys.executable, "-m", "py_compile", "app.py"],
                           cwd=REPO_DIR, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return (False, "app.py 構文エラー: " + (r.stderr or "")[-200:])
        return (True, "")
    except Exception as e:
        return (True, "")   # チェック自体が失敗したらロールバックはしない（誤爆防止）

def _http_ok(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "danran-bridge"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = (r.read(64) or b"").decode("utf-8", "ignore").lower()
            return getattr(r, "status", 200) == 200 and ("ok" in body or body.strip() == "")
    except Exception:
        return False

def storage_api(method, path, body=None, tries=3):
    """Supabase Storage REST 呼び出し（/storage/v1/...）。api() と同じくリトライ付き。"""
    data = json.dumps(body).encode() if body is not None else None
    last_err = None
    for attempt in range(tries):
        req = urllib.request.Request(URL + "/storage/v1/" + path, data=data, headers=HDR, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600 and attempt < tries - 1:
                last_err = e; time.sleep(0.6 * (attempt + 1)); continue
            raise
        except _RETRYABLE as e:
            last_err = e
            if attempt < tries - 1:
                time.sleep(0.6 * (attempt + 1)); continue
            raise
    if last_err:
        raise last_err

def _obj_name_from_url(url, bucket):
    """公開URL …/object/public/{bucket}/{name} から name を取り出す。"""
    if not url:
        return ""
    marker = "/" + bucket + "/"
    i = url.find(marker)
    if i < 0:
        return ""
    return url[i + len(marker):].split("?")[0]

ORPHAN_GRACE_H = 24   # アップロード直後（メッセージ未挿入の窓）を消さない猶予

def _sweep_bucket(bucket, referenced):
    """bucket の孤児（referenced に無く ORPHAN_GRACE_H 時間より古い）を削除。削除数を返す。"""
    deleted = 0
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ORPHAN_GRACE_H)
        offset = 0
        orphans = []
        while True:
            page = storage_api("POST", "object/list/" + urllib.parse.quote(bucket),
                               {"prefix": "", "limit": 1000, "offset": offset,
                                "sortBy": {"column": "name", "order": "asc"}}) or []
            if not page:
                break
            for o in page:
                nm = o.get("name", "")
                if not nm or nm in referenced:
                    continue
                ca = o.get("created_at") or o.get("updated_at") or ""
                try:
                    dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                except Exception:
                    dt = None
                if dt is None or dt < cutoff:   # 日付不明も猶予超えとみなして対象
                    orphans.append(nm)
            if len(page) < 1000:
                break
            offset += 1000
        # まとめて削除（prefixes 指定）。50件ずつ。
        for i in range(0, len(orphans), 50):
            chunk = orphans[i:i + 50]
            try:
                storage_api("DELETE", "object/" + urllib.parse.quote(bucket), {"prefixes": chunk})
                deleted += len(chunk)
            except Exception as e:
                print("[danran-bridge] orphan delete err:", e)
    except Exception as e:
        print("[danran-bridge] sweep err (%s):" % bucket, e)
    return deleted

def orphan_sweep():
    """孤児ファイル（削除済みメッセージ/ルームの画像など）を Storage から自動掃除。日次。"""
    try:
        # chat-images: messages.image_url で参照されている name 集合
        refs_img = set()
        rows = api("GET", "messages?select=image_url") or []
        for m in rows:
            n = _obj_name_from_url(m.get("image_url"), "chat-images")
            if n:
                refs_img.add(n)
        d1 = _sweep_bucket("chat-images", refs_img)

        # avatars: users.avatar / rooms.icon が指す name 集合
        refs_av = set()
        for tbl, col in (("users", "avatar"), ("rooms", "icon")):
            try:
                rr = api("GET", "%s?select=%s" % (tbl, col)) or []
                for r in rr:
                    n = _obj_name_from_url(r.get(col), "avatars")
                    if n:
                        refs_av.add(n)
            except Exception:
                pass
        d2 = _sweep_bucket("avatars", refs_av)

        if d1 or d2:
            print(f"[danran-bridge] 🧹 孤児掃除: chat-images={d1} / avatars={d2} 件削除")
            notify_owner("danran 孤児ファイル掃除",
                         f"未使用ファイルを削除しました（画像{d1}・アイコン{d2}）。",
                         to_support=False)
        else:
            print("[danran-bridge] 🧹 孤児掃除: 対象なし")
    except Exception as e:
        print("[danran-bridge] orphan_sweep err:", e)

def reclaim_stuck_tasks():
    """mini が実装中に落ちる等で implementing のまま固着したタスクを failed に戻す。"""
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STUCK_MIN)).isoformat()
        stuck = api("GET", "ai_tasks?select=id&status=eq.implementing&updated_at=lt."
                    + urllib.parse.quote(cutoff)) or []
        for t in stuck:
            # STUCK_MIN(12分) > IMPL_TIMEOUT(10分) なので、ここに来る＝実装フローは既に終了/死亡。
            # ロックは同一プロセス内で finally 解放済み or プロセスごと消滅しているため触らない。
            set_task_status(t["id"], "failed", "実装プロセスが途中で停止（自動回収）")
            print("[danran-bridge] ♻ 固着タスクを回収:", t["id"])
    except Exception:
        pass

def post_deploy_healthcheck(room):
    """デプロイ後、アプリが応答するか確認（通知のみ・自動ロールバックはしない＝誤爆防止）。"""
    try:
        time.sleep(150)   # Streamlit 再デプロイを待つ
        for _ in range(4):
            if _http_ok(APP_HEALTH_URL):
                return
            time.sleep(20)
        notify_owner("danran デプロイ後 応答なし",
                     f"[{room}] 直近の自動実装の後、アプリの health チェックが通りません。確認して。")
    except Exception:
        pass

def run_implementer(request_text, convo):
    """実装役 claude を起動して実装→push まで行う。戻り: (status, summary)
       status: 'done'（push 済み）/ 'needs_owner'（破壊的で保留）/ 'failed'。"""
    head_before = _git("rev-parse", "HEAD").stdout.strip()
    prompt = (IMPL_SYS + "\n\n--- 依頼 ---\n" + (request_text or "") +
              "\n\n--- 参考: 直近の会話 ---\n" + (convo or "") +
              "\n\n--- 指示 ---\n上の依頼を厳守ルールに従って実装し、最後に家族向けの要約を1〜2文で出力してください。")
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--max-turns", "60",
             "--permission-mode", "acceptEdits", "--allowedTools", IMPL_ALLOWED],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=IMPL_TIMEOUT)
        out = (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return ("failed", "実装がタイムアウトしました")
    except Exception as e:
        return ("failed", f"実装の起動に失敗: {e}")

    if "NEEDS_OWNER" in out:
        reason = out.split("NEEDS_OWNER", 1)[1].lstrip(": ：").strip()[:300]
        return ("needs_owner", reason or "破壊的/要注意のため保留")

    def _finalize_done():
        # 安全網: push 済みツリーが壊れていたら自動ロールバック
        ok, err = _compile_guard()
        if not ok:
            _git("revert", "--no-edit", "HEAD")
            _git("push", "origin", "main")
            _git("fetch", "origin", "main")
            return ("reverted", err)
        return ("done", out[-500:].strip() or "実装してデプロイしました")

    # 実際に push されたか検証（HEAD が進み、origin/main に乗ったか）
    _git("fetch", "origin", "main")
    head_after = _git("rev-parse", "HEAD").stdout.strip()
    origin = _git("rev-parse", "origin/main").stdout.strip()
    if head_after and head_after != head_before and head_after == origin:
        return _finalize_done()
    # コミットはあるが push できていない等
    if head_after != head_before:
        _git("push", "origin", "main")
        _git("fetch", "origin", "main")
        if _git("rev-parse", "HEAD").stdout.strip() == _git("rev-parse", "origin/main").stdout.strip():
            return _finalize_done()
    return ("failed", "コード変更が確認できませんでした（実装されなかった可能性）")


def _group_by_room(rows):
    """desc 取得の rows を room_name → 昇順メッセージ列 にまとめる。"""
    by = {}
    for m in rows:
        by.setdefault(m.get("room_name", ""), []).append(m)
    for rn in by:
        by[rn] = list(reversed(by[rn]))   # 昇順
    return by


def implement_flow(task, room):
    """依頼者の合図を受けて実装役 claude を起動し、結果をルームに返す（別スレッド）。
    IMPL_LOCK は呼び出し側が取得済み。ここで必ず release する。"""
    try:
        set_task_status(task["id"], "implementing")
        post_reply("🛠 実装を始めるね。少し待ってて（1〜2分くらい）", room)
        # 直近会話（実装役に文脈として渡す）
        msgs = _group_by_room(fetch_all_recent()).get(room, [])
        lines = []
        for m in msgs[-MAX_HIST:]:
            c = (m.get("content") or "").strip()
            if not c:
                continue
            who = "アシスタント" if m.get("user_id") == BOT_UID else (m.get("user_name") or "家族")
            lines.append(f"{who}: {c}")
        convo = "\n".join(lines)

        status, summary = run_implementer(task.get("request_text", ""), convo)
        req = task.get("requester", "")
        if status == "done":
            set_task_status(task["id"], "done", summary)
            post_reply("✅ 直したよ！" + (("\n" + summary) if summary else "") +
                       "\nアプリ更新済み（1〜2分で反映、出ないときは再起動してね）", room)
            notify_owner("danran 自動実装", f"[{room}] {req} の依頼を実装＆デプロイ: {summary[:120]}")
            # デプロイ後ヘルスチェック（別スレッド・通知のみ）
            threading.Thread(target=post_deploy_healthcheck, args=(room,), daemon=True).start()
        elif status == "reverted":
            set_task_status(task["id"], "failed", "デプロイ後に構文エラーを検知し自動ロールバック: " + summary)
            post_reply("ごめん、変更でエラーが出たので自動で元に戻したよ。まさとに見てもらうね🙏", room)
            notify_owner("danran 自動ロールバック",
                         f"[{room}] {task.get('request_text','')[:60]} → {summary[:120]}")
        elif status == "needs_owner":
            set_task_status(task["id"], "needs_review", summary)
            post_reply("ごめん、これは念のためまさとに確認してから対応するね🙏", room)
            notify_owner("danran 要確認", f"[{room}] {req}: {task.get('request_text','')[:80]} → 保留: {summary[:120]}")
        else:
            set_task_status(task["id"], "failed", summary)
            post_reply("うまく実装できなかったみたい…まさとに見てもらうね🙏", room)
            notify_owner("danran 実装失敗", f"[{room}] {task.get('request_text','')[:80]} → {summary[:120]}")
        print(f"[danran-bridge] 実装完了({status}) [{room}]: {summary[:60]}")
    except Exception as e:
        print("[danran-bridge] implement_flow err:", e)
        try:
            set_task_status(task["id"], "failed", str(e)[:300])
        except Exception:
            pass
    finally:
        try:
            IMPL_LOCK.release()
        except Exception:
            pass

def main():
    print(f"[danran-bridge] 起動。全ルームを {POLL_SEC}s ごとに監視（AIサポート＝常時 / 他＝@AI 呼びかけ時）")
    # 起動時の各ルーム最新時刻＝バックログ無視の基準
    last_by_room = {}
    for rn, msgs in _group_by_room(fetch_all_recent()).items():
        if msgs:
            last_by_room[rn] = parse_ts(msgs[-1].get("created_at"))
    print(f"[danran-bridge] 既存はスキップ。新着を待機中…（Ctrl+C で停止）")
    _last_reclaim = 0.0
    _last_sweep   = 0.0   # 0 = 起動直後に1回 → 以後24時間ごと
    while True:
        try:
            heartbeat()
            if time.time() - _last_reclaim > 60:   # 固着タスク回収は約60秒ごと
                reclaim_stuck_tasks()
                _last_reclaim = time.time()
            if time.time() - _last_sweep > 86400:  # 孤児ファイル掃除は1日ごと（別スレッド）
                _last_sweep = time.time()
                threading.Thread(target=orphan_sweep, daemon=True).start()
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
                content = newest.get("content") or ""
                sender  = newest.get("user_name", "")

                # ── 1) 合図待ち(proposed)タスクの確認（@AI 不要・どの部屋でも）──
                if IMPLEMENT_ON:
                    prop = get_proposed_task(rn)
                    if prop and sender == prop.get("requester", ""):
                        if is_affirmative(content):
                            if IMPL_LOCK.acquire(blocking=False):
                                print(f"[danran-bridge] ✅ 合図受領 → 実装開始 [{rn}] {sender}")
                                threading.Thread(target=implement_flow, args=(prop, rn),
                                                 daemon=True).start()
                            else:
                                post_reply("いま別の対応をしてるんだ、終わったらやるね🙏", rn)
                            last_by_room[rn] = nts
                            continue
                        if is_negative(content):
                            set_task_status(prop["id"], "skipped", "依頼者が見送り")
                            post_reply("おっけー、やめておくね。また必要になったら言ってね", rn)
                            last_by_room[rn] = nts
                            continue
                        # 肯定でも否定でもない → 下の通常処理（新たな依頼/雑談かも）

                # ── 2) 新規の問い合わせ/依頼（AIサポートは常時 / 他ルームは @AI）──
                is_ai_room = (rn == ROOM)
                if not is_ai_room and not mentions_ai(content):
                    last_by_room[rn] = nts
                    continue
                print(f"[danran-bridge] 新着 ← [{rn}] {sender}: {(content or '(画像)')[:50]}")
                prompt = build_prompt(msgs, is_ai_room)
                imgs = download_images(msgs)   # 直近の画像があれば落として Vision で見せる
                if imgs:
                    prompt += ("\n\n--- 添付画像 ---\n次の画像ファイルを Read ツールで開いて"
                               "内容を確認し、回答に反映してください: " + ", ".join(imgs))
                reply = run_claude(prompt, has_images=bool(imgs))
                cleanup_tmp()
                # 末尾の TASK / DESTRUCTIVE フラグを取り出し、本文からは除去して家族に見せる
                clean, is_task, is_destr = split_flags(reply)
                post_reply(clean or "⚠️ うまく応答できませんでした。もう一度試してください。", rn)
                if is_task and IMPLEMENT_ON:
                    if is_destr:
                        enqueue_task(newest, status="needs_review", result=clean[:500])
                        notify_owner("danran 要確認(破壊的)",
                                     f"[{rn}] {sender}: {content[:80]}")
                    else:
                        enqueue_task(newest, status="proposed", result=clean[:500])
                elif is_task:
                    enqueue_task(newest)   # 自動実装オフ時は従来どおりキューに積むだけ
                print(f"[danran-bridge] 返信 → [{rn}]{' [img]' if imgs else ''}"
                      f"{' [task]' if is_task else ''}{' [destr]' if is_destr else ''} "
                      f"{(clean or '(エラー)')[:60]}")
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
