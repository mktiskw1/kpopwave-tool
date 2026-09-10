import json
import logging
import os
import re
import secrets
import threading
import unicodedata
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError

from config import Config
from database import (
    Article, BuzzPost, ChapterClip, ChapterJob, Comment, DailyStat, EarlyAdvanceLog, Group, Hook, Member,
    PostStat, Setting, TextPostStock, ThreadsAccount, VideoTrimJob, get_active_account, db,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# yt-dlpの部分ダウンロード可否チェック(FFmpegFD.available())はffmpeg_locationを見ず
# システムPATHしか見ない上に結果をプロセス内でキャッシュするため、最初のyt-dlp呼び出しより
# 前、モジュール読み込み時点でPATHに同梱ffmpegを追加しておく必要がある
_ffmpeg_bin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin")
if _ffmpeg_bin_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _ffmpeg_bin_dir + os.pathsep + os.environ.get("PATH", "")

# 人気動画ではYouTube側のBot対策によりCookie無しのアクセスが403で拒否されることがあるため、
# 存在すればこのCookieファイル(Netscape形式)をyt-dlpに渡す。未配置ならNoneのまま(=従来通り無認証)。
_YOUTUBE_COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "youtube_cookies.txt")

# Cookie認証時、yt-dlpはJS署名解読が必要なクライアント(web_creator等)を使うようになる。
# デフォルトではJSランタイムはdenoのみが有効(このマシンには無い)で、node自体があってもyt-dlp公式の
# 署名解読スクリプト(GitHub上のyt-dlp/ejsから取得)のダウンロードが既定で禁止されているため失敗する。
# このマシンにはNode.js(v20以上)が入っているため、両方を明示的に有効化する。
_YT_DLP_JS_OPTS = {"js_runtimes": {"node": {}}, "remote_components": {"ejs:github"}}

_THREADS_SCOPES = (
    "threads_basic,threads_content_publish,threads_manage_replies,"
    "threads_read_replies,threads_manage_mentions,threads_manage_insights,"
    "threads_profile_discovery,threads_delete,threads_keyword_search,"
    "threads_share_to_instagram"
)

DEFAULT_YOUTUBE_CHANNELS = [
    {"name": "aespa",        "url": "https://www.youtube.com/@aespa"},
    {"name": "NewJeans",     "url": "https://www.youtube.com/@NewJeans_official"},
    {"name": "BLACKPINK",    "url": "https://www.youtube.com/@BLACKPINK"},
    {"name": "TWICE",        "url": "https://www.youtube.com/@TWICE"},
    {"name": "IVE",          "url": "https://www.youtube.com/@IVEstarship"},
    {"name": "LE SSERAFIM",  "url": "https://www.youtube.com/channel/UCs-QBT4qkj_YiQw1ZntDO3g"},
    {"name": "ILLIT",        "url": "https://www.youtube.com/@ILLIT_official"},
    {"name": "tripleS",      "url": "https://www.youtube.com/channel/UCJnL-TBcsYrF2SLs7tmiC8Q"},
]

DEFAULT_FEEDS = [
    {"name": "Soompi",      "url": "https://www.soompi.com/feed/"},
    {"name": "Koreaboo",    "url": "https://www.koreaboo.com/feed/"},
    {"name": "Hellokpop",   "url": "https://www.hellokpop.com/feed/"},
    {"name": "KpopPost",    "url": "https://kpoppost.com/feed/"},
    {"name": "NME K-Pop",   "url": "https://www.nme.com/tag/k-pop/feed"},
    {"name": "AsianJunkie", "url": "https://www.asianjunkie.com/feed/"},
    {"name": "TheBiasList", "url": "https://thebiaslist.com/feed/"},
    {"name": "KpopReviewed","url": "https://kpopreviewed.com/feed/"},
    {"name": "SeoulBeats",  "url": "https://seoulbeats.com/feed/"},
    # 日本語KPOPサイト（lang:ja → キーワードフィルタースキップ、AI判定のみ）
    {"name": "Kstyle",       "url": "https://news.google.com/rss/search?q=site:kstyle.com&hl=ja&gl=JP&ceid=JP:ja", "lang": "ja"},
    {"name": "BARKS",        "url": "https://barks.jp/feed/", "lang": "ja"},
    {"name": "Daebak Tokyo", "url": "https://daebak.tokyo/feed/", "lang": "ja"},
]


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        _init_default_settings()
        _migrate_db()

    # 動画保存用ディレクトリを起動時に作成
    videos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "videos")
    os.makedirs(videos_dir, exist_ok=True)

    return app


def _migrate_db():
    """既存DBに新カラムを追加する（SQLite用）。"""
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)

    # articles テーブル
    existing_articles = {c["name"] for c in inspector.get_columns("articles")}
    article_cols = [
        ("thumbnail_url", "VARCHAR(500)"),
        ("like_count", "INTEGER"),
        ("reply_count", "INTEGER"),
        ("repost_count", "INTEGER"),
        ("quote_count", "INTEGER"),
        ("engagement_fetched_at", "DATETIME"),
        ("post_style", "VARCHAR(20)"),
        ("image_urls", "TEXT"),
        ("content_type", "VARCHAR(20) DEFAULT 'article'"),
        ("video_file_path", "VARCHAR(500)"),
        ("is_fancam", "INTEGER DEFAULT 0"),
        ("view_count", "INTEGER"),
        ("account_id", "INTEGER"),
        ("group_id", "INTEGER"),
        ("member_id", "INTEGER"),
        ("is_favorite", "INTEGER DEFAULT 0"),
    ]
    with db.engine.connect() as conn:
        for col, typedef in article_cols:
            if col not in existing_articles:
                conn.execute(text(f"ALTER TABLE articles ADD COLUMN {col} {typedef}"))
                conn.commit()
                logger.info("DB migration: articles.%s added", col)

    # threads_accounts テーブル: content_topic 列
    existing_accounts_cols = {c["name"] for c in inspector.get_columns("threads_accounts")}
    account_cols = [
        ("content_topic", "VARCHAR(200)"),
    ]
    with db.engine.connect() as conn:
        for col, typedef in account_cols:
            if col not in existing_accounts_cols:
                conn.execute(text(f"ALTER TABLE threads_accounts ADD COLUMN {col} {typedef}"))
                conn.commit()
                logger.info("DB migration: threads_accounts.%s added", col)

    # threads_accounts テーブル: 既存の単一アカウント設定を初期レコードとして移行
    if ThreadsAccount.query.count() == 0:
        acquired_at = None
        acquired_at_str = Setting.get("threads_token_acquired_at", "")
        if acquired_at_str:
            try:
                acquired_at = datetime.fromisoformat(acquired_at_str)
            except ValueError:
                pass
        default_account = ThreadsAccount(
            account_label="kpopwave.daily",
            threads_user_id=Setting.get("threads_user_id", ""),
            threads_access_token=Setting.get("threads_access_token", ""),
            token_acquired_at=acquired_at,
            is_active=True,
        )
        db.session.add(default_account)
        db.session.commit()
        logger.info("DB migration: threads_accounts に初期アカウント作成 (id=%d, label=%s)",
                    default_account.id, default_account.account_label)

    # articles.account_id が未設定の既存レコードをデフォルトアカウントに紐付け
    default_account = ThreadsAccount.query.filter_by(account_label="kpopwave.daily").first()
    if default_account:
        with db.engine.connect() as conn:
            result = conn.execute(
                text("UPDATE articles SET account_id = :aid WHERE account_id IS NULL"),
                {"aid": default_account.id},
            )
            conn.commit()
            if result.rowcount:
                logger.info("DB migration: articles.account_id を %d 件バックフィル (account_id=%d)",
                            result.rowcount, default_account.id)

    # follow_candidates テーブル
    existing_fc = {c["name"] for c in inspector.get_columns("follow_candidates")}
    fc_cols = [
        ("follow_status", "VARCHAR(20)"),
        ("priority",      "VARCHAR(10)"),
    ]
    with db.engine.connect() as conn:
        for col, typedef in fc_cols:
            if col not in existing_fc:
                conn.execute(text(f"ALTER TABLE follow_candidates ADD COLUMN {col} {typedef}"))
                conn.commit()
                logger.info("DB migration: follow_candidates.%s added", col)

    # comments テーブル
    existing_comments = {c["name"] for c in inspector.get_columns("comments")}
    comment_cols = [
        ("is_liked", "INTEGER DEFAULT 0"),
    ]
    with db.engine.connect() as conn:
        for col, typedef in comment_cols:
            if col not in existing_comments:
                conn.execute(text(f"ALTER TABLE comments ADD COLUMN {col} {typedef}"))
                conn.commit()
                logger.info("DB migration: comments.%s added", col)

    # chapter_jobs テーブル: 既存ファイルからのチャプター分割用カラム
    existing_chapter_jobs = {c["name"] for c in inspector.get_columns("chapter_jobs")}
    chapter_job_cols = [
        ("source_local_path", "VARCHAR(500)"),
    ]
    with db.engine.connect() as conn:
        for col, typedef in chapter_job_cols:
            if col not in existing_chapter_jobs:
                conn.execute(text(f"ALTER TABLE chapter_jobs ADD COLUMN {col} {typedef}"))
                conn.commit()
                logger.info("DB migration: chapter_jobs.%s added", col)

    # post_stats テーブル: 投稿直後(60分以内)の初速記録用カラム
    existing_post_stats = {c["name"] for c in inspector.get_columns("post_stats")}
    post_stat_cols = [
        ("minute_offset", "INTEGER"),
    ]
    with db.engine.connect() as conn:
        for col, typedef in post_stat_cols:
            if col not in existing_post_stats:
                conn.execute(text(f"ALTER TABLE post_stats ADD COLUMN {col} {typedef}"))
                conn.commit()
                logger.info("DB migration: post_stats.%s added", col)

    # hooks テーブル: デフォルトフックの初回投入
    if Hook.query.count() == 0:
        default_hooks = {
            1: [
                "待って、これやばい。", "え、この子なに。", "これ知ってる人少ないと思う。",
                "布教させてください。", "好きにならない方が無理じゃない？", "これ好きな人いる？",
                "保存推奨。", "語彙力消えた。", "今のうちに見て。", "なんで知らなかったんだろ。",
            ],
            2: [
                "これマジで欲しい…", "新作きてる…！", "見つけた瞬間テンション上がった。",
                "これは即回さなきゃ。", "ガチャ勢は絶対チェックして。", "今回のクオリティやばい。",
                "うわ、これ欲しすぎる。", "推しキャラのガチャ来た…！", "この造形細かすぎない？",
                "コンプリートしたくなる…",
            ],
        }
        for account_id, phrases in default_hooks.items():
            for order, phrase in enumerate(phrases):
                db.session.add(Hook(account_id=account_id, phrase=phrase, display_order=order))
        db.session.commit()
        logger.info("DB migration: hooks にデフォルトフックを投入 (account_id=1: %d件, account_id=2: %d件)",
                    len(default_hooks[1]), len(default_hooks[2]))


def _fail_chapter_job(job, reason: str) -> None:
    """ChapterJobとその非終端(pending/processing)クリップをfailedに遷移させる。
    db.session.commitは呼び出し側で行う。"""
    job.status = "failed"
    job.error_message = reason
    for clip in ChapterClip.query.filter_by(job_id=job.id).filter(
        ChapterClip.status.in_(["pending", "processing"])
    ).all():
        clip.status = "failed"
        clip.error_message = reason


def _recover_orphaned_jobs():
    """アプリ起動時に、processingのまま残っているバックグラウンドジョブをfailedへ復旧する。

    このアプリはrun.py(ファイル監視watcher)が app.py を単一サブプロセスとして起動し、
    コード編集のたび taskkill /F /T でプロセスツリーごと強制終了して再起動する。
    バックグラウンドスレッド(_run_trim_job / _run_chapter_job)はその完了を待たずに
    道連れで殺されるため、DB上のstatusが 'processing' のまま永久に固まる。

    起動直後の時点では、これらのジョブを実行していたスレッドは(前プロセスと一緒に)
    確実に消滅しているため、残っている 'processing' は全て孤児と断定してよい。
    ffmpeg側のtimeout(600秒)はプロセス強制終了時には実行されないため、ここで検知する。
    """
    reason = "アプリ再起動によりバックグラウンド処理が中断されたため自動的に失敗扱いにしました。"

    stale_trim = VideoTrimJob.query.filter_by(status="processing").all()
    for job in stale_trim:
        job.status = "failed"
        job.error_message = reason

    stale_chapter = ChapterJob.query.filter_by(status="processing").all()
    for job in stale_chapter:
        _fail_chapter_job(job, reason)

    if stale_trim or stale_chapter:
        db.session.commit()
        logger.warning(
            "起動時ジョブ復旧: VideoTrimJob %d件 / ChapterJob %d件 を failed に遷移",
            len(stale_trim), len(stale_chapter),
        )


def _init_default_settings():
    defaults = {
        "rss_feeds": json.dumps(DEFAULT_FEEDS),
        "post_times": "09:00,15:00,21:00",
        "collect_interval_hours": "2",
        "youtube_collect_interval_hours": "6",
        "youtube_api_key": os.getenv("YOUTUBE_API_KEY", ""),
        "kpop_seed_accounts": "",
        "youtube_min_view_count": "5000000",
        "youtube_max_view_count": "0",
        "test_mode": "false",
        "early_advance_enabled": "true",
        "threads_user_id": os.getenv("THREADS_USER_ID", ""),
        "threads_access_token": os.getenv("THREADS_ACCESS_TOKEN", ""),
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "meta_app_id": os.getenv("META_APP_ID", ""),
        "meta_app_secret": os.getenv("META_APP_SECRET", ""),
        "app_base_url": os.getenv("APP_BASE_URL", "http://localhost:5000"),
        "youtube_channels": json.dumps(DEFAULT_YOUTUBE_CHANNELS),
    }
    for key, value in defaults.items():
        if not Setting.query.filter_by(key=key).first():
            db.session.add(Setting(key=key, value=value))
    db.session.commit()


app = create_app()


@app.template_filter("utc_to_jst")
def utc_to_jst_filter(dt):
    """UTC naive datetime → JST naive datetime (+9h)"""
    if dt is None:
        return dt
    return dt + timedelta(hours=9)


@app.context_processor
def inject_globals():
    account_id = _selected_account_id()
    legacy = get_active_account(app)
    legacy_id = legacy["id"] if legacy else None

    def _scope(query):
        return _account_query_scope(query, Article, account_id, legacy_id)

    nav_accounts = ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all()
    active_account = next((a for a in nav_accounts if a.id == account_id), None)
    # KPOP専用収集機能（YouTube収集・動画収集）の表示判定。
    # content_topic未設定＝KPOPアカウントという既存の運用規約に従う。
    # アカウントを解決できない場合は従来通り表示する（後方互換）。
    nav_is_kpop_account = (active_account is None) or not (active_account.content_topic or "").strip()

    return {
        "pending_count": _scope(Article.query.filter_by(status="pending")).count(),
        "queued_count": _scope(Article.query.filter_by(status="queued")).count(),
        "unread_comments_count": Comment.query.filter_by(is_read=0).count(),
        "youtube_min_view_count": Setting.get("youtube_min_view_count", "5000000"),
        "youtube_max_view_count": Setting.get("youtube_max_view_count", "0"),
        "nav_accounts": nav_accounts,
        "nav_active_account_id": account_id,
        "nav_is_kpop_account": nav_is_kpop_account,
    }


@app.template_filter("format_comment_time")
def format_comment_time_filter(ts_str):
    """Threads API のタイムスタンプ（ISO形式）→ JST 表示。"""
    if not ts_str:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(ts_str.replace("+0000", "+00:00"))
        jst = dt.astimezone(ZoneInfo("Asia/Tokyo"))
        return jst.strftime("%m/%d %H:%M")
    except Exception:
        return ts_str


@app.template_filter("json_loads")
def json_loads_filter(s):
    """JSON文字列をPythonオブジェクトに変換。失敗時は空リストを返す。"""
    if not s:
        return []
    try:
        import json as _json
        return _json.loads(s)
    except Exception:
        return []


@app.template_filter("from_json")
def from_json_filter(s):
    """JSON文字列をdictに変換。失敗時は空dictを返す。"""
    if not s:
        return {}
    try:
        import json as _json
        return _json.loads(s)
    except Exception:
        return {}


@app.context_processor
def inject_timedelta():
    return {"timedelta": timedelta}


# ── ダッシュボード ──────────────────────────────────────────────────────────


@app.route("/")
def index():
    account_id = _selected_account_id()
    legacy = get_active_account(app)
    legacy_id = legacy["id"] if legacy else None

    def _scope(query):
        return _account_query_scope(query, Article, account_id, legacy_id)

    stats = {
        s: _scope(Article.query.filter_by(status=s)).count()
        for s in ("pending", "queued", "posted", "rejected", "failed")
    }
    recent = _scope(Article.query).order_by(Article.created_at.desc()).limit(15).all()
    from youtube_collector import PROGRAM_CHANNELS, DEFAULT_PROGRAM_KEY, DEFAULT_TARGET_GROUP
    program_options = [{"key": k, "name": v["name"]} for k, v in PROGRAM_CHANNELS.items()]
    group_names = [g.name for g in Group.query.order_by(Group.name.asc()).all()]
    return render_template("index.html", stats=stats, recent=recent,
                            program_options=program_options, group_names=group_names,
                            DEFAULT_PROGRAM_KEY=DEFAULT_PROGRAM_KEY,
                            DEFAULT_TARGET_GROUP=DEFAULT_TARGET_GROUP)


# ── 承認待ち記事 ───────────────────────────────────────────────────────────


def _selected_account_id():
    """account_id解決順序: クエリパラメータ → セッション → レガシー（最古の）アクティブアカウント。
    解決したIDは常にセッションへ書き戻し、以降のリクエストに引き継ぐ。"""
    raw = request.args.get("account_id")
    if raw:
        try:
            resolved = int(raw)
        except ValueError:
            resolved = None
        if resolved is not None and ThreadsAccount.query.get(resolved):
            session["active_account_id"] = resolved
            return resolved

    session_id = session.get("active_account_id")
    if session_id is not None:
        if ThreadsAccount.query.get(session_id):
            return session_id
        session.pop("active_account_id", None)

    legacy = get_active_account(app)
    legacy_id = legacy["id"] if legacy else None
    if legacy_id is not None:
        session["active_account_id"] = legacy_id
    return legacy_id


def _account_query_scope(query, model_cls, account_id, legacy_id):
    """account_id でクエリをスコープする。レガシーアカウントは account_id IS NULL の記事も含める。"""
    if account_id is None:
        return query
    if account_id == legacy_id:
        return query.filter(or_(model_cls.account_id == account_id, model_cls.account_id.is_(None)))
    return query.filter(model_cls.account_id == account_id)


def _explicit_account_id(source):
    """操作対象アカウントIDを解決する。フロントから明示的に渡された値を最優先とし、
    未指定・不正値の場合のみ現在表示中アカウント（セッション経由）にフォールバックする。
    セッションは複数タブ間で共有され食い違いうるため、画面に描画された値を優先する。"""
    raw = source.get("account_id")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return _selected_account_id()


_PREVIEW_EXCLUDE = (
    "gstatic.com",
    "news.google.com",
    "googleusercontent.com",
    "lh3.google.com",
)
_PREVIEW_SMALL_HINTS = (
    "=s16", "=s24", "=s32", "=s48", "=s64",
    "/s16/", "/s24/", "/s32/", "/s48/", "/s64/",
    "/s16-", "/s24-", "/s32-", "/s48-", "/s64-",
    "16x16", "24x24", "32x32", "48x48", "64x64",
)


def _is_preview_valid_image(url: str) -> bool:
    """プレビュー表示に使用可能な画像URLか判定する（threads_api._is_valid_image_url と同一基準）。"""
    if not url or not url.startswith("http"):
        return False
    if any(d in url for d in _PREVIEW_EXCLUDE):
        return False
    low = url.lower()
    if any(h in low for h in _PREVIEW_SMALL_HINTS):
        return False
    return True


def _delete_video_files(video_file_path: str, static_dir: str) -> int:
    """動画ファイル本体・クリップ・オリジナルを削除する。削除したファイル数を返す。"""
    if not video_file_path:
        return 0
    base_name = os.path.splitext(os.path.basename(video_file_path))[0]
    videos_dir = os.path.join(static_dir, "videos")
    deleted = 0

    main_path = os.path.join(static_dir, video_file_path)
    if os.path.exists(main_path):
        try:
            os.remove(main_path)
            deleted += 1
        except OSError:
            pass

    if os.path.isdir(videos_dir):
        for fname in os.listdir(videos_dir):
            if fname.endswith(".mp4") and (
                fname.startswith(base_name + "_clip_")
                or fname.startswith(base_name + "_original")
            ):
                try:
                    os.remove(os.path.join(videos_dir, fname))
                    deleted += 1
                except OSError:
                    pass

    return deleted


def _build_image_list(thumbnail_url, image_urls_json, max_images=20):
    """投稿画像リストを構築する（threads_api.py と同一ロジック）。"""
    import json as _json
    imgs: list = []
    if _is_preview_valid_image(thumbnail_url):
        imgs.append(thumbnail_url)
    if image_urls_json:
        try:
            parsed = _json.loads(image_urls_json)
            for url in parsed:
                if _is_preview_valid_image(url) and url not in imgs:
                    imgs.append(url)
                    if len(imgs) >= max_images:
                        break
        except Exception:
            pass
    return imgs


def _is_manual_trim_clip(article) -> bool:
    """手動トリミング機能(_run_trim_job)で作成されたクリップかどうかを判定する。
    手動トリミングはURLフラグメントに '#clip_<timestamp>' を付与する。チャプター分割
    (chapter_job_confirm)・範囲指定ダウンロード(add_video_manual)はどちらも '#t=...' 形式
    でこれとは異なるため、フラグメントの形で区別できる。"""
    return "#clip_" in (article.url or "")


@app.route("/pending")
def pending():
    tab = request.args.get("tab", "all")
    account_id = _selected_account_id()
    legacy = get_active_account(app)
    legacy_id = legacy["id"] if legacy else None

    def _scope(query):
        return _account_query_scope(query, Article, account_id, legacy_id)

    all_pending = _scope(Article.query.filter_by(status="pending")).order_by(Article.created_at.desc()).all()

    counts = {
        "all":    len(all_pending),
        "rss":    0,
        "youtube": 0,
        "video":  0,
        "clipped": 0,
        "posted": _scope(Article.query.filter_by(status="posted", content_type="video")).count(),
    }
    for a in all_pending:
        src = a.feed_source or ""
        if (a.content_type or "article") == "video":
            counts["video"] += 1
            if _is_manual_trim_clip(a):
                counts["clipped"] += 1
        elif src.startswith("YouTube:"):
            counts["youtube"] += 1
        else:
            counts["rss"] += 1

    early_engagement_map = {}
    if tab == "posted":
        articles = (_scope(Article.query.filter_by(status="posted", content_type="video"))
                    .order_by(Article.created_at.desc())
                    .all())
        images_map = {}
        if articles:
            early_stats = (
                PostStat.query
                .filter(
                    PostStat.article_id.in_([a.id for a in articles]),
                    PostStat.minute_offset.isnot(None),
                )
                .order_by(PostStat.minute_offset.asc())
                .all()
            )
            for stat in early_stats:
                early_engagement_map.setdefault(stat.article_id, {})[stat.minute_offset] = stat.likes
    elif tab == "video":
        articles = [a for a in all_pending if (a.content_type or "article") == "video"]
        images_map = {}
    elif tab == "clipped":
        articles = [a for a in all_pending
                    if (a.content_type or "article") == "video" and _is_manual_trim_clip(a)]
        images_map = {}
    elif tab == "youtube":
        articles = [a for a in all_pending
                    if (a.feed_source or "").startswith("YouTube:")
                    and (a.content_type or "article") != "video"]
        images_map = {}
        for a in articles:
            images_map[a.id] = _build_image_list(a.thumbnail_url, a.image_urls)
    elif tab == "rss":
        articles = [a for a in all_pending
                    if not (a.feed_source or "").startswith("YouTube")
                    and (a.content_type or "article") != "video"]
        images_map = {}
        for a in articles:
            images_map[a.id] = _build_image_list(a.thumbnail_url, a.image_urls)
    else:
        articles = all_pending
        images_map = {}
        for a in articles:
            imgs = _build_image_list(a.thumbnail_url, a.image_urls)
            images_map[a.id] = imgs
            logger.debug("pending preview article=%d imgs=%d", a.id, len(imgs))

    active_trim_jobs = {
        j.source_article_id: j.id
        for j in VideoTrimJob.query.filter(
            VideoTrimJob.source_article_id.in_([a.id for a in articles]),
            VideoTrimJob.status == "processing",
        ).all()
    } if articles else {}

    video_paths = [a.video_file_path for a in articles if a.video_file_path]
    active_chapter_jobs = {
        j.source_local_path: j.id
        for j in ChapterJob.query.filter(
            ChapterJob.source_local_path.in_(video_paths),
            ChapterJob.status == "processing",
        ).all()
    } if video_paths else {}

    all_groups = Group.query.order_by(Group.name.asc()).all()
    group_by_id = {g.id: g for g in all_groups}
    group_tag_map = {}
    for a in articles:
        if a.group_id and a.group_id in group_by_id:
            group_tag_map[a.id] = group_by_id[a.group_id].name
        else:
            group_tag_map[a.id] = _guess_group_tag(a.title, all_groups)

    return render_template("pending.html", articles=articles, images_map=images_map,
                           active_tab=tab, counts=counts, now_utc=datetime.utcnow(),
                           active_trim_jobs=active_trim_jobs, active_chapter_jobs=active_chapter_jobs,
                           all_groups=all_groups, group_tag_map=group_tag_map,
                           early_engagement_map=early_engagement_map)


@app.route("/pending/bulk-delete", methods=["POST"])
def bulk_delete_articles():
    ids = request.form.getlist("ids")
    tab = request.form.get("tab", "all")
    if not ids:
        flash("記事が選択されていません", "secondary")
        return redirect(url_for("pending", tab=tab))
    int_ids = [int(i) for i in ids]
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    articles = Article.query.filter(Article.id.in_(int_ids)).all()
    targets = [a for a in articles if not a.is_favorite]
    skipped_favorite_count = len(articles) - len(targets)
    for a in targets:
        if a.video_file_path:
            _delete_video_files(a.video_file_path, static_dir)
    target_ids = [a.id for a in targets]
    if target_ids:
        for tid in target_ids:
            _cleanup_article_related_records(tid)
        Article.query.filter(Article.id.in_(target_ids)).delete(synchronize_session=False)
        db.session.commit()
    msg = f"{len(target_ids)} 件の記事を削除しました"
    if skipped_favorite_count:
        msg += f"（お気に入り登録済み {skipped_favorite_count} 件はスキップしました）"
    flash(msg, "warning")
    return redirect(url_for("pending", tab=tab))


@app.route("/pending/delete-all", methods=["POST"])
def delete_all_pending():
    account_id = _explicit_account_id(request.form)
    legacy = get_active_account(app)
    legacy_id = legacy["id"] if legacy else None

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    articles = _account_query_scope(
        Article.query.filter_by(status="pending"), Article, account_id, legacy_id
    ).all()
    for a in articles:
        if a.video_file_path:
            _delete_video_files(a.video_file_path, static_dir)
    count = len(articles)
    Article.query.filter(Article.id.in_([a.id for a in articles])).delete(synchronize_session=False)
    db.session.commit()
    flash(f"承認待ち記事 {count} 件をすべて削除しました", "warning")
    return redirect(url_for("pending", account_id=account_id) if account_id else url_for("pending"))


def _normalize_tag_name(name: str) -> str:
    """グループ・メンバー名の表記ゆれ（前後空白・全角/半角・大文字小文字）を吸収する正規化キーを作る。"""
    return unicodedata.normalize("NFKC", (name or "").strip()).lower()


@app.route("/api/groups/<int:id>", methods=["DELETE"])
def delete_group(id):
    """groupsマスタから削除する。既にこのグループ(またはその所属メンバー)でタグ付けされている
    記事がある場合、参照が壊れないよう group_id/member_id を NULL に戻してからグループを削除する
    (ブロックではなくNULL化を選択: 単純な操作で済み、記事自体は失われないため)。"""
    group = Group.query.get_or_404(id)
    member_ids = [m.id for m in Member.query.filter_by(group_id=id).all()]

    query = Article.query.filter(Article.group_id == id)
    if member_ids:
        query = Article.query.filter(
            db.or_(Article.group_id == id, Article.member_id.in_(member_ids))
        )
    articles_to_clear = query.all()
    for a in articles_to_clear:
        if a.group_id == id:
            a.group_id = None
        if a.member_id in member_ids:
            a.member_id = None

    Member.query.filter_by(group_id=id).delete(synchronize_session=False)
    db.session.delete(group)
    db.session.commit()
    return jsonify({"ok": True, "cleared_articles": len(articles_to_clear)})


def _resolve_group_and_member(group_name: str, member_name: str) -> tuple:
    """自由入力のグループ名・メンバー名からgroup_id・member_idを解決する。
    マスタに存在しなければ自動作成する。group_nameが空なら (None, None)。
    group_nameが空でmember_nameだけある場合はmember_nameを無視する。"""
    group_name = (group_name or "").strip()
    member_name = (member_name or "").strip()

    if not group_name:
        if member_name:
            logger.warning("グループ名なしでメンバー名のみ指定されたため無視: member_name=%r", member_name)
        return None, None

    norm = _normalize_tag_name(group_name)
    group = Group.query.filter_by(normalized_name=norm).first()
    if not group:
        group = Group(name=group_name, normalized_name=norm)
        db.session.add(group)
        db.session.flush()

    if not member_name:
        return group.id, None

    mnorm = _normalize_tag_name(member_name)
    member = Member.query.filter_by(group_id=group.id, normalized_name=mnorm).first()
    if not member:
        member = Member(group_id=group.id, name=member_name, normalized_name=mnorm)
        db.session.add(member)
        db.session.flush()

    return group.id, member.id


def _guess_group_tag(title: str, groups: list) -> str | None:
    """タイトルにgroupsマスタのいずれかのグループ名が単語として含まれていれば、そのグループ名を
    返す(承認待ち一覧のタグ表示用)。音楽番組検索のグループマッチングと同じロジックを再利用する。"""
    from youtube_collector import _matches_target_artist
    for g in groups:
        if _matches_target_artist(title, "", g.name):
            return g.name
    return None


def _guess_group_id(chapter_title: str) -> int | None:
    """"グループ名 (ハングル等) - 曲名" 形式のチャプタータイトルから、既存groupsマスタと
    正規化キーで完全一致するものがあれば group.id を返す。一致しなければ None(新規作成はしない)。"""
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
def approve_article(id):
    from scheduler import next_post_slot
    from datetime import timedelta

    article = Article.query.get_or_404(id)
    article.status = "queued"

    tag_data = request.get_json(silent=True) or {}
    group_name = tag_data.get("group_name", "")
    member_name = tag_data.get("member_name", "")
    if group_name or member_name:
        group_id, member_id = _resolve_group_and_member(group_name, member_name)
        article.group_id = group_id
        article.member_id = member_id

    slot_utc = next_post_slot(app, account_id=article.account_id)
    if slot_utc:
        article.scheduled_at = slot_utc
        slot_jst = slot_utc + timedelta(hours=9)
        slot_label = f"（{slot_jst.strftime('%m/%d %H:%M')} JST 予定）"
    else:
        slot_label = ""

    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "slot_label": slot_label})
    flash(f"キューに追加しました{slot_label}: {article.title[:50]}", "success")
    return redirect(url_for("pending"))


@app.route("/articles/<int:id>/reject", methods=["POST"])
def reject_article(id):
    article = Article.query.get_or_404(id)
    article.status = "rejected"
    db.session.commit()
    flash(f"却下しました: {article.title[:50]}", "secondary")
    return redirect(request.referrer or url_for("pending"))


_ARTICLE_RESTORE_FIELDS = [
    "feed_source", "title", "url", "published_at", "raw_content", "summary",
    "status", "thumbnail_url", "scheduled_at", "posted_at", "threads_post_id",
    "error_message", "created_at", "like_count", "view_count", "reply_count",
    "repost_count", "quote_count", "engagement_fetched_at", "post_style",
    "image_urls", "content_type", "video_file_path", "is_fancam", "account_id",
    "group_id", "member_id",
]
_ARTICLE_DATETIME_FIELDS = {
    "published_at", "scheduled_at", "posted_at", "created_at", "engagement_fetched_at",
}


def _article_snapshot(article):
    """削除前の記事を元に戻せるよう、復元に必要な全カラムをJSON化する（undo用）。"""
    data = {}
    for field in _ARTICLE_RESTORE_FIELDS:
        value = getattr(article, field)
        if field in _ARTICLE_DATETIME_FIELDS and value is not None:
            value = value.isoformat()
        data[field] = value
    return data


def _article_from_snapshot(snapshot):
    kwargs = {}
    for field in _ARTICLE_RESTORE_FIELDS:
        value = snapshot.get(field)
        if field in _ARTICLE_DATETIME_FIELDS and value:
            value = datetime.fromisoformat(value)
        kwargs[field] = value
    return Article(**kwargs)


def _cleanup_article_related_records(article_id: int) -> None:
    """記事削除前に、その記事を参照する子レコードの整合性を保つ。PostStat・VideoTrimJob
    (source側、そのジョブが「この記事を切り取る」ジョブである場合)は記事と一対で意味を持つ
    データのため一緒に削除する。VideoTrimJob(result側、このarticle_idが切り取り結果として
    生成されたクリップの場合)は、元の切り取りジョブの完了履歴自体は残す意味があるため
    削除はせずresult_article_idをNULL化するに留める。"""
    PostStat.query.filter_by(article_id=article_id).delete(synchronize_session=False)
    VideoTrimJob.query.filter_by(source_article_id=article_id).delete(synchronize_session=False)
    VideoTrimJob.query.filter_by(result_article_id=article_id).update(
        {"result_article_id": None}, synchronize_session=False
    )
    # EarlyAdvanceLogは前倒し投稿の実行履歴そのものに意味があるため、記事が消えても
    # ログ行自体は残し、参照だけNULL化する。
    EarlyAdvanceLog.query.filter_by(source_article_id=article_id).update(
        {"source_article_id": None}, synchronize_session=False
    )
    EarlyAdvanceLog.query.filter_by(target_article_id=article_id).update(
        {"target_article_id": None}, synchronize_session=False
    )


@app.route("/articles/<int:id>/delete", methods=["POST"])
def delete_article(id):
    article = Article.query.get_or_404(id)
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    if article.is_favorite:
        error = "お気に入り登録済みのため削除できません。先にお気に入りを解除してください。"
        if is_fetch:
            return jsonify({"ok": False, "error": error}), 400
        flash(error, "warning")
        return redirect(request.referrer or url_for("pending"))
    snapshot = _article_snapshot(article) if is_fetch else None
    _cleanup_article_related_records(id)
    db.session.delete(article)
    db.session.commit()
    if is_fetch:
        return jsonify({"ok": True, "snapshot": snapshot})
    flash("記事を削除しました", "warning")
    return redirect(request.referrer or url_for("pending"))


@app.route("/articles/<int:id>/toggle-favorite", methods=["POST"])
def toggle_favorite_article(id):
    article = Article.query.get_or_404(id)
    article.is_favorite = not article.is_favorite
    db.session.commit()
    return jsonify({"ok": True, "is_favorite": article.is_favorite})


@app.route("/articles/restore", methods=["POST"])
def restore_article():
    """deleteAjaxのundo用: スナップショットから記事を再作成する。"""
    data = request.get_json(force=True, silent=True) or {}
    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict):
        return jsonify({"ok": False, "error": "復元データがありません"}), 400
    try:
        article = _article_from_snapshot(snapshot)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": f"復元データが不正です: {exc}"}), 400
    db.session.add(article)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"ok": False, "error": "同じURLの記事が既に存在するため復元できませんでした"}), 409
    return jsonify({"ok": True, "id": article.id})


# ── URL手動追加 ────────────────────────────────────────────────────────────

_ADD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _extract_youtube_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        vid = parse_qs(parsed.query).get("v", [""])[0]
        if vid:
            return vid
        m = re.match(r"/(?:shorts|embed)/([a-zA-Z0-9_-]{11})", parsed.path)
        if m:
            return m.group(1)
    elif host == "youtu.be":
        return parsed.path.lstrip("/").split("?")[0]
    return ""


def _parse_iso_duration(s: str) -> int:
    """PT#H#M#S 形式を秒数に変換する。"""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, sec = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + sec


def _fetch_youtube_info(video_id: str) -> tuple:
    db_key = Setting.get("youtube_api_key", "")
    api_key = db_key or os.getenv("YOUTUBE_API_KEY", "")
    if api_key:
        try:
            resp = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "snippet", "id": video_id, "key": api_key},
                timeout=15,
            )
            items = resp.json().get("items", [])
            if items:
                sn = items[0]["snippet"]
                th = sn.get("thumbnails", {})
                thumbnail = (th.get("maxres") or th.get("high") or th.get("medium") or {}).get("url", "")
                return sn.get("title", ""), sn.get("description", "")[:5000], thumbnail, f"YouTube: {sn.get('channelTitle', 'YouTube')}"
        except Exception as e:
            logger.warning("YouTube API fetch error: %s", e)
    # oEmbed fallback（APIキー不要）
    try:
        r = requests.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=10,
        )
        d = r.json()
        return d.get("title", ""), "", f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg", f"YouTube: {d.get('author_name', 'YouTube')}"
    except Exception:
        return "", "", "", "YouTube"


def _fetch_article_info(url: str) -> tuple:
    try:
        resp = requests.get(url, headers={"User-Agent": _ADD_UA, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9,ja;q=0.8"}, timeout=15)
        if resp.status_code != 200:
            return "", "", "", ""
        html = resp.text
        # タイトル: og:title → <title>
        title = ""
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']{1,500})["\']|<meta[^>]+content=["\']([^"\']{1,500})["\'][^>]+property=["\']og:title["\']', html, re.IGNORECASE)
        if m:
            title = (m.group(1) or m.group(2) or "").strip()
        if not title:
            m2 = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
            if m2:
                title = m2.group(1).strip()
        # OGP画像
        thumbnail_url = ""
        m3 = re.search(r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.IGNORECASE)
        if m3:
            img = (m3.group(1) or m3.group(2) or "").strip()
            if img.startswith("http"):
                thumbnail_url = img
        # 本文
        clean = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.IGNORECASE)
        for tag in ("article", "main", "body"):
            bm = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", clean, re.IGNORECASE)
            if bm:
                clean = bm.group(1); break
        content = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", clean)).strip()[:5000]
        domain = urlparse(url).netloc.removeprefix("www.")
        return title, content, thumbnail_url, f"手動追加: {domain}"
    except Exception as e:
        logger.error("記事取得エラー: %s — %s", url, e)
        return "", "", "", ""


@app.route("/articles/add-from-url", methods=["POST"])
def add_article_from_url():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "URLを入力してください"})
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    yt_id = _extract_youtube_id(url)
    canonical_url = f"https://www.youtube.com/watch?v={yt_id}" if yt_id else url

    if Article.query.filter_by(url=canonical_url).first():
        return jsonify({"ok": False, "error": "このURLはすでに登録済みです"})

    if yt_id:
        title, content, thumbnail_url, feed_source = _fetch_youtube_info(yt_id)
    else:
        title, content, thumbnail_url, feed_source = _fetch_article_info(canonical_url)

    if not title:
        return jsonify({"ok": False, "error": "タイトルを取得できませんでした。URLを確認してください"})

    article = Article(
        feed_source=feed_source,
        title=title[:500],
        url=canonical_url,
        raw_content=content,
        thumbnail_url=thumbnail_url or None,
        status="pending",
        account_id=_explicit_account_id(data),
    )
    db.session.add(article)
    db.session.commit()
    logger.info("URL手動追加: id=%d source=%s title=%s", article.id, feed_source, title[:60])
    return jsonify({"ok": True, "id": article.id, "title": article.title, "feed_source": feed_source})


@app.route("/articles/<int:id>/edit", methods=["POST"])
def edit_article(id):
    article = Article.query.get_or_404(id)
    summary = (request.form.get("summary") or "").strip()
    if not summary:
        return jsonify({"success": False, "error": "要約が空です"})
    article.summary = summary
    db.session.commit()
    return jsonify({"success": True, "summary": summary, "length": len(summary)})


@app.route("/articles/<int:id>/update-images", methods=["POST"])
def update_article_images(id):
    article = Article.query.get_or_404(id)
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])
    if not isinstance(urls, list):
        return jsonify({"ok": False, "error": "urls must be a list"}), 400
    urls = [u for u in urls if isinstance(u, str) and u.strip()]
    article.thumbnail_url = urls[0] if urls else None
    article.image_urls = json.dumps(urls[1:], ensure_ascii=False) if len(urls) > 1 else None
    db.session.commit()
    return jsonify({"ok": True, "count": len(urls)})


@app.route("/articles/<int:id>/resummary", methods=["POST"])
def resummary_article(id):
    from summarizer import summarize_article

    style = (request.form.get("style") or "つぶやき型").strip()
    scheduled_at = (request.form.get("scheduled_at") or "").strip() or None
    logger.info("[resummary] article=%d style=%r scheduled_at=%r", id, style, scheduled_at)
    success = summarize_article(app, id, style=style, scheduled_at=scheduled_at)
    # summarize_article は内部で別 app_context を開くため、セッションを明示的にリフレッシュ
    db.session.expire_all()
    article = db.session.get(Article, id)
    logger.info("resummary article=%d success=%s error_message=%r", id, success, article.error_message if article else None)
    if success:
        return jsonify({"success": True, "summary": article.summary, "length": len(article.summary or "")})
    error_msg = (article.error_message if article else None) or "要約の生成に失敗しました（サーバーログを確認してください）"
    return jsonify({"success": False, "error": error_msg})


# ── 動画配信（ngrokブラウザ警告バイパス） ──────────────────────────────────


@app.route("/video/<path:filename>")
def serve_video(filename):
    """ngrok経由でThreads APIが動画を取得できるよう専用エンドポイントで配信する。"""
    response = send_from_directory("static/videos", filename)
    response.headers["ngrok-skip-browser-warning"] = "true"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# ── 投稿キュー ─────────────────────────────────────────────────────────────


@app.route("/queue")
def queue():
    account_id = _selected_account_id()
    legacy = get_active_account(app)
    legacy_id = legacy["id"] if legacy else None

    def _scope(query):
        return _account_query_scope(query, Article, account_id, legacy_id)

    queued = (
        _scope(Article.query.filter_by(status="queued"))
        .order_by(Article.scheduled_at.asc().nullsfirst(), Article.created_at.asc())
        .all()
    )
    posted = (
        _scope(Article.query.filter_by(status="posted"))
        .order_by(Article.posted_at.desc())
        .limit(30)
        .all()
    )
    failed = _scope(Article.query.filter_by(status="failed")).order_by(Article.updated_at.desc()).limit(10).all()
    images_map = {}
    for a in queued:
        if (a.content_type or "article") != "video":
            images_map[a.id] = _build_image_list(a.thumbnail_url, a.image_urls)

    return render_template("queue.html", queued=queued, posted=posted, failed=failed, images_map=images_map)


@app.route("/queue/<int:id>/schedule", methods=["POST"])
def schedule_article(id):
    article = Article.query.get_or_404(id)
    dt_str = (request.form.get("scheduled_at") or "").strip()
    if not dt_str:
        return jsonify({"success": False, "error": "日時を指定してください"})
    try:
        # フォームは JST で来るので UTC に変換 (9 時間引く)
        from datetime import timedelta
        dt_jst = datetime.fromisoformat(dt_str)
        dt_utc = dt_jst - timedelta(hours=9)
        article.scheduled_at = dt_utc
        db.session.commit()
        return jsonify({"success": True, "scheduled_at": dt_jst.strftime("%m/%d %H:%M")})
    except ValueError:
        return jsonify({"success": False, "error": "日時の形式が正しくありません"})


@app.route("/queue/<int:id>/post-now", methods=["POST"])
def post_now(id):
    from threads_api import post_to_threads

    article = Article.query.get_or_404(id)
    test_mode = Setting.get("test_mode", "true").lower() == "true"
    success, msg = post_to_threads(app, id, test_mode=test_mode, account_id=article.account_id)
    flash(msg, "success" if success else "danger")
    return redirect(url_for("queue"))


@app.route("/queue/<int:id>/unqueue", methods=["POST"])
def unqueue_article(id):
    article = Article.query.get_or_404(id)
    article.status = "pending"
    article.scheduled_at = None
    db.session.commit()
    flash("承認待ちに戻しました", "secondary")
    return redirect(url_for("queue"))


@app.route("/queue/<int:id>/retry", methods=["POST"])
def retry_article(id):
    article = Article.query.get_or_404(id)
    article.status = "queued"
    article.error_message = None
    db.session.commit()
    flash("再キューに追加しました", "info")
    return redirect(url_for("queue"))


@app.route("/queue/reorder", methods=["POST"])
def reorder_queue():
    """ドラッグ&ドロップ並び替え後に未来スロットを新順序で割り当てる。"""
    from datetime import timedelta
    from scheduler import get_weekly_schedule, _JST, _UTC, _DAY_KEYS

    data = request.get_json(silent=True) or {}
    ids = data.get("order", [])
    if not ids:
        return jsonify({"success": False, "error": "no ids"})

    try:
        id_to_art = {a.id: a for a in Article.query.filter(Article.id.in_(ids)).all()}
        ordered = [id_to_art[i] for i in ids if i in id_to_art]
        if not ordered:
            return jsonify({"success": False, "error": "articles not found"})

        legacy = get_active_account(app)
        legacy_id = legacy["id"] if legacy else None
        account_id = ordered[0].account_id
        if account_id is None:
            account_id = legacy_id

        # 並び替え対象以外のキュー済みスロットを占有セットに入れる（同一アカウントのみ）
        occupied_query = (
            Article.query.filter_by(status="queued")
                          .filter(~Article.id.in_(ids))
        )
        occupied_query = _account_query_scope(occupied_query, Article, account_id, legacy_id)
        occupied = {
            a.scheduled_at
            for a in occupied_query.all()
            if a.scheduled_at is not None
        }

        schedule = get_weekly_schedule(app, account_id=account_id)
        now_jst = datetime.now(_JST)

        def _next_future_slot():
            """次の空き未来スロット (UTC naive) を返す。"""
            for offset in range(14):
                check_date = now_jst.date() + timedelta(days=offset)
                day_key = _DAY_KEYS[check_date.weekday()]
                for t in sorted(schedule.get(day_key, [])):
                    try:
                        h, m = map(int, t.strip().split(":"))
                        slot_jst = datetime(
                            check_date.year, check_date.month, check_date.day,
                            h, m, tzinfo=_JST,
                        )
                        if slot_jst <= now_jst:
                            continue
                        slot_utc = slot_jst.astimezone(_UTC).replace(tzinfo=None)
                        if slot_utc not in occupied:
                            return slot_utc
                    except Exception:
                        pass
            return None

        # 新しい順序で未来スロットを順番に割り当て
        for art in ordered:
            slot = _next_future_slot()
            art.scheduled_at = slot
            if slot:
                occupied.add(slot)

        db.session.commit()
        logger.info(
            "Queue reordered: %s",
            [(a.id, str(a.scheduled_at)) for a in ordered],
        )
        return jsonify({"success": True})

    except Exception as exc:
        db.session.rollback()
        logger.error("Queue reorder error: %s", exc)
        return jsonify({"success": False, "error": str(exc)})


@app.route("/queue/<int:id>/prioritize", methods=["POST"])
def prioritize_queue_article(id):
    """選択したキュー記事を「次に投稿される1件」に割り込ませる。

    現在キューの先頭（次に投稿される予定）の記事と scheduled_at をスワップするだけで、
    投稿スケジュールの枠（時刻）自体は変更しない。元々先頭だった記事は2番目にずれる。
    対象は選択記事と同じアカウントのキューに限定し、他アカウントには影響しない。
    選択記事が既に先頭の場合は何もしない。
    """
    from datetime import timedelta

    article = Article.query.get_or_404(id)
    if article.status != "queued":
        return jsonify({"success": False, "error": "この記事はキューにありません"})

    legacy = get_active_account(app)
    legacy_id = legacy["id"] if legacy else None
    account_id = article.account_id if article.account_id is not None else legacy_id

    scoped = _account_query_scope(
        Article.query.filter_by(status="queued"), Article, account_id, legacy_id
    )
    queued = scoped.order_by(
        Article.scheduled_at.asc().nullsfirst(), Article.created_at.asc()
    ).all()

    if not queued:
        return jsonify({"success": False, "error": "キューが空です"})

    head = queued[0]
    if head.id == article.id:
        return jsonify({"success": False, "already_head": True,
                        "error": "この投稿はすでに次の投稿です"})

    # scheduled_at をスワップ（スケジュール枠自体は変えない）
    head.scheduled_at, article.scheduled_at = article.scheduled_at, head.scheduled_at

    # 両方 scheduled_at=None など、スワップしても並び順が変わらない場合は
    # created_at を繰り上げて確実に先頭へ出す
    if article.scheduled_at == head.scheduled_at:
        article.created_at = (head.created_at or datetime.utcnow()) - timedelta(seconds=1)

    db.session.commit()
    logger.info(
        "[prioritize] account_id=%s id=%d を先頭へ（旧先頭 id=%d とスワップ）"
        " new_head_scheduled=%s old_head_scheduled=%s",
        account_id, article.id, head.id, article.scheduled_at, head.scheduled_at,
    )
    return jsonify({"success": True})


# ── 設定 ───────────────────────────────────────────────────────────────────


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        for key in ("anthropic_api_key",
                    "collect_interval_hours",
                    "youtube_api_key", "youtube_collect_interval_hours",
                    "youtube_min_view_count", "youtube_max_view_count",
                    "meta_app_id", "meta_app_secret", "app_base_url"):
            Setting.set(key, (request.form.get(key) or "").strip())

        # Threads 認証情報は「手動で上書き」欄。空送信では絶対に消さない
        # (OAuth 済みの値をフォーム保存のたびに空で潰す事故を防ぐ)。
        # 非空で送られた場合のみ更新し、レガシーアカウント行にも反映する。
        _manual_uid = (request.form.get("threads_user_id") or "").strip()
        _manual_token = (request.form.get("threads_access_token") or "").strip()
        if _manual_uid or _manual_token:
            _legacy = (ThreadsAccount.query.filter_by(is_active=True)
                       .order_by(ThreadsAccount.id.asc()).first())
            if _manual_uid:
                Setting.set("threads_user_id", _manual_uid)
                if _legacy:
                    _legacy.threads_user_id = _manual_uid
            if _manual_token:
                Setting.set("threads_access_token", _manual_token)
                if _legacy:
                    _legacy.threads_access_token = _manual_token
                    _legacy.token_acquired_at = datetime.utcnow()
                    Setting.set("threads_token_acquired_at", datetime.utcnow().isoformat())
            db.session.commit()

        Setting.set("test_mode", "true" if request.form.get("test_mode") else "false")
        Setting.set("early_advance_enabled", "true" if request.form.get("early_advance_enabled") else "false")

        feed_names = request.form.getlist("feed_name")
        feed_urls = request.form.getlist("feed_url")
        feed_langs = request.form.getlist("feed_lang")
        feed_account_ids = request.form.getlist("feed_account_id")
        feeds = []
        for i, (n, u) in enumerate(zip(feed_names, feed_urls)):
            if not u.strip():
                continue
            feed = {"name": n.strip(), "url": u.strip()}
            lang = feed_langs[i].strip() if i < len(feed_langs) else ""
            if lang:
                feed["lang"] = lang
            try:
                acc_id = int(feed_account_ids[i]) if i < len(feed_account_ids) and feed_account_ids[i] else 1
            except ValueError:
                acc_id = 1
            feed["account_id"] = acc_id
            feeds.append(feed)
        Setting.set("rss_feeds", json.dumps(feeds))

        ch_names = request.form.getlist("youtube_channel_name")
        ch_urls = request.form.getlist("youtube_channel_url")
        channels = [{"name": n.strip(), "url": u.strip()}
                    for n, u in zip(ch_names, ch_urls) if u.strip()]
        Setting.set("youtube_channels", json.dumps(channels))

        flash("設定を保存しました", "success")

        if hasattr(app, "reschedule_post_jobs"):
            app.reschedule_post_jobs()

        return redirect(url_for("settings"))

    base_url = Setting.get("app_base_url", "http://localhost:5000").rstrip("/")

    # 「現在の認証状態」パネルはレガシー(最古のアクティブ)アカウントを表す。
    # settings のミラーキーはフォーム保存等でドリフトしやすいので、実体である
    # ThreadsAccount 行を優先し、無ければ settings にフォールバックする。
    legacy_account = (ThreadsAccount.query.filter_by(is_active=True)
                      .order_by(ThreadsAccount.id.asc()).first())
    panel_user_id = (legacy_account.threads_user_id if legacy_account else None) \
        or Setting.get("threads_user_id")
    panel_token = (legacy_account.threads_access_token if legacy_account else None) \
        or Setting.get("threads_access_token")

    # トークン有効期限の計算(レガシーアカウントの token_acquired_at を優先)
    acquired_at = legacy_account.token_acquired_at if (legacy_account and legacy_account.token_acquired_at) else None
    if acquired_at is None:
        try:
            acquired_at = datetime.fromisoformat(Setting.get("threads_token_acquired_at", ""))
        except (ValueError, TypeError):
            acquired_at = None
    threads_token_expires_in_days = None
    if acquired_at is not None:
        expires_at = acquired_at + timedelta(days=60)
        threads_token_expires_in_days = max(0, (expires_at - datetime.utcnow()).days)

    current = {
        "threads_user_id": panel_user_id,
        "threads_access_token": panel_token,
        "threads_token_expires_in_days": threads_token_expires_in_days,
        "anthropic_api_key": Setting.get("anthropic_api_key"),
        "collect_interval_hours": Setting.get("collect_interval_hours", "2"),
        "youtube_api_key": Setting.get("youtube_api_key"),
        "youtube_collect_interval_hours": Setting.get("youtube_collect_interval_hours", "6"),
        "youtube_min_view_count": Setting.get("youtube_min_view_count", "5000000"),
        "youtube_max_view_count": Setting.get("youtube_max_view_count", "0"),
        "test_mode": Setting.get("test_mode", "true") == "true",
        "early_advance_enabled": Setting.get("early_advance_enabled", "true") == "true",
        "rss_feeds": json.loads(Setting.get("rss_feeds", "[]") or "[]"),
        "youtube_channels": json.loads(Setting.get("youtube_channels", "[]") or "[]"),
        "meta_app_id": Setting.get("meta_app_id"),
        "meta_app_secret": Setting.get("meta_app_secret"),
        "app_base_url": base_url,
        "callback_url": base_url + "/auth/threads/callback",
    }
    accounts = ThreadsAccount.query.order_by(ThreadsAccount.id.asc()).all()
    return render_template("settings.html", settings=current, accounts=accounts)


@app.route("/api/quick-setting", methods=["POST"])
def quick_setting():
    data = request.get_json(silent=True) or {}
    key = data.get("key", "")
    value = str(data.get("value", ""))
    _allowed = {"youtube_min_view_count", "youtube_max_view_count"}
    if key not in _allowed:
        return jsonify({"ok": False, "error": "invalid key"}), 400
    Setting.set(key, value)
    return jsonify({"ok": True})


# ── 週間スケジュール ──────────────────────────────────────────────────────────


@app.route("/schedule", methods=["GET", "POST"])
def schedule():
    from scheduler import get_weekly_schedule, set_weekly_schedule, _DAY_KEYS

    account_id = _selected_account_id()

    if request.method == "POST":
        new_schedule = {}
        for day in _DAY_KEYS:
            raw_times = request.form.getlist(f"times_{day}")
            valid = []
            for t in raw_times:
                t = t.strip()
                if not t:
                    continue
                try:
                    h, m = t.split(":")
                    if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
                        valid.append(f"{int(h):02d}:{int(m):02d}")
                except Exception:
                    pass
            new_schedule[day] = sorted(set(valid))

        set_weekly_schedule(app, new_schedule, account_id=account_id)

        if hasattr(app, "reschedule_post_jobs"):
            app.reschedule_post_jobs()

        flash("週間スケジュールを保存しました", "success")
        return redirect(url_for("schedule"))

    _DAY_LABELS = {
        "mon": "月", "tue": "火", "wed": "水", "thu": "木",
        "fri": "金", "sat": "土", "sun": "日",
    }
    current = get_weekly_schedule(app, account_id)
    checked_hours = {
        day: {t[:2] for t in times if len(t) >= 2}
        for day, times in current.items()
    }
    return render_template(
        "schedule.html",
        schedule=current,
        day_keys=_DAY_KEYS,
        day_labels=_DAY_LABELS,
        checked_hours=checked_hours,
    )


TEXT_POST_MAX_CHARS = 500  # Threads API の投稿文字数上限


@app.route("/text-post", methods=["GET", "POST"])
def text_post():
    accounts = ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all()

    if request.method == "POST":
        body = (request.form.get("body") or "").strip()
        action = request.form.get("action") or ""
        try:
            account_id = int(request.form.get("account_id") or 0)
        except ValueError:
            account_id = 0

        account = ThreadsAccount.query.filter_by(id=account_id, is_active=True).first()

        if not body:
            return jsonify({"success": False, "message": "投稿文を入力してください"})
        if len(body) > TEXT_POST_MAX_CHARS:
            return jsonify({"success": False, "message": f"投稿文は{TEXT_POST_MAX_CHARS}文字以内で入力してください"})
        if not account:
            return jsonify({"success": False, "message": "アカウントを選択してください"})
        if action not in ("post_now", "queue", "stock"):
            return jsonify({"success": False, "message": "不正なリクエストです"})

        if action == "stock":
            stock = TextPostStock(account_id=account.id, body=body)
            db.session.add(stock)
            db.session.commit()
            logger.info("テキスト投稿ストック保存: id=%d account_id=%d", stock.id, account.id)
            return jsonify({"success": True, "message": "ストックに保存しました"})

        title = body[:30] + ("…" if len(body) > 30 else "")
        article = Article(
            feed_source="テキスト投稿",
            title=title,
            url=f"text-post:{uuid.uuid4().hex}",
            summary=body,
            status="queued",
            content_type="text",
            account_id=account.id,
        )
        if action == "queue":
            from scheduler import next_post_slot
            article.scheduled_at = next_post_slot(app, account_id=account.id)
        db.session.add(article)
        db.session.commit()
        logger.info("テキスト投稿作成: id=%d account_id=%d action=%s", article.id, account.id, action)

        if action == "queue":
            return jsonify({"success": True, "message": "キューに追加しました"})

        from threads_api import post_to_threads
        test_mode = Setting.get("test_mode", "true").lower() == "true"
        success, msg = post_to_threads(app, article.id, test_mode=test_mode, account_id=account.id)
        return jsonify({"success": success, "message": msg})

    # サイドバーで選択中のアクティブアカウントをデフォルトにする(キュー投稿実行と同じ解決方法)。
    # そのアカウントが非アクティブ化されている等で候補に無ければ先頭にフォールバックする。
    default_account_id = _selected_account_id()
    if accounts and default_account_id not in {acc.id for acc in accounts}:
        default_account_id = accounts[0].id

    stocks = (
        TextPostStock.query
        .filter_by(account_id=default_account_id)
        .order_by(TextPostStock.created_at.desc())
        .all()
    )

    return render_template(
        "text_post.html",
        accounts=accounts,
        default_account_id=default_account_id,
        max_chars=TEXT_POST_MAX_CHARS,
        stocks=stocks,
    )


@app.route("/api/text-stock/<int:id>", methods=["PUT", "DELETE"])
def text_stock_item(id):
    stock = TextPostStock.query.get_or_404(id)

    if request.method == "DELETE":
        db.session.delete(stock)
        db.session.commit()
        return jsonify({"success": True, "message": "削除しました"})

    body = (request.form.get("body") or "").strip()
    if not body:
        return jsonify({"success": False, "message": "投稿文を入力してください"})
    if len(body) > TEXT_POST_MAX_CHARS:
        return jsonify({"success": False, "message": f"投稿文は{TEXT_POST_MAX_CHARS}文字以内で入力してください"})

    stock.body = body
    db.session.commit()
    return jsonify({"success": True, "message": "更新しました"})


@app.route("/api/text-stock/<int:id>/queue", methods=["POST"])
def text_stock_to_queue(id):
    from scheduler import next_post_slot

    stock = TextPostStock.query.get_or_404(id)
    account = ThreadsAccount.query.get(stock.account_id)
    if not account:
        return jsonify({"success": False, "message": "紐づくアカウントが見つかりません"})

    title = stock.body[:30] + ("…" if len(stock.body) > 30 else "")
    article = Article(
        feed_source="テキスト投稿",
        title=title,
        url=f"text-post:{uuid.uuid4().hex}",
        summary=stock.body,
        status="queued",
        content_type="text",
        account_id=account.id,
        scheduled_at=next_post_slot(app, account_id=account.id),
    )
    db.session.add(article)
    db.session.delete(stock)
    db.session.commit()
    logger.info("ストックからキューに追加: article_id=%d account_id=%d", article.id, account.id)
    return jsonify({"success": True, "message": "キューに追加しました"})


# ── Threads OAuth 認証 ────────────────────────────────────────────────────


def _sync_threads_account_token(user_id: str, token: str, username: str = None,
                                 force_new: bool = False, label: str = None):
    """OAuth認証成功時にトークンを保存する。

    まず threads_user_id が一致する既存アカウントを探し、あればそのアカウントの
    トークンを更新する（＝「Threadsで認証する」時にログインしていたアカウントの
    再認証になる。田中アカウント等、レガシー以外の再認証もこれで可能）。

    一致が無い場合:
      force_new=True（「新しいアカウントを追加」）: threads_accounts に新規レコード追加。
      force_new=False（「トークンを再取得」）: 最古のアクティブアカウント（レガシー）を更新。

    settings テーブルのミラーキー（threads_access_token 等）は、更新対象が
    最古のアクティブアカウント（レガシー）のときだけ書き込む。
    """
    now = datetime.utcnow()
    first_active = (ThreadsAccount.query.filter_by(is_active=True)
                    .order_by(ThreadsAccount.id.asc()).first())
    legacy_id = first_active.id if first_active else None

    # 1) user_id 一致の既存アカウントがあればそれを更新（再認証）
    existing = ThreadsAccount.query.filter_by(threads_user_id=user_id).first()
    if existing:
        existing.threads_access_token = token
        existing.token_acquired_at = now
        existing.is_active = True
        if existing.id == legacy_id:
            Setting.set("threads_access_token", token)
            Setting.set("threads_user_id", user_id)
            Setting.set("threads_token_acquired_at", now.isoformat())
        db.session.commit()
        return existing, False

    # 2) 一致なし
    if force_new:
        account = ThreadsAccount(
            account_label=label or username or f"account_{user_id}",
            threads_user_id=user_id,
            threads_access_token=token,
            token_acquired_at=now,
            is_active=True,
        )
        db.session.add(account)
        db.session.commit()
        return account, True

    Setting.set("threads_access_token", token)
    Setting.set("threads_user_id", user_id)
    Setting.set("threads_token_acquired_at", now.isoformat())

    if first_active:
        first_active.threads_user_id = user_id
        first_active.threads_access_token = token
        first_active.token_acquired_at = now
        account = first_active
        created = False
    else:
        account = ThreadsAccount(
            account_label=username or "default",
            threads_user_id=user_id,
            threads_access_token=token,
            token_acquired_at=now,
            is_active=True,
        )
        db.session.add(account)
        created = True
    db.session.commit()
    return account, created


@app.route("/auth/threads/start")
def threads_auth_start():
    app_id = Setting.get("meta_app_id")
    app_secret = Setting.get("meta_app_secret")
    if not app_id or not app_secret:
        flash("Meta App ID と App Secret を設定・保存してから認証を開始してください", "warning")
        return redirect(url_for("settings"))

    state = secrets.token_urlsafe(32)
    session["threads_oauth_state"] = state

    base_url = Setting.get("app_base_url", "http://localhost:5000").rstrip("/")
    redirect_uri = base_url + "/auth/threads/callback"

    auth_url = "https://threads.net/oauth/authorize?" + urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": _THREADS_SCOPES,
        "response_type": "code",
        "state": state,
    })
    return redirect(auth_url)


@app.route("/accounts/start-oauth", methods=["POST"])
def accounts_start_oauth():
    """新しいThreadsアカウントを追加するためのOAuthフローを開始する（既存アカウントは変更しない）。"""
    app_id = Setting.get("meta_app_id")
    app_secret = Setting.get("meta_app_secret")
    if not app_id or not app_secret:
        flash("Meta App ID と App Secret を設定・保存してから認証を開始してください", "warning")
        return redirect(url_for("settings"))

    label = (request.form.get("account_label") or "").strip()
    if not label:
        flash("アカウント名を入力してください", "warning")
        return redirect(url_for("settings"))

    state = secrets.token_urlsafe(32)
    session["threads_oauth_state"] = state
    session["threads_oauth_new_account"] = True
    session["threads_oauth_new_label"] = label

    base_url = Setting.get("app_base_url", "http://localhost:5000").rstrip("/")
    redirect_uri = base_url + "/auth/threads/callback"

    auth_url = "https://threads.net/oauth/authorize?" + urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": _THREADS_SCOPES,
        "response_type": "code",
        "state": state,
    })
    return redirect(auth_url)


@app.route("/accounts/<int:id>/rename", methods=["POST"])
def rename_account(id):
    account = ThreadsAccount.query.get_or_404(id)
    label = (request.form.get("account_label") or "").strip()
    if not label:
        return jsonify({"ok": False, "error": "アカウント名を入力してください"}), 400
    account.account_label = label
    db.session.commit()
    return jsonify({"ok": True, "account_label": account.account_label})


@app.route("/accounts/<int:id>/content-topic", methods=["POST"])
def update_account_content_topic(id):
    account = ThreadsAccount.query.get_or_404(id)
    topic = (request.form.get("content_topic") or "").strip()
    account.content_topic = topic or None
    db.session.commit()
    return jsonify({"ok": True, "content_topic": account.content_topic or ""})


@app.route("/accounts/<int:id>/toggle-active", methods=["POST"])
def toggle_account_active(id):
    account = ThreadsAccount.query.get_or_404(id)
    account.is_active = not account.is_active
    db.session.commit()
    if hasattr(app, "reschedule_post_jobs"):
        app.reschedule_post_jobs()
    flash(
        f"{account.account_label} を{'有効化' if account.is_active else '無効化'}しました",
        "success",
    )
    return redirect(url_for("settings"))


@app.route("/accounts/switch/<int:id>")
def switch_account(id):
    """サイドバーのアカウント切り替えドロップダウンから呼ばれる。セッションに保存して元のページへ戻る。"""
    account = ThreadsAccount.query.get_or_404(id)
    session["active_account_id"] = account.id
    referrer = request.referrer or ""
    # /hooks/<id>はURLにaccount_idを直接含むため、他ページと違いセッション更新だけでは切り替わらない
    if urlparse(referrer).path.startswith("/hooks/"):
        return redirect(url_for("hooks_page", account_id=account.id))
    return redirect(referrer or url_for("index"))


@app.route("/hooks/<int:account_id>")
def hooks_page(account_id):
    account = ThreadsAccount.query.get_or_404(account_id)
    hooks = (
        Hook.query.filter_by(account_id=account_id)
        .order_by(Hook.last_used_at.asc(), Hook.display_order.asc())
        .all()
    )
    next_hook_id = hooks[0].id if hooks else None
    return render_template("hooks.html", account=account, hooks=hooks, next_hook_id=next_hook_id)


@app.route("/hooks/<int:account_id>/add", methods=["POST"])
def add_hook(account_id):
    ThreadsAccount.query.get_or_404(account_id)
    phrase = (request.form.get("phrase") or "").strip()
    if not phrase:
        return jsonify({"ok": False, "error": "フレーズを入力してください"}), 400
    max_order = db.session.query(db.func.max(Hook.display_order)).filter(Hook.account_id == account_id).scalar()
    hook = Hook(account_id=account_id, phrase=phrase, display_order=(max_order or 0) + 1)
    db.session.add(hook)
    db.session.commit()
    return jsonify({"ok": True, "id": hook.id})


@app.route("/hooks/<int:id>/edit", methods=["POST"])
def edit_hook(id):
    hook = Hook.query.get_or_404(id)
    phrase = (request.form.get("phrase") or "").strip()
    if not phrase:
        return jsonify({"ok": False, "error": "フレーズを入力してください"}), 400
    hook.phrase = phrase
    db.session.commit()
    return jsonify({"ok": True, "phrase": hook.phrase})


@app.route("/hooks/<int:id>/delete", methods=["POST"])
def delete_hook(id):
    hook = Hook.query.get_or_404(id)
    db.session.delete(hook)
    db.session.commit()
    return jsonify({"ok": True})


_ANALYTICS_ACCOUNT_ID = 1


@app.route("/analytics")
def analytics():
    daily_rows = (
        DailyStat.query
        .filter_by(account_id=_ANALYTICS_ACCOUNT_ID)
        .order_by(DailyStat.stat_date.asc())
        .all()
    )
    daily_labels = [d.stat_date.strftime("%Y-%m-%d") for d in daily_rows]
    daily_followers = [d.followers_count for d in daily_rows]
    daily_views = [d.views_count for d in daily_rows]

    group_rows = [
        dict(row) for row in db.session.execute(text("""
            SELECT g.name AS name, COUNT(*) AS post_count,
                   AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
            FROM post_stats ps
            JOIN articles a ON a.id = ps.article_id
            JOIN groups g ON g.id = a.group_id
            WHERE ps.is_final = 1 AND ps.day_index <= 7 AND a.account_id = :account_id
            GROUP BY g.id
            ORDER BY avg_likes DESC
        """), {"account_id": _ANALYTICS_ACCOUNT_ID}).mappings().all()
    ]

    member_rows = [
        dict(row) for row in db.session.execute(text("""
            SELECT g.name AS group_name, m.name AS member_name, COUNT(*) AS post_count,
                   AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
            FROM post_stats ps
            JOIN articles a ON a.id = ps.article_id
            JOIN members m ON m.id = a.member_id
            JOIN groups g ON g.id = m.group_id
            WHERE ps.is_final = 1 AND ps.day_index <= 7 AND a.account_id = :account_id AND a.member_id IS NOT NULL
            GROUP BY m.id
            ORDER BY avg_likes DESC
        """), {"account_id": _ANALYTICS_ACCOUNT_ID}).mappings().all()
    ]

    hour_rows = [
        dict(row) for row in db.session.execute(text("""
            SELECT CAST(strftime('%H', datetime(a.posted_at, '+9 hours')) AS INTEGER) AS hour,
                   COUNT(*) AS post_count,
                   AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
            FROM post_stats ps
            JOIN articles a ON a.id = ps.article_id
            WHERE ps.is_final = 1 AND ps.day_index <= 7 AND a.account_id = :account_id AND a.posted_at IS NOT NULL
            GROUP BY hour
            ORDER BY hour ASC
        """), {"account_id": _ANALYTICS_ACCOUNT_ID}).mappings().all()
    ]

    _WEEKDAY_LABELS = {0: "日", 1: "月", 2: "火", 3: "水", 4: "木", 5: "金", 6: "土"}
    weekday_rows = [
        {**dict(row), "weekday_label": _WEEKDAY_LABELS[row["weekday"]]}
        for row in db.session.execute(text("""
            SELECT CAST(strftime('%w', datetime(a.posted_at, '+9 hours')) AS INTEGER) AS weekday,
                   COUNT(*) AS post_count,
                   AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
            FROM post_stats ps
            JOIN articles a ON a.id = ps.article_id
            WHERE ps.is_final = 1 AND ps.day_index <= 7 AND a.account_id = :account_id AND a.posted_at IS NOT NULL
            GROUP BY weekday
            ORDER BY CASE weekday WHEN 0 THEN 7 ELSE weekday END ASC
        """), {"account_id": _ANALYTICS_ACCOUNT_ID}).mappings().all()
    ]

    early_advance_logs = (
        EarlyAdvanceLog.query
        .order_by(EarlyAdvanceLog.created_at.desc())
        .limit(20)
        .all()
    )

    return render_template(
        "analytics.html",
        daily_labels=daily_labels,
        daily_followers=daily_followers,
        daily_views=daily_views,
        daily_rows=daily_rows,
        group_rows=group_rows,
        member_rows=member_rows,
        hour_rows=hour_rows,
        weekday_rows=weekday_rows,
        early_advance_logs=early_advance_logs,
    )


@app.route("/analytics/daily-stats/add", methods=["POST"])
def add_daily_stat():
    date_str = (request.form.get("stat_date") or "").strip()
    followers_str = (request.form.get("followers_count") or "").strip()
    views_str = (request.form.get("views_count") or "").strip()

    if not date_str or not followers_str:
        return jsonify({"ok": False, "error": "日付とフォロワー数は必須です"}), 400
    try:
        stat_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        followers_count = int(followers_str)
        views_count = int(views_str) if views_str else None
    except ValueError:
        return jsonify({"ok": False, "error": "入力値が不正です"}), 400

    row = DailyStat.query.filter_by(account_id=_ANALYTICS_ACCOUNT_ID, stat_date=stat_date).first()
    if row:
        row.followers_count = followers_count
        row.views_count = views_count
        row.source = "manual"
    else:
        db.session.add(DailyStat(
            account_id=_ANALYTICS_ACCOUNT_ID, stat_date=stat_date,
            followers_count=followers_count, views_count=views_count, source="manual",
        ))
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/auth/threads/manual")
def threads_auth_manual():
    app_id = Setting.get("meta_app_id")
    app_secret = Setting.get("meta_app_secret")
    if not app_id or not app_secret:
        flash("Meta App ID と App Secret を設定・保存してから認証を開始してください", "warning")
        return redirect(url_for("settings"))

    state = secrets.token_urlsafe(32)
    session["threads_oauth_state"] = state

    base_url = Setting.get("app_base_url", "http://localhost:5000").rstrip("/")
    redirect_uri = base_url + "/auth/threads/callback"

    auth_url = "https://threads.net/oauth/authorize?" + urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": _THREADS_SCOPES,
        "response_type": "code",
        "state": state,
    })
    return render_template("auth_manual.html", auth_url=auth_url, redirect_uri=redirect_uri, state=state)


@app.route("/auth/threads/exchange", methods=["POST"])
def threads_auth_exchange():
    code = (request.form.get("code") or "").strip()
    state = (request.form.get("state") or "").strip()

    if not code:
        flash("認証コードを入力してください", "warning")
        return redirect(url_for("threads_auth_manual"))

    if not state or state != session.pop("threads_oauth_state", None):
        flash("セッションが切れました。ページを再読み込みしてやり直してください", "danger")
        return redirect(url_for("threads_auth_manual"))

    is_new_account = session.pop("threads_oauth_new_account", False)
    new_account_label = session.pop("threads_oauth_new_label", None)

    app_id = Setting.get("meta_app_id")
    app_secret = Setting.get("meta_app_secret")
    base_url = Setting.get("app_base_url", "http://localhost:5000").rstrip("/")
    redirect_uri = base_url + "/auth/threads/callback"

    try:
        resp = requests.post(
            "https://graph.threads.net/oauth/access_token",
            data={
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
        resp.raise_for_status()
        short_token = resp.json().get("access_token")
        if not short_token:
            raise ValueError(f"短期トークンが見つかりません: {resp.text}")

        resp2 = requests.get(
            "https://graph.threads.net/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": app_secret,
                "access_token": short_token,
            },
            timeout=15,
        )
        resp2.raise_for_status()
        resp2_data = resp2.json()
        long_token = resp2_data.get("access_token")
        if not long_token:
            raise ValueError(f"長期トークンが見つかりません: {resp2.text}")
        expires_in_days = resp2_data.get("expires_in", 5184000) // 86400

        resp3 = requests.get(
            "https://graph.threads.net/v1.0/me",
            params={"fields": "id,username", "access_token": long_token},
            timeout=15,
        )
        resp3.raise_for_status()
        user_data = resp3.json()
        user_id = user_data.get("id", "")
        username = user_data.get("username", "")

        account, created = _sync_threads_account_token(
            user_id, long_token, username,
            force_new=is_new_account, label=new_account_label,
        )
        if created:
            if hasattr(app, "reschedule_post_jobs"):
                app.reschedule_post_jobs()
            flash(
                f"新しいアカウント「{account.account_label}」を追加しました！ @{username}（ID: {user_id}）"
                f"有効期限：{expires_in_days}日後",
                "success",
            )
        else:
            flash(
                f"「{account.account_label}」のトークンを更新しました！ @{username}（ID: {user_id}）"
                f"有効期限：{expires_in_days}日後",
                "success",
            )
    except Exception as e:
        logger.exception("Threads OAuth 処理エラー（手動コード）")
        flash(f"認証処理中にエラーが発生しました: {e}", "danger")
        return redirect(url_for("threads_auth_manual"))

    return redirect(url_for("settings"))


@app.route("/auth/threads/callback")
def threads_auth_callback():
    error = request.args.get("error")
    if error:
        desc = request.args.get("error_description", error)
        flash(f"認証エラー: {desc}", "danger")
        return redirect(url_for("settings"))

    state = request.args.get("state")
    if not state or state != session.pop("threads_oauth_state", None):
        flash("不正なリクエストです（state パラメータ不一致）", "danger")
        return redirect(url_for("settings"))

    is_new_account = session.pop("threads_oauth_new_account", False)
    new_account_label = session.pop("threads_oauth_new_label", None)

    code = request.args.get("code")
    if not code:
        flash("認証コードが取得できませんでした", "danger")
        return redirect(url_for("settings"))

    app_id = Setting.get("meta_app_id")
    app_secret = Setting.get("meta_app_secret")
    base_url = Setting.get("app_base_url", "http://localhost:5000").rstrip("/")
    redirect_uri = base_url + "/auth/threads/callback"

    try:
        # 短期アクセストークン取得
        resp = requests.post(
            "https://graph.threads.net/oauth/access_token",
            data={
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
        resp.raise_for_status()
        short_token = resp.json().get("access_token")
        if not short_token:
            raise ValueError(f"短期トークンが見つかりません: {resp.text}")

        # 長期アクセストークンに交換（60日有効）
        resp2 = requests.get(
            "https://graph.threads.net/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": app_secret,
                "access_token": short_token,
            },
            timeout=15,
        )
        resp2.raise_for_status()
        resp2_data = resp2.json()
        long_token = resp2_data.get("access_token")
        if not long_token:
            raise ValueError(f"長期トークンが見つかりません: {resp2.text}")
        expires_in_days = resp2_data.get("expires_in", 5184000) // 86400

        # ユーザー情報取得
        resp3 = requests.get(
            "https://graph.threads.net/v1.0/me",
            params={"fields": "id,username", "access_token": long_token},
            timeout=15,
        )
        resp3.raise_for_status()
        user_data = resp3.json()
        user_id = user_data.get("id", "")
        username = user_data.get("username", "")

        account, created = _sync_threads_account_token(
            user_id, long_token, username,
            force_new=is_new_account, label=new_account_label,
        )
        if created:
            if hasattr(app, "reschedule_post_jobs"):
                app.reschedule_post_jobs()
            flash(
                f"新しいアカウント「{account.account_label}」を追加しました！ @{username}（ID: {user_id}）"
                f"有効期限：{expires_in_days}日後",
                "success",
            )
        else:
            flash(
                f"「{account.account_label}」のトークンを更新しました！ @{username}（ID: {user_id}）"
                f"有効期限：{expires_in_days}日後",
                "success",
            )
    except Exception as e:
        logger.exception("Threads OAuth 処理エラー")
        flash(f"認証処理中にエラーが発生しました: {e}", "danger")

    return redirect(url_for("settings"))


# ── コメント管理 ───────────────────────────────────────────────────────────


@app.route("/comments")
def comments_page():
    filter_tab = request.args.get("tab", "unread")
    if filter_tab == "unread":
        comments_list = Comment.query.filter_by(is_read=0).order_by(Comment.created_at.desc()).all()
    elif filter_tab == "replied":
        comments_list = Comment.query.filter_by(is_replied=1).order_by(Comment.created_at.desc()).all()
    else:
        comments_list = Comment.query.order_by(Comment.created_at.desc()).all()

    # 各コメントに対応する投稿タイトルを取得
    post_ids = {c.post_id for c in comments_list if c.post_id}
    post_titles = {}
    for pid in post_ids:
        article = Article.query.filter_by(threads_post_id=pid).first()
        if article:
            post_titles[pid] = article.title[:20]

    return render_template(
        "comments.html",
        comments=comments_list,
        filter_tab=filter_tab,
        post_titles=post_titles,
        auto_like_enabled=(Setting.get("auto_like_comments", "false") == "true"),
    )


@app.route("/api/comments", methods=["GET", "POST"])
def api_fetch_comments():
    from comments import fetch_comments as _fetch
    result = _fetch(app)
    if "error" in result:
        flash(result["error"], "danger")
    else:
        flash(f"コメント取得完了: {result['fetched']}件取得 / {result['new']}件新規", "success")
    return redirect(url_for("comments_page"))


@app.route("/api/comments/<reply_id>/like", methods=["POST"])
def api_like_comment(reply_id):
    comment = Comment.query.filter_by(id=reply_id).first()
    if not comment:
        return jsonify({"error": "コメントが見つかりません"}), 404
    if comment.is_liked:
        return jsonify({"ok": True, "already_liked": True})
    from comments import like_comment
    return jsonify(like_comment(app, reply_id))


@app.route("/api/comments/<reply_id>/reply", methods=["POST"])
def api_reply_comment(reply_id):
    from comments import post_reply
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "返信文を入力してください"})
    return jsonify(post_reply(app, reply_id, text))


@app.route("/api/comments/<reply_id>/generate-reply", methods=["POST"])
def api_generate_reply(reply_id):
    from comments import generate_ai_reply
    return jsonify(generate_ai_reply(app, reply_id))


@app.route("/api/comments/<reply_id>/delete", methods=["POST"])
def api_delete_comment(reply_id):
    comment = Comment.query.filter_by(id=reply_id).first()
    if not comment:
        return jsonify({"error": "コメントが見つかりません"}), 404
    db.session.delete(comment)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/comments/delete-all", methods=["POST"])
def api_delete_all_comments():
    count = Comment.query.count()
    Comment.query.delete()
    db.session.commit()
    return jsonify({"ok": True, "deleted": count})


@app.route("/api/comments/<reply_id>/mark-read", methods=["POST"])
def api_mark_comment_read(reply_id):
    comment = Comment.query.filter_by(id=reply_id).first_or_404()
    comment.is_read = 1
    db.session.commit()
    return jsonify({"ok": True})


# ── プライバシーポリシー ────────────────────────────────────────────────────


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


# ── 手動操作 API ───────────────────────────────────────────────────────────


@app.route("/collect", methods=["POST"])
def collect():
    from rss_collector import collect_articles

    account_id = _explicit_account_id(request.form)
    new = collect_articles(app, account_id=account_id)
    flash(f"RSS 収集完了: {new} 件の新記事を取得しました（承認待ち画面で要約を生成してください）", "success")
    return redirect(url_for("index", account_id=account_id) if account_id else url_for("index"))


@app.route("/collect-youtube", methods=["POST"])
def collect_youtube():
    from youtube_collector import collect_youtube_videos

    new = collect_youtube_videos(app)
    flash(f"YouTube 収集完了: {new} 件の新しい動画を取得しました（承認待ち画面で要約を生成してください）", "success")
    return redirect(url_for("index"))


def _ffmpeg_trim_clip(source_path: str, dest_path: str, start: float, end: float | None, timeout: int = 600) -> None:
    """ffmpegで source_path の [start, end) 区間を dest_path に切り出す。end が None なら start 以降を最後まで。
    失敗時は例外(メッセージにffmpegのstderr末尾500文字を含む)を送出する。"""
    import subprocess as _sp

    ffmpeg_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin", "ffmpeg.exe")

    # -ss を -i より前に置くキーフレームシークで高速化する。
    # この場合 -to は使えない（シーク後の相対時刻ではなく元の絶対時刻のままになるため）ので、
    # 代わりに相対時間指定の -t (end - start) を使う。
    cmd = [ffmpeg_exe, "-y", "-ss", str(start), "-i", source_path]
    if end is not None:
        cmd += ["-t", str(end - start)]
    cmd += [
        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "128k",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        dest_path,
    ]

    result = _sp.run(cmd, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(err)


def _run_trim_job(app, job_id):
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

            try:
                _ffmpeg_trim_clip(video_path, clip_path, job.start, job.end)
            except Exception as exc:
                err = str(exc)[:500]
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
                account_id=article.account_id,
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
            db.session.rollback()
            job.status = "failed"
            job.error_message = str(exc)
            db.session.commit()


JOB_STALE_MINUTES = 15
CHAPTER_JOB_STALE_MINUTES = 60  # チャプター分割は複数クリップを順次処理するため長めに設定
_CHAPTER_TIMEOUT_MESSAGE = (
    f"処理が{CHAPTER_JOB_STALE_MINUTES}分以上完了しなかったためタイムアウトしました。"
    "バックグラウンド処理が中断された可能性があります。もう一度お試しください。"
)


def _is_job_stale(job, stale_minutes: int = JOB_STALE_MINUTES) -> bool:
    """processingのまま長時間放置されているかを判定する(VideoTrimJob/ChapterJob共通)。
    開発中のコード編集によるapp.py自動再起動(run.pyのwatcherがtaskkillでプロセスツリーを
    強制終了する)で、実行中のバックグラウンドスレッド(_run_trim_job/_run_chapter_job)が
    道連れで終了させられると、DBのstatusが更新されないまま'processing'に固まってしまう
    ことがある。ffmpeg自体にはtimeout(デフォルト600秒)があるが、プロセスごと強制終了
    された場合はそれも実行されないため、別途ここで検知する。"""
    if job.status != "processing":
        return False
    reference = job.updated_at or job.created_at
    if not reference:
        return False
    return datetime.utcnow() - reference > timedelta(minutes=stale_minutes)


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

    existing_job = VideoTrimJob.query.filter_by(
        source_article_id=article_id, status="processing"
    ).first()
    if existing_job:
        if _is_job_stale(existing_job):
            existing_job.status = "failed"
            existing_job.error_message = (
                f"処理が{JOB_STALE_MINUTES}分以上完了しなかったためタイムアウトしました。"
                "バックグラウンド処理が中断された可能性があります。"
            )
            db.session.commit()
            logger.warning("動画トリミングタイムアウト(新規リクエスト時に検知): job_id=%d", existing_job.id)
        else:
            return jsonify({"ok": False, "error": "この動画は既にトリミング処理中です", "job_id": existing_job.id}), 409

    job = VideoTrimJob(source_article_id=article_id, start=start, end=end, status="processing")
    db.session.add(job)
    db.session.commit()

    threading.Thread(target=_run_trim_job, args=(app, job.id), daemon=True).start()

    return jsonify({"ok": True, "job_id": job.id})


@app.route("/api/videos/trim-jobs/<int:job_id>")
def trim_job_status(job_id):
    job = VideoTrimJob.query.get_or_404(job_id)
    if _is_job_stale(job):
        job.status = "failed"
        job.error_message = (
            f"処理が{JOB_STALE_MINUTES}分以上完了しなかったためタイムアウトしました。"
            "バックグラウンド処理が中断された可能性があります。もう一度お試しください。"
        )
        db.session.commit()
        logger.warning("動画トリミングタイムアウト(ポーリング時に検知): job_id=%d", job_id)
    return jsonify({
        "status": job.status,
        "error": job.error_message,
        "new_article_id": job.result_article_id,
    })


@app.route("/api/articles/<int:article_id>/requeue", methods=["POST"])
def requeue_article(article_id):
    import shutil, tempfile

    logger.info("[requeue] リクエスト受信: article_id=%d", article_id)
    article = Article.query.get_or_404(article_id)
    logger.info("[requeue] 取得: id=%d status=%r content_type=%r scheduled_at=%s title=%.50s",
                article_id, article.status, article.content_type, article.scheduled_at, article.title)

    if (article.content_type or "article") != "video":
        prev_status = article.status
        article.status = "queued"
        article.scheduled_at = None
        article.error_message = None
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error("[requeue] DBコミット失敗: id=%d %s", article_id, exc)
            return jsonify({"ok": False, "error": f"DB更新失敗: {exc}"}), 500
        logger.info("[requeue] 完了(記事): id=%d %s→queued scheduled_at=None", article_id, prev_status)
        return jsonify({"ok": True})

    logger.info("[requeue] 動画処理開始: id=%d video_file_path=%r", article_id, article.video_file_path)

    # 動画ファイルが存在しない場合は再ダウンロード
    needs_download = False
    if article.video_file_path:
        vpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", article.video_file_path)
        if not os.path.exists(vpath):
            logger.info("[requeue] 動画ファイル不在、再ダウンロード必要: %s", vpath)
            needs_download = True
        else:
            logger.info("[requeue] 動画ファイル確認OK: %s", vpath)
    else:
        logger.info("[requeue] video_file_path未設定、再ダウンロード必要")
        needs_download = True

    if needs_download:
        yt_url = article.url
        from urllib.parse import urlparse, parse_qs as _parse_qs
        parsed = urlparse(yt_url)
        vid_id = _parse_qs(parsed.query).get("v", [None])[0]
        if not vid_id:
            return jsonify({"ok": False, "error": "動画IDを取得できませんでした"}), 400

        try:
            import yt_dlp
        except ImportError:
            return jsonify({"ok": False, "error": "yt-dlpがインストールされていません"}), 500

        tmp_dir = os.path.join(tempfile.gettempdir(), "kpopwave_videos")
        os.makedirs(tmp_dir, exist_ok=True)
        ffmpeg_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin")
        dl_opts = {
            "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]",
            "ffmpeg_location": ffmpeg_bin,
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(tmp_dir, f"{vid_id}.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
        }
        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([yt_url])
        except Exception as exc:
            return jsonify({"ok": False, "error": f"ダウンロードエラー: {_classify_ytdlp_error(exc)}"}), 500

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

        article.video_file_path = f"videos/{dest_filename}"
        logger.info("[requeue] 動画再ダウンロード完了: id=%d %s", article_id, dest_filename)

    prev_status = article.status
    article.status = "queued"
    article.scheduled_at = None
    article.error_message = None
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("[requeue] DBコミット失敗(動画): id=%d %s", article_id, exc)
        return jsonify({"ok": False, "error": f"DB更新失敗: {exc}"}), 500
    logger.info("[requeue] 完了(動画): id=%d %s→queued", article_id, prev_status)
    return jsonify({"ok": True})


@app.route("/api/videos/fill-view-counts", methods=["POST"])
def fill_video_view_counts():
    api_key = Setting.get("youtube_api_key", "") or os.getenv("YOUTUBE_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "YouTube APIキーが設定されていません"}), 400

    SHORT_KEYWORDS = ["shorts", "#shorts"]
    candidates = (
        Article.query
        .filter(
            Article.status == "pending",
            Article.content_type == "video",
            db.or_(Article.view_count.is_(None), Article.view_count == 0),
        )
        .all()
    )

    targets = []
    for a in candidates:
        if any(kw in (a.title or "").lower() for kw in SHORT_KEYWORDS):
            continue
        vid_id = _extract_youtube_id(a.url)
        if not vid_id:
            continue
        targets.append((a, vid_id))

    if not targets:
        return jsonify({"ok": True, "updated": 0, "skipped": 0, "message": "対象動画がありません"})

    vid_ids = [vid_id for _, vid_id in targets]
    stats = {}
    for i in range(0, len(vid_ids), 50):
        batch = vid_ids[i : i + 50]
        try:
            resp = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "statistics,contentDetails", "id": ",".join(batch), "key": api_key},
                timeout=15,
            )
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                vid = item["id"]
                try:
                    vc = int(item.get("statistics", {}).get("viewCount", 0))
                except (ValueError, TypeError):
                    vc = 0
                dur = _parse_iso_duration(item.get("contentDetails", {}).get("duration", ""))
                stats[vid] = {"view_count": vc, "duration": dur}
        except Exception as exc:
            return jsonify({"ok": False, "error": f"YouTube APIエラー: {str(exc)[:120]}"}), 500

    updated = 0
    skipped = 0
    for article, vid_id in targets:
        info = stats.get(vid_id)
        if not info:
            skipped += 1
            continue
        if 0 < info["duration"] <= 60:
            skipped += 1
            continue
        article.view_count = info["view_count"]
        updated += 1

    db.session.commit()
    logger.info("再生数補完: %d件更新 %d件スキップ", updated, skipped)
    return jsonify({"ok": True, "updated": updated, "skipped": skipped})


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


def _classify_ytdlp_error(exc: Exception) -> str:
    """yt-dlp/ffmpegの例外を、原因が推測しやすい日本語メッセージに分類する。
    どのパターンにも一致しない場合は元のエラーメッセージの要点(先頭数行)を返す。"""
    text = str(exc)
    lower = text.lower()

    if "403" in text or "forbidden" in lower:
        return "配信元の認証エラーです(403)。この動画は現在ダウンロードできない可能性があります。"

    if any(k in lower for k in ("no video could be found", "no media found", "does not contain a video")):
        return "この投稿には動画が含まれていないようです。"

    if any(k in lower for k in ("age-restricted", "age restricted", "nsfw", "sensitive media", "adult content")):
        return "年齢制限・センシティブ設定のある投稿のため取得できませんでした。"

    if any(k in lower for k in ("private account", "account is private", "not authorized to view",
                                "login required", "requested tweet is not available",
                                "sign in to confirm", "this account is protected")):
        return "非公開アカウント、またはログインが必要な投稿のため取得できません。"

    if any(k in lower for k in ("no status found", "tweet is not available", "post unavailable",
                                "page does not exist", "tweet was deleted", "media has been deleted")):
        return "投稿が見つかりません。削除済みか、URLが間違っている可能性があります。"

    if any(k in lower for k in ("incomplete youtube id", "is not a valid url", "unsupported url", "looks truncated", "invalid url")):
        return "URLが正しくない可能性があります。動画IDが省略・欠落していないか確認してください。"

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    summary = " / ".join(lines[:3])[:300]
    return summary if summary else "不明なエラーが発生しました。"


class _YouTubeDownloadNotFoundError(Exception):
    """ダウンロード自体は成功したが、ローカルに出力ファイルが見つからない場合に送出する。"""


def _download_youtube_range(yt_url: str, vid_id: str, start_time: float | None, end_time: float | None, suffix: str, use_cookie: bool = True, playlist_index: int | None = None) -> tuple[str, str]:
    """指定範囲(start_time が None なら動画全体)をダウンロードし、(ローカルの一時ファイルパス, 拡張子) を返す。
    ダウンロード自体の失敗は例外をそのまま送出する。ダウンロードは成功したがファイルが見つからない場合は
    _YouTubeDownloadNotFoundError を送出する。use_cookie=False の場合、Cookie認証・js_runtimes・
    remote_componentsを一切使わない素のダウンロードを行う(範囲指定なしの通常ダウンロードで十分な場合用)。
    playlist_index 指定時は、複数動画を含む投稿(X の複数動画ツイート等)からその番号(1始まり)の動画だけを取得する。"""
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
    }
    if use_cookie:
        dl_opts.update(_YT_DLP_JS_OPTS)
        if os.path.exists(_YOUTUBE_COOKIE_FILE):
            dl_opts["cookiefile"] = _YOUTUBE_COOKIE_FILE
    if start_time is not None:
        dl_opts["download_ranges"] = download_range_func(
            [], [(start_time, end_time if end_time is not None else float("inf"))]
        )
    if playlist_index is not None:
        dl_opts["playlist_items"] = str(playlist_index)

    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([yt_url])

    found = _find_downloaded_file(tmp_dir, vid_id + suffix)
    if not found:
        raise _YouTubeDownloadNotFoundError("ダウンロードファイルが見つかりません")
    return found


@app.route("/api/videos/music-bank/search", methods=["POST"])
def search_music_bank_videos():
    from youtube_collector import search_program_videos, DEFAULT_PROGRAM_KEY, DEFAULT_TARGET_GROUP

    data = request.get_json(silent=True) or {}
    program_key = (data.get("program") or DEFAULT_PROGRAM_KEY).strip()
    target_group = (data.get("target_group") or DEFAULT_TARGET_GROUP).strip()
    page_token = (data.get("page_token") or "").strip() or None
    order = (data.get("order") or "date").strip()
    # 自由入力のチャンネル(@ハンドル / URL / チャンネルID)。指定時はプリセットより優先する。
    channel_input = (data.get("channel") or "").strip()
    if not target_group:
        return jsonify({"ok": False, "error": "グループ名を入力してください"}), 400

    result = search_program_videos(app, program_key, target_group, page_token=page_token,
                                   order=order, channel_input=channel_input)
    if not result["ok"]:
        return jsonify({"ok": False, "error": result["error"]}), 400
    return jsonify({
        "ok": True,
        "videos": result["videos"],
        "next_page_token": result["next_page_token"],
        "order": result["order"],
    })


@app.route("/api/videos/fancam/search", methods=["POST"])
def search_fancam_videos_endpoint():
    from youtube_collector import search_fancam_videos, DEFAULT_TARGET_GROUP

    data = request.get_json(silent=True) or {}
    target_group = (data.get("target_group") or DEFAULT_TARGET_GROUP).strip()
    page_token = (data.get("page_token") or "").strip() or None
    order = (data.get("order") or "date").strip()
    query_suffix = (data.get("query_suffix") or "").strip() or None
    if not target_group:
        return jsonify({"ok": False, "error": "グループ名を入力してください"}), 400

    result = search_fancam_videos(app, target_group, page_token=page_token, order=order,
                                   query_suffix=query_suffix)
    if not result["ok"]:
        return jsonify({"ok": False, "error": result["error"]}), 400
    return jsonify({
        "ok": True,
        "videos": result["videos"],
        "next_page_token": result["next_page_token"],
        "order": result["order"],
        "query_suffix": result["query_suffix"],
    })


@app.route("/api/videos/add-manual", methods=["POST"])
def add_video_manual():
    import shutil

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

    cookie_used = os.path.exists(_YOUTUBE_COOKIE_FILE)
    info_opts = {"quiet": True, "no_warnings": True, **_YT_DLP_JS_OPTS}
    if cookie_used:
        info_opts["cookiefile"] = _YOUTUBE_COOKIE_FILE
    try:
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            full = ydl.extract_info(yt_url, download=False)
        if not full:
            return jsonify({"ok": False, "error": "動画情報を取得できませんでした"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"動画情報取得エラー: {_classify_ytdlp_error(exc)}"}), 500

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
        error_msg = _classify_ytdlp_error(exc)
        return jsonify({"ok": False, "error": f"ダウンロードエラー: {error_msg}"}), 500

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


_SOCIAL_X_HINTS = ("x.com/", "twitter.com/", "mobile.twitter.com/", "mobile.x.com/")
_SOCIAL_THREADS_HINTS = ("threads.net/", "threads.com/")


@app.route("/api/videos/add-social", methods=["POST"])
def add_video_social():
    """X（Twitter）の投稿URLを貼り付けて動画をフルダウンロードし、承認待ちキューに追加する。
    Threads は yt-dlp に extractor が無く未対応のため、URLは受け付けるが未対応メッセージを返す。
    複数動画を含むツイートは include_all=True なら全動画、既定では先頭のみ取り込む。"""
    import shutil

    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    include_all = bool(data.get("include_all"))

    if not url:
        return jsonify({"ok": False, "error": "URLを入力してください"}), 400

    lower = url.lower()
    if any(h in lower for h in _SOCIAL_THREADS_HINTS):
        return jsonify({"ok": False, "error": "Threadsは現在 yt-dlp が未対応のため動画を取得できません。X（x.com / twitter.com）のURLを使ってください。"}), 400
    if not any(h in lower for h in _SOCIAL_X_HINTS):
        return jsonify({"ok": False, "error": "X（x.com / twitter.com）の投稿URLを入力してください"}), 400

    if Article.query.filter(
        Article.url == url,
        Article.status.in_(["pending", "queued"]),
    ).first():
        return jsonify({"ok": False, "error": "この投稿はすでに承認待ち・キュー中です"}), 400

    try:
        import yt_dlp
    except ImportError:
        return jsonify({"ok": False, "error": "yt-dlpがインストールされていません"}), 500

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            full = ydl.extract_info(url, download=False)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"動画情報取得エラー: {_classify_ytdlp_error(exc)}"}), 500
    if not full:
        return jsonify({"ok": False, "error": "動画情報を取得できませんでした"}), 400

    raw_entries = full.get("entries")
    if raw_entries is not None:
        entries = [e for e in raw_entries if e]
        if not entries:
            return jsonify({"ok": False, "error": "この投稿に動画が見つかりませんでした"}), 400
        targets = [(i + 1, e) for i, e in enumerate(entries)]
        if not include_all:
            targets = targets[:1]
    else:
        targets = [(None, full)]

    multi = len(targets) > 1
    account_id = _explicit_account_id(data)
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "videos")
    os.makedirs(static_dir, exist_ok=True)

    parent_uploader = full.get("uploader") or full.get("uploader_id") or full.get("channel") or "X"

    added_titles = []
    last_error = None
    for pl_index, info in targets:
        vid_id = (info.get("id") or "").strip()
        if not vid_id:
            last_error = "動画IDを取得できませんでした"
            if not multi:
                return jsonify({"ok": False, "error": last_error}), 400
            continue

        suffix = f"_{pl_index}" if multi else ""
        article_url = f"{url}#v{pl_index}" if multi else url
        if Article.query.filter_by(url=article_url).first():
            continue

        try:
            found = _download_youtube_range(
                url, vid_id, None, None, suffix,
                use_cookie=False, playlist_index=pl_index,
            )
        except _YouTubeDownloadNotFoundError:
            last_error = "ダウンロードファイルが見つかりません"
            if not multi:
                return jsonify({"ok": False, "error": last_error}), 500
            continue
        except Exception as exc:
            last_error = f"ダウンロードエラー: {_classify_ytdlp_error(exc)}"
            if not multi:
                return jsonify({"ok": False, "error": last_error}), 500
            continue

        local_path, ext = found
        dest_filename = f"{vid_id}{suffix}.{ext}"
        dest_path = os.path.join(static_dir, dest_filename)
        try:
            shutil.copy2(local_path, dest_path)
            try:
                os.remove(local_path)
            except Exception:
                pass
        except Exception as exc:
            last_error = f"ファイルコピーエラー: {str(exc)[:120]}"
            if not multi:
                return jsonify({"ok": False, "error": last_error}), 500
            continue

        published_at = None
        ts = info.get("timestamp") or full.get("timestamp")
        ud = info.get("upload_date") or full.get("upload_date")
        if ts:
            try:
                published_at = datetime.utcfromtimestamp(int(ts))
            except Exception:
                pass
        if published_at is None and ud and len(str(ud)) == 8:
            try:
                published_at = datetime.strptime(str(ud), "%Y%m%d")
            except Exception:
                pass

        uploader = info.get("uploader") or info.get("uploader_id") or parent_uploader
        title = (info.get("title") or info.get("description") or "X動画")[:500]
        article = Article(
            feed_source=f"X動画: {uploader}",
            title=title,
            url=article_url,
            published_at=published_at,
            raw_content=(info.get("description") or full.get("description") or "")[:5000],
            thumbnail_url=info.get("thumbnail") or full.get("thumbnail") or None,
            status="pending",
            content_type="video",
            video_file_path=f"videos/{dest_filename}",
            view_count=info.get("view_count") or full.get("view_count"),
            account_id=account_id,
        )
        db.session.add(article)
        db.session.commit()
        added_titles.append(title)
        logger.info("X動画手動追加: %s (%s)", title[:60], article_url)

    if not added_titles:
        return jsonify({"ok": False, "error": last_error or "この投稿はすでに追加済みです"}), 400

    if len(added_titles) == 1 and not last_error:
        return jsonify({"ok": True, "title": added_titles[0], "count": 1,
                        "message": f"追加しました！ {added_titles[0]}"})
    msg = f"{len(added_titles)}件の動画を承認待ちに追加しました"
    if last_error:
        msg += f"（一部失敗: {last_error}）"
    return jsonify({"ok": True, "title": added_titles[0], "count": len(added_titles), "message": msg})


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
    """動画全体を用意し(既存ファイル指定があればそれを使い、なければ一度だけダウンロードする。
    ダウンロード時はCookie認証を使わない)、ffmpegでローカルにチャプターごと切り出す。
    個別チャプターごとのネットワークアクセスは発生しないため、Cookie認証・JSチャレンジ解決が不要になる。"""
    with app.app_context():
        job = db.session.get(ChapterJob, job_id)
        if not job:
            return

        clips = ChapterClip.query.filter_by(job_id=job_id).order_by(ChapterClip.chapter_index).all()
        videos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "videos")
        os.makedirs(videos_dir, exist_ok=True)

        use_local_source = bool(job.source_local_path)
        if use_local_source:
            static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
            full_path = os.path.join(static_dir, job.source_local_path)
            if not os.path.exists(full_path):
                error_msg = f"元動画ファイルが見つかりません({job.source_local_path})"
                job.status = "failed"
                job.error_message = error_msg
                for clip in clips:
                    clip.status = "failed"
                    clip.error_message = error_msg
                db.session.commit()
                logger.warning("チャプタージョブ失敗(既存ファイル不在): job_id=%d path=%s", job_id, job.source_local_path)
                return
            ext = os.path.splitext(full_path)[1].lstrip(".") or "mp4"
        else:
            try:
                full_path, ext = _download_youtube_range(
                    job.source_url, job.video_id, None, None, "", use_cookie=False
                )
            except Exception as exc:
                error_msg = _classify_ytdlp_error(exc)
                job.status = "failed"
                job.error_message = f"元動画のダウンロードに失敗しました: {error_msg}"
                for clip in clips:
                    clip.status = "failed"
                    clip.error_message = error_msg
                db.session.commit()
                logger.warning("チャプタージョブ失敗(フルダウンロード): job_id=%d error=%s", job_id, error_msg)
                return

        # ダウンロード完了時点でハートビートを打つ(大きな動画のDLで時間を食っても
        # クリップ処理開始前にstale判定されないようにする)
        job.updated_at = datetime.utcnow()
        db.session.commit()

        any_success = False
        for clip in clips:
            clip.status = "processing"
            db.session.commit()

            start_label = int(clip.start_time)
            end_label = int(clip.end_time) if clip.end_time is not None else ""
            dest_filename = f"{job.video_id}_{start_label}-{end_label}.{ext}"
            dest_path = os.path.join(videos_dir, dest_filename)

            try:
                _ffmpeg_trim_clip(full_path, dest_path, clip.start_time, clip.end_time)
                clip.video_file_path = f"videos/{dest_filename}"
                clip.duration = _probe_duration(dest_path)
                clip.guessed_group_id = _guess_group_id(clip.title)
                clip.status = "done"
                any_success = True
            except Exception as exc:
                clip.status = "failed"
                clip.error_message = str(exc)[:500]
                logger.warning("チャプタークリップ生成失敗: job_id=%d chapter_index=%d error=%s",
                                job_id, clip.chapter_index, clip.error_message)

            # 進捗ハートビート: クリップを1つ処理し終えるたびに job.updated_at を更新する。
            # これがないと _is_job_stale が「作成からの総経過時間」を見てしまい、
            # 正当に時間のかかる大きな動画(多数チャプター)が処理中に誤ってタイムアウト
            # 判定される。updated_at が動いていれば「最後に進捗した時刻からの経過」で
            # 判定できる。
            job.updated_at = datetime.utcnow()
            db.session.commit()

        # ダウンロードした一時ファイルのみ削除する(既存ファイル指定時は元のArticleのファイルなので残す)
        if not use_local_source:
            try:
                os.remove(full_path)
            except OSError:
                pass

        job.status = "done" if any_success else "failed"
        if not any_success:
            job.error_message = "全チャプターの生成に失敗しました"
        db.session.commit()
        logger.info("チャプタージョブ完了: job_id=%d status=%s clips=%d", job_id, job.status, len(clips))


def _create_chapter_job(yt_url: str, vid_id: str, chapters: list, video_title: str,
                         thumbnail_url: str | None, account_id, source_local_path: str | None = None) -> "ChapterJob":
    """ChapterJob・ChapterClip群を作成しコミットした上でバックグラウンドジョブを起動し、作成したjobを返す。
    呼び出し側はchaptersが空でないことを事前に確認しておくこと。"""
    job = ChapterJob(
        source_url=yt_url,
        video_id=vid_id,
        video_title=video_title,
        thumbnail_url=thumbnail_url,
        account_id=account_id,
        status="processing",
        source_local_path=source_local_path,
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
    return job


def _sweep_stale_chapter_jobs() -> int:
    """processingのまま長時間放置されたChapterJobをfailedへ遷移させる。
    起動時の_recover_orphaned_jobsに漏れた(=アプリを再起動せず長時間動かし続けている間に
    バックグラウンド処理だけが停止した)ケースの保険。start系エンドポイントから呼ぶ。"""
    stale_jobs = [
        j for j in ChapterJob.query.filter_by(status="processing").all()
        if _is_job_stale(j, stale_minutes=CHAPTER_JOB_STALE_MINUTES)
    ]
    for job in stale_jobs:
        _fail_chapter_job(job, _CHAPTER_TIMEOUT_MESSAGE)
    if stale_jobs:
        db.session.commit()
        logger.warning("チャプター分割タイムアウト(新規リクエスト時に検知): job_ids=%s",
                       [j.id for j in stale_jobs])
    return len(stale_jobs)


def _is_supported_youtube_url(yt_url: str) -> bool:
    return ("youtube.com/watch" in yt_url or "youtu.be/" in yt_url
            or "youtube.com/shorts/" in yt_url)


@app.route("/api/videos/chapters/detect", methods=["POST"])
def detect_chapters():
    """URLの動画にチャプターがあるかだけを調べて返す。ジョブは作成しない。

    チャプターが見つかった場合、フロント側で「チャプターごとに分割」/「動画全体を
    そのままダウンロード」をユーザーに選ばせる。分割が選ばれたときだけ
    /api/videos/chapters/start が呼ばれる。"""
    data = request.get_json(force=True) or {}
    yt_url = (data.get("url") or "").strip()

    if not yt_url:
        return jsonify({"ok": False, "error": "URLを入力してください"}), 400
    if not _is_supported_youtube_url(yt_url):
        return jsonify({"ok": False, "error": "YouTube動画のURLを入力してください"}), 400

    try:
        import yt_dlp
    except ImportError:
        return jsonify({"ok": False, "error": "yt-dlpがインストールされていません"}), 500

    info_opts = {"quiet": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            full = ydl.extract_info(yt_url, download=False)
        if not full:
            return jsonify({"ok": False, "error": "動画情報を取得できませんでした"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"動画情報取得エラー: {_classify_ytdlp_error(exc)}"}), 500

    chapters = full.get("chapters") or []
    return jsonify({
        "ok": True,
        "has_chapters": bool(chapters),
        "chapter_count": len(chapters),
        "video_title": (full.get("title") or "")[:500],
    })


@app.route("/api/videos/chapters/start", methods=["POST"])
def start_chapter_job():
    data = request.get_json(force=True) or {}
    yt_url = (data.get("url") or "").strip()

    if not yt_url:
        return jsonify({"ok": False, "error": "URLを入力してください"}), 400
    if not _is_supported_youtube_url(yt_url):
        return jsonify({"ok": False, "error": "YouTube動画のURLを入力してください"}), 400

    try:
        import yt_dlp
    except ImportError:
        return jsonify({"ok": False, "error": "yt-dlpがインストールされていません"}), 500

    # チャプター検出・後続のフルダウンロードはCookie認証を使わない(通常アクセスで十分)
    info_opts = {"quiet": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            full = ydl.extract_info(yt_url, download=False)
        if not full:
            return jsonify({"ok": False, "error": "動画情報を取得できませんでした"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"動画情報取得エラー: {_classify_ytdlp_error(exc)}"}), 500

    vid_id = full.get("id", "")
    if not vid_id:
        return jsonify({"ok": False, "error": "動画IDを取得できませんでした"}), 400

    chapters = full.get("chapters") or []
    if not chapters:
        return jsonify({"ok": True, "has_chapters": False})

    _sweep_stale_chapter_jobs()
    title = (full.get("title") or "YouTube動画")[:500]
    job = _create_chapter_job(yt_url, vid_id, chapters, title, full.get("thumbnail") or None,
                               _explicit_account_id(data))

    return jsonify({"ok": True, "has_chapters": True, "job_id": job.id})


@app.route("/api/videos/chapters/start-from-article", methods=["POST"])
def start_chapter_job_from_article():
    data = request.get_json(force=True) or {}
    try:
        article_id = int(data.get("article_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "article_idが不正です"}), 400

    article = db.session.get(Article, article_id)
    if not article:
        return jsonify({"ok": False, "error": "記事が見つかりません"}), 404
    if (article.content_type or "article") != "video" or not article.video_file_path:
        return jsonify({"ok": False, "error": "この記事には動画ファイルがありません"}), 400
    if "#t=" in (article.url or ""):
        return jsonify({"ok": False, "error": "この動画は範囲指定でダウンロードされた部分クリップのため、チャプター分割には使用できません"}), 400

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    local_path = os.path.join(static_dir, article.video_file_path)
    if not os.path.exists(local_path):
        return jsonify({"ok": False, "error": f"動画ファイルが見つかりません({article.video_file_path})。ファイルが削除されているか移動されている可能性があります。"}), 400
    if _probe_duration(local_path) is None:
        return jsonify({"ok": False, "error": "動画ファイルが壊れているか、読み込めません。"}), 400

    yt_url = article.url

    try:
        import yt_dlp
    except ImportError:
        return jsonify({"ok": False, "error": "yt-dlpがインストールされていません"}), 500

    info_opts = {"quiet": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            full = ydl.extract_info(yt_url, download=False)
        if not full:
            return jsonify({"ok": False, "error": "動画情報を取得できませんでした"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"動画情報取得エラー: {_classify_ytdlp_error(exc)}"}), 500

    vid_id = full.get("id", "")
    if not vid_id:
        return jsonify({"ok": False, "error": "動画IDを取得できませんでした"}), 400

    chapters = full.get("chapters") or []
    if not chapters:
        return jsonify({"ok": True, "has_chapters": False})

    _sweep_stale_chapter_jobs()
    title = article.title or (full.get("title") or "YouTube動画")[:500]
    job = _create_chapter_job(yt_url, vid_id, chapters, title,
                               article.thumbnail_url or full.get("thumbnail") or None,
                               article.account_id, source_local_path=article.video_file_path)

    return jsonify({"ok": True, "has_chapters": True, "job_id": job.id})


@app.route("/api/videos/chapters/<int:job_id>/status")
def chapter_job_status(job_id):
    job = ChapterJob.query.get_or_404(job_id)
    if _is_job_stale(job, stale_minutes=CHAPTER_JOB_STALE_MINUTES):
        _fail_chapter_job(job, _CHAPTER_TIMEOUT_MESSAGE)
        db.session.commit()
        logger.warning("チャプター分割タイムアウト(ポーリング時に検知): job_id=%d", job_id)
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
    paths_to_delete = []
    try:
        for clip in clips:
            if clip.id in selected_ids and clip.status == "done" and clip.video_file_path:
                start_label = int(clip.start_time)
                end_label = int(clip.end_time) if clip.end_time is not None else ""
                article = Article(
                    feed_source=f"YouTube動画: {job.video_title or job.video_id}",
                    title=clip.title[:500],
                    # 末尾にランダムな8桁を付けて常に一意にする(同じ動画・同じ範囲を
                    # 再分割・再確認しても Article.url の UNIQUE 制約に衝突しない。
                    # clip.id は削除後に再利用されるため一意性の保証にならない)。
                    # '#t=' マーカーは維持するので start_chapter_job_from_article 側の
                    # 「部分クリップは再分割不可」判定は従来通り効く。
                    url=f"{job.source_url}#t={start_label}-{end_label}-{uuid.uuid4().hex[:8]}",
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
                paths_to_delete.append(os.path.join(static_dir, clip.video_file_path))

        db.session.flush()  # INSERT をここで確定させ、衝突は下の except で捕捉する
        ChapterClip.query.filter_by(job_id=job_id).delete(synchronize_session=False)
        db.session.delete(job)
        db.session.commit()
    except IntegrityError:
        # 万一 URL が衝突した場合でも 500 にせず、ジョブ・クリップ・中間ファイルは
        # 一切消さずに残して再試行できるようにする。
        db.session.rollback()
        logger.warning("チャプター確認でURL衝突: job_id=%d", job_id)
        flash("このクリップは既に承認待ちに追加済みのようです。追加済みのものを確認してください。", "warning")
        return redirect(url_for("chapter_job_view", job_id=job_id))

    # コミット成功後にのみ、選択されなかったクリップの中間ファイルを削除する
    # (コミット前に消すと、コミット失敗時にファイルだけ失われる)
    for full_path in paths_to_delete:
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
            except OSError:
                pass

    flash(f"{added} 件のクリップを承認待ちに追加しました", "success")
    return redirect(url_for("pending"))


@app.route("/collect-videos", methods=["POST"])
def collect_videos():
    from video_collector import collect_youtube_videos as collect_yt_dlp_videos

    new = collect_yt_dlp_videos(app)
    flash(f"動画収集完了: {new} 件の動画をダウンロードしました（承認待ち画面で確認してください）", "success")
    return redirect(url_for("index"))


@app.route("/learning")
def learning():
    from database import BuzzPost
    posts = BuzzPost.query.order_by(BuzzPost.created_at.desc()).all()
    total = len(posts)
    analyzed = sum(1 for p in posts if p.analysis)
    return render_template("learning.html", posts=posts, total=total, analyzed=analyzed)


@app.route("/learning/add", methods=["POST"])
def learning_add():
    from database import BuzzPost
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "投稿本文は必須です"})
    post = BuzzPost(
        platform=(data.get("platform") or "その他").strip(),
        url=(data.get("url") or "").strip() or None,
        content=content,
        likes=int(data.get("likes") or 0),
        comments=int(data.get("comments") or 0),
        shares=int(data.get("shares") or 0),
        memo=(data.get("memo") or "").strip() or None,
    )
    db.session.add(post)
    db.session.commit()
    logger.info("BuzzPost登録: id=%d platform=%s", post.id, post.platform)
    return jsonify({"ok": True, "id": post.id})


@app.route("/learning/<int:id>/analyze", methods=["POST"])
def learning_analyze(id):
    import json as _json
    import anthropic as _anthropic
    from database import BuzzPost

    post = BuzzPost.query.get_or_404(id)
    api_key = Setting.get("anthropic_api_key", "") or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "Anthropic APIキーが未設定です"})

    prompt = (
        "以下のSNS投稿はバズりました。KPOPアカウントの投稿文を改善するために、"
        "以下の観点で分析してJSONのみで返してください（前置き・説明文不要）：\n"
        "- writing_style: 文章スタイルの特徴（1〜2文）\n"
        "- emotion: 感情的な切り口（共感・驚き・笑いなど）\n"
        "- opening: 書き出しのパターン（1文）\n"
        "- effective_elements: 効果的な要素リスト（配列）\n"
        "- tips: 投稿文生成時に活かせるアドバイス（日本語・1〜3文）\n\n"
        f"投稿内容：\n{post.content}"
    )
    try:
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # JSONブロック抽出
        import re as _re
        m = _re.search(r"\{[\s\S]+\}", raw)
        json_str = m.group(0) if m else raw
        parsed = _json.loads(json_str)
        post.analysis = _json.dumps(parsed, ensure_ascii=False)
        db.session.commit()
        logger.info("BuzzPost分析完了: id=%d", id)
        return jsonify({"ok": True, "analysis": parsed})
    except Exception as exc:
        logger.error("BuzzPost分析エラー id=%d: %s", id, exc)
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/learning/<int:id>", methods=["DELETE"])
def learning_delete(id):
    from database import BuzzPost
    post = BuzzPost.query.get_or_404(id)
    db.session.delete(post)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/follow-candidates")
def follow_candidates():
    from follow_candidates import get_page_data
    filter_status   = request.args.get("status", "")
    filter_priority = request.args.get("priority", "")
    data = get_page_data(app, filter_status=filter_status, filter_priority=filter_priority)
    return render_template(
        "follow_candidates.html",
        filter_status=filter_status,
        filter_priority=filter_priority,
        **data,
    )


@app.route("/follow-candidates/refresh", methods=["POST"])
def refresh_follow_candidates():
    from follow_candidates import refresh_candidates
    result = refresh_candidates(app)
    parts = []
    if result["purged"]:
        parts.append(f"不要アカウント削除 {result['purged']} 件")
    if result["new_reddit"]:
        parts.append(f"Reddit発見 +{result['new_reddit']} 件")
    if result["scraped"]:
        parts.append(f"フォロワー数取得 {result['scraped']} 件")
    if result.get("removed_non_kpop"):
        parts.append(f"非K-POP除外 {result['removed_non_kpop']} 件")
    flash("更新完了: " + (" / ".join(parts) or "変更なし"), "success")
    return redirect(url_for("follow_candidates"))


@app.route("/follow-candidates/add", methods=["POST"])
def add_follow_candidate():
    from follow_candidates import add_candidate, set_follower_count
    username = (request.form.get("username") or "").strip().lstrip("@")
    display_name = (request.form.get("display_name") or "").strip()
    fc_str = (request.form.get("followers_count") or "").strip()
    if not username:
        flash("ユーザー名を入力してください", "warning")
        return redirect(url_for("follow_candidates"))
    added = add_candidate(app, username, display_name)
    if added and fc_str.isdigit():
        from database import FollowCandidate
        with app.app_context():
            fc = FollowCandidate.query.filter_by(username=username.lower()).first()
            if fc:
                set_follower_count(app, fc.id, int(fc_str))
    flash(f"@{username} を追加しました" if added else f"@{username} は既に登録されています", "success" if added else "secondary")
    return redirect(url_for("follow_candidates"))


@app.route("/follow-candidates/<int:id>/delete", methods=["POST"])
def delete_follow_candidate(id):
    from follow_candidates import delete_candidate
    delete_candidate(app, id)
    flash("候補を削除しました", "secondary")
    return redirect(url_for("follow_candidates"))


@app.route("/follow-candidates/<int:id>/followers", methods=["POST"])
def update_follow_candidate_followers(id):
    from follow_candidates import set_follower_count
    fc_str = (request.form.get("followers_count") or "").strip()
    count = int(fc_str) if fc_str.isdigit() else None
    set_follower_count(app, id, count)
    return redirect(url_for("follow_candidates"))


@app.route("/follow-candidates/fetch-engagers", methods=["POST"])
def fetch_engagers():
    from follow_candidates import fetch_engagers_from_threads
    result = fetch_engagers_from_threads(app)
    if "error" in result:
        flash(f"エラー: {result['error']}", "danger")
    else:
        flash(
            f"エンゲージメント取得完了: {result['found']}名発見 / {result['added']}名追加",
            "success",
        )
    return redirect(url_for("follow_candidates"))


@app.route("/follow-candidates/scan-kpop", methods=["POST"])
def scan_kpop_accounts():
    from follow_candidates import fetch_kpop_account_repliers, DEFAULT_KPOP_ACCOUNTS
    accounts_raw = (request.form.get("accounts") or "").strip()
    if accounts_raw:
        Setting.set("kpop_seed_accounts", accounts_raw)
        accounts = [a.strip().lstrip("@").lower() for a in accounts_raw.split(",") if a.strip()]
    else:
        accounts = None
    result = fetch_kpop_account_repliers(app, accounts=accounts)
    log = result["scan_log"]
    ok_parts = [f"@{r['account']}({r.get('repliers',0)}名)" for r in log if r.get("ok")]
    ng_parts = [f"@{r['account']}" for r in log if not r.get("ok")]
    msg = f"スキャン完了: {result['added']}名追加 / {result['found']}名発見"
    if ok_parts:
        msg += " — " + " ".join(ok_parts[:6])
    if ng_parts:
        msg += f" ※取得失敗: {', '.join(ng_parts)}"
    flash(msg, "success" if result["found"] > 0 else "secondary")
    return redirect(url_for("follow_candidates"))


@app.route("/follow-candidates/<int:id>/status", methods=["POST"])
def update_follow_candidate_status(id):
    from database import FollowCandidate
    fc = FollowCandidate.query.get_or_404(id)
    fc.follow_status = request.form.get("follow_status") or None
    fc.priority      = request.form.get("priority") or None
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/debug/threads_search")
def debug_threads_search():
    """GET /threads/search デバッグエンドポイント。"""
    import json as _json
    from flask import Response

    account = get_active_account(app)
    token = account["threads_access_token"] if account else Setting.get("threads_access_token", "")
    if not token:
        return Response(
            _json.dumps({"error": "threads_access_token が未設定です"}, ensure_ascii=False),
            content_type="application/json; charset=utf-8",
        )

    try:
        r = requests.get(
            "https://graph.threads.net/v1.0/threads/search",
            params={"q": "KPOP", "access_token": token},
            timeout=15,
        )
        payload = {
            "status": r.status_code,
            "url": r.url,
            "body": r.json(),
        }
    except Exception as e:
        payload = {"error": str(e)}

    return Response(
        _json.dumps(payload, ensure_ascii=False, indent=2),
        content_type="application/json; charset=utf-8",
    )


@app.route("/api/stats")
def api_stats():
    return jsonify({
        s: Article.query.filter_by(status=s).count()
        for s in ("pending", "queued", "posted", "rejected", "failed")
    })


@app.route("/api/debug/threads")
def debug_threads_api():
    """Threads API 診断エンドポイント（開発用）。
    ブラウザで開くと各エンドポイントの生レスポンスを確認できます。"""
    import time as _time
    _BASE = "https://graph.threads.net/v1.0"

    account = get_active_account(app)
    token   = account["threads_access_token"] if account else Setting.get("threads_access_token", "")
    user_id = account["threads_user_id"] if account else Setting.get("threads_user_id", "")

    if not token or not user_id:
        return jsonify({"error": "threads_access_token / threads_user_id が未設定です"})

    token_preview = f"{token[:12]}...{token[-4:]}" if len(token) > 20 else "短いトークン"
    now_ts   = int(_time.time())
    since_ts = now_ts - 86400 * 3

    checks = [
        ("①  GET /me (基本フィールド: id,username,name)",
         f"{_BASE}/me",
         {"fields": "id,username,name", "access_token": token}),

        ("②  GET /me (followers_count フィールド)",
         f"{_BASE}/me",
         {"fields": "id,username,followers_count", "access_token": token}),

        ("③  GET /me (follower_count — 単数形バリアント)",
         f"{_BASE}/me",
         {"fields": "id,username,follower_count", "access_token": token}),

        ("④  GET /{user_id} (followers_count フィールド)",
         f"{_BASE}/{user_id}",
         {"fields": "id,username,followers_count", "access_token": token}),

        ("⑤  GET /{user_id}/insights (metric=followers_count, period=day)",
         f"{_BASE}/{user_id}/insights",
         {"metric": "followers_count", "period": "day",
          "since": since_ts, "until": now_ts, "access_token": token}),

        ("⑥  GET /{user_id}/insights (metric=views, period=day) ← 動作確認用",
         f"{_BASE}/{user_id}/insights",
         {"metric": "views", "period": "day",
          "since": since_ts, "until": now_ts, "access_token": token}),

        ("⑦  GET /me (フィールド指定なし — 利用可能なデフォルトフィールドを確認)",
         f"{_BASE}/me",
         {"access_token": token}),
    ]

    results = {}
    for label, url, params in checks:
        safe_params = {k: (v if k != "access_token" else token_preview) for k, v in params.items()}
        try:
            r = requests.get(url, params=params, timeout=10)
            results[label] = {
                "url": url,
                "params": safe_params,
                "status": r.status_code,
                "body": r.json(),
            }
        except Exception as e:
            results[label] = {"url": url, "params": safe_params, "error": str(e)}

    import json as _json
    from flask import Response
    payload = {
        "user_id_in_db": user_id,
        "token_preview": token_preview,
        "token_length": len(token),
        "results": results,
    }
    return Response(
        _json.dumps(payload, ensure_ascii=False, indent=2),
        content_type="application/json; charset=utf-8",
    )


@app.route("/api/debug/threads_video")
def debug_threads_video():
    """Threads API 動画投稿コンテナ作成テスト（公開はしない）。"""
    import json as _json
    from flask import Response

    _BASE = "https://graph.threads.net/v1.0"
    _TEST_VIDEO_URL = "https://www.w3schools.com/html/mov_bbb.mp4"

    account = get_active_account(app)
    token   = account["threads_access_token"] if account else Setting.get("threads_access_token", "")
    user_id = account["threads_user_id"] if account else Setting.get("threads_user_id", "")

    if not token or not user_id:
        return Response(
            _json.dumps({"error": "threads_access_token / threads_user_id が未設定です"}, ensure_ascii=False),
            content_type="application/json; charset=utf-8",
        )

    token_preview = f"{token[:12]}...{token[-4:]}" if len(token) > 20 else token

    try:
        res = requests.post(
            f"{_BASE}/{user_id}/threads",
            data={
                "media_type": "VIDEO",
                "video_url": _TEST_VIDEO_URL,
                "text": "テスト",
                "access_token": token,
            },
            timeout=30,
        )
        payload = {
            "step": "コンテナ作成（公開なし）",
            "request": {
                "url": f"{_BASE}/{user_id}/threads",
                "media_type": "VIDEO",
                "video_url": _TEST_VIDEO_URL,
                "text": "テスト",
                "access_token": token_preview,
            },
            "http_status": res.status_code,
            "response": res.json(),
        }
    except Exception as e:
        payload = {"error": str(e)}

    return Response(
        _json.dumps(payload, ensure_ascii=False, indent=2),
        content_type="application/json; charset=utf-8",
    )


# ── 起動 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from scheduler import setup_scheduler

    logger.info(
        "====== ContentWave 起動 (二重投稿防止v2: post_to_threads内アトミックロック) ======"
    )
    # 起動時の孤児ジョブ復旧は本番起動時のみ行う(create_app内だと、ヘルパースクリプトが
    # `from app import app` した際に、稼働中の本番プロセスが実行中の正当なジョブまで
    # failed化してしまうため)。
    with app.app_context():
        _recover_orphaned_jobs()
    setup_scheduler(app)
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000, threaded=True)
