# フォロー管理機能 設計書

## 背景・目的

`@kpopwave.daily` アカウントの相互フォロー整理を支援するため、管理画面に新規ページ「フォロー管理」を追加する。以下を一覧表示する。

- 片思いフォロー（自分がフォローしているのに相手がフォローしていない）
- 片思いフォロワー（相手がフォローしているのに自分がフォローしていない）
- 相互フォロー一覧

各アカウントにはThreadsプロフィールへのリンクを表示する。

## 制約とデータ取得方式

Threads公式Graph API（`graph.threads.net/v1.0`）には「フォロー中一覧」「フォロワー一覧」を取得するエンドポイントが存在しない（取得できるのはフォロワー**数**のみ。`follow_candidates.py` 内のコメントにも明記済み）。

検討した代替手段：

| 方式 | 内容 | 採用 |
|---|---|---|
| A. ログインセッションCookie方式 | Threads/Instagramの内部APIをログイン済みCookieで叩き一覧取得。リアルタイム性は高いが非公式利用のためアカウントリスクあり | 不採用 |
| B. 手動エクスポート取り込み方式 | Meta公式の「アクティビティをダウンロード」機能でエクスポートしたJSONを管理画面からアップロードし、差分計算する | **採用** |

ユーザー判断によりB（手動エクスポート取り込み方式）を採用する。アカウントリスクがなく、既存コードベースの非公式スクレイピング依存を増やさない。

## エクスポートファイル形式（Meta標準フォーマット）

Instagram/Threadsアカウント設定の「アクティビティをダウンロード」からJSON形式でエクスポートすると、以下の2ファイルが得られる。

- `followers_1.json` — ルートが配列
- `following.json` — ルートがオブジェクトで `relationships_following` キー配下に配列

共通の要素構造：

```json
{
  "title": "",
  "media_list_data": [],
  "string_list_data": [
    { "href": "https://www.instagram.com/username", "value": "username", "timestamp": 1234567890 }
  ]
}
```

パーサーは両方の形状（ルートが配列 / `relationships_following` などのキー配下）を吸収できるようにする。`string_list_data[].value` が空の場合は `href` の末尾セグメントから復元する。

## データモデル

新規テーブル `FollowRelation`（`database.py`）。既存の `FollowCandidate` と同様にアカウント紐付けなし・グローバル管理とする（要件が単一アカウント運用のため）。

| カラム | 型 | 説明 |
|---|---|---|
| id | INTEGER PK | |
| username | VARCHAR(100) UNIQUE, INDEX | Threadsユーザー名（小文字正規化） |
| is_following | BOOLEAN | 自分がフォロー中かどうか |
| is_follower | BOOLEAN | 相手が自分をフォロー中かどうか |
| updated_at | DATETIME | 最終更新日時（UTC） |

新規テーブルのため `db.create_all()` により自動作成される。`_migrate_db()` への追記は不要（既存テーブルへのカラム追加ではないため）。

最終同期日時は `Setting` テーブルに以下のキーで保存する。

- `follow_mgmt_following_synced_at`
- `follow_mgmt_followers_synced_at`

## 取り込みロジック（新規 `follow_management.py`）

- `following.json` / `followers_1.json` は個別にアップロード可能（片方のみの更新も許可）
- アップロードされたファイル種別ごとに **フルリプレース方式** で反映する：
  1. 対象フラグ（`is_following` または `is_follower`）が現在 `True` の行を全て `False` にする
  2. アップロードされたユーザー名一覧に対して、該当行を `get_or_create` し対象フラグを `True` にする
  3. `is_following` と `is_follower` が両方 `False` になった行は削除する（クリーンアップ）
  4. 対応する `Setting` の同期日時を更新する
- 不正なJSON・想定外の構造の場合はエラーメッセージを返し、既存データは変更しない

## 画面構成（新規 `templates/follow_management.html`）

`follow_candidates.html` の既存パターン（カードUI・テーブル・外部リンクボタン）を踏襲する。

- 上部：アップロードフォーム（`multipart/form-data`、`following_file` と `followers_file` の2つのファイル入力、それぞれ最終取り込み日時を表示）
- 統計カード：相互フォロー数／片思いフォロー数／片思いフォロワー数
- 3セクション（相互フォロー／片思いフォロー／片思いフォロワー）：ユーザー名 + Threadsプロフィールへの外部リンクボタン（`https://www.threads.net/@username`、`target="_blank" rel="noopener"`）
- 各セクションにJS（vanilla、フレームワーク不使用）によるユーザー名部分一致の検索ボックスを設置（クライアントサイドフィルタ、ページ再読込なし）

## ルーティング（`app.py`）

- `GET /follow-management` — ページ表示。`follow_management.py` の関数でDBから3分類を計算して渡す
- `POST /follow-management/upload` — ファイルアップロード処理。完了後 `/follow-management` にリダイレクト

## ナビゲーション（`templates/base.html`）

- デスクトップサイドバー：「フォロー候補」の下に「フォロー管理」リンクを追加（アイコン `bi-people-fill`、`request.endpoint=='follow_management'` でactive判定）
- モバイル横スクロールナビバー：同様に追加

## エラーハンドリング

- ファイル未選択でのアップロード送信 → 何もせずページに戻る（両方未選択ならバリデーションエラー表示）
- JSON parse失敗・想定外構造 → フラッシュメッセージ的な表示でエラーを伝え、既存データは保持
- 大きすぎるファイル等の特別なハンドリングは行わない（YAGNI）

## スコープ外（今回やらないこと）

- 自動フォロー/アンフォロー操作（表示のみ、実際のフォロー操作はThreadsアプリ側で手動）
- 履歴管理（過去のアップロードとの差分表示、アンフォロー検知の通知など）
- マルチアカウント対応（`FollowCandidate` 同様、単一アカウント運用前提）
