"""チャンネル監視: 実績のあるYouTubeチャンネルの新着を毎日自動で承認待ちに取り込む。

APIクォータ節約のため、新着取得はsearch.list(100ユニット)ではなくアップロード再生リストの
playlistItems.list(1ユニット)を使い、動画詳細はvideos.listで50件ずつまとめて取得する。
既存のfancam検索・音楽番組検索の除外ルール(放送局チャンネル除外・MPD직캠等の除外キーワード)は
この監視経由の取り込みには適用しない(youtube_collector側の動作は変更しない)。
"""
import logging
import os
import shutil
import tempfile
import threading
from datetime import datetime, timedelta

import requests
from sqlalchemy import and_, func, or_

from config import YOUTUBE_DL_FORMAT
from channel_info import extract_youtube_video_id, get_youtube_api_key
from database import (
    Article, DeletedPostLog, Group, Setting, ThreadsAccount, WatchedChannel, db,
)
from youtube_collector import (
    YOUTUBE_CHANNELS_URL, YOUTUBE_VIDEOS_URL, _best_thumbnail, _is_fancam_title,
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
    """既にDBにある動画(ステータス不問)と、削除記録に残っている動画のYouTube動画IDの集合。"""
    known = set()
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
    """有効な監視チャンネルの新着を取得して承認待ちへ取り込む(毎朝のジョブと「今すぐ取得」の共通処理)。
    dry_run=Trueならダウンロード・DB書き込みを行わず、取り込み予定の動画を数えるだけ。
    戻り値: {"ok", "error", "dry_run", "api_units", "imported_total", "channels": [...]}"""
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "別の取得処理が実行中です", "dry_run": dry_run,
                "api_units": 0, "imported_total": 0, "channels": []}
    try:
        return _run_watch(app, dry_run)
    finally:
        _run_lock.release()


def _run_watch(app, dry_run: bool) -> dict:
    units = [0]
    result = {"ok": True, "error": None, "dry_run": dry_run, "api_units": 0,
              "imported_total": 0, "channels": []}

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
            "stat": {"channel_id": r.channel_id, "name": r.channel_name, "new": 0, "imported": 0,
                     "skip_group": 0, "skip_fancam": 0, "skip_shorts": 0, "skip_long": 0,
                     "skip_gone": 0, "skip_dup": 0, "download_failed": 0, "error": None,
                     "titles": []},
        } for r in rows]
        known = _known_video_ids()

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
                if c["fancam_required"] and not _is_fancam_title(title):
                    stat["skip_fancam"] += 1
                    continue
                matched = [(gid, gname) for gid, gname in groups if _matches_target_artist(title, "", gname)]
                if not matched:
                    stat["skip_group"] += 1
                    continue

                if dry_run:
                    stat["imported"] += 1
                    stat["titles"].append(title[:80])
                else:
                    try:
                        video_path = _download_video(vid)
                    except Exception as exc:
                        stat["download_failed"] += 1
                        logger.error("[channel_watch] %s ダウンロード失敗 %s: %s", c["name"], vid, exc)
                        continue
                    from video_collector import _is_fancam
                    article = Article(
                        feed_source=f"{WATCH_FEED_PREFIX}{c['name']}",
                        title=(title or "YouTube動画")[:500],
                        url=f"https://www.youtube.com/watch?v={vid}",
                        published_at=d["published_at"] or it["published_at"],
                        raw_content=d["description"][:5000],
                        thumbnail_url=d["thumbnail"] or None,
                        status="pending",
                        content_type="video",
                        video_file_path=video_path,
                        is_fancam=_is_fancam(title),
                        view_count=d["view_count"],
                        account_id=c["account_id"],
                        group_id=matched[0][0] if len(matched) == 1 else None,
                        channel_id=c["channel_id"],
                        channel_name=c["name"],
                        watched_channel_id=c["id"],
                    )
                    db.session.add(article)
                    db.session.commit()
                    known.add(vid)
                    stat["imported"] += 1
                    stat["titles"].append(title[:80])
                    logger.info("[channel_watch] 取り込み: %s / %s", c["name"], title[:60])

                if stat["imported"] >= MAX_IMPORT_PER_CHANNEL:
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
            result["imported_total"] += stat["imported"]
            result["channels"].append(stat)

        # 取得自体に失敗したチャンネルも結果に載せる
        result["channels"].extend(c["stat"] for c in chans if c["failed"])
        result["api_units"] = units[0]
        logger.info("[channel_watch] 完了%s: 取り込み%d本 / APIユニット%d / %s",
                    "(ドライラン)" if dry_run else "", result["imported_total"], units[0],
                    ", ".join(f"{s['name']}={s['imported']}" for s in result["channels"]))
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
            _state["result"] = {"ok": False, "error": str(exc)[:200], "channels": [], "imported_total": 0,
                                "api_units": 0, "dry_run": False}
        finally:
            _state["running"] = False

    _state.update(running=True, started_at=datetime.utcnow().isoformat(), result=None)
    threading.Thread(target=_target, name="channel_watch_manual", daemon=True).start()
    return True


def get_run_state() -> dict:
    return dict(_state)


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
    out = []
    for r in rows:
        s = stats.get(r.channel_id, {})
        out.append({
            "id": r.id, "channel_id": r.channel_id, "channel_name": r.channel_name,
            "enabled": bool(r.enabled), "fancam_required": bool(r.fancam_required),
            "last_fetched_at": (r.last_fetched_at.isoformat() + "Z") if r.last_fetched_at else None,
            "memo": r.memo or "", "pending": pending_counts.get(r.id, 0),
            "posts": s.get("posts", 0), "judged": s.get("judged", 0),
            "avg_likes": s.get("avg_likes"), "buzz": s.get("buzz", 0),
            "deleted": s.get("deleted", 0), "hit_rate": s.get("hit_rate"),
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
