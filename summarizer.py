import logging
import os
import re
from urllib.parse import urlparse

import anthropic
import requests

from database import Article, Setting, db

logger = logging.getLogger(__name__)

THREADS_MAX = 500
SOURCE_PREFIX = "\n\n📎 source: "

_RANKING_TITLE_KEYWORDS = frozenset([
    "ranking", "rankings", "ranked", "chart", "charts",
    "poll", "top 10", "top10", "brand reputation",
])

# 「1. Name (Group)」と「1. Name – Group」の2形式に対応
_RANK_PATTERNS = [
    re.compile(
        r'(\d{1,2})[.)]\s+'
        r'([\w][\w\s\'\-\.]{1,35}?)\s*'
        r'\(([\w\s\'\-\.&]{1,40}?)\)',
        re.UNICODE,
    ),
    re.compile(
        r'(\d{1,2})[.)]\s+'
        r'([\w][\w\s\'\-\.]{1,35}?)\s*'
        r'[–\-]\s*([\w\s\'\-\.&]{1,40}?)'
        r'(?=\s+\d{1,2}[.)]|\s*$)',
        re.UNICODE,
    ),
]

_FETCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _domain(url: str) -> str:
    host = urlparse(url).netloc
    return host.removeprefix("www.")


def _get_api_key(app) -> str:
    with app.app_context():
        key = Setting.get("anthropic_api_key", "")
    return key or os.getenv("ANTHROPIC_API_KEY", "")


def _fetch_article_content(url: str) -> tuple:
    """
    元記事URLから本文テキストを取得する。
    Returns: (text: str, success: bool)
    優先順位: <article> → <main> → <body> → 全体HTML
    """
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": _FETCH_UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
            },
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning("元記事 HTTP %d: %s", r.status_code, url)
            return "", False

        html = r.text

        # スクリプト・スタイルを事前除去
        html = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.IGNORECASE)

        # 優先順でコンテンツブロックを抽出
        for tag in ("article", "main", "body"):
            m = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", html, re.IGNORECASE)
            if m:
                block = m.group(1)
                break
        else:
            block = html

        # HTMLタグ除去・空白正規化
        text = re.sub(r"<[^>]+>", " ", block)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) < 100:
            return "", False

        logger.info("元記事取得成功 (%d chars): %s", len(text), url)
        return text[:8000], True

    except Exception as exc:
        logger.warning("元記事取得失敗: %s — %s", url, exc)
        return "", False


def _is_ranking_article(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _RANKING_TITLE_KEYWORDS)


def _extract_rankings(text: str) -> list:
    """元記事テキストから (rank, name, group) のリストを抽出して rank 順に返す。"""
    for pattern in _RANK_PATTERNS:
        matches = pattern.findall(text)
        if len(matches) < 3:
            continue
        seen, results = set(), []
        for m in matches:
            try:
                rank = int(m[0])
            except ValueError:
                continue
            if rank in seen or not (1 <= rank <= 100):
                continue
            seen.add(rank)
            results.append((rank, m[1].strip(), m[2].strip()))
        if len(results) >= 3 and min(r for r, _, _ in results) <= 3:
            results.sort(key=lambda x: x[0])
            return results
    return []


def summarize_article(app, article_id: int) -> bool:
    """1 記事の日本語要約を生成して DB に保存する。成功なら True。"""
    with app.app_context():
        article = Article.query.get(article_id)
        if not article:
            return False
        title       = article.title
        stored_body = (article.raw_content or "")[:3000]
        url         = article.url

    api_key = _get_api_key(app)
    if not api_key:
        logger.error("Anthropic API key not configured")
        return False

    # 元記事から本文取得（最優先）
    fresh_body, fetch_ok = _fetch_article_content(url)
    if fetch_ok:
        article_body = fresh_body
        fetch_note   = ""
    else:
        article_body = stored_body
        fetch_note   = "\n・元記事にアクセスできなかった場合のみ末尾に「詳細は元記事へ」と1行追加する"

    source_part     = SOURCE_PREFIX + url
    max_summary_len = THREADS_MAX - len(source_part) - 5

    # ランキング記事の前処理：正規表現で順位データを抽出
    extracted_rankings: list = []
    if _is_ranking_article(title):
        extracted_rankings = _extract_rankings(article_body)
        if extracted_rankings:
            logger.info("ランキング抽出成功: %d件", len(extracted_rankings))
        else:
            logger.info("ランキング抽出失敗: 通常プロンプトで処理")

    if extracted_rankings:
        top10 = extracted_rankings[:10]
        has_more = len(extracted_rankings) > 10
        ranking_lines = "\n".join(
            f"{rank}位 {name}（{group}）" for rank, name, group in top10
        )
        more_note = "\n他は元記事へ" if has_more else ""
        prompt = f"""以下のK-POPランキングをThreads投稿テキストに変換してください。

タイトル: {title}

━━ ルール ━━
・日本語のみ・{max_summary_len}文字以内（厳守）
・ハッシュタグ禁止。絵文字は冒頭1〜2個のみ
・投稿本文だけ出力（前置き・説明不要）

━━ 出力形式 ━━
1行目: 絵文字＋タイトルの短い説明（例: 🏆 5月ブランド評判ランキング）
続き: 下記ランキングデータを1行も変えずそのまま全行出力
末尾: 問いかけ1行（例: 推しはいた？）

━━ ランキングデータ（順番・内容の変更・省略禁止） ━━
{ranking_lines}{more_note}"""
    else:
        prompt = f"""以下のK-POP女性アイドル記事を、Threads投稿テキストに変換してください。

タイトル: {title}
本文: {article_body}

━━ 共通ルール ━━
・日本語のみ・{max_summary_len}文字以内（厳守）
・投稿を読むだけで完結する内容にする。リンク誘導・出し惜しみ・引きは一切しない{fetch_note}
・「〜です」「〜ます」「記事によると」「詳しくは」は禁止
・ハッシュタグ禁止。絵文字は冒頭1〜2個のみ
・投稿本文だけ出力（前置き・説明不要）

━━ 記事タイプ別の書き方 ━━

【ランキング・結果・順位・チャート・〇選 を含む記事】
・必ず1位を最初に書き、2位・3位…と数字が増える順に並べる
・順位・名前・グループ名のみ。感情表現・コメント・前置き・説明文は一切不要
・最後に一言の問いかけ（「推しいた？」など短く）

【情報提供・ニュース・レビュー系の記事】
・口語体・短文・体言止めで自然にまとめる
・グループ名・メンバー名・曲名・リリース名など核心の固有名詞を入れる
・感情は添える程度（「やばい」「鳥肌」「神曲」など自然に）
・最後に一言の問いかけ（「聴いた？」「どう思う？」など）"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        summary_text = msg.content[0].text.strip()

        if len(summary_text) > max_summary_len:
            summary_text = summary_text[:max_summary_len - 1] + "…"

        post_text = summary_text + source_part

        with app.app_context():
            art = Article.query.get(article_id)
            if art:
                art.summary = post_text
                art.error_message = None
                db.session.commit()

        logger.info("Summarized article %d (%d chars, fetch=%s)", article_id, len(post_text), fetch_ok)
        return True

    except Exception as exc:
        logger.error("Summarize error for article %d: %s", article_id, exc)
        with app.app_context():
            art = Article.query.get(article_id)
            if art:
                art.error_message = str(exc)
                db.session.commit()
        return False


def summarize_pending_articles(app) -> int:
    """要約未生成の pending 記事をまとめて処理する。成功件数を返す。"""
    with app.app_context():
        ids = [
            a.id
            for a in Article.query.filter_by(status="pending")
            .filter(Article.summary.is_(None))
            .all()
        ]

    count = 0
    for article_id in ids:
        if summarize_article(app, article_id):
            count += 1
    return count
