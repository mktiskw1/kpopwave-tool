import json
import logging
import os
import time
from datetime import datetime

import requests

from database import Article, Setting, db  # Setting は動画URL生成に使用

logger = logging.getLogger(__name__)

THREADS_API = "https://graph.threads.net/v1.0"
# カルーセル1投稿あたりの最大画像枚数（Threads APIの上限は20）
MAX_CAROUSEL_IMAGES = 20

# 投稿画像から除外するドメイン（Googleデフォルト画像・プロフィール写真など）
_EXCLUDE_DOMAINS = (
    "gstatic.com",
    "news.google.com",
    "googleusercontent.com",
    "lh3.google.com",
)

# URLに含まれるサイズヒントから64px以下の小画像を検出するパターン
# Google profile: =s64, =s64-c, /s64/, /s64-c/  一般: 64x64, _64., -64.
_SMALL_SIZE_HINTS = (
    "=s16", "=s24", "=s32", "=s48", "=s64",
    "/s16/", "/s24/", "/s32/", "/s48/", "/s64/",
    "/s16-", "/s24-", "/s32-", "/s48-", "/s64-",
    "16x16", "24x24", "32x32", "48x48", "64x64",
)


def _is_valid_image_url(url: str) -> bool:
    """投稿に使用可能な画像URLか判定する。"""
    if not url or not url.startswith("http"):
        return False
    if any(d in url for d in _EXCLUDE_DOMAINS):
        return False
    low = url.lower()
    if any(h in low for h in _SMALL_SIZE_HINTS):
        return False
    return True


def _get_credentials(app):
    with app.app_context():
        user_id = Setting.get("threads_user_id") or os.getenv("THREADS_USER_ID", "")
        token = Setting.get("threads_access_token") or os.getenv("THREADS_ACCESS_TOKEN", "")
    return user_id, token


def _mark_posted(app, article_id: int, post_id: str):
    with app.app_context():
        art = Article.query.get(article_id)
        if art:
            art.status = "posted"
            art.posted_at = datetime.utcnow()
            art.threads_post_id = post_id
            db.session.commit()


def _mark_failed(app, article_id: int, error: str):
    with app.app_context():
        art = Article.query.get(article_id)
        if art:
            art.status = "failed"
            art.error_message = error
            db.session.commit()


def _publish(user_id: str, token: str, container_id: str) -> tuple[bool, str]:
    """作成済みコンテナを公開する。(success, post_id_or_error) を返す。"""
    res = requests.post(
        f"{THREADS_API}/{user_id}/threads_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    data = res.json()
    logger.info("Publish: HTTP %d %s", res.status_code, data)
    if res.status_code != 200:
        err = data.get("error", {}).get("message", res.text[:200])
        return False, f"公開失敗: {err}"
    return True, data.get("id", "")


def _post_video(user_id: str, token: str, post_text: str, video_url: str, article_id: int, app) -> tuple[bool, str]:
    """動画投稿。コンテナ作成→処理待ち→公開。失敗時はテキストにフォールバック。"""
    res = requests.post(
        f"{THREADS_API}/{user_id}/threads",
        data={
            "media_type": "VIDEO",
            "video_url": video_url,
            "text": post_text,
            "access_token": token,
        },
        timeout=30,
    )
    data = res.json()
    logger.info("Container (VIDEO): HTTP %d %s", res.status_code, data)
    if res.status_code != 200:
        err = data.get("error", {}).get("message", res.text[:200])
        logger.warning("動画コンテナ作成失敗、テキスト投稿にフォールバック: %s", err)
        return _post_text_only(user_id, token, post_text, article_id, app)

    container_id = data.get("id")
    if not container_id:
        logger.warning("コンテナIDなし、テキスト投稿にフォールバック")
        return _post_text_only(user_id, token, post_text, article_id, app)

    # 動画処理完了を最大150秒ポーリング
    logger.info("動画処理待ち: container_id=%s", container_id)
    for attempt in range(30):
        time.sleep(5)
        st_res = requests.get(
            f"{THREADS_API}/{container_id}",
            params={"fields": "status,error_message", "access_token": token},
            timeout=15,
        )
        if st_res.status_code != 200:
            continue
        st = st_res.json()
        status = st.get("status", "")
        logger.info("動画処理状態 [%d/30]: %s", attempt + 1, status)
        if status == "FINISHED":
            break
        if status == "ERROR":
            err_msg = st.get("error_message", "動画処理エラー")
            logger.warning("動画処理エラー、テキスト投稿にフォールバック: %s", err_msg)
            return _post_text_only(user_id, token, post_text, article_id, app)
    else:
        logger.warning("動画処理タイムアウト、テキスト投稿にフォールバック")
        return _post_text_only(user_id, token, post_text, article_id, app)

    ok, result = _publish(user_id, token, container_id)
    if ok:
        _mark_posted(app, article_id, result)
        return True, f"投稿成功 (VIDEO, ID: {result})"
    _mark_failed(app, article_id, result)
    return False, result


def _post_text_only(user_id: str, token: str, post_text: str, article_id: int, app) -> tuple[bool, str]:
    """テキストのみ投稿。"""
    res = requests.post(
        f"{THREADS_API}/{user_id}/threads",
        data={"media_type": "TEXT", "text": post_text, "access_token": token},
        timeout=30,
    )
    data = res.json()
    logger.info("Container (TEXT): HTTP %d %s", res.status_code, data)
    if res.status_code != 200:
        err = data.get("error", {}).get("message", res.text[:200])
        return False, f"コンテナ作成失敗: {err}"

    ok, result = _publish(user_id, token, data["id"])
    if ok:
        _mark_posted(app, article_id, result)
        return True, f"投稿成功 (ID: {result})"
    _mark_failed(app, article_id, result)
    return False, result


def _post_single_image(user_id: str, token: str, post_text: str, image_url: str, article_id: int, app) -> tuple[bool, str]:
    """1枚画像投稿。失敗時はテキストにフォールバック。"""
    res = requests.post(
        f"{THREADS_API}/{user_id}/threads",
        data={
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": post_text,
            "access_token": token,
        },
        timeout=30,
    )
    data = res.json()
    logger.info("Container (IMAGE): HTTP %d %s", res.status_code, data)
    if res.status_code != 200:
        logger.warning("画像投稿失敗、テキスト投稿にフォールバック: %s", data.get("error", {}).get("message", ""))
        return _post_text_only(user_id, token, post_text, article_id, app)

    ok, result = _publish(user_id, token, data["id"])
    if ok:
        _mark_posted(app, article_id, result)
        return True, f"投稿成功 (IMAGE, ID: {result})"
    _mark_failed(app, article_id, result)
    return False, result


def _post_carousel(user_id: str, token: str, post_text: str, images: list, article_id: int, app) -> tuple[bool, str]:
    """複数画像をCarouselとして投稿。失敗時は1枚画像→テキストにフォールバック。"""
    # Step 1: カルーセルアイテムコンテナを画像ごとに作成
    child_ids = []
    for img_url in images[:MAX_CAROUSEL_IMAGES]:
        res = requests.post(
            f"{THREADS_API}/{user_id}/threads",
            data={
                "media_type": "IMAGE",
                "image_url": img_url,
                "is_carousel_item": "true",
                "access_token": token,
            },
            timeout=30,
        )
        data = res.json()
        if res.status_code == 200 and data.get("id"):
            child_ids.append(data["id"])
            logger.info("Carousel item created: %s", data["id"])
        else:
            logger.warning("Carousel item失敗 (スキップ): %s", data.get("error", {}).get("message", ""))

    if len(child_ids) < 2:
        # 有効な画像が2枚未満 → 1枚投稿かテキストにフォールバック
        logger.warning("Carousel item不足 (%d件) → フォールバック", len(child_ids))
        fallback_url = images[0] if images else ""
        if fallback_url and child_ids:
            return _post_single_image(user_id, token, post_text, fallback_url, article_id, app)
        return _post_text_only(user_id, token, post_text, article_id, app)

    # Threads API はカルーセルコンテナ作成前に少し待つと安定する
    time.sleep(1)

    # Step 2: カルーセルコンテナ作成
    res = requests.post(
        f"{THREADS_API}/{user_id}/threads",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "text": post_text,
            "access_token": token,
        },
        timeout=30,
    )
    data = res.json()
    logger.info("Container (CAROUSEL): HTTP %d %s", res.status_code, data)
    if res.status_code != 200:
        err = data.get("error", {}).get("message", res.text[:200])
        logger.warning("カルーセルコンテナ作成失敗、1枚投稿にフォールバック: %s", err)
        return _post_single_image(user_id, token, post_text, images[0], article_id, app)

    # Step 3: 公開
    ok, result = _publish(user_id, token, data["id"])
    if ok:
        _mark_posted(app, article_id, result)
        return True, f"投稿成功 (CAROUSEL {len(child_ids)}枚, ID: {result})"
    _mark_failed(app, article_id, result)
    return False, result


def post_to_threads(app, article_id: int, test_mode: bool = False) -> tuple[bool, str]:
    """Threads に記事を投稿する。(success: bool, message: str) を返す。"""
    with app.app_context():
        article = Article.query.get(article_id)
        if not article:
            return False, "記事が見つかりません"

        # 冪等ガード: 既に投稿済み or 想定外ステータスなら即リターン
        if article.status == "posted":
            logger.warning("[post_to_threads] id=%d は既にposted → スキップ（二重投稿防止）", article_id)
            return False, "既に投稿済みです"
        if article.status not in ("queued", "posting"):
            logger.warning(
                "[post_to_threads] id=%d status=%r → 投稿不可ステータスのためスキップ",
                article_id, article.status,
            )
            return False, f"投稿不可のステータス: {article.status}"

        if not article.summary:
            return False, "要約がありません。先に要約を生成してください"
        post_text      = article.summary
        thumbnail_url  = article.thumbnail_url or ""
        content_type   = (getattr(article, "content_type", None) or "article")
        video_file_path = getattr(article, "video_file_path", None)

        # thumbnail を1枚目、image_urls を全て追加（除外ドメイン・小画像・重複を除く）
        images: list = []
        if _is_valid_image_url(thumbnail_url):
            images.append(thumbnail_url)
        elif thumbnail_url:
            logger.info("サムネイル除外 (除外ドメインまたは小画像): %s", thumbnail_url)
        raw_image_urls = getattr(article, "image_urls", None)
        if raw_image_urls:
            try:
                parsed = json.loads(raw_image_urls)
                for url in parsed:
                    if _is_valid_image_url(url) and url not in images:
                        images.append(url)
                        if len(images) >= MAX_CAROUSEL_IMAGES:
                            break
            except (json.JSONDecodeError, TypeError):
                pass

    logger.info(
        "Threads投稿準備 article=%d content_type=%s images=%d test=%s",
        article_id, content_type, len(images), test_mode,
    )

    # ── テストモード ──────────────────────────────────────────────
    if test_mode:
        logger.info("[TEST] Post text:\n%s", post_text)
        if content_type == "video":
            logger.info("[TEST] video_file_path: %s", video_file_path)
        else:
            logger.info("[TEST] images: %s", images)
        with app.app_context():
            art = Article.query.get(article_id)
            if art:
                art.status = "posted"
                art.posted_at = datetime.utcnow()
                art.threads_post_id = f"test_{article_id}"
                db.session.commit()
        mode_label = "VIDEO" if content_type == "video" else f"{len(images)}枚"
        return True, f"テストモード: 投稿シミュレーション成功 ({mode_label})"

    # ── 実投稿 ───────────────────────────────────────────────────
    user_id, token = _get_credentials(app)
    if not user_id or not token:
        return False, "Threads の認証情報が設定されていません"

    try:
        # 動画投稿
        if content_type == "video" and video_file_path:
            with app.app_context():
                base_url = Setting.get("app_base_url", "http://localhost:5000").rstrip("/")
            video_url = f"{base_url}/static/{video_file_path}"
            logger.info("動画URL: %s", video_url)
            return _post_video(user_id, token, post_text, video_url, article_id, app)

        # 記事投稿（画像なし・1枚・複数）
        if len(images) >= 2:
            return _post_carousel(user_id, token, post_text, images, article_id, app)
        elif len(images) == 1:
            return _post_single_image(user_id, token, post_text, images[0], article_id, app)
        else:
            return _post_text_only(user_id, token, post_text, article_id, app)

    except Exception as exc:
        logger.error("Post error for article %d: %s", article_id, exc)
        _mark_failed(app, article_id, str(exc))
        return False, str(exc)
