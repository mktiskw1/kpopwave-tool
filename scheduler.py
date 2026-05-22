import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import or_

from database import Article, Setting, db

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")

_JITTER_SECONDS = 1800  # ±30分
_JST = ZoneInfo("Asia/Tokyo")
_UTC = ZoneInfo("UTC")
_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DEFAULT_TIMES = ["09:00", "15:00", "21:00"]


# ── ユーティリティ ─────────────────────────────────────────────────────────────

def get_weekly_schedule(app) -> dict:
    """DB から週間スケジュールを取得。未設定なら post_times 設定で全曜日を埋めて返す。"""
    with app.app_context():
        raw = Setting.get("weekly_schedule", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    # フォールバック: 既存の post_times を全曜日に適用
    with app.app_context():
        times_str = Setting.get("post_times", ",".join(_DEFAULT_TIMES))
    times = [t.strip() for t in times_str.split(",") if t.strip()]
    return {day: times for day in _DAY_KEYS}


def next_post_slot(app) -> datetime | None:
    """週間スケジュールから次の投稿スロット（UTC naive）を返す。
    既に同スロットにキュー済み記事がある場合は次のスロットを探す。"""
    schedule = get_weekly_schedule(app)
    now_jst = datetime.now(_JST)

    with app.app_context():
        occupied = {
            a.scheduled_at
            for a in Article.query.filter_by(status="queued").all()
            if a.scheduled_at is not None
        }

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
                # 同じスロットに記事が入っていなければ採用
                if slot_utc not in occupied:
                    return slot_utc
            except Exception:
                pass

    return None


# ── ジョブ関数 ─────────────────────────────────────────────────────────────────

def _collect_job(app):
    from rss_collector import collect_articles
    logger.info("Running scheduled RSS collection")
    collect_articles(app)


def _collect_youtube_job(app):
    from youtube_collector import collect_youtube_videos
    logger.info("Running scheduled YouTube collection")
    collect_youtube_videos(app)


def _collect_comments_job(app):
    from comments import fetch_comments
    logger.info("Running scheduled comment collection")
    fetch_comments(app)


def _post_job(app):
    from threads_api import post_to_threads

    with app.app_context():
        test_mode = Setting.get("test_mode", "true").lower() == "true"
        now = datetime.utcnow()
        article = (
            Article.query.filter_by(status="queued")
            .filter(
                or_(
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


def _rollover_overdue_job(app):
    """予定時刻を過ぎたキュー済み記事を次の空きスロットに自動繰り越す。"""
    with app.app_context():
        now_utc = datetime.utcnow()
        overdue = (
            Article.query.filter_by(status="queued")
            .filter(Article.scheduled_at.isnot(None))
            .filter(Article.scheduled_at < now_utc)
            .order_by(Article.scheduled_at.asc())
            .all()
        )
        if not overdue:
            return

        logger.info("繰り越し対象: %d件", len(overdue))

        # 未来スロットの使用済みセットを構築
        occupied = {
            a.scheduled_at
            for a in Article.query.filter_by(status="queued").all()
            if a.scheduled_at is not None and a.scheduled_at > now_utc
        }

        schedule = get_weekly_schedule(app)
        now_jst = datetime.now(_JST)

        def _next_free_slot():
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

        for article in overdue:
            new_slot = _next_free_slot()
            if new_slot:
                logger.info("繰り越し: article %d %s → %s UTC", article.id, article.scheduled_at, new_slot)
                article.scheduled_at = new_slot
                occupied.add(new_slot)
            else:
                logger.warning("繰り越し先スロットなし: article %d", article.id)

        db.session.commit()


# ── スケジューラーセットアップ ─────────────────────────────────────────────────

def _setup_weekly_post_jobs(app):
    """週間スケジュールから CronJob を再設定する（±30分ゆらぎ付き）。"""
    for job in scheduler.get_jobs():
        if job.id.startswith("post_"):
            scheduler.remove_job(job.id)

    schedule = get_weekly_schedule(app)
    job_count = 0

    for day, times in schedule.items():
        for i, t in enumerate(times or []):
            t = t.strip()
            if not t:
                continue
            try:
                hour, minute = t.split(":")
                scheduler.add_job(
                    _post_job,
                    CronTrigger(
                        day_of_week=day,
                        hour=int(hour),
                        minute=int(minute),
                        timezone="Asia/Tokyo",
                        jitter=_JITTER_SECONDS,
                    ),
                    args=[app],
                    id=f"post_{day}_{i}",
                    replace_existing=True,
                )
                job_count += 1
            except Exception as exc:
                logger.error("Invalid schedule '%s %s': %s", day, t, exc)

    logger.info("投稿ジョブ設定完了: %d件", job_count)


def setup_scheduler(app):
    """スケジューラを初期化して起動する。"""
    with app.app_context():
        interval_h = int(Setting.get("collect_interval_hours", "2"))
        yt_interval_h = int(Setting.get("youtube_collect_interval_hours", "6"))

    scheduler.add_job(
        _collect_job,
        IntervalTrigger(hours=interval_h),
        args=[app],
        id="collect_rss",
        replace_existing=True,
    )

    scheduler.add_job(
        _collect_youtube_job,
        IntervalTrigger(hours=yt_interval_h),
        args=[app],
        id="collect_youtube",
        replace_existing=True,
    )

    _setup_weekly_post_jobs(app)

    scheduler.add_job(
        _rollover_overdue_job,
        IntervalTrigger(minutes=30),
        args=[app],
        id="rollover_overdue",
        replace_existing=True,
    )

    scheduler.add_job(
        _collect_comments_job,
        IntervalTrigger(minutes=30),
        args=[app],
        id="collect_comments",
        replace_existing=True,
    )

    app.reschedule_post_jobs = lambda: _setup_weekly_post_jobs(app)

    scheduler.start()
    logger.info(
        "Scheduler started (RSS every %dh, YouTube every %dh)",
        interval_h, yt_interval_h,
    )
    return scheduler
