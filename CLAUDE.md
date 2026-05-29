# danran — Claude 向けプロジェクト知識

> このファイルは Claude Code が読むプロジェクト固有の指示・設計メモです。
> 実装を変更する前に必ずここを参照してください。

---

## デプロイ・インフラ情報

| 項目 | 値 |
|------|-----|
| **本番 URL** | `https://danran-dhawa6nhapcwnq6lrjqzhw.streamlit.app/` |
| **ホスティング** | Streamlit Community Cloud（無料枠） |
| **GitHub リポジトリ** | `https://github.com/kinakonism/danran` |
| **デプロイ方法** | `main` ブランチへ push → Streamlit Cloud が自動デプロイ（1〜2分） |
| **Supabase プロジェクト** | `https://fyadpbzlvyzihynpcckw.supabase.co` |
| **PWA インストール URL** | `https://danran-dhawa6nhapcwnq6lrjqzhw.streamlit.app/install.mobileconfig` |

> `install.mobileconfig` は iOS 向け Web Clip プロファイル。Safari でタップ→設定でインストール→ホーム画面に自動追加。

---

## プロジェクト概要

- **名前**: danran（団欒）― 家族専用チャットアプリ
- **スタック**: Python Streamlit + Supabase (PostgreSQL + Storage)
- **起動**: `uv run python run.py`（旧: `uv run streamlit run app.py` — PWA後は run.py を使う）
- **主ファイル**:
  - `run.py` — エントリーポイント。Starlette の create_streamlit_routes をパッチして `/sw.js` `/manifest.json` `/icons/*` を追加してから Streamlit を起動する
  - `app.py` — Streamlit アプリ本体（全画面・全 DB 操作）
  - `components/longpress/index.html` — ゼロ高さカスタムコンポーネント（JS）
  - `sw.js` — Service Worker（プッシュ通知受信・通知タップ処理）
  - `manifest.json` — PWA マニフェスト（ホーム画面追加・スプラッシュ等）
  - `icons/` — アイコン画像（icon-192.png, icon-512.png, badge.png）

---

## アーキテクチャ

### 画面遷移

```
select_user（ログイン）
    ↓ do_login()
chat（チャット）⇄ ルーム選択（_show_rooms=True で同一 view 内トグル）
    │   │           ├ room_edit（既存ルーム編集）← ルーム行の ⚙️
    │   │           └ room_create（新規ルーム作成）← ルーム選択の + ボタン
    │   ↓ go_profile（ルーム選択ヘッダー右上アバター）
    │ profile（プロフィール編集 ＋ 通知設定ボタン ＋ ログアウト[2段階確認]）
    │   ↓
    └ notifications（通知設定）
```

`st.session_state["view"]` で管理。`match` 文でルーティング。  
ルーム選択⇄チャットは view を変えず `_show_rooms` フラグで切替（[セッション管理](#ルーム選択--チャットの状態管理_show_rooms)参照）。  
**アカウント編集・ログアウトはルーム選択画面に置かず、ヘッダー右上アバター→プロフィール画面に集約**（LINE 風 UX）。

### セッション管理

- ログイン時: `sessions` テーブルに INSERT → `?s=SESSION_ID` をURLにセット
- ブラウザ復元: JS が localStorage → stSetValue(restore_session) → Python が `get_session_user()` で復元
- ログアウト: `sessions` レコード削除 + localStorage クリア。**プロフィール画面下部のボタンから「本当にログアウトしますか？→ いいえ/はい」の2段階確認**（`_logout_confirm` session_state）。誤操作防止のためルーム選択画面には置かない。
- **セッション TTL**: pg_cron `danran-session-cleanup`（毎朝4時）で30日以上前の `sessions` を削除。
- **Render 1 フラッシュ防止**: `_lp_result is None` かつ未ログイン時は空白画面を表示しログインフォームを出さない。JS が必ず restore_session を送るので Render 2 以降で正しい画面に遷移。
- **ログアウト後の blank screen 防止**: JS `handleSession(clearSession=true)` 時に `_sessionRestoreSent=false` なら `restore_session(session_id='')` を送って Python の `_waiting_for_js` デッドロックを解除する。

### ルーム選択 ⇄ チャットの状態管理（`_show_rooms`）

- **`st.session_state["_show_rooms"]` で管理**（旧 `?sr=1` URL パラメータは廃止）。
  - 理由: URL を変えると iOS Safari / Web Clip のネイティブ戻るジェスチャーが履歴を巻き戻し、「一瞬戻ってすぐ閉じる」バグになる。session_state なら URL 不変。
- `go_rooms` で `True`、`go_chat` / `go_room` で `pop`。
- ルーム選択はトップ画面。ヘッダーに `＜` を出さず、右上のアバター（`data-hdr-profile`）→ プロフィール画面へ。

### リアルタイム更新

- `@st.fragment(run_every="2s")` の `render_messages()` が2秒ごとにメッセージをポーリング
- `@st.fragment(run_every="5s")` の `render_room_list()` がルーム選択中の未読バッジを更新
- フラグメントはページ全体を rerun しない
- **両フラグメントとも冒頭にガードが必須**（`_show_rooms` / `current_user` / `view` を確認して早期 return）。`run_every` フラグメントはナビゲーション後も独立してタイマー発火するため、ガードがないと遷移直後に旧画面が一瞬再描画されてちらつく。

---

## JS カスタムコンポーネント（index.html）

### 重要な制約

**`isStreamlitMessage: true` が必須**  
全ての `postMessage` に含めないと Streamlit が無視する。

```javascript
window.parent.postMessage(
  { type: ..., isStreamlitMessage: true, ...data }, '*'
);
```

### Python へのナビゲーション指示（全アクション一覧）

```javascript
stSetValue({ action: 'go_rooms',       ts: Date.now() });
stSetValue({ action: 'go_chat',        ts: Date.now() });
stSetValue({ action: 'go_profile',     ts: Date.now() });   // ルーム選択ヘッダー右上アバター(data-hdr-profile)から
stSetValue({ action: 'go_back',        ts: Date.now() });
stSetValue({ action: 'go_notifications', ts: Date.now() });
stSetValue({ action: 'go_room',        room_name: '...', ts: Date.now() });
stSetValue({ action: 'go_room_edit',   room_id: '...', ts: Date.now() });
stSetValue({ action: 'go_room_create', ts: Date.now() });
stSetValue({ action: 'restore_session', session_id: '...', ts: Date.now() });
stSetValue({ action: 'save_push_subscription', subscription: '...', user_id: '...', ts: Date.now() });
// 注: 'logout' アクションは廃止。ログアウトはプロフィール画面の Streamlit ボタン（2段階確認）に移動。
//     旧 data-logout / data-profile-nav ハンドラは index.html に残るが対象要素が無く不発（無害）。
```

**必ず `ts: Date.now()` を付ける**。Python 側は `_last_nav_ts` で重複処理を防いでいる。`ts` がないと古いキャッシュ値が再発火して無限ループになる。

**★ 最重要: スワイプ等「一度だけ登録するハンドラ」からの nav 送信は `pDoc._danranNav` 経由**。  
`scan()` が毎回ライブ iframe の `stSetValue` を `window.parent.document._danranNav` に上書き登録する。スワイプハンドラ自体は古い iframe が所有していることがあり、その死んだ window から `postMessage` すると Streamlit に `event.source` 不一致で無視される（→ [[swipe-back-live-iframe]] メモリ参照）。

### DOM config パターン

JS が Python の設定を読む仕組み:

```python
# Python 側（app.py エントリーポイント）
st.html(f'<div id="_danran_cfg" data-room="{room}" data-user="{user}" ...>')
```

```javascript
// JS 側
var cfg = window.parent.document.getElementById('_danran_cfg');
var room = cfg.getAttribute('data-room');
```

`st.html()` は Streamlit rerun ごとに更新されるので、render イベント不要。

#### `_danran_cfg` の全属性（現在）

| 属性 | 内容 |
|------|------|
| `data-sb-url` | Supabase URL |
| `data-sb-key` | Supabase anon key |
| `data-user` | ログイン中ユーザー名 |
| `data-avatar` | ログイン中ユーザーアバター |
| `data-uid` | ログイン中ユーザーID |
| `data-room` | アクティブルーム名 |
| `data-sess` | 保存すべきセッションID |
| `data-clear` | `"true"` でlocalStorageを消す |
| `data-show-rooms` | `"true"` でルーム選択中（`st.session_state["_show_rooms"]` 由来） |
| `data-view` | 現在の view 名 |
| `data-vapid-pub` | VAPID 公開鍵 |
| `data-users-json` | 全ユーザーの名前・電話番号 JSON（FaceTime 用） |

#### ヘッダーの構成（Python が `st.html` で描画）

- ログイン中は `#_danran_hdr`（`position:fixed`）を Python 側がレンダリング。クリックハンドラのみ JS（`attachHdrButtons`）が `data-hdr-nav` / `data-hdr-back` / `data-hdr-profile` に付与。
- **チャット画面**: 左 `＜`（`data-hdr-nav`→`go_rooms`）｜中央ルーム名｜右スペーサー
- **ルーム選択画面（トップ）**: 左スペーサー（`＜` を出さない）｜中央「ルーム選択」｜右アバター（`data-hdr-profile`→`go_profile`）
- **編集画面**: 左 `＜`（`data-hdr-back`→`go_back`）｜中央タイトル｜右スペーサー

#### `data-users-json` の形式（FaceTime 機能追加後）

```python
import json as _json
_all_users = fetch_all_users()  # [{"name": "...", "phone": "...", "avatar": "..."}]
data_users = _json.dumps(_all_users, ensure_ascii=False)
st.html(f'<div id="_danran_cfg" ... data-users-json=\'{data_users}\'>')
```

```javascript
// JS 側: ユーザー名 → 電話番号マップを構築
var usersJson = cfg.getAttribute('data-users-json') || '[]';
var _usersMap = {};  // name → { phone, avatar }
JSON.parse(usersJson).forEach(function(u) { _usersMap[u.name] = u; });
```

### data-room が空になってはいけない

`data-room=""` になると JS の `injectHeader()` と `injectCameraUI()` が早期リターンし、
**ヘッダーが消え・padding-bottom CSS が消え・カメラボタンが消える**。

エントリーポイントの `_active_room` は必ず非空にする:

```python
# ✅ 正しい実装（セッション復元直後に active_room 未セットでも DB の先頭ルームを使う）
_rooms_for_hdr = fetch_rooms()
_active_room   = st.session_state.get("active_room") or (
    _rooms_for_hdr[0]["name"] if _rooms_for_hdr else ""
)

# ❌ NG（active_room 未セット時に "" になりヘッダーが消える）
_active_room = st.session_state.get("active_room", "")
```

### キャッシュバスト

JS コンポーネントをブラウザにキャッシュさせないため、変更時は component 名をインクリメント:

```python
_lp_detector = st.components.v1.declare_component(
    "danran_lp_v49",   # ← v49, v50... と上げる（現在 v49）
    path=_LP_COMPONENT_DIR,
)
```

> JS（index.html）を変更したら必ずインクリメント。Python のみの変更（CSS 文字列等）なら据え置きで可。

### 右スワイプで戻る（指追従ドラッグ）

チャット画面で右にドラッグすると `.stApp` を `translateX` で指追従させ、離したら戻る/進むを判定する。

- **対象**: `view==='chat' && !_show_rooms` のときのみドラッグ有効。ルーム選択（トップ）・編集画面では指追従しない。
- **touchstart** `passive:true`（左50%以内で開始） / **touchmove** `passive:false`（横ロック後に `preventDefault` で縦スクロール・iOS戻りを抑止） / **touchend** `passive:true`。
- **完了判定**: `dx > 画面幅*0.32` または `dx>60 && 速度>0.5px/ms`。
- **完了時**: `clearDrag(el, false)` で**アニメなし瞬時**に `translateX(0)`（「戻る」動きを見せない）→ `go_rooms`。ルーム選択は CSS `danranSlideInLeft` でスライドイン。
- **キャンセル時**: `clearDrag(el, true)` でバネ戻し。
- **`touchcancel`** でも必ず `clearDrag`（通話・通知での中断対策）。
- **★ 画面外へ投げ飛ばさない**: 旧実装は `.stApp` を画面外へ throw → `go_rooms` 後に戻す設計だったが、サーバー往復ラグで戻せず**真っ暗固着**した。完了時も必ず `translateX(0)` に戻すこと。
- nav 送信は `pDoc._danranNav` 経由（古い iframe 問題、上記参照）。

### スライドイン演出 / 選択フィードバック

- `_nav_anim="left"`（`go_rooms`/編集からの戻り時にセット）→ `render_room_list` が `#_danran_room_list` に `danranSlideInLeft` を1回だけ適用。`pop` で消費するのでフラグメントの定期更新では再生しない。
- ルームタップ時: `button.dr-room` に `:active` 押下ハイライト＋JS が即 `.dr-selected`（緑）を付与し、サーバー往復中も選択状態が見える。

---

## Supabase スキーマ（現在の完全版）

### `users`

| カラム         | 型      | 説明                         |
|--------------|---------|------------------------------|
| id           | uuid PK | ユーザーID                    |
| name         | text    | 表示名                        |
| avatar       | text    | 絵文字 or 写真URL              |
| phone        | text    | 電話番号（正規化済・任意）      |
| password_hash| text    | bcrypt ハッシュ               |
| created_at   | timestamptz | 作成日時                |

RLS: SELECT は全許可 / UPDATE は `true` ポリシーで全許可（家族アプリ）

### `sessions`

| カラム    | 型      | 説明               |
|---------|---------|-------------------|
| id      | uuid PK | セッションID        |
| user_id | uuid FK | users.id → CASCADE |
| created_at | timestamptz | |

### `messages`

| カラム       | 型      | 説明                     |
|------------|---------|--------------------------|
| id         | uuid PK |                          |
| room_name  | text    | ルーム名（rooms.name と同期） |
| user_id    | uuid    | 送信者ID（is_mine 判定用） |
| user_name  | text    | 送信時のスナップショット    |
| user_avatar| text    | 送信時のアバタースナップショット |
| content    | text    | メッセージ本文（空可）     |
| image_url  | text    | 添付画像URL（任意）        |
| created_at | timestamptz | |

**`is_mine` 判定**: `user_id` で比較（名前変更後も正しく動く）。`user_id` が空の旧メッセージは `user_name` でフォールバック。

### `reactions`

| カラム      | 型      | 説明             |
|-----------|---------|-----------------|
| id        | uuid PK |                 |
| message_id| uuid FK | messages.id     |
| user_name | text    | リアクションしたユーザー名 |
| emoji     | text    | 絵文字           |

### `last_read`

| カラム     | 型      | 説明             |
|----------|---------|-----------------|
| user_id  | uuid    | ユーザーID        |
| room_name| text    | ルーム名          |
| read_at  | timestamptz | 最終既読日時  |

UNIQUE(user_id, room_name) で upsert。

### `rooms`

| カラム    | 型      | 説明             |
|---------|---------|-----------------|
| id      | uuid PK |                 |
| name    | text    | ルーム名（表示・外部キー代わり） |
| icon    | text    | 絵文字 or 写真URL |
| created_at | timestamptz | 作成順 |

- ルーム名を変更すると `messages.room_name` と `last_read.room_name` も連動更新
- ルーム削除時は reactions → messages → last_read → rooms の順で削除

### Supabase Storage バケット

| バケット      | 用途                                      |
|-------------|------------------------------------------|
| avatars     | ユーザーアイコン写真（`{user_id}.jpg`）、ルームアイコン写真（`room_{room_id}.jpg`） |
| chat-images | チャット添付画像（JS が直接アップロード）   |

### pg_cron（無料枠停止防止）

```sql
SELECT cron.schedule('danran-keep-alive', '0 9 */3 * *',
  'SELECT count(*) FROM public.messages');
```

3日ごと午前9時に実行してデータベースの自動一時停止を防ぐ。

---

## 重要な実装パターン

### EXIF 回転補正（スマホ写真）

```python
def _fix_exif(f) -> tuple[bytes, str]:
    f.seek(0)
    img = Image.open(f)
    img = ImageOps.exif_transpose(img)   # EXIF に従って自動回転
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue(), "image/jpeg"
```

`st.image()` や他の処理でファイルポインタが消費されるため、`_fix_exif` 内で `f.seek(0)` している。

### プロフィール / ルーム編集 / ルーム作成 のウィジェットリセット

編集画面を開くたびに session_state からウィジェットキーを削除しないと、  
前回の値が残り（特に radio）意図しないデフォルトになる。

```python
_PROFILE_WIDGET_KEYS = ("profile_atype", "profile_emoji", "profile_photo", "profile_name", "profile_phone")
def _reset_profile_widgets():
    for k in _PROFILE_WIDGET_KEYS: st.session_state.pop(k, None)

_ROOM_EDIT_WIDGET_KEYS = ("room_edit_atype", "room_edit_emoji", "room_edit_photo", "room_edit_name")
def _reset_room_edit_widgets():
    for k in _ROOM_EDIT_WIDGET_KEYS: st.session_state.pop(k, None)
    for k in list(st.session_state.keys()):
        if k.startswith("room_delete_confirm_"): st.session_state.pop(k, None)

# ★NEW: ルーム作成用
_ROOM_CREATE_WIDGET_KEYS = ("room_create_atype", "room_create_emoji", "room_create_photo", "room_create_name")
def _reset_room_create_widgets():
    for k in _ROOM_CREATE_WIDGET_KEYS: st.session_state.pop(k, None)
```

### ルームキャッシュ

```python
@st.cache_data(ttl=60)
def fetch_rooms() -> list[dict]:
    ...

def invalidate_rooms_cache():
    fetch_rooms.clear()  # 更新・削除後に必ず呼ぶ
```

### 電話番号正規化

```python
def normalize_phone(phone: str) -> str:
    import re
    return re.sub(r"[\s\-ー－]", "", phone)
```

ログイン時は名前 → 電話番号の順に検索。

### 招待コード

```toml
# .streamlit/secrets.toml
[app]
register_key = "danran2024"
```

未設定なら誰でも登録可能（初期セットアップ用）。

---

## PWA + Web Push アーキテクチャ

### なぜ run.py が必要か

Streamlit 1.57 は Tornado ではなく **Starlette + Uvicorn** ベース。  
サービスワーカーに必要な `/sw.js` はルートパスで配信が必要だが、Streamlit は内部ルートを固定している。

`run.py` は `create_streamlit_routes` を Streamlit が呼ぶ前にモンキーパッチし、  
`/sw.js`・`/manifest.json`・`/icons/*` の 3 ルートを最優先で追加する。  
sw.js には `Service-Worker-Allowed: /` ヘッダーを付与してスコープをルートに拡張している。

```
uv run python run.py   ← 必ずこちらで起動
# uv run streamlit run app.py  ← これだと /sw.js が 404 になる
```

### VAPID キー管理

`.streamlit/secrets.toml` の `[push]` セクション:
```toml
[push]
vapid_public_key  = "..."   # フロントエンド（JS）に渡す
vapid_private_key = "..."   # バックエンド（pywebpush）専用、絶対に公開しない
vapid_subject     = "mailto:..."
```

キーを再生成する場合:
```python
from py_vapid import Vapid
import base64
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
v = Vapid(); v.generate_keys()
pub = base64.urlsafe_b64encode(v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)).decode().rstrip('=')
print("public:", pub)
print("private:", v.private_pem().decode())
```

### push_subscriptions テーブル

| カラム   | 型   | 説明 |
|--------|------|------|
| id     | uuid PK | |
| user_id | uuid FK → users | |
| endpoint | text | プッシュエンドポイント URL |
| p256dh | text | 暗号化キー |
| auth   | text | 認証シークレット |

`UNIQUE(user_id, endpoint)` で upsert。  
410 Gone が返った購読は `send_push()` 内で自動削除。

### iOS Web Push の制約

- **iOS 16.4 以上** + **ホーム画面に追加した PWA** からのみ動作
- Safari のブラウザタブから開いた場合は通知が来ない
- 通知許可ダイアログはユーザーの操作（バナータップ）に応答して表示
- `Notification.requestPermission()` は top-level frame のコンテキストで呼ぶ必要がある  
  → `window.parent._danranSubscribe()` 経由で親ウィンドウから呼ぶ

### 通知の流れ

```
send_message() → send_push(room, sender_uid, sender_name, content)
    → push_subscriptions から送信者以外の全購読を取得
    → pywebpush.webpush() で各デバイスに送信
    → 410 Gone の期限切れ購読は自動削除
```

---

## FaceTime 通話機能

### 概要（未実装・計画段階）

チャット画面で他ユーザーのアバターをタップすると、プロフィールポップアップが表示され、  
電話番号が登録されている場合は FaceTime 通話ボタンが現れる。

### iOS FaceTime URL スキーム

```
facetime:{phone_or_email}        # ビデオ通話
facetime-audio:{phone_or_email}  # 音声のみ
```

**制限**: iOS のみ。Android・PC ではリンクが動作しない。

### 実装方針

- `_danran_cfg` の `data-users-json` に全ユーザーの名前・電話番号を渡す
- 他ユーザーのアバター要素に `data-lp-sender="{sender_name}"` 属性を付与（Python 側）
- JS: アバタークリック → `_usersMap[senderName]` で電話番号を参照 → ポップアップ表示
- JS ポップアップ: アバター大表示 + 名前 + FaceTime ボタン（電話番号がある場合のみ）

### 自分のアバタータップとの使い分け

| 対象 | 属性 | 動作 |
|------|------|------|
| 自分のアバター | `data-lp-my-avatar="1"` | → プロフィール編集画面へ |
| 他人のアバター | `data-lp-sender="{name}"` | → FaceTime ポップアップ |

### fetch_all_users()（追加する関数）

```python
@st.cache_data(ttl=300)
def fetch_all_users() -> list[dict]:
    """FaceTime 用: 全ユーザーの名前・電話番号・アバターを取得。"""
    result = supabase.table("users")\
        .select("name, avatar, phone")\
        .execute()
    return result.data or []
```

---

## ルーム作成機能

### 概要（実装済み）

ルーム選択画面の「チャットルーム」セクションラベル横に `+` ボタンを配置。  
タップするとルーム作成画面（`show_room_create()`）に遷移する。

### `show_room_create()` の設計

`show_room_edit()` と同じ UI だが以下の点が異なる:

| 項目 | room_edit | room_create |
|------|-----------|-------------|
| タイトル | ⚙️ ルーム編集 | ✨ 新しいルーム |
| 入力フォーム | 既存値を初期値 | 空欄（デフォルト絵文字 `💬`） |
| 保存ボタン | 更新 | 作成 |
| 削除ボタン | あり | なし |
| DB 操作 | `update_room()` | `create_room()` |

### `create_room()` DB 関数

```python
def create_room(name: str, icon: str) -> dict:
    """新しいルームを作成して返す。"""
    new_id = str(uuid.uuid4())
    result = supabase.table("rooms")\
        .insert({"id": new_id, "name": name, "icon": icon})\
        .execute()
    invalidate_rooms_cache()
    return result.data[0] if result.data else {}
```

### + ボタンの HTML（ルームリスト内）

```html
<!-- セクションラベルと + ボタンを横並びに -->
<div style="display:flex;align-items:center;justify-content:space-between;...">
  <span>チャットルーム</span>
  <button data-room-create="true" ...>＋</button>
</div>
```

---

## 既知のハマりポイント（過去の失敗から）

### RLS UPDATE ポリシー漏れ

`users` テーブルに UPDATE ポリシーがないと、`supabase.update()` が例外を出さず静かに 0 行更新になる。プロフィール保存が「成功」に見えて実際は何も変わらない症状。

→ Supabase Dashboard > Authentication > Policies で UPDATE ポリシーを確認すること。

### stSetValue の二重発火

古い `stSetValue` の値は Streamlit が rerun ごとにキャッシュから再送する。  
`ts: Date.now()` なしで `{action: 'go_profile'}` を送ると、  
profile 画面から戻るたびにまた profile に飛ぶ無限ループになる。

→ 全ての `stSetValue` に `ts: Date.now()` を付け、Python 側で `_last_nav_ts` と比較して一度だけ処理する。

### active_room 未セット時の空ヘッダー

`active_room` は `show_chat()` の中で初めてセットされる。  
エントリーポイント（`show_chat` より前）で `data-room` を計算する際、  
`active_room` が未セットだと `""` になりヘッダーが消える。  
`fetch_rooms()[0]["name"]` をフォールバックにすること（`ROOMS_FALLBACK[0]` でも可だが DB の実態と乖離する）。

### 写真アップロード後のファイルポインタ消費

`st.image(file)` を呼ぶと内部でファイルを読み切ってポインタが末尾に移動する。  
その後 `upload_photo()` を呼んでも 0 バイトになる。  
→ `_fix_exif()` で `f.seek(0)` しているので、これを経由することで解決済み。

### ルーム作成でキャッシュ破棄を忘れない

`create_room()` 後は必ず `invalidate_rooms_cache()` を呼ぶ。  
`fetch_rooms()` は TTL=60s でキャッシュされるため、呼ばないと新ルームが一覧に現れない。

### FaceTime ポップアップが iOS 以外で動作しない

`facetime:` URL スキームは iOS 専用。  
Android・PC 環境では何も起きないか、ブラウザがエラーを出す。  
ポップアップ上に「iPhone でのみ動作します」等の注意書きを入れることを検討。

### スワイプ等の nav が「ボタンは効くのにスワイプだけ効かない」

一度だけ登録するハンドラ（スワイプ等）が**古い iframe に取り残され**、その死んだ window から `postMessage` → Streamlit が `event.source` 不一致で無視するのが原因。  
→ nav 送信は必ず `scan()` がライブ iframe で毎回上書きする `pDoc._danranNav` 経由にする。ボタンは `scan()` が毎回ハンドラを付け直すので影響を受けない。詳細は [[swipe-back-live-iframe]] メモリ。

### iOS でスワイプ遷移が「一瞬戻ってすぐ閉じる」

`?sr=1` など **URL を変える**と iOS のネイティブ戻るジェスチャーが履歴を巻き戻して競合する。  
→ ルーム選択状態は `st.session_state["_show_rooms"]`（URL 不変）で管理する。

### 指追従ドラッグで画面を「投げ飛ばさない」

`.stApp` を画面外へ `translateX` で飛ばして遷移後に戻す設計は、サーバー往復ラグで戻せず**真っ暗固着**する。  
→ 完了時も必ず `translateX(0)` に戻す（瞬時クリア）。遷移演出は CSS スライドインに任せる。

### run_every フラグメントは遷移後もタイマー発火する

`render_messages`（2s）/`render_room_list`（5s）はナビゲーション後も独立して再実行されるため、  
冒頭ガード（`_show_rooms`/`current_user`/`view`）がないと遷移直後に旧画面が一瞬ちらつく。両方に必須。

### メッセージ削除は user_id で認可

`delete_message` は `user_name`（変更可能）ではなく `user_id`（UUID）で認可する。JS の `deleteMsg` も `ME_UID` を使う。名前を他人に合わせて他人のメッセージを消せてしまう穴を防ぐため。

---

## 開発時のデバッグ

```bash
# ローカル起動
uv run python run.py

# Supabase ログ確認
# → MCP ツール: mcp__supabase__get_logs

# コンポーネントキャッシュが怪しいとき
# → "danran_lp_v49" の数字をインクリメント（現在 v49）

# iOS PWA など画面にログを出せない環境のデバッグ
# → JS 側: 色付きの fixed div を一定時間表示する _dbg(color,msg) 方式、
#    Python 側: st.html の状態バッジ、を併用すると JS→Python のどこで
#    切れているか切り分けやすい（過去のスワイプ不具合はこれで特定）
```
