import logging
import os
from datetime import datetime

import requests

from database import Article, Setting, db

logger = logging.getLogger(__name__)

THREADS_API = "https://graph.threads.net/v1.0"


def _get_credentials(app):
    with app.app_context():
        user_id = Setting.get("threads_user_id") or os.getenv("THREADS_USER_ID", "")
        token = Setting.get("threads_access_token") or os.getenv("THREADS_ACCESS_TOKEN", "")
    return user_id, token


def post_to_threads(app, article_id: int, test_mode: bool = False) -> tuple[bool, str]:
    """Threads に記事を投稿する。(success: bool, message: str) を返す。"""
    with app.app_context():
        article = Article.query.get(article_id)
        if not article:
            return False, "記事が見つかりません"
        if not article.summary:
            return False, "要約がありません。先に要約を生成してください"
        post_text = article.summary

    # ---- テストモード ----
    if test_mode:
        logger.info("[TEST] Would post to Threads:\n%s", post_text)
        with app.app_context():
            art = Article.query.get(article_id)
            if art:
                art.status = "posted"
                art.posted_at = datetime.utcnow()
                art.threads_post_id = f"test_{article_id}"
                db.session.commit()
        return True, "テストモード: 投稿シミュレーション成功"

    # ---- 実投稿 ----
    user_id, token = _get_credentials(app)
    if not user_id or not token:
        return False, "Threads の認証情報が設定されていません"

    try:
        # Step 1: メディアコンテナ作成
        res = requests.post(
            f"{THREADS_API}/{user_id}/threads",
            data={"media_type": "TEXT", "text": post_text, "access_token": token},
            timeout=30,
        )
        data = res.json()
        logger.info("Container create: HTTP %d %s", res.status_code, data)
        if res.status_code != 200:
            err = data.get("error", {}).get("message", res.text)
            return False, f"コンテナ作成失敗: {err}"

        container_id = data.get("id")

        # Step 2: 公開
        res2 = requests.post(
            f"{THREADS_API}/{user_id}/threads_publish",
            data={"creation_id": container_id, "access_token": token},
            timeout=30,
        )
        data2 = res2.json()
        logger.info("Publish: HTTP %d %s", res2.status_code, data2)
        if res2.status_code != 200:
            err = data2.get("error", {}).get("message", res2.text)
            return False, f"公開失敗: {err}"

        post_id = data2.get("id", "")
        with app.app_context():
            art = Article.query.get(article_id)
            if art:
                art.status = "posted"
                art.posted_at = datetime.utcnow()
                art.threads_post_id = post_id
                db.session.commit()

        logger.info("Posted article %d → Threads post ID %s", article_id, post_id)
        return True, f"投稿成功 (ID: {post_id})"

    except Exception as exc:
        logger.error("Post error for article %d: %s", article_id, exc)
        with app.app_context():
            art = Article.query.get(article_id)
            if art:
                art.status = "failed"
                art.error_message = str(exc)
                db.session.commit()
        return False, str(exc)
