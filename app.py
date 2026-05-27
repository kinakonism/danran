"""
danran - 家族専用チャットアプリ  Streamlit × Supabase
セッション: Supabase sessions + URL ?s=SESSION_ID
リアルタイム: @st.fragment(run_every="2s")
通知: in-app toast + ntfy.sh push (secrets.ntfy.topic が必要)
"""

import html as _html
import os
import uuid
from datetime import datetime, timezone, timedelta

import bcrypt
import httpx
import streamlit as st
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
[data-testid="stToolbar"]          { display: none !important; }
[data-testid="stDecoration"]       { display: none !important; }
[data-testid="stSidebar"]          { display: none !important; }
[data-testid="collapsedControl"]   { display: none !important; }
#MainMenu                          { display: none !important; }
[data-testid="stDeployButton"]     { display: none !important; }
[class*="viewerBadge"]             { display: none !important; }
/* コンテンツの余白調整 */
[data-testid="stMainBlockContainer"] > div:first-child { padding-top: 0.5rem; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────
ROOMS: list[str]  = ["家族みんな", "連絡事項", "おでかけ計画", "料理・レシピ"]
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
# プッシュ通知（ntfy.sh）
# ─────────────────────────────────────
def send_push(room: str, sender: str, content: str, has_image: bool = False) -> None:
    """secrets に [ntfy] topic が設定されていれば ntfy.sh へ通知。"""
    try:
        topic = st.secrets.get("ntfy", {}).get("topic", "")
        if not topic:
            return
        body = f"{sender}: {'📷 写真' if has_image and not content else content[:80]}"
        httpx.post(
            f"https://ntfy.sh/{topic}",
            content=body.encode(),
            headers={
                "Title":    f"danran 💬 {room}",
                "Tags":     "speech_balloon",
                "Priority": "default",
            },
            timeout=3.0,
        )
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
        return supabase.table("users").select("id, name, avatar").eq("id", sess["user_id"]).single().execute().data
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
    st.session_state["current_user"] = {k: user[k] for k in ("id", "name", "avatar")}
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
def upload_photo(bucket: str, file_id: str, f) -> str:
    ext = f.name.rsplit(".", 1)[-1].lower()
    fn  = f"{file_id}.{ext}"
    supabase.storage.from_(bucket).upload(fn, f.read(), {"content-type": f.type, "upsert": "true"})
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

def register_user(name: str, avatar: str, pw: str, uid: str | None = None) -> dict:
    uid = uid or str(uuid.uuid4())
    return supabase.table("users").insert({
        "id": uid, "name": name, "avatar": avatar, "password_hash": hash_password(pw),
    }).execute().data[0]

# ─────────────────────────────────────
# メッセージ DB
# ─────────────────────────────────────
def fetch_messages(room: str, limit: int = 100) -> list[dict]:
    try:
        return supabase.table("messages")\
            .select("id, user_name, user_avatar, content, image_url, created_at")\
            .eq("room_name", room).order("created_at").limit(limit).execute().data or []
    except Exception as e:
        st.error(f"❌ {e}"); return []

def send_message(room: str, uname: str, uavatar: str, content: str, image_url: str | None = None) -> bool:
    try:
        supabase.table("messages").insert({
            "room_name": room, "user_name": uname, "user_avatar": uavatar,
            "content": content, "image_url": image_url,
        }).execute()
        send_push(room, uname, content, has_image=bool(image_url))
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
def get_unread_counts(user_id: str) -> dict[str, int]:
    try:
        last_reads = {
            r["room_name"]: r["read_at"]
            for r in supabase.table("last_read").select("room_name, read_at").eq("user_id", user_id).execute().data or []
        }
        counts = {}
        for room in ROOMS:
            lr = last_reads.get(room)
            q  = supabase.table("messages").select("id", count="exact").eq("room_name", room)
            if lr:
                q = q.gt("created_at", lr)
            counts[room] = q.execute().count or 0
        return counts
    except Exception:
        return {r: 0 for r in ROOMS}

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
    "danran_longpress",
    path=_LP_COMPONENT_DIR,
)

# ─────────────────────────────────────
# ★ リアルタイムタイムライン（フラグメント）
#   5秒ごとに自動更新。ページ全体は再描画しない。
# ─────────────────────────────────────
@st.fragment(run_every="2s")
def render_messages() -> None:
    current_user  = st.session_state.get("current_user", {})
    selected_room = st.session_state.get("active_room", ROOMS[0])
    uname         = current_user.get("name", "")

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
        msg_id  = msg.get("id",          "")
        sender  = msg.get("user_name",   "不明")
        body    = msg.get("content",     "") or ""
        ts      = msg.get("created_at",  "")
        avatar  = msg.get("user_avatar", "🙂")
        img_url = msg.get("image_url")
        is_mine = sender == uname

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
        # data-lp-mine="1" → JS が「自分のメッセージ」と判別して削除ボタン表示
        if is_mine:
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
                f'{av_html}'
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
# 画面① ユーザー選択
# ─────────────────────────────────────
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
                        st.session_state.update(view="enter_password", selected_user=user)
                        st.rerun()
        else:
            st.info("まだメンバーが登録されていません。")
        st.divider()
        if st.button("＋ 新しいメンバーとして登録", use_container_width=True):
            st.session_state["view"] = "register"; st.rerun()

# ─────────────────────────────────────
# 画面② パスワード入力
# ─────────────────────────────────────
def show_enter_password() -> None:
    user = st.session_state.get("selected_user", {})
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("<br>" * 2, unsafe_allow_html=True)
        av = user.get("avatar", "🙂")
        if av.startswith("http"):
            st.image(av, width=80); st.markdown(f"### {user.get('name','')}")
        else:
            st.markdown(f"## {av} {user.get('name','')}")
        st.markdown("パスワードを入力してください")
        st.divider()
        pw = st.text_input("パスワード", type="password", key="pw_input")
        if st.button("🔓 ログイン", use_container_width=True, type="primary"):
            if not pw:
                st.error("パスワードを入力してください"); return
            u = get_user_with_hash(user["id"])
            if u and verify_password(pw, u.get("password_hash") or ""):
                st.session_state.pop("selected_user", None); do_login(u); st.rerun()
            else:
                st.error("パスワードが違います 🔒")
        if st.button("← 戻る", use_container_width=True):
            st.session_state.update(view="select_user"); st.session_state.pop("selected_user", None); st.rerun()

# ─────────────────────────────────────
# 画面③ 新規登録
# ─────────────────────────────────────
def show_register() -> None:
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("<br>" * 2, unsafe_allow_html=True)
        st.markdown("## 👋 新しいメンバー登録")
        st.divider()
        name = st.text_input("お名前", placeholder="例：パパ、ママ、はなこ…", max_chars=20)
        st.markdown("**アバター**")
        atype = st.radio("", ["絵文字", "写真"], horizontal=True, label_visibility="collapsed")
        avatar_emoji = "🙂"; avatar_photo = None
        if atype == "絵文字":
            st.caption("スマホのキーボードから絵文字を選んでね 😊")
            avatar_emoji = st.text_input("", value="🙂", max_chars=8, label_visibility="collapsed") or "🙂"
        else:
            avatar_photo = st.file_uploader("", type=["jpg","jpeg","png","webp"], label_visibility="collapsed")
            if avatar_photo: st.image(avatar_photo, width=80)
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
                user = register_user(name.strip(), final_av, pw, uid=new_uid)
            do_login(user); st.rerun()
        if st.button("← 戻る", use_container_width=True):
            st.session_state["view"] = "select_user"; st.rerun()

# ─────────────────────────────────────
# 画面④ メインチャット
# ─────────────────────────────────────
def show_chat(current_user: dict) -> None:

    # ── サイドバー ──
    # ── ルーム状態 ──
    selected_room = st.session_state.setdefault("active_room", ROOMS[0])
    show_rooms    = st.session_state.get("show_rooms", False)

    # ── LINE 風ヘッダー：[ ＜ | ルーム名 ] ──
    # 📷 ボタンは JS コンポーネントがチャット入力欄の左側に固定表示するため不要
    c_back, c_title = st.columns([1, 8])
    with c_back:
        label = "✕" if show_rooms else "＜"
        if st.button(label, use_container_width=True):
            st.session_state["show_rooms"] = not show_rooms
            st.rerun()
    with c_title:
        av = current_user["avatar"]
        av_str = (f'<img src="{av}" style="width:22px;height:22px;border-radius:4px;vertical-align:middle;margin-right:6px">'
                  if av.startswith("http") else f'{av} ')
        st.markdown(
            f'<h3 style="margin:0;line-height:1.4">{av_str}{selected_room}</h3>',
            unsafe_allow_html=True,
        )

    # ── ＜ を押したときのルーム選択パネル ──
    if show_rooms:
        st.divider()
        unread = get_unread_counts(current_user["id"])
        for room in ROOMS:
            badge = f"  🔴 +{unread[room]}" if unread.get(room, 0) > 0 else ""
            btn_label = f"{'▶ ' if room == selected_room else '　'}{room}{badge}"
            if st.button(btn_label, key=f"rs_{room}", use_container_width=True):
                st.session_state["active_room"] = room
                st.session_state["show_rooms"]  = False
                st.rerun()
        st.divider()
        # ユーザー情報 + ログアウト
        av = current_user["avatar"]
        if av.startswith("http"):
            c1, c2 = st.columns([1, 5])
            with c1: st.image(av, width=36)
            with c2: st.markdown(f"**{current_user['name']}**")
        else:
            st.markdown(f"{av} **{current_user['name']}**")
        if st.button("🔒 ログアウト", use_container_width=True):
            do_logout(); st.rerun()
        st.divider()
        return   # ルーム選択中はメッセージ非表示

    # ★ リアルタイム更新フラグメント（5秒ごと）
    render_messages()

    # ── テキスト入力 ──
    av_str2 = current_user["avatar"]
    ph = (
        f"{av_str2} {current_user['name']} としてメッセージを送る…"
        if not av_str2.startswith("http")
        else f"{current_user['name']} としてメッセージを送る…"
    )
    if prompt := st.chat_input(ph):
        send_message(selected_room, current_user["name"], current_user["avatar"], prompt)
        st.rerun()

# ─────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────

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
_active_room = (st.session_state.get("active_room", ROOMS[0])
                if "current_user" in st.session_state else "")
st.html(
    f'<div id="_danran_cfg" style="position:absolute;width:0;height:0;overflow:hidden;pointer-events:none" '
    f'data-sb-url="{_html.escape(st.secrets["supabase"]["url"])}" '
    f'data-sb-key="{_html.escape(st.secrets["supabase"]["anon_key"])}" '
    f'data-user="{_html.escape(_cu.get("name",""))}" '
    f'data-avatar="{_html.escape(_cu.get("avatar",""))}" '
    f'data-room="{_html.escape(_active_room)}" '
    f'data-sess="{_html.escape(st.session_state.get("session_id",""))}" '
    f'data-clear="{str(_clear_flag).lower()}">'
    f'</div>'
)

# ── グローバルコンポーネント（常時実行・ゼロ高さ）──
# ★ setValue を一切送らないので Python rerun は発生しない
#   セッション / Supabase 認証情報 / カメラ設定は上の DOM 要素から JS が読む。
#   render イベントは使わず、MutationObserver + scan() で常に最新値を取得。
_lp_detector(
    save_session  = st.session_state.get("session_id", ""),
    clear_session = _clear_flag,
    default       = None,
)

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
