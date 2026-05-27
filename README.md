# 🏠 danran — 家族専用チャットアプリ

> **danran（団欒）** — 家族が集まって、なごやかに語り合うこと。

Streamlit + Supabase で作る、家族だけのプライベートチャットアプリです。

---

## 機能一覧

| 機能 | 説明 |
|------|------|
| 📱 LINE 風 UI | 固定ヘッダー・吹き出し・アバター表示 |
| 🔐 名前 / 電話番号ログイン | パスワード認証 + 招待コードによる新規登録制限 |
| 🏠 複数チャットルーム | ルーム選択・未読バッジ表示 |
| ⚙️ ルーム編集 | ルーム名・アイコン（絵文字 or 写真）変更・削除 |
| 👤 プロフィール編集 | 表示名・アイコン（絵文字 or 写真）・電話番号変更 |
| 📷 写真送信 | カメラボタンから直接アップロード |
| 👍 絵文字リアクション | 長押しでリアクション選択・カウント表示 |
| 🗑️ メッセージ削除 | 自分のメッセージを長押しで削除 |
| 🔔 未読管理 | ルームごとの未読件数をバッジで表示 |
| ⏱️ リアルタイム更新 | 2秒ごとに新着メッセージを自動取得 |
| 💾 自動ログイン | localStorage にセッションを保存し次回自動ログイン |

---

## セットアップ

### 1. リポジトリのクローン & 依存インストール

```bash
git clone https://github.com/yourname/danran.git
cd danran
pip install -r requirements.txt
# または uv を使う場合
uv sync
```

### 2. Supabase プロジェクトの作成

1. [supabase.com](https://supabase.com) でプロジェクトを新規作成
2. 下記の SQL でテーブルを作成（SQL Editor で実行）
3. Settings → API から `URL` と `anon key` をコピー

### 3. シークレットの設定

`.streamlit/secrets.toml` を作成:

```toml
[supabase]
url      = "https://xxxx.supabase.co"
anon_key = "eyJ..."

[app]
# 新規メンバー登録に必要な招待コード
# 設定しない場合は誰でも登録可能（初期セットアップ時など）
register_key = "yourfamilycode"
```

> ⚠️ `secrets.toml` は **絶対に Git に含めないでください。**（`.gitignore` 済み）

### 4. Supabase Storage バケット作成

Supabase Dashboard → Storage から以下を作成（どちらも Public）:

- `avatars` — ユーザー・ルームのアイコン写真
- `chat-images` — チャット内の添付写真

### 5. 起動

```bash
streamlit run app.py
# または
uv run streamlit run app.py
```

---

## Supabase テーブル定義 (SQL)

Supabase の **SQL Editor** にペーストして実行してください。

```sql
-- ============================================================
-- users テーブル
-- ============================================================
CREATE TABLE IF NOT EXISTS public.users (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text        NOT NULL,
  avatar        text        NOT NULL DEFAULT '🙂',
  phone         text,
  password_hash text        NOT NULL,
  created_at    timestamptz DEFAULT now()
);

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
CREATE POLICY "users_select" ON public.users FOR SELECT USING (true);
CREATE POLICY "users_insert" ON public.users FOR INSERT WITH CHECK (true);
CREATE POLICY "users_update" ON public.users FOR UPDATE USING (true);

-- ============================================================
-- sessions テーブル
-- ============================================================
CREATE TABLE IF NOT EXISTS public.sessions (
  id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "sessions_all" ON public.sessions FOR ALL USING (true) WITH CHECK (true);

-- ============================================================
-- rooms テーブル
-- ============================================================
CREATE TABLE IF NOT EXISTS public.rooms (
  id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  name       text        NOT NULL,
  icon       text        NOT NULL DEFAULT '💬',
  created_at timestamptz DEFAULT now()
);

ALTER TABLE public.rooms ENABLE ROW LEVEL SECURITY;
CREATE POLICY "rooms_all" ON public.rooms FOR ALL USING (true) WITH CHECK (true);

-- デフォルトルーム
INSERT INTO public.rooms (name, icon) VALUES
  ('家族みんな',   '🏠'),
  ('連絡事項',     '📋'),
  ('おでかけ計画', '🗺️'),
  ('料理・レシピ', '🍳');

-- ============================================================
-- messages テーブル
-- ============================================================
CREATE TABLE IF NOT EXISTS public.messages (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  room_name   text        NOT NULL,
  user_id     uuid,
  user_name   text        NOT NULL,
  user_avatar text        NOT NULL DEFAULT '🙂',
  content     text        NOT NULL DEFAULT '',
  image_url   text,
  created_at  timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_room_created_idx
  ON public.messages (room_name, created_at ASC);

ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY "messages_select" ON public.messages FOR SELECT USING (true);
CREATE POLICY "messages_insert" ON public.messages FOR INSERT WITH CHECK (true);
CREATE POLICY "messages_update" ON public.messages FOR UPDATE USING (true);
CREATE POLICY "messages_delete" ON public.messages FOR DELETE USING (true);

-- ============================================================
-- reactions テーブル
-- ============================================================
CREATE TABLE IF NOT EXISTS public.reactions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id uuid NOT NULL REFERENCES public.messages(id) ON DELETE CASCADE,
  user_name  text NOT NULL,
  emoji      text NOT NULL,
  UNIQUE(message_id, user_name, emoji)
);

ALTER TABLE public.reactions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "reactions_all" ON public.reactions FOR ALL USING (true) WITH CHECK (true);

-- ============================================================
-- last_read テーブル（未読管理）
-- ============================================================
CREATE TABLE IF NOT EXISTS public.last_read (
  user_id   uuid NOT NULL,
  room_name text NOT NULL,
  read_at   timestamptz NOT NULL,
  PRIMARY KEY (user_id, room_name)
);

ALTER TABLE public.last_read ENABLE ROW LEVEL SECURITY;
CREATE POLICY "last_read_all" ON public.last_read FOR ALL USING (true) WITH CHECK (true);

-- ============================================================
-- pg_cron（無料枠の自動一時停止防止）
-- Dashboard → Database → Extensions で pg_cron を有効化後に実行
-- ============================================================
SELECT cron.schedule('danran-keep-alive', '0 9 */3 * *',
  'SELECT count(*) FROM public.messages');
```

---

## 使い方

### 初期メンバー登録

1. アプリを開く → 「＋ 新しいメンバーとして登録」
2. 招待コード（`secrets.toml` の `register_key`）を入力
3. お名前・アイコン（絵文字 or 写真）・電話番号（任意）・パスワードを入力して登録

### ログイン

- お名前 **または** 電話番号（ハイフンなし）でログイン
- 一度ログインしたデバイスは次回から自動ログイン

### チャット

- **メッセージ送信**: 下部の入力欄に入力して送信
- **写真送信**: 📷 ボタンをタップして写真を選択
- **リアクション**: 他人のメッセージを長押し → 絵文字を選択
- **メッセージ削除**: 自分のメッセージを長押し → 🗑️

### ルーム切り替え

- 左上の ＜ ボタン → ルーム一覧
- ルーム名をタップ → そのルームに移動
- ⚙️ ボタン → ルーム名・アイコンの編集、ルームの削除

### プロフィール編集

- ルーム一覧画面の下部にある自分のアイコン・名前をタップ
- または チャット画面で自分のアイコンをタップ

---

## ライセンス

MIT
