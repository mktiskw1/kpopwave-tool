import json
import logging
import os
import re
import requests
from datetime import datetime

from database import FollowCandidate, Setting, db

logger = logging.getLogger(__name__)

THREADS_API = "https://graph.threads.net/v1.0"
_REDDIT_UA  = "KpopWaveBot/1.0 (kpop follow-candidate discovery)"

# Threads HTMLスクレイピング用。モバイルUAが必要（PCだとフォロワー数が返らないことがある）
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# ── 旧シード（公式）の削除対象 ───────────────────────────────────────────────
# 以前追加した公式グループ・メディアアカウントを自動クリーンアップする
_OLD_OFFICIAL = {
    "aespa_official", "allkpop", "babymonster_official", "blackpink",
    "dreamcatcher_official", "fromis_9", "girlsgeneration", "illit_official",
    "itzy.all.in.us", "ive.official", "kep1er_official", "kissoflife_official",
    "koreaboo", "le_sserafim", "mamamoo_official", "newjeans_official",
    "nmixx_official", "qwer_official", "redvelvet.smtown", "seoulbeats",
    "soompi", "stayc_official", "thebiaslist", "twicetagram", "weeekly_official",
}

# シードアカウントは空（ファンアカウントは特定できないため Reddit 発見 or 手動追加で運用）
SEED_ACCOUNTS: list = []

# ── ユーティリティ ──────────────────────────────────────────────────────────

def _parse_follower_count(text: str) -> "int | None":
    text = re.sub(r"[,\s]", "", str(text)).upper()
    m = re.match(r"^([\d.]+)([KM]?)$", text)
    if not m:
        return None
    num = float(m.group(1))
    suffix = m.group(2)
    if suffix == "K":
        return int(num * 1_000)
    if suffix == "M":
        return int(num * 1_000_000)
    return int(num)


def fmt_followers(n: "int | None") -> str:
    if n is None:
        return "不明"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


# ── Threads API: 自分のフォロワー数 ─────────────────────────────────────────

def get_my_follower_count(app) -> "int | None":
    with app.app_context():
        user_id = Setting.get("threads_user_id") or os.getenv("THREADS_USER_ID", "")
        token   = Setting.get("threads_access_token") or os.getenv("THREADS_ACCESS_TOKEN", "")
    if not user_id or not token:
        return None
    try:
        r = requests.get(
            f"{THREADS_API}/{user_id}",
            params={"fields": "followers_count", "access_token": token},
            timeout=10,
        )
        return r.json().get("followers_count")
    except Exception as exc:
        logger.warning("自分のフォロワー数取得失敗: %s", exc)
        return None


# ── Threads プロフィール取得（内部APIを使用） ────────────────────────────────

# Threads web app が実際に使用している内部 API エンドポイント。
# Instagram インフラを共有しているため Instagram の内部 API パターンで動作する。
_PROFILE_API = "https://www.threads.net/api/v1/users/web_profile_info/"
_IG_APP_ID   = "238260118697367"
_IG_UA       = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 Instagram/300.0.0.29.109"
)


def _scrape_threads_profile(username: str) -> dict:
    """
    Threads 内部 API からフォロワー数と biography を取得する。
    レスポンス例: data.user.edge_followed_by.count / data.user.biography
    """
    try:
        r = requests.get(
            _PROFILE_API,
            params={"username": username},
            headers={
                "User-Agent": _IG_UA,
                "X-IG-App-ID": _IG_APP_ID,
                "Accept": "application/json",
                "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
            },
            timeout=12,
        )
        if r.status_code != 200:
            logger.debug("Threads API HTTP %d for @%s", r.status_code, username)
            return {}

        user = r.json().get("data", {}).get("user") or {}
        result: dict = {}

        # フォロワー数: edge_followed_by.count
        fc = (user.get("edge_followed_by") or {}).get("count")
        if fc is not None:
            result["followers"] = int(fc)

        # 表示名
        fn = user.get("full_name") or ""
        if fn:
            result["display_name"] = fn[:100]

        # biography
        bio = user.get("biography") or ""
        if bio:
            result["bio"] = bio[:300]

        return result

    except Exception as exc:
        logger.debug("プロフィールAPI失敗 @%s: %s", username, exc)
        return {}


# ── K-POP 文脈・バイオ検証 ────────────────────────────────────────────────────

# 投稿本文・バイオ共通のK-POPキーワード判定
_KPOP_RE = re.compile(
    r"kpop|k-pop|girlgroup|girl[\s._-]?group|idol|twice|blackpink|newjeans|"
    r"aespa|lesserafim|le[\s._-]sserafim|itzy|\bive\b|nmixx|stayc|kep1er|"
    r"dreamcatcher|weeekly|mamamoo|girlsgeneration|babymonster|katseye|"
    r"ファン|팬|fanaccount|fan[\s._-]account|fansite|stan\b|kpopjapan",
    re.IGNORECASE,
)


# ── Reddit 発見 ──────────────────────────────────────────────────────────────

_NOISE = {
    "kpop", "kpopthreads", "threads", "reddit", "instagram",
    "twitter", "youtube", "tiktok", "spotify", "weverse",
    "naver", "vlive", "melon", "mnet", "p", "t", "i", "n",
    "share", "the", "at", "my", "me", "us", "to", "a",
}

_URL_RE = re.compile(r"threads\.net/@?([A-Za-z0-9._]{3,30})", re.IGNORECASE)


def _extract_kpop_usernames(text: str) -> set:
    """
    K-POPキーワードが存在する投稿内の Threads URL のみからユーザー名を抽出する。
    テキスト全体にK-POPキーワードがない → 完全スキップ（非K-POPスパムを排除）。
    """
    if not _KPOP_RE.search(text):
        return set()
    found = set()
    for m in _URL_RE.finditer(text):
        # URLの前後200文字にもK-POPキーワードが必要（文脈チェック）
        snippet = text[max(0, m.start() - 200) : m.end() + 200]
        if not _KPOP_RE.search(snippet):
            continue
        u = m.group(1).lower().rstrip(".")
        if u not in _NOISE and len(u) >= 3:
            found.add(u)
    return found


def _reddit_fetch(url: str, params: dict) -> list:
    """Reddit JSON エンドポイントからポスト一覧を取得する。"""
    try:
        r = requests.get(
            url, params=params,
            headers={"User-Agent": _REDDIT_UA},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return r.json().get("data", {}).get("children", [])
    except Exception as exc:
        logger.debug("Reddit fetch エラー %s: %s", url, exc)
        return []


def _discover_reddit(limit: int = 50) -> list:
    """
    r/kpop・r/kpopthreads・r/girlgroups 専用スレッドから
    K-POPファンの Threads アカウントを収集する。
    K-POP文脈なしのURLは一切採用しない。
    """
    found: set = set()

    # 3サブレ限定 × SNS共有文脈のキーワードで検索
    search_targets = [
        ("kpop",        "threads.net kpop fan follow",  False),
        ("kpop",        "threads.net follow my threads", False),
        ("kpopthreads", "threads.net",                  True),
        ("kpopthreads", "follow threads kpop",          True),
        ("girlgroups",  "threads.net fan account",      True),
        ("girlgroups",  "threads follow kpop",          True),
    ]

    for sub, q, restrict in search_targets:
        params = {"q": q, "sort": "new", "limit": limit}
        if restrict:
            params["restrict_sr"] = 1
        posts = _reddit_fetch(
            f"https://www.reddit.com/r/{sub}/search.json", params
        )
        for post in posts:
            pd = post.get("data", {})
            text = pd.get("title", "") + " " + (pd.get("selftext", "") or "")
            found |= _extract_kpop_usernames(text)

    # 3サブレの新着・ホット投稿をスキャン（SNS共有スレッドを拾う）
    for sub in ("kpop", "kpopthreads", "girlgroups"):
        for sort in ("new", "hot"):
            posts = _reddit_fetch(
                f"https://www.reddit.com/r/{sub}/{sort}.json",
                {"limit": 50},
            )
            for post in posts:
                pd = post.get("data", {})
                text = pd.get("title", "") + " " + (pd.get("selftext", "") or "")
                found |= _extract_kpop_usernames(text)

    return list(found)


# ── CRUD ヘルパー ───────────────────────────────────────────────────────────

def add_candidate(app, username: str, display_name: str = "", bio: str = "") -> bool:
    username = username.lstrip("@").strip().lower()
    if not username:
        return False
    with app.app_context():
        if FollowCandidate.query.filter_by(username=username).first():
            return False
        db.session.add(FollowCandidate(
            username=username,
            display_name=display_name or None,
            bio=bio or None,
            source="manual",
        ))
        db.session.commit()
    return True


def delete_candidate(app, cid: int) -> None:
    with app.app_context():
        fc = FollowCandidate.query.get(cid)
        if fc:
            db.session.delete(fc)
            db.session.commit()


def set_follower_count(app, cid: int, count: "int | None") -> None:
    with app.app_context():
        fc = FollowCandidate.query.get(cid)
        if fc:
            fc.followers_count = count
            fc.updated_at = datetime.utcnow()
            db.session.commit()


# ── メイン更新関数 ──────────────────────────────────────────────────────────

def _purge_old_official(app) -> int:
    """旧シード（公式グループ・メディア）を DB から削除する。手動追加は除外。"""
    with app.app_context():
        deleted = (
            FollowCandidate.query
            .filter(
                FollowCandidate.username.in_(_OLD_OFFICIAL),
                FollowCandidate.source != "manual",
            )
            .delete(synchronize_session=False)
        )
        db.session.commit()
    return deleted


def purge_non_kpop(app) -> int:
    """バイオが取得済みで K-POP キーワードを含まない非K-POPアカウントを除外する。手動追加は除外しない。"""
    with app.app_context():
        targets = FollowCandidate.query.filter(
            FollowCandidate.bio.isnot(None),
            FollowCandidate.source != "manual",
        ).all()
        deleted = 0
        for fc in targets:
            if fc.bio and not _KPOP_RE.search(fc.bio):
                logger.info("非K-POPバイオ一括除外: @%s", fc.username)
                db.session.delete(fc)
                deleted += 1
        db.session.commit()
    return deleted


def refresh_candidates(app) -> dict:
    """
    クリーンアップ → Reddit 発見 → プロフィールスクレイピング を実行。
    フォロワー数不明の非手動アカウントは更新ごとに削除する。
    """
    purged = _purge_old_official(app)
    purged += purge_non_kpop(app)

    # フォロワー数不明の非手動アカウントを削除（架空アカウント・前回スクレイプ失敗分）
    with app.app_context():
        unknown_purged = (
            FollowCandidate.query
            .filter(
                FollowCandidate.followers_count.is_(None),
                FollowCandidate.source != "manual",
            )
            .delete(synchronize_session=False)
        )
        db.session.commit()
    purged += unknown_purged

    new_reddit = scraped = removed_non_kpop = 0

    # Reddit 発見（K-POP文脈チェック済み）
    reddit_names = _discover_reddit()
    with app.app_context():
        for uname in reddit_names[:50]:
            if not FollowCandidate.query.filter_by(username=uname).first():
                db.session.add(FollowCandidate(username=uname, source="reddit"))
                new_reddit += 1
        db.session.commit()

    # フォロワー数未取得アカウントのスクレイピング（最大40件）
    with app.app_context():
        targets = [
            (c.username, c.source) for c in
            FollowCandidate.query
            .filter(FollowCandidate.followers_count.is_(None))
            .limit(40).all()
        ]

    for uname, source in targets:
        profile = _scrape_threads_profile(uname)

        if not profile:
            # プロフィール取得失敗 = アカウント不存在 → 手動追加以外は削除
            if source != "manual":
                with app.app_context():
                    fc = FollowCandidate.query.filter_by(username=uname).first()
                    if fc:
                        db.session.delete(fc)
                        db.session.commit()
            continue

        bio = profile.get("bio", "") or ""

        # バイオがあってK-POPキーワードが一切ない → 除外（手動追加は除外しない）
        if bio and not _KPOP_RE.search(bio) and source != "manual":
            logger.info("非K-POPバイオのため除外: @%s — %s", uname, bio[:80])
            with app.app_context():
                fc = FollowCandidate.query.filter_by(username=uname).first()
                if fc:
                    db.session.delete(fc)
                    db.session.commit()
            removed_non_kpop += 1
            continue

        with app.app_context():
            fc = FollowCandidate.query.filter_by(username=uname).first()
            if fc:
                if profile.get("followers") is not None:
                    fc.followers_count = profile["followers"]
                    scraped += 1
                if bio and not fc.bio:
                    fc.bio = bio
                if profile.get("display_name") and not fc.display_name:
                    fc.display_name = profile["display_name"]
                fc.updated_at = datetime.utcnow()
                db.session.commit()

    logger.info(
        "フォロー候補更新: 削除=%d reddit+%d scraped=%d 非KPOP除外=%d",
        purged, new_reddit, scraped, removed_non_kpop,
    )
    return {
        "purged": purged,
        "new_reddit": new_reddit,
        "scraped": scraped,
        "removed_non_kpop": removed_non_kpop,
    }


# ── ページデータ構築 ────────────────────────────────────────────────────────

def get_page_data(app) -> dict:
    with app.app_context():
        all_cands = FollowCandidate.query.order_by(
            FollowCandidate.followers_count.desc()
        ).all()

    my_fc = get_my_follower_count(app)
    if my_fc is None:
        min_target  = None
        max_target  = None
        fixed_range = False
    elif my_fc == 0:
        # フォロワー0人の場合は固定レンジで候補を表示
        min_target  = 100
        max_target  = 1000
        fixed_range = True
    else:
        min_target  = my_fc * 3
        max_target  = my_fc * 15
        fixed_range = False

    in_range, out_range, unknown_fc = [], [], []
    for c in all_cands:
        if c.followers_count is None:
            unknown_fc.append(c)
        elif min_target is not None and min_target <= c.followers_count <= max_target:
            in_range.append(c)
        else:
            out_range.append(c)

    return {
        "my_followers":  my_fc,
        "min_target":    min_target,
        "max_target":    max_target,
        "fixed_range":   fixed_range,
        "in_range":      in_range,
        "out_range":     out_range,
        "unknown_fc":    unknown_fc,
        "total":         len(all_cands),
        "fmt_followers": fmt_followers,
    }
