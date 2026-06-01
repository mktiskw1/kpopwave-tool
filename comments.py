import logging

import requests

from database import Comment, Setting, db

logger = logging.getLogger(__name__)

_BASE = "https://graph.threads.net/v1.0"
_FIELDS = "id,text,username,timestamp,has_replies,replied_to{id},root_post{id}"
_OWN_USERNAME = "kpopwave.daily"
_POSTS_TO_CHECK = 10    # 返信を確認する最近の投稿数
_REPLIES_PER_POST = 20  # 1投稿あたりの取得返信数


def _creds(app):
    with app.app_context():
        return Setting.get("threads_access_token"), Setting.get("threads_user_id")


def fetch_comments(app):
    """Threads API から自分の投稿への他者コメントを取得して DB に保存。"""
    token, user_id = _creds(app)
    if not token or not user_id:
        return {"error": "Threadsの認証情報が設定されていません", "fetched": 0, "new": 0}

    # Step 1: 自分の最近の投稿IDを取得
    try:
        resp = requests.get(
            f"{_BASE}/{user_id}/threads",
            params={"fields": "id", "limit": _POSTS_TO_CHECK, "access_token": token},
            timeout=30,
        )
        resp.raise_for_status()
        posts = resp.json().get("data", [])
    except Exception as e:
        logger.exception("投稿一覧取得APIエラー")
        return {"error": str(e), "fetched": 0, "new": 0}

    # Step 2: 各投稿の返信を取得し、自分のコメントを除外
    items_with_post: list[tuple[dict, str]] = []
    for post in posts:
        parent_post_id = post.get("id")
        if not parent_post_id:
            continue
        try:
            r = requests.get(
                f"{_BASE}/{parent_post_id}/replies",
                params={"fields": _FIELDS, "limit": _REPLIES_PER_POST, "access_token": token},
                timeout=30,
            )
            r.raise_for_status()
            for reply in r.json().get("data", []):
                if not reply.get("id"):
                    continue
                if reply.get("username") == _OWN_USERNAME:
                    continue
                items_with_post.append((reply, parent_post_id))
        except Exception as e:
            logger.warning("返信取得APIエラー post_id=%s: %s", parent_post_id, e)

    with app.app_context():
        new_count = 0
        for c, parent_post_id in items_with_post:
            cid = c.get("id")
            if not cid:
                continue

            # root_post → replied_to → 親投稿ID の順で post_id を解決
            root = c.get("root_post") or {}
            replied = c.get("replied_to") or {}
            post_id = (root.get("id") if isinstance(root, dict) else None) or \
                      (replied.get("id") if isinstance(replied, dict) else None) or \
                      parent_post_id

            existing = Comment.query.filter_by(id=cid).first()
            if existing:
                existing.username = c.get("username") or ""
                existing.text = c.get("text") or ""
                existing.timestamp = c.get("timestamp") or ""
                if post_id:
                    existing.post_id = post_id
            else:
                db.session.add(Comment(
                    id=cid,
                    post_id=post_id,
                    username=c.get("username") or "",
                    text=c.get("text") or "",
                    timestamp=c.get("timestamp") or "",
                    is_read=0,
                    is_replied=0,
                ))
                new_count += 1

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("コメントDB保存エラー")
            return {"error": "DB保存に失敗しました", "fetched": 0, "new": 0}

    total = len(items_with_post)
    logger.info("コメント取得: %d件取得 / %d件新規", total, new_count)
    return {"fetched": total, "new": new_count}


def like_comment(app, reply_id):
    """コメントにいいね。"""
    token, _ = _creds(app)
    if not token:
        return {"error": "Threadsの認証情報が設定されていません"}
    try:
        resp = requests.post(
            f"{_BASE}/{reply_id}/likes",
            params={"access_token": token},
            timeout=15,
        )
        resp.raise_for_status()
        return {"ok": True}
    except Exception as e:
        logger.error("いいねエラー reply_id=%s: %s", reply_id, e)
        return {"error": str(e)}


def post_reply(app, reply_id, text):
    """コメントに返信を投稿（create → publish の2ステップ）。"""
    token, user_id = _creds(app)
    if not token or not user_id:
        return {"error": "Threadsの認証情報が設定されていません"}
    try:
        # Step 1: コンテナ作成
        r1 = requests.post(
            f"{_BASE}/{user_id}/threads",
            params={
                "media_type": "TEXT",
                "text": text,
                "reply_to_id": reply_id,
                "access_token": token,
            },
            timeout=15,
        )
        r1.raise_for_status()
        container_id = r1.json().get("id")
        if not container_id:
            raise ValueError(f"コンテナID取得失敗: {r1.text}")

        # Step 2: 公開
        r2 = requests.post(
            f"{_BASE}/{user_id}/threads_publish",
            params={"creation_id": container_id, "access_token": token},
            timeout=15,
        )
        r2.raise_for_status()
        post_id = r2.json().get("id")
    except Exception as e:
        logger.error("返信投稿エラー reply_id=%s: %s", reply_id, e)
        return {"error": str(e)}

    # DB 更新
    with app.app_context():
        comment = Comment.query.filter_by(id=reply_id).first()
        if comment:
            comment.is_replied = 1
            comment.is_read = 1
            db.session.commit()

    logger.info("返信投稿成功: reply_id=%s post_id=%s", reply_id, post_id)
    return {"ok": True, "post_id": post_id}


def generate_ai_reply(app, reply_id):
    """Claude Haiku でコメントへの返信文を生成。"""
    with app.app_context():
        comment = Comment.query.filter_by(id=reply_id).first()
        if not comment:
            return {"error": "コメントが見つかりません"}
        comment_text = comment.text or ""
        anthropic_key = Setting.get("anthropic_api_key")

    if not anthropic_key:
        return {"error": "Anthropic APIキーが設定されていません"}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=(
                "あなたはKPOP女性アイドル情報を発信するThreadsアカウントの中の人です。\n"
                "ファンからのコメントに対して、フレンドリーで親しみやすい口語体（LINEっぽい感じ）で\n"
                "返信文を日本語で1〜3文生成してください。絵文字を1〜2個含めてください。\n"
                "翻訳っぽさは不要です。"
            ),
            messages=[{"role": "user", "content": f"ファンからのコメント:\n{comment_text}"}],
        )
        reply_text = response.content[0].text.strip()
    except Exception as e:
        logger.error("AI返信生成エラー: %s", e)
        return {"error": str(e)}

    with app.app_context():
        comment = Comment.query.filter_by(id=reply_id).first()
        if comment:
            comment.is_read = 1
            db.session.commit()

    logger.info("AI返信生成成功: reply_id=%s", reply_id)
    return {"ok": True, "text": reply_text}
