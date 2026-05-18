import json, sqlite3, pathlib

feeds = [
    {"name": "Soompi",      "url": "https://www.soompi.com/feed/"},
    {"name": "Koreaboo",    "url": "https://www.koreaboo.com/feed/"},
    {"name": "Hellokpop",   "url": "https://www.hellokpop.com/feed/"},
    {"name": "KpopPost",    "url": "https://kpoppost.com/feed/"},
    {"name": "NME K-Pop",   "url": "https://www.nme.com/tag/k-pop/feed"},
    {"name": "AsianJunkie", "url": "https://www.asianjunkie.com/feed/"},
    {"name": "TheBiasList", "url": "https://thebiaslist.com/feed/"},
    {"name": "KpopReviewed","url": "https://kpopreviewed.com/feed/"},
    {"name": "SeoulBeats",  "url": "https://seoulbeats.com/feed/"},
]

db = pathlib.Path(__file__).parent / "instance" / "rock_metal.db"
con = sqlite3.connect(db)
val = json.dumps(feeds, ensure_ascii=False)
row = con.execute("SELECT id FROM settings WHERE key='rss_feeds'").fetchone()
if row:
    con.execute("UPDATE settings SET value=? WHERE key='rss_feeds'", (val,))
else:
    con.execute("INSERT INTO settings (key,value) VALUES ('rss_feeds',?)", (val,))
con.commit()
con.close()
print(f"Updated: {len(feeds)} feeds")
for f in feeds:
    print(f"  - {f['name']}")
