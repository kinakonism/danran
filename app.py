"""
danran - 家族専用チャットアプリ  Streamlit × Supabase
セッション: Supabase sessions + URL ?s=SESSION_ID
リアルタイム: @st.fragment(run_every="2s")
通知: PWA Web Push (static/ → /app/static/ で配信、enableStaticServing=true)
"""

import html as _html
import io
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone, timedelta

import bcrypt
import httpx
import streamlit as st
from PIL import Image, ImageOps
from supabase import create_client, Client

# ─────────────────────────────────────
# ページ設定
# ─────────────────────────────────────
st.set_page_config(
    page_title="danran 🏠",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Streamlit のツールバー・サイドバーを非表示
st.markdown("""
<style>
/* ── Streamlit chrome を非表示 ── */
/* stHeader はコンテナごと非表示（z-index:999990 でカスタムヘッダーを覆うため）*/
[data-testid="stHeader"]           { display: none !important; }
[data-testid="stToolbar"]          { display: none !important; }
[data-testid="stDecoration"]       { display: none !important; }
[data-testid="stSidebar"]          { display: none !important; }
[data-testid="collapsedControl"]   { display: none !important; }
#MainMenu                          { display: none !important; }
[data-testid="stDeployButton"]     { display: none !important; }
[data-testid="stActionButton"]     { display: none !important; }
[data-testid="stStatusWidget"]     { display: none !important; }
[class*="viewerBadge"]             { display: none !important; }
a[href*="streamlit.io"]            { display: none !important; }
/* Streamlit Cloud status embed (statuspage.io iframe) */
iframe[src*="statuspage.io"]       { display: none !important; }
iframe[src*="streamlitstatus"]     { display: none !important; }
iframe[title*="Streamlit Cloud"]   { display: none !important; }
/* embed モードが chat input を隠す場合の保険 */
[data-testid="stBottom"]           { display: block !important; visibility: visible !important; }
[data-testid="stChatInput"]        { display: flex !important; visibility: visible !important; }
/* コンテンツの余白調整 */
[data-testid="stMainBlockContainer"] > div:first-child { padding-top: 0.5rem; }
/* ── スワイプ戻りのスライドイン演出 ── */
@keyframes danranSlideInLeft {
  from { transform: translateX(-32px); opacity: 0.3; }
  to   { transform: translateX(0);     opacity: 1;   }
}
/* ── ボタン押下フィードバック（タップ即応感）── */
#_danran_hdr button:active { background: rgba(255,255,255,0.14) !important; border-radius: 10px; }
#_danran_room_list button[data-room-gear]:active,
#_danran_room_list button[data-room-create]:active { background: rgba(255,255,255,0.12) !important; }
#_danran_hdr button:active,
#_danran_room_list button:active { transform: scale(0.94); transition: transform 0.06s; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
ROOMS_FALLBACK: list[str] = ["家族みんな", "連絡事項", "おでかけ計画", "料理・レシピ"]
REACTION_EMOJIS   = ["👍", "❤️", "😂", "😲", "🙏"]
AVATAR_BUCKET     = "avatars"
CHAT_IMG_BUCKET   = "chat-images"
SESSION_PARAM     = "s"
JST               = timezone(timedelta(hours=9))

# ─────────────────────────────────────
# Supabase クライアント
# ─────────────────────────────────────
@st.cache_resource
def get_supabase() -> Client:
    # st.secrets（ローカル / Streamlit Cloud）→ 環境変数（Render）の順で取得
    try:
        url = (
            (st.secrets.get("supabase") or {}).get("url")
            or os.environ.get("SUPABASE_URL", "")
        )
        key = (
            (st.secrets.get("supabase") or {}).get("anon_key")
            or os.environ.get("SUPABASE_ANON_KEY", "")
        )
    except Exception:
        url, key = "", ""
    if not url or not key:
        st.error("⚠️ Supabase の設定が見つかりません。Streamlit Cloud の Secrets に [supabase] url と anon_key を設定してください。")
        st.stop()
    return create_client(url, key)

supabase = get_supabase()

# ─────────────────────────────────────
# ルーム DB（DB に rooms テーブルがある場合は取得、なければフォールバック）
# ─────────────────────────────────────
@st.cache_data(ttl=60)
def fetch_rooms(user_id: str = "") -> list[dict]:
    """ルーム一覧 (id, name, icon) を取得。
    user_id を渡すと room_members で「参加ルームのみ」に絞る（招待制）。
    user_id="" は全ルーム（重複名チェック等の管理用途）。"""
    try:
        if user_id:
            mrows = supabase.table("room_members").select("room_id")\
                .eq("user_id", user_id).execute().data or []
            rids = [m["room_id"] for m in mrows]
            if not rids:
                return []                      # 参加ルームなし＝空（招待待ち）
            return supabase.table("rooms").select("id, name, icon")\
                .in_("id", rids).order("created_at").execute().data or []
        # user_id なし: 全ルーム
        data = supabase.table("rooms").select("id, name, icon").order("created_at").execute().data or []
        if data:
            return data
    except Exception:
        pass
    return [{"id": r, "name": r, "icon": "💬"} for r in ROOMS_FALLBACK]

def invalidate_rooms_cache() -> None:
    """rooms キャッシュを破棄（更新・削除・メンバー変更後に必ず呼ぶ）。"""
    fetch_rooms.clear()

# ─────────────────────────────────────
# ルームメンバー（招待制）
# ─────────────────────────────────────
def fetch_room_members(room_id: str) -> list[dict]:
    """ルームの参加メンバー (id, name, avatar) を返す。"""
    try:
        mrows = supabase.table("room_members").select("user_id")\
            .eq("room_id", room_id).execute().data or []
        uids = [m["user_id"] for m in mrows]
        if not uids:
            return []
        return supabase.table("users").select("id, name, avatar")\
            .in_("id", uids).order("created_at").execute().data or []
    except Exception:
        return []

def add_room_member(room_id: str, user_id: str) -> None:
    try:
        supabase.table("room_members").upsert(
            {"room_id": room_id, "user_id": user_id}, on_conflict="room_id,user_id"
        ).execute()
        invalidate_rooms_cache()
    except Exception:
        pass

DEFAULT_ROOM_NAME = "main"   # 新規登録時に自動参加させる既定ルーム

def add_to_default_room(user_id: str) -> None:
    """新規登録ユーザーを既定ルーム（main）へ自動参加させる。
    招待リンクから来た家族がすぐチャットを使えるようにするため。"""
    try:
        _rooms = fetch_rooms()  # user_id 無し＝全ルーム
        _main = next((r for r in _rooms if r.get("name") == DEFAULT_ROOM_NAME), None)
        if _main:
            add_room_member(_main["id"], user_id)
    except Exception:
        pass

def remove_room_member(room_id: str, user_id: str) -> None:
    try:
        supabase.table("room_members").delete()\
            .eq("room_id", room_id).eq("user_id", user_id).execute()
        invalidate_rooms_cache()
    except Exception:
        pass

def _member_room_names(user_id: str) -> list[str]:
    """user が参加するルーム名一覧。バックグラウンドスレッドからも呼べるよう
    st.* / cache を使わず素の supabase クエリのみ。"""
    try:
        mrows = supabase.table("room_members").select("room_id")\
            .eq("user_id", user_id).execute().data or []
        rids = [m["room_id"] for m in mrows]
        if not rids:
            return []
        rrows = supabase.table("rooms").select("name").in_("id", rids).execute().data or []
        return [r["name"] for r in rrows]
    except Exception:
        return []

# ─────────────────────────────────────
# Web Push 通知
# ─────────────────────────────────────
def _vapid_cfg() -> dict:
    """VAPID 設定を返す。st.secrets → 環境変数 の順でフォールバック。"""
    try:
        cfg = dict(st.secrets.get("push", {}))
        if cfg:
            return cfg
    except Exception:
        pass
    # Render 等、secrets.toml がない環境では環境変数から読む
    result = {}
    for key in ("vapid_public_key", "vapid_private_key", "vapid_subject"):
        val = os.environ.get(key.upper())
        if val:
            result[key] = val
    return result

def has_push_subscription(user_id: str) -> bool:
    """ユーザーが既に push_subscriptions を持っているか確認する。"""
    try:
        rows = supabase.table("push_subscriptions").select("id").eq("user_id", user_id).limit(1).execute().data
        return bool(rows)
    except Exception:
        return False

def save_push_subscription(user_id: str, subscription_json: str) -> None:
    """JS からの Web Push 購読情報を push_subscriptions テーブルに保存（upsert）。"""
    try:
        sub = json.loads(subscription_json)
        endpoint = sub.get("endpoint", "")
        keys     = sub.get("keys", {})
        p256dh   = keys.get("p256dh", "")
        auth     = keys.get("auth", "")
        if not (endpoint and p256dh and auth):
            return
        supabase.table("push_subscriptions").upsert(
            {"user_id": user_id, "endpoint": endpoint, "p256dh": p256dh, "auth": auth},
            on_conflict="user_id,endpoint",
        ).execute()
    except Exception:
        pass

def send_push(room: str, sender_uid: str, sender_name: str,
              content: str, has_image: bool = False,
              priv: str = "", subj: str = "") -> None:
    """送信者以外の全購読者に Web Push 通知を送る。
    ペイロードに unread_count を含めることで sw.js が即座にバッジを更新できる。

    ★ バックグラウンドスレッドから呼ばれるため、st.secrets / @st.cache_data 等の
      Streamlit コンテキスト依存 API は呼ばない。VAPID 鍵(priv/subj)は呼び出し元
      （メインスレッド）が取得して渡す。未読数は受信者ごとの参加ルームを
      _member_room_names()（素の supabase クエリ）で取得して集計する。"""
    try:
        if not (priv and subj):
            return

        from pywebpush import webpush, WebPushException

        body = f"{sender_name}: {'📷 写真' if has_image and not content else content[:80]}"

        # 送信者以外の購読情報を取得
        rows = supabase.table("push_subscriptions")\
            .select("endpoint, p256dh, auth, user_id")\
            .neq("user_id", sender_uid)\
            .execute().data or []

        expired: list[str] = []
        for row in rows:
            # 受信者ごとに未読数を計算してペイロードに乗せる（sw.js がバッジに使う）
            recipient_uid = row.get("user_id", "")
            # 受信者ごとの参加ルームで未読を集計（スレッド安全な素クエリ）
            try:
                r_rooms = _member_room_names(recipient_uid) if recipient_uid else []
                unread = sum(get_unread_counts(recipient_uid, r_rooms).values()) if r_rooms else 1
            except Exception:
                unread = 1
            payload = json.dumps({
                "title":        f"danran 🏠 {room}",
                "body":         body,
                "room":         room,
                "url":          "/",
                "unread_count": unread,
            }, ensure_ascii=False)
            try:
                webpush(
                    subscription_info={
                        "endpoint": row["endpoint"],
                        "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
                    },
                    data=payload,
                    vapid_private_key=priv,
                    vapid_claims={"sub": subj},
                )
            except WebPushException as ex:
                # 410 Gone = 購読期限切れ → あとで削除
                if "410" in str(ex):
                    expired.append(row["endpoint"])
            except Exception:
                pass

        # 期限切れ購読を削除
        for ep in expired:
            try:
                supabase.table("push_subscriptions").delete().eq("endpoint", ep).execute()
            except Exception:
                pass
    except Exception:
        pass

# ─────────────────────────────────────
# パスワード
# ─────────────────────────────────────
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False

# ─────────────────────────────────────
# セッション管理
# ─────────────────────────────────────
def create_session(user_id: str) -> str:
    return supabase.table("sessions").insert({"user_id": user_id}).execute().data[0]["id"]

def get_session_user(session_id: str) -> dict | None:
    try:
        sess = supabase.table("sessions").select("user_id").eq("id", session_id).single().execute().data
        if not sess:
            return None
        return supabase.table("users").select("id, name, avatar, phone").eq("id", sess["user_id"]).single().execute().data
    except Exception:
        return None

def delete_session(sid: str) -> None:
    try:
        supabase.table("sessions").delete().eq("id", sid).execute()
    except Exception:
        pass

def do_login(user: dict) -> None:
    sid = create_session(user["id"])
    # ★ セッションIDは URL に載せない（載せると URL 共有で他人がログイン状態になり
    #   チャットが見えてしまう重大な穴になる）。復元は localStorage + restore_session 経由のみ。
    st.session_state["session_id"]   = sid
    st.session_state["current_user"] = {k: user.get(k, "") for k in ("id", "name", "avatar", "phone")}
    st.session_state["view"]         = "chat"
    st.session_state.pop("_invite_ok", None)

def do_logout() -> None:
    delete_session(st.session_state.pop("session_id", "") or "")
    st.session_state.pop("current_user", None)
    st.session_state.pop("_show_rooms", None)
    st.session_state["view"]            = "select_user"
    st.session_state["_clear_session"]  = True   # localStorage もクリア
    st.query_params.clear()

# ─────────────────────────────────────
# ストレージ
# ─────────────────────────────────────
def _fix_exif(f) -> tuple[bytes, str]:
    """EXIF orientation を適用して正しい向きの JPEG バイト列を返す。
    スマホ撮影写真の横向き問題を解消。"""
    f.seek(0)
    img = Image.open(f)
    img = ImageOps.exif_transpose(img)   # EXIF に従って自動回転
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue(), "image/jpeg"


def upload_photo(bucket: str, file_id: str, f) -> str:
    data, content_type = _fix_exif(f)   # EXIF 回転適用 + JPEG 変換
    fn = f"{file_id}.jpg"               # 常に .jpg（JPEG 変換後）
    supabase.storage.from_(bucket).upload(fn, data, {"content-type": content_type, "upsert": "true"})
    return supabase.storage.from_(bucket).get_public_url(fn)

# ─────────────────────────────────────
# ユーザー DB
# ─────────────────────────────────────
def fetch_all_users() -> list[dict]:
    try:
        return supabase.table("users").select("id, name, avatar").order("created_at").execute().data or []
    except Exception:
        return []

def get_user_with_hash(user_id: str) -> dict | None:
    try:
        return supabase.table("users").select("id, name, avatar, password_hash").eq("id", user_id).single().execute().data
    except Exception:
        return None

def register_user(name: str, avatar: str, pw: str, uid: str | None = None, phone: str = "") -> dict:
    uid = uid or str(uuid.uuid4())
    row: dict = {"id": uid, "name": name, "avatar": avatar, "password_hash": hash_password(pw)}
    if phone:
        row["phone"] = phone
    return supabase.table("users").insert(row).execute().data[0]

def update_user_profile(user_id: str, old_name: str, new_name: str, avatar: str, phone: str = "") -> None:
    """名前・アバター・電話番号を更新し、過去メッセージも一括で書き換える。
    user_id でフィルタするため名前変更後も確実に更新される。"""
    try:
        supabase.table("users").update({"name": new_name, "avatar": avatar, "phone": phone or None}).eq("id", user_id).execute()
        # 過去メッセージを user_id で一括更新（名前変更に依存しない）
        supabase.table("messages").update({"user_name": new_name, "user_avatar": avatar}).eq("user_id", user_id).execute()
    except Exception as e:
        raise RuntimeError(str(e))

# ─────────────────────────────────────
# ルーム管理 DB
# ─────────────────────────────────────
def update_room(room_id: str, old_name: str, new_name: str, icon: str) -> None:
    """ルーム名・アイコンを更新。名前変更時はメッセージ・既読情報も連動して更新。"""
    try:
        if old_name != new_name:
            supabase.table("messages").update({"room_name": new_name}).eq("room_name", old_name).execute()
            supabase.table("last_read").update({"room_name": new_name}).eq("room_name", old_name).execute()
        supabase.table("rooms").update({"name": new_name, "icon": icon}).eq("id", room_id).execute()
        invalidate_rooms_cache()
    except Exception as e:
        raise RuntimeError(str(e))

def create_room(name: str, icon: str, creator_id: str = "") -> dict:
    """新しいルームを作成し、作成者をメンバーに追加して返す。"""
    new_id = str(uuid.uuid4())
    result = supabase.table("rooms").insert({"id": new_id, "name": name, "icon": icon}).execute()
    if creator_id:
        try:
            supabase.table("room_members").upsert(
                {"room_id": new_id, "user_id": creator_id}, on_conflict="room_id,user_id"
            ).execute()
        except Exception:
            pass
    invalidate_rooms_cache()
    return result.data[0] if result.data else {}

def delete_room(room_id: str, room_name: str) -> None:
    """ルームと、そのメッセージ・リアクション・既読情報・メンバーをすべて削除。"""
    try:
        # リアクションを先に削除（FK cascade が未設定の場合の保険）
        msgs = supabase.table("messages").select("id").eq("room_name", room_name).execute().data or []
        msg_ids = [m["id"] for m in msgs]
        if msg_ids:
            supabase.table("reactions").delete().in_("message_id", msg_ids).execute()
        supabase.table("last_read").delete().eq("room_name", room_name).execute()
        supabase.table("messages").delete().eq("room_name", room_name).execute()
        supabase.table("room_members").delete().eq("room_id", room_id).execute()
        supabase.table("rooms").delete().eq("id", room_id).execute()
        invalidate_rooms_cache()
    except Exception as e:
        raise RuntimeError(str(e))

# ─────────────────────────────────────
# メッセージ DB
# ─────────────────────────────────────
def fetch_messages(room: str, limit: int = 100) -> list[dict] | None:
    """メッセージ取得。成功時はリスト（空可）、通信エラー時は None を返す。
    ★ 一時的なエラーで [] を返すと「0件」描画でチャットが一瞬空になるため、
      呼び出し元は None のとき前回表示を維持する。"""
    try:
        return supabase.table("messages")\
            .select("id, user_id, user_name, user_avatar, content, image_url, created_at, "
                    "reply_to_id, reply_to_name, reply_to_text, reply_to_image")\
            .eq("room_name", room).order("created_at").limit(limit).execute().data or []
    except Exception:
        return None

def send_message(room: str, uid: str, uname: str, uavatar: str, content: str, image_url: str | None = None,
                 reply_to: dict | None = None) -> bool:
    try:
        _row = {
            "room_name": room, "user_id": uid, "user_name": uname,
            "user_avatar": uavatar, "content": content, "image_url": image_url,
        }
        if reply_to and reply_to.get("id"):
            _row["reply_to_id"]    = reply_to.get("id")
            _row["reply_to_name"]  = reply_to.get("name", "")
            _row["reply_to_text"]  = (reply_to.get("text", "") or "")[:120]
            _row["reply_to_image"] = reply_to.get("image", "") or None
        supabase.table("messages").insert(_row).execute()
        # ── プッシュ通知はバックグラウンドスレッドで送信（UI をブロックしない）──
        # VAPID 鍵だけメインスレッドで取得して渡す（スレッド内で st.secrets を呼ぶと
        # ScriptRunContext 不在で失敗しプッシュが静かに飛ばなくなるため）。
        # 受信者ごとの未読集計は send_push 内の _member_room_names() が担う。
        _cfg = _vapid_cfg()
        _priv = _cfg.get("vapid_private_key", "")
        _subj = _cfg.get("vapid_subject", "")
        threading.Thread(
            target=send_push,
            args=(room, uid, uname, content),
            kwargs={
                "has_image": bool(image_url),
                "priv": _priv, "subj": _subj,
            },
            daemon=True,
        ).start()
        return True
    except Exception as e:
        st.error(f"❌ {e}"); return False

def delete_message(msg_id: str, user_id: str) -> bool:
    """メッセージを削除する。user_id（UUID）で認可するため名前変更後も安全。"""
    try:
        supabase.table("messages").delete().eq("id", msg_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        st.error(f"❌ {e}"); return False

# ─────────────────────────────────────
# リアクション DB
# ─────────────────────────────────────
def fetch_reactions_bulk(msg_ids: list[str]) -> dict[str, dict[str, list[str]]]:
    """{msg_id: {emoji: [user_name, ...]}} を一括取得。"""
    if not msg_ids:
        return {}
    try:
        rows = supabase.table("reactions")\
            .select("message_id, emoji, user_name").in_("message_id", msg_ids).execute().data or []
        result: dict = {}
        for r in rows:
            result.setdefault(r["message_id"], {}).setdefault(r["emoji"], []).append(r["user_name"])
        return result
    except Exception:
        return {}

def toggle_reaction(msg_id: str, uname: str, emoji: str) -> None:
    try:
        existing = supabase.table("reactions").select("id")\
            .eq("message_id", msg_id).eq("user_name", uname).eq("emoji", emoji).execute().data
        if existing:
            supabase.table("reactions").delete().eq("id", existing[0]["id"]).execute()
        else:
            supabase.table("reactions").insert({"message_id": msg_id, "user_name": uname, "emoji": emoji}).execute()
    except Exception:
        pass

# ─────────────────────────────────────
# 既読管理
# ─────────────────────────────────────
def get_unread_counts(user_id: str, room_names: list[str] | None = None) -> dict[str, int]:
    if room_names is None:
        room_names = [r["name"] for r in fetch_rooms(user_id)]

    def _parse_ts(ts: str | None):
        """timestamptz 文字列を tz-aware datetime に。失敗時 None。"""
        try:
            return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        except Exception:
            return None

    try:
        # ① last_read を取得（1クエリ）→ room_name ごとの既読時刻を datetime で保持
        lr_rows = supabase.table("last_read")\
            .select("room_name, read_at").eq("user_id", user_id).execute().data or []
        last_reads = {}
        for r in lr_rows:
            dt = _parse_ts(r.get("read_at"))
            if dt is not None:
                last_reads[r["room_name"]] = dt

        # ② 全メッセージの room_name + created_at を 1クエリで取得しクライアント集計
        #    （旧実装はルーム数ぶん count クエリを投げて N+1 だった → 常に2クエリに）
        #    タイムゾーン差（+09:00 と +00:00）で誤判定しないよう datetime 比較する。
        msg_rows = supabase.table("messages")\
            .select("room_name, created_at").execute().data or []

        room_set = set(room_names)
        counts = {rname: 0 for rname in room_names}
        for m in msg_rows:
            rn = m.get("room_name")
            if rn not in room_set:
                continue
            lr = last_reads.get(rn)
            if lr is None:
                counts[rn] += 1                      # 既読基準なし＝全件未読（旧挙動と一致）
            else:
                cdt = _parse_ts(m.get("created_at"))
                if cdt is not None and cdt > lr:
                    counts[rn] += 1
        return counts
    except Exception:
        return {r: 0 for r in room_names}

def mark_as_read(user_id: str, room: str) -> None:
    try:
        supabase.table("last_read").upsert({
            "user_id": user_id, "room_name": room,
            "read_at": datetime.now(JST).isoformat(),
        }).execute()
    except Exception:
        pass

def read_by_users(room: str, my_id: str, msg_created_iso: str) -> list[dict]:
    """指定メッセージ(msg_created_iso)以降に既読にした「自分以外」のユーザー一覧を返す。
    last_read（ユーザー×ルームの最終既読時刻）を流用。軽い既読表示用。"""
    def _p(ts):
        try:
            return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        except Exception:
            return None
    cdt = _p(msg_created_iso)
    if cdt is None:
        return []
    try:
        lr = supabase.table("last_read").select("user_id, read_at")\
            .eq("room_name", room).execute().data or []
        users_by_id = {u["id"]: u for u in fetch_all_users()}
        out = []
        for r in lr:
            uid = r.get("user_id", "")
            if not uid or uid == my_id:
                continue
            rdt = _p(r.get("read_at"))
            if rdt and rdt >= cdt and uid in users_by_id:
                out.append(users_by_id[uid])
        # ★ 順序を固定（user_id ソート）。並びが毎回変わると既読HTMLが変化し、
        #   チャット全体(1つのst.markdown)が2秒ごとに再描画されて画像がチカチカするため。
        out.sort(key=lambda u: u.get("id", ""))
        return out
    except Exception:
        return []

# ─────────────────────────────────────
# タイムスタンプ
# ─────────────────────────────────────
def fmt_ts(ts_str: str) -> str:
    try:
        dt  = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(JST)
        now = datetime.now(JST)
        if dt.date() == now.date():
            return f"今日 {dt.strftime('%H:%M')}"
        elif dt.date() == (now - timedelta(days=1)).date():
            return f"昨日 {dt.strftime('%H:%M')}"
        return dt.strftime("%-m/%-d %H:%M")
    except Exception:
        return ts_str

# ─────────────────────────────────────
# 長押し検出カスタムコンポーネント
#   components/longpress/index.html が Streamlit の正式プロトコルで
#   メッセージ ID を Python に返す（srcdoc ではなく同一オリジンで配信）
# ─────────────────────────────────────
_LP_COMPONENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "components", "longpress"
)
_lp_detector = st.components.v1.declare_component(
    "danran_lp_v89",   # Worker経由で招待コードが要求される問題（JSが実URLのinviteを転送）
    path=_LP_COMPONENT_DIR,
)

# ─────────────────────────────────────
# 本文の URL リンク化
# ─────────────────────────────────────
_URL_RE = re.compile(r'(https?://[^\s<>"\']+)')

def _body_attr(body: str) -> str:
    """data-lp-body 属性用エスケープ。生の改行を属性に入れると st.markdown の
    Markdown 処理が HTML タグを壊すため &#10; に変換（getAttribute では \\n に復元される）。"""
    return _html.escape(body).replace("\r", "").replace("\n", "&#10;")

def linkify_body(body: str) -> str:
    """本文を HTML エスケープしつつ URL を <a> 化して返す（改行は <br>）。
    URL タップで target=_blank → iOS PWA では既定ブラウザ(Safari)で開く。
    エスケープは URL/非URL を分けて行い XSS を防ぐ。"""
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out: list[str] = []
    last = 0
    for m in _URL_RE.finditer(body):
        out.append(esc(body[last:m.start()]))
        url = m.group(1)
        trail = ""                       # 末尾の句読点・閉じ括弧はリンクから除外
        while url and url[-1] in '.,!?。、）)」』】':
            trail = url[-1] + trail
            url = url[:-1]
        u = esc(url)
        # data-lp-link: JS がタップを横取りして「開く/コピー/共有」メニューを出す。
        # href/target は JS 無効時のフォールバック（既定ブラウザで開く）。
        out.append(
            f'<a href="{u}" data-lp-link="{u}" target="_blank" rel="noopener noreferrer" '
            f'style="color:inherit;text-decoration:underline;word-break:break-all">{u}</a>'
        )
        out.append(esc(trail))
        last = m.end()
    out.append(esc(body[last:]))
    return ''.join(out).replace("\n", "<br>")

# ─────────────────────────────────────
# ★ リアルタイムタイムライン
#   変化検知ポーラー（poll_messages）が変化時のみ再描画する。
# ─────────────────────────────────────
def build_messages_html(selected_room: str, current_user: dict) -> str | None:
    """チャットの全メッセージ HTML（バブル＋軽い既読）を 1 つの文字列で返す。
    副作用なし（fetch のみ）。静的描画と変化検知ポーラーの両方から共用する。
    ★ この出力が前回と同一なら再描画しない＝画像チカチカ防止の要。"""
    uname = current_user.get("name", "")
    my_id = current_user.get("id", "")

    messages = fetch_messages(selected_room)
    if messages is None:
        return None   # 取得失敗（通信エラー）→ 呼び出し元が前回表示を維持
    if not messages:
        return ('<div style="padding:22px 16px;text-align:center;'
                'color:rgba(255,255,255,0.5);font-size:0.9rem;line-height:1.7">'
                '📭 まだメッセージはありません。<br>最初のメッセージを送ってみましょう！</div>')

    # リアクション一括取得
    all_reactions = fetch_reactions_bulk([m["id"] for m in messages])

    # ── リアクション pills 生成（通常バブル・連投グリッド両方で共用）──
    def _build_pills(msg_reactions: dict) -> str:
        pills = ""
        for emoji in REACTION_EMOJIS:
            users = msg_reactions.get(emoji, [])
            if users:
                my  = uname in users
                bg  = "rgba(232,145,91,0.32)" if my else "rgba(255,255,255,0.08)"
                bdr = "rgba(240,168,104,0.9)" if my else "rgba(255,255,255,0.2)"
                pills += (
                    f'<span style="display:inline-flex;align-items:center;gap:2px;'
                    f'background:{bg};border:1px solid {bdr};border-radius:20px;'
                    f'padding:1px 7px;font-size:0.8rem;margin-right:3px">'
                    f'{emoji}&nbsp;{len(users)}</span>'
                )
        return pills

    # ── 連投画像のグルーピング（LINE 風コンパクトグリッド）──
    #   同一送信者・画像のみ（本文なし）・直前から WINDOW 秒以内 のメッセージが
    #   2件以上連続したら、1つのグリッドバブルにまとめて描画する。
    #   各セルは個別の data-lp-msg を保持するので長押し削除・タップ全画面は従来どおり動く。
    def _ts_sec(s: str) -> float:
        if not s:
            return 0.0
        try:
            from datetime import datetime as _DT
            return _DT.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    _IMG_GROUP_WINDOW = 600  # 秒（10分以内の連投をまとめる）
    def _is_img_only(m: dict) -> bool:
        return bool(m.get("image_url")) and not (m.get("content") or "").strip()
    def _same_sender(a: dict, b: dict) -> bool:
        if a.get("user_id") and b.get("user_id"):
            return a["user_id"] == b["user_id"]
        return a.get("user_name") == b.get("user_name")

    _group_start: dict[int, list] = {}  # 開始index -> [msg,...]（len>=2 のみ）
    _skip: set[int] = set()             # グループ2件目以降（個別描画しない）
    _gi = 0
    _n = len(messages)
    while _gi < _n:
        _m = messages[_gi]
        if _is_img_only(_m):
            _run = [_m]
            _gj = _gi + 1
            while _gj < _n:
                _x = messages[_gj]
                if (_is_img_only(_x) and _same_sender(_x, _m)
                        and abs(_ts_sec(_x.get("created_at", "")) -
                                _ts_sec(_run[-1].get("created_at", ""))) <= _IMG_GROUP_WINDOW):
                    _run.append(_x)
                    _gj += 1
                else:
                    break
            if len(_run) >= 2:
                _group_start[_gi] = _run
                for _k in range(_gi + 1, _gj):
                    _skip.add(_k)
            _gi = _gj
        else:
            _gi += 1

    # ── 連投グリッド HTML（各セル = 正方形・object-fit:cover・個別 data-lp-msg）──
    def _img_grid_html(run: list, is_mine: bool) -> str:
        # ★ 常に2列固定（枚数が増えても3列にせず、下に行が増えていく）
        cols = 2
        cells = ""
        for im in run:
            u   = im.get("image_url") or ""
            mid = im.get("id", "")
            pills = _build_pills(all_reactions.get(mid, {}))
            overlay = (
                f'<div data-lp-react="{mid}" style="position:absolute;left:3px;bottom:3px;'
                f'z-index:1;pointer-events:none;line-height:1;display:flex;flex-wrap:wrap;'
                f'gap:2px">{pills}</div>'
            )
            mine_attr = ' data-lp-mine="1"' if is_mine else ''
            cells += (
                f'<span class="lp-imgslot" data-fit="cover" '
                f'data-img="{_html.escape(u)}" data-lp-image="{_html.escape(u)}" '
                f'data-lp-msg="{mid}" data-lp-name="{_html.escape(im.get("user_name", "") or "")}"{mine_attr} '
                f'style="position:relative;display:block;width:100%;aspect-ratio:1/1;'
                f'background:rgba(255,255,255,0.06);cursor:pointer;overflow:hidden">'
                f'{overlay}</span>'
            )
        return (
            f'<div style="display:grid;grid-template-columns:repeat({cols},1fr);'
            f'gap:3px;width:210px;max-width:72vw;border-radius:12px;overflow:hidden;'
            f'{"margin-left:auto" if is_mine else ""}">{cells}</div>'
        )

    # ── 引用返信ブロック（バブル上部に元メッセージのスナップショットを表示）──
    #   data-lp-jump: タップで元メッセージへスクロール＋ぷるぷる強調（JS）
    def _reply_quote_html(m: dict) -> str:
        rid = m.get("reply_to_id")
        if not rid:
            return ""
        rname = _html.escape(m.get("reply_to_name", "") or "")
        rimg  = m.get("reply_to_image") or ""
        rtext = _html.escape((m.get("reply_to_text", "") or "").strip()[:60]) or ("写真" if rimg else "")
        thumb = (
            f'<img src="{_html.escape(rimg)}" loading="lazy" '
            f'style="width:34px;height:34px;border-radius:6px;object-fit:cover;flex-shrink:0">'
        ) if rimg else ""
        return (
            f'<div data-lp-jump="{rid}" style="display:flex;align-items:center;gap:7px;'
            f'border-left:3px solid rgba(255,255,255,0.5);padding:1px 0 1px 8px;'
            f'margin-bottom:5px;opacity:0.9;font-size:0.78rem;line-height:1.35;text-align:left;'
            f'cursor:pointer">'
            f'<div style="flex:1;min-width:0">'
            f'<div style="font-weight:700;opacity:0.85;margin-bottom:1px">{rname}</div>'
            f'<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
            f'max-width:180px">{rtext}</div>'
            f'</div>'
            f'{thumb}'
            f'</div>'
        )

    # メッセージごとに st.markdown を呼ぶと 2 秒ポーリングのたびに N 個の Streamlit
    # 要素を生成・差分計算してもっさり/ちらつきの原因になる。
    # バブル HTML をリストに溜めてループ後に 1 回だけ描画する。
    _bubbles: list[str] = []
    _last_mine_bidx = None   # _bubbles 内の自分の最新バブル位置（軽い既読表示用）
    _last_mine_created = ""  # その既読判定に使う created_at
    for _mi, msg in enumerate(messages):
        if _mi in _skip:
            continue   # 連投グリッドにまとめ済み
        msg_id   = msg.get("id",          "")
        sender   = msg.get("user_name",  "不明")
        msg_uid  = msg.get("user_id",    "")
        body     = msg.get("content",    "") or ""
        ts       = msg.get("created_at", "")
        avatar   = msg.get("user_avatar","🙂")
        img_url  = msg.get("image_url")
        # user_id があればIDで判定（名前変更後も正しく動く）、なければ名前フォールバック
        is_mine  = (msg_uid == my_id) if (msg_uid and my_id) else (sender == uname)

        # ── アバター HTML（他人用・自分用）──
        # 他人アバターは data-lp-sender/data-lp-avatar 付き → タップで全画面プロフィール
        _av_attr = (f'data-lp-sender="{_html.escape(sender)}" '
                    f'data-lp-avatar="{_html.escape(avatar)}"')
        av_html = (
            f'<img {_av_attr} src="{avatar}" style="width:40px;height:40px;border-radius:8px;'
            f'object-fit:cover;flex-shrink:0;display:block;cursor:pointer">'
            if avatar.startswith("http")
            else f'<span {_av_attr} style="font-size:1.8rem;line-height:40px;display:block;'
                 f'width:40px;text-align:center;flex-shrink:0;cursor:pointer">{avatar}</span>'
        )
        # ★ 自分のアバターはチャットに表示しない（LINE 同様）。プロフィール編集は
        #   ルーム選択ヘッダー右上のアバターから。

        # ── 連投画像グループ → コンパクトグリッドバブル ──
        _grp = _group_start.get(_mi)
        if is_mine:
            _last_mine_bidx    = len(_bubbles)   # このバブルが入る位置
            _last_mine_created = (_grp[-1] if _grp else msg).get("created_at", "")
        if _grp:
            grid_html = _img_grid_html(_grp, is_mine)
            _last_ts  = _grp[-1].get("created_at", "")
            if is_mine:
                bubble = (
                    f'<div style="display:flex;justify-content:flex-end;align-items:flex-start;'
                    f'margin:4px 0 2px 48px">'
                    f'<div style="text-align:right">'
                    f'<div style="font-size:0.7rem;color:#888;margin-bottom:3px">{fmt_ts(_last_ts)}</div>'
                    f'{grid_html}'
                    f'</div>'
                    f'</div>'
                )
            else:
                bubble = (
                    f'<div style="display:flex;align-items:flex-start;gap:8px;margin:4px 0 2px 0">'
                    f'{av_html}'
                    f'<div>'
                    f'<div style="font-size:0.75rem;color:#9a9a9a;font-weight:600;'
                    f'margin-bottom:3px">{sender}</div>'
                    f'{grid_html}'
                    f'<div style="font-size:0.7rem;color:#888;margin-top:3px">{fmt_ts(_last_ts)}</div>'
                    f'</div>'
                    f'</div>'
                )
            _bubbles.append(bubble)
            continue

        msg_reactions = all_reactions.get(msg_id, {})

        # ── 本文・画像 HTML ──（URL はリンク化。エスケープは linkify_body 内で実施）
        body_esc  = linkify_body(body)
        is_img_only = bool(img_url) and not body.strip()  # 画像のみ（テキスト無し）
        img_piece = (
            # ★ <img> を直接出さず JS 管理のスロットにする。
            #   2秒ポーリングで再描画されても、JS が「同じ画像ノードを移動させるだけ」で
            #   再ロードしないためチカチカしない（fillImageSlots）。
            #   data-lp-image: タップで全画面ビューア / 薄グレー枠 = 読込前プレースホルダー
            f'<span class="lp-imgslot" data-img="{_html.escape(img_url)}" '
            f'data-lp-image="{_html.escape(img_url)}" '
            f'style="display:block;max-width:200px;min-height:80px;border-radius:10px;'
            f'cursor:pointer;background:rgba(255,255,255,0.06);'
            f'{"margin-bottom:6px" if body else ""}"></span>'
        ) if img_url else ""
        content = _reply_quote_html(msg) + img_piece + body_esc

        # ── リアクション pills ──
        pills = _build_pills(msg_reactions)
        # data-lp-react: JS がリアルタイムで書き換えるためのコンテナ（常に出力）
        pills_row = (
            f'<div data-lp-react="{msg_id}" style="margin-top:4px;text-align:{"right" if is_mine else "left"};'
            f'line-height:2;min-height:0">{pills}</div>'
        )

        # ── LINE 風バブル HTML（自分＝右、他人＝左） ──
        # data-lp-mine="1"   → JS が「自分のメッセージ」と判別して削除ボタン表示
        # data-lp-my-avatar  → JS が「自分のアバター」と判別してタップでプロフィール遷移
        if is_mine:
            # 画像のみなら透明バブル（緑背景・パディング不要）
            _mine_bstyle = (
                'background:transparent;padding:0;border-radius:0'
                if is_img_only else
                'background:#e8915b;color:#fff;border-radius:18px 18px 4px 18px;padding:10px 14px'
            )
            bubble = (
                # data-lp-body: 楽観的バブルとの照合用に生テキストを保持
                f'<div data-lp-msg="{msg_id}" data-lp-mine="1" '
                f'data-lp-name="{_html.escape(sender)}" '
                f'data-lp-body="{_body_attr(body)}" style="'
                f'display:flex;justify-content:flex-end;align-items:flex-end;'
                f'margin:4px 0 2px 48px">'
                f'<div style="text-align:right">'
                f'<div style="font-size:0.7rem;color:#888;margin-bottom:3px">{fmt_ts(ts)}</div>'
                f'<div style="{_mine_bstyle};'
                f'display:inline-block;max-width:100%;text-align:left;'
                f'word-break:break-word;font-size:0.93rem">{content}</div>'
                f'{pills_row}'
                f'</div>'
                f'</div>'
            )
        else:
            # 画像のみなら透明バブル（グレー背景・パディング不要）
            _other_bstyle = (
                'background:transparent;padding:0;border-radius:0'
                if is_img_only else
                'background:#2e2926;color:#f0e8e0;border-radius:18px 18px 18px 4px;padding:10px 14px'
            )
            bubble = (
                f'<div data-lp-msg="{msg_id}" data-lp-name="{_html.escape(sender)}" '
                f'data-lp-body="{_body_attr(body)}" style="'
                f'display:flex;align-items:flex-end;gap:8px;margin:4px 0 2px 0">'
                f'{av_html}'
                f'<div>'
                f'<div style="font-size:0.75rem;color:#9a9a9a;font-weight:600;'
                f'margin-bottom:3px">{sender}</div>'
                f'<div style="{_other_bstyle};'
                f'display:inline-block;max-width:100%;text-align:left;'
                f'word-break:break-word;font-size:0.93rem">{content}</div>'
                f'<div style="font-size:0.7rem;color:#888;margin-top:3px">{fmt_ts(ts)}</div>'
                f'{pills_row}'
                f'</div>'
                f'</div>'
            )

        _bubbles.append(bubble)

    # ── 軽い既読表示（自分の最新メッセージにだけ・既読した人だけ・圧をかけない）──
    if _last_mine_bidx is not None and my_id:
        # 既読人数だけ表示（誰が読んだかは出さない＝アイコン無し）
        _readers = read_by_users(selected_room, my_id, _last_mine_created)
        if _readers:
            _bubbles[_last_mine_bidx] += (
                f'<div style="text-align:right;margin:0 2px 6px 0;font-size:0.66rem;'
                f'color:rgba(240,232,224,0.45)">既読 {len(_readers)}</div>'
            )

    # 全バブルを 1 つの文字列で返す（呼び出し元が 1 回だけ st.markdown する）
    return '\n'.join(_bubbles)


# ── 変化時のみ再描画する仕組み ────────────────────────────────────────
# 旧実装は @st.fragment(run_every="2s") で毎2秒メッセージDOMを再生成しており、
# 内容が同じでも画像スロットが作り直されてチカチカしていた。
# → 静的描画(render_chat_messages) ＋ 変化検知ポーラー(poll_messages) に分離。
#   描画は「実際に内容が変わった時」だけ行われる。
def render_chat_messages(current_user: dict) -> None:
    """チャットメッセージを描画（full rerun 時のみ実行＝変化駆動）。
    キャッシュ済み HTML があればそれを使い、無ければ build して保存する。"""
    room = st.session_state.get("active_room", "")
    if st.session_state.get("_chat_html_room") != room or "_chat_html" not in st.session_state:
        _html = build_messages_html(room, current_user)
        if _html is not None:   # None=取得失敗。前回の _chat_html を維持（空表示で消さない）
            st.session_state["_chat_html"] = _html
            st.session_state["_chat_html_room"] = room
    st.markdown(st.session_state.get("_chat_html", "") or "", unsafe_allow_html=True)


@st.fragment(run_every="2s")
def poll_messages() -> None:
    """2秒ごとに「内容が変わったか」だけを確認し、変わった時だけ st.rerun()。
    何も描画しない（＝変化が無ければ DOM は一切いじらない→画像チカチカしない）。"""
    if (st.session_state.get("_show_rooms", False)
            or "current_user" not in st.session_state
            or st.session_state.get("view") != "chat"):
        return
    current_user  = st.session_state.get("current_user", {})
    selected_room = st.session_state.get("active_room", "")
    uname         = current_user.get("name", "")
    my_id         = current_user.get("id", "")

    # 既読マーク（自分がこのルームを見ている＝最新まで既読）
    if my_id:
        mark_as_read(my_id, selected_room)

    # 新着トースト（他人のメッセージのみ）
    _msgs = fetch_messages(selected_room)
    if _msgs is None:
        return   # 取得失敗（通信エラー）→ 今回はスキップ（前回表示を維持）
    count_key = f"cnt_{selected_room}"
    prev = st.session_state.get(count_key, -1)
    if prev >= 0 and len(_msgs) > prev:
        for m in _msgs[prev:]:
            if m["user_name"] != uname:
                preview = m["content"][:30] if m["content"] else "📷 写真"
                st.toast(f"💬 {m['user_name']}: {preview}", icon="🔔")
    st.session_state[count_key] = len(_msgs)

    # 内容（バブルHTML）が前回と変わっていれば再描画。同じなら何もしない。
    _html_now = build_messages_html(selected_room, current_user)
    if _html_now is None:
        return   # 取得失敗 → 前回表示を維持
    if _html_now != st.session_state.get("_chat_html") \
            or st.session_state.get("_chat_html_room") != selected_room:
        st.session_state["_chat_html"]      = _html_now
        st.session_state["_chat_html_room"] = selected_room
        st.rerun()

# ─────────────────────────────────────
# 画面① ログイン（名前 + パスワード）
# ユーザーリストは一切表示しない → 他人のアカウントが見えない
# ─────────────────────────────────────
def show_user_select() -> None:
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("<br>" * 2, unsafe_allow_html=True)
        st.markdown("## 🏠 danran")
        st.divider()
        identifier = st.text_input("お名前 または 電話番号", placeholder="例：ユーザー名　または　09012345678", key="login_name")
        st.caption("電話番号はハイフンなし（09012345678）で入力してください")
        pw         = st.text_input("パスワード", type="password", key="login_pw")
        # ── ブルートフォース対策（同一セッション内で10回失敗でロック）──
        _fail_count = st.session_state.get("_login_fails", 0)
        if _fail_count >= 10:
            st.error("⛔ ログイン試行回数が多すぎます。ページを更新してください。")
            return
        if st.button("🔓 ログイン", use_container_width=True, type="primary"):
            if not identifier.strip():
                st.error("お名前または電話番号を入力してください"); return
            if not pw:
                st.error("パスワードを入力してください"); return
            # ハイフンを除去して正規化（どちらの形式で入力されても対応）
            ident      = identifier.strip()
            ident_norm = normalize_phone(ident)   # 電話番号検索用
            # 名前 → 電話番号（正規化済み）の順で検索
            u = None
            try:
                result = supabase.table("users")\
                    .select("id, name, avatar, phone, password_hash")\
                    .eq("name", ident).single().execute()
                if result.data:
                    u = result.data
            except Exception:
                pass
            if not u and ident_norm:
                try:
                    result = supabase.table("users")\
                        .select("id, name, avatar, phone, password_hash")\
                        .eq("phone", ident_norm).single().execute()
                    if result.data:
                        u = result.data
                except Exception:
                    pass
            if u and verify_password(pw, u.get("password_hash") or ""):
                st.session_state.pop("_login_fails", None)   # 成功時はカウンターリセット
                do_login(u); st.rerun()
            else:
                st.session_state["_login_fails"] = _fail_count + 1
                st.error("名前・電話番号またはパスワードが違います 🔒")
        st.divider()
        if st.button("＋ 新しいメンバーとして登録", use_container_width=True):
            st.session_state["view"] = "register"; st.rerun()

# ─────────────────────────────────────
# 画面② パスワード入力（後方互換のため残す・実質未使用）
# ─────────────────────────────────────
def show_enter_password() -> None:
    # ログイン画面に統合したためリダイレクト
    st.session_state["view"] = "select_user"
    st.rerun()

# ─────────────────────────────────────
# 画面③ 新規登録
# ─────────────────────────────────────
def normalize_phone(phone: str) -> str:
    """電話番号をハイフン・スペースなしの数字のみに正規化する。
    例: '090-1234-5678' → '09012345678'"""
    import re
    return re.sub(r"[\s\-ー－]", "", phone)

def _get_register_key() -> str:
    """招待コードを返す。st.secrets → REGISTER_KEY 環境変数 → 空文字（制限なし）。"""
    try:
        val = st.secrets.get("app", {}).get("register_key", "")
        if val:
            return val
    except Exception:
        pass
    return os.environ.get("REGISTER_KEY", "")

def _app_url() -> str:
    """本番アプリ URL（末尾スラッシュ付き）。"""
    try:
        u = (st.secrets.get("app") or {}).get("url")
    except Exception:
        u = None
    # 既定は Cloudflare Worker URL。Worker は HTML の <head> に apple-touch-icon を
    # 注入するため「ホーム画面に追加」でアイコン/名前が danran になる
    # （streamlit.app 直アクセスは Streamlit のラッパーHTMLが最上位で、アイコンを差し替えられない）。
    u = u or os.environ.get("APP_URL", "") or "https://danran-chat.kinakonism.workers.dev/"
    return u if u.endswith("/") else u + "/"

def _invite_url() -> str:
    """家族に共有する招待リンク。?invite=<招待コード> で登録画面に着地する。"""
    rk = _get_register_key()
    base = _app_url()
    return f"{base}?invite={rk}" if rk else f"{base}?invite=1"

def show_register() -> None:
    _, col, _ = st.columns([1, 3, 1])
    with col:
        # ── iOS 向け「ホーム画面に追加」案内 ──
        # 招待リンクは Safari タブに着地する。通知＆アプリ化にはホーム画面追加が必須なので、
        # 登録前にまず追加させる。display-mode:standalone（＝ホーム画面アプリで起動）の
        # ときは CSS メディアクエリで自動的に隠す。
        st.html(
            '<style>@media (display-mode: standalone){#_danran_a2hs{display:none!important;}}</style>'
            '<div id="_danran_a2hs" style="background:#241f1c;border:1px solid rgba(240,168,104,0.45);'
            'border-left:4px solid #f0a868;border-radius:12px;padding:13px 15px;margin:6px 0 4px 0;'
            'color:#f0e8e0;font-size:0.9rem;line-height:1.7">'
            '<div style="font-weight:700;color:#f0a868;margin-bottom:6px;font-size:0.95rem">'
            '📲 通知を使うには「ホーム画面に追加」してください</div>'
            '<div style="margin-bottom:8px;color:rgba(240,232,224,0.85)">'
            'iPhone は<b>ホーム画面に追加したアプリ</b>からのみ通知が届きます。'
            '下の順番がおすすめです👇</div>'
            '<div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:4px">'
            '<span style="color:#f0a868;font-weight:700">①</span>'
            '<span>まず<b>この画面で登録を完了</b>する（下のフォーム）</span></div>'
            '<div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:4px">'
            '<span style="color:#f0a868;font-weight:700">②</span>'
            '<span>Safari <b>右下の「・・・」</b>（メニュー）をタップ → <b>「共有」</b></span></div>'
            '<div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:4px">'
            '<span style="color:#f0a868;font-weight:700">③</span>'
            '<span><b>下にスクロール</b>して<b>「ホーム画面に追加」</b> → 右上の<b>「追加」</b></span></div>'
            '<div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px">'
            '<span style="color:#f0a868;font-weight:700">④</span>'
            '<span>ホーム画面の <b>danran アイコン</b>から開き、'
            '<b>①で作ったアカウントでログイン</b></span></div>'
            '<div style="color:rgba(240,232,224,0.6);font-size:0.82rem;line-height:1.55">'
            '※ ホーム画面アプリは最初ログイン画面が出ます。①で決めた<b>名前/電話番号＋パスワード</b>で'
            'ログインしてください（チャットはそのまま見えます）。<br>'
            '※ LINE などから開いた場合は、まず<b>「Safari で開く」</b>を選んでください'
            '（アプリ内ブラウザではホーム画面に追加できません）。</div>'
            '</div>'
        )
        st.markdown("<br>", unsafe_allow_html=True)
        if st.session_state.get("_invite_ok"):
            # 1行で収める（折り返し防止）
            st.markdown(
                "<div style='font-size:1.5rem;font-weight:700;white-space:nowrap;"
                "text-align:center'>🏠 danran へようこそ！</div>",
                unsafe_allow_html=True,
            )
            st.caption("家族から招待されました。アカウントを作成して参加しましょう。")
        else:
            st.markdown("## 👋 新しいメンバー登録")
        st.divider()

        # ── 招待コード認証（secrets に register_key が設定されている場合のみ） ──
        #   招待リンク経由（_invite_ok）はコード検証済みなので入力をスキップ。
        req_key = _get_register_key()
        if req_key and not st.session_state.get("_invite_ok"):
            entered_key = st.text_input(
                "🔑 招待コード", type="password",
                placeholder="管理者から受け取ったコードを入力",
                key="reg_invite_key",
            )
            if not entered_key:
                st.caption("招待コードを入力してください。")
                if st.button("← 戻る", use_container_width=True):
                    st.session_state["view"] = "select_user"; st.rerun()
                return
            if entered_key != req_key:
                st.error("招待コードが違います 🔒")
                if st.button("← 戻る", use_container_width=True):
                    st.session_state["view"] = "select_user"; st.rerun()
                return

        name = st.text_input("お名前", placeholder="例：パパ、ママ、はなこ…", max_chars=20)
        st.markdown("**アバター**")
        atype = st.radio("", ["絵文字", "写真"], horizontal=True, label_visibility="collapsed")
        avatar_emoji = "🙂"; avatar_photo = None
        if atype == "絵文字":
            st.caption("スマホのキーボードから絵文字を選んでね 😊")
            avatar_emoji = st.text_input("", value="🙂", max_chars=8, label_visibility="collapsed") or "🙂"
        else:
            avatar_photo = st.file_uploader("", type=["jpg","jpeg","png","webp"], label_visibility="collapsed")
            if avatar_photo:
                try:
                    preview_bytes, _ = _fix_exif(avatar_photo)
                    st.image(preview_bytes, width=80)
                except Exception:
                    st.image(avatar_photo, width=80)
        st.markdown("**電話番号**（任意）")
        phone = st.text_input("", placeholder="例：09012345678（ハイフンなし）", label_visibility="collapsed",
                              key="reg_phone")
        st.caption("ハイフンなしで入力してください。電話番号でもログインできるようになります。")
        st.markdown("**パスワード**")
        pw  = st.text_input("", type="password", placeholder="4文字以上", label_visibility="collapsed")
        pw2 = st.text_input("パスワード（確認）", type="password", placeholder="もう一度入力")
        st.divider()
        # ── ボタン（主アクション→戻る の順） ──
        if st.button("✅ 登録する", use_container_width=True, type="primary"):
            if not name.strip(): st.error("お名前を入力してください"); return
            if atype == "写真" and not avatar_photo: st.error("写真を選択してください"); return
            if len(pw) < 4: st.error("パスワードは4文字以上"); return
            if pw != pw2: st.error("パスワードが一致しません"); return
            new_uid  = str(uuid.uuid4())
            final_av = (
                upload_photo(AVATAR_BUCKET, new_uid, avatar_photo) if atype == "写真"
                else avatar_emoji
            )
            with st.spinner("登録中…"):
                user = register_user(name.strip(), final_av, pw, uid=new_uid, phone=normalize_phone(phone))
                # ★ 新規登録は既定ルーム（main）へ自動参加（招待リンクの家族がすぐ使える）
                add_to_default_room(new_uid)
            do_login(user); st.rerun()
        if st.button("← 戻る", use_container_width=True):
            st.session_state["view"] = "select_user"; st.rerun()

# ─────────────────────────────────────
# 画面④ プロフィール編集
# ─────────────────────────────────────
_PROFILE_WIDGET_KEYS = ("profile_atype", "profile_emoji", "profile_photo", "profile_name", "profile_phone")

def _reset_profile_widgets() -> None:
    """プロフィール画面を開くたびにウィジェット状態をクリアする。
    これにより radio・テキスト・ファイルアップローダーが現在値を初期値として表示する。"""
    for k in _PROFILE_WIDGET_KEYS:
        st.session_state.pop(k, None)
    st.session_state.pop("_logout_confirm", None)   # ログアウト確認状態もリセット

def show_profile(current_user: dict) -> None:
    import time as _time
    _, col, _ = st.columns([1, 3, 1])
    with col:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("## 👤 プロフィール編集")
        st.divider()

        # 現在のアイコン表示
        av = current_user["avatar"]
        if av.startswith("http"):
            c1, c2 = st.columns([1, 3])
            with c1: st.image(av, width=80)
            with c2: st.markdown(f"### {current_user['name']}")
        else:
            st.markdown(f"# {av}")
            st.markdown(f"### {current_user['name']}")

        st.divider()

        # ── 表示名変更 ──
        st.markdown("**表示名を変更**")
        new_name = (
            st.text_input(
                "", value=current_user["name"],
                max_chars=20, label_visibility="collapsed", key="profile_name",
            ) or current_user["name"]
        ).strip()

        # ── 電話番号 ──
        st.markdown("**電話番号**（任意）")
        new_phone = normalize_phone(st.text_input(
            "", value=current_user.get("phone") or "",
            placeholder="例：09012345678（ハイフンなし）",
            label_visibility="collapsed", key="profile_phone",
        ).strip())
        st.caption("ハイフンなしで入力してください。電話番号でもログインできます。")

        st.divider()

        # ── アイコン変更 ──
        st.markdown("**アイコンを変更**")
        # 初回表示時のみ：現在のアバター種別（写真 or 絵文字）をデフォルト選択にする
        if "profile_atype" not in st.session_state:
            st.session_state["profile_atype"] = "写真" if av.startswith("http") else "絵文字"
        atype = st.radio(
            "", ["絵文字", "写真"], horizontal=True,
            label_visibility="collapsed", key="profile_atype",
        )

        new_avatar: str | None = None
        avatar_photo = None

        if atype == "絵文字":
            st.caption("スマホのキーボードから絵文字を選んでね 😊")
            cur_emoji = av if not av.startswith("http") else "🙂"
            new_emoji = (
                st.text_input("", value=cur_emoji, max_chars=8,
                              label_visibility="collapsed", key="profile_emoji")
                or cur_emoji
            )
            st.markdown(f"プレビュー: {new_emoji} **{new_name}**")
            new_avatar = new_emoji
        else:
            avatar_photo = st.file_uploader(
                "", type=["jpg", "jpeg", "png", "webp"],
                label_visibility="collapsed", key="profile_photo",
            )
            if avatar_photo:
                # EXIF 自動回転を適用してからプレビュー表示
                try:
                    preview_bytes, _ = _fix_exif(avatar_photo)
                    st.image(preview_bytes, width=100)
                except Exception:
                    st.image(avatar_photo, width=100)
                st.caption("この写真に変更します")

        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ 保存", type="primary", use_container_width=True, key="profile_save"):
                if not new_name:
                    st.error("表示名を入力してください"); return
                try:
                    if atype == "絵文字":
                        final_av = new_avatar or av
                    else:
                        if not avatar_photo:
                            st.error("写真を選択してください"); return
                        with st.spinner("アップロード中…"):
                            final_av = upload_photo(AVATAR_BUCKET, current_user["id"], avatar_photo)

                    with st.spinner("保存中…"):
                        update_user_profile(
                            current_user["id"],
                            old_name=current_user["name"],
                            new_name=new_name,
                            avatar=final_av,
                            phone=new_phone,
                        )

                    # セッションを即時反映
                    st.session_state["current_user"]["name"]   = new_name
                    st.session_state["current_user"]["avatar"] = final_av
                    st.session_state["current_user"]["phone"]  = new_phone
                    _reset_profile_widgets()   # 次回オープン時に再初期化
                    st.success("✅ プロフィールを更新しました！")
                    _time.sleep(0.3)
                    st.session_state["view"] = "chat"
                    st.session_state["_show_rooms"] = True
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 保存に失敗しました: {e}")
        with c2:
            if st.button("← 戻る", use_container_width=True, key="profile_back"):
                st.session_state["view"] = "chat"
                st.session_state["_show_rooms"] = True
                st.rerun()

        st.markdown("<div style='margin-top:8px'></div>", unsafe_allow_html=True)
        if st.button("🔔 通知設定", use_container_width=True, key="profile_to_notif"):
            st.session_state["view"] = "notifications"
            st.rerun()

        # ── 家族を招待 ──
        st.divider()
        st.markdown("### 📨 家族を招待")
        st.caption("このリンクを家族に送ると、サインアップ画面が開きます（チャットは登録するまで見えません）。")
        st.code(_invite_url(), language=None)

        # ── ログアウト（2 段階確認） ──
        st.divider()
        if st.session_state.get("_logout_confirm"):
            st.warning("本当にログアウトしますか？")
            lc1, lc2 = st.columns(2)
            with lc1:
                if st.button("いいえ", use_container_width=True, key="logout_no"):
                    st.session_state.pop("_logout_confirm", None)
                    st.rerun()
            with lc2:
                if st.button("はい、ログアウト", type="primary",
                             use_container_width=True, key="logout_yes"):
                    st.session_state.pop("_logout_confirm", None)
                    do_logout()
                    st.rerun()
        else:
            if st.button("🔒 ログアウト", use_container_width=True, key="logout_start"):
                st.session_state["_logout_confirm"] = True
                st.rerun()

# ─────────────────────────────────────
# 画面⑤ ルーム編集
# ─────────────────────────────────────
_ROOM_EDIT_WIDGET_KEYS = ("room_edit_atype", "room_edit_emoji", "room_edit_photo", "room_edit_name", "room_edit_add_members")

def _reset_room_edit_widgets() -> None:
    """ルーム編集画面を開くたびにウィジェット状態をリセットする。"""
    for k in _ROOM_EDIT_WIDGET_KEYS:
        st.session_state.pop(k, None)
    # 削除確認フラグ（ルーム削除・メンバー削除）もクリア
    for k in list(st.session_state.keys()):
        if k.startswith("room_delete_confirm_") or k.startswith("_rm_member_confirm_"):
            st.session_state.pop(k, None)

def show_room_edit(room: dict) -> None:
    import time as _time

    if not room or not room.get("id"):
        st.session_state["view"] = "chat"
        st.session_state["_show_rooms"] = True
        st.rerun()
        return

    room_id   = room.get("id",   "")
    room_name = room.get("name", "")
    room_icon = room.get("icon", "💬")

    _, col, _ = st.columns([1, 3, 1])
    with col:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("## ⚙️ ルーム編集")
        st.divider()

        # 現在のアイコン・名前プレビュー
        if room_icon.startswith("http"):
            c1, c2 = st.columns([1, 3])
            with c1: st.image(room_icon, width=60)
            with c2: st.markdown(f"### {room_name}")
        else:
            st.markdown(f"# {room_icon}　{room_name}")

        st.divider()

        # ── ルーム名変更 ──
        st.markdown("**ルーム名を変更**")
        new_name = (
            st.text_input("", value=room_name, max_chars=30,
                          label_visibility="collapsed", key="room_edit_name")
            or room_name
        ).strip()

        # ── アイコン変更 ──
        st.divider()
        st.markdown("**アイコンを変更**")
        if "room_edit_atype" not in st.session_state:
            st.session_state["room_edit_atype"] = "写真" if room_icon.startswith("http") else "絵文字"
        atype = st.radio("", ["絵文字", "写真"], horizontal=True,
                         label_visibility="collapsed", key="room_edit_atype")

        new_icon: str | None = None
        icon_photo = None

        if atype == "絵文字":
            st.caption("スマホのキーボードから絵文字を選んでね 😊")
            cur_emoji = room_icon if not room_icon.startswith("http") else "💬"
            new_emoji = (
                st.text_input("", value=cur_emoji, max_chars=8,
                              label_visibility="collapsed", key="room_edit_emoji")
                or cur_emoji
            )
            st.markdown(f"プレビュー: {new_emoji} **{new_name}**")
            new_icon = new_emoji
        else:
            icon_photo = st.file_uploader("", type=["jpg", "jpeg", "png", "webp"],
                                          label_visibility="collapsed", key="room_edit_photo")
            if icon_photo:
                try:
                    preview_bytes, _ = _fix_exif(icon_photo)
                    st.image(preview_bytes, width=80)
                except Exception:
                    st.image(icon_photo, width=80)
                st.caption("この写真に変更します")

        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ 保存", type="primary", use_container_width=True, key="room_edit_save"):
                if not new_name:
                    st.error("ルーム名を入力してください"); return
                try:
                    if atype == "絵文字":
                        final_icon = new_icon or room_icon
                    else:
                        if not icon_photo:
                            st.error("写真を選択してください"); return
                        with st.spinner("アップロード中…"):
                            final_icon = upload_photo(AVATAR_BUCKET, f"room_{room_id}", icon_photo)

                    with st.spinner("保存中…"):
                        update_room(room_id, room_name, new_name, final_icon)

                    # アクティブルームが変更されたルームなら新しい名前に追随
                    if st.session_state.get("active_room") == room_name:
                        st.session_state["active_room"] = new_name

                    st.success("✅ ルームを更新しました！")
                    _time.sleep(0.3)
                    _reset_room_edit_widgets()
                    st.session_state["view"] = "chat"
                    st.session_state["_show_rooms"] = True
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 保存に失敗しました: {e}")
        with c2:
            if st.button("← 戻る", use_container_width=True, key="room_edit_back"):
                _reset_room_edit_widgets()
                st.session_state["view"] = "chat"
                st.session_state["_show_rooms"] = True
                st.rerun()

        # ── メンバー管理（招待制）──
        st.divider()
        st.markdown("### 👥 メンバー")
        _me_id   = st.session_state.get("current_user", {}).get("id", "")
        _members = fetch_room_members(room_id)
        _member_ids = {m["id"] for m in _members}
        st.caption(f"このルームに参加しているメンバー（{len(_members)}人）")
        for m in _members:
            _av = m.get("avatar", "🙂")
            _icon = "🖼️" if _av.startswith("http") else _av
            _rm_key = f"_rm_member_confirm_{m['id']}"
            # 確認中（✕ を押した後）→ 本当に外すか はい/いいえ
            if st.session_state.get(_rm_key):
                st.warning(f"{_icon}　**{m['name']}** さんをこのルームから外しますか？")
                rc1, rc2 = st.columns(2)
                with rc1:
                    if st.button("いいえ", use_container_width=True, key=f"rm_no_{m['id']}"):
                        st.session_state.pop(_rm_key, None)
                        st.rerun()
                with rc2:
                    if st.button("はい、外す", type="primary", use_container_width=True,
                                 key=f"rm_yes_{m['id']}"):
                        remove_room_member(room_id, m["id"])
                        st.session_state.pop(_rm_key, None)
                        st.toast(f"{m['name']} さんを外しました", icon="👋")
                        st.rerun()
                continue
            mc1, mc2 = st.columns([6, 1])
            with mc1:
                _suffix = "　（あなた）" if m["id"] == _me_id else ""
                st.markdown(f"{_icon}　**{m['name']}**{_suffix}")
            with mc2:
                # 自分以外は外せる（自分を外すと自分がルームを見られなくなるため不可）
                if m["id"] != _me_id:
                    if st.button("✕", key=f"rm_member_{m['id']}", help="このメンバーを外す"):
                        st.session_state[_rm_key] = True   # まず確認を表示
                        st.rerun()

        # 追加候補（まだ参加していないユーザー）
        _all_users  = fetch_all_users()
        _candidates = [u for u in _all_users if u["id"] not in _member_ids]
        if _candidates:
            _name_to_id = {u["name"]: u["id"] for u in _candidates}
            _picked = st.multiselect(
                "メンバーを追加", list(_name_to_id.keys()),
                key="room_edit_add_members",
                placeholder="招待する家族を選ぶ",
            )
            if st.button("＋ 追加して招待", use_container_width=True, key="room_edit_add_btn"):
                if _picked:
                    for _nm in _picked:
                        add_room_member(room_id, _name_to_id[_nm])
                    st.success(f"{len(_picked)}人を追加しました！")
                    st.session_state.pop("room_edit_add_members", None)
                    _time.sleep(0.3)
                    st.rerun()
                else:
                    st.warning("追加するメンバーを選んでください")
        else:
            st.caption("全員がこのルームに参加済みです 🎉")

        # ── ルーム削除（2 段階確認） ──
        st.divider()
        st.markdown("### 🗑️ ルームの削除")
        st.warning("⚠️ 削除するとこのルームのすべてのメッセージが完全に失われます。元に戻せません。")

        delete_confirm_key = f"room_delete_confirm_{room_id}"
        if st.session_state.get(delete_confirm_key):
            st.error("🚨 本当に削除しますか？この操作は絶対に取り消せません。")
            cd1, cd2 = st.columns(2)
            with cd1:
                if st.button("✕ キャンセル", use_container_width=True, key="room_delete_cancel"):
                    st.session_state.pop(delete_confirm_key, None)
                    st.rerun()
            with cd2:
                if st.button("🗑️ 完全に削除", type="primary", use_container_width=True,
                             key="room_delete_exec"):
                    try:
                        with st.spinner("削除中…"):
                            delete_room(room_id, room_name)
                        # 削除されたルームがアクティブなら残りの先頭ルームへ
                        if st.session_state.get("active_room") == room_name:
                            _me = st.session_state.get("current_user", {}).get("id", "")
                            remaining = fetch_rooms(_me)
                            st.session_state["active_room"] = remaining[0]["name"] if remaining else ""
                        st.session_state.pop(delete_confirm_key, None)
                        _reset_room_edit_widgets()
                        st.session_state["view"] = "chat"
                        st.session_state["_show_rooms"] = True
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ 削除に失敗しました: {e}")
        else:
            if st.button("🗑️ このルームを削除する", use_container_width=True,
                         key="room_delete_start"):
                st.session_state[delete_confirm_key] = True
                st.rerun()

# ─────────────────────────────────────
# 画面⑤-b ルーム作成（room_edit から削除機能を除いたもの）
# ─────────────────────────────────────
_ROOM_CREATE_WIDGET_KEYS = ("room_create_atype", "room_create_emoji", "room_create_photo", "room_create_name")

def _reset_room_create_widgets() -> None:
    for k in _ROOM_CREATE_WIDGET_KEYS:
        st.session_state.pop(k, None)

def show_room_create() -> None:
    import time as _time

    _, col, _ = st.columns([1, 3, 1])
    with col:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("## ✨ 新しいルーム")
        st.divider()

        # ── ルーム名 ──
        st.markdown("**ルーム名**")
        new_name = (
            st.text_input("", placeholder="例：おでかけ計画", max_chars=30,
                          label_visibility="collapsed", key="room_create_name")
            or ""
        ).strip()

        # ── アイコン ──
        st.divider()
        st.markdown("**アイコン**")
        atype = st.radio("", ["絵文字", "写真"], horizontal=True,
                         label_visibility="collapsed", key="room_create_atype")

        new_icon: str = "💬"
        icon_photo = None

        if atype == "絵文字":
            st.caption("スマホのキーボードから絵文字を選んでね 😊")
            new_icon = (
                st.text_input("", value="💬", max_chars=8,
                              label_visibility="collapsed", key="room_create_emoji")
                or "💬"
            )
            if new_name:
                st.markdown(f"プレビュー: {new_icon} **{new_name}**")
        else:
            icon_photo = st.file_uploader("", type=["jpg", "jpeg", "png", "webp"],
                                          label_visibility="collapsed", key="room_create_photo")
            if icon_photo:
                try:
                    preview_bytes, _ = _fix_exif(icon_photo)
                    st.image(preview_bytes, width=80)
                except Exception:
                    st.image(icon_photo, width=80)
                st.caption("このアイコンで作成します")

        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ 作成", type="primary", use_container_width=True, key="room_create_save"):
                if not new_name:
                    st.error("ルーム名を入力してください"); return
                # 同名ルームの重複チェック
                existing = [r["name"] for r in fetch_rooms()]
                if new_name in existing:
                    st.error(f"「{new_name}」はすでに存在します"); return
                try:
                    if atype == "写真":
                        if not icon_photo:
                            st.error("写真を選択してください"); return
                        with st.spinner("アップロード中…"):
                            tmp_id = str(uuid.uuid4())
                            icon_url = upload_photo(AVATAR_BUCKET, f"room_{tmp_id}", icon_photo)
                            new_icon = icon_url
                    with st.spinner("作成中…"):
                        _creator = st.session_state.get("current_user", {}).get("id", "")
                        new_room = create_room(new_name, new_icon, creator_id=_creator)
                    st.success(f"✅ 「{new_name}」を作成しました！")
                    _time.sleep(0.3)
                    # 作成したルームをアクティブにしてチャットへ
                    st.session_state["active_room"] = new_name
                    _reset_room_create_widgets()
                    st.session_state["view"] = "chat"
                    st.session_state.pop("_show_rooms", None)
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 作成に失敗しました: {e}")
        with c2:
            if st.button("← 戻る", use_container_width=True, key="room_create_back"):
                _reset_room_create_widgets()
                st.session_state["view"] = "chat"
                st.session_state["_show_rooms"] = True
                st.rerun()

# ─────────────────────────────────────
# ★ ルーム選択パネル（フラグメント・5秒ごと自動更新）
#   未読バッジをリアルタイムで反映する。
# ─────────────────────────────────────
@st.fragment(run_every="5s")
def render_room_list() -> None:
    # ルーム選択中でない・未ログインならフラグメントを空にして終了
    # （ログアウト後や画面遷移後にフラグメントが残らないようにする）
    current_user = st.session_state.get("current_user")
    if not current_user or not st.session_state.get("_show_rooms", False):
        return
    _all_rooms      = fetch_rooms(current_user["id"])
    _all_room_names = [r["name"] for r in _all_rooms]
    selected_room   = st.session_state.get("active_room") or (_all_room_names[0] if _all_room_names else "")
    unread = get_unread_counts(current_user["id"], _all_room_names)

    # ─ セクションラベルのスタイル（iOS Settings 風小文字グレー） ─
    _sec_label = (
        'display:block;font-size:0.68rem;font-weight:600;'
        'color:rgba(255,255,255,0.38);letter-spacing:0.07em;'
        'text-transform:uppercase;padding:18px 4px 6px'
    )
    _first_sec_label = (
        'display:block;font-size:0.68rem;font-weight:600;'
        'color:rgba(255,255,255,0.38);letter-spacing:0.07em;'
        'text-transform:uppercase;padding:6px 4px 6px'
    )

    # ═══ 通知バナー（DB にサブスクリプションがなければ表示）═══
    _show_push_banner = (
        bool(_vapid_cfg().get("vapid_public_key"))
        and not has_push_subscription(current_user["id"])
    )

    # ナビゲーション時のみスライドイン演出（fragment の定期更新では再生しない）
    _anim = st.session_state.pop("_nav_anim", "")
    _anim_css = "animation:danranSlideInLeft 0.22s ease-out;" if _anim == "left" else ""

    # ═══ Section 1: チャットルーム ═══
    rows: list[str] = [
        # ルーム行の押下/選択フィードバック用スタイル
        '<style>'
        '#_danran_room_list button.dr-room{transition:background 0.12s;}'
        '#_danran_room_list button.dr-room:active{background:rgba(255,255,255,0.10)!important;}'
        '#_danran_room_list button.dr-room.dr-selected{background:rgba(240,168,104,0.22)!important;}'
        '</style>'
        f'<div id="_danran_room_list" style="padding-bottom:20px;{_anim_css}">',
    ]

    if _show_push_banner:
        rows.append(
            '<div id="_danran_push_banner" style="'
            'display:flex;align-items:center;gap:10px;'
            'background:rgba(240,168,104,0.12);'
            'border:1px solid rgba(240,168,104,0.30);'
            'border-radius:12px;padding:12px 14px;margin-bottom:12px;'
            'cursor:pointer;-webkit-tap-highlight-color:transparent;">'
            '<span style="font-size:1.4rem;flex-shrink:0">🔔</span>'
            '<div style="flex:1">'
            '<div style="font-size:0.88rem;font-weight:600;color:#fff">通知を有効にする</div>'
            '<div style="font-size:0.75rem;color:rgba(255,255,255,0.5);margin-top:2px">'
            '新着メッセージを iPhone に通知'
            '</div></div>'
            '<span style="color:rgba(255,255,255,0.25);font-size:1.1rem">›</span>'
            '</div>'
        )

    rows.extend([
        # ラベル + 新規ルーム作成ボタンを横並び
        '<div style="display:flex;align-items:center;justify-content:space-between;'
        'padding:6px 4px 6px">',
        f'<span style="{_first_sec_label};padding:0">チャットルーム</span>',
        '<button data-room-create="true" '
        'style="width:30px;height:30px;border-radius:50%;'
        'background:rgba(255,255,255,0.14);border:none;'
        'color:#fff;font-size:1.2rem;line-height:1;cursor:pointer;'
        'display:flex;align-items:center;justify-content:center;'
        'flex-shrink:0;-webkit-tap-highlight-color:transparent">＋</button>',
        '</div>',
        # グループカード（iOS の grouped list 風）
        '<div style="background:rgba(255,255,255,0.06);'
        'border:1px solid rgba(255,255,255,0.1);'
        'border-radius:14px;overflow:hidden">',
    ])

    # 参加ルームが無い場合（新規ユーザー等）の案内
    if not _all_rooms:
        rows.append(
            '<div style="padding:20px 16px;text-align:center;color:rgba(255,255,255,0.5);'
            'font-size:0.85rem;line-height:1.7">'
            'まだ参加しているルームがありません。<br>'
            '右上の <b>＋</b> で作るか、家族に招待してもらってください。'
            '</div>'
        )

    for i, room in enumerate(_all_rooms):
        rname   = room["name"]
        ricon   = room.get("icon", "💬")
        room_id = room["id"]
        count   = unread.get(rname, 0)
        is_last = (i == len(_all_rooms) - 1)
        is_active = (rname == selected_room)

        badge = (
            f'<span style="background:#e0654f;color:#fff;border-radius:20px;'
            f'padding:1px 8px;font-size:0.68rem;font-weight:700;flex-shrink:0">'
            f'+{count}</span>'
        ) if count > 0 else ""

        # アクティブインジケーター（緑の点）
        active_dot = (
            '<span style="width:7px;height:7px;border-radius:50%;'
            'background:#f0a868;flex-shrink:0"></span>'
        ) if is_active else ""

        if ricon.startswith("http"):
            icon_html = (
                f'<img src="{_html.escape(ricon)}" '
                f'style="width:28px;height:28px;border-radius:7px;'
                f'object-fit:cover;flex-shrink:0">'
            )
        else:
            icon_html = (
                f'<span style="font-size:1.3rem;width:28px;text-align:center;'
                f'flex-shrink:0;line-height:1">{ricon}</span>'
            )

        # 行の下ボーダー（最終行以外）
        row_border = '' if is_last else 'border-bottom:1px solid rgba(255,255,255,0.07);'

        rows.append(
            f'<div style="display:flex;align-items:center;{row_border}">'
            # ── ルームナビボタン（左・flex:1） ──
            f'<button data-room-nav="{_html.escape(room_id)}" '
            f'data-room-name="{_html.escape(rname)}" class="dr-room" '
            f'style="flex:1;display:flex;align-items:center;gap:10px;'
            f'padding:12px 14px;min-height:52px;'
            f'background:none;border:none;color:#fff;'
            f'font-size:0.95rem;text-align:left;cursor:pointer;'
            f'-webkit-tap-highlight-color:transparent;font-family:inherit">'
            f'{icon_html}'
            f'<span style="flex:1;white-space:nowrap;overflow:hidden;'
            f'text-overflow:ellipsis">{_html.escape(rname)}</span>'
            f'{badge}{active_dot}'
            f'</button>'
            # ── 歯車ボタン（右・固定幅） ──
            f'<button data-room-gear="{_html.escape(room_id)}" '
            f'style="width:48px;min-height:52px;flex-shrink:0;'
            f'background:none;border:none;'
            f'border-left:1px solid rgba(255,255,255,0.07);'
            f'font-size:1.05rem;cursor:pointer;'
            f'color:rgba(255,255,255,0.45);'
            f'-webkit-tap-highlight-color:transparent">'
            f'⚙️</button>'
            f'</div>'
        )

    rows.append('</div>')  # グループカード閉じ

    # アカウント編集・ログアウトはヘッダー右上のアバター → プロフィール画面に集約
    # （ルーム選択画面にアカウント操作を置かない＝LINE 風 UX）
    rows.append('</div>')   # _danran_room_list

    st.markdown('\n'.join(rows), unsafe_allow_html=True)


# ─────────────────────────────────────
# 画面⑥ メインチャット
# ─────────────────────────────────────
def show_chat(current_user: dict) -> None:

    # ── ルーム状態 ──
    _all_rooms      = fetch_rooms(current_user["id"])
    _all_room_names = [r["name"] for r in _all_rooms]
    _default_room   = _all_room_names[0] if _all_room_names else ""
    # active_room 初期化 or 削除済みルームのフォールバック
    if "active_room" not in st.session_state or st.session_state["active_room"] not in _all_room_names:
        st.session_state["active_room"] = _default_room
    selected_room = st.session_state["active_room"]
    show_rooms    = st.session_state.get("_show_rooms", False)

    # ── ＜ を押したときのルーム選択パネル（フラグメント：5秒ごとに未読バッジを更新）──
    if show_rooms:
        render_room_list()
        return   # ルーム選択中はメッセージ非表示

    # メッセージ描画（変化駆動）＋ 2秒ポーラー（変化検知時のみ rerun）
    render_chat_messages(current_user)
    poll_messages()

    # ── 返信（引用）ターゲット（バー自体は JS が入力欄の上に固定描画する＝スクロール追従）──
    _reply = st.session_state.get("_reply_to")

    # ── テキスト入力 ──
    av_str2 = current_user["avatar"]
    # プレースホルダーは短く固定（名前を入れると折り返して最新メッセージが隠れるため）
    ph = "メッセージ" if av_str2.startswith("http") else f"{av_str2} メッセージ"
    if prompt := st.chat_input(ph, max_chars=2000):
        send_message(selected_room, current_user["id"], current_user["name"], current_user["avatar"], prompt,
                     reply_to=_reply if (_reply and _reply.get("id")) else None)
        st.session_state.pop("_reply_to", None)   # 返信ターゲットを消費
        # 送信した自分のメッセージを即描画するためキャッシュを破棄（次の描画で再ビルド）
        st.session_state.pop("_chat_html", None)
        st.rerun()

# ─────────────────────────────────────
# 画面⑦ PWA インストールページ（?install=1）
# Streamlit Cloud はカスタム HTTP ルートを遮断するため、
# st.download_button 経由で mobileconfig を配信する。
# ─────────────────────────────────────
def _build_mobileconfig() -> bytes:
    """danran Web Clip 用 .mobileconfig (plist XML) を生成して返す。"""
    import base64 as _b64
    import uuid as _uuid

    app_url = (
        (st.secrets.get("app") or {}).get("url")
        or os.environ.get("APP_URL", "")
        or "https://danran-dhawa6nhapcwnq6lrjqzhw.streamlit.app/"
    )

    icon_path = os.path.join(os.path.dirname(__file__), "icons", "icon-192.png")
    try:
        with open(icon_path, "rb") as f:
            icon_b64 = _b64.b64encode(f.read()).decode()
    except OSError:
        icon_b64 = ""

    profile_uuid = str(_uuid.uuid4()).upper()
    webclip_uuid = str(_uuid.uuid4()).upper()

    icon_elem = f"      <key>Icon</key><data>{icon_b64}</data>\n" if icon_b64 else ""

    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        '  <key>PayloadContent</key><array><dict>\n'
        '    <key>FullScreen</key><true/>\n'
        f'{icon_elem}'
        '    <key>IsRemovable</key><true/>\n'
        '    <key>Label</key><string>danran</string>\n'
        '    <key>PayloadDescription</key><string>danran ホーム画面に追加</string>\n'
        '    <key>PayloadDisplayName</key><string>danran</string>\n'
        '    <key>PayloadIdentifier</key><string>com.danran.webclip</string>\n'
        '    <key>PayloadType</key><string>com.apple.webClip.managed</string>\n'
        f'    <key>PayloadUUID</key><string>{webclip_uuid}</string>\n'
        '    <key>PayloadVersion</key><integer>1</integer>\n'
        f'    <key>URL</key><string>{app_url}</string>\n'
        '  </dict></array>\n'
        '  <key>PayloadDescription</key><string>danran をホーム画面に追加します</string>\n'
        '  <key>PayloadDisplayName</key><string>danran 🏠</string>\n'
        f'  <key>PayloadIdentifier</key><string>com.danran.profile.{profile_uuid.lower()}</string>\n'
        '  <key>PayloadRemovalDisallowed</key><false/>\n'
        '  <key>PayloadType</key><string>Configuration</string>\n'
        f'  <key>PayloadUUID</key><string>{profile_uuid}</string>\n'
        '  <key>PayloadVersion</key><integer>1</integer>\n'
        '</dict>\n</plist>'
    )
    return plist.encode("utf-8")


def show_install_page() -> None:
    """?install=1 でアクセスしたとき表示する iOS PWA インストールページ。
    ログイン不要・セッション不問でアクセス可能。
    【重要】mobileconfig (Web Clip) では通知・バッジが動かない。
    Safari の「ホーム画面に追加」= 正式 PWA インストールが必須。"""
    st.markdown("""
<style>
[data-testid="stToolbar"],[data-testid="stDecoration"],[data-testid="stSidebar"],
[data-testid="collapsedControl"],#MainMenu,[data-testid="stDeployButton"],
[class*="viewerBadge"]{display:none!important}
[data-testid="stMainBlockContainer"]>div:first-child{padding-top:2rem}
</style>""", unsafe_allow_html=True)

    st.markdown(
        '<div style="text-align:center;padding:16px 0 8px">'
        '<div style="font-size:3rem">🏠</div>'
        '<div style="font-size:1.4rem;font-weight:700;color:#fff;margin-top:8px">danran</div>'
        '<div style="font-size:0.85rem;color:rgba(255,255,255,0.5);margin-top:4px">家族専用チャット</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── ステップごとの大きな説明カード ────────────────────────────
    steps = [
        ("1", "🧭", "Safari で開く",
         "このページを <b>Safari</b> で開いてください。<br>"
         "<span style='color:rgba(255,255,255,0.45);font-size:0.8rem'>"
         "LINE や Chrome から開いている場合は、右上の「…」→「Safari で開く」</span>"),
        ("2", "…", "右下の「…」をタップ",
         "Safari 画面の <b>右下にある「…」</b>（3 点メニュー）をタップ<br>"
         "→ 出てきたメニューの <b>「共有」</b> をタップ"),
        ("3", "＋", "ホーム画面に追加",
         "共有メニューを <b>下にスクロール</b> して<br>"
         "「<b>ホーム画面に追加</b>」をタップ"),
        ("4", "✅", "「追加」をタップ",
         "右上の <b>「追加」</b> をタップすれば完了 🎉<br>"
         "ホーム画面に danran のアイコンが追加されます"),
        ("5", "🔔", "通知を許可する",
         "アイコンからアプリを開いて<br><b>通知の許可</b> を求められたら「許可」をタップ"),
    ]

    cards_html = '<div style="display:flex;flex-direction:column;gap:10px;margin:12px 0 20px">'
    for num, icon, title, desc in steps:
        cards_html += (
            f'<div style="display:flex;align-items:flex-start;gap:14px;'
            f'background:rgba(255,255,255,0.06);border-radius:14px;padding:14px 16px">'
            f'<div style="flex-shrink:0;width:42px;height:42px;border-radius:50%;'
            f'background:rgba(240,168,104,0.22);border:2px solid rgba(240,168,104,0.55);'
            f'display:flex;align-items:center;justify-content:center;'
            f'font-size:1.25rem;line-height:1">{icon}</div>'
            f'<div>'
            f'<div style="font-size:0.72rem;color:rgba(240,168,104,0.85);font-weight:700;'
            f'letter-spacing:.06em;margin-bottom:2px">STEP {num}</div>'
            f'<div style="font-size:0.95rem;font-weight:700;color:#fff;margin-bottom:4px">{title}</div>'
            f'<div style="font-size:0.82rem;color:rgba(255,255,255,0.65);line-height:1.6">{desc}</div>'
            f'</div></div>'
        )
    cards_html += '</div>'
    st.markdown(cards_html, unsafe_allow_html=True)

    st.markdown(
        '<div style="background:rgba(255,200,0,0.08);border:1px solid rgba(255,200,0,0.25);'
        'border-radius:10px;padding:10px 14px;font-size:0.8rem;'
        'color:rgba(255,255,255,0.55);line-height:1.6;margin-bottom:16px">'
        '⚠️ <b style="color:rgba(255,200,0,0.85)">必ず Safari を使ってください</b><br>'
        'LINE・Chrome から開いた場合は通知・バッジが動きません'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    # ── サブ: mobileconfig（アイコンのみ・通知なし）──────────────────
    st.markdown(
        '<div style="font-size:0.78rem;color:rgba(255,255,255,0.35);'
        'text-align:center;padding:4px 0 8px">'
        '上の手順が難しい場合のみ ↓（通知・バッジは使えません）'
        '</div>',
        unsafe_allow_html=True,
    )
    mobileconfig_bytes = _build_mobileconfig()
    st.download_button(
        label="プロファイルでアイコンだけ追加（通知なし）",
        data=mobileconfig_bytes,
        file_name="danran.mobileconfig",
        mime="application/x-apple-aspen-config",
        use_container_width=True,
        type="secondary",
    )

    st.markdown(
        '<div style="font-size:0.72rem;color:rgba(255,255,255,0.2);text-align:center;'
        'margin-top:10px">iOS 16.4 以上 / Safari 推奨</div>',
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────
# 画面⑧ 通知設定
# ─────────────────────────────────────
def show_notifications(current_user: dict) -> None:
    """通知設定画面 — JS が Notification.permission を読んで UI を更新する。"""

    # 現在の許可状態を JS が書き込むプレースホルダー
    st.html('<div id="_danran_notif_ui"></div>')

    # 通知の説明（JS がステータス表示を上書きする前のフォールバック兼ガイド）
    st.markdown("""
<div style="color:rgba(255,255,255,0.55);font-size:0.85rem;line-height:1.6;
            padding:0 2px 16px">
  <b style="color:#fff">📱 ホーム画面追加が必要です</b><br>
  iOS の Web Push は <b>「ホーム画面に追加」したアプリ</b>（iOS 16.4以上）からのみ動作します。
  Safari のブラウザタブからは通知は届きません。
</div>
""", unsafe_allow_html=True)

    # ── デバッグパネル ──────────────────────────────────────────────────
    with st.expander("🔧 デバッグ情報", expanded=True):
        uid = current_user.get("id", "")
        uname = current_user.get("name", "")

        # VAPID 設定チェック
        vcfg = _vapid_cfg()
        has_priv = bool(vcfg.get("vapid_private_key"))
        has_pub  = bool(vcfg.get("vapid_public_key"))
        has_subj = bool(vcfg.get("vapid_subject"))
        st.markdown(f"**VAPID 設定**: 秘密鍵={has_priv} 公開鍵={has_pub} subject={has_subj}")

        # 購読状況チェック
        try:
            all_subs = supabase.table("push_subscriptions")\
                .select("user_id, endpoint")\
                .execute().data or []
            my_subs = [s for s in all_subs if s.get("user_id") == uid]
            st.markdown(f"**購読数（全体）**: {len(all_subs)} 件")
            st.markdown(f"**購読数（自分）**: {len(my_subs)} 件")
            if my_subs:
                for s in my_subs:
                    ep = s.get("endpoint", "")
                    st.code(f"endpoint: ...{ep[-50:]}" if ep else "(空)", language=None)
            else:
                st.warning("⚠️ あなたの購読がDBに登録されていません")
        except Exception as e:
            st.error(f"購読取得エラー: {e}")

        # 購読リセット（VapidPkHashMismatch などキー不一致の修復用）
        st.markdown("---")
        st.markdown("**購読をリセットする**（キー不一致エラーの修復）")
        if st.button("🗑️ 購読をブラウザごとリセット", key="push_reset_btn"):
            try:
                supabase.table("push_subscriptions").delete().eq("user_id", uid).execute()
                # JS へ「ブラウザ側も unsubscribe して再登録せよ」フラグをセット
                st.session_state["_push_force_resubscribe"] = True
                st.rerun()
            except Exception as e:
                st.error(f"❌ 削除エラー: {e}")

        # テスト通知送信
        st.markdown("---")
        st.markdown("**自分宛にテスト通知を送る**")
        if st.button("📤 テスト通知を送信", key="push_test_btn"):
            try:
                from pywebpush import webpush, WebPushException
                priv = vcfg.get("vapid_private_key", "")
                subj = vcfg.get("vapid_subject", "")
                test_rows = supabase.table("push_subscriptions")\
                    .select("endpoint, p256dh, auth")\
                    .eq("user_id", uid)\
                    .execute().data or []
                if not test_rows:
                    st.error("❌ 購読がありません。まず通知を許可してください。")
                elif not (priv and subj):
                    st.error("❌ VAPIDキーが設定されていません。")
                else:
                    ok_cnt = 0
                    for row in test_rows:
                        try:
                            import json as _j
                            webpush(
                                subscription_info={
                                    "endpoint": row["endpoint"],
                                    "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
                                },
                                data=_j.dumps({
                                    "title": "danran テスト通知",
                                    "body": f"{uname} さん、通知は正常に動作しています！",
                                    "url": "/",
                                }, ensure_ascii=False),
                                vapid_private_key=priv,
                                vapid_claims={"sub": subj},
                            )
                            ok_cnt += 1
                        except WebPushException as ex:
                            st.error(f"❌ WebPushException: {ex}")
                        except Exception as ex:
                            st.error(f"❌ 送信エラー: {ex}")
                    if ok_cnt:
                        st.success(f"✅ {ok_cnt} 件送信しました！通知が届くか確認してください。")
            except Exception as e:
                st.error(f"❌ 予期しないエラー: {e}")

# ─────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────

# embed=true を URL から除去（Streamlit embed モード = chat input 非表示を防ぐ）
# JS コンポーネントから location.replace() は sandbox に阻まれるため Python 側で処理する
if "embed" in st.query_params:
    del st.query_params["embed"]
    st.rerun()

# ① セッション復元は localStorage 経由のみ（JS が restore_session を送る）。
#   ★ 旧実装は ?s=SESSION_ID（URL）から自動ログインしていたが、URL を共有すると
#     受け取った人が共有者としてログイン状態になりチャットが丸見えになる重大な穴だった。
#     URL からのセッション復元は完全に廃止する。
#   後方互換: 万一 URL に ?s= が残っていても無視し、痕跡を消す。
if SESSION_PARAM in st.query_params:
    try:
        del st.query_params[SESSION_PARAM]
    except Exception:
        pass

# ② 招待リンク: ?invite=<招待コード> で新規登録画面へ誘導（未ログイン時のみ）。
#   コードが一致すれば登録画面で招待コード入力をスキップする（家族が手間なくサインアップ）。
#   ★ 招待リンクはサインアップ画面に着地するだけ。ログインも、チャット表示もしない。
if "current_user" not in st.session_state and "invite" in st.query_params:
    _inv = st.query_params.get("invite") or ""
    st.session_state["view"] = "register"
    _rk = _get_register_key()
    if _rk and _inv == _rk:
        st.session_state["_invite_ok"] = True
    try:
        del st.query_params["invite"]
    except Exception:
        pass

# ── DOM config 要素（JS コンポーネントが直接読む設定ストア）──
# render イベントのタイミング問題を回避するため、
# Python が HTML data 属性として埋め込み JS が window.parent.document から参照。
_cu          = st.session_state.get("current_user", {})
_clear_flag  = st.session_state.pop("_clear_session", False)
# JS に「ブラウザ側も unsubscribe して再登録せよ」を伝えるフラグ（1回のみ）
_push_resub  = st.session_state.pop("_push_force_resubscribe", False)
# プロフィール・ルーム編集画面中は JS カメラボタンを非表示にするため active_room を空にする
_is_profile  = st.session_state.get("view") in ("profile", "room_edit", "notifications")
_active_room_id = ""
if "current_user" in st.session_state and not _is_profile:
    # active_room が未セット（セッション復元直後）のときは参加ルームの先頭をフォールバック
    _rooms_for_hdr = fetch_rooms(_cu.get("id", ""))
    _active_room   = st.session_state.get("active_room") or (
        _rooms_for_hdr[0]["name"] if _rooms_for_hdr else ""
    )
    # チャットヘッダーのメンバー管理ボタン用に active room の id を引く
    _active_room_id = next(
        (r["id"] for r in _rooms_for_hdr if r["name"] == _active_room), ""
    )
else:
    _active_room = ""
_show_rooms  = st.session_state.get("_show_rooms", False)
_cur_view    = st.session_state.get("view", "")
_vapid_pub = _vapid_cfg().get("vapid_public_key", "")
# Supabase URL/key: st.secrets → 環境変数 の順でフォールバック（Render 対応）
_sb_url = ((st.secrets.get("supabase") or {}).get("url") or os.environ.get("SUPABASE_URL", ""))
_sb_key = ((st.secrets.get("supabase") or {}).get("anon_key") or os.environ.get("SUPABASE_ANON_KEY", ""))

# ── ヘッダーを Python 側でレンダリング ──
# JS 注入ではなく Python が st.html() で直接 DOM に書くことでタイミング問題を解消。
# クリックハンドラだけは JS コンポーネント(attachHdrButtons)が付与する。
_HDR_DIV_STYLE = (
    'position:fixed;top:0;left:0;right:0;height:52px;z-index:2147483647;'
    'background:rgba(36,31,28,0.97);border-bottom:1px solid rgba(255,255,255,0.08);'
    'display:flex;align-items:center;padding:0 4px;gap:0;'
    'box-shadow:0 1px 10px rgba(0,0,0,0.5);'
    'backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);'
    'user-select:none;-webkit-user-select:none;'
)
_HDR_BTN_STYLE = (
    'background:none;border:none;color:#e0e0e0;font-size:1.25rem;'
    'cursor:pointer;padding:8px 12px;border-radius:10px;line-height:1;'
    'flex-shrink:0;min-width:44px;text-align:center;'
    '-webkit-tap-highlight-color:transparent;'
)
_HDR_TITLE_STYLE = (
    'flex:1;text-align:center;font-size:1rem;font-weight:700;color:#fff;'
    'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 4px;'
)
_hdr_html = ""
if "current_user" in st.session_state:
    if _is_profile:
        _title_map = {
            "profile":       "プロフィール編集",
            "room_edit":     "ルーム編集",
            "notifications": "🔔 通知設定",
        }
        _hdr_title_text = _title_map.get(_cur_view, "設定")
        _hdr_html = (
            f'<div id="_danran_hdr" style="{_HDR_DIV_STYLE}">'
            f'<button data-hdr-back style="{_HDR_BTN_STYLE}">＜</button>'
            f'<div style="{_HDR_TITLE_STYLE}">{_html.escape(_hdr_title_text)}</div>'
            f'<div style="flex-shrink:0;min-width:44px;"></div>'
            f'</div>'
        )
    elif _show_rooms or _active_room:
        # _show_rooms 時は参加ルーム0でもヘッダーを出す（アバター→プロフィール導線を確保）
        _hdr_btn_text  = "＜"
        _hdr_title_text = "ルーム選択" if _show_rooms else _active_room
        # ルーム選択画面では右上にアバターボタンを置きプロフィール画面へ誘導（LINE 風）
        if _show_rooms:
            _hdr_av = _cu.get("avatar", "") or "🙂"
            if _hdr_av.startswith("http"):
                _hdr_av_inner = (
                    f'<img src="{_html.escape(_hdr_av)}" '
                    f'style="width:32px;height:32px;border-radius:50%;'
                    f'object-fit:cover;display:block">'
                )
            else:
                _hdr_av_inner = (
                    f'<span style="width:32px;height:32px;border-radius:50%;'
                    f'background:rgba(255,255,255,0.12);display:flex;'
                    f'align-items:center;justify-content:center;font-size:1.2rem">'
                    f'{_html.escape(_hdr_av)}</span>'
                )
            _hdr_right = (
                f'<button data-hdr-profile style="background:none;border:none;'
                f'padding:6px;cursor:pointer;flex-shrink:0;min-width:44px;'
                f'display:flex;align-items:center;justify-content:center;'
                f'-webkit-tap-highlight-color:transparent">'
                f'{_hdr_av_inner}</button>'
            )
            # ルーム選択はトップ画面なので戻る（＜）ボタンは出さない
            _hdr_left = '<div style="flex-shrink:0;min-width:44px;"></div>'
        else:
            # チャット画面: 右上に「👥」メンバー管理ボタン → ルーム編集（メンバー画面）へ
            if _active_room_id:
                _hdr_right = (
                    f'<button data-hdr-roomedit="{_html.escape(_active_room_id)}" '
                    f'style="{_HDR_BTN_STYLE}font-size:1.1rem">👥</button>'
                )
            else:
                _hdr_right = '<div style="flex-shrink:0;min-width:44px;"></div>'
            _hdr_left = (
                f'<button data-hdr-nav style="{_HDR_BTN_STYLE}">'
                f'{_html.escape(_hdr_btn_text)}</button>'
            )
        _hdr_html = (
            f'<div id="_danran_hdr" style="{_HDR_DIV_STYLE}">'
            f'{_hdr_left}'
            f'<div style="{_HDR_TITLE_STYLE}">{_html.escape(_hdr_title_text)}</div>'
            f'{_hdr_right}'
            f'</div>'
        )

st.html(
    # PWA manifest + iOS メタタグ（毎 rerun で同じ内容を書くが副作用なし）
    '<link rel="manifest" href="/manifest.json">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    '<meta name="apple-mobile-web-app-title" content="danran">'
    '<link rel="apple-touch-icon" href="/icons/icon-192.png">'
    # DOM config（JS が参照するデータ属性）
    f'<div id="_danran_cfg" style="position:absolute;width:0;height:0;overflow:hidden;pointer-events:none" '
    f'data-sb-url="{_html.escape(_sb_url)}" '
    f'data-sb-key="{_html.escape(_sb_key)}" '
    f'data-user="{_html.escape(_cu.get("name",""))}" '
    f'data-avatar="{_html.escape(_cu.get("avatar",""))}" '
    f'data-room="{_html.escape(_active_room)}" '
    f'data-sess="{_html.escape(st.session_state.get("session_id",""))}" '
    f'data-clear="{str(_clear_flag).lower()}" '
    f'data-show-rooms="{str(_show_rooms).lower()}" '
    f'data-view="{_html.escape(_cur_view)}" '
    f'data-vapid-pub="{_html.escape(_vapid_pub)}" '
    f'data-uid="{_html.escape(_cu.get("id",""))}" '
    f'data-push-resub="{str(_push_resub).lower()}" '
    # ── 引用返信ターゲット（JS が入力欄の上に固定バーを描画する）──
    f'data-reply-id="{_html.escape((st.session_state.get("_reply_to") or {}).get("id","") or "")}" '
    f'data-reply-name="{_html.escape((st.session_state.get("_reply_to") or {}).get("name","") or "")}" '
    f'data-reply-text="{_html.escape((st.session_state.get("_reply_to") or {}).get("text","") or "")}" '
    f'data-reply-image="{_html.escape((st.session_state.get("_reply_to") or {}).get("image","") or "")}">'
    f'</div>'
    # ── Python レンダリングヘッダー ──
    # ログイン済み: チャットヘッダー or 編集ヘッダー
    # 未ログイン / active_room 未確定: 表示なし（JS フォールバックが担う）
    + (
        '<style>'
        '[data-testid="stMainBlockContainer"]'
        '{padding-top:62px!important;padding-bottom:160px!important;}'
        '</style>'
        + _hdr_html
        if _hdr_html else ""
    )
)

# ── グローバルコンポーネント（常時実行・ゼロ高さ）──
# ★ setValue を一切送らないので Python rerun は発生しない
#   セッション / Supabase 認証情報 / カメラ設定は上の DOM 要素から JS が読む。
#   render イベントは使わず、MutationObserver + scan() で常に最新値を取得。
_lp_result = _lp_detector(
    save_session  = st.session_state.get("session_id", ""),
    clear_session = _clear_flag,
    default       = None,
)

# ── ?install=1 → PWA インストールページ（ログイン不要）──
# ★ _lp_detector の後に置くことで JS コンポーネントが先にロードされ、
#    injectAutoInstall() が download ボタンを自動クリックできる。
if st.query_params.get("install") == "1":
    show_install_page()
    st.stop()

# JS ヘッダー ＜/✕ ボタン・アバタータップからのナビゲーション指示
# 全アクションに ts タイムスタンプを付与し、同じ値は一度だけ処理する
# （stSetValue のキャッシュが古い rerun でも再発火するのを防ぐ）
if isinstance(_lp_result, dict):
    _nav    = _lp_result.get("action", "")
    _nav_ts = _lp_result.get("ts", 0)
    _last_ts = st.session_state.get("_last_nav_ts", 0)
    if _nav and _nav_ts and _nav_ts != _last_ts:
        st.session_state["_last_nav_ts"] = _nav_ts
        if _nav == "go_rooms":
            st.session_state["_show_rooms"] = True
            st.session_state["_nav_anim"] = "left"   # ルーム選択を左からスライドイン
            st.rerun()
        elif _nav == "go_chat":
            st.session_state.pop("_show_rooms", None)
            st.rerun()
        elif _nav == "go_profile":
            _reset_profile_widgets()
            st.session_state["view"] = "profile"
            st.rerun()
        elif _nav == "go_back":
            # 編集画面ヘッダーの ＜ → ルームリストに戻る
            cur = st.session_state.get("view", "")
            if cur == "profile":
                _reset_profile_widgets()
            elif cur == "room_edit":
                _reset_room_edit_widgets()
            st.session_state["view"] = "chat"
            st.session_state["_show_rooms"] = True
            st.session_state["_nav_anim"] = "left"
            st.rerun()
        elif _nav == "go_notifications":
            st.session_state["view"] = "notifications"
            st.rerun()
        elif _nav == "go_room":
            # JS ルームリストのルーム名ボタンクリック → そのルームに遷移
            _room_name = _lp_result.get("room_name", "")
            if _room_name:
                st.session_state["active_room"] = _room_name
                st.session_state.pop("_show_rooms", None)
                st.rerun()
        elif _nav == "go_room_edit":
            # JS ルームリストの ⚙️ クリック → ルーム編集画面
            _room_id = _lp_result.get("room_id", "")
            if _room_id:
                _found = [r for r in fetch_rooms(_cu.get("id", "")) if r["id"] == _room_id]
                if _found:
                    _reset_room_edit_widgets()
                    st.session_state["editing_room"] = _found[0]
                    st.session_state["view"] = "room_edit"
                    st.rerun()
        elif _nav == "go_room_create":
            # ルームリストの + ボタン → ルーム作成画面
            _reset_room_create_widgets()
            st.session_state["view"] = "room_create"
            st.rerun()
        elif _nav == "refresh_chat":
            # メッセージ削除後など: チャットHTMLキャッシュを捨ててDBから綺麗に再描画
            st.session_state.pop("_chat_html", None)
            st.rerun()
        elif _nav == "set_reply":
            # メッセージ長押し/左スワイプ → 引用返信ターゲットをセット（入力欄上にバー表示）
            _rid = _lp_result.get("reply_id", "")
            if _rid:
                st.session_state["_reply_to"] = {
                    "id":    _rid,
                    "name":  _lp_result.get("reply_name", ""),
                    "text":  _lp_result.get("reply_text", ""),
                    "image": _lp_result.get("reply_image", ""),
                }
                st.rerun()
        elif _nav == "clear_reply":
            # 引用バーの ✕ → 返信ターゲット解除
            st.session_state.pop("_reply_to", None)
            st.rerun()
        elif _nav == "set_invite":
            # 招待リンク ?invite=CODE を JS が実URLから読み取って通知（Worker 経由で
            # st.query_params に乗らないケースの保険）。未ログイン時のみ登録画面へ。
            if "current_user" not in st.session_state:
                _code = _lp_result.get("invite_code", "")
                _rk = _get_register_key()
                _need = (st.session_state.get("view") != "register"
                         or (bool(_rk and _code == _rk) and not st.session_state.get("_invite_ok")))
                st.session_state["view"] = "register"
                if _rk and _code == _rk:
                    st.session_state["_invite_ok"] = True
                if _need:
                    st.rerun()
        elif _nav == "restore_session":
            # JS コンポーネントが localStorage からセッションIDを読み取り postMessage で通知
            # sandbox の allow-top-navigation がないため location.href が使えないための代替手段
            _sid = _lp_result.get("session_id", "")
            if _sid and "current_user" not in st.session_state:
                _user = get_session_user(_sid)
                if _user:
                    st.session_state["current_user"] = _user
                    st.session_state["session_id"]   = _sid
                    # ★ setdefault ではなく直接代入：
                    #    最初の描画で view="select_user" がすでにセットされているため
                    #    setdefault は何もせず chat に遷移できないバグを修正
                    st.session_state["view"] = "chat"
                    st.session_state["_show_rooms"] = True   # 復元後はルーム選択画面から再開
                    st.rerun()
                else:
                    # 復元失敗（セッション失効・無効/漏洩SID 等）→ localStorage を消して
                    # 古いSIDを送り続けないようにし、ログイン画面へ。
                    st.session_state["_clear_session"] = True
                    st.toast("再ログインしてください。", icon="⚠️")
                    st.rerun()
        elif _nav == "save_push_subscription":
            # JS からの Web Push 購読情報を DB に保存（rerun 不要）
            _sub_json = _lp_result.get("subscription", "")
            _sub_uid  = _lp_result.get("user_id", "") or _cu.get("id", "")
            if _sub_json and _sub_uid:
                save_push_subscription(_sub_uid, _sub_json)

# ── Render 1 フラッシュ防止 ──────────────────────────────────────────────
# _lp_result is None = コンポーネントがまだ stSetValue を送っていない（初回描画）。
# このタイミングで show_user_select() を呼ぶとログインフォームが一瞬表示される。
# JS は streamlit:render を受信したら必ず restore_session を送るので
# ここでは空画面を出してその到着を待つ。
# 招待リンクで register を表示する場合はスプラッシュを出さず即フォームを見せる
_waiting_for_js = (_lp_result is None and "current_user" not in st.session_state
                   and st.session_state.get("view") not in ("register",))
if _waiting_for_js:
    # セッション復元待ちの間、意図的なスプラッシュを全画面で表示する。
    # （旧実装は #1a1a2e のベタ塗り div が全画面を覆えず「黒地に紺の四角」が
    #   浮いて壊れて見えた。fixed:inset:0 で全画面・地色に馴染ませ・ロゴをパルス）
    st.html(
        '<style>@keyframes danranSplash{'
        '0%,100%{opacity:0.45;transform:scale(0.96)}50%{opacity:1;transform:scale(1)}}</style>'
        '<div style="position:fixed;inset:0;z-index:2147483646;background:#1a1614;'
        'display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px">'
        '<div style="font-size:3.4rem;animation:danranSplash 1.4s ease-in-out infinite">🏠</div>'
        '<div style="color:rgba(255,255,255,0.45);font-size:0.9rem;font-weight:700;'
        'letter-spacing:0.12em">danran</div>'
        '</div>'
    )
else:
    st.session_state.setdefault(
        "view",
        "chat" if "current_user" in st.session_state else "select_user",
    )

    match st.session_state["view"]:
        case "chat" if "current_user" in st.session_state:
            show_chat(st.session_state["current_user"])
        case "profile" if "current_user" in st.session_state:
            show_profile(st.session_state["current_user"])
        case "room_edit" if "current_user" in st.session_state:
            show_room_edit(st.session_state.get("editing_room", {}))
        case "room_create" if "current_user" in st.session_state:
            show_room_create()
        case "notifications" if "current_user" in st.session_state:
            show_notifications(st.session_state["current_user"])
        case "enter_password":
            show_enter_password()
        case "register":
            show_register()
        case _:
            show_user_select()
