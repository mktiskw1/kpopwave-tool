import json
import logging
import os
import re
from urllib.parse import urlparse

import anthropic
import requests

from database import Article, Setting, db

logger = logging.getLogger(__name__)

THREADS_MAX = 500
BODY_MAX = 50          # 本文（ハッシュタグ・URL除く）の上限文字数
BODY_MAX_RETRIES = 3   # 超過時の再生成試行回数
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

# スタイルごとの投稿トーン指示（1〜2行の超短い投稿）
_STYLE_PROMPTS: dict = {
    "つぶやき型": (
        "感情ワードで始めて、驚き・発見・感情を1〜2行に凝縮。"
        "情報は最小限。問いかけ不要。勢いとテンションが伝わる短さで。"
    ),
    "情報型": (
        "「ねえ聞いて」「知ってた？」などで始めてKPOP情報を1〜2行で届ける。"
        "グループ名・曲名など固有名詞は正確に。口語体で簡潔に。"
    ),
    "体験談型": (
        "「これ鳥肌立った」「泣いた」など感情を最初にぶつけて、"
        "グループ名・曲名・イベント名と感情表現だけで1〜2行まとめる。"
        "「〜してたんだけど」「〜見たんだけど」のような説明的書き出し禁止。"
    ),
    "バズり型": (
        "「これ絶対見て！」「来たーーー！」レベルの熱量を1〜2行で。"
        "興奮と勢いを凝縮。大げさなくらいでOK。"
    ),
}

# ハッシュタグ生成用KPOPグループリスト（長いグループ名を先に並べて誤検出防止）
_KPOP_GROUPS = [
    "KISS OF LIFE", "Girls Generation", "LE SSERAFIM", "BABYMONSTER",
    "Red Velvet", "MAMAMOO", "BLACKPINK", "NewJeans", "TWICE", "fromis_9",
    "tripleS", "NMIXX", "Kep1er", "KiiiKiii", "MEOVV", "STAYC", "ARTMS",
    "Billlie", "aespa", "ITZY", "ILLIT", "WJSN", "IZNA", "UNIS", "KARA",
    "Apink", "NiziU", "IVE",
]


def _domain(url: str) -> str:
    host = urlparse(url).netloc
    return host.removeprefix("www.")


def _get_api_key(app) -> str:
    with app.app_context():
        key = Setting.get("anthropic_api_key", "")
    return key or os.getenv("ANTHROPIC_API_KEY", "")


def _detect_group_name(feed_source: str, title: str) -> str:
    """feed_sourceまたはtitleからKPOPグループ名を検出して返す。"""
    text = (feed_source + " " + title)
    text_lower = text.lower()
    for group in _KPOP_GROUPS:
        if group.lower() in text_lower:
            return group
    return ""


def _build_hashtags(group_name: str, is_youtube: bool = False) -> str:
    """グループ名・KPOP・韓国音楽タグを組み合わせたハッシュタグ文字列を生成する（3〜5個）。"""
    tags = []
    if group_name:
        # スペースとハイフンを除去してハッシュタグ化（fromis_9 はアンダースコア保持）
        tag = "#" + re.sub(r'[\s\-]', '', group_name)
        tags.append(tag)
    tags.append("#KPOP")
    tags.append("#韓国音楽")
    tags.append("#女性アイドル")          # グループ不明でも最低3タグ保証
    if is_youtube:
        tags.append("#KpopMV")
    return " ".join(tags)


def _fetch_article_page(url: str) -> tuple:
    """
    元記事URLからHTMLを取得し (text, image_urls, success) を返す。
    text: 本文テキスト（最大8000文字）
    image_urls: 記事の画像URL一覧（最大4件）
    success: 取得成功フラグ
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
            return "", [], False

        html = r.text

        # ── 画像URL抽出 ────────────────────────────────────────────
        images = _extract_images_from_html(html)

        # ── テキスト抽出 ───────────────────────────────────────────
        # スクリプト・スタイルを事前除去
        html_clean = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.IGNORECASE)

        for tag in ("article", "main", "body"):
            m = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", html_clean, re.IGNORECASE)
            if m:
                block = m.group(1)
                break
        else:
            block = html_clean

        text = re.sub(r"<[^>]+>", " ", block)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) < 100:
            return "", images, False

        logger.info("元記事取得成功 (%d chars, %d images): %s", len(text), len(images), url)
        return text[:8000], images, True

    except Exception as exc:
        logger.warning("元記事取得失敗: %s — %s", url, exc)
        return "", [], False


def _extract_images_from_html(html: str) -> list:
    """HTMLからog:imageと記事本文内の画像URLを最大4件抽出する。"""
    images = []

    # og:image（最優先：記事のメイン画像）
    og_match = re.search(
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']'
        r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        html, re.IGNORECASE,
    )
    if og_match:
        og_url = og_match.group(1) or og_match.group(2) or ""
        if og_url.startswith("http"):
            images.append(og_url)

    # article/main ブロック内の <img src>
    for section_tag in ("article", "main"):
        m = re.search(rf"<{section_tag}[^>]*>([\s\S]*?)</{section_tag}>", html, re.IGNORECASE)
        if m:
            block = m.group(1)
            for img_url in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', block, re.IGNORECASE):
                if img_url.startswith("http") and img_url not in images:
                    # SVG・1px追跡画像・アイコン系を除外
                    low = img_url.lower()
                    if any(x in low for x in (".svg", "1x1", "pixel", "tracking", "avatar", "icon", "logo")):
                        continue
                    images.append(img_url)
                    if len(images) >= 4:
                        break
            break

    return images[:4]


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
        title        = article.title
        stored_body  = (article.raw_content or "")[:3000]
        url          = article.url
        feed_source  = article.feed_source or ""
        thumbnail_url = article.thumbnail_url or ""
        is_ja_src    = _is_japanese_source(feed_source)

    api_key = _get_api_key(app)
    if not api_key:
        logger.error("Anthropic API key not configured")
        return False

    # ── コンテンツ取得 ─────────────────────────────────────────────
    is_youtube = "youtube.com/watch" in url or "youtu.be/" in url

    if is_youtube:
        article_body  = stored_body
        article_images = [thumbnail_url] if thumbnail_url else []
        fetch_ok      = True
        fetch_note    = ""
    else:
        fresh_body, article_images, fetch_ok = _fetch_article_page(url)
        if fetch_ok:
            article_body = fresh_body
            fetch_note   = ""
        else:
            article_body = stored_body
            fetch_note   = "\n・元記事にアクセスできていない場合は末尾に「詳細は元記事で」と一言だけ添える"

    # ── ハッシュタグ生成 ───────────────────────────────────────────
    group_name    = _detect_group_name(feed_source, title)
    hashtag_text  = _build_hashtags(group_name, is_youtube=is_youtube)
    source_part   = SOURCE_PREFIX + url
    # ハッシュタグは本文の直後、ソースURLの前に配置
    hashtag_part  = "\n" + hashtag_text
    max_summary_len = THREADS_MAX - len(source_part) - len(hashtag_part) - 5

    # ── 共通ルール ─────────────────────────────────────────────────
    _COMMON_RULES = f"""━━ ルール ━━
・日本語のみ・本文{BODY_MAX}文字以内（ハッシュタグ・URL除く。厳守）
・必ず1〜2行に収める（それ以上絶対に書かない。改行も最小限）
・ハッシュタグ禁止（自動追加されるので絶対に書かない）
・絵文字は1〜3個（自然な位置に。ハートや推しグループに合うもの選んで）
・「〜です」「〜ます」口調禁止。自然な口語体で
・問いかけは入れなくていい
・出力は本文のみ（前置き・スタイル名・説明不要）

━━ 冒頭は「感情」か「対象＋感情」で始める（必須） ━━
1行目は感情・発見・衝撃を直接ぶつけること。説明や状況説明から入らない。

◎ 良い例（1〜2行でこれくらいの短さ）:
・「IVEのFLUライブ映像やばすぎ😭ウォンヨンのビジュアル最強」
・「BLACKPINK新曲来たーーー！これ絶対神曲」
・「えっLE SSERAFIM新アルバム最高すぎ沼った」
・「KISS OF LIFEのMVやばい🔥もう100回見た」

✕ NG例（絶対使わない書き出し）:
・「〜見たんだけどさ」「〜なんだけど」「〜してたんだけど」
・「ちょっと聞いてほしいんだけど」「実はさ〜」「そういえば〜」
・「〜について書くと」「〜を見て思ったんだけど」
・状況説明・前置き・接続詞で始まるもの全般

━━ 絶対禁止 ━━
以下の表現・ニュアンスを一切使わない。
「記事によると」「〜と伝えられている」「〜とのこと」「〜と報じられている」
「〜と発表された」「記事では」「報道によれば」
「〜らしい」（伝聞）「〜みたい」（情報を受け取った感じ）
記事・レビュー・情報源・ニュース・媒体の存在を暗示する言葉すべて
「〜見たんだけどさ」「〜なんだけど」「〜してたんだけど」など説明的書き出しすべて

━━ 具体的な描写は作らない（重要） ━━
ダンス・歌声・衣装・振り付け・表情など「直接確認しないとわからない」具体的な描写は一切書かない。
事実として確認できるもの（グループ名・曲名・アルバム名・イベント名・リリース日）だけを使い、
それ以外は「やばい」「最高」「神」「沼った」「エモい」「鳥肌」「泣ける」などの感情表現で十分。

✕ NG（推測・作り話になるため禁止）:
「ダンスが切れすぎ」「歌声が透き通ってる」「衣装が豪華だった」「振り付けがえぐい」「表情がやばい」
◎ OK（確定情報＋感情のみ）:
「IVEの新曲まじやばすぎ」「LE SSERAFIMのライブ最高だった」「NewJeansのMV沼った」

━━ 翻訳文にしない ━━
日本語ネイティブのKPOPファンが最初から日本語で考えて書いた文として仕上げる。
英語から翻訳したような語順・表現・接続詞の使い方は絶対に避ける。"""

    # ── プロンプト組み立て ─────────────────────────────────────────
    extracted_rankings: list = []
    if _is_ranking_article(title):
        extracted_rankings = _extract_rankings(article_body)
        if extracted_rankings:
            logger.info("ランキング抽出成功: %d件", len(extracted_rankings))
        else:
            logger.info("ランキング抽出失敗: 通常プロンプトで処理")

    if is_youtube:
        prompt = f"""あなたは生まれも育ちも日本のKPOPオタクです。日本語が母語で、英語記事は読んでいない。
この動画の存在を知って興奮している自分として、親しい友達にLINEで送るメッセージを書いてください。
動画の内容を詳しく描写するのではなく、グループ名・曲名・イベント名と感情表現だけで書く。

【動画情報】
タイトル: {title}
{article_body[:1000]}

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
このランキングを自分が直接見つけた情報として、友達にLINEで送る感じで書いてください。
「〜と発表された」など受動的・伝聞的な表現は一切使わない。自分の発見・驚きとして語る。

━━ スタイル: {style} ━━
{style_instruction}
{learned_section}
━━ 出力フォーマット ━━
・1行目: 感情ワードで始まる導入一言（絵文字1〜2個OK）
・続き: 下記ランキングデータをそのまま全行出力（順番・内容の変更・省略禁止）
・末尾: スタイルに合った一言

━━ ルール ━━
・日本語のみ・{max_summary_len}文字以内（厳守）
・ハッシュタグ禁止
・記事・情報源を暗示する表現すべて禁止
・出力は本文のみ

━━ ランキングデータ ━━
{ranking_lines}{more_note}"""

    elif is_ja_src:
        prompt = f"""あなたは生まれも育ちも日本のKPOPオタクです。
以下の情報は日本語記事からの内容です。翻訳は一切不要。
この出来事・ニュースを自分がリアルタイムで見聞きしたこととして、親しい友達にLINEで送るメッセージを書いてください。
情報源・記事の存在は消す。完全に一人称の体験・感情として書く。

【情報】
タイトル: {title}
{article_body[:2000]}

━━ スタイル: {style} ━━
{style_instruction}
{learned_section}
{_COMMON_RULES}{fetch_note}"""

    else:
        prompt = f"""あなたは生まれも育ちも日本のKPOPオタクです。日本語が母語で、英語記事は読んでいない。
このニュース・出来事を自分が直接体験・発見したかのように、親しい友達にLINEで送るメッセージを書いてください。
記事・情報源は存在しない。完全に一人称の自分の意見・感情として書く。

【情報】
タイトル: {title}
{article_body[:2000]}

━━ スタイル: {style} ━━
{style_instruction}
{learned_section}
{_COMMON_RULES}{fetch_note}"""

    # ── Claude API 呼び出し（50文字超過時は再生成） ──────────────────
    try:
        client = anthropic.Anthropic(api_key=api_key)
        summary_text = ""
        for attempt in range(1, BODY_MAX_RETRIES + 1):
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            summary_text = msg.content[0].text.strip()
            if len(summary_text) <= BODY_MAX:
                break
            logger.warning(
                "本文 %d文字（上限 %d）→ 再生成 %d/%d: article=%d",
                len(summary_text), BODY_MAX, attempt, BODY_MAX_RETRIES, article_id,
            )
        else:
            # 全リトライ失敗 → 強制切り詰め
            summary_text = summary_text[:BODY_MAX - 1] + "…"
            logger.warning("再生成上限到達、強制切り詰め: article=%d", article_id)

        # ハッシュタグ + ソースURLを付加
        post_text = summary_text + hashtag_part + source_part

        with app.app_context():
            art = Article.query.get(article_id)
            if art:
                art.summary    = post_text
                art.post_style = style
                art.error_message = None
                # 画像URL（複数枚対応）を保存
                if article_images:
                    art.image_urls = json.dumps(article_images, ensure_ascii=False)
                db.session.commit()

        logger.info(
            "Summarized article %d (%d chars, %d images, fetch=%s, group=%s)",
            article_id, len(post_text), len(article_images), fetch_ok, group_name or "不明",
        )
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
