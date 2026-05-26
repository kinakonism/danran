"""
danran - 家族専用チャットアプリ
Streamlit × Supabase
セッション管理: Supabase sessions テーブル + URL クエリパラム (?s=SESSION_ID)
"""

import uuid
from datetime import datetime

import bcrypt
import streamlit as st
from supabase import create_client, Client

# ──────────────────────────────────────────────
# ページ設定
# ──────────────────────────────────────────────
st.set_page_config(
    page_title="danran 🏠",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────
ROOMS: list[str] = [
    "家族みんな",
    "連絡事項",
    "おでかけ計画",
    "料理・レシピ",
]

AVATAR_BUCKET = "avatars"
SESSION_PARAM = "s"   # URL クエリパラムのキー

# ──────────────────────────────────────────────
# Supabase クライアント
# ──────────────────────────────────────────────
@st.cache_resource
def get_supabase() -> Client:
    return create_client(
        st.secrets["supabase"]["url"],
        st.secrets["supabase"]["anon_key"],
    )

supabase = get_supabase()

# ──────────────────────────────────────────────
# パスワードヘルパー
# ──────────────────────────────────────────────
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False

# ──────────────────────────────────────────────
# セッション管理
# ──────────────────────────────────────────────
def create_session(user_id: str) -> str:
    """Supabase に session を INSERT してセッション ID を返す。"""
    res = supabase.table("sessions").insert({"user_id": user_id}).execute()
    return res.data[0]["id"]

def get_session_user(session_id: str) -> dict | None:
    """セッション ID → ユーザー情報（無効なら None）。"""
    try:
        sess = (
            supabase.table("sessions")
            .select("user_id")
            .eq("id", session_id)
            .single()
            .execute()
            .data
        )
        if not sess:
            return None
        return (
            supabase.table("users")
            .select("id, name, avatar")
            .eq("id", sess["user_id"])
            .single()
            .execute()
            .data
        )
    except Exception:
        return None

def delete_session(session_id: str) -> None:
    try:
        supabase.table("sessions").delete().eq("id", session_id).execute()
    except Exception:
        pass

def do_login(user: dict) -> None:
    """ログイン: session 作成 → URL パラム書き込み → session_state 更新。"""
    sid = create_session(user["id"])
    st.query_params[SESSION_PARAM]   = sid
    st.session_state["session_id"]   = sid
    st.session_state["current_user"] = {k: user[k] for k in ("id", "name", "avatar")}
    st.session_state["view"]         = "chat"

def do_logout() -> None:
    """ログアウト: session 削除 → URL パラム・session_state をクリア。"""
    delete_session(st.session_state.pop("session_id", "") or "")
    st.session_state.pop("current_user", None)
    st.session_state["view"] = "select_user"
    st.query_params.clear()

# ──────────────────────────────────────────────
# ストレージ（写真アバター）
# ──────────────────────────────────────────────
def upload_avatar_photo(user_id: str, uploaded_file) -> str:
    ext      = uploaded_file.name.rsplit(".", 1)[-1].lower()
    filename = f"{user_id}.{ext}"
    supabase.storage.from_(AVATAR_BUCKET).upload(
        path=filename,
        file=uploaded_file.read(),
        file_options={"content-type": uploaded_file.type, "upsert": "true"},
    )
    return supabase.storage.from_(AVATAR_BUCKET).get_public_url(filename)

# ──────────────────────────────────────────────
# ユーザー DB
# ──────────────────────────────────────────────
def fetch_all_users() -> list[dict]:
    try:
        return (
            supabase.table("users")
            .select("id, name, avatar")
            .order("created_at")
            .execute()
            .data or []
        )
    except Exception:
        return []

def get_user_with_hash(user_id: str) -> dict | None:
    try:
        return (
            supabase.table("users")
            .select("id, name, avatar, password_hash")
            .eq("id", user_id)
            .single()
            .execute()
            .data
        )
    except Exception:
        return None

def register_user(
    name: str, avatar: str, password: str, user_id: str | None = None
) -> dict:
    uid = user_id or str(uuid.uuid4())
    return (
        supabase.table("users")
        .insert({
            "id":            uid,
            "name":          name,
            "avatar":        avatar,
            "password_hash": hash_password(password),
        })
        .execute()
        .data[0]
    )

# ──────────────────────────────────────────────
# メッセージ DB
# ──────────────────────────────────────────────
def fetch_messages(room_name: str, limit: int = 100) -> list[dict]:
    try:
        return (
            supabase.table("messages")
            .select("id, user_name, user_avatar, content, created_at")
            .eq("room_name", room_name)
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
            .data or []
        )
    except Exception as e:
        st.error(f"❌ {e}")
        return []

def send_message(
    room_name: str, user_name: str, user_avatar: str, content: str
) -> bool:
    try:
        supabase.table("messages").insert({
            "room_name":   room_name,
            "user_name":   user_name,
            "user_avatar": user_avatar,
            "content":     content,
        }).execute()
        return True
    except Exception as e:
        st.error(f"❌ {e}")
        return False

def format_timestamp(ts_str: str) -> str:
    try:
        return datetime.fromisoformat(
            ts_str.replace("Z", "+00:00")
        ).strftime("%-m/%-d %H:%M")
    except Exception:
        return ts_str

# ──────────────────────────────────────────────
# 画面① ユーザー選択（ロック画面）
# ──────────────────────────────────────────────
def show_user_select() -> None:
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("<br>" * 2, unsafe_allow_html=True)
        st.markdown("## 🏠 danran")
        st.markdown("あなたを選んでください")
        st.divider()

        users = fetch_all_users()
        if users:
            cols = st.columns(min(len(users), 4))
            for i, user in enumerate(users):
                with cols[i % 4]:
                    is_photo = user["avatar"].startswith("http")
                    if is_photo:
                        st.image(user["avatar"], width=64)
                    label = user["name"] if is_photo else f"{user['avatar']}  {user['name']}"
                    if st.button(label, key=f"sel_{user['id']}", use_container_width=True):
                        st.session_state["view"]          = "enter_password"
                        st.session_state["selected_user"] = user
                        st.rerun()
        else:
            st.info("まだメンバーが登録されていません。")

        st.divider()
        if st.button("＋ 新しいメンバーとして登録", use_container_width=True):
            st.session_state["view"] = "register"
            st.rerun()

# ──────────────────────────────────────────────
# 画面② パスワード入力
# ──────────────────────────────────────────────
def show_enter_password() -> None:
    user = st.session_state.get("selected_user", {})
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("<br>" * 2, unsafe_allow_html=True)
        avatar = user.get("avatar", "🙂")
        if avatar.startswith("http"):
            st.image(avatar, width=80)
            st.markdown(f"### {user.get('name', '')}")
        else:
            st.markdown(f"## {avatar} {user.get('name', '')}")
        st.markdown("パスワードを入力してください")
        st.divider()

        password = st.text_input("パスワード", type="password", key="pw_input")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("← 戻る", use_container_width=True):
                st.session_state["view"] = "select_user"
                st.session_state.pop("selected_user", None)
                st.rerun()
        with col2:
            if st.button("🔓 ログイン", use_container_width=True, type="primary"):
                if not password:
                    st.error("パスワードを入力してください")
                    return
                u = get_user_with_hash(user["id"])
                if u and verify_password(password, u.get("password_hash") or ""):
                    st.session_state.pop("selected_user", None)
                    do_login(u)
                    st.rerun()
                else:
                    st.error("パスワードが違います 🔒")

# ──────────────────────────────────────────────
# 画面③ 新規メンバー登録
# ──────────────────────────────────────────────
def show_register() -> None:
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("<br>" * 2, unsafe_allow_html=True)
        st.markdown("## 👋 新しいメンバー登録")
        st.divider()

        name = st.text_input("お名前", placeholder="例：パパ、ママ、はなこ…", max_chars=20)

        st.markdown("**アバター**")
        avatar_type = st.radio(
            "アバタータイプ", ["絵文字", "写真"],
            horizontal=True, label_visibility="collapsed",
        )
        avatar_emoji = "🙂"
        avatar_photo = None

        if avatar_type == "絵文字":
            st.caption("スマホのキーボードから好きな絵文字を選んでね 😊")
            avatar_emoji = st.text_input(
                "絵文字", value="🙂", max_chars=8, label_visibility="collapsed"
            ) or "🙂"
        else:
            avatar_photo = st.file_uploader(
                "写真を選ぶ", type=["jpg", "jpeg", "png", "webp"],
                label_visibility="collapsed",
            )
            if avatar_photo:
                st.image(avatar_photo, width=80)

        st.markdown("**パスワード**")
        password = st.text_input(
            "パスワード", type="password",
            placeholder="4文字以上", label_visibility="collapsed",
        )
        password_confirm = st.text_input(
            "パスワード（確認用）", type="password", placeholder="もう一度入力"
        )

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            if st.button("← 戻る", use_container_width=True):
                st.session_state["view"] = "select_user"
                st.rerun()
        with col2:
            if st.button("✅ 登録する", use_container_width=True, type="primary"):
                if not name.strip():
                    st.error("お名前を入力してください"); return
                if avatar_type == "写真" and not avatar_photo:
                    st.error("写真を選択してください"); return
                if len(password) < 4:
                    st.error("パスワードは4文字以上にしてください"); return
                if password != password_confirm:
                    st.error("パスワードが一致しません"); return

                new_uid = str(uuid.uuid4())
                if avatar_type == "写真":
                    with st.spinner("写真をアップロード中…"):
                        final_avatar = upload_avatar_photo(new_uid, avatar_photo)
                else:
                    final_avatar = avatar_emoji

                with st.spinner("登録中…"):
                    user = register_user(name.strip(), final_avatar, password, user_id=new_uid)

                do_login(user)
                st.rerun()

# ──────────────────────────────────────────────
# 画面④ メインチャット（LINE風）
# ──────────────────────────────────────────────
def show_chat(current_user: dict) -> None:

    # ── サイドバー ──
    with st.sidebar:
        st.markdown("## 🏠 danran")
        av = current_user["avatar"]
        if av.startswith("http"):
            st.image(av, width=48)
            st.markdown(f"**{current_user['name']}** としてログイン中")
        else:
            st.markdown(f"**{av} {current_user['name']}** としてログイン中")

        st.divider()
        st.markdown("### 💬 チャットルーム")
        selected_room: str = st.radio("ルーム", ROOMS, label_visibility="collapsed")

        st.divider()
        if st.button("🔒 ログアウト", use_container_width=True):
            do_logout()
            st.rerun()

        st.markdown("---")
        st.caption("© danran family")

    # ── ヘッダー ──
    col_title, col_reload = st.columns([5, 1])
    with col_title:
        st.markdown(f"## 💬 {selected_room}")
    with col_reload:
        if st.button("🔄", help="最新のメッセージを取得"):
            st.rerun()

    # ── タイムライン（LINE風） ──
    messages = fetch_messages(selected_room)

    if not messages:
        st.info("📭 まだメッセージはありません。最初のメッセージを送ってみましょう！")
    else:
        for msg in messages:
            sender: str = msg.get("user_name",  "不明")
            body:   str = msg.get("content",    "")
            ts:     str = msg.get("created_at", "")
            avatar: str = msg.get("user_avatar","🙂")
            is_mine     = sender == current_user["name"]

            with st.chat_message(
                name="user" if is_mine else sender,
                avatar=avatar,
            ):
                # 他人のメッセージにのみ名前をアイコン直下に表示（LINE風）
                if not is_mine:
                    st.markdown(
                        f'<p style="font-size:0.75rem;color:#9a9a9a;'
                        f'font-weight:600;margin:0 0 4px 0">{sender}</p>',
                        unsafe_allow_html=True,
                    )
                st.markdown(body)
                if ts:
                    st.caption(format_timestamp(ts))

    # ── 送信フォーム ──
    av = current_user["avatar"]
    ph = (
        f"{av} {current_user['name']} としてメッセージを送る…"
        if not av.startswith("http")
        else f"{current_user['name']} としてメッセージを送る…"
    )
    if prompt := st.chat_input(ph):
        if send_message(selected_room, current_user["name"], current_user["avatar"], prompt):
            st.rerun()

# ──────────────────────────────────────────────
# エントリーポイント
# ──────────────────────────────────────────────
# URL クエリパラム ?s=SESSION_ID からセッション復元
# → ページリロード・スワイプリロードでも URL に ?s= が残るので消えない
if "current_user" not in st.session_state:
    sid = st.query_params.get(SESSION_PARAM)
    if sid:
        user = get_session_user(sid)
        if user:
            st.session_state["current_user"] = user
            st.session_state["session_id"]   = sid
            st.session_state.setdefault("view", "chat")

st.session_state.setdefault(
    "view",
    "chat" if "current_user" in st.session_state else "select_user",
)

match st.session_state["view"]:
    case "chat" if "current_user" in st.session_state:
        show_chat(st.session_state["current_user"])
    case "enter_password":
        show_enter_password()
    case "register":
        show_register()
    case _:
        show_user_select()
