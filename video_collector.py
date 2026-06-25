import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta

from database import Article, Setting, db

logger = logging.getLogger(__name__)

MAX_DURATION = 600          # 秒（MV・ティザー対応：最大10分）
DAYS_LIMIT = 7              # 過去N日以内の動画のみ対象
MAX_VIDEOS_PER_CHANNEL = 3  # チャンネルごとの最大取得件数

DEFAULT_CHANNELS = [
    {"name": "aespa",        "url": "https://www.youtube.com/@aespa"},
    {"name": "NewJeans",     "url": "https://www.youtube.com/@NewJeans_official"},
    {"name": "BLACKPINK",    "url": "https://www.youtube.com/@BLACKPINK"},
    {"name": "TWICE",        "url": "https://www.youtube.com/@TWICE"},
    {"name": "IVE",          "url": "https://www.youtube.com/@IVEstarship"},
    # channel ID使用（@ハンドルが404のため）
    {"name": "LE SSERAFIM",  "url": "https://www.youtube.com/channel/UCs-QBT4qkj_YiQw1ZntDO3g"},
    {"name": "ILLIT",        "url": "https://www.youtube.com/@ILLIT_official"},
    {"name": "tripleS",      "url": "https://www.youtube.com/channel/UCJnL-TBcsYrF2SLs7tmiC8Q"},
]

# 公式グループチャンネル名セット（このチャンネルは女性アイドルフィルターをスキップ）
_OFFICIAL_GROUP_NAMES = frozenset(ch["name"].lower() for ch in DEFAULT_CHANNELS)

# チャンネルURLに試みるタブサフィックス（Shortsは別関数で処理するため除外）
_URL_SUFFIXES = ["/videos", ""]

# ダウンロード後に無視する一時拡張子
_SKIP_EXTS = {".part", ".ytdl", ".temp", ".tmp", ".jpg", ".png", ".webp"}

# ファンカム判定キーワード
_FANCAM_KEYWORDS = frozenset(["fancam", "직캠", "focus"])

# Shorts判定キーワード（タイトル小文字で照合）
_SHORTS_TITLE_KEYWORDS = frozenset(["shorts", "#shorts"])

# Shorts判定の最大動画時間（この秒数以下はShortsとして除外）
_SHORTS_MAX_DURATION = 60

# Threads API動画要件チェック・変換用
_FFMPEG_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin")
_FFMPEG_EXE  = os.path.join(_FFMPEG_DIR, "ffmpeg.exe")
_FFPROBE_EXE = os.path.join(_FFMPEG_DIR, "ffprobe.exe")
_MAX_VIDEO_SIZE_MB = 95  # Threads API上限100MBに対し余裕を持たせる


def _is_fancam(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _FANCAM_KEYWORDS) or "[4k]" in t


def _probe_video(file_path: str) -> dict:
    """ffprobeで動画スペックを取得する。失敗時は空dictを返す。"""
    import subprocess
    import json as _json
    if not os.path.exists(_FFPROBE_EXE):
        logger.warning("ffprobe が見つかりません: %s", _FFPROBE_EXE)
        return {}
    try:
        result = subprocess.run(
            [_FFPROBE_EXE, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", file_path],
            capture_output=True, text=True, timeout=30, encoding="utf-8",
        )
        if result.returncode != 0:
            logger.warning("ffprobe失敗 rc=%d: %s", result.returncode, result.stderr[:200])
            return {}
        info = _json.loads(result.stdout)
        vstream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        astream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
        size_bytes = int(info.get("format", {}).get("size", 0))
        return {
            "video_codec": vstream.get("codec_name", ""),
            "audio_codec": astream.get("codec_name", ""),
            "width":    int(vstream.get("width", 0)),
            "height":   int(vstream.get("height", 0)),
            "size_mb":  size_bytes / (1024 * 1024),
            "duration": float(info.get("format", {}).get("duration", 0)),
        }
    except Exception as exc:
        logger.warning("ffprobe例外: %s — %s", os.path.basename(file_path), exc)
        return {}


def _transcode_to_h264(file_path: str) -> bool:
    """H.264+AACにインプレース変換する（元ファイルを上書き）。成功したらTrueを返す。"""
    import subprocess
    if not os.path.exists(_FFMPEG_EXE):
        logger.error("ffmpeg が見つかりません: %s", _FFMPEG_EXE)
        return False
    tmp_path = file_path + ".converting.mp4"
    cmd = [
        _FFMPEG_EXE, "-y", "-i", file_path,
        "-vcodec", "libx264", "-crf", "23", "-preset", "fast",
        "-acodec", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        tmp_path,
    ]
    logger.info("H.264+AAC変換開始: %s", os.path.basename(file_path))
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace")[-400:]
            logger.error("ffmpeg変換失敗 rc=%d: %s", result.returncode, err)
            return False
        os.replace(tmp_path, file_path)
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        logger.info("H.264+AAC変換完了: %s (%.1fMB)", os.path.basename(file_path), size_mb)
        return True
    except Exception as exc:
        logger.error("ffmpeg変換例外: %s", exc)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return False


def _ensure_threads_compatible(file_path: str, channel_name: str = "") -> bool:
    """
    動画ファイルがThreads API要件を満たすか確認し、
    必要ならH.264+AACにインプレース変換する。
    Threads要件: H.264コーデック・AAC音声・MP4コンテナ・最大100MB
    変換不要または変換成功ならTrueを返す。
    """
    specs = _probe_video(file_path)
    if not specs:
        logger.info("[%s] ffprobe取得失敗 → そのまま使用: %s", channel_name, os.path.basename(file_path))
        return True

    video_codec = specs.get("video_codec", "")
    audio_codec = specs.get("audio_codec", "")
    size_mb     = specs.get("size_mb", 0)
    width       = specs.get("width", 0)
    height      = specs.get("height", 0)
    duration    = specs.get("duration", 0)

    logger.info(
        "[%s] 動画スペック: codec=%s/%s size=%.1fMB %dx%d dur=%.1fs",
        channel_name, video_codec, audio_codec, size_mb, width, height, duration,
    )

    reasons = []
    if video_codec != "h264":
        reasons.append(f"videoCodec={video_codec}（H.264必須）")
    if audio_codec and audio_codec not in ("aac", "mp3"):
        reasons.append(f"audioCodec={audio_codec}（AAC必須）")
    if size_mb > _MAX_VIDEO_SIZE_MB:
        reasons.append(f"size={size_mb:.1f}MB（上限{_MAX_VIDEO_SIZE_MB}MB）")

    if reasons:
        logger.warning("[%s] Threads非互換: %s → H.264+AACに変換", channel_name, ", ".join(reasons))
        return _transcode_to_h264(file_path)

    logger.info("[%s] Threads互換OK: %s", channel_name, os.path.basename(file_path))
    return True


def _get_channel_list(app) -> list:
    with app.app_context():
        raw = Setting.get("youtube_channels", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return DEFAULT_CHANNELS


def _get_static_videos_dir() -> str:
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "videos")
    os.makedirs(static_dir, exist_ok=True)
    return static_dir


def _get_existing_yt_urls(app) -> set:
    with app.app_context():
        rows = Article.query.filter(
            Article.url.like("https://www.youtube.com/watch?v=%")
        ).all()
        return {r.url for r in rows}


def _is_official_group_channel(channel_name: str) -> bool:
    """公式グループチャンネルかどうかを判定する（フィルタースキップ用）。"""
    return channel_name.lower() in _OFFICIAL_GROUP_NAMES


def _is_female_idol_video(title: str, api_key: str) -> bool:
    """Claude HaikuでKPOP女性アイドル関連かどうかを判定する。
    判定できない場合はTrue（通過扱い）を返す。
    """
    if not api_key or not title:
        return True
    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": (
                    "以下のYouTube動画タイトルはKPOP女性アイドルに関連していますか？\n"
                    "男性アイドル・男性アーティスト・関係ない動画は除外してください。\n"
                    "YESかNOだけ答えてください。\n"
                    f"タイトル：{title}"
                ),
            }],
        )
        answer = msg.content[0].text.strip().upper()
        return answer.startswith("YES")
    except Exception as exc:
        logger.warning("女性アイドルフィルター判定失敗（通過扱い）: %s — %s", title[:40], exc)
        return True


def _find_downloaded_file(tmp_dir: str, vid_id: str) -> tuple[str, str] | None:
    """
    ダウンロード済みファイルをディレクトリスキャンで検索する。
    ファイル名に vid_id が含まれていればヒット（部分一致）。
    例: {vid_id}.mp4 / {vid_id}.f398.mp4 / {vid_id}.webm 全て対象。
    mp4 を最優先、次いて他の動画形式。(絶対パス, 拡張子) を返す。
    """
    found_mp4 = None
    found_other = None

    try:
        entries = os.listdir(tmp_dir)
    except Exception as exc:
        logger.error("tmpディレクトリ読み取り失敗: %s — %s", tmp_dir, exc)
        return None

    logger.info("tmpディレクトリ内容 (vid_id=%s): %s", vid_id, entries)

    for filename in entries:
        # vid_id が含まれているファイルのみ対象（部分一致）
        if vid_id not in filename:
            continue
        _, ext = os.path.splitext(filename)
        if ext.lower() in _SKIP_EXTS:
            continue
        full_path = os.path.join(tmp_dir, filename)
        if not os.path.isfile(full_path):
            continue
        if ext.lower() == ".mp4":
            # 複数のmp4がある場合はファイルサイズが大きい方（マージ済み）を優先
            if found_mp4 is None or os.path.getsize(full_path) > os.path.getsize(found_mp4[0]):
                found_mp4 = (full_path, "mp4")
        elif found_other is None:
            found_other = (full_path, ext.lstrip(".").lower())

    result = found_mp4 or found_other
    if result:
        logger.info("ダウンロードファイル検出: %s", result[0])
    else:
        logger.error(
            "ダウンロードファイル未検出（vid_id=%s）。tmp_dir内ファイル: %s",
            vid_id, entries,
        )
    return result


def _fetch_channel_entries(channel_url: str, flat_opts: dict, cutoff_str: str,
                           existing_urls: set, channel_name: str) -> list:
    """
    URLサフィックスを順番に試してチャンネルの動画リストを取得する。
    candidates リストを返す（空の場合もある）。
    """
    try:
        import yt_dlp
    except ImportError:
        return []

    base = channel_url.rstrip("/")
    candidates = []

    for suffix in _URL_SUFFIXES:
        attempt_url = base + suffix
        try:
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                info = ydl.extract_info(attempt_url, download=False)
        except Exception as exc:
            logger.debug("[%s] %s 取得失敗: %s", channel_name, attempt_url, exc)
            continue

        if not info:
            continue

        entries = info.get("entries") or []
        for entry in entries:
            if not entry:
                continue
            vid_id = entry.get("id") or ""
            if not vid_id or len(vid_id) != 11:
                continue
            yt_url = f"https://www.youtube.com/watch?v={vid_id}"
            if yt_url in existing_urls:
                continue
            upload_date = entry.get("upload_date", "")
            if upload_date and upload_date < cutoff_str:
                logger.debug(
                    "[%s] スキップ（%sは7日以上前）: %s",
                    channel_name, upload_date, entry.get("title", "")[:60],
                )
                continue
            candidates.append({
                "id":          vid_id,
                "url":         yt_url,
                "title":       entry.get("title", ""),
                "upload_date": upload_date,
            })

        if candidates:
            logger.info("[%s] URLパターン成功: %s (%d件候補)", channel_name, suffix or "(base)", len(candidates))
            return candidates

        logger.debug("[%s] %s: 候補なし", channel_name, attempt_url)

    return candidates


def _collect_channel_videos(channel_info: dict, tmp_dir: str, existing_urls: set,
                             api_key: str = "", apply_filter: bool = False) -> list[dict]:
    """
    yt-dlpで指定チャンネルから最新動画（≤MAX_DURATION秒・DAYS_LIMIT日以内）を
    最大MAX_VIDEOS_PER_CHANNEL件取得する。
    apply_filter=True の場合は Claude Haiku で女性アイドル関連かチェックする。
    ダウンロード済み動画メタデータのリストを返す（0件の場合は空リスト）。
    """
    try:
        import yt_dlp
    except ImportError:
        logger.error("yt-dlpが未インストールです。pip install yt-dlp を実行してください。")
        return []

    channel_name = channel_info.get("name", "Unknown")
    channel_url = channel_info.get("url", "").rstrip("/")
    if not channel_url:
        return []

    cutoff_str = (datetime.utcnow() - timedelta(days=DAYS_LIMIT)).strftime("%Y%m%d")

    # ─── Step 1: チャンネル動画リストをフラット取得 ───────────────────────────
    flat_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 20,
        "ignoreerrors": True,
    }

    candidates = _fetch_channel_entries(channel_url, flat_opts, cutoff_str, existing_urls, channel_name)

    if not candidates:
        logger.info("[%s] 新着動画なし（過去%d日・未収録）", channel_name, DAYS_LIMIT)
        return []

    # ── 診断ログ: 全候補タイトルとファンカム判定結果を出力 ─────────────────────
    logger.info("[%s] Step1取得: %d件候補", channel_name, len(candidates))
    for i, c in enumerate(candidates):
        fc = _is_fancam(c["title"])
        logger.info(
            "[%s]   候補[%d] fancam=%s date=%s title=%s",
            channel_name, i, fc, c.get("upload_date", "?"), c["title"][:80],
        )

    # ファンカムを先頭に優先ソート
    candidates.sort(key=lambda c: not _is_fancam(c["title"]))
    fancam_in_candidates = sum(1 for c in candidates if _is_fancam(c["title"]))
    logger.info("[%s] ファンカム候補: %d件 / 全%d件", channel_name, fancam_in_candidates, len(candidates))

    # ─── Step 2: 各候補の詳細情報でduration・日付チェック（最大MAX_VIDEOS_PER_CHANNEL件）
    info_opts = {"quiet": True, "no_warnings": True, "ignoreerrors": True}

    targets = []
    for c in candidates:
        if len(targets) >= MAX_VIDEOS_PER_CHANNEL:
            break
        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                full = ydl.extract_info(c["url"], download=False)
            if not full:
                continue

            full_title = full.get("title") or c["title"]
            fc = _is_fancam(full_title)

            # upload_date を確定する（flat取得では空のことが多いためStep2で取得）
            actual_date = full.get("upload_date") or c.get("upload_date", "")
            # upload_date が空の場合は timestamp から変換を試みる
            if not actual_date:
                ts = full.get("timestamp") or full.get("release_timestamp")
                if ts:
                    try:
                        actual_date = datetime.utcfromtimestamp(ts).strftime("%Y%m%d")
                    except Exception:
                        pass

            duration = full.get("duration") or 9999
            logger.info(
                "[%s] Step2確認 fancam=%s date=%s duration=%ds cutoff=%s title=%s",
                channel_name, fc,
                actual_date or "(空)",
                duration,
                cutoff_str,
                full_title[:60],
            )

            # 日付が確認できない動画は取り込まない
            if not actual_date:
                logger.info("[%s] スキップ理由=日付不明 title=%s", channel_name, full_title[:60])
                continue

            if actual_date < cutoff_str:
                logger.info(
                    "[%s] スキップ理由=古い(%s < %s) fancam=%s title=%s",
                    channel_name, actual_date, cutoff_str, fc, full_title[:60],
                )
                continue

            if duration > MAX_DURATION:
                logger.info(
                    "[%s] スキップ理由=長すぎる(%ds > %ds) fancam=%s title=%s",
                    channel_name, duration, MAX_DURATION, fc, full_title[:60],
                )
                continue

            if any(kw in full_title.lower() for kw in _SHORTS_TITLE_KEYWORDS):
                logger.info("[%s] スキップ理由=Shortsタイトル title=%s", channel_name, full_title[:60])
                continue

            if duration <= _SHORTS_MAX_DURATION:
                logger.info(
                    "[%s] スキップ理由=Shorts短時間(%ds) title=%s",
                    channel_name, duration, full_title[:60],
                )
                continue

            # 複合チャンネルのみ女性アイドルフィルターを適用
            if apply_filter:
                title_to_check = full_title
                if not _is_female_idol_video(title_to_check, api_key):
                    logger.info(
                        "[%s] スキップ理由=女性アイドル以外 fancam=%s title=%s",
                        channel_name, fc, title_to_check[:60],
                    )
                    continue

            targets.append({
                "id":           c["id"],
                "url":          c["url"],
                "title":        full_title[:500],
                "description":  (full.get("description") or "")[:5000],
                "thumbnail":    full.get("thumbnail", ""),
                "duration":     duration,
                "channel_name": full.get("uploader") or channel_name,
                "upload_date":  actual_date,
            })
            logger.info("[%s] ダウンロード追加: fancam=%s title=%s", channel_name, fc, full_title[:60])
        except Exception as exc:
            logger.warning("[%s] 動画情報取得エラー %s: %s", channel_name, c["url"], exc)

    if not targets:
        logger.info("[%s] 条件に合う動画なし（全%d候補を確認）", channel_name, len(candidates))
        return []

    fancam_targets = sum(1 for t in targets if _is_fancam(t["title"]))
    logger.info(
        "[%s] ダウンロード対象: %d件（うちファンカム %d件）",
        channel_name, len(targets), fancam_targets,
    )

    # ─── Step 3: ダウンロード（各ターゲット） ────────────────────────────────
    dl_opts_base = {
        "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]",
        "ffmpeg_location": r"C:\Users\mktis\kpopwave-tool\ffmpeg\bin",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": True,
    }

    results = []
    for target in targets:
        vid_id = target["id"]
        outtmpl_path = os.path.join(tmp_dir, f"{vid_id}.%(ext)s")
        dl_opts = {**dl_opts_base, "outtmpl": outtmpl_path}

        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([target["url"]])
        except Exception as exc:
            logger.error("[%s] ダウンロードエラー: %s", channel_name, exc)
            continue

        found = _find_downloaded_file(tmp_dir, vid_id)
        if not found:
            continue

        local_path, ext = found
        target["local_path"] = local_path
        target["ext"] = ext
        logger.info(
            "[%s] ダウンロード完了: %s (%ds) → %s",
            channel_name, target["title"][:60], target["duration"], os.path.basename(local_path),
        )
        results.append(target)

    return results


def _collect_channel_shorts(channel_info: dict, tmp_dir: str, existing_urls: set,
                             api_key: str = "", apply_filter: bool = False) -> list[dict]:
    """
    yt-dlpで指定チャンネルのShortsを最新MAX_VIDEOS_PER_CHANNEL件取得する。
    Shortsは60秒以内なのでduration制限は適用しない。
    apply_filter=True の場合は Claude Haiku で女性アイドル関連かチェックする。
    """
    try:
        import yt_dlp
    except ImportError:
        return []

    channel_name = channel_info.get("name", "Unknown")
    channel_url  = channel_info.get("url", "").rstrip("/")
    if not channel_url:
        return []

    cutoff_str = (datetime.utcnow() - timedelta(days=DAYS_LIMIT)).strftime("%Y%m%d")
    shorts_url  = channel_url + "/shorts"

    flat_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 15,
        "ignoreerrors": True,
    }

    # ─── Step1: Shortsリストをフラット取得 ──────────────────────────────────
    try:
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            info = ydl.extract_info(shorts_url, download=False)
    except Exception as exc:
        logger.debug("[%s] Shorts取得失敗: %s", channel_name, exc)
        return []

    if not info:
        return []

    candidates = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue
        vid_id = entry.get("id") or ""
        if not vid_id or len(vid_id) != 11:
            continue
        yt_url = f"https://www.youtube.com/watch?v={vid_id}"
        if yt_url in existing_urls:
            continue
        upload_date = entry.get("upload_date", "")
        if upload_date and upload_date < cutoff_str:
            continue
        candidates.append({
            "id":          vid_id,
            "url":         yt_url,
            "title":       entry.get("title", ""),
            "upload_date": upload_date,
        })

    if not candidates:
        logger.info("[%s] Shorts: 新着なし（過去%d日・未収録）", channel_name, DAYS_LIMIT)
        return []

    logger.info("[%s] Shorts: %d件候補", channel_name, len(candidates))

    # ─── Step2: 詳細情報で日付確認（duration制限は適用しない） ──────────────
    info_opts = {"quiet": True, "no_warnings": True, "ignoreerrors": True}
    targets = []
    for c in candidates:
        if len(targets) >= MAX_VIDEOS_PER_CHANNEL:
            break
        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                full = ydl.extract_info(c["url"], download=False)
            if not full:
                continue

            actual_date = full.get("upload_date") or c.get("upload_date", "")
            if not actual_date:
                ts = full.get("timestamp") or full.get("release_timestamp")
                if ts:
                    try:
                        actual_date = datetime.utcfromtimestamp(ts).strftime("%Y%m%d")
                    except Exception:
                        pass

            logger.info(
                "[%s] Shorts日付確認 upload_date=%s cutoff=%s title=%s",
                channel_name,
                full.get("upload_date") or "(空)",
                cutoff_str,
                c["title"][:40],
            )

            if not actual_date:
                logger.info("[%s] Shorts スキップ（日付不明）: %s", channel_name, c["title"][:60])
                continue
            if actual_date < cutoff_str:
                logger.info(
                    "[%s] Shorts スキップ（%sは%d日以上前）: %s",
                    channel_name, actual_date, DAYS_LIMIT, c["title"][:60],
                )
                continue

            # 複合チャンネルのみ女性アイドルフィルターを適用
            if apply_filter:
                title_to_check = full.get("title") or c["title"]
                if not _is_female_idol_video(title_to_check, api_key):
                    logger.info("[%s] Shorts スキップ（女性アイドル以外）: %s", channel_name, title_to_check[:60])
                    continue

            targets.append({
                "id":           c["id"],
                "url":          c["url"],
                "title":        (full.get("title") or c["title"])[:500],
                "description":  (full.get("description") or "")[:5000],
                "thumbnail":    full.get("thumbnail", ""),
                "duration":     full.get("duration") or 0,
                "channel_name": full.get("uploader") or channel_name,
                "upload_date":  actual_date,
            })
        except Exception as exc:
            logger.warning("[%s] Shorts情報取得エラー %s: %s", channel_name, c["url"], exc)

    if not targets:
        logger.info("[%s] Shorts: 条件に合う動画なし", channel_name)
        return []

    logger.info("[%s] Shorts ダウンロード対象: %d件", channel_name, len(targets))

    # ─── Step3: ダウンロード ────────────────────────────────────────────────
    dl_opts_base = {
        "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]",
        "ffmpeg_location": r"C:\Users\mktis\kpopwave-tool\ffmpeg\bin",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": True,
    }

    results = []
    for target in targets:
        vid_id = target["id"]
        outtmpl_path = os.path.join(tmp_dir, f"{vid_id}.%(ext)s")
        dl_opts = {**dl_opts_base, "outtmpl": outtmpl_path}

        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([target["url"]])
        except Exception as exc:
            logger.error("[%s] Shorts ダウンロードエラー: %s", channel_name, exc)
            continue

        found = _find_downloaded_file(tmp_dir, vid_id)
        if not found:
            continue

        local_path, ext = found
        target["local_path"] = local_path
        target["ext"]        = ext
        logger.info(
            "[%s] Shorts ダウンロード完了: %s (%ds) → %s",
            channel_name, target["title"][:60], target["duration"], os.path.basename(local_path),
        )
        results.append(target)

    return results


def collect_youtube_videos(app) -> int:
    """
    全チャンネルからyt-dlpで最新動画を収集してDBに保存する。
    新規追加件数を返す。
    """
    channels = _get_channel_list(app)
    static_dir = _get_static_videos_dir()
    existing_urls = _get_existing_yt_urls(app)

    with app.app_context():
        api_key = Setting.get("anthropic_api_key", "") or ""

    tmp_dir = os.path.join(tempfile.gettempdir(), "kpopwave_videos")
    os.makedirs(tmp_dir, exist_ok=True)

    new_count = 0

    for ch in channels:
        channel_name = ch.get("name", "Unknown")
        # 公式グループチャンネルはフィルタースキップ、複合チャンネルのみ適用
        apply_filter = not _is_official_group_channel(channel_name) and bool(api_key)
        logger.info("動画収集開始: %s（フィルター: %s）", channel_name, "あり" if apply_filter else "スキップ")

        videos = _collect_channel_videos(ch, tmp_dir, existing_urls, api_key, apply_filter)
        shorts = []  # Shorts収集は無効化

        # (コンテンツ種別フラグ, videoデータ) のリストに統合
        all_content = [("video", v) for v in videos] + [("short", s) for s in shorts]
        if not all_content:
            continue

        for content_flag, video in all_content:
            is_short = (content_flag == "short")
            vid_id = video["id"]
            ext = video.get("ext", "mp4")
            dest_filename = f"{vid_id}.{ext}"
            dest_path = os.path.join(static_dir, dest_filename)

            try:
                shutil.copy2(video["local_path"], dest_path)
                try:
                    os.remove(video["local_path"])
                except Exception:
                    pass
            except Exception as exc:
                logger.error("[%s] ファイルコピーエラー: %s", channel_name, exc)
                continue

            # Threads API要件チェック・H.264+AAC変換（非互換コーデックを変換）
            _ensure_threads_compatible(dest_path, channel_name)

            yt_url = video["url"]
            with app.app_context():
                if Article.query.filter_by(url=yt_url).first():
                    logger.info("[%s] 重複スキップ: %s", channel_name, yt_url)
                    continue

                published_at = None
                ud = video.get("upload_date", "")
                if ud and len(ud) == 8:
                    try:
                        published_at = datetime.strptime(ud, "%Y%m%d")
                    except Exception:
                        pass

                feed_src      = f"YouTube Shorts: {channel_name}" if is_short else f"YouTube動画: {channel_name}"
                title_default = "YouTube Shorts" if is_short else "YouTube動画"
                label         = "Shorts" if is_short else "動画"

                fancam = _is_fancam(video["title"])
                if fancam:
                    logger.info("[%s] ファンカム検出: %s", channel_name, video["title"][:60])

                article = Article(
                    feed_source=feed_src,
                    title=video["title"] or title_default,
                    url=yt_url,
                    published_at=published_at,
                    raw_content=video.get("description", ""),
                    thumbnail_url=video.get("thumbnail") or None,
                    status="pending",
                    content_type="video",
                    video_file_path=f"videos/{dest_filename}",
                    is_fancam=fancam,
                    view_count=video.get("view_count"),
                )
                db.session.add(article)
                db.session.commit()

                existing_urls.add(yt_url)
                new_count += 1
                logger.info(
                    "[%s] DB追加(%s%s): %s (%ds)",
                    channel_name, label, "/FANCAM" if fancam else "",
                    video["title"][:60], video.get("duration", 0),
                )

    logger.info("動画収集完了: %d件追加", new_count)
    return new_count
