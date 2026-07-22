import json
from app import app
from database import Setting, db

with app.app_context():
    feeds = json.loads(Setting.get("rss_feeds", "[]") or "[]")
    new_url = "https://hobby.dengeki.com/tag/syokugan/feed/"
    if not any(f.get("url") == new_url for f in feeds):
        feeds.append({
            "name": "電撃ホビーウェブ（食玩）",
            "url": new_url,
            "account_id": 2,
            "lang": "ja",
        })
        Setting.set("rss_feeds", json.dumps(feeds, ensure_ascii=False))
        print("added")
    else:
        print("already exists")

    for f in feeds:
        print(f)
