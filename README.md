# 🏠 danran — 家族専用チャットアプリ

> **danran（団欒）** — 家族が集まって、なごやかに語り合うこと。

Streamlit + Supabase で作る、家族だけのプライベートチャットアプリです。

---

## スクリーンショット

```
┌─────────────────┬──────────────────────────────────────┐
│  🏠 danran      │  💬 家族みんな                        │
│                 │                                      │
│ 💬 チャットルーム │  👨 パパ                  4/1 08:30 │
│ ● 家族みんな    │  おはよう！今日は早めに帰れるよ        │
│   連絡事項      │                                      │
│   おでかけ計画  │       ママ 👩              4/1 08:45 │
│   料理・レシピ  │       了解！夕ご飯作って待ってるね     │
│                 │                                      │
│ 👤 発言者       │  👧 長女                  4/1 09:00 │
│ ● パパ          │  パパ早く帰ってきて〜！               │
│   ママ          │                                      │
│   長女          │ ┌──────────────────────────────┐    │
│   長男          │ │ 👨 パパ としてメッセージを入力… │    │
└─────────────────┴──────────────────────────────────────┘
```

---

## セットアップ

### 1. リポジトリのクローン & 依存インストール

```bash
git clone https://github.com/yourname/danran.git
cd danran
pip install -r requirements.txt
```

### 2. Supabase プロジェクトの作成

1. [supabase.com](https://supabase.com) でプロジェクトを新規作成
2. 下記の SQL でテーブルを作成（SQL Editor で実行）
3. Settings → API から `URL` と `anon key` をコピー

### 3. シークレットの設定

`.streamlit/secrets.toml` を開き、取得した値を入力してください。

```toml
[supabase]
url      = "https://xxxx.supabase.co"
anon_key = "eyJ..."
```

> ⚠️ `secrets.toml` は **絶対に Git に含めないでください。**

### 4. 起動

```bash
streamlit run app.py
```

---

## Supabase テーブル定義 (SQL)

Supabase の **SQL Editor** にペーストして実行してください。

```sql
-- ============================================================
-- messages テーブル
-- ============================================================
create table if not exists public.messages (
  id         uuid        primary key default gen_random_uuid(),
  room_name  text        not null,
  user_name  text        not null,
  content    text        not null,
  created_at timestamptz not null default now()
);

-- 古い順・ルームごとに高速取得するインデックス
create index if not exists messages_room_created_idx
  on public.messages (room_name, created_at asc);

-- ============================================================
-- Row Level Security (RLS) — まず全許可で動作確認する場合
-- ============================================================
alter table public.messages enable row level security;

-- 全ユーザーが SELECT/INSERT 可能なポリシー（家族内運用向け）
create policy "allow_all_select"
  on public.messages for select
  using (true);

create policy "allow_all_insert"
  on public.messages for insert
  with check (true);

-- ============================================================
-- (参考) リアルタイム更新を有効にする場合
-- Supabase Dashboard → Database → Replication から
-- messages テーブルを replication に追加してください。
-- ============================================================
```

---

## カスタマイズ

`app.py` の先頭にある定数を編集するだけで、ルーム名・メンバー名・絵文字を変更できます。

```python
ROOMS: list[str] = [
    "家族みんな",
    "連絡事項",
    "おでかけ計画",
    "料理・レシピ",
]

USERS: dict[str, str] = {
    "パパ": "👨",
    "ママ": "👩",
    "長女": "👧",
    "長男": "👦",
}
```

---

## 今後の拡張アイデア

- [ ] Supabase Realtime でリアルタイム受信（`st.experimental_fragment`）
- [ ] 画像・スタンプ送信 (`supabase.storage`)
- [ ] メッセージ既読機能
- [ ] プッシュ通知 (LINE Notify / Web Push)
- [ ] パスワード認証 (Supabase Auth)

---

## ライセンス

MIT
