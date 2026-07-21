import json
import logging
import os
import re
from datetime import datetime, timedelta

import feedparser

from database import Article, Setting, ThreadsAccount, db

logger = logging.getLogger(__name__)

DAYS_LIMIT = 7
AI_BATCH_SIZE = 30  # 1回のAPIコールで送るタイトルの上限

# ── 女性KPOPキーワード（1つでも含めば第1フィルター通過） ────────────────────
_FEMALE_GROUPS = [
    "blackpink", "twice", "aespa", "ive", "newjeans", "new jeans",
    "itzy", "red velvet", "girls' generation", "girls generation", "snsd",
    "(g)i-dle", "gidle", "g i-dle", "mamamoo", "le sserafim",
    "nmixx", "kep1er", "stayc", "fromis_9", "fromis", "illit",
    "babymonster", "baby monster", "triples",
    "sistar", "2ne1", "miss a", "f(x)", "4minute", "apink",
    "gfriend", "wjsn", "cosmic girls", "oh my girl", "loona",
    "exid", "t-ara", "after school", "nine muses", "momoland",
    "clc", "brave girls", "secret number", "weeekly", "viviz",
    "billlie", "lapillus", "kiss of life", "young posse", "meovv",
    "bugaboo", "purple kiss", "uni.t", "nature", "cignature",
    "lightsum", "mimiirose", "qwer", "rescene",
    "artms", "katseye", "unis", "dreamcatcher", "gwsn",
    "rocket punch", "bvndit", "dkz", "hinapia", "alice",
    "bambino", "dnation", "candy shop",
    "cortis", "fifty fifty", "hot issue",
]

_FEMALE_SOLOS = [
    "taeyeon", "sunmi", "hyuna", "chungha", "somi", "heize",
    "hwasa", "yubin", "moonbyul", "wheein", "solar",
    "tiffany", "sooyoung", "yoona", "seohyun", "wendy",
    "momo", "sana", "jihyo", "mina", "dahyun", "chaeyoung", "tzuyu",
    "nayeon", "jeongyeon", "seulgi", "irene", "yeri",
    "jennie", "jisoo", "lisa", "chaelisa", "rosé",
    "minnie", "miyeon", "soyeon", "yuqi", "soojin", "shuhua",
    "hyunjin", "kim lip", "jinsoul", "choerry",
]

_FEMALE_GENERAL = [
    "girl group", "girl band", "girlgroup", "female idol",
    "girl crush", "female artist", "kpop girl", "k-pop girl",
    "kpop queen", "k-pop queen", "girl power",
]

_FEMALE_WORD_BOUNDARY = ["iu", "joy", "gain", "rose"]

FEMALE_KEYWORDS = _FEMALE_GROUPS + _FEMALE_SOLOS + _FEMALE_GENERAL

# ── 除外キーワード ──────────────────────────────────────────────────────────
EXCLUDE_KEYWORDS = [
    "bts", "exo", "nct", "stray kids", "straykids", "seventeen",
    "ateez", "txt", "shinee", "bigbang", "big bang", "2pm", "got7",
    "enhypen", "the boyz", "ab6ix", "astro", "monsta x", "vixx",
    "btob", "infinite", "block b", "b.a.p", "victon", "sf9",
    "day6", "cnblue", "ftisland", "super junior", "shinhwa",
    "nu'est", "wanna one", "pentagon", "cix", "oneus", "onewe",
    "omega x", "8turn", "riize", "zerobaseone", "tempest", "boynextdoor",
    "tiot", "mcnd", "ghost9", "verivery", "drippin", "cravity",
    "election", "president", "government", "parliament", "minister",
    "politics", "political", "stock market", "economy", "gdp",
    "soccer", "football", "basketball", "baseball",
    "olympic", "athlete", "championship", "world cup",
    "c-drama", "c drama", "chinese drama", "thai drama", "japanese drama",
    "kdrama tips", "dramas to watch", "drama to watch",
    "drama series", "drama review", "thriller romance drama",
    "upcoming drama", "new drama confirms",
]


# ── フィルター関数 ───────────────────────────────────────────────────────────

def _check_female_kpop(title: str, content: str) -> bool:
    text = (title + " " + content).lower()
    for kw in _FEMALE_WORD_BOUNDARY:
        if re.search(r"\b" + re.escape(kw) + r"\b", text):
            return True
    return any(kw in text for kw in FEMALE_KEYWORDS)


def _check_excluded(title: str, content: str) -> bool:
    text = (title + " " + content).lower()
    return any(kw in text for kw in EXCLUDE_KEYWORDS)


# ── content_topicアカウント（非KPOP）向け除外キーワード ──────────────────────
# 現状ガチャ沼の住人（ガチャガチャ・カプセルトイ）向け。将来別のcontent_topic
# アカウントが増えた場合はアカウント別リストへの汎用化を検討する。
CONTENT_TOPIC_EXCLUDE_KEYWORDS = ["オープン", "ゲームセンター", "ゲーセン"]


def _check_content_topic_excluded(title: str, content: str) -> bool:
    text = title + " " + content
    return any(kw in text for kw in CONTENT_TOPIC_EXCLUDE_KEYWORDS)


def _is_recent(published_at) -> bool:
    if published_at is None:
        return True
    return published_at >= datetime.utcnow() - timedelta(days=DAYS_LIMIT)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


# ── AI判定（第2フィルター） ──────────────────────────────────────────────────

def _ai_judge_titles(titles: list, api_key: str, topic_label: str = "女性KPOPアイドル") -> list:
    """
    タイトルリストをClaude Haikuに送り、指定トピックに関連するインデックス(0始まり)を返す。
    APIエラー・キー未設定時はフォールバックとして全インデックスを返す。
    """
    if not titles:
        return []
    if not api_key:
        logger.warning("AI判定スキップ: Anthropic APIキー未設定")
        return list(range(len(titles)))

    import anthropic

    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    if topic_label == "女性KPOPアイドル":
        prompt = (
            "以下の記事タイトルのうち、女性KPOPアイドル（グループ・ソロ）に関する"
            "ニュース・レビュー・インタビュー・カムバック・コンサート情報のものを選び、"
            "番号をカンマ区切りで返してください。\n"
            "除外: 男性アイドル・韓国ドラマ・映画・スポーツ・政治・一般音楽\n\n"
            f"{numbered}\n\n"
            "回答は番号のみ（例: 1,3,5）。対象なし→「なし」"
        )
    else:
        prompt = (
            f"以下の記事タイトルのうち、{topic_label}に関するニュースを選び、"
            "番号をカンマ区切りで返してください。\n"
            f"除外: イベント・展示会情報のみのもの、{topic_label}と直接関係のないニュース\n\n"
            f"{numbered}\n\n"
            "回答は番号のみ（例: 1,3,5）。対象なし→「なし」"
        )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        response = msg.content[0].text.strip()
        logger.info("AI判定レスポンス: [%s]", response)

        if not response or response == "なし":
            return []

        approved = []
        for part in response.replace("、", ",").split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(titles):
                    approved.append(idx)
        return approved

    except Exception as exc:
        logger.error("AI判定エラー（フォールバック: 全件通過）: %s", exc)
        return list(range(len(titles)))


def _ai_judge_batched(titles: list, api_key: str, topic_label: str = "女性KPOPアイドル") -> list:
    """AI_BATCH_SIZE を超える場合は分割して判定する。"""
    if len(titles) <= AI_BATCH_SIZE:
        return _ai_judge_titles(titles, api_key, topic_label=topic_label)

    approved = []
    for offset in range(0, len(titles), AI_BATCH_SIZE):
        batch = titles[offset:offset + AI_BATCH_SIZE]
        indices = _ai_judge_titles(batch, api_key, topic_label=topic_label)
        approved.extend(i + offset for i in indices)
    return approved


# ── メイン収集関数 ───────────────────────────────────────────────────────────

def get_feed_list(app) -> list:
    with app.app_context():
        raw = Setting.get("rss_feeds", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return []


def _is_japanese_feed(feed_info) -> bool:
    """feedのlangフィールドが"ja"ならTrue（日本語フィードとして扱う）。"""
    if isinstance(feed_info, dict):
        return feed_info.get("lang", "").lower() == "ja"
    return False


def collect_articles(app) -> int:
    """全RSSフィードから新着記事を取得してDBに保存する。新記事数を返す。"""
    feeds = get_feed_list(app)
    with app.app_context():
        api_key = Setting.get("anthropic_api_key", "") or os.getenv("ANTHROPIC_API_KEY", "")
        topic_by_account = {
            acc.id: (acc.content_topic or "").strip()
            for acc in ThreadsAccount.query.all()
        }

    new_count = 0
    seen_urls = set()  # 今回の収集内での重複防止

    for feed_info in feeds:
        url  = feed_info.get("url", "") if isinstance(feed_info, dict) else str(feed_info)
        name = feed_info.get("name", url) if isinstance(feed_info, dict) else url
        is_ja = _is_japanese_feed(feed_info)
        account_id = feed_info.get("account_id", 1) if isinstance(feed_info, dict) else 1
        content_topic = topic_by_account.get(account_id, "")
        if not url:
            continue

        try:
            parsed = feedparser.parse(url, request_headers={"User-Agent": "KpopWaveBot/1.0"})
            entries = parsed.entries[:50]
        except Exception as exc:
            logger.error("Feed fetch error [%s]: %s", name, exc)
            continue

        skipped_date = skipped_kw = skipped_dup = skipped_ai = added = 0
        candidates = []  # (entry_data_dict) キーワード通過済み候補

        with app.app_context():
            for entry in entries:
                article_url = entry.get("link", "").strip()
                if not article_url:
                    continue

                # 公開日時
                published_at = None
                if getattr(entry, "published_parsed", None):
                    try:
                        published_at = datetime(*entry.published_parsed[:6])
                    except Exception:
                        pass

                # ── フィルター1: 日付 ──────────────────────────────────────
                if not _is_recent(published_at):
                    skipped_date += 1
                    continue

                # コンテンツ取得
                content = ""
                if hasattr(entry, "content") and entry.content:
                    content = entry.content[0].get("value", "")
                elif hasattr(entry, "summary"):
                    content = entry.summary
                elif hasattr(entry, "description"):
                    content = entry.description
                plain_content = _strip_html(content)
                title = (entry.get("title") or "")

                # content_topic 設定済みアカウント（非KPOP）のフィードは、女性KPOP
                # キーワード辞書の代わりにcontent_topic向け除外キーワードを適用する。
                # KPOPアカウント（content_topic未設定）は既存通り、日本語フィードのみスキップ。
                if content_topic:
                    # ── フィルター2': content_topic向け除外キーワード ─────
                    if _check_content_topic_excluded(title, plain_content):
                        skipped_kw += 1
                        continue
                elif not is_ja:
                    # ── フィルター2: 除外キーワード ──────────────────────
                    if _check_excluded(title, plain_content):
                        skipped_kw += 1
                        continue

                    # ── フィルター3: 女性KPOPキーワード ──────────────────
                    if not _check_female_kpop(title, plain_content):
                        skipped_kw += 1
                        continue

                # ── 重複チェック（DB + 今回収集分） ───────────────────────
                if article_url in seen_urls or Article.query.filter_by(url=article_url).first():
                    skipped_dup += 1
                    continue

                candidates.append({
                    "title":        title,
                    "url":          article_url,
                    "published_at": published_at,
                    "raw_content":  plain_content[:5000],
                    "feed_source":  name,
                    "lang":         "ja" if is_ja else "en",
                    "account_id":   account_id,
                })

        if not candidates:
            logger.info(
                "[%s] 追加:0 除外(日付):%d 除外(KW):%d 重複:%d",
                name, skipped_date, skipped_kw, skipped_dup,
            )
            continue

        # ── フィルター4: Claude Haiku AI判定（タイトルのみ送信） ──────────
        titles_only = [c["title"] for c in candidates]
        topic_label = content_topic or "女性KPOPアイドル"
        approved_indices = _ai_judge_batched(titles_only, api_key, topic_label=topic_label)
        skipped_ai = len(candidates) - len(approved_indices)

        with app.app_context():
            for idx in approved_indices:
                c = candidates[idx]
                article = Article(
                    feed_source=c["feed_source"],
                    title=c["title"][:500] or "No Title",
                    url=c["url"],
                    published_at=c["published_at"],
                    raw_content=c["raw_content"],
                    status="pending",
                    account_id=c["account_id"],
                )
                db.session.add(article)
                seen_urls.add(c["url"])
                added += 1
                new_count += 1
            db.session.commit()

        logger.info(
            "[%s][%s] 追加:%d 除外(日付):%d 除外(KW):%d 除外(AI):%d 重複:%d",
            name, "ja" if is_ja else "en", added, skipped_date, skipped_kw, skipped_ai, skipped_dup,
        )

    logger.info("収集完了 — 新規追加合計: %d 件", new_count)
    return new_count
