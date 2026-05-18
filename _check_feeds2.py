import feedparser

candidates = [
    ("Hellokpop",    "https://www.hellokpop.com/feed/"),
    ("KpopPost",     "https://kpoppost.com/feed/"),
    ("Allkpop v2",  "https://www.allkpop.com/rss.xml"),
    ("Pop Crush",    "https://popcrush.com/feed/"),
    ("Pinkvilla",    "https://www.pinkvilla.com/feed/"),
]
for name, url in candidates:
    try:
        p = feedparser.parse(url, request_headers={"User-Agent": "KpopWaveBot/1.0"})
        status = p.get("status", "?")
        n = len(p.entries)
        title = p.entries[0].get("title", "")[:60] if p.entries else "(none)"
        print(f"{'OK' if n>0 else 'NG'} {name}: HTTP {status}, {n} entries | {title}")
    except Exception as e:
        print(f"NG {name}: {e}")
