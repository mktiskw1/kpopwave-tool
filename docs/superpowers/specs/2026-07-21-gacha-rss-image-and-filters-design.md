# ガチャパラRSS収集: 画像取得・除外フィルタ強化 設計書

## 背景・目的

`account_id=2`（ガチャ沼の住人）向けのガチャパラRSS収集（[2026-07-19-gacha-rss-collection-design.md](2026-07-19-gacha-rss-collection-design.md)で実装済み）は現在タイトル・URL・本文のみを取得しており、記事画像を持たない。承認待ち画面ではKPOP記事・YouTube動画と異なり画像プレビューが出ない。

また、ガチャパラのRSSには以下のカプセルトイ記事の関心から外れるカテゴリが混入する：
- 店舗オープン情報（「オープン」を含む記事）
- ゲームセンター関連
- イベント・展示会情報
- カプセルトイ以外のニュース

これらを収集段階で除外し、かつ収集した記事にOGP画像を付与して承認待ち画面で即座にプレビューできるようにする。

## 現状の仕組み（前提）

- `rss_collector.py`の`collect_articles()`は、フィードに紐づく`ThreadsAccount.content_topic`が設定されている場合（＝非KPOPアカウント）、KPOP専用キーワードフィルタ（`_check_excluded`/`_check_female_kpop`）を丸ごとスキップし、Claude Haikuによる汎用AI関連度判定（`_ai_judge_titles`の`topic_label`分岐）のみで記事を選別する
- `Article.thumbnail_url`列は既存（`database.py`）。承認待ち画面（`pending.html`）・投稿キュー（`queue.html`）の画像プレビューは`app.py`の`_build_image_list(thumbnail_url, image_urls)`が`thumbnail_url`→`image_urls`の順で構築し、`images_map`としてテンプレートに渡す仕組みが既に存在する（YouTube動画収集で使用中）
- KPOPアカウントの記事は、RSS収集時点では画像を持たず、`summarizer.py`の`_fetch_article_page()`が「要約を生成」操作時に初めてOGP画像・本文画像を取得し`thumbnail_url`/`image_urls`にセットする

## 方針

`content_topic`が設定されたアカウント（現状ガチャ沼の住人のみ）に限定して、`rss_collector.py`の収集処理内で変更を行う。KPOPアカウントの収集ロジック・画像取得タイミングには一切手を加えない。

## 変更内容（`rss_collector.py`のみ）

### 1. OGP画像取得

新規関数`_fetch_ogp_thumbnail(url: str) -> str`を追加する。

- `requests.get()`で記事ページのHTMLを取得（タイムアウト10秒、失敗時は空文字を返しログに警告を出す）
- `<meta property="og:image">`（`og:image:secure_url`含む）をタイトル同様の正規表現で抽出する（`app.py`の`_fetch_article_info`・`summarizer.py`の`_extract_images_from_html`と同一パターンを踏襲、コードの重複は許容する — 3ファイルとも既存の独立したOGP抽出実装を持つ既存パターンに合わせる）
- 抽出したURLが`http`で始まらない場合は空文字を返す

`collect_articles()`内、AI判定（フィルタ4）通過後・`Article`作成前に、`content_topic`が設定されているフィードの承認済み候補についてのみこの関数を呼び出し、結果を`Article.thumbnail_url`にセットする。KPOPフィードの候補には呼び出さない（既存の要約生成時取得フローを維持）。

HTTP取得はDBセッション（`app.app_context()`ブロック）の外側で行い、取得結果を`idx`をキーにした辞書に保持してから`Article`作成ループ内で参照する。

### 2. 除外フィルタ（ハイブリッド方式）

**a. ハードキーワード除外**（新規関数`_check_content_topic_excluded(title, content) -> bool`）

`content_topic`が設定されたアカウント向けの除外キーワードリストを新設する:

```python
CONTENT_TOPIC_EXCLUDE_KEYWORDS = ["オープン", "ゲームセンター", "ゲーセン"]
```

タイトル・本文いずれかにこれらの語を含む記事は、既存の`_check_excluded`と同様の仕組みでフィルタ2相当として除外する（「除外(KW)」カウンタに計上）。

`collect_articles()`のフィルタ分岐を以下のように変更する:

```python
if content_topic:
    if _check_content_topic_excluded(title, plain_content):
        skipped_kw += 1
        continue
elif not is_ja:
    if _check_excluded(title, plain_content):
        skipped_kw += 1
        continue
    if not _check_female_kpop(title, plain_content):
        skipped_kw += 1
        continue
```

（現状は`if not content_topic and not is_ja:`の単一分岐だったものを、`content_topic`有無で完全に分岐させる）

**b. AI判定プロンプトの強化**

`_ai_judge_titles()`の汎用分岐（`topic_label`が「女性KPOPアイドル」以外の場合）に、除外指示を追記する:

```python
prompt = (
    f"以下の記事タイトルのうち、{topic_label}に関するニュースを選び、"
    "番号をカンマ区切りで返してください。\n"
    f"除外: イベント・展示会情報のみのもの、{topic_label}と直接関係のないニュース\n\n"
    f"{numbered}\n\n"
    "回答は番号のみ（例: 1,3,5）。対象なし→「なし」"
)
```

これにより「イベント・展示会情報」「カプセルトイ以外のニュース」の判定はAIの意味理解に委ねる（タイトルのみからの判定のため、確実性はハードキーワード除外より劣るが、パターン化しにくいカテゴリのため許容する）。

## 影響を受けないもの

- KPOPアカウント（`content_topic`未設定）のRSS収集ロジック・画像取得タイミング・投稿文生成
- `database.py`（スキーマ変更なし。既存の`thumbnail_url`列を使うのみ）
- `app.py`・`templates/pending.html`・`templates/queue.html`（`_build_image_list`・`images_map`の仕組みは既存のまま流用できるため変更不要）

## テスト・検証方針

このプロジェクトにはpytest等のテストフレームワークがないため、実際にFlaskアプリを起動しての手動検証を行う:

1. `_check_content_topic_excluded`・`_fetch_ogp_thumbnail`の単体動作を`venv/Scripts/python.exe -c "..."`で確認
2. ガチャパラフィード（`https://gachapara.jp/feed/`）に対して`collect_articles()`を実行し、除外キーワードを含む記事が保存されないこと、保存された記事に`thumbnail_url`が入っていることをDBクエリで確認
3. 承認待ち画面（`/pending?account_id=2&tab=rss`または`tab=all`）でガチャ記事に画像プレビューが表示されることをHTTPレスポンスで確認

## スコープ外（今回やらないこと）

- KPOPアカウントの画像取得タイミングの変更（RSS収集時への前倒し）
- 除外キーワードリストの設定画面からの編集UI（現状ハードコード。将来別のcontent_topicアカウントが増えた際に汎用化を検討）
- YouTube動画収集（`video_collector.py`）への影響（対象外）
