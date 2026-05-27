import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from config import Config
from database import Article, Comment, Setting, db

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
    {"name": "LE SSERAFIM",  "url": "https://www.youtube.com/@LESSERAFIM"},
    {"name": "ILLIT",        "url": "https://www.youtube.com/@ILLIT_official"},
    {"name": "tripleS",      "url": "https://www.youtube.com/@tripleS"},
    {"name": "KISS OF LIFE", "url": "https://www.youtube.com/@KISSOFLIFE"},
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
    ]
    with db.engine.connect() as conn:
        for col, typedef in article_cols:
            if col not in existing_articles:
                conn.execute(text(f"ALTER TABLE articles ADD COLUMN {col} {typedef}"))
                conn.commit()
                logger.info("DB migration: articles.%s added", col)

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
    return {
        "pending_count": Article.query.filter_by(status="pending").count(),
        "queued_count": Article.query.filter_by(status="queued").count(),
        "unread_comments_count": Comment.query.filter_by(is_read=0).count(),
        "youtube_min_view_count": Setting.get("youtube_min_view_count", "5000000"),
        "youtube_max_view_count": Setting.get("youtube_max_view_count", "0"),
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
    stats = {
        s: Article.query.filter_by(status=s).count()
        for s in ("pending", "queued", "posted", "rejected", "failed")
    }
    recent = Article.query.order_by(Article.created_at.desc()).limit(15).all()
    return render_template("index.html", stats=stats, recent=recent)


# ── 承認待ち記事 ───────────────────────────────────────────────────────────


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
    articles = Article.query.filter_by(status="pending").order_by(Article.created_at.desc()).all()
    images_map: dict = {}
    for a in articles:
        imgs = _build_image_list(a.thumbnail_url, a.image_urls)
        images_map[a.id] = imgs
        logger.debug("pending preview article=%d imgs=%d", a.id, len(imgs))
    return render_template("pending.html", articles=articles, images_map=images_map)


@app.route("/pending/bulk-delete", methods=["POST"])
def bulk_delete_articles():
    ids = request.form.getlist("ids")
    if not ids:
        flash("記事が選択されていません", "secondary")
        return redirect(url_for("pending"))
    Article.query.filter(Article.id.in_([int(i) for i in ids])).delete(synchronize_session=False)
    db.session.commit()
    flash(f"{len(ids)} 件の記事を削除しました", "warning")
    return redirect(url_for("pending"))


@app.route("/pending/delete-all", methods=["POST"])
def delete_all_pending():
    count = Article.query.filter_by(status="pending").count()
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

    slot_utc = next_post_slot(app)
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
    success = summarize_article(app, id, style=style)
    # summarize_article は内部で別 app_context を開くため、セッションを明示的にリフレッシュ
    db.session.expire_all()
    article = db.session.get(Article, id)
    logger.info("resummary article=%d success=%s error_message=%r", id, success, article.error_message if article else None)
    if success:
        return jsonify({"success": True, "summary": article.summary, "length": len(article.summary or "")})
    error_msg = (article.error_message if article else None) or "要約の生成に失敗しました（サーバーログを確認してください）"
    return jsonify({"success": False, "error": error_msg})


# ── 投稿キュー ─────────────────────────────────────────────────────────────


@app.route("/queue")
def queue():
    queued = (
        Article.query.filter_by(status="queued")
        .order_by(Article.scheduled_at.asc().nullsfirst(), Article.created_at.asc())
        .all()
    )
    posted = (
        Article.query.filter_by(status="posted")
        .order_by(Article.posted_at.desc())
        .limit(30)
        .all()
    )
    failed = Article.query.filter_by(status="failed").order_by(Article.updated_at.desc()).limit(10).all()
    return render_template("queue.html", queued=queued, posted=posted, failed=failed)


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

    test_mode = Setting.get("test_mode", "true").lower() == "true"
    success, msg = post_to_threads(app, id, test_mode=test_mode)
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

        # 並び替え対象以外のキュー済みスロットを占有セットに入れる
        occupied = {
            a.scheduled_at
            for a in Article.query.filter_by(status="queued")
                                  .filter(~Article.id.in_(ids))
                                  .all()
            if a.scheduled_at is not None
        }

        schedule = get_weekly_schedule(app)
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
    return render_template("settings.html", settings=current)


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
    from scheduler import get_weekly_schedule, _setup_weekly_post_jobs, _DAY_KEYS

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

        Setting.set("weekly_schedule", json.dumps(new_schedule))

        if hasattr(app, "reschedule_post_jobs"):
            app.reschedule_post_jobs()

        flash("週間スケジュールを保存しました", "success")
        return redirect(url_for("schedule"))

    from scheduler import get_weekly_schedule
    _DAY_LABELS = {
        "mon": "月", "tue": "火", "wed": "水", "thu": "木",
        "fri": "金", "sat": "土", "sun": "日",
    }
    current = get_weekly_schedule(app)
    return render_template(
        "schedule.html",
        schedule=current,
        day_keys=_DAY_KEYS,
        day_labels=_DAY_LABELS,
    )


# ── Threads OAuth 認証 ────────────────────────────────────────────────────


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

        Setting.set("threads_access_token", long_token)
        Setting.set("threads_user_id", user_id)
        Setting.set("threads_token_acquired_at", datetime.utcnow().isoformat())
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

        Setting.set("threads_access_token", long_token)
        Setting.set("threads_user_id", user_id)
        Setting.set("threads_token_acquired_at", datetime.utcnow().isoformat())
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

    token = Setting.get("threads_access_token", "")
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

    token   = Setting.get("threads_access_token", "")
    user_id = Setting.get("threads_user_id", "")

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

    token   = Setting.get("threads_access_token", "")
    user_id = Setting.get("threads_user_id", "")

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

    setup_scheduler(app)
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000)
