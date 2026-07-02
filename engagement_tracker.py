import logging
import os
import time
from datetime import datetime, timezone

import requests
from sqlalchemy import or_

from database import Article, Setting, get_active_account, db

logger = logging.getLogger(__name__)

THREADS_API = "https://graph.threads.net/v1.0"


def _get_token(app, account_id: int = None) -> tuple:
    """(token, account_id) を返す。account_id はトークン解決に使ったアカウントのid（記事フィルタに使用）。"""
    account = get_active_account(app, account_id)
    if account:
        token = account["threads_access_token"] or os.getenv("THREADS_ACCESS_TOKEN", "")
        return token, account["id"]
    # フォールバック: アカウント未登録時（マイグレーション前など）は settings を直接参照
    with app.app_context():
        token = Setting.get("threads_access_token") or os.getenv("THREADS_ACCESS_TOKEN", "")
    return token, None


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


def refresh_engagement(app, account_id: int = None) -> dict:
    """posted 状態の全記事のエンゲージメントを Threads Insights API で更新する。

    account_id 省略時はアクティブアカウント。該当アカウントの記事のみ対象とする
    （account_id が未割当のレコードも従来互換のため含める）。
    """
    token, resolved_account_id = _get_token(app, account_id)
    if not token:
        return {"error": "Threadsアクセストークン未設定", "updated": 0, "total": 0}

    # account_id 未割当の記事は「最古のアクティブアカウント（レガシー）」の対象範囲としてのみ含める
    legacy_account = get_active_account(app)
    is_legacy = bool(legacy_account and resolved_account_id == legacy_account["id"])

    with app.app_context():
        query = (
            Article.query
            .filter_by(status="posted")
            .filter(Article.threads_post_id.isnot(None))
            .filter(Article.threads_post_id != "")
        )
        if resolved_account_id is not None:
            if is_legacy:
                query = query.filter(
                    or_(Article.account_id == resolved_account_id, Article.account_id.is_(None))
                )
            else:
                query = query.filter(Article.account_id == resolved_account_id)
        rows = query.with_entities(Article.id, Article.threads_post_id).all()

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
