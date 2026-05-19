import logging
import os
import time
from datetime import datetime, timezone

import requests

from database import Article, Setting, db

logger = logging.getLogger(__name__)

THREADS_API = "https://graph.threads.net/v1.0"


def _get_token(app) -> str:
    with app.app_context():
        return Setting.get("threads_access_token") or os.getenv("THREADS_ACCESS_TOKEN", "")


def _fetch_insights(post_id: str, token: str) -> dict:
    """{metric_name: value} を返す。失敗時は {}"""
    try:
        resp = requests.get(
            f"{THREADS_API}/{post_id}/insights",
            params={"metric": "views,likes,replies,reposts,quotes", "access_token": token},
            timeout=15,
        )
        if not resp.ok:
            logger.warning("Insights HTTP %d [%s]: %s", resp.status_code, post_id, resp.text[:200])
            return {}
        result = {}
        for item in resp.json().get("data", []):
            name = item.get("name")
            values = item.get("values", [])
            if name and values:
                result[name] = values[0].get("value", 0)
        return result
    except Exception as exc:
        logger.error("Insights fetch error [%s]: %s", post_id, exc)
        return {}


def refresh_engagement(app) -> dict:
    """posted 状態の全記事のエンゲージメントを Threads Insights API で更新する。"""
    token = _get_token(app)
    if not token:
        return {"error": "Threadsアクセストークン未設定", "updated": 0, "total": 0}

    with app.app_context():
        rows = (
            Article.query
            .filter_by(status="posted")
            .filter(Article.threads_post_id.isnot(None))
            .filter(Article.threads_post_id != "")
            .with_entities(Article.id, Article.threads_post_id)
            .all()
        )

    updated = skipped = errors = 0

    for article_id, post_id in rows:
        if post_id.startswith("test_"):
            skipped += 1
            continue

        insights = _fetch_insights(post_id, token)
        if not insights:
            errors += 1
            continue

        with app.app_context():
            art = Article.query.get(article_id)
            if art:
                art.like_count = insights.get("likes", 0)
                art.reply_count = insights.get("replies", 0)
                art.repost_count = insights.get("reposts", 0)
                art.quote_count = insights.get("quotes", 0)
                art.engagement_fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.session.commit()
                updated += 1

        time.sleep(0.3)  # Threads API レート制限を考慮

    result = {"updated": updated, "skipped": skipped, "errors": errors, "total": len(rows)}
    logger.info("エンゲージメント取得完了: %s", result)
    return result
