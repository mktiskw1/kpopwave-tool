# 長尺コンピレーション動画のチャプター単位自動クリップ抽出 実装プラン

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 長尺コンピレーション動画をYouTube URL手動追加フォームに投入したとき、動画にチャプターがあれば自動検出し、チャプターごとに個別クリップとしてダウンロード、選択画面でユーザーが取捨選択してから承認待ちキューに登録できるようにする。グループ名はチャプタータイトルから既存マスタとの照合のみで自動タグ付けする（新規作成・メンバー推測はしない）。

**Architecture:** 既存の`add_video_manual()`(`app.py`)が持つ範囲指定ダウンロード機構（Cookie認証・`js_runtimes`・`remote_components`込み）を`_download_youtube_range()`ヘルパーに抽出し、新規のチャプタージョブ機構（`ChapterJob`/`ChapterClip`モデル、バックグラウンドスレッド、進捗ポーリング、選択画面テンプレート）から再利用する。既存の`VideoTrimJob`のジョブパターン（DB永続化ステータス・`threading.Thread(daemon=True)`・ポーリングエンドポイント）を踏襲する。

**Tech Stack:** Flask + SQLAlchemy (`Flask-SQLAlchemy`) + SQLite、yt-dlp Python API、ffmpeg/ffprobe(同梱バイナリ)、素のJavaScript(フレームワークなし)。

## Global Constraints

- 手入力の開始・終了時刻欄の**どちらかにでも値がある場合は、従来通りの単一範囲（またはフィールドが空欄の方は無制限側）ダウンロードを行い、チャプター検出は行わない**。**両方空欄の場合のみ**チャプター検出を試みる。
- グループ名の自動照合は**既存`groups`マスタとの完全一致のみ**。一致しなければ`NULL`のまま（新規作成しない）。
- メンバー名の自動推測は**行わない**。
- 各チャプターのダウンロード後（最後のチャプター処理後も含む）に`time.sleep(1.5)`を挟み、YouTube側への連続リクエストを緩和する。
- 実際のネットワークアクセスを伴う検証は必要最小限に留める（前回までの調査でYouTube側のセッション認識が短時間の大量リクエストで不安定になることが判明しているため）。モックでのロジック検証を優先し、実ネットワーク確認は各タスク合計で数回まで。
- `video_collector.py`のチャンネル自動巡回収集、`add_video_manual()`の「範囲未指定（動画全体ダウンロード）」分岐そのもの、承認待ち画面のグループ・メンバー手動タグ付けモーダルには変更を加えない。

---

## ファイル構成

- `database.py` — `ChapterJob`・`ChapterClip`モデルを追加（既存末尾`DailyStat`の後に追記）
- `app.py` — `_download_youtube_range()`・`_YouTubeDownloadNotFoundError`・`_guess_group_id()`・`_probe_duration()`ヘルパー、`_run_chapter_job()`バックグラウンドジョブ、3つの新規ルートを追加。既存`add_video_manual()`を`_download_youtube_range()`を使うようリファクタ
- `templates/chapter_clips.html`（新規） — 進捗表示＋クリップ選択画面
- `templates/index.html` — `addVideoManual()`のJSを変更し、チャプター検出フローに分岐させる

---

### Task 1: DBモデル追加 + グループ自動照合ヘルパー

**Files:**
- Modify: `database.py:239`（末尾、`DailyStat`クラスの後）
- Modify: `app.py:19-22`（import文）、`app.py:622-624`（`_resolve_group_and_member`の直後）

**Interfaces:**
- Produces: `ChapterJob`モデル（`database.py`）— フィールド: `id, source_url, video_id, video_title, thumbnail_url, account_id, status, error_message, created_at, updated_at`
- Produces: `ChapterClip`モデル（`database.py`）— フィールド: `id, job_id, chapter_index, title, start_time, end_time, status, video_file_path, duration, guessed_group_id, error_message, created_at`
- Produces: `_guess_group_id(chapter_title: str) -> int | None`（`app.py`）— Task 3の`_run_chapter_job`が呼ぶ

- [ ] **Step 1: `database.py`末尾に2モデルを追記**

`database.py`の239行目（ファイル末尾、`DailyStat`クラスの`__table_args__`行）の直後に以下を追記する:

```python


class ChapterJob(db.Model):
    __tablename__ = "chapter_jobs"

    id = db.Column(db.Integer, primary_key=True)
    source_url = db.Column(db.String(1000), nullable=False)
    video_id = db.Column(db.String(50), nullable=False)
    video_title = db.Column(db.String(500), nullable=True)
    thumbnail_url = db.Column(db.String(500), nullable=True)
    account_id = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="processing")  # processing / done / failed
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChapterClip(db.Model):
    __tablename__ = "chapter_clips"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("chapter_jobs.id"), nullable=False, index=True)
    chapter_index = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(500), nullable=False)
    start_time = db.Column(db.Float, nullable=False)
    end_time = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending / downloading / done / failed
    video_file_path = db.Column(db.String(500), nullable=True)
    duration = db.Column(db.Float, nullable=True)
    guessed_group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
```

新規テーブルのため、アプリ起動時の`db.create_all()`で自動作成される（既存テーブルへの`ALTER TABLE`は不要）。

- [ ] **Step 2: `app.py`のimportに`ChapterJob`・`ChapterClip`を追加**

`app.py:19-22`の現在の内容:

```python
from database import (
    Article, BuzzPost, Comment, DailyStat, Group, Hook, Member, PostStat,
    Setting, ThreadsAccount, VideoTrimJob, get_active_account, db,
)
```

これを以下に置き換える:

```python
from database import (
    Article, BuzzPost, ChapterClip, ChapterJob, Comment, DailyStat, Group, Hook, Member,
    PostStat, Setting, ThreadsAccount, VideoTrimJob, get_active_account, db,
)
```

- [ ] **Step 3: `_guess_group_id`ヘルパーを追加**

`app.py:622-624`の現在の内容（`_resolve_group_and_member`の末尾から`approve_article`ルートの手前）:

```python
    return group.id, member.id


@app.route("/articles/<int:id>/approve", methods=["POST"])
```

これを以下に置き換える（`_guess_group_id`を間に挿入）:

```python
    return group.id, member.id


def _guess_group_id(chapter_title: str) -> int | None:
    """"グループ名 (ハングル等) - 曲名" 形式のチャプタータイトルから、既存groupsマスタと
    正規化キーで完全一致するものがあれば group.id を返す。一致しなければ None（新規作成はしない）。"""
    if " - " not in chapter_title:
        return None
    candidate = chapter_title.split(" - ", 1)[0].strip()
    if not candidate:
        return None

    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", candidate).strip()
    for name in (stripped, candidate):
        if not name:
            continue
        norm = _normalize_tag_name(name)
        group = Group.query.filter_by(normalized_name=norm).first()
        if group:
            return group.id
    return None


@app.route("/articles/<int:id>/approve", methods=["POST"])
```

`re`モジュールは`app.py:4`で既にインポート済み（`import re`）。

- [ ] **Step 4: 検証**

`ChapterJob`・`ChapterClip`がインポート・作成できること、`_guess_group_id`が正しく動作することを確認する。以下をプロジェクトルートに一時ファイル `verify_task1.py` として保存し実行する:

```python
import sys
sys.path.insert(0, ".")
from app import app, _guess_group_id
from database import db, ChapterJob, ChapterClip, Group

with app.app_context():
    # テーブルが作成されていることを確認（既存DBに対してcreate_allは差分のみ追加）
    db.create_all()

    # 既存マスタに無いグループ名 -> None
    assert _guess_group_id("NoSuchGroup (없음) - SomeSong") is None, "未登録グループはNoneのはず"

    # テスト用グループを一時作成し、一致することを確認
    test_group = Group(name="_TestGroupXYZ", normalized_name="_testgroupxyz")
    db.session.add(test_group)
    db.session.flush()

    gid = _guess_group_id("_TestGroupXYZ (테스트) - Song Title")
    assert gid == test_group.id, f"括弧除去マッチが失敗: {gid} != {test_group.id}"

    gid2 = _guess_group_id("_TestGroupXYZ - Song Title")
    assert gid2 == test_group.id, f"括弧なしマッチが失敗: {gid2} != {test_group.id}"

    gid3 = _guess_group_id("タイトルのみ（区切りなし）")
    assert gid3 is None, "' - ' が無い場合はNoneのはず"

    # ChapterJob/ChapterClipの作成・削除がエラーなく行えることを確認
    job = ChapterJob(source_url="https://example.com/x", video_id="testid", status="processing")
    db.session.add(job)
    db.session.flush()
    clip = ChapterClip(job_id=job.id, chapter_index=0, title="t", start_time=0.0, end_time=10.0)
    db.session.add(clip)
    db.session.flush()
    assert clip.id is not None

    # クリーンアップ（テスト用データを残さない）
    db.session.delete(clip)
    db.session.delete(job)
    db.session.delete(test_group)
    db.session.commit()

print("OK: Task 1 verification passed")
```

実行: `python verify_task1.py`
期待される出力: `OK: Task 1 verification passed`

検証後、`verify_task1.py`を削除する。

- [ ] **Step 5: コミット**

```bash
git add database.py app.py
git commit -m "feat: チャプター抽出用DBモデルとグループ自動照合ヘルパーを追加"
```

---

### Task 2: 範囲指定ダウンロード処理の共通化（既存機能の無変更リファクタ）

**Files:**
- Modify: `app.py:2259-2361`（`_parse_time_input`の直後〜`add_video_manual`の範囲指定ダウンロード部分）

**Interfaces:**
- Consumes: `_YOUTUBE_COOKIE_FILE`（`app.py:37`）、`_YT_DLP_JS_OPTS`（`app.py:43`）、`video_collector._find_downloaded_file(tmp_dir, prefix) -> tuple[str, str] | None`
- Produces: `_download_youtube_range(yt_url: str, vid_id: str, start_time: float | None, end_time: float | None, suffix: str) -> tuple[str, str]`（成功時は`(ローカルファイルパス, 拡張子)`を返す。`start_time`が`None`なら範囲指定なし＝動画全体。ダウンロード自体の失敗は元の例外をそのまま送出。ダウンロードは成功したがファイルが見つからない場合は`_YouTubeDownloadNotFoundError`を送出）— Task 3が再利用する
- Produces: `_YouTubeDownloadNotFoundError`（`Exception`サブクラス）

このタスクは既存の`add_video_manual()`の外部から見た挙動（レスポンスJSON・エラーメッセージ・作成される`Article`の内容）を一切変更しない、純粋な抽出リファクタである。

- [ ] **Step 1: `_download_youtube_range`ヘルパーと例外クラスを追加**

`app.py:2259-2273`の現在の内容:

```python
def _parse_time_input(s: str | None) -> float | None:
    """"H:MM:SS" / "MM:SS" / "SS" 形式の文字列を秒数(float)に変換する。空文字列・Noneは None。"""
    s = (s or "").strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError("時刻の形式が不正です")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


@app.route("/api/videos/add-manual", methods=["POST"])
```

これを以下に置き換える（`_download_youtube_range`とその例外クラスを間に挿入）:

```python
def _parse_time_input(s: str | None) -> float | None:
    """"H:MM:SS" / "MM:SS" / "SS" 形式の文字列を秒数(float)に変換する。空文字列・Noneは None。"""
    s = (s or "").strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError("時刻の形式が不正です")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


class _YouTubeDownloadNotFoundError(Exception):
    """ダウンロード自体は成功したが、ローカルに出力ファイルが見つからない場合に送出する。"""


def _download_youtube_range(yt_url: str, vid_id: str, start_time: float | None, end_time: float | None, suffix: str) -> tuple[str, str]:
    """指定範囲(start_time が None なら動画全体)をダウンロードし、(ローカルの一時ファイルパス, 拡張子) を返す。
    ダウンロード自体の失敗は例外をそのまま送出する。ダウンロードは成功したがファイルが見つからない場合は
    _YouTubeDownloadNotFoundError を送出する。"""
    import tempfile
    import yt_dlp
    from yt_dlp.utils import download_range_func
    from video_collector import _find_downloaded_file

    tmp_dir = os.path.join(tempfile.gettempdir(), "kpopwave_videos")
    os.makedirs(tmp_dir, exist_ok=True)
    outtmpl = os.path.join(tmp_dir, f"{vid_id}{suffix}.%(ext)s")
    ffmpeg_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin")

    dl_opts = {
        "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]",
        "ffmpeg_location": ffmpeg_bin,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        **_YT_DLP_JS_OPTS,
    }
    if os.path.exists(_YOUTUBE_COOKIE_FILE):
        dl_opts["cookiefile"] = _YOUTUBE_COOKIE_FILE
    if start_time is not None:
        dl_opts["download_ranges"] = download_range_func(
            [], [(start_time, end_time if end_time is not None else float("inf"))]
        )

    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([yt_url])

    found = _find_downloaded_file(tmp_dir, vid_id + suffix)
    if not found:
        raise _YouTubeDownloadNotFoundError("ダウンロードファイルが見つかりません")
    return found


@app.route("/api/videos/add-manual", methods=["POST"])
```

- [ ] **Step 2: `add_video_manual`をヘルパー呼び出しに置き換え**

`app.py`の`add_video_manual`関数のうち、以下の現在のブロック（関数冒頭の`import`行〜ダウンロード〜ファイル発見部分、旧`app.py:2274-2361`相当）:

```python
def add_video_manual():
    import shutil, tempfile
    from yt_dlp.utils import download_range_func

    data = request.get_json(force=True) or {}
```

これを以下に置き換える（`import shutil, tempfile` → `import shutil`、`download_range_func`のimportを削除。ヘルパー内で完結するため）:

```python
def add_video_manual():
    import shutil

    data = request.get_json(force=True) or {}
```

続いて、以下の現在のブロック（`range_suffix`計算の直後、`tmp_dir`構築〜ダウンロード〜`_find_downloaded_file`まで）:

```python
    tmp_dir = os.path.join(tempfile.gettempdir(), "kpopwave_videos")
    os.makedirs(tmp_dir, exist_ok=True)
    outtmpl = os.path.join(tmp_dir, f"{vid_id}{range_suffix}.%(ext)s")
    ffmpeg_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin")

    dl_opts = {
        "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]",
        "ffmpeg_location": ffmpeg_bin,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        **_YT_DLP_JS_OPTS,
    }
    if os.path.exists(_YOUTUBE_COOKIE_FILE):
        dl_opts["cookiefile"] = _YOUTUBE_COOKIE_FILE
    if has_range:
        dl_opts["download_ranges"] = download_range_func(
            [], [(start_time or 0, end_time if end_time is not None else float("inf"))]
        )

    try:
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([yt_url])
    except Exception as exc:
        return jsonify({"ok": False, "error": f"ダウンロードエラー: {str(exc)[:120]}"}), 500

    from video_collector import _find_downloaded_file
    found = _find_downloaded_file(tmp_dir, vid_id + range_suffix)
    if not found:
        return jsonify({"ok": False, "error": "ダウンロードファイルが見つかりません"}), 500

    local_path, ext = found
```

これを以下に置き換える:

```python
    try:
        found = _download_youtube_range(
            yt_url, vid_id,
            (start_time or 0) if has_range else None,
            end_time if has_range else None,
            range_suffix,
        )
    except _YouTubeDownloadNotFoundError:
        return jsonify({"ok": False, "error": "ダウンロードファイルが見つかりません"}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": f"ダウンロードエラー: {str(exc)[:120]}"}), 500

    local_path, ext = found
```

**重要な等価性の確認**: 元のコードは`has_range`が真のとき常に`start_time or 0`（`start_time`が`None`でも`0`）を範囲開始として渡していた。新しい呼び出しも`(start_time or 0) if has_range else None`とすることで、`has_range`が真の場合は必ず非`None`の数値（`0`以上）が`_download_youtube_range`に渡り、ヘルパー内の`if start_time is not None:`分岐が正しく発火する。`has_range`が偽の場合のみ`None`が渡り、範囲指定なし（動画全体）の分岐になる。これは元の`if has_range: dl_opts["download_ranges"] = ...`と完全に等価。

- [ ] **Step 3: 退行がないことをモックで検証**

以下をプロジェクトルートに一時ファイル `verify_task2.py` として保存し実行する（実際のネットワークアクセスは行わない）:

```python
import sys, os, tempfile
sys.path.insert(0, ".")
from unittest.mock import patch, MagicMock

import app as app_module
from app import app, db

def _make_fake_ydl(captured_opts_list):
    class FakeYDL:
        def __init__(self, opts):
            captured_opts_list.append(opts)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def extract_info(self, url, download=False):
            return {"id": "FAKEID123", "title": "テスト動画", "upload_date": "20260101",
                    "uploader": "TestChan", "description": "desc", "thumbnail": None,
                    "view_count": 100}
        def download(self, urls):
            return None
    return FakeYDL

with app.app_context():
    client = app.test_client()

    # --- ケース1: 範囲指定なし（動画全体） ---
    captured = []
    FakeYDL = _make_fake_ydl(captured)
    tmp_dir = os.path.join(tempfile.gettempdir(), "kpopwave_videos")
    os.makedirs(tmp_dir, exist_ok=True)
    fake_file = os.path.join(tmp_dir, "FAKEID123.mp4")
    with open(fake_file, "wb") as f:
        f.write(b"0" * 100)

    with patch("yt_dlp.YoutubeDL", FakeYDL), \
         patch("video_collector._find_downloaded_file", return_value=(fake_file, "mp4")):
        resp = client.post("/api/videos/add-manual", json={
            "url": "https://www.youtube.com/watch?v=FAKEID123",
        })
        data = resp.get_json()
        assert data["ok"] is True, f"範囲なしケース失敗: {data}"
        dl_opts = captured[1]  # 1件目はinfo_opts, 2件目がdl_opts
        assert "download_ranges" not in dl_opts, "範囲なしなのにdownload_rangesが設定されている"
        from database import Article
        art = Article.query.filter_by(url="https://www.youtube.com/watch?v=FAKEID123").first()
        assert art is not None and art.video_file_path == "videos/FAKEID123.mp4"
        db.session.delete(art)
        db.session.commit()
        cleanup_path = os.path.join(os.path.dirname(os.path.abspath(app_module.__file__)), "static", "videos", "FAKEID123.mp4")
        if os.path.exists(cleanup_path):
            os.remove(cleanup_path)

    # --- ケース2: 範囲指定あり（start_timeのみ） ---
    captured2 = []
    FakeYDL2 = _make_fake_ydl(captured2)
    fake_file2 = os.path.join(tmp_dir, "FAKEID123_90-.mp4")
    with open(fake_file2, "wb") as f:
        f.write(b"0" * 100)

    with patch("yt_dlp.YoutubeDL", FakeYDL2), \
         patch("video_collector._find_downloaded_file", return_value=(fake_file2, "mp4")):
        resp = client.post("/api/videos/add-manual", json={
            "url": "https://www.youtube.com/watch?v=FAKEID123",
            "start_time": "1:30",
        })
        data = resp.get_json()
        assert data["ok"] is True, f"範囲ありケース失敗: {data}"
        dl_opts2 = captured2[1]
        assert "download_ranges" in dl_opts2, "範囲ありなのにdownload_rangesが未設定"
        from database import Article
        art2 = Article.query.filter(Article.url.like("%FAKEID123#t=90-%")).first()
        assert art2 is not None, "範囲付きArticleのURLが期待形式でない"
        db.session.delete(art2)
        db.session.commit()
        cleanup_path2 = os.path.join(os.path.dirname(os.path.abspath(app_module.__file__)), "static", "videos", "FAKEID123_90-.mp4")
        if os.path.exists(cleanup_path2):
            os.remove(cleanup_path2)

    # --- ケース3: ダウンロードファイルが見つからない場合のエラーメッセージ確認 ---
    captured3 = []
    FakeYDL3 = _make_fake_ydl(captured3)
    with patch("yt_dlp.YoutubeDL", FakeYDL3), \
         patch("video_collector._find_downloaded_file", return_value=None):
        resp = client.post("/api/videos/add-manual", json={
            "url": "https://www.youtube.com/watch?v=FAKEID123",
        })
        data = resp.get_json()
        assert data == {"ok": False, "error": "ダウンロードファイルが見つかりません"}, f"エラーメッセージ不一致: {data}"

print("OK: Task 2 verification passed")
```

実行: `python verify_task2.py`
期待される出力: `OK: Task 2 verification passed`

検証後、`verify_task2.py`を削除する。

- [ ] **Step 4: コミット**

```bash
git add app.py
git commit -m "refactor: 範囲指定ダウンロード処理を_download_youtube_rangeヘルパーに抽出"
```

---

### Task 3: チャプター検出開始ルート + バックグラウンドジョブ

**Files:**
- Modify: `app.py`（Task 2で追加した`_download_youtube_range`の直後、および`add_video_manual`関数の末尾付近に新規ルート・関数を追加）

**Interfaces:**
- Consumes: `_download_youtube_range`, `_YouTubeDownloadNotFoundError`, `_guess_group_id`（Task 1・2で追加）、`_explicit_account_id`（`app.py:387`）、`_YOUTUBE_COOKIE_FILE`, `_YT_DLP_JS_OPTS`
- Produces: `POST /api/videos/chapters/start` — レスポンス `{"ok": bool, "has_chapters": bool, "job_id": int}` または `{"ok": false, "error": str}`
- Produces: `_run_chapter_job(app, job_id: int)` — バックグラウンドスレッドから呼ばれる
- Produces: `_probe_duration(path: str) -> float | None`

`add_video_manual`関数の直後（`app.py`のこの関数の末尾、次のルート定義の手前）に以下をまとめて追加する。まず現状把握のため、`add_video_manual`の直後に何があるかを確認すること: `grep -n "^def add_video_manual\|^@app.route" app.py` で`add_video_manual`の次のルートを特定し、その直前に挿入する。

- [ ] **Step 1: `_probe_duration`・`_run_chapter_job`・2つのルートを追加**

`add_video_manual`関数の末尾（`return jsonify({"ok": True, ...})`で終わる箇所)の直後、次の`@app.route`定義の直前に、以下をまとめて挿入する:

```python
def _probe_duration(path: str) -> float | None:
    """ffprobeで動画ファイルの長さ(秒)を取得する。失敗時はNone。"""
    import subprocess as _sp

    ffprobe_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin", "ffprobe.exe")
    try:
        result = _sp.run(
            [ffprobe_exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, timeout=30,
        )
        return float(result.stdout.decode("utf-8", errors="replace").strip())
    except Exception:
        return None


def _run_chapter_job(app, job_id):
    import shutil as _shutil
    import time as _time

    with app.app_context():
        job = db.session.get(ChapterJob, job_id)
        if not job:
            return

        clips = ChapterClip.query.filter_by(job_id=job_id).order_by(ChapterClip.chapter_index).all()
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "videos")
        os.makedirs(static_dir, exist_ok=True)
        any_success = False

        for clip in clips:
            clip.status = "downloading"
            db.session.commit()

            start_label = int(clip.start_time)
            end_label = int(clip.end_time) if clip.end_time is not None else ""
            suffix = f"_{start_label}-{end_label}"

            try:
                local_path, ext = _download_youtube_range(
                    job.source_url, job.video_id, clip.start_time, clip.end_time, suffix
                )
                dest_filename = f"{job.video_id}{suffix}.{ext}"
                dest_path = os.path.join(static_dir, dest_filename)
                _shutil.copy2(local_path, dest_path)
                try:
                    os.remove(local_path)
                except Exception:
                    pass

                clip.video_file_path = f"videos/{dest_filename}"
                clip.duration = _probe_duration(dest_path)
                clip.guessed_group_id = _guess_group_id(clip.title)
                clip.status = "done"
                any_success = True
            except Exception as exc:
                clip.status = "failed"
                clip.error_message = str(exc)[:500]
                logger.warning("チャプタークリップ取得失敗: job_id=%d chapter_index=%d error=%s",
                                job_id, clip.chapter_index, clip.error_message)

            db.session.commit()
            _time.sleep(1.5)

        job.status = "done" if any_success else "failed"
        if not any_success:
            job.error_message = "全チャプターのダウンロードに失敗しました"
        db.session.commit()
        logger.info("チャプタージョブ完了: job_id=%d status=%s clips=%d", job_id, job.status, len(clips))


@app.route("/api/videos/chapters/start", methods=["POST"])
def start_chapter_job():
    data = request.get_json(force=True) or {}
    yt_url = (data.get("url") or "").strip()

    if not yt_url:
        return jsonify({"ok": False, "error": "URLを入力してください"}), 400
    if "youtube.com/watch" not in yt_url and "youtu.be/" not in yt_url and "youtube.com/shorts/" not in yt_url:
        return jsonify({"ok": False, "error": "YouTube動画のURLを入力してください"}), 400

    try:
        import yt_dlp
    except ImportError:
        return jsonify({"ok": False, "error": "yt-dlpがインストールされていません"}), 500

    info_opts = {"quiet": True, "no_warnings": True, "ignoreerrors": True, **_YT_DLP_JS_OPTS}
    if os.path.exists(_YOUTUBE_COOKIE_FILE):
        info_opts["cookiefile"] = _YOUTUBE_COOKIE_FILE
    try:
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            full = ydl.extract_info(yt_url, download=False)
        if not full:
            return jsonify({"ok": False, "error": "動画情報を取得できませんでした"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"動画情報取得エラー: {str(exc)[:120]}"}), 500

    vid_id = full.get("id", "")
    if not vid_id:
        return jsonify({"ok": False, "error": "動画IDを取得できませんでした"}), 400

    chapters = full.get("chapters") or []
    if not chapters:
        return jsonify({"ok": True, "has_chapters": False})

    title = (full.get("title") or "YouTube動画")[:500]
    job = ChapterJob(
        source_url=yt_url,
        video_id=vid_id,
        video_title=title,
        thumbnail_url=full.get("thumbnail") or None,
        account_id=_explicit_account_id(data),
        status="processing",
    )
    db.session.add(job)
    db.session.flush()

    for idx, ch in enumerate(chapters):
        ch_start = ch.get("start_time")
        ch_end = ch.get("end_time")
        if ch_start is None:
            continue
        db.session.add(ChapterClip(
            job_id=job.id,
            chapter_index=idx,
            title=(ch.get("title") or f"チャプター{idx + 1}")[:500],
            start_time=float(ch_start),
            end_time=float(ch_end) if ch_end is not None else None,
            duration=(float(ch_end) - float(ch_start)) if ch_end is not None else None,
        ))
    db.session.commit()

    threading.Thread(target=_run_chapter_job, args=(app, job.id), daemon=True).start()

    return jsonify({"ok": True, "has_chapters": True, "job_id": job.id})
```

`threading`は`app.py:6`で既にインポート済み（`import threading`）。

- [ ] **Step 2: モックでの動作検証**

以下をプロジェクトルートに一時ファイル `verify_task3.py` として保存し実行する（実ネットワークアクセスなし）:

```python
import sys, time
sys.path.insert(0, ".")
from unittest.mock import patch

from app import app, db
from database import ChapterJob, ChapterClip, Group

FAKE_CHAPTERS = [
    {"start_time": 0.0, "end_time": 227.0, "title": "Red Velvet (레드벨벳) - Cosmic"},
    {"start_time": 227.0, "end_time": 450.0, "title": "IVE (아이브) - ELEVEN"},
]

class FakeYDLInfo:
    def __init__(self, opts):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def extract_info(self, url, download=False):
        return {"id": "CHAPFAKE1", "title": "テストコンピレーション", "thumbnail": "http://example.com/t.jpg",
                "chapters": FAKE_CHAPTERS}

with app.app_context():
    client = app.test_client()

    # --- ケース1: チャプターありの動画 -> ジョブとクリップが作成される ---
    with patch("yt_dlp.YoutubeDL", FakeYDLInfo), \
         patch("threading.Thread") as mock_thread:
        resp = client.post("/api/videos/chapters/start", json={"url": "https://www.youtube.com/watch?v=CHAPFAKE1"})
        data = resp.get_json()
        assert data["ok"] is True and data["has_chapters"] is True, f"チャプター検出失敗: {data}"
        job_id = data["job_id"]
        mock_thread.assert_called_once()

        job = db.session.get(ChapterJob, job_id)
        assert job is not None and job.video_id == "CHAPFAKE1"
        clips = ChapterClip.query.filter_by(job_id=job_id).order_by(ChapterClip.chapter_index).all()
        assert len(clips) == 2, f"クリップ数不一致: {len(clips)}"
        assert clips[0].title == "Red Velvet (레드벨벳) - Cosmic"
        assert clips[1].start_time == 227.0

        db.session.delete(clips[0]); db.session.delete(clips[1]); db.session.delete(job)
        db.session.commit()

    # --- ケース2: チャプターなしの動画 -> has_chapters: false ---
    class FakeYDLNoChapters(FakeYDLInfo):
        def extract_info(self, url, download=False):
            return {"id": "NOCHAPFAKE", "title": "普通の動画"}

    with patch("yt_dlp.YoutubeDL", FakeYDLNoChapters):
        resp = client.post("/api/videos/chapters/start", json={"url": "https://www.youtube.com/watch?v=NOCHAPFAKE"})
        data = resp.get_json()
        assert data == {"ok": True, "has_chapters": False}, f"チャプターなし判定失敗: {data}"

    # --- ケース3: _run_chapter_job のロジック（ダウンロードをモック） ---
    test_group = Group(name="Red Velvet", normalized_name="red velvet")
    db.session.add(test_group)
    db.session.flush()

    job2 = ChapterJob(source_url="https://www.youtube.com/watch?v=CHAPFAKE1", video_id="CHAPFAKE1", status="processing")
    db.session.add(job2)
    db.session.flush()
    clip_a = ChapterClip(job_id=job2.id, chapter_index=0, title="Red Velvet (레드벨벳) - Cosmic", start_time=0.0, end_time=227.0)
    clip_b = ChapterClip(job_id=job2.id, chapter_index=1, title="NoSuchGroup - Song", start_time=227.0, end_time=None)
    db.session.add_all([clip_a, clip_b])
    db.session.commit()

    sleep_calls = []
    def fake_sleep(sec):
        sleep_calls.append(sec)

    with patch("app._download_youtube_range", return_value=("/tmp/fake.mp4", "mp4")), \
         patch("shutil.copy2"), \
         patch("os.remove"), \
         patch("app._probe_duration", return_value=42.5), \
         patch("time.sleep", side_effect=fake_sleep):
        from app import _run_chapter_job
        _run_chapter_job(app, job2.id)

    job2 = db.session.get(ChapterJob, job2.id)
    assert job2.status == "done", f"ジョブステータス不一致: {job2.status}"
    clip_a = db.session.get(ChapterClip, clip_a.id)
    clip_b = db.session.get(ChapterClip, clip_b.id)
    assert clip_a.status == "done" and clip_a.duration == 42.5
    assert clip_a.guessed_group_id == test_group.id, "グループ自動照合が失敗"
    assert clip_b.guessed_group_id is None, "未登録グループなのにguessed_group_idが設定されている"
    assert len(sleep_calls) == 2 and all(s == 1.5 for s in sleep_calls), f"sleep呼び出し不一致: {sleep_calls}"

    db.session.delete(clip_a); db.session.delete(clip_b); db.session.delete(job2); db.session.delete(test_group)
    db.session.commit()

print("OK: Task 3 verification passed")
```

実行: `python verify_task3.py`
期待される出力: `OK: Task 3 verification passed`

検証後、`verify_task3.py`を削除する。

- [ ] **Step 3: コミット**

```bash
git add app.py
git commit -m "feat: チャプター検出・分割ダウンロードのバックグラウンドジョブを追加"
```

---

### Task 4: 進捗ポーリング + 選択画面

**Files:**
- Modify: `app.py`（Task 3で追加したルート群の末尾に2ルートを追加）
- Create: `templates/chapter_clips.html`

**Interfaces:**
- Consumes: `ChapterJob`, `ChapterClip`, `Group`（DBモデル）
- Produces: `GET /api/videos/chapters/<int:job_id>/status` — `{"status": str, "total": int, "completed": int, "failed": int}`
- Produces: `GET /videos/chapters/<int:job_id>` — `chapter_clips.html`をレンダリング

- [ ] **Step 1: ポーリングルートと画面ルートを追加**

`app.py`の`start_chapter_job`関数（Task 3で追加）の直後に以下を追加する:

```python
@app.route("/api/videos/chapters/<int:job_id>/status")
def chapter_job_status(job_id):
    job = ChapterJob.query.get_or_404(job_id)
    clips = ChapterClip.query.filter_by(job_id=job_id).all()
    completed = sum(1 for c in clips if c.status in ("done", "failed"))
    failed = sum(1 for c in clips if c.status == "failed")
    return jsonify({
        "status": job.status,
        "total": len(clips),
        "completed": completed,
        "failed": failed,
    })


@app.route("/videos/chapters/<int:job_id>")
def chapter_job_view(job_id):
    job = ChapterJob.query.get_or_404(job_id)
    clips = ChapterClip.query.filter_by(job_id=job_id).order_by(ChapterClip.chapter_index).all()
    total = len(clips)
    completed = sum(1 for c in clips if c.status in ("done", "failed"))

    group_names = {}
    group_ids = {c.guessed_group_id for c in clips if c.guessed_group_id}
    if group_ids:
        for g in Group.query.filter(Group.id.in_(group_ids)).all():
            group_names[g.id] = g.name

    return render_template(
        "chapter_clips.html",
        job=job, clips=clips, total=total, completed=completed, group_names=group_names,
    )
```

- [ ] **Step 2: `templates/chapter_clips.html`を新規作成**

```html
{% extends 'base.html' %}
{% block content %}

<div class="d-flex justify-content-between align-items-center mb-4">
  <h4 class="mb-0 fw-bold"><i class="bi bi-collection-play-fill me-2"></i>チャプター分割クリップの確認</h4>
  <a href="/" class="btn btn-outline-secondary btn-sm">ダッシュボードへ戻る</a>
</div>

<div class="card mb-4">
  <div class="card-body">
    <div class="fw-bold mb-1">{{ job.video_title or job.video_id }}</div>
    <div style="font-size:.85rem;color:var(--text-muted)">{{ job.source_url }}</div>
  </div>
</div>

{% if job.status == "processing" %}
<div class="card" id="progress-card">
  <div class="card-body text-center py-5">
    <div class="spinner-border mb-3" role="status"></div>
    <div id="progress-text">クリップをダウンロード中... (<span id="progress-completed">{{ completed }}</span> / <span id="progress-total">{{ total }}</span>)</div>
  </div>
</div>
<script>
(function poll() {
  fetch('/api/videos/chapters/{{ job.id }}/status')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      document.getElementById('progress-completed').textContent = d.completed;
      document.getElementById('progress-total').textContent = d.total;
      if (d.status === 'processing') {
        setTimeout(poll, 2000);
      } else {
        window.location.reload();
      }
    })
    .catch(function() { setTimeout(poll, 3000); });
})();
</script>

{% else %}

{% if job.status == "failed" %}
<div class="alert alert-danger">{{ job.error_message or "ダウンロードに失敗しました" }}</div>
{% endif %}

<form method="post" action="/videos/chapters/{{ job.id }}/confirm">
  <div class="card">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span>クリップを選択（{{ clips | selectattr("status", "equalto", "done") | list | length }} 件が利用可能）</span>
      <div>
        <button type="button" class="btn btn-sm btn-outline-secondary" onclick="toggleAll(true)">全選択</button>
        <button type="button" class="btn btn-sm btn-outline-secondary" onclick="toggleAll(false)">全解除</button>
      </div>
    </div>
    <div class="table-responsive">
      <table class="table table-hover mb-0" style="font-size:.85rem">
        <thead>
          <tr>
            <th style="width:2.5rem"></th>
            <th style="width:5rem">サムネイル</th>
            <th>タイトル</th>
            <th style="width:6rem">長さ</th>
            <th style="width:10rem">グループ</th>
            <th style="width:6rem">状態</th>
          </tr>
        </thead>
        <tbody>
          {% for c in clips %}
          <tr>
            <td>
              {% if c.status == "done" %}
              <input type="checkbox" class="clip-checkbox" name="clip_ids" value="{{ c.id }}" checked>
              {% endif %}
            </td>
            <td>
              {% if job.thumbnail_url %}
              <img src="{{ job.thumbnail_url }}" style="width:64px;height:36px;object-fit:cover;border-radius:4px">
              {% endif %}
            </td>
            <td>{{ c.title }}</td>
            <td>
              {% if c.duration %}
              {{ (c.duration // 60) | int }}:{{ '%02d' % (c.duration % 60) }}
              {% endif %}
            </td>
            <td>
              {% if c.guessed_group_id and group_names.get(c.guessed_group_id) %}
              <span class="badge bg-info-subtle text-info-emphasis">{{ group_names[c.guessed_group_id] }}</span>
              {% else %}
              <span style="color:var(--text-muted)">未検出</span>
              {% endif %}
            </td>
            <td>
              {% if c.status == "done" %}<span class="badge badge-posted">OK</span>
              {% elif c.status == "failed" %}<span class="badge badge-failed" title="{{ c.error_message }}">失敗</span>
              {% else %}<span class="badge badge-pending">{{ c.status }}</span>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  <div class="mt-3">
    <button type="submit" class="btn btn-accent px-4">
      <i class="bi bi-check-circle-fill me-1"></i>選択したクリップを承認待ちに追加
    </button>
  </div>
</form>

<script>
function toggleAll(checked) {
  document.querySelectorAll('.clip-checkbox').forEach(function(cb) { cb.checked = checked; });
}
</script>
{% endif %}

{% endblock %}
```

`base.html`のCSSクラス（`badge-posted`・`badge-failed`・`badge-pending`・`btn-accent`）は`templates/index.html`・`templates/pending.html`と同一のものを再利用しているため、既存スタイルとの整合が取れる。

- [ ] **Step 3: 検証**

以下をプロジェクトルートに一時ファイル `verify_task4.py` として保存し実行する:

```python
import sys
sys.path.insert(0, ".")
from app import app, db
from database import ChapterJob, ChapterClip, Group

with app.app_context():
    client = app.test_client()

    # --- processing中はポーリング画面が表示され、進捗が返る ---
    job = ChapterJob(source_url="https://example.com/x", video_id="vid1", status="processing")
    db.session.add(job)
    db.session.flush()
    c1 = ChapterClip(job_id=job.id, chapter_index=0, title="A", start_time=0.0, end_time=10.0, status="done")
    c2 = ChapterClip(job_id=job.id, chapter_index=1, title="B", start_time=10.0, end_time=20.0, status="pending")
    db.session.add_all([c1, c2])
    db.session.commit()

    resp = client.get(f"/api/videos/chapters/{job.id}/status")
    data = resp.get_json()
    assert data == {"status": "processing", "total": 2, "completed": 1, "failed": 0}, f"進捗レスポンス不一致: {data}"

    resp2 = client.get(f"/videos/chapters/{job.id}")
    assert resp2.status_code == 200
    assert b"progress-card" in resp2.data or "progress-card".encode() in resp2.data

    # --- done後は選択画面が表示される ---
    job.status = "done"
    c2.status = "done"
    db.session.commit()
    resp3 = client.get(f"/videos/chapters/{job.id}")
    assert resp3.status_code == 200
    assert b"clip_ids" in resp3.data, "選択チェックボックスが表示されていない"

    db.session.delete(c1); db.session.delete(c2); db.session.delete(job)
    db.session.commit()

print("OK: Task 4 verification passed")
```

実行: `python verify_task4.py`
期待される出力: `OK: Task 4 verification passed`

検証後、`verify_task4.py`を削除する。

- [ ] **Step 4: コミット**

```bash
git add app.py templates/chapter_clips.html
git commit -m "feat: チャプタージョブの進捗表示・クリップ選択画面を追加"
```

---

### Task 5: 選択確定ルート（承認待ちへの登録・非選択分のクリーンアップ）

**Files:**
- Modify: `app.py`（Task 4で追加した`chapter_job_view`の直後にルートを追加）

**Interfaces:**
- Consumes: `ChapterJob`, `ChapterClip`, `Article`（DBモデル）
- Produces: `POST /videos/chapters/<int:job_id>/confirm` — フォームの`clip_ids`（複数値）を受理し、`/pending`へリダイレクト

- [ ] **Step 1: 確定ルートを追加**

`app.py`の`chapter_job_view`関数（Task 4で追加）の直後に以下を追加する:

```python
@app.route("/videos/chapters/<int:job_id>/confirm", methods=["POST"])
def chapter_job_confirm(job_id):
    job = ChapterJob.query.get_or_404(job_id)
    selected_ids = set()
    for raw_id in request.form.getlist("clip_ids"):
        try:
            selected_ids.add(int(raw_id))
        except (TypeError, ValueError):
            pass

    clips = ChapterClip.query.filter_by(job_id=job_id).all()
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    added = 0
    for clip in clips:
        if clip.id in selected_ids and clip.status == "done" and clip.video_file_path:
            start_label = int(clip.start_time)
            end_label = int(clip.end_time) if clip.end_time is not None else ""
            article = Article(
                feed_source=f"YouTube動画: {job.video_title or job.video_id}",
                title=clip.title[:500],
                url=f"{job.source_url}#t={start_label}-{end_label}",
                thumbnail_url=job.thumbnail_url,
                status="pending",
                content_type="video",
                video_file_path=clip.video_file_path,
                group_id=clip.guessed_group_id,
                account_id=job.account_id,
            )
            db.session.add(article)
            added += 1
        elif clip.video_file_path:
            full_path = os.path.join(static_dir, clip.video_file_path)
            if os.path.exists(full_path):
                try:
                    os.remove(full_path)
                except OSError:
                    pass

    ChapterClip.query.filter_by(job_id=job_id).delete(synchronize_session=False)
    db.session.delete(job)
    db.session.commit()

    flash(f"{added} 件のクリップを承認待ちに追加しました", "success")
    return redirect(url_for("pending"))
```

- [ ] **Step 2: 検証**

以下をプロジェクトルートに一時ファイル `verify_task5.py` として保存し実行する:

```python
import sys, os
sys.path.insert(0, ".")
from app import app, db
from database import ChapterJob, ChapterClip, Article, Group

with app.app_context():
    client = app.test_client()

    static_dir = os.path.join(os.path.dirname(os.path.abspath(app.root_path)), "kpopwave-tool", "static")
    if not os.path.isdir(static_dir):
        static_dir = os.path.join(app.root_path, "static")
    videos_dir = os.path.join(static_dir, "videos")
    os.makedirs(videos_dir, exist_ok=True)

    group = Group(name="TestConfirmGroup", normalized_name="testconfirmgroup")
    db.session.add(group)
    db.session.flush()

    job = ChapterJob(source_url="https://www.youtube.com/watch?v=CONFIRMTEST", video_id="CONFIRMTEST",
                      video_title="テストジョブ", status="done")
    db.session.add(job)
    db.session.flush()

    kept_file = os.path.join(videos_dir, "CONFIRMTEST_0-10.mp4")
    discarded_file = os.path.join(videos_dir, "CONFIRMTEST_10-20.mp4")
    with open(kept_file, "wb") as f: f.write(b"0")
    with open(discarded_file, "wb") as f: f.write(b"0")

    clip_keep = ChapterClip(job_id=job.id, chapter_index=0, title="Kept Song", start_time=0.0, end_time=10.0,
                             status="done", video_file_path="videos/CONFIRMTEST_0-10.mp4", guessed_group_id=group.id)
    clip_discard = ChapterClip(job_id=job.id, chapter_index=1, title="Discarded Song", start_time=10.0, end_time=20.0,
                                status="done", video_file_path="videos/CONFIRMTEST_10-20.mp4")
    db.session.add_all([clip_keep, clip_discard])
    db.session.commit()
    keep_id = clip_keep.id

    resp = client.post(f"/videos/chapters/{job.id}/confirm", data={"clip_ids": [str(keep_id)]})
    assert resp.status_code == 302 and "/pending" in resp.headers.get("Location", ""), f"リダイレクト先不一致: {resp.headers.get('Location')}"

    art = Article.query.filter(Article.url.like("%CONFIRMTEST#t=0-10%")).first()
    assert art is not None, "選択したクリップのArticleが作成されていない"
    assert art.group_id == group.id, "group_idが引き継がれていない"
    assert art.video_file_path == "videos/CONFIRMTEST_0-10.mp4"

    assert os.path.exists(kept_file), "選択したクリップのファイルが削除されてしまった"
    assert not os.path.exists(discarded_file), "非選択クリップのファイルが削除されていない"

    assert ChapterClip.query.filter_by(job_id=job.id).count() == 0, "ChapterClipが削除されていない"
    assert db.session.get(ChapterJob, job.id) is None, "ChapterJobが削除されていない"

    db.session.delete(art)
    db.session.delete(group)
    db.session.commit()
    if os.path.exists(kept_file):
        os.remove(kept_file)

print("OK: Task 5 verification passed")
```

実行: `python verify_task5.py`
期待される出力: `OK: Task 5 verification passed`

検証後、`verify_task5.py`を削除する。

- [ ] **Step 3: コミット**

```bash
git add app.py
git commit -m "feat: チャプタークリップ選択確定ルート（承認待ち登録・非選択分クリーンアップ）を追加"
```

---

### Task 6: フロントエンド統合

**Files:**
- Modify: `templates/index.html:164-210`（`addVideoManual()`関数）

**Interfaces:**
- Consumes: `POST /api/videos/chapters/start`（Task 3）、`GET /videos/chapters/<job_id>`（Task 4）、`POST /api/videos/add-manual`（既存）

- [ ] **Step 1: `addVideoManual()`を分岐対応に変更**

`templates/index.html:164-210`の現在の内容（`addVideoManual`関数全体）:

```javascript
function addVideoManual() {
  var input  = document.getElementById('add-video-input');
  var btn    = document.getElementById('add-video-btn');
  var result = document.getElementById('add-video-result');
  var startInput = document.getElementById('add-video-start');
  var endInput   = document.getElementById('add-video-end');
  var url = input.value.trim();
  if (!url) { input.focus(); return; }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>ダウンロード中...';
  result.innerHTML = '<span style="color:var(--text-muted)"><span class="spinner-border spinner-border-sm me-1"></span>動画をダウンロード中... しばらくお待ちください</span>';

  fetch('/api/videos/add-manual', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      url: url,
      account_id: CURRENT_ACCOUNT_ID,
      start_time: startInput.value.trim(),
      end_time: endInput.value.trim()
    })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-camera-video-fill me-1"></i>動画を追加';
    if (d.ok) {
      input.value = '';
      startInput.value = '';
      endInput.value = '';
      result.innerHTML =
        '<span style="color:var(--accent3)"><i class="bi bi-check-circle-fill me-1"></i>' +
        '追加しました！ <strong>' + escHtml(d.title) + '</strong>' +
        ' — <a href="/pending">承認待ち画面で確認してください</a></span>';
    } else {
      result.innerHTML =
        '<span style="color:var(--accent)"><i class="bi bi-exclamation-circle-fill me-1"></i>' +
        escHtml(d.error) + '</span>';
    }
  })
  .catch(function() {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-camera-video-fill me-1"></i>動画を追加';
    result.innerHTML = '<span style="color:var(--accent)">通信エラーが発生しました</span>';
  });
}
```

これを以下に置き換える:

```javascript
function addVideoManual() {
  var input  = document.getElementById('add-video-input');
  var btn    = document.getElementById('add-video-btn');
  var result = document.getElementById('add-video-result');
  var startInput = document.getElementById('add-video-start');
  var endInput   = document.getElementById('add-video-end');
  var url = input.value.trim();
  if (!url) { input.focus(); return; }

  var startVal = startInput.value.trim();
  var endVal   = endInput.value.trim();

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>ダウンロード中...';
  result.innerHTML = '<span style="color:var(--text-muted)"><span class="spinner-border spinner-border-sm me-1"></span>動画をダウンロード中... しばらくお待ちください</span>';

  if (!startVal && !endVal) {
    fetch('/api/videos/chapters/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url: url, account_id: CURRENT_ACCOUNT_ID })
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok && d.has_chapters) {
        window.location.href = '/videos/chapters/' + d.job_id;
        return;
      }
      if (d.ok && !d.has_chapters) {
        submitAddVideoManual(url, startVal, endVal);
        return;
      }
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-camera-video-fill me-1"></i>動画を追加';
      result.innerHTML =
        '<span style="color:var(--accent)"><i class="bi bi-exclamation-circle-fill me-1"></i>' +
        escHtml(d.error) + '</span>';
    })
    .catch(function() {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-camera-video-fill me-1"></i>動画を追加';
      result.innerHTML = '<span style="color:var(--accent)">通信エラーが発生しました</span>';
    });
    return;
  }

  submitAddVideoManual(url, startVal, endVal);
}

function submitAddVideoManual(url, startVal, endVal) {
  var input  = document.getElementById('add-video-input');
  var btn    = document.getElementById('add-video-btn');
  var result = document.getElementById('add-video-result');
  var startInput = document.getElementById('add-video-start');
  var endInput   = document.getElementById('add-video-end');

  fetch('/api/videos/add-manual', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      url: url,
      account_id: CURRENT_ACCOUNT_ID,
      start_time: startVal,
      end_time: endVal
    })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-camera-video-fill me-1"></i>動画を追加';
    if (d.ok) {
      input.value = '';
      startInput.value = '';
      endInput.value = '';
      result.innerHTML =
        '<span style="color:var(--accent3)"><i class="bi bi-check-circle-fill me-1"></i>' +
        '追加しました！ <strong>' + escHtml(d.title) + '</strong>' +
        ' — <a href="/pending">承認待ち画面で確認してください</a></span>';
    } else {
      result.innerHTML =
        '<span style="color:var(--accent)"><i class="bi bi-exclamation-circle-fill me-1"></i>' +
        escHtml(d.error) + '</span>';
    }
  })
  .catch(function() {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-camera-video-fill me-1"></i>動画を追加';
    result.innerHTML = '<span style="color:var(--accent)">通信エラーが発生しました</span>';
  });
}
```

開始・終了のいずれかに値がある場合は`submitAddVideoManual`が直接呼ばれ、既存の`/api/videos/add-manual`フローと完全に同一の挙動になる（変更なし）。両方空欄の場合のみ`/api/videos/chapters/start`を経由する。

- [ ] **Step 2: 手動検証（ブラウザ操作）**

Flask開発サーバーを起動し、ブラウザで以下を確認する:
1. 開始・終了欄を両方空欄のまま、チャプターのない通常の動画URLを追加 → 従来通り即座に承認待ちに追加されること
2. 開始欄のみに値を入れて動画URLを追加 → チャプター検出を経由せず、従来通り単一範囲でダウンロードされること
3. （Task 7の実動画テストで）チャプターのある動画URLを両方空欄で追加 → `/videos/chapters/<job_id>`にリダイレクトされ、進捗表示→選択画面が表示されること

- [ ] **Step 3: コミット**

```bash
git add templates/index.html
git commit -m "feat: URL手動追加フォームをチャプター検出フローに対応"
```

---

### Task 7: 実動画での軽量な動作確認（コントローラーが直接実施）

このタスクはサブエージェントに委譲せず、コントローラー（プランを実行しているセッション）が直接、実ネットワークアクセスを伴う最終確認として行う。前回までの調査でYouTube側のセッション認識が短時間の大量リクエストで不安定になることが判明しているため、**チャプター全体（既知の実例では45個）をダウンロードすることはせず、先頭2〜3チャプターに限定する一時的な検証**を行う。

- [ ] **Step 1: Flask開発サーバーを起動**

`python app.py`（または既存の起動手順）でローカルサーバーを起動する。

- [ ] **Step 2: `_download_youtube_range`と`chapters`取得は既存機構の組み合わせのため、実ネットワーク経由の`POST /api/videos/chapters/start`を1回だけ、実際の長尺コンピレーション動画URLに対して呼び出す**

チャプター検出（`extract_info`のみ、ダウンロードなし）が成功し、`ChapterJob`/`ChapterClip`が正しい件数で作成され、バックグラウンドジョブが起動することを確認する。

- [ ] **Step 3: バックグラウンドジョブが最初の2〜3クリップを処理し終えた時点で、進捗ポーリング・選択画面が正しく表示されることを`/videos/chapters/<job_id>`で確認する**

全チャプター処理完了を待つ必要はない。先頭数クリップが`done`になった時点で選択画面のチェックボックス・サムネイル・長さ・グループ自動タグ付け結果の表示を目視確認すれば十分。確認後、DB上の該当`ChapterJob`・`ChapterClip`・ダウンロード済みファイルは手動でクリーンアップする（本番の承認待ちキューを汚さないため、選択画面で全て未選択のまま確定するか、DBから直接削除する）。

- [ ] **Step 4: 選択→確定フローを1回実施し、選択したクリップが承認待ち（`/pending`）に正しいタイトル・グループタグ付きで表示されることを確認する**

確認後、テストで作成した`Article`・動画ファイルは削除する（本番データとして残さない）。

- [ ] **Step 5: 結果をユーザーに報告する**

チャプター検出・分割ダウンロード・選択画面・グループ自動タグ付けそれぞれについて、実際に確認できたこと／できなかったことを正直に報告する。

---

## セルフレビュー結果

- **仕様網羅性**: 設計書（`docs/superpowers/specs/2026-08-19-chapter-based-clip-extraction-design.md`）の各項目（DBモデル・共通化・グループ自動照合・バックグラウンドジョブ・4ルート・URL/ファイル名一意化・フロントエンド分岐・実動画での軽量検証）はすべてTask 1〜7でカバーされている。
- **プレースホルダ**: 各タスクのコードはすべて完全な実装であり、`TODO`等は含まれない。
- **型・シグネチャの一貫性**: `_download_youtube_range`のシグネチャ（Task 2で定義）は`_run_chapter_job`（Task 3）・`add_video_manual`（Task 2）の両方で同一の`(yt_url, vid_id, start_time, end_time, suffix) -> (path, ext)`として使われている。`_guess_group_id`（Task 1で定義、`app.py`）は`_run_chapter_job`（Task 3）から同一シグネチャで呼ばれている。`ChapterJob`/`ChapterClip`のフィールド名はTask 1で定義したものをTask 3〜5で一貫して使用している。
