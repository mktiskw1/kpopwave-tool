# 動画トリミング中の他操作ブロック解消 設計書

## 背景・調査結果

動画トリミングは前回（2026-07-29）バックグラウンドスレッド化されており、`VideoTrimJob`テーブル・3秒間隔ポーリング・ページ遷移をまたいだ進捗復元は既に実装済み。トリミングUIもモーダルではなく各記事カード内のインライン表示で、処理中は該当ボタンのみが`disabled`になる設計であり、JS側で他のボタンや他ページへの遷移をブロックするコードは存在しない。バックエンドの重複防止ガード（`VideoTrimJob.query.filter_by(source_article_id=article_id, status="processing")`）も記事単位のスコープであり、異なる記事同士の並行トリミングは元々禁止されていない。

**実際のボトルネックを特定した**: `app.py`末尾の`app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000)`に`threaded`パラメータが指定されておらず、Werkzeug開発サーバーのデフォルト`threaded=False`のまま動作している。さらに`/collect`（RSS収集）・`/collect-youtube`・`/collect-videos`はいずれもリクエストハンドラ内で同期的にダウンロード・収集処理を行っており、数十秒〜数分かかりうる。`threaded=False`のFlask開発サーバーは同時に1リクエストしか処理できないため、これらの収集処理が実行されている間、トリミングの進捗ポーリング・承認ボタン・他ページへの遷移を含む**あらゆる他のHTTPリクエストが待たされる**。これがユーザー体験上の「他の操作がブロックされる」の主因である。

## 方針

1. `app.run()`に`threaded=True`を追加し、Flask開発サーバーがリクエストを並行処理できるようにする。バックグラウンドスレッド（`_run_trim_job`）は元々`app.app_context()`とFlask-SQLAlchemyのスコープドセッションで多重アクセスに対応済みであり、SQLiteへの複数スレッドからの同時アクセスは既存のAPScheduler（`scheduler.py`）でも発生している前提であるため、リスクの新規追加ではなく既存の対応方針の延長線上にある。
2. トリミング完了時の挙動を、現在の`location.href = '/pending?tab=video'`（強制ページ全体遷移）から、**自動遷移しない**方式に変更する。完了検知時は既存の`trim-msg-{{a.id}}`表示に「一覧を更新」リンクを添えて表示し、トリミングボタンは再度有効化する（同一記事から別区間を続けて切り出せるように）。ユーザーが他の記事の編集中であってもその作業は中断されない。

## 変更箇所

### `app.py`
- `app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000, threaded=True)`

### `templates/pending.html`
- `pollTrimJob`の`status === 'done'`分岐: `location.href`による自動遷移を削除し、`trim-msg-{{a.id}}`要素の内容を「✅ 完了！新しいクリップが承認待ちに追加されました」＋「一覧を更新」リンク（`/pending?tab=video`への通常の`<a>`リンク、クリック時のみ遷移）に差し替える。トリミングボタンは`disabled`を解除し元のラベルに戻す。

## 影響を受けないもの

- `VideoTrimJob`モデル・`_run_trim_job`・`trim_video`ルート・`trim_job_status`ルート（バックエンドの非同期処理自体は変更なし）
- 同一記事への並行トリムジョブ防止ガード（`source_article_id`単位のスコープのまま維持）
- 失敗時（`status === 'failed'`）の`alert()`表示（今回のスコープ外）
- `active_trim_jobs`によるページ再訪問時の進捗復元ロジック（既に正しく動作している）

## テスト・検証方針

pytest等のフレームワークがないため手動検証を行う:

1. `venv/Scripts/python.exe -m py_compile app.py`で構文確認
2. `app.run`の呼び出しに`threaded=True`が渡っていることをコード上確認
3. `templates/pending.html`のレンダリングを`app.test_client()`で確認（`location.href`削除・新しい完了メッセージ文言・ボタン再有効化ロジックが含まれること）
4. 実際に動画トリミングを実行し、完了時に強制的なページ遷移が発生しないこと、「一覧を更新」リンクをクリックすると新しいクリップが表示されることを目視確認する（開発サーバーが既に起動中の場合は新規プロセスを立てず、既存プロセスの動作を確認する）

## スコープ外（今回やらないこと）

- `/collect`・`/collect-youtube`・`/collect-videos`自体の非同期化（`threaded=True`により「他の操作をブロックしない」という目的は達成されるため、収集処理自体を非同期ジョブ化する大掛かりな変更は今回行わない）
- 失敗時のエラー表示を`alert()`から非ブロッキングな表示に変更すること
- 新しいクリップをJSで動的にカード追加する（DOM構築の複雑化を避けるため、リンククリックによる通常のページ遷移で表示する）
