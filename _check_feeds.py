import feedparser

feeds = [
    ("AllKPOP",     "https://www.allkpop.com/feed/"),
    ("Kpopmap",     "https://www.kpopmap.com/feed/"),
    ("Kpop Herald", "https://www.koreaherald.com/rss/020100000000.xml"),
]
for name, url in feeds:
    p = feedparser.parse(url)
    status = p.get("status", "?")
    entries = len(p.entries)
    print(f"{name}: HTTP {status}, {entries} entries, bozo={p.bozo}")
    if p.entries:
        e = p.entries[0]
        print(f"  title: {e.get('title','')[:70]}")
        print(f"  link:  {e.get('link','')[:70]}")
        print(f"  pub:   {getattr(e, 'published', '(none)')}")
    else:
        print("  (エントリなし)")
