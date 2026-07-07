# アカウント切り替えUI 設計書

日付: 2026-07-07

## 背景・目的

マルチアカウント対応（`threads_accounts` テーブル、`ThreadsAccount` モデル）は既に実装済みだが、
どのアカウントを見ているかを切り替えるUIが管理画面全体に存在しない。

現状：
- `pending` / `queue` / `schedule` の各ページは `account_id` クエリパラメータで個別にスコープ可能だが、
  ページごとにボタン式の「アカウント選択タブ」を持ち、ページ遷移すると選択状態が引き継がれない
- ダッシュボード（`index.html`）はアカウントの概念がなく、常に全アカウント合算の数値を表示
- サイドバーなど共通レイアウトには「今どのアカウントを操作中か」を示す表示が一切ない

これをセッションベースの単一のグローバル切り替えUIに統一する。

## スコープ

対象ページ: ダッシュボード（`index`）、承認待ち（`pending`）、投稿キュー（`queue`）、スケジュール（`schedule`）

対象外（今回は変更しない）:
- コメント管理（`comments.html` / `comments.py`）— アカウント概念自体を持たないままとする
- `settings.html` のアカウント管理テーブル（追加・有効/無効切り替え）— 別機能として現状維持
- モバイル表示（768px以下の横スクロールナビバー）— 今回はデスクトップサイドバーのみ対応

## 設計

### 1. セッションベースのアカウント解決

`app.py` の `_selected_account_id()`（現 268行目付近）を拡張する。

解決順序:
1. `request.args.get("account_id")` が指定されていれば最優先（既存の挙動を維持、直接リンク対応）
2. なければ `session["active_account_id"]` を使う（存在し、かつ該当アカウントがまだ存在する場合）
3. どちらもなければレガシーデフォルト（`get_active_account(app)` = 最古のアクティブアカウント）

いずれの経路で解決しても、解決した `account_id` を `session["active_account_id"]` に書き戻し、
以降のリクエストでセッションから引き継がれるようにする。

セッション上のアカウントIDが指す `ThreadsAccount` が既に存在しない（削除済み）場合は、
セッション値を破棄してレガシーデフォルトにフォールバックする。

### 2. アカウント切り替えルート

新規ルートを追加:

```python
@app.route("/accounts/switch/<int:id>")
def switch_account(id):
    account = ThreadsAccount.query.get_or_404(id)
    session["active_account_id"] = account.id
    return redirect(request.referrer or url_for("index"))
```

- GETのみ。JSは使わず、通常の `<a href="...">` リンクから呼び出す
- 遷移元ページ（`request.referrer`）に戻ることで、「今見ていたページのまま、アカウントだけ切り替わる」体験にする

### 3. サイドバーのドロップダウンUI（デスクトップのみ）

`app.py` の既存 `inject_globals()` context processor（198行目付近）に以下を追加し、
全ページのテンプレートから個別に渡さなくても参照できるようにする:

```python
"nav_accounts": ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all(),
"nav_active_account_id": _selected_account_id(),
```

`base.html` のサイドバー、ブランドロゴ直下・ナビリンク一覧の上に、Bootstrapドロップダウンを追加する:

- ボタン表示: 現在選択中のアカウントラベル（例: `👤 kpopwave.daily ▾`）
- 展開時: `nav_accounts` を列挙し、各項目は `<a href="{{ url_for('switch_account', id=acc.id) }}">`
- 現在選択中のアカウント（`acc.id == nav_active_account_id`）にはチェックマーク／ハイライトを付与
- アカウントが1件以下の場合はドロップダウンを表示しない（切り替える意味がないため）

### 4. ダッシュボードのアカウント絞り込み

`index()` ルート（255-262行目）を修正し、`pending`/`queue` と同じパターンで
`_selected_account_id()` + `_account_query_scope()` を使って以下を選択中アカウントに絞り込む:

- 統計値（`pending`/`queued`/`posted`/`rejected`/`failed` の件数）
- 最近の記事一覧（`recent`）

レガシーアカウントが選択中の場合は、既存ルールと同じく `account_id IS NULL` の記事も含める。

### 5. 既存の「アカウント選択タブ」の削除

以下のテンプレートから、ページごとのボタン式アカウント選択タブを削除する:
- `pending.html`（11-19行目付近の `{% if accounts and accounts|length > 1 %}` ブロック）
- `queue.html`（同様のブロック）
- `schedule.html`（同様のブロック）

Python側（`app.py` の `pending()` / `queue()` / `schedule()` ルート）は
既に `_selected_account_id()` を呼んでいるだけなので、アカウント絞り込みロジック自体の変更は不要
（セッション対応は自動的に効く）。

タブ削除により `accounts` / `active_account_id` をテンプレートに渡す唯一の目的がなくなるため、
実装時に各テンプレート（`pending.html` / `queue.html` / `schedule.html`）内でこの2変数が
タブ以外に使われていないことを確認したうえで、ルート側の `render_template()` 呼び出しからも
該当キーワード引数を削除する（未使用コードを残さない）。

## テスト方針

- 手動確認: 複数アカウントが存在する状態で、サイドバーのドロップダウンからアカウントを切り替え、
  ダッシュボード・承認待ち・投稿キュー・スケジュールの各ページで正しく絞り込まれること、
  ページ間を移動しても選択状態が引き継がれることを確認する
- アカウントが1件のみの場合、ドロップダウンが表示されない、または実質何も変わらないことを確認
- セッションに存在しないアカウントIDが入っていた場合（アカウント削除後など）にエラーにならず
  レガシーデフォルトにフォールバックすることを確認
