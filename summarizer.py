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

# スタイルごとの投稿トーン指示
_STYLE_PROMPTS: dict = {
    "つぶやき型": (
        "感情が先に出るつぶやき。「やばい」「嘘でしょ」「ちょっと待って」みたいな出だしもOK。"
        "情報は最小限でいい。驚き・感情・発見を中心に2〜4文。問いかけ不要。"
    ),
    "情報型": (
        "「ねえ聞いて」「知ってた？」みたいな自然な入りで情報を届けるスタイル。"
        "グループ名・曲名・メンバー名など固有名詞を正確に。口語体だけど内容はしっかり。4〜6文程度。"
    ),
    "体験談型": (
        "自分が体験したように書く。「さっき〜してたんだけど」「これ見て鳥肌立った」など"
        "一人称の体験として。何に驚いたか・どう感じたか・気づいたことを自然に語る。"
    ),
    "バズり型": (
        "「これ絶対見て！」「TLに流れてきたんだけど」レベルの熱量。"
        "興奮と勢いが伝わる、友達に今すぐ共有したくなるエネルギーで書く。大げさなくらいでOK。"
    ),
}


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


_JA_FEED_SOURCES = frozenset(["kstyle", "barks", "daebak"])


def _is_japanese_source(feed_source: str) -> bool:
    return any(s in (feed_source or "").lower() for s in _JA_FEED_SOURCES)


def summarize_article(app, article_id: int, style: str = "つぶやき型") -> bool:
    """1 記事の日本語投稿テキストを生成して DB に保存する。成功なら True。"""
    style_instruction = _STYLE_PROMPTS.get(style, _STYLE_PROMPTS["つぶやき型"])

    # 学習済みヒントを読み込む
    with app.app_context():
        learned_hints = Setting.get("learned_style_hints", "")
    learned_section = (
        f"\n━━ 学習済みインサイト（過去の高エンゲージメント投稿の傾向） ━━\n{learned_hints}"
        if learned_hints else ""
    )

    with app.app_context():
        article = Article.query.get(article_id)
        if not article:
            return False
        title       = article.title
        stored_body = (article.raw_content or "")[:3000]
        url         = article.url
        is_ja_src   = _is_japanese_source(article.feed_source or "")

    api_key = _get_api_key(app)
    if not api_key:
        logger.error("Anthropic API key not configured")
        return False

    # YouTube動画はフェッチせずDBの説明文を使用
    is_youtube = "youtube.com/watch" in url or "youtu.be/" in url
    if is_youtube:
        article_body = stored_body
        fetch_ok     = True
        fetch_note   = ""
    else:
        fresh_body, fetch_ok = _fetch_article_content(url)
        if fetch_ok:
            article_body = fresh_body
            fetch_note   = ""
        else:
            article_body = stored_body
            fetch_note   = "\n・元記事にアクセスできていない場合は末尾に「詳細は元記事で」と一言だけ添える"

    source_part     = SOURCE_PREFIX + url
    max_summary_len = THREADS_MAX - len(source_part) - 5

    _COMMON_RULES = f"""━━ ルール ━━
・日本語のみ・{max_summary_len}文字以内（厳守）
・ハッシュタグ禁止。絵文字は0〜2個まで
・「〜です」「〜ます」口調禁止。自然な口語体で
・同じ表現・フレーズを繰り返さない
・問いかけは毎回入れなくていい。入れるなら内容にぴったりな自然な一言だけ
・出力は本文のみ（前置き・スタイル名・説明不要）

━━ 絶対禁止 ━━
以下の表現・ニュアンスを一切使わない。これらは記事・情報源の存在を匂わせるため完全NG。
「記事によると」「〜と伝えられている」「〜とのこと」「〜と報じられている」
「〜と発表された」「このレビュアーも」「記事では」「報道によれば」
「〜らしい」（伝聞ニュアンスのもの）「〜みたい」（情報を受け取った感じのもの）
記事・レビュー・情報源・ニュース・媒体の存在を暗示する言葉すべて

━━ 翻訳文にしない（最重要） ━━
元の情報が英語でも、英語から翻訳した文章に絶対にしない。
日本語ネイティブのKPOPファンが最初から日本語で考えて書いた文として仕上げる。
以下は翻訳臭が出る典型パターンなので絶対使わない：
・「〜について言えば」「〜という観点から」「〜に関して言うと」（英語の regarding / in terms of の直訳）
・「彼/彼女は〜だ」を主語に置いた固い文（日本語では主語は省くのが自然）
・「〜することができる」（→「〜できる」に縮める）
・長い連体修飾節を前置した英語的な語順
・「そして」「しかし」「また」を文頭に連続して置く英語的な接続詞多用
・カタカナ語の後に「〜ということ」「〜という事実」と続ける持って回った言い方
・意味が同じでも英語に対応する単語を選びがちな語彙（例：「パフォーマンス」→「ステージ」「披露」など日本のKPOPファンが実際使う言葉）"""

    # ランキング記事の前処理：正規表現で順位データを抽出
    extracted_rankings: list = []
    if _is_ranking_article(title):
        extracted_rankings = _extract_rankings(article_body)
        if extracted_rankings:
            logger.info("ランキング抽出成功: %d件", len(extracted_rankings))
        else:
            logger.info("ランキング抽出失敗: 通常プロンプトで処理")

    if is_youtube:
        prompt = f"""あなたは生まれも育ちも日本のKPOPオタクです。日本語が母語で、英語記事は読んでいない。
このMV・動画を自分が実際に見て感じたこととして、親しい友達にLINEで送るメッセージを書いてください。
情報源は存在しない。あくまで自分が見た・聴いた体験として完全に一人称で書く。
最初から日本語で考えて書く。英語の文章を翻訳したような文は絶対NG。

【動画情報】
タイトル: {title}
{article_body}

━━ スタイル: {style} ━━
{style_instruction}
{learned_section}
{_COMMON_RULES}"""

    elif extracted_rankings:
        top10 = extracted_rankings[:10]
        has_more = len(extracted_rankings) > 10
        ranking_lines = "\n".join(
            f"{rank}位 {name}（{group}）" for rank, name, group in top10
        )
        more_note = "\n他は元記事へ" if has_more else ""
        prompt = f"""あなたは生まれも育ちも日本のKPOPオタクです。日本語が母語で、英語記事は読んでいない。
このランキングを自分が直接知った・見つけた情報として、友達にLINEで送る感じで書いてください。
「〜と発表された」など受動的・伝聞的な表現は一切使わない。自分の発見・驚きとして語る。
最初から日本語で考えて書く。英語の文章を翻訳したような文は絶対NG。

━━ スタイル: {style} ━━
{style_instruction}
{learned_section}
━━ 出力フォーマット ━━
・1行目: スタイルに合わせた導入一言（絵文字1〜2個OK）
・続き: 下記ランキングデータをそのまま全行出力（順番・内容の変更・省略禁止）
・末尾: スタイルに合った一言（問いかけでなくてもOK）

━━ ルール ━━
・日本語のみ・{max_summary_len}文字以内（厳守）
・ハッシュタグ禁止
・記事・情報源・媒体の存在を匂わせる表現すべて禁止
・出力は本文のみ

━━ ランキングデータ ━━
{ranking_lines}{more_note}"""

    elif is_ja_src:
        # 日本語ソース記事（Kstyle・BARKS 等）
        prompt = f"""あなたは生まれも育ちも日本のKPOPオタクです。
以下の情報は日本語記事からの内容です。翻訳は一切不要。
この出来事・ニュースを自分がリアルタイムで見聞きしたこととして、親しい友達にLINEで送るメッセージを書いてください。
情報源・記事の存在は消す。完全に一人称の体験・感情として書く。
日本語ネイティブらしい自然な話し言葉で。英語的な語順や言い回しは入れない。

【情報】
タイトル: {title}
{article_body}

━━ スタイル: {style} ━━
{style_instruction}
{learned_section}
{_COMMON_RULES}{fetch_note}"""

    else:
        prompt = f"""あなたは生まれも育ちも日本のKPOPオタクです。日本語が母語で、英語記事は読んでいない。
このニュース・出来事を自分が直接体験・発見したかのように、親しい友達にLINEで送るメッセージを書いてください。
記事・レビュー・情報源は存在しない。完全に一人称の自分の意見・感情として書く。
最初から日本語で考えて書く。英語の文章を翻訳したような文は絶対NG。

【情報】
タイトル: {title}
{article_body}

━━ スタイル: {style} ━━
{style_instruction}
{learned_section}
{_COMMON_RULES}{fetch_note}"""

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
                art.post_style = style
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
