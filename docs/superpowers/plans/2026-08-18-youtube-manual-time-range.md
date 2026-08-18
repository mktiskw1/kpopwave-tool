# YouTube手動追加の時間範囲指定ダウンロード Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ダッシュボードの「YouTube動画URLを追加」フォームに任意の開始・終了時刻入力を追加し、指定があればyt-dlpの`download_ranges`機能でその範囲だけをダウンロードできるようにする。これにより長尺動画（例: 2時間22分）を分割して収集できるようにし、「ダウンロードファイルが見つかりません」エラーを回避する。

**Architecture:** `app.py`の`add_video_manual()`に、時刻文字列パース・`yt_dlp.utils.download_range_func`を使った範囲ダウンロード・範囲指定時のURL/ファイル名の一意化・重複チェックのスキップを追加する。`templates/index.html`のフォームに開始・終了の任意入力欄を追加する。範囲未指定時は完全に既存動作を維持する。

**Tech Stack:** Python 3.x / Flask / yt-dlp / vanilla JS

## Global Constraints

- 対象ファイルは`app.py`・`templates/index.html`のみ
- 範囲未指定時の動作（URL・ファイル名・重複チェック）は一切変更しない
- `--force-keyframes-at-cuts`相当のオプション（`force_keyframes_at_cuts`）は設定しない（キーフレーム単位のズレを許容する）
- `video_collector.py`の自動収集ロジック（チャンネル巡回収集）は変更しない
- コメントは原則書かない（WHYが非自明な場合のみ1行）
- Shellコマンドの実行環境はWindows。Bashツールを使う場合はGit Bash構文（`$VAR`、`&&`可）、PowerShellツールを使う場合はPowerShell構文（`$env:VAR`、`&&`不可）に注意する
- このプロジェクトにはpytest等のテストフレームワークが存在しない。検証は`venv/Scripts/python.exe -c "..."`によるロジック確認・`unittest.mock.patch`によるyt-dlp呼び出しのモック化・実際のyt-dlpを使った軽量な実動画テストで行う
- **重要**: `app.py`は`app = create_app()`をモジュール読み込み時に実行する。`test_client()`での検証には必ず`from app import app`を使うこと
- 開発サーバー（ポート5000）は既に起動中のプロセスが存在する可能性が高い。新たに`python app.py`/`python run.py`を起動しないこと（`run.py`のファイルウォッチャーがコード変更を検知して自動再起動する）

---

## ファイル構成

| ファイル | 変更内容 |
|---|---|
| `app.py` | `_parse_time_input`ヘルパー追加、`add_video_manual()`に範囲指定ダウンロード対応を追加 |
| `templates/index.html` | YouTube動画追加フォームに開始・終了の任意入力欄を追加、JSを対応させる |

---

### Task 1: 時間範囲指定ダウンロード機能の追加

**Files:**
- Modify: `app.py`（`add_video_manual()`ルートとその直前）
- Modify: `templates/index.html`（YouTube動画追加フォーム・`addVideoManual()`）

**Interfaces:**
- Consumes: `yt_dlp.utils.download_range_func`（yt-dlpの既存公開API）
- Produces: `_parse_time_input(s: str | None) -> float | None`（不正な形式は`ValueError`）。`POST /api/videos/add-manual`が新たに任意の`start_time`・`end_time`（"H:MM:SS"/"MM:SS"/"SS"形式の文字列）を受理する

- [ ] **Step 1: `app.py`に`_parse_time_input`ヘルパーを追加し、`add_video_manual()`を書き換える**

`app.py`内、以下のブロック全体（目印。`add_video_manual`ルート全体）:

```python
@app.route("/api/videos/add-manual", methods=["POST"])
def add_video_manual():
    import shutil, tempfile

    data = request.get_json(force=True) or {}
    yt_url = (data.get("url") or "").strip()

    if not yt_url:
        return jsonify({"ok": False, "error": "URLを入力してください"}), 400
    if "youtube.com/watch" not in yt_url and "youtu.be/" not in yt_url and "youtube.com/shorts/" not in yt_url:
        return jsonify({"ok": False, "error": "YouTube動画のURLを入力してください"}), 400

    if Article.query.filter(
        Article.url == yt_url,
        Article.status.in_(["pending", "queued"])
    ).first():
        return jsonify({"ok": False, "error": "この動画はすでに承認待ち・キュー中です"}), 400

    try:
        import yt_dlp
    except ImportError:
        return jsonify({"ok": False, "error": "yt-dlpがインストールされていません"}), 500

    info_opts = {"quiet": True, "no_warnings": True, "ignoreerrors": True}
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

    title = (full.get("title") or "YouTube動画")[:500]

    tmp_dir = os.path.join(tempfile.gettempdir(), "kpopwave_videos")
    os.makedirs(tmp_dir, exist_ok=True)
    outtmpl = os.path.join(tmp_dir, f"{vid_id}.%(ext)s")
    ffmpeg_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin")

    dl_opts = {
        "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]",
        "ffmpeg_location": ffmpeg_bin,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
    }

    try:
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([yt_url])
    except Exception as exc:
        return jsonify({"ok": False, "error": f"ダウンロードエラー: {str(exc)[:120]}"}), 500

    from video_collector import _find_downloaded_file
    found = _find_downloaded_file(tmp_dir, vid_id)
    if not found:
        return jsonify({"ok": False, "error": "ダウンロードファイルが見つかりません"}), 500

    local_path, ext = found
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "videos")
    os.makedirs(static_dir, exist_ok=True)
    dest_filename = f"{vid_id}.{ext}"
    dest_path = os.path.join(static_dir, dest_filename)

    try:
        shutil.copy2(local_path, dest_path)
        try:
            os.remove(local_path)
        except Exception:
            pass
    except Exception as exc:
        return jsonify({"ok": False, "error": f"ファイルコピーエラー: {str(exc)[:120]}"}), 500

    ud = full.get("upload_date", "")
    published_at = None
    if ud and len(ud) == 8:
        try:
            published_at = datetime.strptime(ud, "%Y%m%d")
        except Exception:
            pass

    uploader = full.get("uploader") or full.get("channel") or "YouTube"
    article = Article(
        feed_source=f"YouTube動画: {uploader}",
        title=title,
        url=yt_url,
        published_at=published_at,
        raw_content=(full.get("description") or "")[:5000],
        thumbnail_url=full.get("thumbnail") or None,
        status="pending",
        content_type="video",
        video_file_path=f"videos/{dest_filename}",
        view_count=full.get("view_count"),
        account_id=_explicit_account_id(data),
    )
    db.session.add(article)
    db.session.commit()

    logger.info("動画手動追加: %s (%s)", title[:60], yt_url)
    return jsonify({"ok": True, "title": title})
```

これを以下に置き換える:

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
def add_video_manual():
    import shutil, tempfile
    from yt_dlp.utils import download_range_func

    data = request.get_json(force=True) or {}
    yt_url = (data.get("url") or "").strip()

    if not yt_url:
        return jsonify({"ok": False, "error": "URLを入力してください"}), 400
    if "youtube.com/watch" not in yt_url and "youtu.be/" not in yt_url and "youtube.com/shorts/" not in yt_url:
        return jsonify({"ok": False, "error": "YouTube動画のURLを入力してください"}), 400

    try:
        start_time = _parse_time_input(data.get("start_time"))
        end_time = _parse_time_input(data.get("end_time"))
    except ValueError:
        return jsonify({"ok": False, "error": "開始・終了時刻の形式が不正です（例: 1:00:00 または 5:30）"}), 400
    if start_time is not None and end_time is not None and end_time <= start_time:
        return jsonify({"ok": False, "error": "終了時刻は開始時刻より後にしてください"}), 400
    has_range = start_time is not None or end_time is not None

    if not has_range and Article.query.filter(
        Article.url == yt_url,
        Article.status.in_(["pending", "queued"])
    ).first():
        return jsonify({"ok": False, "error": "この動画はすでに承認待ち・キュー中です"}), 400

    try:
        import yt_dlp
    except ImportError:
        return jsonify({"ok": False, "error": "yt-dlpがインストールされていません"}), 500

    info_opts = {"quiet": True, "no_warnings": True, "ignoreerrors": True}
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

    title = (full.get("title") or "YouTube動画")[:500]

    # 範囲指定時は動画IDのみでは同一動画の別範囲と衝突するため、範囲をファイル名・URLに含めて一意化する
    range_suffix = ""
    if has_range:
        start_label = int(start_time or 0)
        end_label = int(end_time) if end_time is not None else ""
        range_suffix = f"_{start_label}-{end_label}"

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
    }
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
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "videos")
    os.makedirs(static_dir, exist_ok=True)
    dest_filename = f"{vid_id}{range_suffix}.{ext}"
    dest_path = os.path.join(static_dir, dest_filename)

    try:
        shutil.copy2(local_path, dest_path)
        try:
            os.remove(local_path)
        except Exception:
            pass
    except Exception as exc:
        return jsonify({"ok": False, "error": f"ファイルコピーエラー: {str(exc)[:120]}"}), 500

    ud = full.get("upload_date", "")
    published_at = None
    if ud and len(ud) == 8:
        try:
            published_at = datetime.strptime(ud, "%Y%m%d")
        except Exception:
            pass

    uploader = full.get("uploader") or full.get("channel") or "YouTube"
    article_url = f"{yt_url}#t={range_suffix[1:]}" if has_range else yt_url
    article = Article(
        feed_source=f"YouTube動画: {uploader}",
        title=title,
        url=article_url,
        published_at=published_at,
        raw_content=(full.get("description") or "")[:5000],
        thumbnail_url=full.get("thumbnail") or None,
        status="pending",
        content_type="video",
        video_file_path=f"videos/{dest_filename}",
        view_count=full.get("view_count"),
        account_id=_explicit_account_id(data),
    )
    db.session.add(article)
    db.session.commit()

    logger.info("動画手動追加: %s (%s)", title[:60], yt_url)
    return jsonify({"ok": True, "title": title})
```

- [ ] **Step 2: `templates/index.html`のYouTube動画追加フォームに開始・終了入力欄を追加**

`templates/index.html:43-58`（目印）:

```html
<!-- YouTube動画手動追加 -->
<div class="card mb-4">
  <div class="card-header"><i class="bi bi-camera-video-fill me-2"></i>YouTube動画URLを追加（動画投稿用）</div>
  <div class="card-body">
    <div class="d-flex gap-2">
      <input type="url" id="add-video-input" class="form-control"
             placeholder="https://www.youtube.com/watch?v=... または /shorts/..."
             style="font-size:.88rem"
             onkeydown="if(event.key==='Enter') addVideoManual()">
      <button class="btn btn-accent px-3 flex-shrink-0" onclick="addVideoManual()" id="add-video-btn">
        <i class="bi bi-camera-video-fill me-1"></i>動画を追加
      </button>
    </div>
    <div id="add-video-result" class="mt-2" style="font-size:.83rem;min-height:1.2em"></div>
  </div>
</div>
```

これを以下に置き換える:

```html
<!-- YouTube動画手動追加 -->
<div class="card mb-4">
  <div class="card-header"><i class="bi bi-camera-video-fill me-2"></i>YouTube動画URLを追加（動画投稿用）</div>
  <div class="card-body">
    <div class="d-flex gap-2">
      <input type="url" id="add-video-input" class="form-control"
             placeholder="https://www.youtube.com/watch?v=... または /shorts/..."
             style="font-size:.88rem"
             onkeydown="if(event.key==='Enter') addVideoManual()">
      <button class="btn btn-accent px-3 flex-shrink-0" onclick="addVideoManual()" id="add-video-btn">
        <i class="bi bi-camera-video-fill me-1"></i>動画を追加
      </button>
    </div>
    <div class="d-flex align-items-center gap-2 mt-2" style="font-size:.82rem;color:var(--text-muted)">
      <span>長尺動画は範囲指定（任意）:</span>
      <input type="text" id="add-video-start" class="form-control form-control-sm" style="width:130px"
             placeholder="開始 例: 1:00:00">
      <span>〜</span>
      <input type="text" id="add-video-end" class="form-control form-control-sm" style="width:130px"
             placeholder="終了 例: 2:22:00">
    </div>
    <div id="add-video-result" class="mt-2" style="font-size:.83rem;min-height:1.2em"></div>
  </div>
</div>
```

- [ ] **Step 3: `addVideoManual()`を範囲指定に対応させる**

`templates/index.html`内、以下のブロック（目印。`addVideoManual`関数全体）:

```javascript
function addVideoManual() {
  var input  = document.getElementById('add-video-input');
  var btn    = document.getElementById('add-video-btn');
  var result = document.getElementById('add-video-result');
  var url = input.value.trim();
  if (!url) { input.focus(); return; }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>ダウンロード中...';
  result.innerHTML = '<span style="color:var(--text-muted)"><span class="spinner-border spinner-border-sm me-1"></span>動画をダウンロード中... しばらくお待ちください</span>';

  fetch('/api/videos/add-manual', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: url, account_id: CURRENT_ACCOUNT_ID})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-camera-video-fill me-1"></i>動画を追加';
    if (d.ok) {
      input.value = '';
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

- [ ] **Step 4: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile app.py
```
Expected: エラーなし。

- [ ] **Step 5: `_parse_time_input`の単体動作を確認する**

```bash
venv/Scripts/python.exe -c "
from app import _parse_time_input

print('1:00:00 ->', _parse_time_input('1:00:00'))
print('2:22:00 ->', _parse_time_input('2:22:00'))
print('5:30 ->', _parse_time_input('5:30'))
print('90 ->', _parse_time_input('90'))
print('空文字 ->', _parse_time_input(''))
print('None ->', _parse_time_input(None))
try:
    _parse_time_input('abc')
    print('abc -> エラーにならなかった（NG）')
except ValueError:
    print('abc -> ValueError（OK）')
try:
    _parse_time_input('1:2:3:4')
    print('1:2:3:4 -> エラーにならなかった（NG）')
except ValueError:
    print('1:2:3:4 -> ValueError（OK）')
"
```
Expected: `1:00:00 -> 3600.0`、`2:22:00 -> 8520.0`、`5:30 -> 330.0`、`90 -> 90.0`、`空文字 -> None`、`None -> None`、`abc -> ValueError（OK）`、`1:2:3:4 -> ValueError（OK）`。

- [ ] **Step 6: 範囲指定時のURL・ファイル名の一意化ロジックとdownload_rangesの構築をモックで確認する**

```bash
venv/Scripts/python.exe -c "
from unittest.mock import patch, MagicMock
from app import app

fake_info = {
    'id': 'testvid123',
    'title': 'テスト動画',
    'uploader': 'テストチャンネル',
    'upload_date': '20260101',
    'description': '',
    'thumbnail': None,
    'view_count': 100,
}

captured_dl_opts = {}

class FakeYoutubeDL:
    def __init__(self, opts):
        captured_dl_opts.clear()
        captured_dl_opts.update(opts)
        self.opts = opts
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def extract_info(self, url, download=False):
        return fake_info
    def download(self, urls):
        return 0

with patch('yt_dlp.YoutubeDL', FakeYoutubeDL), \
     patch('video_collector._find_downloaded_file', return_value=('/tmp/fake_testvid123_60-3600.mp4', 'mp4')), \
     patch('shutil.copy2'), \
     patch('os.remove'), \
     patch('os.makedirs'):
    client = app.test_client()
    r = client.post('/api/videos/add-manual', json={
        'url': 'https://www.youtube.com/watch?v=testvid123',
        'start_time': '1:00',
        'end_time': '1:00:00',
    })
    print('status:', r.status_code, r.get_json())
    print('download_ranges設定あり:', 'download_ranges' in captured_dl_opts)
    print('outtmplに範囲サフィックスあり:', '_60-3600' in captured_dl_opts.get('outtmpl', ''))

from database import Article, db
with app.app_context():
    art = Article.query.filter(Article.url.like('%testvid123%')).order_by(Article.id.desc()).first()
    print('保存されたURL:', art.url if art else None)
    print('保存されたvideo_file_path:', art.video_file_path if art else None)
    if art:
        db.session.delete(art)
        db.session.commit()
"
```
Expected: `status: 200`、`download_ranges設定あり: True`、`outtmplに範囲サフィックスあり: True`、`保存されたURL`が`...#t=60-3600`で終わる、`保存されたvideo_file_path`が`videos/testvid123_60-3600.mp4`。

- [ ] **Step 7: 範囲未指定時の動作が完全に既存のままであることを確認する**

```bash
venv/Scripts/python.exe -c "
from unittest.mock import patch
from app import app

fake_info = {
    'id': 'testvid456', 'title': 'テスト動画2', 'uploader': 'テストチャンネル',
    'upload_date': '20260101', 'description': '', 'thumbnail': None, 'view_count': 50,
}
captured_dl_opts = {}

class FakeYoutubeDL:
    def __init__(self, opts):
        captured_dl_opts.clear()
        captured_dl_opts.update(opts)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def extract_info(self, url, download=False): return fake_info
    def download(self, urls): return 0

with patch('yt_dlp.YoutubeDL', FakeYoutubeDL), \
     patch('video_collector._find_downloaded_file', return_value=('/tmp/fake_testvid456.mp4', 'mp4')), \
     patch('shutil.copy2'), patch('os.remove'), patch('os.makedirs'):
    client = app.test_client()
    r = client.post('/api/videos/add-manual', json={'url': 'https://www.youtube.com/watch?v=testvid456'})
    print('status:', r.status_code, r.get_json())
    print('download_rangesキーなし:', 'download_ranges' not in captured_dl_opts)
    print('outtmplに範囲サフィックスなし:', captured_dl_opts.get('outtmpl', '').endswith('testvid456.%(ext)s'))

from database import Article, db
with app.app_context():
    art = Article.query.filter(Article.url.like('%testvid456%')).order_by(Article.id.desc()).first()
    print('保存されたURL（元URLのままのはず）:', art.url if art else None)
    print('保存されたvideo_file_path:', art.video_file_path if art else None)
    if art:
        db.session.delete(art)
        db.session.commit()
"
```
Expected: `status: 200`、`download_rangesキーなし: True`、`outtmplに範囲サフィックスなし: True`、`保存されたURL（元URLのままのはず）: https://www.youtube.com/watch?v=testvid456`、`保存されたvideo_file_path: videos/testvid456.mp4`。

- [ ] **Step 8: 実際のyt-dlp・実際の動画で範囲ダウンロードが機能することを確認する（軽量な実動画テスト）**

ネットワーク・実際のyt-dlpを使い、公開されている短い動画に対して範囲指定ダウンロードが実際に成功しファイルが生成されることを確認する。長時間の動画を使う必要はない（メカニズム自体の検証が目的）。テスト対象動画URLは自由に選んでよいが、著作権上安全でよく知られる短い公開動画（例: YouTube公式の初投稿動画など）を推奨する。

```bash
venv/Scripts/python.exe -c "
import os, tempfile
import yt_dlp
from yt_dlp.utils import download_range_func

tmp_dir = os.path.join(tempfile.gettempdir(), 'kpopwave_range_test')
os.makedirs(tmp_dir, exist_ok=True)
url = 'https://www.youtube.com/watch?v=jNQXAC9IVRw'  # Me at the zoo（19秒、テスト用に安全な公開動画）

dl_opts = {
    'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]',
    'merge_output_format': 'mp4',
    'outtmpl': os.path.join(tmp_dir, 'rangetest.%(ext)s'),
    'quiet': True, 'no_warnings': True, 'ignoreerrors': True,
    'download_ranges': download_range_func([], [(2, 8)]),
}
with yt_dlp.YoutubeDL(dl_opts) as ydl:
    ydl.download([url])

files = os.listdir(tmp_dir)
print('生成されたファイル:', files)
print('ファイルが1件以上ある:', len(files) >= 1)

import subprocess
ffprobe = os.path.join(os.path.dirname(os.path.abspath('app.py')), 'ffmpeg', 'bin', 'ffprobe.exe')
if files and os.path.exists(ffprobe):
    target = os.path.join(tmp_dir, files[0])
    result = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                              '-of', 'default=noprint_wrappers=1:nokey=1', target],
                             capture_output=True, text=True)
    print('取得できた動画の長さ(秒):', result.stdout.strip())

for f in files:
    os.remove(os.path.join(tmp_dir, f))
os.rmdir(tmp_dir)
"
```
Expected: `ファイルが1件以上ある: True`。取得できた動画の長さが指定範囲（6秒前後、キーフレームの都合で多少前後してもよい）に近いこと。19秒の動画全体（≈19秒）がダウンロードされてしまっていないことを確認する（範囲指定が効いている証拠）。

- [ ] **Step 9: Commit**

```bash
git add app.py templates/index.html
git commit -m "$(cat <<'EOF'
feat: YouTube手動追加に時間範囲指定ダウンロードを追加

長尺動画（例: 2時間22分）を丸ごとダウンロードすると「ダウンロード
ファイルが見つかりません」エラーになる問題への対応として、
ダッシュボードのYouTube動画追加フォームに任意の開始・終了時刻
入力を追加した。指定があればyt_dlp.utils.download_range_funcで
その範囲だけをダウンロードする。範囲指定時は同一動画の別範囲との
衝突を避けるため、保存URL・ファイル名に範囲サフィックスを付与し、
重複チェックをスキップする。範囲未指定時は完全に既存動作のまま。
EOF
)"
```

---

## 完了確認

- [ ] 範囲を指定しない場合、YouTube動画追加は完全に従来通り動作する
- [ ] 範囲を指定した場合、その範囲だけがダウンロードされる（実動画で確認済み）
- [ ] 同一動画に対して範囲を変えて複数回追加でき、互いのファイルを上書きしない
- [ ] `_parse_time_input`が"H:MM:SS"・"MM:SS"・"SS"いずれの形式も正しく解釈する
- [ ] 実際の長尺動画（可能であれば2時間超）での分割ダウンロードをユーザー自身でも確認できるよう、手順を報告する
