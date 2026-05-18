import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database import Article, Setting

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")


def _collect_job(app):
    from rss_collector import collect_articles

    logger.info("Running scheduled RSS collection")
    collect_articles(app)


def _post_job(app):
    from threads_api import post_to_threads

    with app.app_context():
        test_mode = Setting.get("test_mode", "true").lower() == "true"
        now = datetime.utcnow()
        article = (
            Article.query.filter_by(status="queued")
            .filter(
                db.or_(
                    Article.scheduled_at.is_(None),
                    Article.scheduled_at <= now,
                )
            )
            .order_by(Article.scheduled_at.asc().nullsfirst(), Article.created_at.asc())
            .first()
        )
        article_id = article.id if article else None

    if article_id:
        logger.info("Scheduled posting article %d", article_id)
        success, msg = post_to_threads(app, article_id, test_mode=test_mode)
        logger.info("Post result: %s - %s", success, msg)


def _setup_post_jobs(app):
    """設定された投稿時刻に基づいて CronJob を再設定する。"""
    for job in scheduler.get_jobs():
        if job.id.startswith("post_"):
            scheduler.remove_job(job.id)

    with app.app_context():
        times_str = Setting.get("post_times", "09:00,15:00,21:00")

    for i, t in enumerate(times_str.split(",")):
        t = t.strip()
        try:
            hour, minute = t.split(":")
            scheduler.add_job(
                _post_job,
                CronTrigger(hour=int(hour), minute=int(minute), timezone="Asia/Tokyo"),
                args=[app],
                id=f"post_{i}",
                replace_existing=True,
            )
            logger.info("Scheduled post job at %s JST", t)
        except Exception as exc:
            logger.error("Invalid post time '%s': %s", t, exc)


def setup_scheduler(app):
    """スケジューラを初期化して起動する。"""
    with app.app_context():
        interval_h = int(Setting.get("collect_interval_hours", "2"))

    scheduler.add_job(
        _collect_job,
        IntervalTrigger(hours=interval_h),
        args=[app],
        id="collect_rss",
        replace_existing=True,
    )

    _setup_post_jobs(app)

    # 設定変更後に呼び出せるよう app に参照を持たせる
    app.reschedule_post_jobs = lambda: _setup_post_jobs(app)

    scheduler.start()
    logger.info("Scheduler started (collect every %dh)", interval_h)
    return scheduler
