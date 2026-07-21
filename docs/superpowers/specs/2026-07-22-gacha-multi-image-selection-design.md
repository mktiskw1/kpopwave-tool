# ガチャパラ記事の複数画像取得・選択機能 設計書

## 背景・目的

ガチャパラ（gachapara.jp）のRSS収集は現在、記事ページのOGP画像（`og:image`）1枚のみを取得している（[2026-07-21-gacha-rss-image-and-filters-design.md](2026-07-21-gacha-rss-image-and-filters-design.md)で実装済み）。ガチャパラの記事は複数の商品（カプセルトイ）を1記事で紹介することが多く、OGP画像だけでは商品の一部しか伝わらない。

記事本文内の商品画像も複数枚取得し、承認待ち画面でどの画像を投稿に使うか選択できるようにする。

## 現状の仕組み（前提）

- `rss_collector.py`の`collect_articles()`は、`content_topic`設定済みアカウント（現状ガチャ沼の住人のみ）の承認済み記事に対して`_fetch_ogp_thumbnail(url)`を呼び、OGP画像1枚を`Article.thumbnail_url`に保存する
- `app.py`の`_build_image_list(thumbnail_url, image_urls)`が`thumbnail_url`→`image_urls`の順で画像リストを構築し、承認待ち画面（`pending.html`）・投稿キュー（`queue.html`）の画像プレビュー、および`threads_api.py`のカルーセル投稿画像リストとして共通利用されている
- KPOPアカウントの記事は、要約生成時（`summarizer.py`の`_fetch_article_page`→`_extract_images_from_html`）に最大4枚の画像を取得し`image_urls`に保存する（今回のスコープ外、変更しない）
- 承認待ち画面には現在、画像を「表示するだけ」の機能しかなく、選択・除外の仕組みは存在しない。承認すると`thumbnail_url`+`image_urls`の全件がそのままカルーセル投稿される

## 方針

1. ガチャパラのRSS収集時、OGP画像に加えて記事本文内の商品画像を最大6枚（合計最大7枚）取得する。取得対象は`gachapara.jp`ドメインの画像に限定し、広告・バナー・アイコン等をキーワードで除外する
2. 承認待ち画面の画像プレビューにチェックボックスを追加し、投稿に使う画像を選択できるようにする。選択変更は即座に自動保存する
3. 選択UIは全アカウント共通のテンプレート部分に実装する（KPOP記事の複数画像でも同じ恩恵を受けられる）。スクレイピング側の変更（ドメイン制限・広告除外）はガチャアカウント専用のまま

## データモデル

スキーマ変更なし。既存の`Article.thumbnail_url`（先頭画像）・`Article.image_urls`（JSON配列、残りの画像）にそのまま保存する。画像選択の保存も同じ2フィールドへの上書きで実現する（新規カラムは追加しない）。

## 変更内容

### 1. `rss_collector.py`: 記事本文からの複数画像取得

既存の`_fetch_ogp_thumbnail(url) -> str`を、`_fetch_article_images(url, max_body_images=6) -> list[str]`に置き換える（OGP画像取得の責務を新関数が吸収するため、旧関数は削除する）。

- `requests.get()`で記事ページのHTMLを1回取得（OGP画像・本文画像を同一レスポンスから抽出し、追加のHTTPリクエストを発生させない）
- OGP画像（既存の`_OGP_IMAGE_RE`で抽出）を先頭候補とする
- `<article>`または`<main>`タグ内の`<img src="...">`を正規表現で抽出し、`urljoin(url, img_src)`で絶対URL化（本文内画像は相対パスのことが多いため）
- 新規`_is_gachapara_content_image(url: str) -> bool`でフィルタ:
  - `http`で始まらないものは除外
  - `urlparse(url).netloc`が`gachapara.jp`またはそのサブドメインでないものは除外（広告ネットワーク・CDN経由の画像は通常別ドメインのため、この時点で大半の広告が除外される）
  - URLに`banner`, `/ads/`, `ad-`, `-ad.`, `sponsor`, `widget`, `spacer`, `btn-`, `share`, `logo`, `icon`, `avatar`, `sns`, `wp-content/themes`, `.svg`, `1x1`, `pixel`, `tracking`のいずれかを含むものは除外（同一ドメイン内のサイトロゴ・共有ボタン・テーマアセット等を除外）
- 重複除去した上で最大7枚（OGP1枚+本文6枚）を返す。取得失敗時は空リストを返す

`collect_articles()`内、AI判定通過後の`content_topic`設定済みアカウントの候補について、この関数を呼び出す。返却されたリストの先頭を`Article.thumbnail_url`、残り（あれば）を`Article.image_urls`（JSON配列）に保存する。

### 2. `app.py`: 画像選択の保存エンドポイント

新規ルート`POST /articles/<int:id>/update-images`を追加する。

- リクエストボディ（JSON）: `{"urls": ["https://...", "https://...", ...]}`（選択中の画像URLを表示順に並べたもの）
- `urls`の先頭を`Article.thumbnail_url`に、残りを`Article.image_urls`（JSON配列文字列）に上書き保存する
- `urls`が空配列の場合は`thumbnail_url=None`, `image_urls=None`（画像なしのテキスト投稿として扱われる。既存の投稿ロジックは画像0枚のケースに対応済み）
- レスポンス: `{"ok": true, "count": <保存件数>}`

投稿時の画像取得（`threads_api.py`のカルーセル構築ロジック）・他画面のプレビュー（`queue.html`）は既存通り`thumbnail_url`/`image_urls`を読むだけなので、この2フィールドへの保存だけで選択結果が自動的に反映される。追加の変更は不要。

### 3. `templates/pending.html`: 選択UI（全アカウント共通）

既存の画像プレビュー部分（`images_map`を使う横スクロールギャラリー、記事投稿タブ全体で共通利用）に、各画像へチェックボックスをオーバーレイ表示する。

- デフォルトで全画像チェック済み（現状の「全件投稿される」動作を初期状態として維持）
- チェックボックスの状態が変わったら、その記事の現在チェック済み画像URLをDOM順に集めて`POST /articles/<id>/update-images`にfetch送信（自動保存、保存ボタンなし）
- 保存失敗時のみアラート表示（成功時は無音、既存の他のチェックボックス系UIと同様の低摩擦UX）
- チェックボックスのクエリスコープは記事ごとの画像プレビューコンテナに限定し、同カード内の一括削除用チェックボックス（`name="ids"`）と干渉しないようにする

`templates/queue.html`は変更しない（承認前の`pending.html`で選択を確定させる想定のため、投稿キューでは選択後の状態を読み取り専用で表示する既存動作のまま）。

## 影響を受けないもの

- KPOPアカウントの画像取得ロジック（`summarizer.py`の`_extract_images_from_html`、要約生成時の4枚取得）— スクレイピング側は変更しない
- `database.py`（スキーマ変更なし）
- `threads_api.py`（既存の`thumbnail_url`/`image_urls`読み取りロジックがそのまま選択結果を反映するため変更不要）
- `templates/queue.html`（表示のみ、既存のまま）

## テスト・検証方針

このプロジェクトにはpytest等のテストフレームワークがないため、実際にFlaskアプリを起動しての手動検証を行う:

1. `_is_gachapara_content_image`・`_fetch_article_images`の単体動作を`venv/Scripts/python.exe -c "..."`で確認（ネットワーク接続時は実際のガチャパラ記事URLで、接続不可時は正規表現・フィルタ判定のみオフラインで確認）
2. ガチャパラフィードに対して`collect_articles()`を実行し、`thumbnail_url`・`image_urls`に複数枚の`gachapara.jp`ドメイン画像が保存されることをDBクエリで確認
3. 承認待ち画面でチェックボックスの表示・トグル操作・保存後のDB反映をHTTPリクエストで確認

## スコープ外（今回やらないこと）

- KPOPアカウント側のスクレイピング強化（本文画像取得の拡張、ドメイン制限等）— 選択UI自体はKPOP記事にも適用されるが、取得ロジックは変更しない
- 画像の並び替え（ドラッグ&ドロップ等）— チェックボックスによる選択/除外のみ
- 一度チェックを外した画像を再度選択可能にする「候補プール」の保持（承認待ち画面のリロード時点で表示されているものが選択可能な全候補であり、DB上は選択結果のみを保持する）
