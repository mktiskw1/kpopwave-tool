import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import func

from database import Article, DailyStat, PostStat, get_active_account, db

logger = logging.getLogger(__name__)

THREADS_API = "https://graph.threads.net/v1.0"
_JST = ZoneInfo("Asia/Tokyo")


def _get_credentials(app, account_id: int = 1) -> tuple:
    """(threads_user_id, access_token) を返す。取得できなければ (None, None)。"""
    account = get_active_account(app, account_id)
    if not account:
        return None, None
    return account["threads_user_id"], account["threads_access_token"]


def _parse_media_insights(data: dict) -> dict:
    """投稿単位Insights APIレスポンスから {metric_name: value} を作る。"""
    result = {}
    for item in data.get("data", []):
        name = item.get("name")
        values = item.get("values", [])
        if name and values:
            result[name] = values[0].get("value", 0)
    return result


def _fetch_media_insights(post_id: str, token: str) -> dict:
    try:
        resp = requests.get(
            f"{THREADS_API}/{post_id}/insights",
            params={"metric": "views,likes,replies,reposts,quotes", "access_token": token},
            timeout=15,
        )
        if not resp.ok:
            logger.warning("Post insights HTTP %d [%s]: %s", resp.status_code, post_id, resp.text[:200])
            return {}
        return _parse_media_insights(resp.json())
    except Exception as exc:
        logger.error("Post insights fetch error [%s]: %s", post_id, exc)
        return {}


def _compute_day_index(posted_at: datetime, now: datetime = None) -> int:
    """投稿からの経過日数を返す。"""
    now = now or datetime.utcnow()
    return (now - posted_at).days


_EARLY_OFFSETS_MIN = [15, 30, 60]


def track_early_engagement(app, account_id: int = 1) -> dict:
    """account_id の、投稿から60分以内の投稿を対象に、15分・30分・60分経過時点での
    インサイトを記録する(初速追跡)。post_stats に day_index=0, minute_offset=15/30/60 で
    1行ずつ追加する。各offsetは経過時間を超えた時点で1回だけ取得し(既に記録済みならスキップ)、
    60分を超えた投稿は対象から外れ、以後は日次のtrack_post_statsに引き継がれる。
    ジョブの実行間隔ぶんの遅延を許容するため、投稿から65分以内までを対象にする。"""
    _, token = _get_credentials(app, account_id)
    if not token:
        return {"error": "Threadsアクセストークン未設定", "updated": 0, "errors": 0}

    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=65)

    with app.app_context():
        candidates = (
            Article.query
            .filter(
                Article.account_id == account_id,
                Article.status == "posted",
                Article.posted_at.isnot(None),
                Article.posted_at >= cutoff,
                Article.threads_post_id.isnot(None),
                Article.threads_post_id != "",
            )
            .with_entities(Article.id, Article.threads_post_id, Article.posted_at)
            .all()
        )

    updated = errors = 0
    for article_id, post_id, posted_at in candidates:
        if post_id.startswith("test_"):
            continue
        elapsed_min = (now - posted_at).total_seconds() / 60

        with app.app_context():
            recorded_offsets = {
                row[0] for row in
                db.session.query(PostStat.minute_offset)
                .filter(PostStat.article_id == article_id, PostStat.minute_offset.isnot(None))
                .all()
            }

        for offset in _EARLY_OFFSETS_MIN:
            if elapsed_min < offset or offset in recorded_offsets:
                continue
            insights = _fetch_media_insights(post_id, token)
            if not insights:
                errors += 1
                continue
            with app.app_context():
                db.session.add(PostStat(
                    article_id=article_id,
                    day_index=0,
                    minute_offset=offset,
                    likes=insights.get("likes", 0),
                    views=insights.get("views", 0),
                    replies=insights.get("replies", 0),
                    reposts=insights.get("reposts", 0),
                    quotes=insights.get("quotes", 0),
                    is_final=False,
                ))
                db.session.commit()
            updated += 1
            time.sleep(0.3)

    result = {"updated": updated, "errors": errors}
    logger.info("初速追跡完了: %s", result)
    return result


def track_post_stats(app, account_id: int = 1) -> dict:
    """account_id の posted 記事のうち、7日確定値がまだ出ていないものを対象に
    Threads Media Insights を取得し post_stats に1行追加する。
    day_index >= 7 に達した回で is_final=True を立て、以後その「投稿サイクル」は対象から外れる。

    確定済みかどうかは article_id 単体ではなく、現在の posted_at との時系列比較で判定する
    (is_finalな記録のうち最新のfetched_atが現posted_at以降なら確定済み)。これにより、
    記事が再投稿されてposted_atが更新された場合、古いサイクルのis_final記録は「過去のもの」
    とみなされ、新しいサイクルとして再び7日間の追跡が始まる。post_stats の行自体は
    履歴として残し、削除はしない(複数回投稿された記事は分析上も複数の投稿実績として扱う)。
    """
    _, token = _get_credentials(app, account_id)
    if not token:
        return {"error": "Threadsアクセストークン未設定", "updated": 0, "total": 0}

    with app.app_context():
        candidates = (
            Article.query
            .filter(Article.account_id == account_id)
            .filter(Article.status == "posted")
            .filter(Article.posted_at.isnot(None))
            .filter(Article.threads_post_id.isnot(None))
            .filter(Article.threads_post_id != "")
            .with_entities(Article.id, Article.threads_post_id, Article.posted_at)
            .all()
        )
        candidate_ids = [row[0] for row in candidates]
        latest_final_fetched_at = dict(
            db.session.query(PostStat.article_id, func.max(PostStat.fetched_at))
            .filter(PostStat.article_id.in_(candidate_ids), PostStat.is_final.is_(True))
            .group_by(PostStat.article_id)
            .all()
        ) if candidate_ids else {}

        targets = [
            row for row in candidates
            if latest_final_fetched_at.get(row[0]) is None
            or latest_final_fetched_at[row[0]] < row[2]
        ]

    updated = errors = skipped = 0
    now = datetime.utcnow()

    for article_id, post_id, posted_at in targets:
        if post_id.startswith("test_"):
            skipped += 1
            continue

        day_index = _compute_day_index(posted_at, now)
        insights = _fetch_media_insights(post_id, token)

        if not insights:
            errors += 1
            if day_index < 7:
                # まだ7日以内なので今回は記録せず、翌日以降の再取得に委ねる
                continue
            # 7日を過ぎても取得できない投稿は、無期限リトライを避けるため
            # 0値のまま確定させて追跡対象から外す
            insights = {}

        is_final = day_index >= 7

        with app.app_context():
            db.session.add(PostStat(
                article_id=article_id,
                day_index=day_index,
                likes=insights.get("likes", 0),
                views=insights.get("views", 0),
                replies=insights.get("replies", 0),
                reposts=insights.get("reposts", 0),
                quotes=insights.get("quotes", 0),
                is_final=is_final,
            ))
            db.session.commit()
        updated += 1
        time.sleep(0.3)

    result = {"updated": updated, "skipped": skipped, "errors": errors, "total": len(targets)}
    logger.info("投稿別7日間パフォーマンス取得完了: %s", result)
    return result


def _parse_account_insights(data: dict) -> tuple:
    """アカウント単位Insights APIレスポンスから (followers_count, views_count) を作る。
    views_count は期間内の値を合算する。取得できない指標は None。"""
    followers_count = None
    views_count = None
    for item in data.get("data", []):
        name = item.get("name")
        if name == "followers_count":
            total = item.get("total_value") or {}
            if "value" in total:
                followers_count = total["value"]
            elif item.get("values"):
                followers_count = item["values"][-1].get("value")
        elif name == "views":
            values = item.get("values", [])
            if values:
                views_count = sum(v.get("value", 0) for v in values)
            else:
                total = item.get("total_value") or {}
                views_count = total.get("value")
    return followers_count, views_count


# Threads User Insightsの日別views(period=day)は、米国太平洋時間の1日(07:00 UTC区切り)ごとのバケットで返る。
# 各値のend_timeは「そのバケットの開始時刻」(例: 2026-10-01T07:00:00+0000 = 太平洋時間10/01の1日分)で、
# 開始から24時間たつまでは集計途中の値。daily_statsの日付には、このバケットの日付(end_timeのUTC日付)を使う。
_VIEWS_BUCKET_HOURS = 24
_VIEWS_REQUEST_DAYS = 28   # 1回のリクエストで取得する期間(長期間は分割して取得)
SNAPSHOT_LOOKBACK_DAYS = 7  # 日次ジョブで確定済みの日別viewsを取り直す日数(取得漏れの自己修復を兼ねる)


def fetch_daily_view_buckets(user_id: str, token: str, since: datetime, until: datetime) -> dict:
    """since〜until(UTCのaware datetime)の日別viewsバケットを取得して
    {バケット日付(date): (値, 確定済みか)} を返す。確定済み=開始から24時間経過。エラー時は例外。"""
    buckets = {}
    now = datetime.now(timezone.utc)
    cur = since
    while cur < until:
        nxt = min(cur + timedelta(days=_VIEWS_REQUEST_DAYS), until)
        resp = requests.get(
            f"{THREADS_API}/{user_id}/threads_insights",
            params={"metric": "views", "since": int(cur.timestamp()), "until": int(nxt.timestamp()),
                    "access_token": token},
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        for item in resp.json().get("data", []):
            if item.get("name") != "views":
                continue
            for v in item.get("values", []):
                try:
                    start = datetime.strptime(v["end_time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                except (KeyError, ValueError):
                    continue
                complete = start + timedelta(hours=_VIEWS_BUCKET_HOURS) <= now
                buckets[start.date()] = (v.get("value", 0), complete)
        cur = nxt
    return buckets


def sync_daily_views(app, account_id: int, since_date: date, user_id: str, token: str) -> dict:
    """since_date以降の日別viewsを、確定済みバケットの値でdaily_statsに反映する(冪等)。
    - 既存のsource="api"の行はviews_countを確定値で上書きする。source="manual"の行は触らない。
    - 行が無い日は、既存の最古の日付以降に限り新規作成する(followers_countはNone)。
    - 集計途中・取得できない日のviewsはNoneにする(過去に「取得時点までの途中値」を保存していた行の補正を含む)。"""
    since_dt = datetime(since_date.year, since_date.month, since_date.day, tzinfo=timezone.utc) - timedelta(days=1)
    buckets = fetch_daily_view_buckets(user_id, token, since_dt, datetime.now(timezone.utc))

    stats = {"updated": 0, "created": 0, "nulled": 0, "skipped_manual": 0}
    with app.app_context():
        earliest = db.session.query(func.min(DailyStat.stat_date)).filter(DailyStat.account_id == account_id).scalar()
        rows = {r.stat_date: r for r in DailyStat.query.filter(
            DailyStat.account_id == account_id, DailyStat.stat_date >= since_date).all()}
        for d, (value, complete) in sorted(buckets.items()):
            if d < since_date or not complete:
                continue
            row = rows.get(d)
            if row is None:
                if earliest is not None and d >= earliest:
                    db.session.add(DailyStat(account_id=account_id, stat_date=d, followers_count=None,
                                             views_count=value, source="api"))
                    stats["created"] += 1
                continue
            if row.source == "manual":
                stats["skipped_manual"] += 1
                continue
            if row.views_count != value:
                row.views_count = value
                stats["updated"] += 1
        for d, row in rows.items():
            bucket = buckets.get(d)
            if (bucket is None or not bucket[1]) and row.source != "manual" and row.views_count is not None:
                row.views_count = None
                stats["nulled"] += 1
        db.session.commit()
    return stats


def snapshot_daily_stats(app, account_id: int = 1) -> dict:
    """account_id の User Insights から、本日（JST）のfollowers_count(その時点の値)を記録し、
    直近SNAPSHOT_LOOKBACK_DAYS日分の日別views(集計が確定したバケットのみ)を反映する。
    source="manual" の既存行は上書きしない。

    以前は「本日00:00〜24:00(JST)」の1リクエストでviewsも取得していたが、ジョブの実行は毎朝3:30のため、
    取得できるのは太平洋時間の1日が約半分過ぎた時点の途中値になり、日別の値が実際より大幅に小さく
    (かつ日によって割合がばらばらに)記録されていた。"""
    user_id, token = _get_credentials(app, account_id)
    if not token:
        return {"ok": False, "error": "Threadsアクセストークン未設定"}

    today = datetime.now(_JST).date()
    result = {"ok": True}

    # フォロワー数: lifetime指標なので期間指定なしで現在値を取得する
    followers_count = None
    try:
        resp = requests.get(
            f"{THREADS_API}/{user_id}/threads_insights",
            params={"metric": "followers_count", "access_token": token},
            timeout=15,
        )
        if resp.ok:
            followers_count, _ = _parse_account_insights(resp.json())
        else:
            logger.warning("Account insights(followers) HTTP %d: %s", resp.status_code, resp.text[:200])
            result.update(ok=False, error=resp.text[:200])
    except Exception as exc:
        logger.error("Account insights(followers) fetch error: %s", exc)
        result.update(ok=False, error=str(exc))

    if followers_count is not None:
        with app.app_context():
            row = DailyStat.query.filter_by(account_id=account_id, stat_date=today).first()
            if row and row.source == "manual":
                logger.info("daily_stats %s は手動入力済みのためAPI値で上書きしない", today)
            elif row:
                row.followers_count = followers_count
            else:
                db.session.add(DailyStat(account_id=account_id, stat_date=today,
                                         followers_count=followers_count, views_count=None, source="api"))
            db.session.commit()
        result["followers_count"] = followers_count

    # 日別views: 確定済みバケットを直近数日分取り直す
    try:
        result["views_sync"] = sync_daily_views(
            app, account_id, today - timedelta(days=SNAPSHOT_LOOKBACK_DAYS), user_id, token)
    except Exception as exc:
        logger.error("Account insights(views) sync error: %s", exc)
        result.update(ok=False, error=str(exc)[:200])

    logger.info("日次スナップショット完了: %s", result)
    return result
