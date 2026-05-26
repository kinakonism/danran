"""
danran - 家族専用チャットアプリ  Streamlit × Supabase
セッション: Supabase sessions + URL ?s=SESSION_ID
リアルタイム: @st.fragment(run_every="5s")
通知: in-app toast + ntfy.sh push (secrets.ntfy.topic が必要)
"""

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
    initial_sidebar_state="expanded",
)

# Streamlit のツールバー（Share / ☆ / GitHub 等）を非表示
st.markdown("""
<style>
[data-testid="stToolbar"]          { display: none !important; }
[data-testid="stDecoration"]       { display: none !important; }
[data-testid="stMainBlockContainer"] > div:first-child { padding-top: 1rem; }
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
    st.session_state["view"] = "select_user"
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
# 長押し検出 JS（親フレーム DOM に 1 回だけ observer を設定）
# ─────────────────────────────────────
LONG_PRESS_JS = """
<script>
(function() {
    const LP_MS = 500;
    const par   = window.parent;

    // iframe が再作成されるたびに呼ばれるが、初期化は 1 回のみ
    if (par._danranLpReady) { return; }
    par._danranLpReady = true;

    const doc = par.document;

    function fireSignal(msgId) {
        const inp = doc.querySelector('input[placeholder="__lp__"]');
        if (!inp) return;
        const setter = Object.getOwnPropertyDescriptor(
            par.HTMLInputElement.prototype, 'value'
        ).set;
        setter.call(inp, msgId + '|' + Date.now());
        inp.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function attach(el, msgId) {
        if (el._lpOk) return;
        el._lpOk = true;
        let t, moved;
        // passive:false にして iOS の長押しポップアップを抑制
        el.addEventListener('touchstart', (e) => {
            moved = false;
            t = setTimeout(() => { if (!moved) fireSignal(msgId); }, LP_MS);
        }, { passive: false });
        el.addEventListener('touchmove',   () => { moved = true; clearTimeout(t); }, { passive: true });
        el.addEventListener('touchend',    () => clearTimeout(t));
        el.addEventListener('touchcancel', () => clearTimeout(t));
        // デスクトップ: 右クリックでも開く
        el.addEventListener('contextmenu', (e) => { e.preventDefault(); fireSignal(msgId); });
    }

    // [data-lp-msg] 要素に直接アタッチ（stChatMessage 親を探す必要がなくなった）
    function scan() {
        doc.querySelectorAll('[data-lp-msg]:not([data-lp-ok])').forEach(el => {
            el.setAttribute('data-lp-ok', '1');
            attach(el, el.getAttribute('data-lp-msg'));
        });
    }

    new MutationObserver(scan).observe(doc.body, { childList: true, subtree: true });
    scan();
})();
</script>
"""

# ─────────────────────────────────────
# ★ リアルタイムタイムライン（フラグメント）
#   5秒ごとに自動更新。ページ全体は再描画しない。
# ─────────────────────────────────────
@st.fragment(run_every="5s")
def render_messages() -> None:
    current_user  = st.session_state.get("current_user", {})
    selected_room = st.session_state.get("active_room", ROOMS[0])
    uname         = current_user.get("name", "")

    messages  = fetch_messages(selected_room)

    # 新着トースト（他人のメッセージのみ）
    count_key  = f"cnt_{selected_room}"
    prev       = st.session_state.get(count_key, -1)
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

    # ── 長押しシグナル入力（JS から React イベント経由で更新される） ──
    st.markdown("""<style>
    .stTextInput:has(input[placeholder="__lp__"]) {
        height:0!important;min-height:0!important;
        overflow:hidden!important;margin:0!important;padding:0!important;
    }
    /* バブルの長押し時に iOS テキスト選択メニューを抑制 */
    [data-lp-msg] { -webkit-touch-callout:none; -webkit-user-select:none; user-select:none; }
    </style>""", unsafe_allow_html=True)
    st.text_input("lp", placeholder="__lp__", key="_lp_input", label_visibility="collapsed")
    st.components.v1.html(LONG_PRESS_JS, height=0)

    # 長押しされたメッセージ ID（"ID|TS" → ID を取り出す）
    _lp_raw      = st.session_state.get("_lp_input", "") or ""
    reaction_for = _lp_raw.split("|")[0] if "|" in _lp_raw else None

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
        pills_row = (
            f'<div style="margin-top:4px;text-align:{"right" if is_mine else "left"};'
            f'line-height:2">{pills}</div>'
        ) if pills else ""

        # ── LINE 風バブル HTML（自分＝右、他人＝左） ──
        if is_mine:
            bubble = (
                f'<div data-lp-msg="{msg_id}" style="'
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

        # ── 長押し → インライン リアクション＋削除ピッカー ──
        if reaction_for == msg_id:
            n_extra = 2 if is_mine else 1   # 🗑️（自分のみ） + ✕
            rcols   = st.columns(len(REACTION_EMOJIS) + n_extra)
            for i, emoji in enumerate(REACTION_EMOJIS):
                with rcols[i]:
                    already = uname in msg_reactions.get(emoji, [])
                    if st.button(
                        emoji + (" ✓" if already else ""),
                        key=f"r_{msg_id}_{emoji}",
                        use_container_width=True,
                    ):
                        toggle_reaction(msg_id, uname, emoji)
                        st.session_state["_lp_input"] = ""
                        st.rerun()
            if is_mine:
                with rcols[len(REACTION_EMOJIS)]:
                    if st.button("🗑️", key=f"del_{msg_id}",
                                 use_container_width=True, help="削除"):
                        delete_message(msg_id, uname)
                        st.session_state["_lp_input"] = ""
                        st.rerun()
            with rcols[-1]:
                if st.button("✕", key=f"close_r_{msg_id}", use_container_width=True):
                    st.session_state["_lp_input"] = ""
                    st.rerun()

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
        c1, c2 = st.columns(2)
        with c1:
            if st.button("← 戻る", use_container_width=True):
                st.session_state.update(view="select_user"); st.session_state.pop("selected_user", None); st.rerun()
        with c2:
            if st.button("🔓 ログイン", use_container_width=True, type="primary"):
                if not pw:
                    st.error("パスワードを入力してください"); return
                u = get_user_with_hash(user["id"])
                if u and verify_password(pw, u.get("password_hash") or ""):
                    st.session_state.pop("selected_user", None); do_login(u); st.rerun()
                else:
                    st.error("パスワードが違います 🔒")

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
        c1, c2 = st.columns(2)
        with c1:
            if st.button("← 戻る", use_container_width=True):
                st.session_state["view"] = "select_user"; st.rerun()
        with c2:
            if st.button("✅ 登録する", use_container_width=True, type="primary"):
                if not name.strip(): st.error("お名前を入力してください"); return
                if atype == "写真" and not avatar_photo: st.error("写真を選択してください"); return
                if len(pw) < 4: st.error("パスワードは4文字以上"); return
                if pw != pw2: st.error("パスワードが一致しません"); return
                new_uid = str(uuid.uuid4())
                final_av = (
                    upload_photo(AVATAR_BUCKET, new_uid, avatar_photo) if atype == "写真"
                    else avatar_emoji
                )
                with st.spinner("登録中…"):
                    user = register_user(name.strip(), final_av, pw, uid=new_uid)
                do_login(user); st.rerun()

# ─────────────────────────────────────
# 画面④ メインチャット
# ─────────────────────────────────────
def show_chat(current_user: dict) -> None:

    # ── サイドバー ──
    with st.sidebar:
        st.markdown("## 🏠 danran")
        av = current_user["avatar"]
        if av.startswith("http"):
            st.image(av, width=48); st.markdown(f"**{current_user['name']}** としてログイン中")
        else:
            st.markdown(f"**{av} {current_user['name']}** としてログイン中")
        st.divider()

        st.markdown("### 💬 チャットルーム")
        unread = get_unread_counts(current_user["id"])
        room_labels = [
            f"🔴 {r}  +{unread[r]}" if unread.get(r, 0) > 0 else r
            for r in ROOMS
        ]
        selected_idx: int = st.radio(
            "ルーム", range(len(ROOMS)),
            format_func=lambda i: room_labels[i],
            label_visibility="collapsed",
        )
        selected_room = ROOMS[selected_idx]
        st.session_state["active_room"] = selected_room

        st.divider()
        if st.button("🔒 ログアウト", use_container_width=True):
            do_logout(); st.rerun()
        st.markdown("---")
        st.caption("© danran family")

    # ── メインエリア ──
    head_col, img_col = st.columns([6, 1])
    with head_col:
        st.markdown(f"## 💬 {selected_room}")
    with img_col:
        # 写真送信 popover（ヘッダー右端）
        with st.popover("📷", use_container_width=True):
            st.markdown("**写真を送る**")
            img_file = st.file_uploader(
                "画像を選ぶ", type=["jpg","jpeg","png","gif","webp"],
                label_visibility="collapsed", key="chat_img",
            )
            if img_file:
                st.image(img_file, width=200)
                if st.button("📤 送信", type="primary", use_container_width=True):
                    with st.spinner("送信中…"):
                        url = upload_photo(CHAT_IMG_BUCKET, str(uuid.uuid4()), img_file)
                        send_message(selected_room, current_user["name"], current_user["avatar"], "", image_url=url)
                    st.rerun()

    # ★ リアルタイム更新フラグメント（5秒ごと）
    render_messages()

    # ── テキスト入力 ──
    av_str = current_user["avatar"]
    ph = (
        f"{av_str} {current_user['name']} としてメッセージを送る…"
        if not av_str.startswith("http")
        else f"{current_user['name']} としてメッセージを送る…"
    )
    if prompt := st.chat_input(ph):
        send_message(selected_room, current_user["name"], current_user["avatar"], prompt)
        st.rerun()

# ─────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────
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
