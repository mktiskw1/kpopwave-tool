import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from database import Article, Setting, db

logger = logging.getLogger(__name__)

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"

DAYS_LIMIT = 60
MAX_RESULTS_PER_QUERY = 10
# 再生数フィルタ: 0=無効。通常運用時は 10_000 以上を推奨
MIN_VIEW_COUNT = 5000000

# KPOPアーティスト以外の洋楽アーティスト明示ブロックリスト（チャンネル名・タイトル小文字一致）
# 検索クエリのアーティスト名と部分一致してしまうケースを手動で除外する
NON_KPOP_BLOCKLIST = frozenset([
    "tyla",
    "allison russell",
    "kara leona",
    "kara kay",
    "kara winger",
])

# タイトルに含まれていたら除外（リアクション・カバー・ファンメイド・ショート）
EXCLUDE_TITLE_KEYWORDS = frozenset([
    "reaction", "react", "cover", "fan made", "fanmade",
    "#shorts", "shorts", "short video",
])

# まとめ・ランキング系動画を除外するキーワード
EXCLUDE_COMPILATION_KEYWORDS = frozenset([
    "ランキング", "まとめ", "最高", "桁違い",
    "ranking", "compilation", "best of",
    "top 10", "top10", "top 5", "top5", "top 3", "top3",
    " top ",  # "top" 単体（topline 等の誤検出を防ぐため前後にスペース）
])

# タイトルにこれらのいずれかが含まれる動画のみ対象（MV・ティザー・パフォーマンス・ライブ）
TARGET_TITLE_KEYWORDS = frozenset([
    "mv", "m/v", "music video", "musicvideo",
    "teaser",
    "performance",
    "live",
    "official video", "official mv",
    "showcase", "concert",
])

# 検索クエリ — 1クエリ100ユニット消費。デフォルト6h間隔×4回/日 = 6,400ユニット/日 (上限10,000)
SEARCH_QUERIES = [
    "BLACKPINK MV",
    "NewJeans MV",
    "aespa MV",
    "IVE kpop MV",
    "LE SSERAFIM MV",
    "TWICE MV",
    "ITZY MV",
    "NMIXX kpop MV",
    "ILLIT kpop MV",
    "BABYMONSTER MV",
    "STAYC kpop MV",
    "Kep1er kpop MV",
    "fromis_9 MV",
    "KISS OF LIFE MV",
    "MAMAMOO MV",
    "Red Velvet MV",
    "Girls Generation MV",
    "Apink MV",
    "WJSN MV",
    "KARA MV",
    "IZNA MV",
    "KiiiKiii MV",
    "ARTMS MV",
    "Billlie MV",
    "tripleS MV",
    "UNIS MV",
    "MEOVV MV",
    "NiziU MV",
]


def _extract_artist_from_query(query: str) -> str:
    """検索クエリからアーティスト名部分を抽出する。
    例: 'IVE kpop MV' → 'IVE' / 'LE SSERAFIM MV' → 'LE SSERAFIM'
    """
    stop_suffixes = {"mv", "m/v", "kpop", "k-pop"}
    tokens = query.strip().split()
    while tokens and tokens[-1].lower() in stop_suffixes:
        tokens.pop()
    return " ".join(tokens)


def _matches_target_artist(title: str, channel_title: str, artist_name: str) -> bool:
    """タイトルまたはチャンネル名に指定アーティスト名が含まれるか確認。
    単語境界マッチングにより 'IVE' が 'live' に誤検出されるのを防ぐ。
    NON_KPOP_BLOCKLIST に一致する場合は False を返す。
    """
    combined = (title + " " + channel_title).lower()

    # 明示ブロックリスト: KPOPと誤検出しやすい洋楽アーティストを先に除外
    for blocked in NON_KPOP_BLOCKLIST:
        if blocked in combined:
            return False

    # アーティスト名が title または channel_title に（単語として）含まれるか確認
    name_lower = artist_name.lower()
    pattern = r'(?<![a-zA-Z0-9_])' + re.escape(name_lower) + r'(?![a-zA-Z0-9_])'
    return bool(re.search(pattern, combined))


def _best_thumbnail(thumbnails: dict) -> str:
    for quality in ("maxres", "standard", "high", "medium", "default"):
        t = thumbnails.get(quality)
        if t and t.get("url"):
            return t["url"]
    return ""


def _is_excluded(title: str) -> bool:
    t = title.lower()
    return (
        any(kw in t for kw in EXCLUDE_TITLE_KEYWORDS)
        or any(kw in t for kw in EXCLUDE_COMPILATION_KEYWORDS)
    )


def _is_target_type(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in TARGET_TITLE_KEYWORDS)


def _fetch_video_statistics(video_ids: list, api_key: str) -> dict:
    """動画IDのリストから再生数を一括取得する。{video_id: {"viewCount": int, "channelId": str}}"""
    result = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        try:
            resp = requests.get(
                YOUTUBE_VIDEOS_URL,
                params={
                    "part": "statistics,snippet",
                    "id": ",".join(batch),
                    "key": api_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                vid = item["id"]
                try:
                    view_count = int(item.get("statistics", {}).get("viewCount", 0))
                except (ValueError, TypeError):
                    view_count = 0
                result[vid] = {
                    "viewCount": view_count,
                    "channelId": item.get("snippet", {}).get("channelId", ""),
                }
        except Exception as exc:
            logger.error("YouTube動画統計取得エラー: %s", exc)
    return result


def _fetch_channel_subscriber_counts(channel_ids: list, api_key: str) -> dict:
    """チャンネルIDのリストからチャンネル登録者数を一括取得する。{channel_id: int}"""
    counts = {}
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i : i + 50]
        try:
            resp = requests.get(
                YOUTUBE_CHANNELS_URL,
                params={
                    "part": "statistics",
                    "id": ",".join(batch),
                    "key": api_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                try:
                    counts[item["id"]] = int(
                        item.get("statistics", {}).get("subscriberCount", 0)
                    )
                except (ValueError, TypeError):
                    counts[item["id"]] = 0
        except Exception as exc:
            logger.error("YouTubeチャンネル情報取得エラー: %s", exc)
    return counts


def collect_youtube_videos(app) -> int:
    """YouTube Data API v3 で女性KPOPグループのMV/動画を収集してDBに保存する。新規追加数を返す。"""
    with app.app_context():
        db_key = Setting.get("youtube_api_key", "")
        env_key = os.getenv("YOUTUBE_API_KEY", "")
        api_key = db_key or env_key
        try:
            min_view_count = int(Setting.get("youtube_min_view_count", str(MIN_VIEW_COUNT)))
        except (ValueError, TypeError):
            min_view_count = MIN_VIEW_COUNT
        try:
            max_view_count = int(Setting.get("youtube_max_view_count", "0"))
        except (ValueError, TypeError):
            max_view_count = 0

    logger.info(
        "YouTube APIキー — DB: %s / ENV: %s",
        f"{db_key[:8]}...({len(db_key)}文字)" if db_key else "未設定",
        f"{env_key[:8]}...({len(env_key)}文字)" if env_key else "未設定",
    )

    if not api_key:
        logger.warning("YouTube APIキー未設定 — YouTube収集をスキップ")
        return 0

    published_after = (
        datetime.now(timezone.utc) - timedelta(days=DAYS_LIMIT)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("YouTube検索範囲: %s 以降 (直近%d日)", published_after, DAYS_LIMIT)

    # Phase 1: 全クエリの検索結果を収集 & タイトルフィルタ適用
    # DBに既存の動画IDを先読みして重複収集を防止
    with app.app_context():
        existing_video_ids = {
            url.replace("https://www.youtube.com/watch?v=", "")
            for url in (
                a.url for a in Article.query.filter(
                    Article.url.like("https://www.youtube.com/watch?v=%")
                ).all()
            )
        }
    logger.info("DB既存YouTube動画ID: %d件", len(existing_video_ids))

    candidates = []
    seen_ids: set = existing_video_ids  # DB既存IDで初期化して重複収集を防止
    excluded_count = 0
    non_target_count = 0
    non_kpop_count = 0
    already_seen_count = 0
    first_query = True

    for query in SEARCH_QUERIES:
        artist_name = _extract_artist_from_query(query)
        try:
            resp = requests.get(
                YOUTUBE_SEARCH_URL,
                params={
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "videoCategoryId": "10",
                    "order": "relevance",
                    "publishedAfter": published_after,
                    "maxResults": MAX_RESULTS_PER_QUERY,
                    "key": api_key,
                },
                timeout=15,
            )
            logger.info("YouTube検索 [%s]: HTTP %d", query, resp.status_code)
            if not resp.ok:
                logger.error(
                    "YouTube検索 HTTPエラー [%s]: %d — %s",
                    query, resp.status_code, resp.text[:500],
                )
                resp.raise_for_status()
            data = resp.json()
            # 最初のクエリのみレスポンス構造をデバッグ出力
            if first_query:
                first_query = False
                logger.info(
                    "YouTube APIレスポンス構造 (1クエリ目): keys=%s / totalResults=%s / items=%d件",
                    list(data.keys()),
                    data.get("pageInfo", {}).get("totalResults", "N/A"),
                    len(data.get("items", [])),
                )
            items = data.get("items", [])
        except requests.HTTPError:
            continue
        except Exception as exc:
            logger.error("YouTube検索エラー [%s]: %s", query, exc)
            continue

        query_added = 0
        query_seen = 0
        for item in items:
            video_id = item.get("id", {}).get("videoId")
            if not video_id:
                continue
            if video_id in seen_ids:
                already_seen_count += 1
                query_seen += 1
                continue

            snippet = item.get("snippet", {})
            title = (snippet.get("title") or "").strip()
            channel_title = (snippet.get("channelTitle") or "").strip()

            if _is_excluded(title):
                logger.info("  除外(リアクション/カバー等): %s", title[:70])
                excluded_count += 1
                continue

            if not _is_target_type(title):
                logger.info("  除外(対象外タイプ): %s", title[:70])
                non_target_count += 1
                continue

            # KPOPアーティスト名チェック: クエリのアーティスト名がタイトルまたは
            # チャンネル名に含まれない動画は除外（洋楽アーティストの混入を防止）
            if not _matches_target_artist(title, channel_title, artist_name):
                logger.info(
                    "  除外(KPOPアーティスト外): query='%s' / ch='%s' / %s",
                    artist_name, channel_title, title[:60],
                )
                non_kpop_count += 1
                continue

            seen_ids.add(video_id)
            query_added += 1
            candidates.append({
                "video_id": video_id,
                "title": title,
                "description": (snippet.get("description") or "").strip(),
                "channel_id": snippet.get("channelId", ""),
                "channel_title": channel_title,
                "published_at_str": snippet.get("publishedAt", ""),
                "thumbnail_url": _best_thumbnail(snippet.get("thumbnails", {})),
            })

        logger.info(
            "  [%s] → 候補追加: %d件 / DB既存スキップ: %d件 / 検索結果: %d件",
            query, query_added, query_seen, len(items),
        )

    logger.info(
        "YouTube検索完了 — 候補: %d件 / DB既存スキップ: %d件 / 除外(リアクション等): %d件"
        " / 除外(対象外タイプ): %d件 / 除外(KPOP外アーティスト): %d件",
        len(candidates), already_seen_count, excluded_count, non_target_count, non_kpop_count,
    )

    if not candidates:
        return 0

    # Phase 2: 再生数取得（videos.list で一括取得）& フィルタ
    # 検索は order=date (最新順) のため新着MVは再生数が少ない場合がある
    video_ids = [c["video_id"] for c in candidates]
    video_stats = _fetch_video_statistics(video_ids, api_key)

    # 再生数を多い順に全件 INFO ログ出力（デバッグ用）
    sorted_for_log = sorted(
        candidates,
        key=lambda c: video_stats.get(c["video_id"], {}).get("viewCount", 0),
        reverse=True,
    )
    logger.info("=== 再生数確認 (order=date, 直近%d日以内の新着) ===", DAYS_LIMIT)
    for c in sorted_for_log:
        vc = video_stats.get(c["video_id"], {}).get("viewCount", 0)
        pub = c["published_at_str"][:10] if c["published_at_str"] else "?"
        logger.info(
            "  %s views | %s | ch:%s | pub:%s",
            f"{vc:,}", c["title"][:55], c["channel_title"][:25], pub,
        )

    before_view_filter = len(candidates)
    passed = candidates
    if min_view_count > 0:
        passed = [c for c in passed if video_stats.get(c["video_id"], {}).get("viewCount", 0) >= min_view_count]
    if max_view_count > 0:
        passed = [c for c in passed if video_stats.get(c["video_id"], {}).get("viewCount", 0) <= max_view_count]

    if min_view_count > 0 or max_view_count > 0:
        filter_parts = []
        if min_view_count > 0:
            filter_parts.append(f"{min_view_count:,}以上")
        if max_view_count > 0:
            filter_parts.append(f"{max_view_count:,}以下")
        logger.info(
            "再生数フィルタ(%s) — 通過: %d件 / 除外: %d件",
            " / ".join(filter_parts), len(passed), before_view_filter - len(passed),
        )
        if not passed:
            logger.warning("再生数フィルタ後0件 — 管理画面の再生数設定を確認してください")
            return 0
        candidates = passed
    else:
        logger.info("再生数フィルタ: 無効 (最低・最高ともに0) — 全%d件を対象", len(candidates))

    # Phase 3: チャンネル登録者数を取得して公式チャンネル優先でソート
    channel_ids = list({c["channel_id"] for c in candidates if c["channel_id"]})
    sub_counts = _fetch_channel_subscriber_counts(channel_ids, api_key)
    candidates.sort(key=lambda c: sub_counts.get(c["channel_id"], 0), reverse=True)

    # Phase 4: DB保存
    new_count = 0
    with app.app_context():
        for c in candidates:
            video_url = f"https://www.youtube.com/watch?v={c['video_id']}"

            if Article.query.filter_by(url=video_url).first():
                continue

            try:
                published_at = (
                    datetime.strptime(c["published_at_str"], "%Y-%m-%dT%H:%M:%SZ")
                    if c["published_at_str"]
                    else None
                )
            except ValueError:
                published_at = None

            sub_count = sub_counts.get(c["channel_id"], 0)
            view_count = video_stats.get(c["video_id"], {}).get("viewCount", 0)
            channel_label = c["channel_title"] or "YouTube"
            # 登録者100万以上は公式チャンネルとしてタグ付け
            if sub_count >= 1_000_000:
                channel_label += " [公式]"

            article = Article(
                feed_source=f"YouTube: {channel_label}",
                title=c["title"][:500] or "No Title",
                url=video_url,
                published_at=published_at,
                raw_content=c["description"][:5000],
                thumbnail_url=c["thumbnail_url"],
                status="pending",
            )
            db.session.add(article)
            new_count += 1
            logger.info(
                "  DB追加: %s views | %s",
                f"{view_count:,}", c["title"][:65],
            )

        db.session.commit()

    logger.info("YouTube収集完了 — 新規追加: %d 件", new_count)
    return new_count
