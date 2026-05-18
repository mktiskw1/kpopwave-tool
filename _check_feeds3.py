import feedparser

candidates = [
    ("Hellokpop",  "https://www.hellokpop.com/feed/"),
    ("KpopPost",   "https://kpoppost.com/feed/"),
    ("KpopMap2",   "https://www.kpopmap.com/feed/?post_type=post"),
    ("Soompi K",   "https://www.soompi.com/feed/?category=kpop"),
    ("Wkorea",     "https://www.wkorea.com/feed/"),
    ("KpopHerald2","https://www.koreaherald.com/rss/020100000000.xml"),
]
for name, url in candidates:
    try:
        p = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 KpopWaveBot/1.0"})
        status = p.get("status", "?")
        n = len(p.entries)
        title = p.entries[0].get("title", "")[:55] if n else "(none)"
        pub   = getattr(p.entries[0], "published", "") if n else ""
        print(f"{'OK' if n>0 else 'NG'} [{name}] HTTP {status}, {n} entries")
        if n: print(f"   {title} | {pub[:16]}")
    except Exception as e:
        print(f"NG [{name}]: {e}")
