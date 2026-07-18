import json
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from sqlalchemy import or_

from config import Config
from database import Article, BuzzPost, Comment, Setting, ThreadsAccount, get_active_account, db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

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

    return {
        "pending_count": _scope(Article.query.filter_by(status="pending")).count(),
        "queued_count": _scope(Article.query.filter_by(status="queued")).count(),
        "unread_comments_count": Comment.query.filter_by(is_read=0).count(),
        "youtube_min_view_count": Setting.get("youtube_min_view_count", "5000000"),
        "youtube_max_view_count": Setting.get("youtube_max_view_count", "0"),
        "nav_accounts": ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all(),
        "nav_active_account_id": account_id,
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
    return render_template("index.html", stats=stats, recent=recent)


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
        "posted": _scope(Article.query.filter_by(status="posted", content_type="video")).count(),
    }
    for a in all_pending:
        src = a.feed_source or ""
        if (a.content_type or "article") == "video":
            counts["video"] += 1
        elif src.startswith("YouTube:"):
            counts["youtube"] += 1
        else:
            counts["rss"] += 1

    if tab == "posted":
        articles = (_scope(Article.query.filter_by(status="posted", content_type="video"))
                    .order_by(Article.created_at.desc())
                    .all())
        images_map = {}
    elif tab == "video":
        articles = [a for a in all_pending if (a.content_type or "article") == "video"]
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

    return render_template("pending.html", articles=articles, images_map=images_map,
                           active_tab=tab, counts=counts, now_utc=datetime.utcnow())


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
    for a in articles:
        if a.video_file_path:
            _delete_video_files(a.video_file_path, static_dir)
    Article.query.filter(Article.id.in_(int_ids)).delete(synchronize_session=False)
    db.session.commit()
    flash(f"{len(ids)} 件の記事を削除しました", "warning")
    return redirect(url_for("pending", tab=tab))


@app.route("/pending/delete-all", methods=["POST"])
def delete_all_pending():
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    articles = Article.query.filter_by(status="pending").all()
    for a in articles:
        if a.video_file_path:
            _delete_video_files(a.video_file_path, static_dir)
    count = len(articles)
    Article.query.filter_by(status="pending").delete(synchronize_session=False)
    db.session.commit()
    flash(f"承認待ち記事 {count} 件をすべて削除しました", "warning")
    return redirect(url_for("pending"))


@app.route("/articles/<int:id>/approve", methods=["POST"])
def approve_article(id):
    from scheduler import next_post_slot
    from datetime import timedelta

    article = Article.query.get_or_404(id)
    article.status = "queued"

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


@app.route("/articles/<int:id>/delete", methods=["POST"])
def delete_article(id):
    article = Article.query.get_or_404(id)
    db.session.delete(article)
    db.session.commit()
    flash("記事を削除しました", "warning")
    return redirect(request.referrer or url_for("pending"))


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


# ── 設定 ───────────────────────────────────────────────────────────────────


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        for key in ("threads_user_id", "threads_access_token", "anthropic_api_key",
                    "post_times", "collect_interval_hours",
                    "youtube_api_key", "youtube_collect_interval_hours",
                    "youtube_min_view_count", "youtube_max_view_count",
                    "meta_app_id", "meta_app_secret", "app_base_url"):
            Setting.set(key, (request.form.get(key) or "").strip())

        Setting.set("test_mode", "true" if request.form.get("test_mode") else "false")

        feed_names = request.form.getlist("feed_name")
        feed_urls = request.form.getlist("feed_url")
        feeds = [{"name": n.strip(), "url": u.strip()}
                 for n, u in zip(feed_names, feed_urls) if u.strip()]
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

    # トークン有効期限の計算
    token_acquired_at_str = Setting.get("threads_token_acquired_at", "")
    threads_token_expires_in_days = None
    if token_acquired_at_str:
        try:
            acquired_at = datetime.fromisoformat(token_acquired_at_str)
            expires_at = acquired_at + timedelta(days=60)
            threads_token_expires_in_days = max(0, (expires_at - datetime.utcnow()).days)
        except Exception:
            pass

    current = {
        "threads_user_id": Setting.get("threads_user_id"),
        "threads_access_token": Setting.get("threads_access_token"),
        "threads_token_expires_in_days": threads_token_expires_in_days,
        "anthropic_api_key": Setting.get("anthropic_api_key"),
        "post_times": Setting.get("post_times", "09:00,15:00,21:00"),
        "collect_interval_hours": Setting.get("collect_interval_hours", "2"),
        "youtube_api_key": Setting.get("youtube_api_key"),
        "youtube_collect_interval_hours": Setting.get("youtube_collect_interval_hours", "6"),
        "youtube_min_view_count": Setting.get("youtube_min_view_count", "5000000"),
        "youtube_max_view_count": Setting.get("youtube_max_view_count", "0"),
        "test_mode": Setting.get("test_mode", "true") == "true",
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
        if action not in ("post_now", "queue"):
            return jsonify({"success": False, "message": "不正なリクエストです"})

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

    default_account_id = None
    for acc in accounts:
        if acc.account_label == "田中（仮）":
            default_account_id = acc.id
            break
    if default_account_id is None and accounts:
        default_account_id = accounts[0].id

    return render_template(
        "text_post.html",
        accounts=accounts,
        default_account_id=default_account_id,
        max_chars=TEXT_POST_MAX_CHARS,
    )


# ── Threads OAuth 認証 ────────────────────────────────────────────────────


def _sync_threads_account_token(user_id: str, token: str, username: str = None,
                                 force_new: bool = False, label: str = None):
    """OAuth認証成功時にトークンを保存する。

    force_new=False（デフォルト・既存の「トークンを再取得」ボタン用）:
        settings テーブルと「アクティブな最初のアカウント」（＝従来からの唯一アカウント）の
        両方を更新する。マルチアカウント導入前と完全に同じ挙動。
    force_new=True（「新しいアカウントを追加」用）:
        settings テーブルには一切書き込まず、threads_accounts に新規レコードを追加する。
        既存アカウントのトークンには影響しない。
    """
    if force_new:
        account = ThreadsAccount(
            account_label=label or username or f"account_{user_id}",
            threads_user_id=user_id,
            threads_access_token=token,
            token_acquired_at=datetime.utcnow(),
            is_active=True,
        )
        db.session.add(account)
        db.session.commit()
        return account

    Setting.set("threads_access_token", token)
    Setting.set("threads_user_id", user_id)
    Setting.set("threads_token_acquired_at", datetime.utcnow().isoformat())

    account = ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).first()
    if account:
        account.threads_user_id = user_id
        account.threads_access_token = token
        account.token_acquired_at = datetime.utcnow()
    else:
        account = ThreadsAccount(
            account_label=username or "default",
            threads_user_id=user_id,
            threads_access_token=token,
            token_acquired_at=datetime.utcnow(),
            is_active=True,
        )
        db.session.add(account)
    db.session.commit()
    return account


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
    return redirect(request.referrer or url_for("index"))


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

        _sync_threads_account_token(
            user_id, long_token, username,
            force_new=is_new_account, label=new_account_label,
        )
        if is_new_account:
            if hasattr(app, "reschedule_post_jobs"):
                app.reschedule_post_jobs()
            flash(
                f"新しいアカウント「{new_account_label}」を追加しました！ @{username}（ID: {user_id}）"
                f"有効期限：{expires_in_days}日後",
                "success",
            )
        else:
            flash(
                f"トークンを更新しました！ @{username}（ID: {user_id}）"
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

        _sync_threads_account_token(
            user_id, long_token, username,
            force_new=is_new_account, label=new_account_label,
        )
        if is_new_account:
            if hasattr(app, "reschedule_post_jobs"):
                app.reschedule_post_jobs()
            flash(
                f"新しいアカウント「{new_account_label}」を追加しました！ @{username}（ID: {user_id}）"
                f"有効期限：{expires_in_days}日後",
                "success",
            )
        else:
            flash(
                f"トークンを更新しました！ @{username}（ID: {user_id}）"
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

    new = collect_articles(app)
    flash(f"RSS 収集完了: {new} 件の新記事を取得しました（承認待ち画面で要約を生成してください）", "success")
    return redirect(url_for("index"))


@app.route("/collect-youtube", methods=["POST"])
def collect_youtube():
    from youtube_collector import collect_youtube_videos

    new = collect_youtube_videos(app)
    flash(f"YouTube 収集完了: {new} 件の新しい動画を取得しました（承認待ち画面で要約を生成してください）", "success")
    return redirect(url_for("index"))


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
    )
    db.session.add(article)
    db.session.commit()

    logger.info("動画手動追加: %s (%s)", title[:60], yt_url)
    return jsonify({"ok": True, "title": title})


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
    setup_scheduler(app)
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000)
