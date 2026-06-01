# ContentWave — SKILL.md
## 作業前に必ずこのファイルを読んでから実装すること

---

## 1. プロジェクト概要

| 項目 | 内容 |
|---|---|
| アプリ名 | ContentWave |
| 用途 | 女性KPOPアイドル情報を自動収集・AI投稿文生成・Threadsへ自動投稿するツール |
| アカウント | @kpopwave.daily（user_id: `27451984921065618`） |
| スタック | Python 3.x / Flask / SQLite / Bootstrap 5.3 / Windows 11 |
| DB ファイル | `instance/rock_metal.db`（SQLite） |
| 起動 | `python app.py`（venv有効化後） |
| 仮想環境 | `venv\` |

---

## 2. 主要ファイル構成

```
kpopwave-tool/
├── app.py               # Flaskルート・スケジューラ・API定義
├── database.py          # SQLAlchemyモデル定義（全テーブル）
├── config.py            # DB URI等の設定（SQLite: rock_metal.db）
├── summarizer.py        # Claude Haikuによる投稿文生成（2段階生成）
├── threads_api.py       # Threads API 投稿・動画投稿・カルーセル
├── rss_collector.py     # RSSフィード収集・女性KPOPキーワードフィルタ
├── comments.py          # Threadsコメント取得・返信・いいね
├── engagement_tracker.py# いいね数・リプライ数・リポスト数の取得
├── learning.py          # バズ投稿分析（BuzzPost → AI tips）
├── video_collector.py   # YouTube動画収集（yt-dlp）
├── get_token.py         # OAuthトークン取得ヘルパー
├── templates/
│   ├── base.html        # レイアウト・サイドバー・CSSカスタム変数
│   ├── index.html       # ダッシュボード
│   ├── pending.html     # 承認待ち画面（記事・動画タブ）
│   ├── queue.html       # 投稿キュー・投稿済みリスト
│   ├── schedule.html    # スケジュール管理
│   ├── comments.html    # コメント管理画面
│   ├── follow_candidates.html  # フォロー候補
│   ├── learning.html    # 学習・バズ投稿管理
│   └── settings.html    # 設定（APIキー・フィード等）
└── static/
    └── videos/          # ダウンロード済み動画ファイル置き場
```

---

## 3. DBテーブル一覧

### `articles` — 記事・動画レコード

| カラム | 型 | 説明 |
|---|---|---|
| id | INTEGER PK | |
| feed_source | VARCHAR(200) | フィード名（Soompi, aespa等） |
| title | VARCHAR(500) | 記事タイトル |
| url | VARCHAR(1000) UNIQUE | 元URL |
| published_at | DATETIME | 記事公開日時（UTC） |
| raw_content | TEXT | 取得した本文（最大3000文字） |
| summary | TEXT | AI生成済み投稿文 |
| status | VARCHAR(20) | **pending / queued / posted / rejected / failed** |
| thumbnail_url | VARCHAR(500) | サムネイル画像URL |
| scheduled_at | DATETIME | 投稿予定日時（UTC） |
| posted_at | DATETIME | 投稿完了日時（UTC） |
| threads_post_id | VARCHAR(200) | Threads投稿ID |
| error_message | TEXT | エラー詳細 |
| like_count / reply_count / repost_count / quote_count | INTEGER | エンゲージメント数値 |
| post_style | VARCHAR(20) | つぶやき型 / 情報型 / 体験談型 / バズり型 |
| image_urls | TEXT | JSON配列（複数画像URL） |
| content_type | VARCHAR(20) | `article`（デフォルト）または `video` |
| video_file_path | VARCHAR(500) | `videos/xxxx.mp4` 形式（staticディレクトリ相対） |

**ステータス遷移**:
```
pending → queued → posted
pending → rejected
queued  → failed
```
再投稿: posted/failed → pending（`/api/articles/<id>/requeue`）

### `settings` — キーバリュー設定

主なキー：`threads_access_token`, `threads_user_id`, `anthropic_api_key`,
`rss_feeds`（JSON）, `post_times`（`09:00,15:00,21:00`）,
`youtube_channels`（JSON）, `youtube_min_view_count`, `youtube_max_view_count`,
`test_mode`（`true`/`false`）, `learned_style_hints`

### `comments` — 受信コメント

| カラム | 説明 |
|---|---|
| id (STRING PK) | Threads コメントID |
| post_id | 元投稿のThreads ID |
| username | コメント投稿者名 |
| text | コメント本文 |
| is_read | 0=未読 / 1=既読 |
| is_replied | 0=未返信 / 1=返信済み |

### `follow_candidates` — フォロー候補

`username`, `display_name`, `followers_count`, `bio`,
`source`（curated/reddit/engagement）, `follow_status`, `priority`

### `buzz_posts` — バズ投稿（学習用）

`platform`, `url`, `content`, `likes`, `comments`, `shares`, `memo`, `analysis`（JSON）

---

## 4. デザイン（グリーン系パステル）

`base.html` の `:root` CSS変数：

```css
--accent:       #4a9e6b   /* メインカラー（緑） */
--accent-hover: #3a8558
--accent-light: #a8d5b5
--accent-soft:  #e8f5ed
--accent2:      #2e7d32   /* ダークグリーン */
--accent3:      #26A69A   /* ティール */
--peach:        #FFA726   /* アクセント（橙） */
--bg:           #f0f9f4   /* 背景 */
--surface:      #FFFFFF
--border:       #b8ddc8
--text:         #1a3a2a
--text-muted:   #5a8a6a
```

- フォント: Nunito（Google Fonts）
- カード: `border-radius: 18px`、`box-shadow: 0 3px 18px rgba(74,158,107,.06)`
- ボタン: `btn-accent`（グリーングラデーション）、`btn-sm` は `border-radius: 16px`
- バッジ色: pending=橙、queued=緑、posted=ティール、rejected=薄緑、failed=緑

---

## 5. Threads API 注意点（重要）

- Base URL: `https://graph.threads.net/v1.0`
- **`GET /{user_id}/replies` → 自分が書いた返信投稿を返す（他者のコメントではない）**
- **他者からのコメント取得は `GET /{post_id}/replies` で各投稿を個別に叩く**
- コメント取得フロー: `/{user_id}/threads`（投稿一覧）→ `/{post_id}/replies`（各投稿の返信） → `username == "kpopwave.daily"` を除外
- 投稿は2ステップ: `POST /{user_id}/threads`（コンテナ作成）→ `POST /{user_id}/threads_publish`
- 動画投稿: `media_type=VIDEO`, `video_url=<公開URL>`（ローカルファイル不可）
- カルーセル: 最大20枚、CAROUSEL_ITEM を先に作ってから CAROUSEL でまとめる
- 画像フィルタ: `gstatic.com`, `googleusercontent.com` 等は除外、64px以下も除外
- テストモード時は `threads_post_id` が `test_` プレフィックスになる

---

## 6. 実装済み機能一覧

| 機能 | ファイル | 備考 |
|---|---|---|
| RSSフィード収集 | `rss_collector.py` | 女性KPOPキーワードフィルタ + AI判定 |
| YouTube動画収集 | `video_collector.py` | yt-dlp、再生数フィルタあり |
| AI投稿文生成 | `summarizer.py` | Claude Haiku、2段階生成（生成→口語化） |
| Threads投稿 | `threads_api.py` | テキスト・画像カルーセル・動画 |
| 自動スケジューラ | `app.py` | APScheduler、`post_times`設定に従う |
| 承認待ち画面 | `pending.html` | 記事タブ・動画タブ切替、承認/却下/再生成 |
| 投稿キュー管理 | `queue.html` | ドラッグ並替、日時指定、再投稿ボタン |
| コメント管理 | `comments.py` + `comments.html` | 取得・既読・AI返信生成・手動返信 |
| エンゲージメント追跡 | `engagement_tracker.py` | いいね・リプライ・リポスト数 |
| バズ投稿学習 | `learning.py` | 高エンゲージメント投稿を分析→tips化 |
| フォロー候補管理 | `follow_candidates.html` | 優先度設定、フォロー済みマーク |
| 再投稿 | `/api/articles/<id>/requeue` | 記事=status→pending、動画=ファイル確認+再DL |
| スケジュール表示 | `schedule.html` | カレンダー形式 |

---

## 7. 投稿文ルール（AIプロンプトに適用）

### 絶対ルール
- **URLなし・ハッシュタグなし・絵文字なし**
- **文字数上限**: 動画=50文字、記事=150文字（超過時は最大3回再生成→強制切り詰め）
- **必ずフックで始める**（フック前に何も置かない）
- 「〜です」「〜ます」禁止 → 口語体
- 伝聞表現禁止:「〜とのこと」「記事によると」「〜と報じられている」
- グループ名またはメンバー名を必ず1つ以上含める
- 日本語のみ（グループ名・曲名はアルファベットOK）

### 禁止ワード（誤解・炎上リスク）
`興奮` / `止まらん` / `頭おかしい` / `事件` / `事故` / `心臓に悪い` / `狂ってる` / `これガチなんですけど` / `絶対バズる`

### ポジティブ表現（推奨）
`次元が違う` / `レベルが違う` / `完成度が高い` / `本当にうまい` / `かっこよすぎる` / `美しい` / `素敵すぎる`

### フックカテゴリ（`_KPOP_HOOKS` in summarizer.py）
毎回ランダムに1つ選択して渡す。7カテゴリ・計58種:
衝撃・驚き型 / こっそり・共有型 / 問いかけ型 / 保存促進型 / 感情爆発型 / 限定・希少型 / 独り言・つぶやき型

### ペルソナ
25歳日本人女性、KPOPオタク歴8年、推しはaespa、Threadsで情報発信中

---

## 8. コーディングルール

- **コメントは原則書かない**（WHYが非自明な場合のみ1行）
- **既存ファイルを編集**する（新ファイル作成は最後の手段）
- **必要な変更だけ**行う（リファクタリング・クリーンアップは不要）
- **Shell**: WindowsなのでPowerShell構文（`$env:VAR`、`&&` 不可）
- **テンプレートフィルタ**: `utc_to_jst`（+9h）、`format_comment_time`、`json_loads`
- **日時はすべてUTC保存、表示時にJSTに変換**する
- SQLiteスキーマ変更は `_migrate_db()` に `ALTER TABLE ADD COLUMN` を追記
- Bootstrap5のグリッドとユーティリティクラスを活用する
- JSはフォームsubmitよりfetch APIを使う（画面遷移なしのUX）

---

## 9. よくあるバグパターン

| バグ | 原因 | 正しい実装 |
|---|---|---|
| コメント管理に自分の投稿が表示される | `/{user_id}/replies` は自分の返信投稿を返す | `/{user_id}/threads` で投稿一覧 → `/{post_id}/replies` で受信コメントを取得 |
| 日時がズレる（9時間） | UTC/JST変換漏れ | DB保存はUTC、表示は `utc_to_jst` フィルタ経由 |
| 再投稿が動画しかできない | `requeue_article` に動画限定チェックがあった | 記事は `status="pending"` にリセットのみ、動画はファイル確認+再DL |
| 画像が投稿されない | Googleプロフィール画像・小サイズ画像が混入 | `_is_valid_image_url()` でフィルタリング必須 |
| 投稿文に「これガチなんですけど」が入る | 旧プロンプト例文の影響 | 禁止ワードリストに追加済み、例文も差し替え済み |
| フックがビジネス系・ノウハウ系になる | 旧 `_HOOK_PATTERNS_BY_TIME` の名残 | `_KPOP_HOOKS`（カテゴリ別）に全面差し替え済み |
| SQLiteでカラム追加エラー | `ALTER TABLE` の構文差異 | `_migrate_db()` パターンを使う（`ADD COLUMN` のみ可） |
| 投稿文が文字数超過する | Step2の再生成上限に達した | `BODY_MAX_RETRIES=3` 後は末尾切り詰め（`…`付加） |

---

## 10. RSSフィードとYouTubeチャンネル

### RSSフィード（英語）
Soompi / Koreaboo / Hellokpop / KpopPost / NME K-Pop / AsianJunkie / TheBiasList / KpopReviewed / SeoulBeats

### RSSフィード（日本語、`lang:ja`）
Kstyle（Google News経由）/ BARKS / Daebak Tokyo
→ キーワードフィルタースキップ、AIによる女性KPOP記事判定のみ

### YouTubeチャンネル（デフォルト）
aespa / NewJeans / BLACKPINK / TWICE / IVE / LE SSERAFIM / ILLIT / tripleS

---

## 11. 環境変数（.env）

```
SECRET_KEY=
ANTHROPIC_API_KEY=
THREADS_USER_ID=27451984921065618
THREADS_ACCESS_TOKEN=
META_APP_ID=
META_APP_SECRET=
YOUTUBE_API_KEY=
APP_BASE_URL=http://localhost:5000
```
