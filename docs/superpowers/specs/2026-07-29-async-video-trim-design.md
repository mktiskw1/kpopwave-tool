# 動画トリミングの非同期化 設計書

## 背景・目的

KPOPアカウントの動画トリミング機能（承認待ち画面の「切り取って別投稿として追加」）で、Cloudflare Tunnel経由のリクエストが「通信が中断されました」というエラーで失敗し、その後トリミングされたクリップが正しく一覧に反映されないという不具合が報告された。

## 根本原因（調査確定）

- `app.py`の`trim_video`ルートは、ffmpegを`subprocess.run(cmd, capture_output=True, timeout=600)`で**同期的に**呼び出しており、Flaskのリクエストハンドラがffmpeg完了までブロックされる
- 実測で60秒分のクリップ切り出しに50.8秒かかることを確認した。数分尺のクリップでは2〜3分かかる見込みで、Cloudflare無料named tunnelの既定タイムアウト（約100秒）を容易に超える
- クライアント側の`fetch()`がタイムアウトで失敗すると、`.catch()`が「通信が中断されました…一覧を更新して確認します」と表示しページを再読み込みするが、この時点ではサーバー側のffmpeg処理がまだ完了していないことが多く、ユーザーは「新しいクリップが見当たらない／元動画のままに見える」と感じる
- コード上、失敗時に元動画パスで投稿レコードが作成される処理は存在しない（新規`Article`はffmpeg成功後にのみ作成される）。start/endパラメータのffmpegコマンドへの受け渡しも正しく行われている。ETag/pubDate関連の問題はこの機能とは無関係

## 方針

トリミング処理をバックグラウンドスレッドで非同期実行するように変更する。リクエストは即座にジョブIDを返し、フロントエンドはジョブの状態をポーリングして完了・失敗を検知する。ジョブ状態はDBに永続化し、`run.py`によるサーバープロセスの再起動（このプロジェクトの開発運用で頻繁に発生する）を跨いでも状態が失われないようにする。

## データモデル

```python
class VideoTrimJob(db.Model):
    __tablename__ = "video_trim_jobs"
    id = db.Column(db.Integer, primary_key=True)
    source_article_id = db.Column(db.Integer, db.ForeignKey("articles.id"), nullable=False, index=True)
    start = db.Column(db.Float, nullable=False)
    end = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="processing")  # processing / done / failed
    result_article_id = db.Column(db.Integer, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

新規テーブルのため`db.create_all()`で自動作成される（`ALTER TABLE`不要）。

## バックエンド設計

### `POST /api/videos/<article_id>/trim`（既存ルートを書き換え）

- 既存の高速なバリデーション（start/endのパース、記事の存在確認、`video_file_path`の存在確認、ffmpeg実行ファイルの存在確認）はそのまま維持する
- バリデーション通過後、`VideoTrimJob(status="processing")`を作成しコミットする
- `threading.Thread(target=_run_trim_job, args=(app, job.id), daemon=True).start()`でバックグラウンドスレッドを起動する
- 即座に`{"ok": true, "job_id": job.id}`を返す（ffmpeg完了を待たない）

### `_run_trim_job(app, job_id)`（新規、ルートではない通常関数）

- `with app.app_context():`で全体を囲む（バックグラウンドスレッドにはFlaskのリクエストコンテキストが無いため）
- 既存の`trim_video`が行っていたファイルコピー・clip連番決定・ffmpeg実行・`Article`作成のロジックをそのまま移植する
- ffmpeg成功時: `job.status = "done"`, `job.result_article_id = new_article.id`
- ffmpeg失敗時（`returncode != 0`）: `job.status = "failed"`, `job.error_message = <stderr末尾>`
- 想定外の例外時: 広く`except Exception`で捕捉し`job.status = "failed"`, `job.error_message = str(exc)`（バックグラウンドスレッドで例外が握りつぶされるとジョブが「処理中」のまま止まってしまうため、必ず捕捉してジョブ状態を確定させる）

### `GET /api/videos/trim-jobs/<int:job_id>`（新規）

- ジョブが存在しなければ404
- `{"status": job.status, "error": job.error_message, "new_article_id": job.result_article_id}`を返す

### `GET /pending`（既存ルートに追加）

- 表示対象の`articles`のうち動画記事について、`status == "processing"`の`VideoTrimJob`を`source_article_id`ごとに引き当てた辞書`active_trim_jobs`を構築し、テンプレートに渡す。これにより、ページを再読み込み・再訪問しても「処理中」のジョブが可視化される

## フロントエンド設計（`templates/pending.html`）

- 動画プレビューのトリミングUIに、`active_trim_jobs`に該当ジョブがある場合は「処理中」インジケーターを表示し、切り取りボタンを無効化する
- `trimVideo(id, btn)`を書き換える:
  - POSTのレスポンスは今後ほぼ即座に返る（ジョブ作成のみで応答するため）。成功時は`job_id`を受け取り、新設の`pollTrimJob(id, jobId)`を呼び出してポーリングを開始する
  - `.catch()`（真の通信エラー時）のメッセージを「一覧を更新して確認します」という誤解を招く文言から、素直な通信エラーメッセージに変更する。ジョブがサーバー側で作成されたかどうか不明なため、ページ遷移はせずエラー表示のみに留める
- 新規`pollTrimJob(articleId, jobId)`:
  - `/api/videos/trim-jobs/<jobId>`を数秒間隔（3秒）でポーリングする
  - `status == "processing"`: ポーリング継続、「処理中」表示を維持
  - `status == "done"`: ポーリング停止、成功メッセージを表示し、少し待ってからページをリロードして一覧に新しいクリップを反映する
  - `status == "failed"`: ポーリング停止、エラーメッセージを表示し、切り取りボタンを再度有効化する
- ページ読み込み時（`DOMContentLoaded`）、サーバーから渡された`active_trim_jobs`に含まれる動画については自動的に`pollTrimJob`を呼び出し、ポーリングを再開する（ページ遷移・再読み込みをまたいでも進捗を見失わないようにするため）

## 影響を受けないもの

- `database.py`の既存テーブル（新規テーブル追加のみ）
- ffmpegコマンド自体の組み立てロジック（`-ss`/`-t`によるstart/end指定は現状のまま、既に正しく動作することを確認済み）
- トリミング以外の動画関連機能（YouTube収集・投稿）

## テスト・検証方針

このプロジェクトにはpytest等のテストフレームワークがないため、実際にFlaskアプリを起動しての手動検証を行う:

1. `VideoTrimJob`モデルの単体動作を`venv/Scripts/python.exe -c "..."`で確認
2. `POST /api/videos/<id>/trim`が即座に（数秒以内に）`job_id`を返すことを確認する
3. `GET /api/videos/trim-jobs/<job_id>`をポーリングし、`processing`→`done`と状態が遷移すること、`result_article_id`が正しいクリップのarticle_idを指すことを確認する
4. 実際に数分尺の動画でトリミングを行い、タイムアウトが発生しないこと・正しくトリミングされた動画ファイルが新しい投稿として作成されることを確認する
5. 承認待ち画面をトリミング処理中に再読み込みし、「処理中」表示が正しく再現されることを確認する

## スコープ外（今回やらないこと）

- WebSocketによるリアルタイム通知（ポーリングで十分と判断）
- Celery等の本格的なジョブキューシステムの導入（このアプリの規模には過剰）
- 既存の`_original.mp4`（過去の中断されたトリミング試行の残骸）の自動クリーンアップ（今回のスコープ外、必要であれば別途対応）
