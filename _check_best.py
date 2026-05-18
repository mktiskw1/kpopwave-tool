"""有望フィードの記事タイトルを5件ずつ確認"""
import feedparser

BEST = [
    ("NME K-Pop",    "https://www.nme.com/tag/k-pop/feed"),
    ("AsianJunkie",  "https://www.asianjunkie.com/feed/"),
    ("TheBiasList",  "https://thebiaslist.com/feed/"),
    ("SeoulBeats",   "https://seoulbeats.com/feed/"),
    ("KpopReviewed", "https://kpopreviewed.com/feed/"),
    ("Variety KP",   "https://variety.com/tag/k-pop/feed/"),
]

for name, url in BEST:
    p = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 KpopWaveBot/1.0"})
    print(f"\n{'='*55}")
    print(f"  {name}  ({len(p.entries)} entries)")
    print(f"{'='*55}")
    for e in p.entries[:6]:
        t = e.get("title", "").encode("ascii", errors="replace").decode()
        pub = getattr(e, "published", "")[:10]
        print(f"  [{pub}] {t[:65]}")
