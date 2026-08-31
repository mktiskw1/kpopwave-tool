import json
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import or_, text

from database import Article, EarlyAdvanceLog, PostStat, Setting, ThreadsAccount, get_active_account, db

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")

_JITTER_SECONDS = 1800  # ±30分
_JST = ZoneInfo("Asia/Tokyo")
_UTC = ZoneInfo("UTC")
_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DEFAULT_TIMES = ["07:00", "12:00", "15:00", "18:00", "21:00"]


# ── ユーティリティ ─────────────────────────────────────────────────────────────

def _resolve_account_context(app, account_id: int = None) -> tuple:
    """account_id を正規化する。

    account_id が None、または「最古のアクティブアカウント」（＝マルチアカウント化以前から
    存在する唯一のアカウント）と一致する場合は is_legacy=True を返し、
    無プレフィックスの既存設定キー（weekly_schedule / post_times）をそのまま使い続ける。
    それ以外の account_id は専用プレフィックスキーを使う新規アカウントとして扱う。
    """
    if account_id is None:
        return None, True
    legacy = get_active_account(app)
    is_legacy = bool(legacy and legacy["id"] == account_id)
    return account_id, is_legacy


def get_weekly_schedule(app, account_id: int = None) -> dict:
    """DB から週間スケジュールを取得。未設定なら post_times 設定で全曜日を埋めて返す。

    account_id 省略時、または既存の唯一アカウントの場合は従来通り無プレフィックスキーを使う。
    """
    resolved_id, is_legacy = _resolve_account_context(app, account_id)
    schedule_key = "weekly_schedule" if is_legacy else f"weekly_schedule_{resolved_id}"
    post_times_key = "post_times" if is_legacy else f"post_times_{resolved_id}"

    with app.app_context():
        raw = Setting.get(schedule_key, "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    # フォールバック: post_times 設定を全曜日に適用
    with app.app_context():
        times_str = Setting.get(post_times_key, ",".join(_DEFAULT_TIMES))
    times = [t.strip() for t in times_str.split(",") if t.strip()]
    return {day: times for day in _DAY_KEYS}


def set_weekly_schedule(app, schedule_dict: dict, account_id: int = None) -> None:
    """週間スケジュールを保存する。account_id の解決規則は get_weekly_schedule と同じ。"""
    resolved_id, is_legacy = _resolve_account_context(app, account_id)
    schedule_key = "weekly_schedule" if is_legacy else f"weekly_schedule_{resolved_id}"
    with app.app_context():
        Setting.set(schedule_key, json.dumps(schedule_dict))


def next_post_slot(app, account_id: int = None) -> datetime | None:
    """週間スケジュールから次の投稿スロット（UTC naive）を返す。
    既に同スロットに同アカウントのキュー済み記事がある場合は次のスロットを探す。"""
    schedule = get_weekly_schedule(app, account_id)
    now_jst = datetime.now(_JST)

    with app.app_context():
        query = Article.query.filter_by(status="queued")
        if account_id is not None:
            _, is_legacy = _resolve_account_context(app, account_id)
            if is_legacy:
                query = query.filter(or_(Article.account_id == account_id, Article.account_id.is_(None)))
            else:
                query = query.filter(Article.account_id == account_id)
        occupied = {
            a.scheduled_at
            for a in query.all()
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
#
# _collect_job / _collect_youtube_job は意図的に setup_scheduler() に登録していない
# (定期実行しない)。過去に自動収集(RSS/YouTube定期収集)で男性グループの動画まで
# 混ざって収集される精度の問題が起きたため、手動収集に切り替えた
# (app.pyがrss_collector.collect_articles / youtube_collector.collect_youtube_videosを
# 直接呼び出しており、この2つのラッパー関数自体は現在どこからも呼ばれていない)。
# その後実装した「音楽番組から出演回を探す」「fancamを探す」機能が、グループ名を
# 指定した検索によりこの精度問題を解決する代替手段として機能している。
# 定期実行を再開する場合の雛形として残してある。

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


_post_job_locks: dict = {}
_post_job_locks_guard = threading.Lock()


def _get_account_lock(account_id) -> threading.Lock:
    """アカウントごとの排他ロックを取得する（同一アカウントの二重投稿防止用）。"""
    with _post_job_locks_guard:
        if account_id not in _post_job_locks:
            _post_job_locks[account_id] = threading.Lock()
        return _post_job_locks[account_id]


def _active_account_ids(app) -> list:
    with app.app_context():
        return [
            a.id for a in
            ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all()
        ]


def _post_job(app):
    """全アクティブアカウントの投稿処理を順番に実行する（Intervalバックアップ用）。"""
    account_ids = _active_account_ids(app)
    if not account_ids:
        logger.warning("[_post_job] アクティブな threads_accounts が存在しません")
        return
    for account_id in account_ids:
        _post_job_for_account(app, account_id)


def _post_job_for_account(app, account_id):
    # ── 第1防衛: スレッドロック（アカウント単位） ───────────────────────────
    # CronジョブとIntervalバックアップジョブが同一アカウントに対して同時起動しても
    # 1つだけ実行する。別アカウントは別ロックのため並行実行可能。
    lock = _get_account_lock(account_id)
    if not lock.acquire(blocking=False):
        logger.info("[_post_job] account_id=%s 別ジョブ実行中 → スキップ（二重投稿防止）", account_id)
        return
    try:
        _run_post_job_for_account(app, account_id)
    finally:
        lock.release()


def _run_post_job_for_account(app, account_id):
    from threads_api import post_to_threads

    with app.app_context():
        _, is_legacy = _resolve_account_context(app, account_id)
        test_mode = Setting.get("test_mode", "true").lower() == "true"
        now = datetime.utcnow()
        now_jst = datetime.now(_JST)

        def _scope(query):
            if is_legacy:
                return query.filter(or_(Article.account_id == account_id, Article.account_id.is_(None)))
            return query.filter(Article.account_id == account_id)

        all_queued = _scope(
            Article.query.filter_by(status="queued")
        ).order_by(Article.scheduled_at.asc().nullsfirst()).all()

        logger.info(
            "[_post_job] account_id=%s 実行開始 now_utc=%s now_jst=%s test_mode=%s queued=%d件",
            account_id,
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
            logger.info("[_post_job]   account_id=%s id=%-4d %s", account_id, a.id, reason)

        article = (
            _scope(Article.query.filter_by(status="queued"))
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
            logger.info(
                "[_post_job] account_id=%s 投稿対象なし（全%d件が未来スロット or キュー空）",
                account_id, len(all_queued),
            )
            return

        article_id = article.id
        logger.info(
            "[_post_job] account_id=%s 投稿対象決定: id=%d scheduled_at(UTC)=%s has_summary=%s",
            account_id, article_id, article.scheduled_at, bool(article.summary),
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
                "[_post_job] account_id=%s id=%d のDBロック取得失敗（他ジョブが処理中）→ スキップ",
                account_id, article_id,
            )
            return

        logger.info(
            "[_post_job] account_id=%s id=%d status→'posting' ロック完了、投稿実行",
            account_id, article_id,
        )

    success, msg = post_to_threads(app, article_id, test_mode=test_mode, account_id=account_id)
    logger.info(
        "[_post_job] account_id=%s 投稿結果: id=%d success=%s msg=%s",
        account_id, article_id, success, msg,
    )


_EARLY_ADVANCE_OFFSETS = {15, 30, 60}
_EARLY_ADVANCE_CHAIN_LIMIT = 2


def _check_and_advance_on_zero_engagement(app, account_id: int = 1) -> None:
    """投稿から60分経過していいねが0のままの投稿を検出し、次のキュー記事を前倒し投稿する。
    連続で0いいねが続く限り最大2回まで連鎖し、いいねが付いた時点・上限到達時点でリセットする。
    (アプリ再起動をまたいでも安全なよう、連鎖カウンターはSettingテーブルに永続化する)"""
    with app.app_context():
        if Setting.get("early_advance_enabled", "true").lower() != "true":
            return

        _, is_legacy = _resolve_account_context(app, account_id)

        def _scope(query):
            if is_legacy:
                return query.filter(or_(Article.account_id == account_id, Article.account_id.is_(None)))
            return query.filter(Article.account_id == account_id)

        now = datetime.utcnow()
        candidate_ids = [
            row[0] for row in
            _scope(Article.query)
            .filter(
                Article.status == "posted",
                Article.posted_at.isnot(None),
                Article.posted_at <= now - timedelta(minutes=60),
                Article.posted_at >= now - timedelta(minutes=70),
            )
            .with_entities(Article.id)
            .all()
        ]
        if not candidate_ids:
            return

        already_evaluated = {
            row[0] for row in
            db.session.query(EarlyAdvanceLog.source_article_id)
            .filter(EarlyAdvanceLog.source_article_id.in_(candidate_ids))
            .all()
        }

        zero_engagement_article_id = None
        for article_id in candidate_ids:
            if article_id in already_evaluated:
                continue
            offset_likes = {
                row[0]: row[1] for row in
                db.session.query(PostStat.minute_offset, PostStat.likes)
                .filter(PostStat.article_id == article_id, PostStat.minute_offset.in_(_EARLY_ADVANCE_OFFSETS))
                .all()
            }
            if set(offset_likes.keys()) != _EARLY_ADVANCE_OFFSETS:
                continue  # まだ全オフセットの記録が揃っていない
            if any(v > 0 for v in offset_likes.values()):
                # いいねが付いた投稿があった → 連鎖をリセットして通常運用に戻す
                Setting.set("early_advance_chain_count", "0")
                continue
            zero_engagement_article_id = article_id
            break  # 1回のジョブ実行では1件のみ処理する(残りは次回以降のジョブに回す)

    if zero_engagement_article_id is not None:
        _advance_or_stop_chain(app, account_id, zero_engagement_article_id)


def _advance_or_stop_chain(app, account_id: int, source_article_id: int) -> None:
    with app.app_context():
        chain_count = int(Setting.get("early_advance_chain_count", "0") or "0")
        chain_position = chain_count + 1

        if chain_count >= _EARLY_ADVANCE_CHAIN_LIMIT:
            db.session.add(EarlyAdvanceLog(
                source_article_id=source_article_id,
                target_article_id=None,
                chain_position=chain_position,
                action="limit_reached",
                note=f"前倒し投稿の連鎖が上限({_EARLY_ADVANCE_CHAIN_LIMIT}回)に達したため見送り、通常スケジュールに戻します",
            ))
            Setting.set("early_advance_chain_count", "0")
            db.session.commit()
            logger.warning(
                "[early_advance] 連鎖上限到達のため前倒しを見送り: source_article_id=%s", source_article_id,
            )
            return

        _, is_legacy = _resolve_account_context(app, account_id)

        def _scope(query):
            if is_legacy:
                return query.filter(or_(Article.account_id == account_id, Article.account_id.is_(None)))
            return query.filter(Article.account_id == account_id)

        next_article = (
            _scope(Article.query.filter_by(status="queued"))
            .order_by(Article.scheduled_at.asc().nullsfirst(), Article.created_at.asc())
            .first()
        )

        if not next_article:
            db.session.add(EarlyAdvanceLog(
                source_article_id=source_article_id,
                target_article_id=None,
                chain_position=chain_position,
                action="no_queue",
                note="投稿から60分経過していいね0でしたが、前倒しできるキュー記事がありませんでした",
            ))
            db.session.commit()
            logger.warning(
                "[early_advance] 前倒し対象のキューなし: source_article_id=%s", source_article_id,
            )
            return

        target_article_id = next_article.id
        next_article.scheduled_at = None
        db.session.add(EarlyAdvanceLog(
            source_article_id=source_article_id,
            target_article_id=target_article_id,
            chain_position=chain_position,
            action="advanced",
            note=(
                f"投稿id={source_article_id}が60分経過していいね0だったため、"
                f"次の投稿(id={target_article_id})を前倒し実行します({chain_position}/{_EARLY_ADVANCE_CHAIN_LIMIT})"
            ),
        ))
        Setting.set("early_advance_chain_count", str(chain_position))
        db.session.commit()
        logger.info(
            "[early_advance] 前倒し実行: target_article_id=%s chain=%d/%d source_article_id=%s",
            target_article_id, chain_position, _EARLY_ADVANCE_CHAIN_LIMIT, source_article_id,
        )

    _post_job_for_account(app, account_id)


def _rollover_overdue_job(app):
    """予定時刻を過ぎたキュー済み記事を次の空きスロットに自動繰り越す（アカウント単位）。
    _post_job（CronTrigger + jitter最大30分）との競合を避けるため、
    scheduled_at から90分以上経過した記事のみ繰り越す。"""
    with app.app_context():
        now_utc = datetime.utcnow()
        rollover_threshold = now_utc - timedelta(minutes=90)

        logger.info("[_rollover_overdue_job] 実行 UTC=%s threshold(UTC)=%s",
                    now_utc.strftime("%H:%M:%S"), rollover_threshold.strftime("%H:%M:%S"))

        # ── 'posting' スタック回復 ────────────────────────────────────────────
        # クラッシュなどで 'posting' のまま10分以上経過した記事を 'queued' に戻す（全アカウント共通）
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

    account_ids = _active_account_ids(app)
    if not account_ids:
        logger.warning("[_rollover_overdue_job] アクティブな threads_accounts が存在しません")
        return

    for account_id in account_ids:
        _rollover_overdue_for_account(app, account_id, now_utc, rollover_threshold)


def _rollover_overdue_for_account(app, account_id, now_utc, rollover_threshold):
    with app.app_context():
        _, is_legacy = _resolve_account_context(app, account_id)

        def _scope(query):
            if is_legacy:
                return query.filter(or_(Article.account_id == account_id, Article.account_id.is_(None)))
            return query.filter(Article.account_id == account_id)

        overdue = (
            _scope(Article.query.filter_by(status="queued"))
            .filter(Article.scheduled_at.isnot(None))
            .filter(Article.scheduled_at < rollover_threshold)
            .order_by(Article.scheduled_at.asc())
            .all()
        )
        if not overdue:
            logger.debug("[_rollover_overdue_job] account_id=%s 繰り越し対象なし", account_id)
            return

        logger.info("[_rollover_overdue_job] account_id=%s 繰り越し対象: %d件", account_id, len(overdue))

        # 未来スロットの使用済みセットを構築（同アカウント分のみ）
        occupied = {
            a.scheduled_at
            for a in _scope(Article.query.filter_by(status="queued")).all()
            if a.scheduled_at is not None and a.scheduled_at > now_utc
        }

        schedule = get_weekly_schedule(app, account_id)
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
                logger.info("繰り越し: account_id=%s article %d %s → %s UTC",
                            account_id, article.id, article.scheduled_at, new_slot)
                article.scheduled_at = new_slot
                occupied.add(new_slot)
            else:
                logger.warning("繰り越し先スロットなし: account_id=%s article %d", account_id, article.id)

        db.session.commit()


# ── スケジューラーセットアップ ─────────────────────────────────────────────────

def _setup_weekly_post_jobs(app):
    """週間スケジュールから CronJob を再設定する（±30分ゆらぎ付き）。"""
    for job in scheduler.get_jobs():
        # "post_" で始まるジョブを削除するが、interval バックアップジョブ（"cron_post_"）は対象外
        if job.id.startswith("cron_post_"):
            scheduler.remove_job(job.id)

    account_ids = _active_account_ids(app)
    if not account_ids:
        logger.warning("投稿ジョブ設定スキップ: アクティブな threads_accounts が存在しません")
        return

    job_count = 0
    for account_id in account_ids:
        schedule = get_weekly_schedule(app, account_id)
        for day, times in schedule.items():
            for i, t in enumerate(times or []):
                t = t.strip()
                if not t:
                    continue
                try:
                    hour, minute = t.split(":")
                    scheduler.add_job(
                        _post_job_for_account,
                        CronTrigger(
                            day_of_week=day,
                            hour=int(hour),
                            minute=int(minute),
                            timezone="Asia/Tokyo",
                            jitter=_JITTER_SECONDS,
                        ),
                        args=[app, account_id],
                        id=f"cron_post_{account_id}_{day}_{i}",
                        replace_existing=True,
                    )
                    job_count += 1
                except Exception as exc:
                    logger.error("Invalid schedule '%s %s' (account_id=%s): %s", day, t, account_id, exc)

    logger.info("投稿ジョブ設定完了: %d件 (%dアカウント)", job_count, len(account_ids))


def _engagement_job(app):
    """投稿済み記事のいいね数をThreads APIから取得してDBに保存する（毎日1回）。"""
    from engagement_tracker import refresh_engagement
    result = refresh_engagement(app)
    logger.info(
        "エンゲージメント定期取得: 更新=%d スキップ=%d エラー=%d 合計=%d",
        result.get("updated", 0), result.get("skipped", 0),
        result.get("errors", 0), result.get("total", 0),
    )


def _video_cleanup_job(app):
    """投稿済み動画ファイルのうち7日経過・いいね200未満のものを削除する（毎日1回）。"""
    import os
    cutoff = datetime.utcnow() - timedelta(days=7)
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    videos_dir = os.path.join(static_dir, "videos")

    with app.app_context():
        targets = (
            Article.query
            .filter(
                Article.status == "posted",
                Article.content_type == "video",
                Article.video_file_path.isnot(None),
                Article.posted_at < cutoff,
                or_(Article.like_count.is_(None), Article.like_count < 200),
            )
            .all()
        )

        deleted_files = 0
        for article in targets:
            base_name = os.path.splitext(os.path.basename(article.video_file_path))[0]

            main_path = os.path.join(static_dir, article.video_file_path)
            if os.path.exists(main_path):
                try:
                    os.remove(main_path)
                    deleted_files += 1
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
                            deleted_files += 1
                        except OSError:
                            pass

            db.session.delete(article)

        if targets:
            db.session.commit()

        logger.info("動画クリーンアップ: %d件対象 %dファイル削除 %dレコード削除", len(targets), deleted_files, len(targets))


def _post_stats_job(app):
    """KPOPアカウント（account_id=1）の投稿別7日間パフォーマンスを日次取得する。"""
    from analytics_tracker import track_post_stats
    result = track_post_stats(app, account_id=1)
    logger.info("投稿別7日間パフォーマンス定期取得: %s", result)


def _early_engagement_job(app):
    """KPOPアカウント（account_id=1）の投稿直後(60分以内)の初速インサイトを取得し、
    60分経過していいね0の投稿を検出した場合は次のキュー記事を前倒し投稿する。"""
    from analytics_tracker import track_early_engagement
    result = track_early_engagement(app, account_id=1)
    logger.info("初速インサイト定期取得: %s", result)
    _check_and_advance_on_zero_engagement(app, account_id=1)


def _daily_snapshot_job(app):
    """KPOPアカウント（account_id=1）のフォロワー数・閲覧数を日次スナップショットする。"""
    from analytics_tracker import snapshot_daily_stats
    result = snapshot_daily_stats(app, account_id=1)
    logger.info("日次フォロワー・閲覧数スナップショット: %s", result)


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

    scheduler.add_job(
        _early_engagement_job,
        IntervalTrigger(minutes=5),
        args=[app],
        id="early_engagement",
        replace_existing=True,
    )

    scheduler.add_job(
        _engagement_job,
        CronTrigger(hour=2, minute=0, timezone="Asia/Tokyo"),
        args=[app],
        id="engagement_daily",
        replace_existing=True,
    )

    scheduler.add_job(
        _video_cleanup_job,
        CronTrigger(hour=3, minute=0, timezone="Asia/Tokyo"),
        args=[app],
        id="video_cleanup",
        replace_existing=True,
    )

    scheduler.add_job(
        _post_stats_job,
        CronTrigger(hour=2, minute=30, timezone="Asia/Tokyo"),
        args=[app],
        id="post_stats_daily",
        replace_existing=True,
    )

    scheduler.add_job(
        _daily_snapshot_job,
        CronTrigger(hour=3, minute=30, timezone="Asia/Tokyo"),
        args=[app],
        id="daily_snapshot",
        replace_existing=True,
    )

    app.reschedule_post_jobs = lambda: _setup_weekly_post_jobs(app)

    scheduler.start()
    logger.info(
        "Scheduler started (post backup 5min, comments/rollover 30min, early engagement 5min, "
        "engagement 2:00 JST, video cleanup 3:00 JST, post stats 2:30 JST, daily snapshot 3:30 JST)"
    )
    return scheduler
