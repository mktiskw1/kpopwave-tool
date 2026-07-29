# 動画トリミングの非同期化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 動画トリミング処理（ffmpeg呼び出し）を同期ブロッキングから非同期バックグラウンドジョブに変更し、Cloudflare Tunnel経由のリクエストがタイムアウトで失敗する不具合を解消する。ジョブ状態をDBに永続化し、フロントエンドはポーリングで進捗を確認する。

**Architecture:** 新規`VideoTrimJob`テーブルでジョブ状態（processing/done/failed）を管理する。`POST /api/videos/<id>/trim`はバリデーションのみ行いジョブを作成してバックグラウンドスレッドを起動、即座に`job_id`を返す。実際のffmpeg実行・Article作成は新規の`_run_trim_job()`関数がバックグラウンドスレッド内で行う。新規`GET /api/videos/trim-jobs/<job_id>`エンドポイントでフロントエンドがポーリングする。承認待ち画面は処理中ジョブをサーバーサイドで検出し、ページ再読み込みをまたいでも「処理中」表示を維持する。

**Tech Stack:** Python 3.x / Flask / SQLAlchemy / SQLite / threading（標準ライブラリ） / ffmpeg（既存） / vanilla JS（fetch API + ポーリング）

## Global Constraints

- 対象ファイルは`database.py`・`app.py`・`templates/pending.html`のみ
- コメントは原則書かない（WHYが非自明な場合のみ1行）
- 既存ファイルを編集する。新規ファイルは作らない
- 必要な変更だけ行う。リファクタリング・クリーンアップは不要
- ffmpegコマンドの組み立てロジック自体（`-ss`/`-t`の指定方法）は変更しない。既存の実装が正しく動作することを調査済みのため、そのまま`_run_trim_job`に移植する
- Shellコマンドの実行環境はWindows。Bashツールを使う場合はGit Bash構文（`$VAR`、`&&`可）、PowerShellツールを使う場合はPowerShell構文（`$env:VAR`、`&&`不可）に注意する
- このプロジェクトにはpytest等のテストフレームワークが存在しない。各タスクの検証は、実際にFlaskアプリを起動し、`venv/Scripts/python.exe -c "..."`によるDB直接確認や`app.test_client()`によるHTTPリクエストで動作確認する
- `venv/Scripts/python.exe`が仮想環境のPythonインタプリタ
- **重要**: このプロジェクトの`app.py`は`app = create_app()`をモジュール読み込み時に実行し、以降の`@app.route`はこのモジュール変数`app`に紐づく。`test_client()`での検証には必ず`from app import app`を使うこと
- 新規テーブル（`VideoTrimJob`）は`create_app()`内の`db.create_all()`で自動作成されるため、`_migrate_db()`に`ALTER TABLE`は不要
- バックグラウンドスレッド内でのDB操作は必ず`with app.app_context():`で囲むこと（Flaskのリクエストコンテキストはスレッドをまたがない）
- バックグラウンドスレッド内の例外は広く`except Exception`で捕捉し、必ず`job.status`を`failed`に更新すること（捕捉し損なうとジョブが`processing`のまま永久に止まる）

---

## ファイル構成

| ファイル | 変更内容 |
|---|---|
| `database.py` | `VideoTrimJob`モデルを追加 |
| `app.py` | `threading`のimport追加、`trim_video`ルートを非同期化（ジョブ作成のみ）、`_run_trim_job`バックグラウンド関数を新規追加、`GET /api/videos/trim-jobs/<job_id>`を新規追加、`pending`ルートに`active_trim_jobs`を追加 |
| `templates/pending.html` | トリミングUIに処理中インジケーターを追加、`trimVideo()`をジョブ起動+ポーリング開始に書き換え、新規`pollTrimJob()`を追加、ページ読み込み時に処理中ジョブのポーリングを再開 |

---

### Task 1: `VideoTrimJob`モデルを追加（`database.py`）

**Files:**
- Modify: `database.py:44-59`（`Article`クラスの`to_dict()`直後、`ThreadsAccount`クラスの直前）

**Interfaces:**
- Produces: `VideoTrimJob`モデル（`id`, `source_article_id`, `start`, `end`, `status`, `result_article_id`, `error_message`, `created_at`, `updated_at`）。以降の全タスクがこのモデルを読み書きする

- [ ] **Step 1: `database.py`に`VideoTrimJob`クラスを追加**

`database.py:44-59`（目印）:

```python
    # マルチアカウント対応
    account_id = db.Column(db.Integer, db.ForeignKey("threads_accounts.id"), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "status": self.status,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "created_at": self.created_at.isoformat(),
        }


class ThreadsAccount(db.Model):
    __tablename__ = "threads_accounts"
```

これを以下に置き換える（`Article`クラスと`ThreadsAccount`クラスの間に新規クラスを挿入）:

```python
    # マルチアカウント対応
    account_id = db.Column(db.Integer, db.ForeignKey("threads_accounts.id"), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "status": self.status,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "created_at": self.created_at.isoformat(),
        }


class VideoTrimJob(db.Model):
    __tablename__ = "video_trim_jobs"

    id = db.Column(db.Integer, primary_key=True)
    source_article_id = db.Column(db.Integer, db.ForeignKey("articles.id"), nullable=False, index=True)
    start = db.Column(db.Float, nullable=False)
    end = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="processing")
    result_article_id = db.Column(db.Integer, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ThreadsAccount(db.Model):
    __tablename__ = "threads_accounts"
```

- [ ] **Step 2: テーブルが自動作成されることを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app
from database import VideoTrimJob, db
with app.app_context():
    db.create_all()
    print('columns:', [c.name for c in VideoTrimJob.__table__.columns])
    print('count:', VideoTrimJob.query.count())
"
```
Expected: `columns: ['id', 'source_article_id', 'start', 'end', 'status', 'result_article_id', 'error_message', 'created_at', 'updated_at']`、`count: 0`。

- [ ] **Step 3: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile database.py
```
Expected: エラーなし。

- [ ] **Step 4: Commit**

```bash
git add database.py
git commit -m "$(cat <<'EOF'
feat: 動画トリミングジョブ管理用のVideoTrimJobテーブルを追加

status（processing/done/failed）でジョブの進行状況を追跡する。
新規テーブルのためdb.create_all()で自動作成され、ALTER TABLEは不要。
EOF
)"
```

---

### Task 2: トリミングの非同期化とポーリングAPI（`app.py`）

**Files:**
- Modify: `app.py:1`（import: `threading`追加）
- Modify: `app.py:16`（import: `VideoTrimJob`追加）
- Modify: `app.py:1659-1759`（`trim_video`ルートを書き換え、`_run_trim_job`関数を新規追加、`GET /api/videos/trim-jobs/<job_id>`を新規追加）
- Modify: `app.py`の`pending()`ルート（`active_trim_jobs`をテンプレートに渡す）

**Interfaces:**
- Consumes: `VideoTrimJob`モデル（Task 1）
- Produces: `POST /api/videos/<id>/trim`が即座に`{"ok": true, "job_id": N}`を返すようになる。`GET /api/videos/trim-jobs/<int:job_id>`（JSON: `{"status": ..., "error": ..., "new_article_id": ...}`）。`pending()`が`active_trim_jobs`（`{source_article_id: job_id}`の辞書）をテンプレートに渡す

- [ ] **Step 1: importを追加**

`app.py:1-8`（目印）:

```python
import json
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qs
```

これを以下に置き換える:

```python
import json
import logging
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qs
```

`app.py:16`（目印）:

```python
from database import Article, BuzzPost, Comment, Hook, Setting, ThreadsAccount, get_active_account, db
```

これを以下に置き換える:

```python
from database import Article, BuzzPost, Comment, Hook, Setting, ThreadsAccount, VideoTrimJob, get_active_account, db
```

- [ ] **Step 2: `trim_video`ルートを非同期化し、`_run_trim_job`・ポーリングエンドポイントを追加**

`app.py:1659-1759`（目印。現在の`trim_video`ルート全体）:

```python
@app.route("/api/videos/<int:article_id>/trim", methods=["POST"])
def trim_video(article_id):
    import subprocess as _sp
    from werkzeug.exceptions import NotFound

    try:
        data = request.get_json(force=True, silent=True) or {}

        try:
            start = float(data.get("start", 0) or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "開始秒数が不正です"}), 400
        start = round(start * 2) / 2

        end_raw = data.get("end")
        end = None
        if end_raw is not None and end_raw != "":
            try:
                end = float(end_raw)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "終了秒数が不正です"}), 400
            end = round(end * 2) / 2

        article = Article.query.get_or_404(article_id)
        if not article.video_file_path:
            return jsonify({"ok": False, "error": "動画ファイルがありません"}), 400

        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        video_path = os.path.join(static_dir, article.video_file_path)
        if not os.path.exists(video_path):
            return jsonify({"ok": False, "error": "ファイルが見つかりません"}), 404

        ffmpeg_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin", "ffmpeg.exe")
        if not os.path.exists(ffmpeg_exe):
            return jsonify({"ok": False, "error": "ffmpeg.exe が見つかりません"}), 500

        videos_dir = os.path.join(static_dir, "videos")

        # 元ファイルのベース名（拡張子なし）
        # video_file_path は "videos/{video_id}.mp4" 形式
        base_name = os.path.splitext(os.path.basename(video_path))[0]

        # 元ファイルを _original として保持（まだなければリネーム）
        original_filename = base_name + "_original.mp4"
        original_path = os.path.join(videos_dir, original_filename)
        if not os.path.exists(original_path):
            import shutil as _shutil
            _shutil.copy2(video_path, original_path)

        # clip 連番を決定（既存の clip ファイル数をカウント）
        existing_clips = [
            f for f in os.listdir(videos_dir)
            if f.startswith(base_name + "_clip_") and f.endswith(".mp4")
        ]
        clip_num = len(existing_clips) + 1
        clip_filename = f"{base_name}_clip_{clip_num}.mp4"
        clip_path = os.path.join(videos_dir, clip_filename)

        # -ss を -i より前に置くキーフレームシークで高速化する。
        # この場合 -to は使えない（シーク後の相対時刻ではなく元の絶対時刻のままになるため）ので、
        # 代わりに相対時間指定の -t (end - start) を使う。
        cmd = [ffmpeg_exe, "-y", "-ss", str(start), "-i", video_path]
        if end is not None:
            cmd += ["-t", str(end - start)]
        cmd += [
            "-c:v", "libx264", "-crf", "23", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            clip_path,
        ]

        result = _sp.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace")[-500:]
            return jsonify({"ok": False, "error": err}), 500

        # 新しい Article レコードを作成（元記事はそのまま残す）
        import time as _time
        clip_rel_path = f"videos/{clip_filename}"
        new_article = Article(
            feed_source=article.feed_source,
            title=f"{article.title} [クリップ {clip_num}]",
            url=f"{article.url}#clip_{int(_time.time())}",
            status="pending",
            content_type="video",
            thumbnail_url=article.thumbnail_url,
            video_file_path=clip_rel_path,
            published_at=article.published_at,
        )
        db.session.add(new_article)
        db.session.commit()

        logger.info("動画クリップ作成完了: 元article_id=%d -> new_article_id=%d clip=%s",
                    article_id, new_article.id, clip_filename)
        return jsonify({"ok": True, "new_article_id": new_article.id})
    except NotFound:
        raise
    except Exception as exc:
        logger.exception("動画トリミング失敗: article_id=%d", article_id)
        return jsonify({"ok": False, "error": str(exc)}), 500
```

これを以下に置き換える（バリデーションのみ同期実行、実処理はバックグラウンドスレッドへ）:

```python
def _run_trim_job(app, job_id):
    import subprocess as _sp
    import shutil as _shutil
    import time as _time

    with app.app_context():
        job = db.session.get(VideoTrimJob, job_id)
        if not job:
            return
        try:
            article = db.session.get(Article, job.source_article_id)
            static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
            video_path = os.path.join(static_dir, article.video_file_path)

            ffmpeg_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin", "ffmpeg.exe")
            videos_dir = os.path.join(static_dir, "videos")

            # 元ファイルのベース名（拡張子なし）
            # video_file_path は "videos/{video_id}.mp4" 形式
            base_name = os.path.splitext(os.path.basename(video_path))[0]

            # 元ファイルを _original として保持（まだなければリネーム）
            original_filename = base_name + "_original.mp4"
            original_path = os.path.join(videos_dir, original_filename)
            if not os.path.exists(original_path):
                _shutil.copy2(video_path, original_path)

            # clip 連番を決定（既存の clip ファイル数をカウント）
            existing_clips = [
                f for f in os.listdir(videos_dir)
                if f.startswith(base_name + "_clip_") and f.endswith(".mp4")
            ]
            clip_num = len(existing_clips) + 1
            clip_filename = f"{base_name}_clip_{clip_num}.mp4"
            clip_path = os.path.join(videos_dir, clip_filename)

            # -ss を -i より前に置くキーフレームシークで高速化する。
            # この場合 -to は使えない（シーク後の相対時刻ではなく元の絶対時刻のままになるため）ので、
            # 代わりに相対時間指定の -t (end - start) を使う。
            cmd = [ffmpeg_exe, "-y", "-ss", str(job.start), "-i", video_path]
            if job.end is not None:
                cmd += ["-t", str(job.end - job.start)]
            cmd += [
                "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                "-c:a", "aac", "-b:a", "128k",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                clip_path,
            ]

            result = _sp.run(cmd, capture_output=True, timeout=600)
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", errors="replace")[-500:]
                job.status = "failed"
                job.error_message = err
                db.session.commit()
                logger.error("動画トリミング失敗(ffmpeg): job_id=%d article_id=%d error=%s",
                             job_id, job.source_article_id, err)
                return

            # 新しい Article レコードを作成（元記事はそのまま残す）
            clip_rel_path = f"videos/{clip_filename}"
            new_article = Article(
                feed_source=article.feed_source,
                title=f"{article.title} [クリップ {clip_num}]",
                url=f"{article.url}#clip_{int(_time.time())}",
                status="pending",
                content_type="video",
                thumbnail_url=article.thumbnail_url,
                video_file_path=clip_rel_path,
                published_at=article.published_at,
            )
            db.session.add(new_article)
            db.session.flush()  # new_article.id を確定させる（コミット前は未割当のため）
            job.status = "done"
            job.result_article_id = new_article.id
            db.session.commit()

            logger.info("動画クリップ作成完了: job_id=%d 元article_id=%d -> new_article_id=%d clip=%s",
                        job_id, job.source_article_id, new_article.id, clip_filename)
        except Exception as exc:
            logger.exception("動画トリミング失敗(例外): job_id=%d", job_id)
            job.status = "failed"
            job.error_message = str(exc)
            db.session.commit()


@app.route("/api/videos/<int:article_id>/trim", methods=["POST"])
def trim_video(article_id):
    data = request.get_json(force=True, silent=True) or {}

    try:
        start = float(data.get("start", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "開始秒数が不正です"}), 400
    start = round(start * 2) / 2

    end_raw = data.get("end")
    end = None
    if end_raw is not None and end_raw != "":
        try:
            end = float(end_raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "終了秒数が不正です"}), 400
        end = round(end * 2) / 2

    article = Article.query.get_or_404(article_id)
    if not article.video_file_path:
        return jsonify({"ok": False, "error": "動画ファイルがありません"}), 400

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    video_path = os.path.join(static_dir, article.video_file_path)
    if not os.path.exists(video_path):
        return jsonify({"ok": False, "error": "ファイルが見つかりません"}), 404

    ffmpeg_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin", "ffmpeg.exe")
    if not os.path.exists(ffmpeg_exe):
        return jsonify({"ok": False, "error": "ffmpeg.exe が見つかりません"}), 500

    job = VideoTrimJob(source_article_id=article_id, start=start, end=end, status="processing")
    db.session.add(job)
    db.session.commit()

    threading.Thread(target=_run_trim_job, args=(app, job.id), daemon=True).start()

    return jsonify({"ok": True, "job_id": job.id})


@app.route("/api/videos/trim-jobs/<int:job_id>")
def trim_job_status(job_id):
    job = VideoTrimJob.query.get_or_404(job_id)
    return jsonify({
        "status": job.status,
        "error": job.error_message,
        "new_article_id": job.result_article_id,
    })
```

- [ ] **Step 3: `pending()`ルートに`active_trim_jobs`を追加する**

`app.py`内、以下のブロック（目印。`pending()`関数の末尾、`render_template`呼び出し直前）:

```python
    return render_template("pending.html", articles=articles, images_map=images_map,
                           active_tab=tab, counts=counts, now_utc=datetime.utcnow())
```

これを以下に置き換える:

```python
    active_trim_jobs = {
        j.source_article_id: j.id
        for j in VideoTrimJob.query.filter(
            VideoTrimJob.source_article_id.in_([a.id for a in articles]),
            VideoTrimJob.status == "processing",
        ).all()
    } if articles else {}

    return render_template("pending.html", articles=articles, images_map=images_map,
                           active_tab=tab, counts=counts, now_utc=datetime.utcnow(),
                           active_trim_jobs=active_trim_jobs)
```

- [ ] **Step 4: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile app.py
```
Expected: エラーなし。

- [ ] **Step 5: 非同期化されたエンドポイントの動作を確認する**

（実際の動画ファイルが必要。`static/videos/`配下に既存のmp4ファイルがあることを前提とする）

```bash
venv/Scripts/python.exe -c "
from app import app
from database import Article, VideoTrimJob, db
import time

client = app.test_client()

with app.app_context():
    video_art = Article.query.filter(Article.content_type == 'video', Article.video_file_path.isnot(None)).first()
    print('test target article_id:', video_art.id if video_art else None, video_art.video_file_path if video_art else None)
    article_id = video_art.id if video_art else None

if article_id:
    t0 = time.time()
    r = client.post(f'/api/videos/{article_id}/trim', json={'start': 0, 'end': 5})
    elapsed = time.time() - t0
    print('POST status:', r.status_code, 'body:', r.get_json(), 'elapsed:', round(elapsed, 2), 's')
    job_id = r.get_json().get('job_id')

    if job_id:
        for i in range(30):
            time.sleep(2)
            r2 = client.get(f'/api/videos/trim-jobs/{job_id}')
            d = r2.get_json()
            print(f'poll {i}: status={d[\"status\"]}')
            if d['status'] != 'processing':
                print('final:', d)
                break
"
```
Expected: `POST status: 200`、`elapsed`が**数秒未満**（ffmpeg完了を待たずに即座に返ることを確認する。ここがこのタスクの最重要検証ポイント）。その後のポーリングで`status`が`processing`→`done`に遷移し、`new_article_id`が設定される。

- [ ] **Step 6: Commit**

```bash
git add app.py
git commit -m "$(cat <<'EOF'
feat: 動画トリミング処理を非同期バックグラウンドジョブに変更

同期的なsubprocess.run()呼び出しがFlaskのリクエストハンドラを
ffmpeg完了までブロックしており、Cloudflare Tunnelのタイムアウト
（既定約100秒）を数分尺のクリップで容易に超えていた。バリデーション
のみ同期実行してVideoTrimJobを作成しバックグラウンドスレッドへ
処理を委譲、即座にjob_idを返すように変更した。新規ポーリング
エンドポイントGET /api/videos/trim-jobs/<job_id>を追加し、
pending()ルートは処理中ジョブをactive_trim_jobsとしてテンプレートに
渡すようにした。
EOF
)"
```

---

### Task 3: 承認待ち画面のポーリングUI（`templates/pending.html`）

**Files:**
- Modify: `templates/pending.html:234-245`（トリミングボタン部分に処理中インジケーターを追加）
- Modify: `templates/pending.html:562-594`（`trimVideo()`を書き換え、`pollTrimJob()`を新規追加）
- Modify: `templates/pending.html`（ページ読み込み時に処理中ジョブのポーリングを再開する`DOMContentLoaded`処理を追加）

**Interfaces:**
- Consumes: `active_trim_jobs`（`pending()`ルートが渡す、Task 2）、`POST /api/videos/<id>/trim`・`GET /api/videos/trim-jobs/<job_id>`（Task 2）

- [ ] **Step 1: トリミングボタン部分に処理中インジケーターを追加**

`templates/pending.html:234-245`（目印）:

```html
          <!-- 切り取りボタン -->
          <button type="button"
                  onclick="trimVideo({{ a.id }}, this)"
                  style="padding:4px 14px;border-radius:6px;border:1px solid var(--accent);
                         background:var(--accent-soft);color:var(--accent);font-size:.82rem;
                         cursor:pointer;font-weight:700">
            ✂️ 切り取って別投稿として追加
          </button>
          <div id="trim-msg-{{ a.id }}" style="display:none;margin-top:6px;padding:4px 10px;
               border-radius:6px;background:#e8f5ed;color:#2e7d32;font-weight:700;font-size:.82rem">
            ✅ 承認待ちに追加しました！
          </div>
        </div>
      </div>
```

これを以下に置き換える:

```html
          <!-- 切り取りボタン -->
          <button type="button" id="trim-btn-{{ a.id }}"
                  onclick="trimVideo({{ a.id }}, this)"
                  {% if active_trim_jobs.get(a.id) %}disabled{% endif %}
                  style="padding:4px 14px;border-radius:6px;border:1px solid var(--accent);
                         background:var(--accent-soft);color:var(--accent);font-size:.82rem;
                         cursor:pointer;font-weight:700">
            {% if active_trim_jobs.get(a.id) %}
            <span class="spinner-border spinner-border-sm"></span> 処理中...
            {% else %}
            ✂️ 切り取って別投稿として追加
            {% endif %}
          </button>
          <div id="trim-msg-{{ a.id }}" style="display:none;margin-top:6px;padding:4px 10px;
               border-radius:6px;background:#e8f5ed;color:#2e7d32;font-weight:700;font-size:.82rem">
            ✅ 承認待ちに追加しました！
          </div>
          {% if active_trim_jobs.get(a.id) %}
          <div id="trim-job-{{ a.id }}" data-job-id="{{ active_trim_jobs.get(a.id) }}" style="display:none"></div>
          {% endif %}
        </div>
      </div>
```

- [ ] **Step 2: `trimVideo()`を書き換え、`pollTrimJob()`を新規追加**

`templates/pending.html:561-594`（目印）:

```javascript
// ── 動画切り取り（別投稿として追加） ────────────────────────────────────────
function trimVideo(id, btn) {
  const start = parseFloat(document.getElementById('trim-start-' + id).value) || 0;
  const endVal = document.getElementById('trim-end-' + id).value;
  const end = endVal !== '' ? parseFloat(endVal) : null;
  if (end !== null && end <= start) {
    alert('終了時間は開始時間より大きい値を入力してください');
    return;
  }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 処理中...';
  fetch('/api/videos/' + id + '/trim', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({start: start, end: end})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    btn.disabled = false;
    btn.innerHTML = orig;
    if (d.ok) {
      var msg = document.getElementById('trim-msg-' + id);
      if (msg) msg.style.display = 'block';
      setTimeout(function() { location.href = '/pending?tab=video'; }, 1000);
    } else {
      alert('切り取り失敗: ' + (d.error || '不明なエラー'));
    }
  })
  .catch(function() {
    alert('通信が中断されました。処理が完了している可能性があるため、一覧を更新して確認します。');
    location.href = '/pending?tab=video';
  });
}
```

これを以下に置き換える:

```javascript
// ── 動画切り取り（別投稿として追加） ────────────────────────────────────────
function trimVideo(id, btn) {
  const start = parseFloat(document.getElementById('trim-start-' + id).value) || 0;
  const endVal = document.getElementById('trim-end-' + id).value;
  const end = endVal !== '' ? parseFloat(endVal) : null;
  if (end !== null && end <= start) {
    alert('終了時間は開始時間より大きい値を入力してください');
    return;
  }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 処理中...';
  fetch('/api/videos/' + id + '/trim', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({start: start, end: end})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.ok) {
      pollTrimJob(id, d.job_id, btn, orig);
    } else {
      btn.disabled = false;
      btn.innerHTML = orig;
      alert('切り取り失敗: ' + (d.error || '不明なエラー'));
    }
  })
  .catch(function() {
    btn.disabled = false;
    btn.innerHTML = orig;
    alert('通信エラーが発生しました。もう一度お試しください。');
  });
}

function pollTrimJob(articleId, jobId, btn, origBtnHtml) {
  btn = btn || document.getElementById('trim-btn-' + articleId);
  origBtnHtml = origBtnHtml || '✂️ 切り取って別投稿として追加';
  const check = function() {
    fetch('/api/videos/trim-jobs/' + jobId)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.status === 'processing') {
          setTimeout(check, 3000);
        } else if (d.status === 'done') {
          var msg = document.getElementById('trim-msg-' + articleId);
          if (msg) msg.style.display = 'block';
          setTimeout(function() { location.href = '/pending?tab=video'; }, 1000);
        } else {
          if (btn) {
            btn.disabled = false;
            btn.innerHTML = origBtnHtml;
          }
          alert('切り取り失敗: ' + (d.error || '不明なエラー'));
        }
      })
      .catch(function() { setTimeout(check, 3000); });
  };
  check();
}
```

- [ ] **Step 3: ページ読み込み時に処理中ジョブのポーリングを再開する**

`templates/pending.html`内、既存の`DOMContentLoaded`リスナー（`sessionStorage.getItem('pendingEditId')`を扱っているもの）の直後に追記する。目印:

```javascript
document.addEventListener('DOMContentLoaded', function() {
  const editId = sessionStorage.getItem('pendingEditId');
  if (!editId) return;
  sessionStorage.removeItem('pendingEditId');
  const card = document.getElementById('card-' + editId);
  if (!card) return;
  enterEditMode(editId);
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
});
```

このブロックの直後に追加:

```javascript
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('[id^="trim-job-"]').forEach(function(el) {
    const articleId = el.id.replace('trim-job-', '');
    const jobId = el.dataset.jobId;
    if (jobId) pollTrimJob(articleId, jobId);
  });
});
```

- [ ] **Step 4: 構文チェックとテンプレートレンダリング確認**

```bash
venv/Scripts/python.exe -c "
from app import app
client = app.test_client()
r = client.get('/pending?tab=video')
print('status:', r.status_code)
html = r.get_data(as_text=True)
print('has pollTrimJob function:', 'function pollTrimJob' in html)
"
```
Expected: `status: 200`、`has pollTrimJob function: True`。

- [ ] **Step 5: 実際に数分尺の動画でトリミングを行い、タイムアウトが発生しないこと・正しくトリミングされることを確認する**

（実際の`static/videos/`内の数分尺mp4ファイルを使う。Task 2のStep 5と同様のパターンでPOST→ポーリングを行い、`elapsed`が短いこと、最終的に`status: done`になり、`new_article_id`で参照される`Article.video_file_path`が`_clip_`を含む新しいファイルであること、そのファイルの実際の長さ（ffprobe）が指定したstart/endの差分に近いことを確認する）

```bash
venv/Scripts/python.exe -c "
from app import app
from database import Article, VideoTrimJob, db
import subprocess, os, time

client = app.test_client()

with app.app_context():
    video_art = Article.query.filter(Article.content_type == 'video', Article.video_file_path.isnot(None)).first()
    article_id = video_art.id

t0 = time.time()
r = client.post(f'/api/videos/{article_id}/trim', json={'start': 10, 'end': 70})
print('POST elapsed:', round(time.time() - t0, 2), 's, status:', r.status_code)
job_id = r.get_json()['job_id']

result_article_id = None
for i in range(60):
    time.sleep(3)
    d = client.get(f'/api/videos/trim-jobs/{job_id}').get_json()
    if d['status'] != 'processing':
        print('final job status:', d)
        result_article_id = d.get('new_article_id')
        break

if result_article_id:
    with app.app_context():
        clip = db.session.get(Article, result_article_id)
        clip_path = os.path.join('static', clip.video_file_path)
        print('clip path:', clip_path, 'exists:', os.path.exists(clip_path))
        ffprobe = os.path.join('ffmpeg', 'bin', 'ffprobe.exe')
        dur = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', clip_path], capture_output=True, text=True)
        print('clip duration:', dur.stdout.strip(), '(期待値: 60秒前後)')
"
```
Expected: `POST elapsed`が数秒未満。最終的に`status: done`。クリップファイルが実在し、長さが指定区間（60秒）に近い。

- [ ] **Step 6: Commit**

```bash
git add templates/pending.html
git commit -m "$(cat <<'EOF'
feat: 動画トリミングのポーリングUIを追加

trimVideo()をジョブ起動のみに変更し、新規pollTrimJob()で完了/失敗を
ポーリングするようにした。承認待ち画面の再読み込み・再訪問時にも
active_trim_jobsから処理中ジョブを検出し、ポーリングを自動再開する。
誤解を招いていた「通信が中断されました…一覧を更新して確認します」
というエラーメッセージも、素直な通信エラー表示に変更した。
EOF
)"
```

---

## 完了確認

全タスク完了後、以下を満たしていることを確認する:

- [ ] `POST /api/videos/<id>/trim`が数秒以内に`job_id`を返す（ffmpeg完了を待たない）
- [ ] `GET /api/videos/trim-jobs/<job_id>`で`processing`→`done`（または`failed`）への状態遷移が確認できる
- [ ] 数分尺の動画で実際にトリミングを行い、Cloudflare Tunnel経由でもタイムアウトが発生せず、正しくトリミングされたクリップが新しい投稿として作成される
- [ ] 承認待ち画面を処理中に再読み込みしても「処理中」表示が維持され、完了後は自動的に一覧に反映される
- [ ] 失敗時はエラーメッセージが表示され、切り取りボタンが再度有効になる
