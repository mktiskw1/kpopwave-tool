import json
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import or_, text

from database import Article, Setting, db

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")

_JITTER_SECONDS = 1800  # ±30分
# CronジョブとIntervalジョブが同時に _post_job を起動したときの二重投稿防止
_post_job_lock = threading.Lock()
_JST = ZoneInfo("Asia/Tokyo")
_UTC = ZoneInfo("UTC")
_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DEFAULT_TIMES = ["07:00", "12:00", "15:00", "18:00", "21:00"]


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
    # ── 第1防衛: スレッドロック ─────────────────────────────────────────────
    # CronジョブとIntervalバックアップジョブが同時起動しても1つだけ実行する
    if not _post_job_lock.acquire(blocking=False):
        logger.info("[_post_job] 別ジョブ実行中 → スキップ（二重投稿防止）")
        return
    try:
        _run_post_job(app)
    finally:
        _post_job_lock.release()


def _run_post_job(app):
    from threads_api import post_to_threads

    with app.app_context():
        test_mode = Setting.get("test_mode", "true").lower() == "true"
        now = datetime.utcnow()
        now_jst = datetime.now(_JST)

        all_queued = Article.query.filter_by(status="queued").order_by(
            Article.scheduled_at.asc().nullsfirst()
        ).all()

        logger.info(
            "[_post_job] 実行開始 now_utc=%s now_jst=%s test_mode=%s queued=%d件",
            now.strftime("%Y-%m-%d %H:%M:%S"),
            now_jst.strftime("%Y-%m-%d %H:%M:%S"),
            test_mode,
            len(all_queued),
        )

        for a in all_queued:
            if a.scheduled_at is None:
                eligible = True
                reason = "scheduled_at=NULL → 即時対象"
            else:
                eligible = a.scheduled_at <= now
                diff_sec = (a.scheduled_at - now).total_seconds()
                if eligible:
                    reason = f"scheduled_at({a.scheduled_at}) <= now({now.strftime('%H:%M:%S')}) → 対象"
                else:
                    reason = (
                        f"scheduled_at({a.scheduled_at}) > now({now.strftime('%H:%M:%S')}) "
                        f"→ あと{int(diff_sec//60)}分{int(diff_sec%60)}秒"
                    )
            logger.info("[_post_job]   id=%-4d %s", a.id, reason)

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

        if not article:
            logger.info("[_post_job] 投稿対象なし（全%d件が未来スロット or キュー空）", len(all_queued))
            return

        article_id = article.id
        logger.info(
            "[_post_job] 投稿対象決定: id=%d scheduled_at(UTC)=%s has_summary=%s",
            article_id, article.scheduled_at, bool(article.summary),
        )

        # ── 第2防衛: DBレベルのアトミックロック ─────────────────────────────
        # UPDATE WHERE status='queued' が成功した場合のみ投稿を実行する。
        # 万が一スレッドロックをすり抜けた別ジョブも、rowcount==0 でスキップされる。
        result = db.session.execute(
            text("UPDATE articles SET status='posting' WHERE id=:id AND status='queued'"),
            {"id": article_id},
        )
        db.session.commit()

        if result.rowcount == 0:
            logger.warning(
                "[_post_job] id=%d のDBロック取得失敗（他ジョブが処理中）→ スキップ", article_id
            )
            return

        logger.info("[_post_job] id=%d status→'posting' ロック完了、投稿実行", article_id)

    success, msg = post_to_threads(app, article_id, test_mode=test_mode)
    logger.info("[_post_job] 投稿結果: id=%d success=%s msg=%s", article_id, success, msg)


def _rollover_overdue_job(app):
    """予定時刻を過ぎたキュー済み記事を次の空きスロットに自動繰り越す。
    _post_job（CronTrigger + jitter最大30分）との競合を避けるため、
    scheduled_at から90分以上経過した記事のみ繰り越す。"""
    with app.app_context():
        now_utc = datetime.utcnow()
        rollover_threshold = now_utc - timedelta(minutes=90)

        logger.info("[_rollover_overdue_job] 実行 UTC=%s threshold(UTC)=%s",
                    now_utc.strftime("%H:%M:%S"), rollover_threshold.strftime("%H:%M:%S"))

        # ── 'posting' スタック回復 ────────────────────────────────────────────
        # クラッシュなどで 'posting' のまま10分以上経過した記事を 'queued' に戻す
        stuck_threshold = now_utc - timedelta(minutes=10)
        stuck = (
            Article.query
            .filter(Article.status == "posting")
            .filter(Article.updated_at < stuck_threshold)
            .all()
        )
        if stuck:
            for a in stuck:
                logger.warning(
                    "[_rollover_overdue_job] 投稿スタック回復: id=%d updated_at=%s → queued に戻す",
                    a.id, a.updated_at,
                )
                a.status = "queued"
            db.session.commit()

        overdue = (
            Article.query.filter_by(status="queued")
            .filter(Article.scheduled_at.isnot(None))
            .filter(Article.scheduled_at < rollover_threshold)
            .order_by(Article.scheduled_at.asc())
            .all()
        )
        if not overdue:
            logger.debug("[_rollover_overdue_job] 繰り越し対象なし")
            return

        logger.info("[_rollover_overdue_job] 繰り越し対象: %d件", len(overdue))

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
        # "post_" で始まるジョブを削除するが、interval バックアップジョブ（"cron_post_"）は対象外
        if job.id.startswith("cron_post_"):
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
                    id=f"cron_post_{day}_{i}",
                    replace_existing=True,
                )
                job_count += 1
            except Exception as exc:
                logger.error("Invalid schedule '%s %s': %s", day, t, exc)

    logger.info("投稿ジョブ設定完了: %d件", job_count)


def setup_scheduler(app):
    """スケジューラを初期化して起動する。"""
    _setup_weekly_post_jobs(app)

    # バックアップ投稿ジョブ: CronTrigger が missed/競合した場合でも5分以内に投稿を実行する
    # ID は "cron_post_" で始まらない名前にして _setup_weekly_post_jobs で削除されないようにする
    scheduler.add_job(
        _post_job,
        IntervalTrigger(minutes=5),
        args=[app],
        id="interval_post_backup",
        replace_existing=True,
    )

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
    logger.info("Scheduler started (post backup every 5min, comments/rollover every 30min)")
    return scheduler
