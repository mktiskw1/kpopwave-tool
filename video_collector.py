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

# チャンネルURLに試みるタブサフィックス（Shortsは別関数で処理するため除外）
_URL_SUFFIXES = ["/videos", ""]

# ダウンロード後に無視する一時拡張子
_SKIP_EXTS = {".part", ".ytdl", ".temp", ".tmp", ".jpg", ".png", ".webp"}


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


def _collect_channel_videos(channel_info: dict, tmp_dir: str, existing_urls: set) -> list[dict]:
    """
    yt-dlpで指定チャンネルから最新動画（≤MAX_DURATION秒・DAYS_LIMIT日以内）を
    最大MAX_VIDEOS_PER_CHANNEL件取得する。
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

            logger.info(
                "[%s] 日付確認 upload_date=%s timestamp=%s cutoff=%s title=%s",
                channel_name,
                full.get("upload_date") or "(空)",
                full.get("timestamp") or "(空)",
                cutoff_str,
                c["title"][:40],
            )

            # 日付が確認できない動画は取り込まない
            if not actual_date:
                logger.info(
                    "[%s] スキップ（日付不明）: %s",
                    channel_name, c["title"][:60],
                )
                continue

            if actual_date < cutoff_str:
                logger.info(
                    "[%s] スキップ（%sは%d日以上前）: %s",
                    channel_name, actual_date, DAYS_LIMIT, c["title"][:60],
                )
                continue

            duration = full.get("duration") or 9999
            if duration > MAX_DURATION:
                logger.info(
                    "[%s] スキップ（%ds > %ds）: %s",
                    channel_name, duration, MAX_DURATION, c["title"][:60],
                )
                continue

            targets.append({
                "id":           c["id"],
                "url":          c["url"],
                "title":        (full.get("title") or c["title"])[:500],
                "description":  (full.get("description") or "")[:5000],
                "thumbnail":    full.get("thumbnail", ""),
                "duration":     duration,
                "channel_name": full.get("uploader") or channel_name,
                "upload_date":  actual_date,
            })
        except Exception as exc:
            logger.warning("[%s] 動画情報取得エラー %s: %s", channel_name, c["url"], exc)

    if not targets:
        logger.info("[%s] 条件に合う動画なし（%ds以内）", channel_name, MAX_DURATION)
        return []

    logger.info("[%s] ダウンロード対象: %d件", channel_name, len(targets))

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


def _collect_channel_shorts(channel_info: dict, tmp_dir: str, existing_urls: set) -> list[dict]:
    """
    yt-dlpで指定チャンネルのShortsを最新MAX_VIDEOS_PER_CHANNEL件取得する。
    Shortsは60秒以内なのでduration制限は適用しない。
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

    tmp_dir = os.path.join(tempfile.gettempdir(), "kpopwave_videos")
    os.makedirs(tmp_dir, exist_ok=True)

    new_count = 0

    for ch in channels:
        channel_name = ch.get("name", "Unknown")
        logger.info("動画収集開始: %s", channel_name)

        videos = _collect_channel_videos(ch, tmp_dir, existing_urls)
        shorts = _collect_channel_shorts(ch, tmp_dir, existing_urls)

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
                )
                db.session.add(article)
                db.session.commit()

                existing_urls.add(yt_url)
                new_count += 1
                logger.info(
                    "[%s] DB追加(%s): %s (%ds)",
                    channel_name, label, video["title"][:60], video.get("duration", 0),
                )

    logger.info("動画収集完了: %d件追加", new_count)
    return new_count
