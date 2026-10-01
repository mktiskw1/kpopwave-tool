"""チャンネル監視: 実績のあるYouTubeチャンネルの新着を毎日自動で承認待ちに取り込む。

APIクォータ節約のため、新着取得はsearch.list(100ユニット)ではなくアップロード再生リストの
playlistItems.list(1ユニット)を使い、動画詳細はvideos.listで50件ずつまとめて取得する。
既存のfancam検索・音楽番組検索の除外ルール(放送局チャンネル除外・MPD직캠などの除外キーワード)は
この監視経由の取り込みには適用しない(youtube_collector側の動作は変更しない)。

条件に合った動画はダウンロードせず「候補」(WatchedCandidate)として一覧に並べるだけにする。
ユーザーが選んだ候補だけを、バックグラウンドでダウンロードして承認待ち(Article)に取り込む。
"""
import logging
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timedelta

import requests
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError

from config import YOUTUBE_DL_FORMAT
from channel_info import extract_youtube_video_id, get_youtube_api_key
from database import (
    Article, DeletedPostLog, Group, Setting, ThreadsAccount, WatchedCandidate, WatchedChannel, db,
)
from youtube_collector import (
    YOUTUBE_CHANNELS_URL, YOUTUBE_VIDEOS_URL, _best_thumbnail,
    _is_program_shorts, _matches_target_artist, _parse_duration_iso8601,
    _resolve_free_channel_id,
)

logger = logging.getLogger(__name__)

YOUTUBE_PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"

FIRST_RUN_DAYS = 7              # 初回(cursor未設定)は直近N日分
MAX_IMPORT_PER_CHANNEL = 20     # 1チャンネルあたり1回の取り込み上限
MAX_PLAYLIST_PAGES = 4          # 1チャンネルあたりplaylistItems.listの最大ページ数(50件/ページ)
CURSOR_MARGIN = timedelta(minutes=30)   # 再生リスト反映の遅れ対策。重複はDB・削除記録で弾く
WATCH_MAX_DURATION_SEC = 600    # 自動ダウンロードする動画の長さの上限(通常収集のMAX_DURATIONと同じ)
WATCH_FEED_PREFIX = "YouTube動画: "
BUZZ_JUDGE_DAYS = 7             # 投稿からこの日数経過したものを成績判定済みとみなす

# 候補の状態
CANDIDATE_NEW = "new"
CANDIDATE_IMPORTED = "imported"
CANDIDATE_SKIPPED = "skipped"
CANDIDATE_EXPIRED = "expired"
EXPIRE_DAYS_SETTING = "watched_candidate_expire_days"
DEFAULT_EXPIRE_DAYS = 14

# fancam必須のチャンネルで使うタイトル判定キーワード(カンマ区切り。大文字小文字は区別せず、前後の空白は無視)。
# 監視専用の設定で、既存のfancam検索・音楽番組検索の判定(youtube_collector側)には影響しない。
AUTO_FETCH_SETTING = "watched_auto_fetch_enabled"   # OFFなら毎朝6:00の自動取得をスキップ(手動取得・期限切れ処理は動く)
FANCAM_KEYWORDS_SETTING = "watched_fancam_keywords"
DEFAULT_FANCAM_KEYWORDS = "직캠, fancam, 원테이크, 페이스캠, facecam"

SEED_SETTING_KEY = "watched_channels_seeded"
# 初期登録するチャンネル名(channel_idはArticleに保存済みのchannel_idから名前で引き、APIで実在確認する)
SEED_CHANNEL_NAMES = [
    "KBS Kpop", "M2", "SBSKPOP ZOOM",                                   # 放送局fancam系
    "MY 에스파", "DaftTaengk", "im_chirey", "Mr. Egg", "pop7 jp",       # 個人チャンネル
]

_YOUTUBE_COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "youtube_cookies.txt")
# app.pyの_YT_DLP_JS_OPTSと同じ(Cookie認証時のJS署名解読にnodeを使う)
_YT_DLP_JS_OPTS = {"js_runtimes": {"node": {}}, "remote_components": {"ejs:github"}}

_run_lock = threading.Lock()
_state = {"running": False, "started_at": None, "result": None}


# ── アカウント ─────────────────────────────────────────────────────────────

def kpop_account_id():
    """監視の対象アカウント(KPOPアカウント=content_topic未設定の最古のアクティブアカウント)。"""
    accounts = ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all()
    for acc in accounts:
        if not (acc.content_topic or "").strip():
            return acc.id
    return accounts[0].id if accounts else None


# ── YouTube API ───────────────────────────────────────────────────────────

def fetch_channel_details(channel_ids: list, api_key: str, units: list = None) -> dict:
    """channels.list(1ユニット/50件)で {channel_id: (チャンネル名, アップロード再生リストID)} を返す。
    存在しないIDは結果に含まれない。unitsはAPI消費ユニットの加算用(長さ1のリスト)。"""
    result = {}
    ids = list(dict.fromkeys(channel_ids))
    for i in range(0, len(ids), 50):
        resp = requests.get(
            YOUTUBE_CHANNELS_URL,
            params={"part": "snippet,contentDetails", "id": ",".join(ids[i:i + 50]),
                    "maxResults": 50, "key": api_key},
            timeout=20,
        )
        resp.raise_for_status()
        if units is not None:
            units[0] += 1
        for item in resp.json().get("items", []):
            uploads = (item.get("contentDetails", {}).get("relatedPlaylists", {}) or {}).get("uploads")
            result[item["id"]] = ((item.get("snippet", {}).get("title") or "")[:200], uploads)
    return result


def _parse_rfc3339(value: str):
    """'2026-10-01T08:00:00Z' → naive UTC datetime。解釈できなければNone。"""
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _fetch_new_playlist_items(playlist_id: str, since: datetime, api_key: str, units: list) -> list:
    """アップロード再生リスト(新しい順)から since より後に公開された動画を取得し、
    公開日時の古い順の [{"video_id", "published_at"}] で返す。playlistItems.listは1ユニット/ページ。"""
    items = []
    page_token = None
    for _ in range(MAX_PLAYLIST_PAGES):
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50, "key": api_key}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(YOUTUBE_PLAYLIST_ITEMS_URL, params=params, timeout=20)
        resp.raise_for_status()
        units[0] += 1
        data = resp.json()
        reached_old = False
        for it in data.get("items", []):
            cd = it.get("contentDetails", {})
            published = _parse_rfc3339(cd.get("videoPublishedAt", ""))
            if not cd.get("videoId") or published is None:
                continue  # 非公開・削除済みなど
            if published <= since:
                reached_old = True
                continue
            items.append({"video_id": cd["videoId"], "published_at": published})
        page_token = data.get("nextPageToken")
        if reached_old or not page_token:
            break
    items.sort(key=lambda x: x["published_at"])
    return items


def _fetch_video_details(video_ids: list, api_key: str, units: list) -> dict:
    """videos.listを50件ずつまとめて呼び {video_id: 詳細} を返す(削除・非公開は含まれない)。"""
    result = {}
    ids = list(dict.fromkeys(video_ids))
    for i in range(0, len(ids), 50):
        resp = requests.get(
            YOUTUBE_VIDEOS_URL,
            params={"part": "snippet,contentDetails,statistics", "id": ",".join(ids[i:i + 50]),
                    "maxResults": 50, "key": api_key},
            timeout=20,
        )
        resp.raise_for_status()
        units[0] += 1
        for item in resp.json().get("items", []):
            sn = item.get("snippet", {})
            try:
                views = int(item.get("statistics", {}).get("viewCount", 0))
            except (TypeError, ValueError):
                views = 0
            result[item["id"]] = {
                "title": sn.get("title", ""),
                "description": sn.get("description", ""),
                "thumbnail": _best_thumbnail(sn.get("thumbnails", {})),
                "published_at": _parse_rfc3339(sn.get("publishedAt", "")),
                "duration": _parse_duration_iso8601(item.get("contentDetails", {}).get("duration", "")),
                "view_count": views,
            }
    return result


# ── ダウンロード ──────────────────────────────────────────────────────────

def _download_video(video_id: str) -> str:
    """動画をダウンロードして static/videos へ配置し、Article.video_file_path 形式
    ('videos/<id>.<ext>')を返す。失敗時は例外。手動追加(add_video_manual)と同じ取得オプション。"""
    import yt_dlp
    from video_collector import _find_downloaded_file

    base_dir = os.path.dirname(os.path.abspath(__file__))
    tmp_dir = os.path.join(tempfile.gettempdir(), "kpopwave_videos")
    os.makedirs(tmp_dir, exist_ok=True)
    opts = {
        "format": YOUTUBE_DL_FORMAT,
        "ffmpeg_location": os.path.join(base_dir, "ffmpeg", "bin"),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(tmp_dir, f"{video_id}.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        **_YT_DLP_JS_OPTS,
    }
    if os.path.exists(_YOUTUBE_COOKIE_FILE):
        opts["cookiefile"] = _YOUTUBE_COOKIE_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

    found = _find_downloaded_file(tmp_dir, video_id)
    if not found:
        raise RuntimeError("ダウンロードファイルが見つかりません")
    local_path, ext = found
    dest_dir = os.path.join(base_dir, "static", "videos")
    os.makedirs(dest_dir, exist_ok=True)
    dest_name = f"{video_id}.{ext}"
    shutil.copy2(local_path, os.path.join(dest_dir, dest_name))
    try:
        os.remove(local_path)
    except OSError:
        pass
    return f"videos/{dest_name}"


# ── 取得本体 ──────────────────────────────────────────────────────────────

def _known_video_ids() -> set:
    """既にDBにある動画(ステータス不問)・削除記録に残っている動画・候補テーブルにある動画
    (見送り・期限切れを含む全状態)のYouTube動画IDの集合。"""
    known = set()
    for (vid,) in db.session.query(WatchedCandidate.video_id):
        known.add(vid)
    for (url,) in db.session.query(Article.url).filter(
        or_(Article.url.like("%youtube.com%"), Article.url.like("%youtu.be%"))
    ):
        vid = extract_youtube_video_id(url)
        if vid:
            known.add(vid)
    for (vid,) in db.session.query(DeletedPostLog.youtube_video_id).filter(
        DeletedPostLog.youtube_video_id.isnot(None)
    ):
        known.add(vid)
    return known


def run_watch(app, dry_run: bool = False) -> dict:
    """有効な監視チャンネルの新着を取得し、条件に合う動画を候補テーブルへ追加する(毎朝のジョブと
    「今すぐ取得」の共通処理。ダウンロードはしない)。
    dry_run=TrueならDB書き込みを行わず、候補に追加される予定の動画を数えるだけ。
    戻り値: {"ok", "error", "dry_run", "api_units", "added_total", "channels": [...]}"""
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "別の取得処理が実行中です", "dry_run": dry_run,
                "api_units": 0, "added_total": 0, "channels": []}
    try:
        return _run_watch(app, dry_run)
    finally:
        _run_lock.release()


def _run_watch(app, dry_run: bool) -> dict:
    units = [0]
    result = {"ok": True, "error": None, "dry_run": dry_run, "api_units": 0,
              "added_total": 0, "channels": []}

    with app.app_context():
        api_key = get_youtube_api_key()
        if not api_key:
            result.update(ok=False, error="YouTube APIキーが設定されていません")
            return result

        run_started = datetime.utcnow()
        rows = WatchedChannel.query.filter_by(enabled=True).order_by(WatchedChannel.id.asc()).all()
        # 「その他」はタグ用の受け皿グループで、タイトルに現れる名前ではないので照合対象外
        groups = [(g.id, g.name) for g in Group.query.all() if g.name != "その他"]
        chans = [{
            "id": r.id, "account_id": r.account_id, "channel_id": r.channel_id, "name": r.channel_name,
            "fancam_required": bool(r.fancam_required), "playlist_id": r.uploads_playlist_id,
            "since": r.cursor_published_at or (run_started - timedelta(days=FIRST_RUN_DAYS)),
            "stat": {"channel_id": r.channel_id, "name": r.channel_name, "new": 0, "added": 0,
                     "skip_group": 0, "skip_fancam": 0, "skip_shorts": 0, "skip_long": 0,
                     "skip_gone": 0, "skip_dup": 0, "download_failed": 0, "error": None,
                     "titles": []},
        } for r in rows]
        known = _known_video_ids()
        fancam_keywords = get_fancam_keywords()

        # フェーズ1: 再生リストIDの確定と、前回以降の新着動画IDの取得(playlistItems.list: 1ユニット)
        missing = [c["channel_id"] for c in chans if not c["playlist_id"]]
        if missing:
            try:
                details = fetch_channel_details(missing, api_key, units)
            except Exception as exc:
                logger.error("[channel_watch] channels.list失敗: %s", exc)
                details = {}
            for c in chans:
                if not c["playlist_id"] and c["channel_id"] in details:
                    c["playlist_id"] = details[c["channel_id"]][1]
        for c in chans:
            c["items"] = []
            c["failed"] = False
            if not c["playlist_id"]:
                c["failed"] = True
                c["stat"]["error"] = "アップロード再生リストを取得できませんでした"
                continue
            try:
                items = _fetch_new_playlist_items(c["playlist_id"], c["since"], api_key, units)
            except Exception as exc:
                c["failed"] = True
                c["stat"]["error"] = f"新着取得エラー: {str(exc)[:120]}"
                logger.error("[channel_watch] %s playlistItems失敗: %s", c["name"], exc)
                continue
            c["stat"]["new"] = len(items)
            fresh = []
            for it in items:
                if it["video_id"] in known:
                    c["stat"]["skip_dup"] += 1
                else:
                    fresh.append(it)
            c["items"] = fresh

        # フェーズ2: 全チャンネル分の動画詳細をvideos.listで50件ずつまとめて取得
        all_ids = [it["video_id"] for c in chans for it in c["items"]]
        try:
            video_details = _fetch_video_details(all_ids, api_key, units) if all_ids else {}
        except Exception as exc:
            logger.error("[channel_watch] videos.list失敗: %s", exc)
            result.update(ok=False, error=f"videos.list失敗: {str(exc)[:120]}")
            result["api_units"] = units[0]
            result["channels"] = [c["stat"] for c in chans]
            return result

        # フェーズ3: チャンネルごとにフィルタ→ダウンロード→承認待ちへ登録(古い順)
        for c in chans:
            stat = c["stat"]
            if c["failed"]:
                continue
            new_cursor = run_started - CURSOR_MARGIN
            for it in c["items"]:
                vid = it["video_id"]
                d = video_details.get(vid)
                if d is None:
                    stat["skip_gone"] += 1
                    continue
                title = d["title"]
                if _is_program_shorts(title, d["duration"]):
                    stat["skip_shorts"] += 1
                    continue
                if d["duration"] > WATCH_MAX_DURATION_SEC:
                    stat["skip_long"] += 1
                    continue
                if c["fancam_required"] and not is_watch_fancam_title(title, fancam_keywords):
                    stat["skip_fancam"] += 1
                    continue
                matched = [(gid, gname) for gid, gname in groups if _matches_target_artist(title, "", gname)]
                if not matched:
                    stat["skip_group"] += 1
                    continue

                if dry_run:
                    stat["added"] += 1
                    stat["titles"].append(title[:80])
                else:
                    db.session.add(WatchedCandidate(
                        account_id=c["account_id"],
                        watched_channel_id=c["id"],
                        video_id=vid,
                        title=(title or "YouTube動画")[:500],
                        description=d["description"][:5000],
                        channel_id=c["channel_id"],
                        channel_name=c["name"],
                        thumbnail_url=d["thumbnail"] or None,
                        published_at=d["published_at"] or it["published_at"],
                        duration=d["duration"],
                        view_count=d["view_count"],
                        guessed_group=" / ".join(gname for _, gname in matched)[:200],
                        guessed_group_id=matched[0][0] if len(matched) == 1 else None,
                        status=CANDIDATE_NEW,
                    ))
                    try:
                        db.session.commit()
                    except IntegrityError:
                        db.session.rollback()   # 並行して同じ動画が追加された場合
                        stat["skip_dup"] += 1
                        continue
                    known.add(vid)
                    stat["added"] += 1
                    stat["titles"].append(title[:80])
                    logger.info("[channel_watch] 候補に追加: %s / %s", c["name"], title[:60])

                if stat["added"] >= MAX_IMPORT_PER_CHANNEL:
                    # 上限で打ち切り: 最後に処理した動画の公開日時までを済みとし、続きは次回に回す
                    new_cursor = it["published_at"]
                    break

            if not dry_run:
                row = db.session.get(WatchedChannel, c["id"])
                if row:
                    row.last_fetched_at = datetime.utcnow()
                    row.cursor_published_at = new_cursor
                    if c["playlist_id"] and not row.uploads_playlist_id:
                        row.uploads_playlist_id = c["playlist_id"]
                    db.session.commit()
            result["added_total"] += stat["added"]
            result["channels"].append(stat)

        # 取得自体に失敗したチャンネルも結果に載せる
        result["channels"].extend(c["stat"] for c in chans if c["failed"])
        result["api_units"] = units[0]
        logger.info("[channel_watch] 完了%s: 候補%d本追加 / APIユニット%d / %s",
                    "(ドライラン)" if dry_run else "", result["added_total"], units[0],
                    ", ".join(f"{s['name']}={s['added']}" for s in result["channels"]))
        return result


def start_background_run(app) -> bool:
    """「今すぐ取得」用。別スレッドで実行して即座に戻る。実行中ならFalse。"""
    if _state["running"]:
        return False

    def _target():
        try:
            _state["result"] = run_watch(app)
        except Exception as exc:
            logger.exception("[channel_watch] 手動実行で例外")
            _state["result"] = {"ok": False, "error": str(exc)[:200], "channels": [], "added_total": 0,
                                "api_units": 0, "dry_run": False}
        finally:
            _state["running"] = False

    _state.update(running=True, started_at=datetime.utcnow().isoformat(), result=None)
    threading.Thread(target=_target, name="channel_watch_manual", daemon=True).start()
    return True


def get_run_state() -> dict:
    return dict(_state)


# ── 候補の取り込み(バックグラウンド)・見送り・期限切れ ─────────────────────────

_import_lock = threading.Lock()
_import_state = {"queue": [], "current": None, "worker": False, "results": {}}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def start_candidate_import(app, ids: list) -> int:
    """候補をバックグラウンドでダウンロードして承認待ちへ取り込む。順番に1本ずつ処理する。
    既に待機中・処理中のIDは無視する。戻り値は新たに待機列へ入れた件数。"""
    with _import_lock:
        busy = set(_import_state["queue"])
        if _import_state["current"] is not None:
            busy.add(_import_state["current"])
        new_ids = [i for i in dict.fromkeys(ids) if i not in busy]
        for i in new_ids:
            _import_state["results"].pop(i, None)
        _import_state["queue"].extend(new_ids)
        if new_ids and not _import_state["worker"]:
            _import_state["worker"] = True
            threading.Thread(target=_import_worker, args=(app,), name="candidate_import", daemon=True).start()
    return len(new_ids)


def _import_worker(app):
    while True:
        with _import_lock:
            if not _import_state["queue"]:
                _import_state["current"] = None
                _import_state["worker"] = False
                return
            cand_id = _import_state["queue"].pop(0)
            _import_state["current"] = cand_id
        result = _import_one_candidate(app, cand_id)
        with _import_lock:
            _import_state["results"][cand_id] = result
            if len(_import_state["results"]) > 300:
                for k in list(_import_state["results"])[:100]:
                    _import_state["results"].pop(k, None)


def _import_one_candidate(app, cand_id: int) -> dict:
    """1件をダウンロードしてArticle(承認待ち)を作る。失敗時は候補を未確認のまま残し、理由を記録する。"""
    try:
        with app.app_context():
            cand = db.session.get(WatchedCandidate, cand_id)
            if cand is None or cand.status != CANDIDATE_NEW:
                return {"state": "failed", "error": "候補が見つからないか、処理済みです"}
            url = f"https://www.youtube.com/watch?v={cand.video_id}"
            existing = Article.query.filter_by(url=url).first()
            if existing:   # 他の経路で既に取り込まれていた場合はダウンロードせず取り込み済みにする
                cand.status = CANDIDATE_IMPORTED
                cand.imported_article_id = existing.id
                cand.status_changed_at = datetime.utcnow()
                cand.last_error = None
                db.session.commit()
                return {"state": "done", "article_id": existing.id}

            video_path = _download_video(cand.video_id)
            from video_collector import _is_fancam
            article = Article(
                feed_source=f"{WATCH_FEED_PREFIX}{cand.channel_name}",
                title=cand.title[:500],
                url=url,
                published_at=cand.published_at,
                raw_content=(cand.description or "")[:5000],
                thumbnail_url=cand.thumbnail_url,
                status="pending",
                content_type="video",
                video_file_path=video_path,
                is_fancam=_is_fancam(cand.title),
                view_count=cand.view_count,
                account_id=cand.account_id,
                group_id=cand.guessed_group_id,
                channel_id=cand.channel_id,
                channel_name=cand.channel_name,
                watched_channel_id=cand.watched_channel_id,
            )
            db.session.add(article)
            db.session.flush()
            cand.status = CANDIDATE_IMPORTED
            cand.imported_article_id = article.id
            cand.status_changed_at = datetime.utcnow()
            cand.last_error = None
            db.session.commit()
            logger.info("[channel_watch] 候補を取り込み: %s / %s", cand.channel_name, cand.title[:60])
            return {"state": "done", "article_id": article.id}
    except Exception as exc:
        message = _ANSI_RE.sub("", str(exc)).strip().splitlines()[-1][:250] if str(exc).strip() else "不明なエラー"
        logger.error("[channel_watch] 候補の取り込み失敗 id=%s: %s", cand_id, exc)
        try:
            with app.app_context():
                db.session.rollback()
                cand = db.session.get(WatchedCandidate, cand_id)
                if cand is not None and cand.status == CANDIDATE_NEW:
                    cand.last_error = message
                    db.session.commit()
        except Exception:
            logger.exception("[channel_watch] 失敗理由の記録に失敗 id=%s", cand_id)
        return {"state": "failed", "error": message}


def get_import_progress() -> dict:
    """取り込みの進行状況: active={id: queued|downloading}、results={id: {state: done|failed, error}}。"""
    with _import_lock:
        active = {i: "queued" for i in _import_state["queue"]}
        if _import_state["current"] is not None:
            active[_import_state["current"]] = "downloading"
        return {"active": active, "results": dict(_import_state["results"])}


def set_candidates_status(ids: list, status: str) -> int:
    """未確認の候補だけ状態を変更する(見送りなど)。戻り値は変更した件数。app context内で呼ぶこと。"""
    if not ids:
        return 0
    n = WatchedCandidate.query.filter(
        WatchedCandidate.id.in_(ids), WatchedCandidate.status == CANDIDATE_NEW,
    ).update({"status": status, "status_changed_at": datetime.utcnow()}, synchronize_session=False)
    db.session.commit()
    return n


def parse_fancam_keywords(raw: str) -> list:
    """カンマ区切り(半角・全角・読点)の文字列を、前後の空白を除いた小文字のキーワード一覧にする。"""
    seen = []
    for part in re.split(r"[,，、]", raw or ""):
        kw = part.strip().lower()
        if kw and kw not in seen:
            seen.append(kw)
    return seen


def get_fancam_keywords() -> list:
    """設定のキーワード一覧。未設定・空ならデフォルトを使う。app context内で呼ぶこと。"""
    return (parse_fancam_keywords(Setting.get(FANCAM_KEYWORDS_SETTING, DEFAULT_FANCAM_KEYWORDS))
            or parse_fancam_keywords(DEFAULT_FANCAM_KEYWORDS))


def is_watch_fancam_title(title: str, keywords: list) -> bool:
    """タイトルにキーワード(小文字化済み)のどれかが含まれるか(大文字小文字は区別しない)。"""
    t = (title or "").lower()
    return any(kw in t for kw in keywords)


def is_auto_fetch_enabled() -> bool:
    """毎朝の自動取得が有効か(未設定はON)。app context内で呼ぶこと。"""
    return (Setting.get(AUTO_FETCH_SETTING, "true") or "true").strip().lower() != "false"


def get_expire_days() -> int:
    try:
        return max(1, int(Setting.get(EXPIRE_DAYS_SETTING, str(DEFAULT_EXPIRE_DAYS)) or DEFAULT_EXPIRE_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_EXPIRE_DAYS


def expire_old_candidates() -> int:
    """見つけてから設定日数を過ぎた未確認の候補を「期限切れ」にする(動画ファイルはないので記録だけ残す)。
    app context内で呼ぶこと。戻り値は期限切れにした件数。"""
    cutoff = datetime.utcnow() - timedelta(days=get_expire_days())
    n = WatchedCandidate.query.filter(
        WatchedCandidate.status == CANDIDATE_NEW, WatchedCandidate.found_at < cutoff,
    ).update({"status": CANDIDATE_EXPIRED, "status_changed_at": datetime.utcnow()}, synchronize_session=False)
    db.session.commit()
    return n


# ── 追加・成績 ────────────────────────────────────────────────────────────

def add_watched_channel(app, account_id: int, raw_input: str):
    """URL・@ハンドル・チャンネルIDのいずれかから監視チャンネルを追加する。
    戻り値: (WatchedChannelまたはNone, エラーメッセージ)。解決処理は音楽番組検索の自由入力と共通。"""
    api_key = get_youtube_api_key()
    if not api_key:
        return None, "YouTube APIキーが設定されていません"
    channel_id = _resolve_free_channel_id(raw_input, api_key, app)
    if not channel_id:
        return None, f"チャンネルが見つかりません: {raw_input}（@ハンドル・チャンネルURL・チャンネルIDを確認してください）"
    if WatchedChannel.query.filter_by(account_id=account_id, channel_id=channel_id).first():
        return None, "このチャンネルは既に登録されています"
    try:
        details = fetch_channel_details([channel_id], api_key)
    except Exception as exc:
        return None, f"チャンネル情報の取得に失敗しました: {str(exc)[:100]}"
    if channel_id not in details:
        return None, "チャンネル情報を取得できませんでした"
    name, uploads = details[channel_id]
    row = WatchedChannel(account_id=account_id, channel_id=channel_id, channel_name=name or channel_id,
                         uploads_playlist_id=uploads)
    db.session.add(row)
    db.session.commit()
    return row, None


def compute_channel_stats(channel_ids: list, threshold_likes: int) -> dict:
    """監視チャンネルごとの成績を、残っている記事(Article)と削除記録(deleted_post_log)を合わせて集計する。
    - posts: 投稿本数(記事+削除記録)
    - judged: 判定済み本数(投稿から7日以上の記事 + 自動クリーンアップで削除された記録。手動削除は判定に含めない)
    - avg_likes: 判定済みの平均いいね / buzz: しきい値以上の本数 / deleted: 削除された本数
    - hit_rate: buzz / judged (judgedが0ならNone)"""
    stats = {cid: {"posts": 0, "judged": 0, "likes_sum": 0, "buzz": 0, "deleted": 0}
             for cid in channel_ids}
    if not channel_ids:
        return {}
    cutoff = datetime.utcnow() - timedelta(days=BUZZ_JUDGE_DAYS)

    articles = db.session.query(Article.channel_id, Article.like_count, Article.posted_at).filter(
        Article.channel_id.in_(channel_ids),
        or_(Article.status == "posted", and_(Article.status == "queued", Article.posted_at.isnot(None))),
    ).all()
    for cid, likes, posted_at in articles:
        s = stats[cid]
        s["posts"] += 1
        if posted_at is not None and posted_at <= cutoff and likes is not None:
            s["judged"] += 1
            s["likes_sum"] += likes
            s["buzz"] += 1 if likes >= threshold_likes else 0

    logs = db.session.query(DeletedPostLog.channel_id, DeletedPostLog.final_likes,
                            DeletedPostLog.delete_reason).filter(
        DeletedPostLog.channel_id.in_(channel_ids)).all()
    for cid, likes, reason in logs:
        s = stats[cid]
        s["posts"] += 1
        s["deleted"] += 1
        if not (reason or "").startswith("manual") and likes is not None:
            s["judged"] += 1
            s["likes_sum"] += likes
            s["buzz"] += 1 if likes >= threshold_likes else 0

    for s in stats.values():
        s["avg_likes"] = round(s["likes_sum"] / s["judged"]) if s["judged"] else None
        s["hit_rate"] = round(s["buzz"] / s["judged"] * 100) if s["judged"] else None
    return stats


def serialize_watched_channels(account_id: int = None) -> list:
    """設定画面用: 監視チャンネル一覧(成績つき)。"""
    q = WatchedChannel.query
    if account_id is not None:
        q = q.filter_by(account_id=account_id)
    rows = q.order_by(WatchedChannel.id.asc()).all()
    threshold = int(Setting.get("buzz_threshold_likes", "200") or "200")
    stats = compute_channel_stats([r.channel_id for r in rows], threshold)
    pending_counts = dict(
        db.session.query(Article.watched_channel_id, func.count(Article.id))
        .filter(Article.status == "pending", Article.watched_channel_id.isnot(None))
        .group_by(Article.watched_channel_id).all()
    )
    cand_counts = {}
    for wid, st, n in (
        db.session.query(WatchedCandidate.watched_channel_id, WatchedCandidate.status, func.count(WatchedCandidate.id))
        .group_by(WatchedCandidate.watched_channel_id, WatchedCandidate.status).all()
    ):
        cand_counts.setdefault(wid, {})[st] = n
    out = []
    for r in rows:
        s = stats.get(r.channel_id, {})
        cc = cand_counts.get(r.id, {})
        out.append({
            "id": r.id, "channel_id": r.channel_id, "channel_name": r.channel_name,
            "enabled": bool(r.enabled), "fancam_required": bool(r.fancam_required),
            "last_fetched_at": (r.last_fetched_at.isoformat() + "Z") if r.last_fetched_at else None,
            "memo": r.memo or "", "pending": pending_counts.get(r.id, 0),
            "posts": s.get("posts", 0), "judged": s.get("judged", 0),
            "avg_likes": s.get("avg_likes"), "buzz": s.get("buzz", 0),
            "deleted": s.get("deleted", 0), "hit_rate": s.get("hit_rate"),
            "cand_new": cc.get(CANDIDATE_NEW, 0), "cand_imported": cc.get(CANDIDATE_IMPORTED, 0),
            "cand_skipped": cc.get(CANDIDATE_SKIPPED, 0), "cand_expired": cc.get(CANDIDATE_EXPIRED, 0),
        })
    return out


# ── 初期データ ────────────────────────────────────────────────────────────

def seed_default_watched_channels(api_key: str) -> dict:
    """初期の監視チャンネルをKPOPアカウントに登録する。channel_idはArticleに保存済みの値を
    チャンネル名で引き、YouTube APIで実在確認する(実在しないIDは登録しない)。
    app contextの中で呼ぶこと。既登録分はスキップする。"""
    account_id = kpop_account_id()
    summary = {"account_id": account_id, "added": [], "not_found_in_db": [], "not_on_youtube": [],
               "already": []}
    if account_id is None:
        return summary

    candidates = {}
    for name in SEED_CHANNEL_NAMES:
        row = (
            db.session.query(Article.channel_id, func.count(Article.id))
            .filter(Article.channel_name == name, Article.channel_id.isnot(None))
            .group_by(Article.channel_id).order_by(func.count(Article.id).desc()).first()
        )
        if row:
            candidates[name] = row[0]
        else:
            summary["not_found_in_db"].append(name)

    details = fetch_channel_details(list(candidates.values()), api_key) if candidates else {}
    for name, cid in candidates.items():
        if cid not in details:
            summary["not_on_youtube"].append(f"{name}({cid})")
            continue
        if WatchedChannel.query.filter_by(account_id=account_id, channel_id=cid).first():
            summary["already"].append(name)
            continue
        api_name, uploads = details[cid]
        db.session.add(WatchedChannel(account_id=account_id, channel_id=cid, channel_name=api_name or name,
                                      uploads_playlist_id=uploads))
        summary["added"].append(f"{api_name or name}({cid})")
    db.session.commit()
    return summary
