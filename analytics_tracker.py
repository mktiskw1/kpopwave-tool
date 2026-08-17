import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

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


def track_post_stats(app, account_id: int = 1) -> dict:
    """account_id の posted 記事のうち、7日確定値がまだ出ていないものを対象に
    Threads Media Insights を取得し post_stats に1行追加する。
    day_index >= 7 に達した回で is_final=True を立て、以後その記事は対象から外れる。"""
    _, token = _get_credentials(app, account_id)
    if not token:
        return {"error": "Threadsアクセストークン未設定", "updated": 0, "total": 0}

    with app.app_context():
        finalized_ids = {
            row[0] for row in
            db.session.query(PostStat.article_id).filter(PostStat.is_final.is_(True)).distinct()
        }
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
        targets = [row for row in candidates if row[0] not in finalized_ids]

    updated = errors = skipped = 0
    now = datetime.utcnow()

    for article_id, post_id, posted_at in targets:
        if post_id.startswith("test_"):
            skipped += 1
            continue

        insights = _fetch_media_insights(post_id, token)
        if not insights:
            errors += 1
            continue

        day_index = _compute_day_index(posted_at, now)
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


def snapshot_daily_stats(app, account_id: int = 1) -> dict:
    """account_id の User Insights から本日（JST）分の followers_count・views を取得し
    daily_stats に反映する。source="manual" の既存行は上書きしない。"""
    user_id, token = _get_credentials(app, account_id)
    if not token:
        return {"ok": False, "error": "Threadsアクセストークン未設定"}

    now_jst = datetime.now(_JST)
    today = now_jst.date()
    since_dt = datetime(today.year, today.month, today.day, 0, 0, tzinfo=_JST)
    until_dt = since_dt + timedelta(days=1)

    try:
        resp = requests.get(
            f"{THREADS_API}/{user_id}/threads_insights",
            params={
                "metric": "followers_count,views",
                "since": int(since_dt.timestamp()),
                "until": int(until_dt.timestamp()),
                "access_token": token,
            },
            timeout=15,
        )
    except Exception as exc:
        logger.error("Account insights fetch error: %s", exc)
        return {"ok": False, "error": str(exc)}

    if not resp.ok:
        logger.warning("Account insights HTTP %d: %s", resp.status_code, resp.text[:200])
        return {"ok": False, "error": resp.text[:200]}

    followers_count, views_count = _parse_account_insights(resp.json())

    with app.app_context():
        row = DailyStat.query.filter_by(account_id=account_id, stat_date=today).first()
        if row and row.source == "manual":
            logger.info("daily_stats %s は手動入力済みのためAPI値で上書きしない", today)
            return {"ok": True, "skipped": "manual_exists"}
        if row:
            row.followers_count = followers_count
            row.views_count = views_count
        else:
            db.session.add(DailyStat(
                account_id=account_id, stat_date=today,
                followers_count=followers_count, views_count=views_count, source="api",
            ))
        db.session.commit()

    result = {"ok": True, "followers_count": followers_count, "views_count": views_count}
    logger.info("日次スナップショット完了: %s", result)
    return result
