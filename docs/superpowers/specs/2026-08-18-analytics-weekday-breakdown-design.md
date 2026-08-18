# 分析ダッシュボード 曜日別パフォーマンス集計 設計書

## 背景・目的

`/analytics`（KPOPアカウント専用分析ダッシュボード）に、既存の「投稿時刻の時間帯別平均パフォーマンス」（`post_stats`の7日確定値を投稿時刻の「時」で動的グルーピング）と同じデータソースを使い、投稿日の「曜日」で動的グルーピングした平均パフォーマンスを追加する。

## 方針

既存の`analytics()`ルート（`app.py`）に4つ目の集計クエリ`weekday_rows`を追加し、`templates/analytics.html`に既存の時間帯別テーブルと同じ形式（表のみ、Chart.js等は使わない）の曜日別テーブルセクションを追加する。既存の`hour_rows`のクエリ・テンプレート・表示ロジックには一切手を加えない。

## データ集計

```sql
SELECT
    CAST(strftime('%w', datetime(a.posted_at, '+9 hours')) AS INTEGER) AS weekday,
    COUNT(*) AS post_count,
    AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
FROM post_stats ps
JOIN articles a ON a.id = ps.article_id
WHERE ps.is_final = 1 AND ps.day_index <= 7 AND a.account_id = :account_id AND a.posted_at IS NOT NULL
GROUP BY weekday
ORDER BY CASE weekday WHEN 0 THEN 7 ELSE weekday END ASC
```

- `is_final = 1 AND day_index <= 7`は既存の`hour_rows`と全く同じ絞り込み条件（7日確定値のみ、既存の分析汚染防止フィルタを踏襲）。
- SQLiteの`strftime('%w', ...)`は0=日曜〜6=土曜を返す。`ORDER BY CASE weekday WHEN 0 THEN 7 ELSE weekday END`により月(1)→火→…→土(6)→日(0→7扱い)の順に並び替え、要件の「月〜日」順を満たす。
- 曜日ラベル（日本語）はSQLではなくPython側で辞書変換する（`{0: "日", 1: "月", ..., 6: "土"}`）。SQL側でCASE文による文字列変換も可能だが、可読性のため`app.py`側で`weekday_label`をrowに合成してテンプレートへ渡す。

## ルート変更（`app.py`）

`analytics()`内、`hour_rows`クエリの直後に`weekday_rows`を追加し、`render_template`の引数に`weekday_rows=weekday_rows`を追加する。既存の4引数（`daily_*`, `group_rows`, `member_rows`, `hour_rows`）は変更しない。

## テンプレート変更（`templates/analytics.html`）

既存の「投稿時刻の時間帯別平均パフォーマンス」`<div class="card p-3 mb-4">`セクションの直後に、同一構造（`table-responsive` > `table table-hover`、列: 曜日・投稿数・平均いいね・平均閲覧数、空状態メッセージ）の新セクションを追加する。見出しは「投稿曜日別平均パフォーマンス（7日確定値）」。

## スコープ外（今回やらないこと）

- 時間帯×曜日のクロス集計（データが溜まってから改めて検討）
- Chart.jsによる可視化（表のみで対応、ユーザー承認済み）
- 既存の時間帯別セクションの表示・ロジック変更
