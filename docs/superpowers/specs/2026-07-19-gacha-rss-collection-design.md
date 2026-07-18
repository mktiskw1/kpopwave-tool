# ガチャ沼の住人アカウント向けRSS収集 設計書

## 背景・目的

`account_id=2`（アカウント名: 「ガチャ沼の住人」、旧ラベル「田中（仮）」）向けに、ガチャガチャ・カプセルトイ専門サイト「ガチャパラ」のRSSフィード（`https://gachapara.jp/feed/`）を収集源として追加する。KPOPアカウント（account_id=1、`@kpopwave.daily`）と同じRSS収集→承認待ち→投稿の流れで使えるようにする。

## 現状の制約（調査で判明）

1. `rss_feeds`設定（`Setting`テーブル）はアカウントに紐付かない単一の全体リストで、収集記事にも`account_id`が一切セットされない（暗黙的に全件が凡例アカウント＝account_id=1扱いになる）
2. `rss_collector.py`のキーワードフィルタ（`_check_female_kpop`/`EXCLUDE_KEYWORDS`）とAI関連度判定プロンプトは「女性KPOPアイドル記事かどうか」の判定に固定されており、ガチャ関連記事はほぼ確実に弾かれる
3. `summarizer.py`の投稿文生成プロンプト（ペルソナ・フック集・NGワード・「グループ名またはメンバー名を必ず1つ含める」ルール）はKPOP専用に固定されている。このままガチャ記事を流すとKPOPペルソナでガチャ記事を無理に書こうとして破綻する
4. 設定画面のフィード保存フォーム（`/settings` POST）は`feed_name`/`feed_url`のみを読み取ってフィードリストを再構築するため、既存フィードに設定されている`lang`フィールドが保存のたびに失われる（既存の抜け漏れ）

表示側（承認待ち・投稿キュー・ダッシュボード）は`_selected_account_id()` / `_account_query_scope()`で既にアカウントスコープ済みのため、そのまま利用できる。

## 方針

ユーザー判断により、投稿文生成は**汎用シンプルプロンプトでMVP**とする（KPOP専用のフック集・固有名詞強制ルールは使わず、記事タイトル・本文を自然な口語体の一言に整える程度）。将来的に作り込みが必要になった場合は、KPOP専用プロンプトと同構造の専用ペルソナに差し替える余地を残す。

## データモデル

`ThreadsAccount`に列を追加（`database.py`、`_migrate_db()`でALTER TABLE ADD COLUMN）。

| カラム | 型 | 説明 |
|---|---|---|
| content_topic | VARCHAR(200), nullable | 非KPOPアカウント用のコンテンツトピック説明。設定されている場合、RSS関連度判定・投稿文生成の両方でKPOP専用ロジックの代わりに汎用ロジックを使う判定フラグ兼プロンプト材料になる |

- account_id=1（kpopwave.daily）: `content_topic`は空のまま → 既存のKPOP専用フローを継続
- account_id=2（ガチャ沼の住人）: `content_topic = "ガチャガチャ・カプセルトイに関する記事"`をデフォルト値として初期投入。設定画面から編集可能にする

## フィード設定（`rss_feeds` Setting値のJSON構造）

各フィードエントリに`account_id`を追加する。

```json
{"name": "ガチャパラ", "url": "https://gachapara.jp/feed/", "account_id": 2}
```

`account_id`未指定の既存フィードは後方互換のためaccount_id=1（レガシーアカウント）として扱う。

`/settings` POSTハンドラ（`app.py`）のフィード再構築ロジックを、`account_id`と既存の`lang`の両方を保持するよう修正する（現状`lang`が保存のたびに消える抜け漏れも同じ箇所の修正でまとめて直す）。

## RSS収集（`rss_collector.py`）

- フィードごとに紐づくアカウントの`content_topic`を`ThreadsAccount`から解決する
- `content_topic`が設定されている場合：
  - 女性KPOPキーワードフィルタ（`_check_female_kpop`/`_check_excluded`）を丸ごとスキップする（既存の`is_ja`バイパスと同様の仕組みを流用。ガチャパラは単一トピックの専門サイトのため、キーワード辞書構築は不要と判断）
  - AI関連度判定プロンプトは`content_topic`の文言を使う汎用版に差し替える（「女性KPOPアイドル」の代わりに「{content_topic}」を埋め込む）
- `content_topic`が未設定の場合：既存のKPOP専用フロー（キーワードフィルタ＋KPOP関連度判定プロンプト）をそのまま使う
- 収集したArticleに、フィードの`account_id`をセットする（現状ここが未設定だった）

## 投稿文生成（`summarizer.py`）

`summarize_article()`内で`article.account_id`から`ThreadsAccount.content_topic`を解決する。

- `content_topic`が設定されている場合：新しいシンプル生成パスを使う
  - プロンプト: 記事タイトル・本文を渡し、「{content_topic}が好きな人として、自然な口語体の一言に変換する」程度の汎用指示のみ
  - 絵文字なし・ハッシュタグなし・URLなし・150文字以内（`BODY_MAX_ARTICLE`を流用）は既存ルールを踏襲
  - KPOP専用のフック集（`_KPOP_HOOKS`）・表現集・「固有名詞を必ず1つ含める」ルール・NGワードリストは適用しない
  - Step1一発生成のみ（既存のStep1→Step2の人間化2段階生成は行わない）。文字数超過時は末尾切り詰め（既存の`BODY_MAX_RETRIES`ロジックと同様、ただし再生成ではなく即切り詰めでシンプルに済ませる）
- `content_topic`が未設定の場合：既存のKPOP専用プロンプト（2段階生成・フック・固有名詞ルール等）をそのまま使う

## 設定画面（`templates/settings.html`）

- アカウント管理テーブルに`content_topic`の表示・編集欄を追加する。既存のアカウント名インライン編集（`startEditAccountLabel`等）と同じパターンを流用し、`POST /accounts/<id>/rename`と対になる新規ルート`POST /accounts/<id>/content-topic`を追加する
- RSSフィード追加フォームの各行に、アカウント選択`<select name="feed_account_id">`を追加する（既存の`feed_name`/`feed_url`と並列。デフォルトは先頭のアクティブアカウント）

## フィード登録

初期データとして`https://gachapara.jp/feed/`を名前「ガチャパラ」・`account_id=2`で`rss_feeds`に追加する（DBへの直接投入、または`/settings`フォーム経由）。

## スコープ外（今回やらないこと）

- YouTube動画収集（`video_collector.py`）のアカウント別トピック対応（今回はRSSのみが対象）
- ガチャアカウント専用の作り込まれたペルソナ・フック集（MVPでは汎用プロンプトのみ。将来的に必要になれば別途設計する）
- 3アカウント目以降を見据えた汎用的なコンテンツプロファイル管理UI（`content_topic`という単一テキストフィールドで十分と判断）
