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

- ログイン時: `sessions` テーブルに INSERT → セッションIDは **session_state のみ**に保持し、JS が localStorage に保存。
- ブラウザ復元: JS が localStorage → stSetValue(restore_session) → Python が `get_session_user()` で復元。**同一端末のみ**。
- **★ セキュリティ: セッションIDを URL（`?s=`）に絶対に載せない。** 旧実装は `do_login` が `?s=SESSION_ID` を付け、読込時にそれで自動ログインしていたため、**URL を共有すると受け取った人が共有者としてログイン状態になりチャットが丸見え**になる重大な穴だった。URL からのセッション復元は完全廃止（残存 `?s=` は無視して消す）。
- **無効/漏洩セッションの自己修復**: `restore_session` で `get_session_user()` が None のとき `_clear_session=True` にして JS に localStorage を消させる（古い/漏洩SIDを送り続けない）。
- ログアウト: `sessions` レコード削除 + localStorage クリア。**プロフィール画面下部のボタンから「本当にログアウトしますか？→ いいえ/はい」の2段階確認**（`_logout_confirm`）。誤操作防止のためルーム選択画面には置かない。
- **セッション TTL**: pg_cron `danran-session-cleanup`（毎朝4時）で30日以上前の `sessions` を削除。漏洩時の緊急対応は `DELETE FROM sessions`（全失効＝全員1回再ログイン）。
- **Render 1 フラッシュ防止**: `_lp_result is None` かつ未ログイン時は空白画面（🏠スプラッシュ）を表示。JS が必ず restore_session を送るので Render 2 以降で正しい画面に遷移。`view=="register"`（招待リンク）時はスプラッシュをスキップして即フォーム。
- **ログアウト後の blank screen 防止**: JS `handleSession(clearSession=true)` 時に `_sessionRestoreSent=false` なら `restore_session(session_id='')` を送って `_waiting_for_js` デッドロックを解除。

### 招待リンク（家族のサインアップ導線）

- `?invite=<招待コード>` で**未ログイン時に登録画面へ着地**（ログインもチャット表示もしない）。コードが `register_key` と一致すれば登録画面のコード入力をスキップ（`_invite_ok`）。
- プロフィール画面「📨 家族を招待」で `_invite_url()` を `st.code`（ワンタップコピー）。`_invite_url()` = `{_app_url()}?invite={register_key}`。
- 配布フロー: プロフィールで招待リンクをコピー → 家族に送る → サインアップ画面 → 登録 → 参加。**URL 共有でチャットが見える穴は塞いだので、配布はこのリンクで行う。**

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
stSetValue({ action: 'go_room_edit',   room_id: '...', ts: Date.now() });   // 歯車 or チャットヘッダー右上 👥(data-hdr-roomedit) から
stSetValue({ action: 'go_room_create', ts: Date.now() });
stSetValue({ action: 'refresh_chat',   ts: Date.now() });   // 削除後など: _chat_html 破棄→DBから再描画
stSetValue({ action: 'restore_session', session_id: '...', ts: Date.now() });
stSetValue({ action: 'save_push_subscription', subscription: '...', user_id: '...', ts: Date.now() });
stSetValue({ action: 'set_reply', reply_id: '...', reply_name: '...', reply_text: '...', ts: Date.now() });  // 長押し↩︎ / 左スワイプ→引用返信ターゲットをセット（入力欄上に引用バー）
stSetValue({ action: 'clear_reply', ts: Date.now() });   // 引用バー(data-reply-cancel)の ✕→返信解除
// 注: 'logout' アクションは廃止。ログアウトはプロフィール画面の Streamlit ボタン（2段階確認）に移動。
//     旧 data-logout / data-profile-nav ハンドラは削除済み。
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
| `data-reply-id` / `-name` / `-text` / `-image` | 引用返信ターゲット（JS が固定引用バーを描画） |

#### ヘッダーの構成（Python が `st.html` で描画）

- ログイン中は `#_danran_hdr`（`position:fixed`）を Python 側がレンダリング。クリックハンドラのみ JS（`attachHdrButtons`）が `data-hdr-nav` / `data-hdr-back` / `data-hdr-profile` / `data-hdr-roomedit` に付与。
- **チャット画面**: 左 `＜`（`data-hdr-nav`→`go_rooms`）｜中央ルーム名｜右 `👥`（`data-hdr-roomedit="{room_id}"`→`go_room_edit`＝メンバー管理）
- **ルーム選択画面（トップ）**: 左スペーサー（`＜` を出さない）｜中央「ルーム選択」｜右アバター（`data-hdr-profile`→`go_profile`）
- **編集画面**: 左 `＜`（`data-hdr-back`→`go_back`）｜中央タイトル｜右スペーサー
- `_active_room_id` をヘッダー計算時に参加ルームから引いて `data-hdr-roomedit` に埋め込む。

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
    "danran_lp_v74",   # ← v74, v75... と上げる（現在 v74）
    path=_LP_COMPONENT_DIR,
)
```

> JS（index.html）を変更したら必ずインクリメント。Python のみの変更（CSS 文字列等）なら据え置きで可。
> index.html 冒頭に `var DEBUG=false` があり、`true` にすると `dlog/dwarn` 経由の診断ログが出る（本番はセッションID/VAPID先頭を出さないよう false）。

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
- ルームタップ時: `button.dr-room` に `:active` 押下ハイライト＋JS が即 `.dr-selected`（琥珀）を付与し、サーバー往復中も選択状態が見える。

### 送信メッセージの楽観的 UI（送信中→送信完了）

送信経路は Streamlit のまま（insert＋push 維持）。JS が「送信中」バブルを重ねて即時フィードバックする。

- 送信検知は `setupChatSendHook`（document 委譲・Enter と送信ボタン両方）。**IME 変換中の Enter は `e.isComposing`/`keyCode===229` で除外**（日本語入力対策）。
- `addOptimisticBubble`: グレー＋半透明テラコッタの「🕐送信中…」を即表示。
- 照合: mine バブルへ `data-lp-body`（生テキスト）を付与。`reconcileOptimistic`（scan 毎）が同一テキストの本物 mine が増えたら楽観バブルを削除＝送信完了。9秒来なければ「⚠️送信できませんでした」(赤)。

### 画像の全画面ビューア（ライトボックス）

**チャット画像は JS 管理スロット**: Python は `<img>` を直接出さず `<span class="lp-imgslot" data-img="URL" data-lp-image="URL">`（薄グレー枠）だけ出す。JS `fillImageSlots()`（scan 毎）が URL ごとに `<img>` を1つだけ生成して `pDoc._danranImgPool` にプールし、スロットへ**同じノードを移動させるだけ**にする → 2秒ポーリングの再描画でも画像が再ロードされない狙い。タップは `data-lp-image` を持つスロットに直接 `click` を添付（`_danranImgHooked`）。

- `openImageViewer(src)`: 全画像を DOM 順（古い→新しい）で集めギャラリー化。
- **左上 ✕ / 背景タップ** → 閉じる。**下スワイプ(dy>90)** → 指追従で閉じる。
- **右スワイプ → 古い画像 / 左スワイプ → 新しい画像**（`navigate(±1)`・クロスフェード＋ロード待ち）。
- 表示中は `IMG_VIEWER_ID` 存在チェックで**右スワイプ戻りを無効化**。
- アップロード時に `cache-control: max-age=31536000` を付与（キャッシュ命中率↑）。
- **未解決メモ**: 2秒ポーリング由来の画像チカチカは残存（run_every フラグメントが領域を再ペイント）。完全解消には「変化時のみ再描画」構成への refactor が必要。

### 入室遷移（カバー＋🏠ローディング）

ルームをタップ→チャット表示までの「一覧消去→2回 rerun→描画」のチカチカを隠す。

- `showEnterCover()`: タップ click ハンドラで**即**、地色カバーを全画面に出す（z-index はヘッダーと同値＋body 末尾＝ヘッダーごと覆う）。160ms 遅れて 🏠+danran のパルス（`danranSplashPulse`）をフェードイン（高速遷移ではロゴを出さずチラ見え回避）。
- `scrollToLatestOnEnter()`（scan 毎）: ACTIVE_ROOM が変わった瞬間だけ、最下部へスクロール完了（高さ安定 or 最大320ms）後にカバーをフェードアウト。`_lastChatKey` で入室イベントのみ検出。
- カバーは `data-cover-expire` を持ち scan が期限切れを掃除（死んだ iframe 対策）。

### 軽い既読表示

`read_by_users(room, my_id, msg_created_iso)` が `last_read` を流用し、指定メッセージ以降に既読にした自分以外のユーザーを返す（TZ 差は datetime パースで比較・返りは user_id ソートで**順序固定**）。`render_messages` が**自分の最新メッセージにだけ**「既読 N + ミニアバター」を表示。既読した人だけ出す（未読を責めない＝家族向け）。

> ★ 既読アバターの順序が変わると `st.markdown` 全体が再描画され画像チカチカの一因になるため、必ず決定的順序にすること。

### ナビ送信は sendNav（生きた iframe 経由）に統一

DOM 要素へ1回張るクリックハンドラ等は「古い iframe」が所有することがあり、その死んだ window から `postMessage` すると Streamlit に `event.source` 不一致で無視される（スワイプ不発・ルーム入室失敗・ボタン不発の原因）。  
→ `scan()` がライブ iframe の送信口を `pDoc._danranSend` に毎回登録し、ナビ送信は **`sendNav(val)`** 経由で行う（未登録時のみ自分の `stSetValue` にフォールバック）。トースト/カバーも `data-*-expire` ＋ scan 掃除で「死んだ iframe のタイマー」対策。[[swipe-back-live-iframe]]

### テーマ（あたたかダーク）

`.streamlit/config.toml` の `[theme]` で全体テーマを設定（base=dark / primaryColor=`#f0a868` 琥珀 / backgroundColor=`#1a1614` / secondaryBackgroundColor=`#241f1c` / textColor=`#f0e8e0`）。  
ハードコード色も暖色基調: 自分の吹き出し `#e8915b`（テラコッタ）/ 相手 `#2e2926` / アクセント・選択・通知バナー = 琥珀 / 未読バッジ `#e0654f`。色を足すときはこの系統に合わせる。

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
| reply_to_id   | uuid | 引用返信元のメッセージID（任意） |
| reply_to_name | text | 引用元の送信者名スナップショット |
| reply_to_text | text | 引用元本文スナップショット（120字まで・画像は"📷 写真"） |
| reply_to_image | text | 引用元が画像のときその URL スナップショット（引用にサムネ表示） |
| created_at | timestamptz | |

**`is_mine` 判定**: `user_id` で比較（名前変更後も正しく動く）。`user_id` が空の旧メッセージは `user_name` でフォールバック。

**引用返信**: 長押しポップアップの ↩︎ / メッセージ左スワイプ（指追従・閾値超えで発火）→ JS が `set_reply`（id/name/text/image）を送る → Python が `_reply_to` をセット → cfg の `data-reply-*` で JS に渡る。
- **引用バーは JS が固定描画**（`#_danran_reply_bar`・`injectReplyBar`/`alignReplyBar`）。`position:fixed` で stChatInput の真上に貼り、`alignCamBtn` 経由でスクロール・キーボードに追従（Python フロー描画だと一緒にスクロールしてしまうため）。✕（`#_danran_reply_x`）→ `clear_reply`。
- 次のテキスト送信時に `send_message(reply_to=…)` が `reply_to_*` を保存し消費。`build_messages_html` は `reply_to_id` があるバブル上部に引用ブロックを描画（`data-lp-jump` 付き）。
- **引用タップ → 元メッセージへ `scrollIntoView` + `danranJumpPulse`（ぷるぷる強調）**（`jumpToMessage`）。
- **写真への返信**: `reply_to_image` に元画像 URL を保存し、引用バー・引用ブロックに 34–36px のサムネを表示。
- 引用は**スナップショット保存**（元が削除/範囲外でも表示）。返信のトリガーは全メッセージ可だが、送信はテキストのみ（画像送信＝JS経路は reply 未付与）。バブル/グリッドセルに `data-lp-name`（送信者名）を付与。

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
- ルーム削除時は reactions → messages → last_read → **room_members** → rooms の順で削除
- **ルーム名はグローバルに一意**（messages が room_name キーのため）。作成時の重複チェックは `fetch_rooms()`（user_id なし＝全ルーム）で行う

### `room_members`（招待制ルーム）

| カラム    | 型      | 説明             |
|---------|---------|-----------------|
| id      | uuid PK | |
| room_id | uuid    | rooms.id |
| user_id | uuid    | users.id |
| joined_at | timestamptz | |

`UNIQUE(room_id, user_id)`。RLS は他テーブル同様 全許可（家族アプリ・**制御はアプリ層**）。

- `fetch_rooms(user_id)` が `room_members` で「参加ルームのみ」に絞る（`user_id=""` は全ルーム＝管理用途）。`@st.cache_data` なので user_id ごとにキャッシュ。変更時は `invalidate_rooms_cache()`。
- `create_room(name, icon, creator_id)` が作成者を自動でメンバー化。`delete_room` は room_members も削除。
- メンバー操作: `fetch_room_members(room_id)` / `add_room_member` / `remove_room_member`。
- UI: ルーム編集画面の「👥 メンバー」セクション（一覧 + ✕で外す + multiselect で招待）。チャットヘッダー右上 `👥` から遷移。
- 参加ルーム0のユーザー（招待待ち）には案内表示。`_show_rooms` 時は0ルームでもヘッダーを出しプロフィール/ログアウト導線を確保。
- **プッシュ**: `send_push` は受信者ごとに `_member_room_names(uid)`（スレッド安全な素クエリ）で参加ルームを取得して未読集計。
- 既存導入時は `rooms × users` を全 backfill して「全員が全ルーム」を維持（新規ルームのみ招待制）。
- **注意**: anon key + RLS全許可のため「ソフトな制限」。完全秘匿はマルチテナント＋RLS強制が必要。

### Supabase Storage バケット

| バケット      | 用途                                      |
|-------------|------------------------------------------|
| avatars     | ユーザーアイコン写真（`{user_id}.jpg`）、ルームアイコン写真（`room_{room_id}.jpg`） |
| chat-images | チャット添付画像（JS が直接アップロード）   |

### pg_cron

```sql
-- 無料枠停止防止: 3日ごと午前9時に実行
SELECT cron.schedule('danran-keep-alive', '0 9 */3 * *',
  'SELECT count(*) FROM public.messages');

-- セッション TTL: 毎朝4時に30日以上前の sessions を削除
SELECT cron.schedule('danran-session-cleanup', '0 4 * * *',
  $$DELETE FROM public.sessions WHERE created_at < now() - interval '30 days'$$);
```

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

---

## AI サポートルーム（Claude）

- **ルーム名 `🤖 AIサポート`**（`AI_ROOM_NAME`）。全ユーザーが自動参加（登録時 `add_to_default_room` が main と共にメンバー追加）。
- このルームにユーザーが投稿すると `send_message` が**別スレッドで `_generate_ai_reply`** を起動し、Anthropic Claude（`httpx` で `/v1/messages`）に直近20件を渡して返信を生成、**ボットユーザー**（`AI_BOT_UID=…a1` / 名前 `🤖 アシスタント` / 🤖）として messages に insert → 2秒ポーラーが拾う。
- ボットの user_id は users 表に無い固定 UUID（is_mine で他人扱い＝左側表示）。ボット投稿は send_message を通さないので無限ループしない。
- **secrets `[ai]`**（未設定ならボットは沈黙＝普通の部屋）:
  ```toml
  [ai]
  api_key = "sk-ant-..."        # Anthropic API キー（env ANTHROPIC_API_KEY でも可）
  model   = "claude-sonnet-4-6" # 任意。未指定はこの既定
  ```
- システムプロンプト `AI_SYSTEM_PROMPT` に danran の使い方要点を内蔵。画像の中身は見ない（テキストのみ）。バグ報告はこのルームに残るので管理者(まさと)も読める。

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
| 他人のアバター | `data-lp-sender="{name}"` ＋ `data-lp-avatar="{avatar}"` | → **全画面プロフィール**（`openUserProfile`：大アバター＋名前・背景ぼかし・✕/背景タップで閉じる）。将来 FaceTime ボタンを足す余地あり |

> **★ sendNav は併送方式（重要）**: `sendNav` は `pDoc._danranSend`（scan が毎回ライブ iframe に登録）**と**自分の `stSetValue` の**両方**で送る。`_danranSend` が一瞬古い（死んだ）iframe を指していると `go_room` 等が握り潰され、**起動直後にルームへ入れず一覧へ戻る**バグの原因だった。両送＋ Python の `_last_nav_ts` dedup で「確実に1回だけ」届く。

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

**★ JS から messages を insert する箇所（`handleImageUpload` の画像送信）は必ず `user_id: ME_UID` を含める**。これを忘れると user_id が null になり、削除クエリ（`user_id=eq.…`）にヒットせず「消した画像が消えない」バグになる（過去に発生・既存分は user_name から backfill 済み）。

---

## 開発時のデバッグ

```bash
# ローカル起動
uv run python run.py

# Supabase ログ確認
# → MCP ツール: mcp__supabase__get_logs

# コンポーネントキャッシュが怪しいとき
# → "danran_lp_v74" の数字をインクリメント（現在 v74）

# iOS PWA など画面にログを出せない環境のデバッグ
# → JS 側: 色付きの fixed div を一定時間表示する _dbg(color,msg) 方式、
#    Python 側: st.html の状態バッジ、を併用すると JS→Python のどこで
#    切れているか切り分けやすい（過去のスワイプ不具合はこれで特定）
```
