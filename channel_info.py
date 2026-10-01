"""YouTube動画のチャンネル情報(channel_id・channel_name)の取得・既存記事への補完。"""
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_VIDEO_ID_RE = re.compile(r"(?:youtube\.com/watch\?(?:[^#]*&)?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})")
BACKFILL_DONE_SETTING = "channel_backfill_done"


def extract_youtube_video_id(url: str) -> str:
    """YouTube動画URL(watch/youtu.be/shorts。#t=等の付加情報つきも可)から動画IDを返す。
    YouTube以外・判定不能なら空文字。"""
    m = _VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else ""


def fetch_channel_info(video_ids: list, api_key: str) -> dict:
    """videos.listを50件ずつ呼び {video_id: (channel_id, channel_name)} を返す。
    削除・非公開の動画は結果に含まれない。HTTPエラー等は例外を投げる(呼び出し側で判断する)。"""
    result = {}
    ids = list(dict.fromkeys(video_ids))
    for i in range(0, len(ids), 50):
        resp = requests.get(
            YOUTUBE_VIDEOS_URL,
            params={"part": "snippet", "id": ",".join(ids[i:i + 50]), "key": api_key, "maxResults": 50},
            timeout=20,
        )
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            sn = item.get("snippet", {})
            if sn.get("channelId"):
                result[item["id"]] = (sn["channelId"], (sn.get("channelTitle") or "")[:200])
    return result


def backfill_article_channels(api_key: str) -> dict:
    """channel_idが未設定のYouTube由来記事に、YouTube Data APIで取得したチャンネル情報を保存する。
    app contextの中で呼ぶこと。戻り値は集計(総数・更新数・API上に見つからなかった数・YouTube以外の数)。"""
    from database import Article, db

    rows = Article.query.filter(Article.channel_id.is_(None)).with_entities(Article.id, Article.url).all()
    id_by_article = {}
    non_youtube = 0
    for aid, url in rows:
        vid = extract_youtube_video_id(url)
        if vid:
            id_by_article[aid] = vid
        else:
            non_youtube += 1

    info = fetch_channel_info(list(id_by_article.values()), api_key) if id_by_article else {}

    updated = not_found = 0
    for aid, vid in id_by_article.items():
        if vid not in info:
            not_found += 1
            continue
        cid, cname = info[vid]
        db.session.execute(
            Article.__table__.update().where(Article.id == aid).values(channel_id=cid, channel_name=cname)
        )
        updated += 1
    db.session.commit()
    return {"target": len(rows), "youtube": len(id_by_article), "updated": updated,
            "not_found": not_found, "non_youtube": non_youtube}


def get_youtube_api_key() -> str:
    from database import Setting
    return Setting.get("youtube_api_key", "") or os.getenv("YOUTUBE_API_KEY", "")
