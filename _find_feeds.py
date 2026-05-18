"""RSS フィード候補の疎通・内容チェック"""
import feedparser, datetime

CANDIDATES = [
    # 英語KPOPメディア
    ("Billboard K-Pop",   "https://www.billboard.com/feed/?post_type=article&tag=k-pop"),
    ("Variety K-Pop",     "https://variety.com/tag/k-pop/feed/"),
    ("Rolling Stone KP",  "https://www.rollingstone.com/music/music-news/feed/"),
    ("NME K-Pop",         "https://www.nme.com/tag/k-pop/feed"),
    ("Hypebae",           "https://hypebae.com/feed"),
    ("Consequence",       "https://consequence.net/tag/k-pop/feed/"),
    # KPOP専門英語
    ("Allkpop2",          "https://www.allkpop.com/rss/news"),
    ("Allkpop3",          "https://www.allkpop.com/feed.rss"),
    ("KpopStarz",         "https://www.kpopstarz.com/feed/"),
    ("KpopReviewed",      "https://kpopreviewed.com/feed/"),
    ("UnitedKpop",        "https://www.unitedkpop.com/feed/"),
    ("AsianJunkie",       "https://www.asianjunkie.com/feed/"),
    ("TheBiasList",       "https://thebiaslist.com/feed/"),
    ("SeoulBeats",        "https://seoulbeats.com/feed/"),
    ("Mwave",             "https://www.mwave.me/feed/"),
    ("KpopPost2",         "https://kpoppost.com/feed/rss/"),
    ("KpopHerald3",       "https://www.koreaherald.com/rss/kpop.xml"),
    # 韓国英語メディア
    ("Korea Times Ent",   "https://www.koreatimes.co.kr/rss/entertainment.xml"),
    ("Yonhap Ent",        "https://en.yna.co.kr/RSS/entertainment.xml"),
    ("Korea JoongAng",    "https://koreajoongangdaily.joins.com/rss/news"),
    # グローバル音楽メディア（KPOP含む）
    ("Pitchfork",         "https://pitchfork.com/rss/news/"),
    ("The Fader",         "https://www.thefader.com/rss"),
    ("PopCrush2",         "https://popcrush.com/tag/k-pop/feed/"),
    ("Idolator",          "https://www.idolator.com/feed"),
    ("Popdust",           "https://popdust.com/feed/"),
]

now = datetime.datetime.utcnow()
cutoff = now - datetime.timedelta(days=30)

results = []
for name, url in CANDIDATES:
    try:
        p = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 KpopWaveBot/1.0"})
        status = p.get("status", "?")
        n = len(p.entries)
        if n == 0:
            results.append((False, name, url, f"HTTP {status} / 0 entries"))
            continue

        # 最新記事の日付確認
        e0 = p.entries[0]
        pub = getattr(e0, "published_parsed", None)
        age = ""
        if pub:
            dt = datetime.datetime(*pub[:6])
            days = (now - dt).days
            age = f"{days}日前"
            recent = dt >= cutoff
        else:
            recent = True
            age = "日付不明"

        title = e0.get("title", "")[:55].encode("ascii", errors="replace").decode()
        results.append((n > 0 and recent, name, url, f"HTTP {status} / {n}件 / 最新:{age} | {title}"))
    except Exception as ex:
        results.append((False, name, url, f"ERROR: {ex}"))

print(f"{'OK/NG':<4} {'サイト名':<20} {'詳細'}")
print("-" * 80)
for ok, name, url, detail in sorted(results, key=lambda x: not x[0]):
    mark = "OK" if ok else "NG"
    print(f"{mark:<4} {name:<20} {detail}")
