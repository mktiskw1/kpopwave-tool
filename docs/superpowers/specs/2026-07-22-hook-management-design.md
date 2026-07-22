# 投稿文フック管理機能 設計書

## 背景・目的

投稿文の冒頭フックは現在、KPOPアカウント専用の`_KPOP_HOOKS`辞書（58種類）からランダムに1つ選び、AIプロンプトに「このフレーズで必ず書き始めてください」と指示を混ぜ込む方式（`HOOK_SECTION`）で実現している。ガチャアカウント（`content_topic`設定済み）やAI無効時のルールベース生成には、フックの概念自体が存在しない。

管理画面からフックを追加・編集・削除でき、KPOP・ガチャ両アカウントで使え、順番にローテーションして使うフック管理機能を実装する。

## 現状の仕組み（前提）

- `summarizer.py`の`summarize_article()`には3つの生成経路がある:
  1. `content_topic`未設定（KPOP）・AI有効: Step1（初期生成）→Step2（口語化、`BODY_MAX_RETRIES=3`回まで再生成）の2段階生成。`HOOK_SECTION`をStep1プロンプトに注入（video/youtube/ranking/デフォルトの4パターン全てに存在）。Step2プロンプトには「1行目のフックフレーズは絶対に変えないこと」という指示がある
  2. `content_topic`設定済み（ガチャ）・AI有効: 汎用シンプルプロンプトで1段階生成。フックの概念なし
  3. AI無効（`ai_summary_enabled`設定がfalse）: `_title_only_summary()`でタイトルをそのまま使うルールベース生成。フックの概念なし
- `Article.account_id`から`ThreadsAccount.content_topic`を解決して経路2/3を判定する既存の仕組みがある
- サイドバーは`nav_active_account_id`（選択中アカウントID、`_selected_account_id()`が解決）をすべてのテンプレートに注入済み（`app.py`の`inject_globals()`）

## 方針

新規`Hook`テーブル（アカウントごとのフレーズ・使用順・最終使用日時）を追加し、管理画面から編集可能にする。投稿文生成側は、既存の`_KPOP_HOOKS`/`HOOK_SECTION`プロンプト注入方式を廃止し、生成済み本文に対してフックを機械的に連結する方式に置き換える。これにより3つの生成経路すべてで同じフック機構を使い回せる。

## データモデル

```python
class Hook(db.Model):
    __tablename__ = "hooks"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, nullable=False, index=True)
    phrase = db.Column(db.String(200), nullable=False)
    display_order = db.Column(db.Integer, nullable=False, default=0)
    last_used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
```

新規テーブルのため、`create_app()`内の`db.create_all()`で自動作成される。既存カラムへの`ALTER TABLE`は不要（`_migrate_db()`の対象外）。`account_id`に外部キー制約は付けない（`Article.account_id`など既存カラムと同じ緩い結合の慣習に合わせる）。

## ローテーションロジック

`last_used_at`昇順（SQLiteは`ORDER BY ASC`でNULLを先頭に並べるため、未使用のフックが常に最優先で選ばれる）→`display_order`昇順、で1件取得する。使用時に`last_used_at`を現在時刻（UTC）に更新する。この方式により、フックの追加・編集・削除があってもインデックスのズレや範囲外参照が起きず、常に「最も長く使われていないフック」が選ばれる。全フックを使い切ると再び最初（＝最も古く使われたフック）に戻るため、追加の「使い切り検知」ロジックは不要。

「現在のローテーション位置」は、管理画面でフック一覧をローテーション優先順（次に使うものが先頭）に並べ、先頭に「次に使う」バッジを表示することで可視化する。

## ルーティング・画面

| メソッド・パス | 内容 |
|---|---|
| `GET /hooks/<int:account_id>` | フック管理ページ。存在しない`account_id`は404。一覧をローテーション優先順で表示し、`last_used_at`（JST変換表示、未使用は「未使用」表記）と「次に使う」バッジを表示 |
| `POST /hooks/<int:account_id>/add` | フレーズを追加。`display_order`はそのアカウントの既存最大値+1を自動採番 |
| `POST /hooks/<int:id>/edit` | フレーズ本文を編集 |
| `POST /hooks/<int:id>/delete` | フックを削除 |

いずれもfetch APIで呼び出し、成功時は`location.reload()`（既存の`resummary()`等と同様の低摩擦パターン）。新規テンプレート`templates/hooks.html`を作成する。

## サイドバーナビゲーション

`templates/base.html`のデスクトップサイドバー・モバイル横スクロールナビバーの両方に「フック管理」リンクを追加する。リンク先は`url_for('hooks_page', account_id=nav_active_account_id)`とし、選択中アカウントに応じて自動的に`/hooks/1`または`/hooks/2`へ遷移する。

## `summarizer.py`の変更

### 削除するもの
- `_KPOP_HOOKS`辞書（58種類、7カテゴリ）
- `HOOK_SECTION`変数とその注入箇所（Step1プロンプトのvideo/youtube/ranking/デフォルトの4パターン全て）
- `summarize_article()`冒頭の`all_hooks = [...]; selected_hook = random.choice(all_hooks)`
- Step2プロンプト（video/デフォルトの2パターン）内の「1行目のフックフレーズは絶対に変えないこと。そのまま残す。」という指示行（対応するフック注入自体がなくなるため、この指示は意味をなさなくなる）

### 残すもの（変更しない）
- `STRUCTURE_SECTION`の「1行目：フックで引き込む」、`COMMON_RULES`の「━━ フック後の展開（必須） ━━」等の一般的なプロンプト文言。これらはAIへの一般的な文章指導として引き続き有効であり、削除する必要がない。変更範囲を最小化し、入念にチューニングされたプロンプト全体への影響を抑える

### 追加するもの
- `_get_next_hook(app, account_id) -> str | None`: 上記ローテーションロジックでフックを1件取得し`last_used_at`を更新して返す。該当アカウントにフックが1件もない場合は`None`
- `_attach_hook(hook: str | None, body: str, body_max: int) -> str`: `hook`が`None`でなければ`hook + body`を連結し、`body_max`を超えていれば安全側で切り詰める（既存の`…`付加切り詰めパターンを踏襲）。3つの生成経路すべてでこの関数を通す

### 統合方法
`account_id`は既存の「記事情報取得」ブロックで既に取得済みのため、その直後で1回だけ`_get_next_hook()`を呼び出し、以降の3つの生成経路（AI無効時のルールベース、ガチャ汎用AI、KPOP標準AI）それぞれの最終`post_text`確定箇所で`_attach_hook()`を通す。AI側の文字数プロンプト（`{body_max}文字以内`等）は変更しない（本文生成はフック分を差し引かずに従来通りの上限を狙わせ、連結後に超過した場合のみ`_attach_hook()`側で安全に切り詰める。プロンプト変更を最小限に抑えるための判断）。

## デフォルトフック（初回起動時に自動投入）

`_migrate_db()`（`app.py`）に、`Hook.query.count() == 0`の場合のみ以下を投入する処理を追加する（既存の`ThreadsAccount`初期データ投入と同じ冪等パターン）。

**KPOP用（account_id=1、`display_order` 0〜9）**:
1. 待って、これやばい。
2. え、この子なに。
3. これ知ってる人少ないと思う。
4. 布教させてください。
5. 好きにならない方が無理じゃない？
6. これ好きな人いる？
7. 保存推奨。
8. 語彙力消えた。
9. 今のうちに見て。
10. なんで知らなかったんだろ。

**ガチャ用（account_id=2、`display_order` 0〜9）**:
1. これマジで欲しい…
2. 新作きてる…！
3. 見つけた瞬間テンション上がった。
4. これは即回さなきゃ。
5. ガチャ勢は絶対チェックして。
6. 今回のクオリティやばい。
7. うわ、これ欲しすぎる。
8. 推しキャラのガチャ来た…！
9. この造形細かすぎない？
10. コンプリートしたくなる…

## 影響を受けないもの

- `database.py`の既存テーブル（新規テーブル追加のみ、既存カラム変更なし）
- `_STYLE_PROMPTS`、`EXPRESSIONS_*`、`_get_time_style_hint`、`_detect_group_name`など、フック以外のプロンプト構築ロジック
- `threads_api.py`（投稿文の中身が変わるだけで、投稿ロジック自体への影響はない）
- `templates/pending.html`の「要約を生成」ボタン自体の見た目・挙動（内部の生成結果が変わるのみ）

## テスト・検証方針

このプロジェクトにはpytest等のテストフレームワークがないため、実際にFlaskアプリを起動しての手動検証を行う:

1. `_get_next_hook`・`_attach_hook`の単体動作を`venv/Scripts/python.exe -c "..."`で確認（ローテーション順・切り詰め挙動）
2. マイグレーション実行後、KPOP・ガチャ両アカウントに10個ずつデフォルトフックが投入されることをDBクエリで確認
3. `/hooks/1`・`/hooks/2`双方の画面表示・追加・編集・削除をHTTPリクエスト（`app.test_client()`、`from app import app`を使う——後述の既知の落とし穴を参照）で確認
4. 実際に「要約を生成」を複数回実行し、生成された投稿文の先頭がフックのローテーション順と一致すること、`last_used_at`が更新されることを確認
5. サイドバーのフック管理リンクが、選択中アカウントに応じて`/hooks/1`または`/hooks/2`に切り替わることを確認

## 既知の環境上の注意点

- このプロジェクトの`app.py`は`app = create_app()`をモジュール読み込み時に実行し（`app.py:200`）、以降の`@app.route`はこのモジュール変数`app`に紐づく。`test_client()`での検証には必ず`from app import app`を使うこと（`from app import create_app; app = create_app()`は無関係な別インスタンスを作るため404になる）
- 開発サーバー（ポート5000）はセッションを跨いで古いプロセスが残り続けることがある。新たに`python app.py`/`python run.py`を起動しないこと

## スコープ外（今回やらないこと）

- フックのドラッグ&ドロップによる並べ替えUI（`display_order`は追加時の自動採番のみ。手動での並べ替えが必要になった場合は将来検討）
- フックのカテゴリ分類・タグ付け（既存の`_KPOP_HOOKS`が持っていた7カテゴリの概念は今回引き継がない。フラットなリストとして扱う）
- KPOP・ガチャ以外の3つ目以降のアカウントへの対応（`Hook.account_id`は任意の`account_id`を受け付ける設計のため、将来アカウントが増えてもコード変更なしで対応できる）
