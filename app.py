"""
danran - 家族専用チャットアプリ  Streamlit × Supabase
セッション: Supabase sessions + URL ?s=SESSION_ID
リアルタイム: @st.fragment(run_every="2s")
通知: PWA Web Push (run.py 経由で /sw.js を配信)
"""

import html as _html
import io
import json
import os
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
/* embed モードが chat input を隠す場合の保険 */
[data-testid="stBottom"]           { display: block !important; visibility: visible !important; }
[data-testid="stChatInput"]        { display: flex !important; visibility: visible !important; }
/* コンテンツの余白調整 */
[data-testid="stMainBlockContainer"] > div:first-child { padding-top: 0.5rem; }
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
    return create_client(
        st.secrets["supabase"]["url"],
        st.secrets["supabase"]["anon_key"],
    )

supabase = get_supabase()

# ─────────────────────────────────────
# ルーム DB（DB に rooms テーブルがある場合は取得、なければフォールバック）
# ─────────────────────────────────────
@st.cache_data(ttl=60)
def fetch_rooms() -> list[dict]:
    """DB からルーム一覧 (id, name, icon) を取得。失敗時はハードコードで補完。"""
    try:
        data = supabase.table("rooms").select("id, name, icon").order("created_at").execute().data or []
        if data:
            return data
    except Exception:
        pass
    return [{"id": r, "name": r, "icon": "💬"} for r in ROOMS_FALLBACK]

def invalidate_rooms_cache() -> None:
    """rooms キャッシュを破棄（更新・削除後に呼ぶ）。"""
    fetch_rooms.clear()

# ─────────────────────────────────────
# Web Push 通知
# ─────────────────────────────────────
def _vapid_cfg() -> dict:
    """secrets から VAPID 設定を返す。未設定なら空 dict。"""
    try:
        return dict(st.secrets.get("push", {}))
    except Exception:
        return {}

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
              content: str, has_image: bool = False) -> None:
    """送信者以外の全購読者に Web Push 通知を送る。"""
    try:
        cfg = _vapid_cfg()
        priv = cfg.get("vapid_private_key", "")
        subj = cfg.get("vapid_subject", "")
        if not (priv and subj):
            return

        from pywebpush import webpush, WebPushException

        body = f"{sender_name}: {'📷 写真' if has_image and not content else content[:80]}"
        payload = json.dumps({
            "title": f"danran 🏠 {room}",
            "body":  body,
            "room":  room,
            "url":   "/",
        })

        # 送信者以外の購読情報を取得
        rows = supabase.table("push_subscriptions")\
            .select("endpoint, p256dh, auth")\
            .neq("user_id", sender_uid)\
            .execute().data or []

        expired: list[str] = []
        for row in rows:
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
    st.query_params[SESSION_PARAM]   = sid
    st.session_state["session_id"]   = sid
    st.session_state["current_user"] = {k: user.get(k, "") for k in ("id", "name", "avatar", "phone")}
    st.session_state["view"]         = "chat"

def do_logout() -> None:
    delete_session(st.session_state.pop("session_id", "") or "")
    st.session_state.pop("current_user", None)
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

def delete_room(room_id: str, room_name: str) -> None:
    """ルームと、そのメッセージ・リアクション・既読情報をすべて削除。"""
    try:
        # リアクションを先に削除（FK cascade が未設定の場合の保険）
        msgs = supabase.table("messages").select("id").eq("room_name", room_name).execute().data or []
        msg_ids = [m["id"] for m in msgs]
        if msg_ids:
            supabase.table("reactions").delete().in_("message_id", msg_ids).execute()
        supabase.table("last_read").delete().eq("room_name", room_name).execute()
        supabase.table("messages").delete().eq("room_name", room_name).execute()
        supabase.table("rooms").delete().eq("id", room_id).execute()
        invalidate_rooms_cache()
    except Exception as e:
        raise RuntimeError(str(e))

# ─────────────────────────────────────
# メッセージ DB
# ─────────────────────────────────────
def fetch_messages(room: str, limit: int = 100) -> list[dict]:
    try:
        return supabase.table("messages")\
            .select("id, user_id, user_name, user_avatar, content, image_url, created_at")\
            .eq("room_name", room).order("created_at").limit(limit).execute().data or []
    except Exception as e:
        st.error(f"❌ {e}"); return []

def send_message(room: str, uid: str, uname: str, uavatar: str, content: str, image_url: str | None = None) -> bool:
    try:
        supabase.table("messages").insert({
            "room_name": room, "user_id": uid, "user_name": uname,
            "user_avatar": uavatar, "content": content, "image_url": image_url,
        }).execute()
        send_push(room, uid, uname, content, has_image=bool(image_url))
        return True
    except Exception as e:
        st.error(f"❌ {e}"); return False

def delete_message(msg_id: str, uname: str) -> bool:
    try:
        supabase.table("messages").delete().eq("id", msg_id).eq("user_name", uname).execute()
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
        room_names = [r["name"] for r in fetch_rooms()]
    try:
        last_reads = {
            r["room_name"]: r["read_at"]
            for r in supabase.table("last_read").select("room_name, read_at").eq("user_id", user_id).execute().data or []
        }
        counts = {}
        for rname in room_names:
            lr = last_reads.get(rname)
            q  = supabase.table("messages").select("id", count="exact").eq("room_name", rname)
            if lr:
                q = q.gt("created_at", lr)
            counts[rname] = q.execute().count or 0
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
    "danran_lp_v13",   # 名前変更 → ブラウザキャッシュ強制破棄
    path=_LP_COMPONENT_DIR,
)

# ─────────────────────────────────────
# ★ リアルタイムタイムライン（フラグメント）
#   5秒ごとに自動更新。ページ全体は再描画しない。
# ─────────────────────────────────────
@st.fragment(run_every="2s")
def render_messages() -> None:
    current_user  = st.session_state.get("current_user", {})
    selected_room = st.session_state.get("active_room", "")
    uname         = current_user.get("name", "")
    my_id         = current_user.get("id", "")

    messages = fetch_messages(selected_room)

    # 新着トースト（他人のメッセージのみ）
    count_key = f"cnt_{selected_room}"
    prev      = st.session_state.get(count_key, -1)
    if prev >= 0 and len(messages) > prev:
        for m in messages[prev:]:
            if m["user_name"] != uname:
                preview = m["content"][:30] if m["content"] else "📷 写真"
                st.toast(f"💬 {m['user_name']}: {preview}", icon="🔔")
    st.session_state[count_key] = len(messages)

    # 既読マーク
    if uid := current_user.get("id"):
        mark_as_read(uid, selected_room)

    if not messages:
        st.info("📭 まだメッセージはありません。最初のメッセージを送ってみましょう！")
        return

    # リアクション一括取得
    all_reactions = fetch_reactions_bulk([m["id"] for m in messages])

    for msg in messages:
        msg_id   = msg.get("id",          "")
        sender   = msg.get("user_name",  "不明")
        msg_uid  = msg.get("user_id",    "")
        body     = msg.get("content",    "") or ""
        ts       = msg.get("created_at", "")
        avatar   = msg.get("user_avatar","🙂")
        img_url  = msg.get("image_url")
        # user_id があればIDで判定（名前変更後も正しく動く）、なければ名前フォールバック
        is_mine  = (msg_uid == my_id) if (msg_uid and my_id) else (sender == uname)

        msg_reactions = all_reactions.get(msg_id, {})

        # ── アバター HTML ──
        av_html = (
            f'<img src="{avatar}" style="width:40px;height:40px;border-radius:8px;'
            f'object-fit:cover;flex-shrink:0;display:block">'
            if avatar.startswith("http")
            else f'<span style="font-size:1.8rem;line-height:40px;display:block;'
                 f'width:40px;text-align:center;flex-shrink:0">{avatar}</span>'
        )

        # ── 本文・画像 HTML ──
        body_esc  = (body.replace("&", "&amp;").replace("<", "&lt;")
                        .replace(">", "&gt;").replace("\n", "<br>"))
        img_piece = (
            f'<img src="{img_url}" style="max-width:200px;border-radius:10px;'
            f'display:block;{"margin-bottom:6px" if body else ""}">'
        ) if img_url else ""
        content = img_piece + body_esc

        # ── リアクション pills ──
        pills = ""
        for emoji in REACTION_EMOJIS:
            users = msg_reactions.get(emoji, [])
            if users:
                my  = uname in users
                bg  = "rgba(0,185,0,0.3)"  if my else "rgba(255,255,255,0.08)"
                bdr = "rgba(0,185,0,0.85)" if my else "rgba(255,255,255,0.2)"
                pills += (
                    f'<span style="display:inline-flex;align-items:center;gap:2px;'
                    f'background:{bg};border:1px solid {bdr};border-radius:20px;'
                    f'padding:1px 7px;font-size:0.8rem;margin-right:3px">'
                    f'{emoji}&nbsp;{len(users)}</span>'
                )
        # data-lp-react: JS がリアルタイムで書き換えるためのコンテナ（常に出力）
        pills_row = (
            f'<div data-lp-react="{msg_id}" style="margin-top:4px;text-align:{"right" if is_mine else "left"};'
            f'line-height:2;min-height:0">{pills}</div>'
        )

        # ── LINE 風バブル HTML（自分＝右、他人＝左） ──
        # data-lp-mine="1"   → JS が「自分のメッセージ」と判別して削除ボタン表示
        # data-lp-my-avatar  → JS が「自分のアバター」と判別してタップでプロフィール遷移
        if is_mine:
            mine_av_html = (
                f'<img data-lp-my-avatar="1" src="{avatar}" '
                f'style="width:40px;height:40px;border-radius:8px;'
                f'object-fit:cover;flex-shrink:0;display:block;cursor:pointer">'
                if avatar.startswith("http")
                else f'<span data-lp-my-avatar="1" '
                     f'style="font-size:1.8rem;line-height:40px;display:block;'
                     f'width:40px;text-align:center;flex-shrink:0;cursor:pointer">{avatar}</span>'
            )
            bubble = (
                f'<div data-lp-msg="{msg_id}" data-lp-mine="1" style="'
                f'display:flex;justify-content:flex-end;align-items:flex-end;'
                f'gap:8px;margin:4px 0 2px 48px">'
                f'<div style="text-align:right">'
                f'<div style="font-size:0.7rem;color:#888;margin-bottom:3px">{fmt_ts(ts)}</div>'
                f'<div style="background:#00b900;color:#fff;'
                f'border-radius:18px 18px 4px 18px;padding:10px 14px;'
                f'display:inline-block;max-width:100%;text-align:left;'
                f'word-break:break-word;font-size:0.93rem">{content}</div>'
                f'{pills_row}'
                f'</div>'
                f'{mine_av_html}'
                f'</div>'
            )
        else:
            bubble = (
                f'<div data-lp-msg="{msg_id}" style="'
                f'display:flex;align-items:flex-end;gap:8px;margin:4px 0 2px 0">'
                f'{av_html}'
                f'<div>'
                f'<div style="font-size:0.75rem;color:#9a9a9a;font-weight:600;'
                f'margin-bottom:3px">{sender}</div>'
                f'<div style="background:#2c2c2e;color:#fff;'
                f'border-radius:18px 18px 18px 4px;padding:10px 14px;'
                f'display:inline-block;max-width:100%;text-align:left;'
                f'word-break:break-word;font-size:0.93rem">{content}</div>'
                f'<div style="font-size:0.7rem;color:#888;margin-top:3px">{fmt_ts(ts)}</div>'
                f'{pills_row}'
                f'</div>'
                f'</div>'
            )

        st.markdown(bubble, unsafe_allow_html=True)

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
                do_login(u); st.rerun()
            else:
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
    """secrets.toml の [app] register_key を返す。未設定なら空文字（制限なし）。"""
    try:
        return st.secrets.get("app", {}).get("register_key", "")
    except Exception:
        return ""

def show_register() -> None:
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("<br>" * 2, unsafe_allow_html=True)
        st.markdown("## 👋 新しいメンバー登録")
        st.divider()

        # ── 招待コード認証（secrets に register_key が設定されている場合のみ） ──
        req_key = _get_register_key()
        if req_key:
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
                    _time.sleep(0.8)
                    st.session_state["view"] = "chat"
                    st.query_params["sr"] = "1"
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 保存に失敗しました: {e}")
        with c2:
            if st.button("← 戻る", use_container_width=True, key="profile_back"):
                st.session_state["view"] = "chat"
                st.query_params["sr"] = "1"
                st.rerun()

# ─────────────────────────────────────
# 画面⑤ ルーム編集
# ─────────────────────────────────────
_ROOM_EDIT_WIDGET_KEYS = ("room_edit_atype", "room_edit_emoji", "room_edit_photo", "room_edit_name")

def _reset_room_edit_widgets() -> None:
    """ルーム編集画面を開くたびにウィジェット状態をリセットする。"""
    for k in _ROOM_EDIT_WIDGET_KEYS:
        st.session_state.pop(k, None)
    # 削除確認フラグもクリア
    for k in list(st.session_state.keys()):
        if k.startswith("room_delete_confirm_"):
            st.session_state.pop(k, None)

def show_room_edit(room: dict) -> None:
    import time as _time

    if not room or not room.get("id"):
        st.session_state["view"] = "chat"
        st.query_params["sr"] = "1"
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
                    _time.sleep(0.8)
                    _reset_room_edit_widgets()
                    st.session_state["view"] = "chat"
                    st.query_params["sr"] = "1"
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 保存に失敗しました: {e}")
        with c2:
            if st.button("← 戻る", use_container_width=True, key="room_edit_back"):
                _reset_room_edit_widgets()
                st.session_state["view"] = "chat"
                st.query_params["sr"] = "1"
                st.rerun()

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
                            remaining = fetch_rooms()
                            st.session_state["active_room"] = remaining[0]["name"] if remaining else ""
                        st.session_state.pop(delete_confirm_key, None)
                        _reset_room_edit_widgets()
                        st.session_state["view"] = "chat"
                        st.query_params["sr"] = "1"
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ 削除に失敗しました: {e}")
        else:
            if st.button("🗑️ このルームを削除する", use_container_width=True,
                         key="room_delete_start"):
                st.session_state[delete_confirm_key] = True
                st.rerun()

# ─────────────────────────────────────
# 画面⑥ メインチャット
# ─────────────────────────────────────
def show_chat(current_user: dict) -> None:

    # ── ルーム状態 ──
    # ヘッダー（＜/✕ + ルーム名）は JS が fixed 位置に注入する
    # show_rooms は URL パラメータ ?sr=1 を正として参照する
    _all_rooms      = fetch_rooms()
    _all_room_names = [r["name"] for r in _all_rooms]
    _default_room   = _all_room_names[0] if _all_room_names else ""
    # active_room 初期化 or 削除済みルームのフォールバック
    if "active_room" not in st.session_state or st.session_state["active_room"] not in _all_room_names:
        st.session_state["active_room"] = _default_room
    selected_room = st.session_state["active_room"]
    show_rooms    = st.query_params.get("sr") == "1"

    # ── ＜ を押したときのルーム選択パネル ──
    if show_rooms:
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

        # ═══ Section 1: チャットルーム ═══
        rows: list[str] = [
            '<div id="_danran_room_list" style="padding-bottom:20px">',
        ]

        if _show_push_banner:
            rows.append(
                '<div id="_danran_push_banner" style="'
                'display:flex;align-items:center;gap:10px;'
                'background:rgba(52,199,89,0.1);'
                'border:1px solid rgba(52,199,89,0.28);'
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
            f'<span style="{_first_sec_label}">チャットルーム</span>',
            # グループカード（iOS の grouped list 風）
            '<div style="background:rgba(255,255,255,0.06);'
            'border:1px solid rgba(255,255,255,0.1);'
            'border-radius:14px;overflow:hidden">',
        ])

        for i, room in enumerate(_all_rooms):
            rname   = room["name"]
            ricon   = room.get("icon", "💬")
            room_id = room["id"]
            count   = unread.get(rname, 0)
            is_last = (i == len(_all_rooms) - 1)
            is_active = (rname == selected_room)

            badge = (
                f'<span style="background:#e03438;color:#fff;border-radius:20px;'
                f'padding:1px 8px;font-size:0.68rem;font-weight:700;flex-shrink:0">'
                f'+{count}</span>'
            ) if count > 0 else ""

            # アクティブインジケーター（緑の点）
            active_dot = (
                '<span style="width:7px;height:7px;border-radius:50%;'
                'background:#34c759;flex-shrink:0"></span>'
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
                f'data-room-name="{_html.escape(rname)}" '
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

        # ═══ Section 2: アカウント ═══
        av = current_user["avatar"]
        uname = current_user["name"]

        if av.startswith("http"):
            avatar_html = (
                f'<img src="{_html.escape(av)}" '
                f'style="width:48px;height:48px;border-radius:50%;'
                f'object-fit:cover;flex-shrink:0">'
            )
        else:
            avatar_html = (
                f'<div style="width:48px;height:48px;border-radius:50%;'
                f'background:rgba(255,255,255,0.12);'
                f'display:flex;align-items:center;justify-content:center;'
                f'font-size:1.8rem;flex-shrink:0">{av}</div>'
            )

        rows.extend([
            f'<span style="{_sec_label}">アカウント</span>',
            # プロフィールカード（タップでプロフィール編集へ）
            '<div style="background:rgba(255,255,255,0.06);'
            'border:1px solid rgba(255,255,255,0.1);'
            'border-radius:14px;overflow:hidden;margin-bottom:10px">',
            # タップ可能な行
            '<button data-profile-nav="true" '
            'style="width:100%;display:flex;align-items:center;gap:13px;'
            'padding:14px 16px;background:none;border:none;cursor:pointer;'
            '-webkit-tap-highlight-color:transparent;text-align:left">',
            avatar_html,
            '<div style="flex:1;min-width:0">',
            f'<div style="font-size:1rem;font-weight:600;color:#fff;'
            f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
            f'{_html.escape(uname)}</div>',
            '<div style="font-size:0.76rem;color:rgba(255,255,255,0.4);margin-top:3px">'
            'プロフィールを編集</div>',
            '</div>',   # info
            # 右矢印
            '<span style="color:rgba(255,255,255,0.25);font-size:1.1rem;flex-shrink:0">›</span>',
            '</button>',  # タップ行
            '</div>',   # カード
            '</div>',   # _danran_room_list
        ])

        st.markdown('\n'.join(rows), unsafe_allow_html=True)

        # ── ログアウトボタン（Streamlit が担当） ──
        if st.button("🔒 ログアウト", use_container_width=True, key="room_logout"):
            do_logout(); st.rerun()

        return   # ルーム選択中はメッセージ非表示

    # ★ リアルタイム更新フラグメント（5秒ごと）
    render_messages()

    # ── テキスト入力 ──
    av_str2 = current_user["avatar"]
    # プレースホルダーは短く固定（名前を入れると折り返して最新メッセージが隠れるため）
    ph = "メッセージ" if av_str2.startswith("http") else f"{av_str2} メッセージ"
    if prompt := st.chat_input(ph):
        send_message(selected_room, current_user["id"], current_user["name"], current_user["avatar"], prompt)
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
        st.secrets.get("app", {}).get("url", "")
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
    ログイン不要・セッション不問でアクセス可能。"""
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
        '<div style="font-size:0.85rem;color:rgba(255,255,255,0.5);margin-top:4px">家族チャット</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("### 📲 ホーム画面に追加する方法")
    st.markdown("""
1. 下の **「プロファイルをダウンロード」** ボタンをタップ
2. 「**設定**」アプリを開く
3. 一番上に「**プロファイルがダウンロードされました**」→ タップ
4. 「**インストール**」→「**インストール**」
5. ホーム画面に **danran** のアイコンが追加されます 🎉
""")

    mobileconfig_bytes = _build_mobileconfig()
    st.download_button(
        label="📥 プロファイルをダウンロード",
        data=mobileconfig_bytes,
        file_name="danran.mobileconfig",
        mime="application/x-apple-aspen-config",
        use_container_width=True,
        type="primary",
    )

    st.markdown(
        '<div style="font-size:0.75rem;color:rgba(255,255,255,0.3);text-align:center;'
        'margin-top:16px">iOS 16 以上 / Safari 対応</div>',
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

# ─────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────

# embed=true を URL から除去（Streamlit embed モード = chat input 非表示を防ぐ）
# JS コンポーネントから location.replace() は sandbox に阻まれるため Python 側で処理する
if "embed" in st.query_params:
    del st.query_params["embed"]
    st.rerun()

# ① URL パラメータからのセッション復元
#   JS コンポーネントが localStorage→?s= にリダイレクトした場合も同じ経路で処理される
if "current_user" not in st.session_state:
    _url_sid = st.query_params.get(SESSION_PARAM)
    if _url_sid:
        _url_user = get_session_user(_url_sid)
        if _url_user:
            st.session_state["current_user"] = _url_user
            st.session_state["session_id"]   = _url_sid
            st.session_state.setdefault("view", "chat")

# ── DOM config 要素（JS コンポーネントが直接読む設定ストア）──
# render イベントのタイミング問題を回避するため、
# Python が HTML data 属性として埋め込み JS が window.parent.document から参照。
_cu          = st.session_state.get("current_user", {})
_clear_flag  = st.session_state.pop("_clear_session", False)
# プロフィール・ルーム編集画面中は JS カメラボタンを非表示にするため active_room を空にする
_is_profile  = st.session_state.get("view") in ("profile", "room_edit", "notifications")
if "current_user" in st.session_state and not _is_profile:
    # active_room が未セット（セッション復元直後）のときは DB の先頭ルームをフォールバック
    _rooms_for_hdr = fetch_rooms()
    _active_room   = st.session_state.get("active_room") or (
        _rooms_for_hdr[0]["name"] if _rooms_for_hdr else ""
    )
else:
    _active_room = ""
_show_rooms  = st.query_params.get("sr") == "1"
_cur_view    = st.session_state.get("view", "")
_vapid_pub = _vapid_cfg().get("vapid_public_key", "")

# ── ヘッダーを Python 側でレンダリング ──
# JS 注入ではなく Python が st.html() で直接 DOM に書くことでタイミング問題を解消。
# クリックハンドラだけは JS コンポーネント(attachHdrButtons)が付与する。
_HDR_DIV_STYLE = (
    'position:fixed;top:0;left:0;right:0;height:52px;z-index:2147483647;'
    'background:rgba(28,28,30,0.97);border-bottom:1px solid rgba(255,255,255,0.1);'
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
    elif _active_room:
        _hdr_btn_text  = "✕" if _show_rooms else "＜"
        _hdr_title_text = "ルーム選択" if _show_rooms else _active_room
        _hdr_html = (
            f'<div id="_danran_hdr" style="{_HDR_DIV_STYLE}">'
            f'<button data-hdr-nav style="{_HDR_BTN_STYLE}">{_html.escape(_hdr_btn_text)}</button>'
            f'<div style="{_HDR_TITLE_STYLE}">{_html.escape(_hdr_title_text)}</div>'
            f'<div style="flex-shrink:0;min-width:44px;"></div>'
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
    f'data-sb-url="{_html.escape(st.secrets["supabase"]["url"])}" '
    f'data-sb-key="{_html.escape(st.secrets["supabase"]["anon_key"])}" '
    f'data-user="{_html.escape(_cu.get("name",""))}" '
    f'data-avatar="{_html.escape(_cu.get("avatar",""))}" '
    f'data-room="{_html.escape(_active_room)}" '
    f'data-sess="{_html.escape(st.session_state.get("session_id",""))}" '
    f'data-clear="{str(_clear_flag).lower()}" '
    f'data-show-rooms="{str(_show_rooms).lower()}" '
    f'data-view="{_html.escape(_cur_view)}" '
    f'data-vapid-pub="{_html.escape(_vapid_pub)}" '
    f'data-uid="{_html.escape(_cu.get("id",""))}">'
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
            st.query_params["sr"] = "1"
            st.rerun()
        elif _nav == "go_chat":
            if "sr" in st.query_params:
                del st.query_params["sr"]
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
            st.query_params["sr"] = "1"
            st.rerun()
        elif _nav == "go_notifications":
            st.session_state["view"] = "notifications"
            st.rerun()
        elif _nav == "go_room":
            # JS ルームリストのルーム名ボタンクリック → そのルームに遷移
            _room_name = _lp_result.get("room_name", "")
            if _room_name:
                st.session_state["active_room"] = _room_name
                if "sr" in st.query_params:
                    del st.query_params["sr"]
                st.rerun()
        elif _nav == "go_room_edit":
            # JS ルームリストの ⚙️ クリック → ルーム編集画面
            _room_id = _lp_result.get("room_id", "")
            if _room_id:
                _found = [r for r in fetch_rooms() if r["id"] == _room_id]
                if _found:
                    _reset_room_edit_widgets()
                    st.session_state["editing_room"] = _found[0]
                    st.session_state["view"] = "room_edit"
                    st.rerun()
        elif _nav == "save_push_subscription":
            # JS からの Web Push 購読情報を DB に保存（rerun 不要）
            _sub_json = _lp_result.get("subscription", "")
            _sub_uid  = _lp_result.get("user_id", "") or _cu.get("id", "")
            if _sub_json and _sub_uid:
                save_push_subscription(_sub_uid, _sub_json)

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
    case "notifications" if "current_user" in st.session_state:
        show_notifications(st.session_state["current_user"])
    case "enter_password":
        show_enter_password()
    case "register":
        show_register()
    case _:
        show_user_select()
