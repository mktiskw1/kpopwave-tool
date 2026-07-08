# テキスト投稿機能 設計書

日付: 2026-07-08

## 背景・目的

現状、投稿は「RSS/YouTube収集 → AI要約生成 → 承認 → キュー → 自動投稿」という記事・動画向けのフローしかない。
田中（仮）アカウント（`threads_accounts.id=2`）向けに、収集元の記事や動画に紐付かない、管理者が自由に書いたテキストのみの投稿を作成できるようにしたい。

## スコープ

対象:
- 管理画面に新規ページ「テキスト投稿」を追加し、本文入力・アカウント選択・即時投稿/キュー追加を行える
- キューに追加した場合、既存の投稿キュー（`queue.html`・スケジューラ）にそのまま乗る
- 動画なしのテキストのみ投稿（画像も添付しない）

対象外（今回は変更しない）:
- 画像添付・カルーセル投稿（テキストのみ）
- AIによる投稿文自動生成（本文は手入力のみ）
- `pending.html` の承認フロー（テキスト投稿は承認待ちを経由せず直接 queued/posted になる）
- 動画投稿の仕組み（`_post_video` 等）への変更

## 設計

### 1. データモデル（変更なし・既存カラムを再利用）

新規テーブル・カラムは追加しない。既存の `Article` モデルに以下の値でレコードを作成する:

| カラム | 値 |
|---|---|
| `content_type` | `"text"`（新しい値。既存は `"article"` / `"video"`） |
| `summary` | 投稿本文そのもの |
| `title` | 本文冒頭30文字（30文字超なら `…` を付加） |
| `url` | `text-post:{uuid4().hex}`（一意制約 `url` 回避用の合成URL。実URLではない） |
| `feed_source` | `"テキスト投稿"`（キュー画面のバッジに表示される） |
| `account_id` | フォームで選択されたアカウントのID |
| `status` | `"queued"`（即時投稿・キュー追加とも一旦 queued にしてから分岐する） |
| `thumbnail_url` / `image_urls` / `video_file_path` | すべて `None`（画像・動画を一切添付しない） |

`content_type="text"` は既存の `threads_api.post_to_threads()` の分岐で `content_type != "video"` かつ `images` が空リストになるため、
自動的に `_post_text_only()` が呼ばれる。この分岐ロジック自体は変更しない。

### 2. 新規ルート（`app.py`）

```python
@app.route("/text-post", methods=["GET", "POST"])
def text_post():
    ...
```

`schedule()` と同じ「1ルートでGET表示・POST処理を両方受ける」パターンを踏襲する。

**GET**: `ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all()` でアカウント一覧を取得し、
`account_label == "田中（仮）"` のアカウントをデフォルト選択候補としてテンプレートに渡す（見つからなければ先頭のアカウント）。

**POST**: フォームから `body`（本文）・`account_id`・`action`（`"post_now"` または `"queue"`）を受け取る。

バリデーション（サーバー側、失敗時は `flash` + `/text-post` へリダイレクト）:
- `body` が空でないこと
- `len(body) <= 500`（Threads APIの投稿文字数上限。`summarizer.py` の `THREADS_MAX` と同じ値をこのルートでも定数として参照する）
- `account_id` が `ThreadsAccount.query.get(account_id)` で存在し `is_active=True` であること

バリデーション通過後、上記「1. データモデル」の内容で `Article` を新規作成し `db.session.commit()`。

- `action == "queue"`: `flash("キューに追加しました", "success")` → `redirect(url_for("queue"))`
- `action == "post_now"`:
  - `test_mode = Setting.get("test_mode", "true").lower() == "true"`
  - `threads_api.post_to_threads(app, article.id, test_mode=test_mode, account_id=account_id)` を同期呼び出し
  - 結果メッセージを `flash(msg, "success" if success else "danger")` → `redirect(url_for("queue"))`

### 3. 新規テンプレート `templates/text_post.html`

`base.html` を継承。構成要素:
- `<textarea name="body">`：本文入力。JSで文字数カウンターを表示し、500文字を超えたら赤字表示＋両方の送信ボタンを `disabled` にする
- `<select name="account_id">`：`is_active=True` のアカウントを列挙。デフォルト選択は「田中（仮）」（無ければ先頭アカウント）
- 送信ボタン2つ。`<button type="submit" name="action" value="post_now">今すぐ投稿</button>` と `<button type="submit" name="action" value="queue">キューに追加</button>`（既存の `post_now`/`unqueue`/`retry` ボタンと同様、プレーンな `<form method="post">` submitでページ遷移する方式に揃える。fetch化はしない）
- 「今すぐ投稿」ボタンには既存の `post_now` フォームと同様に `onsubmit="return confirm('今すぐ投稿しますか？')"` を付ける

### 4. ナビゲーション（`base.html`）

デスクトップサイドバーとモバイル横スクロールナビバーの両方に「テキスト投稿」リンクを追加する（既存の「スケジュール」リンクの近くに配置）。アイコンは `bi-pencil-square` を使う。

### 5. `queue.html` への最小変更

タイトル表示部分（キュー中カード・投稿済みリストの2箇所）で、現在は常に `<a href="{{ a.url }}">{{ a.title }}</a>` としてリンク化しているが、
`content_type == 'text'` の場合はリンクを外しプレーンテキスト表示にする（`url` が合成値でクリックしても無意味なため）。

```jinja
{% if (a.content_type or 'article') == 'text' %}
  <span style="color:var(--text);font-weight:700">{{ a.title }}</span>
{% else %}
  <a href="{{ a.url }}" target="_blank" ...>{{ a.title }}</a>
{% endif %}
```

画像・動画プレビュー領域（`images_map` / `video_file_path` 分岐）は変更不要。テキスト投稿は `thumbnail_url`/`image_urls`/`video_file_path` が全て `None` のため、既存ロジックのままで何も表示されない。

### 6. 既存機能への影響確認（変更不要と判断した箇所）

- **`threads_api.post_to_threads()`**: `content_type == "video"` 分岐にヒットせず、`images` が空なので `_post_text_only()` が呼ばれる。変更不要
- **`requeue_article`（再投稿）**: `(content_type or "article") != "video"` の分岐で「記事」扱いとなり、`status="queued"` にリセットするだけの既存ロジックがそのまま動く。変更不要
- **`scheduler.py`**: `status="queued"` の `Article` を `content_type` に関係なく拾って投稿対象にする。変更不要
- **`pending.html`**: テキスト投稿は `status="pending"` を経由しないため表示されない。変更不要
- **`summarizer.py`**: テキスト投稿はAI生成を使わないため呼び出されない。変更不要

## テスト方針

- 手動確認: `/text-post` からテスト本文（1）「キューに追加」→ `/queue` の待機中に表示され、日時指定・並び替え・今すぐ投稿ボタンが通常記事と同様に動作すること
- 手動確認: 「今すぐ投稿」→ テストモードON時は `posted` になり `threads_post_id` が `test_` プレフィックスになること。テストモードOFF時は実際にThreadsへテキスト投稿されること
- 手動確認: 500文字超の本文を入力した場合、両方の送信ボタンが無効化されること。またサーバー側でも500文字超のPOSTは弾かれること
- 手動確認: アカウント選択で「田中（仮）」「kpopwave.daily」を切り替えて投稿し、それぞれ正しい `account_id` で `Article` が作成され、正しいThreadsアカウントの認証情報で投稿されること
- 手動確認: キュー画面・投稿済み一覧でテキスト投稿のタイトルがリンクなしで表示され、画像/動画プレビューが表示されないこと
- 手動確認: 投稿済み/失敗したテキスト投稿の「再投稿」ボタンが正常に動作すること
