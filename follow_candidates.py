import json
import logging
import os
import re
import time
import requests
from datetime import datetime, timedelta

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
        logger.info("フォロワー数取得スキップ: 認証情報が未設定")
        return None

    token_preview = f"{token[:12]}...{token[-4:]}" if len(token) > 20 else "***"
    logger.info("フォロワー数取得開始: user_id=%s token=%s", user_id, token_preview)

    # 方法1: GET /me?fields=followers_count（ユーザーオブジェクトフィールド）
    try:
        r = requests.get(
            f"{THREADS_API}/me",
            params={"fields": "id,username,followers_count", "access_token": token},
            timeout=10,
        )
        logger.info("方法1 GET /me: status=%d body=%s", r.status_code, r.text[:300])
        d = r.json()
        if "error" not in d:
            fc = d.get("followers_count")
            if fc is not None:
                logger.info("フォロワー数取得成功（方法1 /me フィールド）: %d", fc)
                return fc
            logger.info("方法1: followers_count フィールドが返されず → 方法2を試行")
        else:
            logger.warning("方法1エラー: code=%s message=%s", d["error"].get("code"), d["error"].get("message"))
    except Exception as e:
        logger.warning("方法1例外: %s", e)

    # 方法2: GET /{user_id}/insights?metric=followers_count（Insights API）
    try:
        now_ts = int(time.time())
        since_ts = now_ts - 86400 * 3  # 3日前
        r = requests.get(
            f"{THREADS_API}/{user_id}/insights",
            params={
                "metric": "followers_count",
                "period": "day",
                "since": since_ts,
                "until": now_ts,
                "access_token": token,
            },
            timeout=10,
        )
        logger.info("方法2 GET /%s/insights: status=%d body=%s", user_id, r.status_code, r.text[:300])
        d = r.json()
        if "error" not in d:
            for item in d.get("data", []):
                if item.get("name") == "followers_count":
                    values = item.get("values", [])
                    if values:
                        fc = values[-1].get("value")
                        if fc is not None:
                            logger.info("フォロワー数取得成功（方法2 Insights）: %d", fc)
                            return fc
            logger.info("方法2: データなし → 方法3を試行")
        else:
            logger.warning("方法2エラー: code=%s message=%s", d["error"].get("code"), d["error"].get("message"))
    except Exception as e:
        logger.warning("方法2例外: %s", e)

    # 方法3: GET /{user_id}?fields=followers_count（user_id 直接指定）
    try:
        r = requests.get(
            f"{THREADS_API}/{user_id}",
            params={"fields": "id,username,followers_count", "access_token": token},
            timeout=10,
        )
        logger.info("方法3 GET /%s: status=%d body=%s", user_id, r.status_code, r.text[:300])
        d = r.json()
        if "error" not in d:
            fc = d.get("followers_count")
            if fc is not None:
                logger.info("フォロワー数取得成功（方法3 /{user_id} フィールド）: %d", fc)
                return fc
            logger.info("方法3: followers_count フィールドなし")
        else:
            logger.warning("方法3エラー: code=%s message=%s", d["error"].get("code"), d["error"].get("message"))
    except Exception as e:
        logger.warning("方法3例外: %s", e)

    logger.warning("フォロワー数取得: すべての方法が失敗 (user_id=%s) — /api/debug/threads で詳細を確認できます", user_id)
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
            logger.info("プロフィールAPI HTTP %d for @%s: %s", r.status_code, username, r.text[:120])
            return {}

        user = r.json().get("data", {}).get("user") or {}
        result: dict = {}

        # 内部pk（スレッド取得に使用）
        pk = user.get("pk") or ""
        if pk:
            result["pk"] = str(pk)

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
        logger.info("プロフィールAPI例外 @%s: %s", username, exc)
        return {}


# ── KPOPアカウント返信者スキャン ─────────────────────────────────────────────

DEFAULT_KPOP_ACCOUNTS = [
    "blackpinkofficial",
    "newjeans_official",
    "aespa_official",
    "le_sserafim",
    "twicetagram",
    "itzy.all.in.us",
    "ive.official",
    "stayc_official",
    "mamamoo_official",
    "redvelvet.smtown",
    "kiss.of.life",
    "illit_official",
    "nmixx_official",
    "babymonster_official",
]


def _get_user_threads_internal(user_pk: str, count: int = 5) -> list:
    """内部APIでユーザーの最新スレッドのpkリストを取得する。"""
    try:
        r = requests.get(
            f"https://www.threads.net/api/v1/text_feed/{user_pk}/profile/",
            params={"count": count},
            headers={"User-Agent": _IG_UA, "X-IG-App-ID": _IG_APP_ID, "Accept": "application/json"},
            timeout=12,
        )
        if r.status_code != 200:
            logger.info("スレッド一覧取得 HTTP %d pk=%s: %s", r.status_code, user_pk, r.text[:120])
            return []
        pks = []
        for item in r.json().get("items", []):
            for ti in item.get("thread_items", []):
                pk = str((ti.get("post") or {}).get("pk") or "")
                if pk and pk not in pks:
                    pks.append(pk)
        return pks
    except Exception as e:
        logger.info("スレッド一覧内部API失敗 pk=%s: %s", user_pk, e)
        return []


def _get_thread_repliers_internal(thread_pk: str) -> set:
    """内部APIでスレッドへの返信者ユーザー名セットを取得する。"""
    usernames: set = set()
    try:
        r = requests.get(
            f"https://www.threads.net/api/v1/media/{thread_pk}/replies/",
            params={"flat": "1"},
            headers={"User-Agent": _IG_UA, "X-IG-App-ID": _IG_APP_ID, "Accept": "application/json"},
            timeout=12,
        )
        if r.status_code != 200:
            return usernames
        for item in r.json().get("items", []):
            uname = ((item.get("user") or {}).get("username") or "").strip().lower()
            if uname:
                usernames.add(uname)
    except Exception as e:
        logger.debug("返信者取得失敗 thread_pk=%s: %s", thread_pk, e)
    return usernames


def fetch_kpop_account_repliers(app, accounts: list = None) -> dict:
    """
    指定したKPOPアカウントの最新スレッドへの返信者をフォロー候補に追加する。
    公式Threads APIではフォロワーリスト取得が不可のため、
    返信者（KPOPファン確率が高い）を代替として収集する。
    """
    with app.app_context():
        setting_str = Setting.get("kpop_seed_accounts", "")

    if accounts is None:
        if setting_str.strip():
            accounts = [a.strip().lstrip("@").lower() for a in setting_str.split(",") if a.strip()]
        else:
            accounts = DEFAULT_KPOP_ACCOUNTS

    all_repliers: set = set()
    scan_log: list = []

    for username in accounts:
        profile = _scrape_threads_profile(username)
        user_pk = profile.get("pk", "")
        if not user_pk:
            logger.info("KPOPスキャン @%s: pk取得失敗", username)
            scan_log.append({"account": username, "ok": False, "error": "プロフィール取得失敗"})
            time.sleep(0.5)
            continue

        thread_pks = _get_user_threads_internal(user_pk, count=5)
        logger.info("KPOPスキャン @%s (pk=%s): %d件のスレッド取得", username, user_pk, len(thread_pks))

        repliers: set = set()
        for tpk in thread_pks:
            repliers |= _get_thread_repliers_internal(tpk)
            time.sleep(0.3)

        all_repliers |= repliers
        scan_log.append({"account": username, "ok": True, "threads": len(thread_pks), "repliers": len(repliers)})
        logger.info("KPOPスキャン @%s: %d名の返信者発見", username, len(repliers))
        time.sleep(1.0)

    added = 0
    with app.app_context():
        for uname in all_repliers:
            if not FollowCandidate.query.filter_by(username=uname).first():
                db.session.add(FollowCandidate(username=uname, source="kpop_reply"))
                added += 1
        db.session.commit()

    logger.info("KPOPスキャン完了: %d名発見 / %d名追加", len(all_repliers), added)
    return {"found": len(all_repliers), "added": added, "scan_log": scan_log}


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


# ── Threads エンゲージメント取得 ─────────────────────────────────────────────

def fetch_engagers_from_threads(app) -> dict:
    """自分の投稿にリプライ・いいねしたユーザーをThreads APIから取得してフォロー候補に追加する。"""
    with app.app_context():
        user_id = Setting.get("threads_user_id") or os.getenv("THREADS_USER_ID", "")
        token   = Setting.get("threads_access_token") or os.getenv("THREADS_ACCESS_TOKEN", "")

    if not user_id or not token:
        return {"error": "Threads APIの認証情報が設定されていません", "added": 0, "found": 0}

    # 1. 最近の投稿IDを取得
    try:
        r = requests.get(
            f"{THREADS_API}/{user_id}/threads",
            params={"fields": "id,timestamp", "limit": 20, "access_token": token},
            timeout=15,
        )
        if r.status_code != 200:
            return {"error": f"投稿取得失敗: HTTP {r.status_code} {r.text[:100]}", "added": 0, "found": 0}
        posts = r.json().get("data", [])
    except Exception as exc:
        return {"error": str(exc), "added": 0, "found": 0}

    usernames: set = set()
    own_lower = user_id.lower()

    for post in posts[:15]:
        post_id = post.get("id")
        if not post_id:
            continue

        # リプライユーザーを取得（username フィールドが直接入っている）
        try:
            rr = requests.get(
                f"{THREADS_API}/{post_id}/replies",
                params={"fields": "id,username", "limit": 100, "access_token": token},
                timeout=15,
            )
            if rr.status_code == 200:
                for reply in rr.json().get("data", []):
                    uname = (reply.get("username") or "").strip().lower()
                    if uname and uname != own_lower:
                        usernames.add(uname)
        except Exception as exc:
            logger.debug("リプライ取得失敗 post=%s: %s", post_id, exc)

        # いいねユーザーを取得（username フィールドが返る場合のみ追加）
        try:
            lr = requests.get(
                f"{THREADS_API}/{post_id}/likes",
                params={"fields": "id,username", "limit": 100, "access_token": token},
                timeout=15,
            )
            if lr.status_code == 200:
                for like in lr.json().get("data", []):
                    uname = (like.get("username") or "").strip().lower()
                    if uname and uname != own_lower:
                        usernames.add(uname)
        except Exception as exc:
            logger.debug("いいね取得失敗 post=%s: %s", post_id, exc)

    # 2. 未登録ユーザーをフォロー候補に追加
    added = 0
    with app.app_context():
        for uname in usernames:
            if not FollowCandidate.query.filter_by(username=uname).first():
                db.session.add(FollowCandidate(username=uname, source="engagement"))
                added += 1
        db.session.commit()

    logger.info("エンゲージメント取得完了: %d名発見 %d名追加", len(usernames), added)
    return {"added": added, "found": len(usernames)}


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

    # 30日以上前に追加されフォロワー数が取得できていないアカウントのみ削除
    # （全削除するとスクレイプ失敗時に候補がすべて消えるため期間を設ける）
    with app.app_context():
        stale_cutoff = datetime.utcnow() - timedelta(days=30)
        stale_purged = (
            FollowCandidate.query
            .filter(
                FollowCandidate.followers_count.is_(None),
                FollowCandidate.source != "manual",
                FollowCandidate.updated_at < stale_cutoff,
            )
            .delete(synchronize_session=False)
        )
        db.session.commit()
    purged += stale_purged

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
            # プロフィール取得失敗 → 削除せずスキップ（内部APIの一時的な不具合でも消えないように）
            logger.info("プロフィール取得失敗（スキップ・保持）: @%s", uname)
            time.sleep(0.5)
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

def _apply_filters(candidates: list, filter_status: str, filter_priority: str) -> list:
    """フォロー状態・優先度でリストを絞り込む。空文字は全件。"""
    result = candidates
    if filter_status:
        if filter_status == "none":
            result = [c for c in result if not c.follow_status]
        else:
            result = [c for c in result if c.follow_status == filter_status]
    if filter_priority:
        if filter_priority == "none":
            result = [c for c in result if not c.priority]
        else:
            result = [c for c in result if c.priority == filter_priority]
    return result


def get_page_data(app, filter_status: str = "", filter_priority: str = "") -> dict:
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

    # フィルタ適用
    in_range   = _apply_filters(in_range,   filter_status, filter_priority)
    out_range  = _apply_filters(out_range,  filter_status, filter_priority)
    unknown_fc = _apply_filters(unknown_fc, filter_status, filter_priority)

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
