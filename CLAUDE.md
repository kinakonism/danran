# danran — Claude 向けプロジェクト知識

> このファイルは Claude Code が読むプロジェクト固有の指示・設計メモです。
> 実装を変更する前に必ずここを参照してください。

---

## プロジェクト概要

- **名前**: danran（団欒）― 家族専用チャットアプリ
- **スタック**: Python Streamlit + Supabase (PostgreSQL + Storage)
- **起動**: `uv run python run.py`（旧: `uv run streamlit run app.py` — PWA後は run.py を使う）
- **主ファイル**:
  - `run.py` — エントリーポイント。Starlette の create_streamlit_routes をパッチして `/sw.js` `/manifest.json` `/static/*` を追加してから Streamlit を起動する
  - `app.py` — Streamlit アプリ本体（全画面・全 DB 操作）
  - `components/longpress/index.html` — ゼロ高さカスタムコンポーネント（JS）
  - `sw.js` — Service Worker（プッシュ通知受信・通知タップ処理）
  - `manifest.json` — PWA マニフェスト（ホーム画面追加・スプラッシュ等）
  - `static/` — アイコン画像（icon-192.png, icon-512.png, badge.png）

---

## アーキテクチャ

### 画面遷移

```
select_user（ログイン）
    ↓ do_login()
chat（チャット）← → room_edit（ルーム編集）
    ↓ go_profile
profile（プロフィール編集）
```

`st.session_state["view"]` で管理。`match` 文でルーティング。

### セッション管理

- ログイン時: `sessions` テーブルに INSERT → `?s=SESSION_ID` をURLにセット
- ブラウザ復元: JS が localStorage → `?s=` → Python が `get_session_user()` で復元
- ログアウト: `sessions` レコード削除 + localStorage クリア

### リアルタイム更新

- `@st.fragment(run_every="2s")` の `render_messages()` が2秒ごとにメッセージをポーリング
- フラグメントはページ全体を rerun しない（メッセージエリアのみ更新）

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

### Python へのナビゲーション指示

```javascript
stSetValue({ action: 'go_rooms', ts: Date.now() });
stSetValue({ action: 'go_chat',  ts: Date.now() });
stSetValue({ action: 'go_profile', ts: Date.now() });
```

**必ず `ts: Date.now()` を付ける**。Python 側は `_last_nav_ts` で重複処理を防いでいる。`ts` がないと古いキャッシュ値が再発火して無限ループになる。

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
    "danran_lp_v5",   # ← v5, v6, v7... と上げる
    path=_LP_COMPONENT_DIR,
)
```

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

### プロフィール / ルーム編集のウィジェットリセット

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
`/sw.js`・`/manifest.json`・`/static/*` の 3 ルートを最優先で追加する。  
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

---

## 開発時のデバッグ

```bash
# Playwright テスト（.venv 内の uv 経由）
uv run python /tmp/test_xxx.py

# Supabase ログ確認
# → MCP ツール: mcp__supabase__get_logs

# コンポーネントキャッシュが怪しいとき
# → "danran_lp_v5" の数字をインクリメント
```
