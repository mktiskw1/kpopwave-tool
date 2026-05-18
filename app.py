import json
import logging
import os
import secrets
from datetime import datetime
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from config import Config
from database import Article, Setting, db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

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
]


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        _init_default_settings()

    return app


def _init_default_settings():
    defaults = {
        "rss_feeds": json.dumps(DEFAULT_FEEDS),
        "post_times": "09:00,15:00,21:00",
        "collect_interval_hours": "2",
        "test_mode": "true",
        "threads_user_id": os.getenv("THREADS_USER_ID", ""),
        "threads_access_token": os.getenv("THREADS_ACCESS_TOKEN", ""),
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "meta_app_id": os.getenv("META_APP_ID", ""),
        "meta_app_secret": os.getenv("META_APP_SECRET", ""),
        "app_base_url": os.getenv("APP_BASE_URL", "http://localhost:5000"),
    }
    for key, value in defaults.items():
        if not Setting.query.filter_by(key=key).first():
            db.session.add(Setting(key=key, value=value))
    db.session.commit()


app = create_app()


@app.context_processor
def inject_globals():
    return {
        "pending_count": Article.query.filter_by(status="pending").count(),
        "queued_count": Article.query.filter_by(status="queued").count(),
    }


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


@app.route("/pending")
def pending():
    articles = Article.query.filter_by(status="pending").order_by(Article.created_at.desc()).all()
    return render_template("pending.html", articles=articles)


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
    article = Article.query.get_or_404(id)
    article.status = "queued"
    db.session.commit()
    flash(f"キューに追加しました: {article.title[:50]}", "success")
    return redirect(request.referrer or url_for("pending"))


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

    success = summarize_article(app, id)
    if success:
        article = Article.query.get(id)
        return jsonify({"success": True, "summary": article.summary, "length": len(article.summary or "")})
    return jsonify({"success": False, "error": "要約の生成に失敗しました"})


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


# ── 設定 ───────────────────────────────────────────────────────────────────


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        for key in ("threads_user_id", "threads_access_token", "anthropic_api_key",
                    "post_times", "collect_interval_hours",
                    "meta_app_id", "meta_app_secret", "app_base_url"):
            Setting.set(key, (request.form.get(key) or "").strip())

        Setting.set("test_mode", "true" if request.form.get("test_mode") else "false")

        feed_names = request.form.getlist("feed_name")
        feed_urls = request.form.getlist("feed_url")
        feeds = [{"name": n.strip(), "url": u.strip()}
                 for n, u in zip(feed_names, feed_urls) if u.strip()]
        Setting.set("rss_feeds", json.dumps(feeds))

        flash("設定を保存しました", "success")

        if hasattr(app, "reschedule_post_jobs"):
            app.reschedule_post_jobs()

        return redirect(url_for("settings"))

    base_url = Setting.get("app_base_url", "http://localhost:5000").rstrip("/")
    current = {
        "threads_user_id": Setting.get("threads_user_id"),
        "threads_access_token": Setting.get("threads_access_token"),
        "anthropic_api_key": Setting.get("anthropic_api_key"),
        "post_times": Setting.get("post_times", "09:00,15:00,21:00"),
        "collect_interval_hours": Setting.get("collect_interval_hours", "2"),
        "test_mode": Setting.get("test_mode", "true") == "true",
        "rss_feeds": json.loads(Setting.get("rss_feeds", "[]") or "[]"),
        "meta_app_id": Setting.get("meta_app_id"),
        "meta_app_secret": Setting.get("meta_app_secret"),
        "app_base_url": base_url,
        "callback_url": base_url + "/auth/threads/callback",
    }
    return render_template("settings.html", settings=current)


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
        "scope": "threads_basic,threads_content_publish",
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
        "scope": "threads_basic,threads_content_publish",
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
        long_token = resp2.json().get("access_token")
        if not long_token:
            raise ValueError(f"長期トークンが見つかりません: {resp2.text}")

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
        flash(
            f"Threads 認証成功！ @{username}（ID: {user_id}）の"
            "アクセストークンを取得・保存しました（有効期限: 60日）",
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
        long_token = resp2.json().get("access_token")
        if not long_token:
            raise ValueError(f"長期トークンが見つかりません: {resp2.text}")

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
        flash(
            f"Threads 認証成功！ @{username}（ID: {user_id}）の"
            "アクセストークンを取得・保存しました（有効期限: 60日）",
            "success",
        )
    except Exception as e:
        logger.exception("Threads OAuth 処理エラー")
        flash(f"認証処理中にエラーが発生しました: {e}", "danger")

    return redirect(url_for("settings"))


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


@app.route("/follow-candidates")
def follow_candidates():
    from follow_candidates import get_page_data
    data = get_page_data(app)
    return render_template("follow_candidates.html", **data)


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


@app.route("/api/stats")
def api_stats():
    return jsonify({
        s: Article.query.filter_by(status=s).count()
        for s in ("pending", "queued", "posted", "rejected", "failed")
    })


# ── 起動 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from scheduler import setup_scheduler

    setup_scheduler(app)
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000)
