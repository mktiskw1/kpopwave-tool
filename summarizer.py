import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import anthropic
import requests

from database import Article, BuzzPost, Setting, db

logger = logging.getLogger(__name__)

THREADS_MAX = 500
BODY_MAX_VIDEO   = 50   # 動画投稿の文字数上限
BODY_MAX_ARTICLE = 150  # 記事投稿の文字数上限
BODY_MAX_RETRIES = 3    # 超過時の再生成試行回数

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

# フックパターン（時間帯別・各3個）
_HOOK_PATTERNS_BY_TIME: dict[str, list[str]] = {
    "朝": ["知らないと損する、", "これ見て損はない。", "まだ知らない人いる？"],
    "昼": ["わかる人にはわかる、", "これ好きな人と友達になりたい。", "なんでみんな話題にしないんだろ。"],
    "夜": ["待って、これやばい。", "これガチなんですけど、", "嘘みたいな本当の話なんですけど、"],
}

# スタイルごとのトーン
_STYLE_PROMPTS: dict = {
    "つぶやき型": {
        "tone": "感情をぶつける・驚き・発見を1〜2行に凝縮。問いかけは不要。勢いとテンションが伝わる短さで。",
    },
    "情報型": {
        "tone": "有益な発見・驚きのファクトを1〜2行で届ける。グループ名・曲名など固有名詞は正確に。",
    },
    "体験談型": {
        "tone": "感情を最初にぶつけて、グループ名・曲名・イベント名と感情表現だけで1〜2行まとめる。",
    },
    "バズり型": {
        "tone": "強烈なフックで引き込む。興奮と勢いを凝縮。熱量MAX。大げさなくらいでOK。",
    },
}


def _get_time_style_hint() -> str:
    """現在のJST時刻に応じた投稿スタイルヒントを返す。"""
    JST = timezone(timedelta(hours=9))
    h = datetime.now(JST).hour
    if 6 <= h <= 9:
        return "【朝の投稿（6〜9時）】学び系・今日から使える情報として届ける。「知ってた？」「今日のKPOP情報」トーンで。"
    if 11 <= h <= 13:
        return "【昼の投稿（11〜13時）】共感系・あるある・短め。サクッと読めてニヤッとできる感じで。"
    if 20 <= h <= 23:
        return "【夜の投稿（20〜23時）】感情系・ストーリー・深い共感。「泣けるんだけど」「ちょっと聞いて」系のトーンで。"
    return ""


def _get_time_hooks(scheduled_at: str | None = None) -> list[str]:
    """JST時刻に応じたフックパターンリストを返す。scheduled_atがあればその時刻で判定、なければ現在時刻。"""
    JST = timezone(timedelta(hours=9))
    if scheduled_at:
        try:
            h = datetime.fromisoformat(scheduled_at).hour
            logger.info("[_get_time_hooks] scheduled_at=%r → hour=%d (JST)", scheduled_at, h)
        except ValueError as e:
            h = datetime.now(JST).hour
            logger.warning("[_get_time_hooks] scheduled_at=%r 解析失敗(%s) → 現在時刻 hour=%d を使用", scheduled_at, e, h)
    else:
        h = datetime.now(JST).hour
        logger.info("[_get_time_hooks] scheduled_at=None → 現在時刻 hour=%d (JST) を使用", h)

    if 6 <= h <= 9:
        slot, hooks = "朝", _HOOK_PATTERNS_BY_TIME["朝"]
    elif 11 <= h <= 13:
        slot, hooks = "昼", _HOOK_PATTERNS_BY_TIME["昼"]
    else:
        slot, hooks = "夜", _HOOK_PATTERNS_BY_TIME["夜"]

    logger.info("[_get_time_hooks] hour=%d → %sフック選択: %s", h, slot, hooks)
    return hooks

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


# 画像除外ドメイン（Googleデフォルト画像・プロフィール写真など）
_EXCLUDE_IMAGE_DOMAINS = (
    "gstatic.com",
    "news.google.com",
    "googleusercontent.com",
    "lh3.google.com",
)
# URLに含まれるサイズヒントから64px以下の小画像を検出するパターン
_SMALL_SIZE_HINTS = (
    "=s16", "=s24", "=s32", "=s48", "=s64",
    "/s16/", "/s24/", "/s32/", "/s48/", "/s64/",
    "/s16-", "/s24-", "/s32-", "/s48-", "/s64-",
    "16x16", "24x24", "32x32", "48x48", "64x64",
)


def _is_valid_image_url(url: str) -> bool:
    """保存・投稿に使用可能な画像URLか判定する（threads_api._is_valid_image_url と同一基準）。"""
    if not url or not url.startswith("http"):
        return False
    if any(d in url for d in _EXCLUDE_IMAGE_DOMAINS):
        return False
    low = url.lower()
    if any(h in low for h in _SMALL_SIZE_HINTS):
        return False
    return True


def _extract_images_from_html(html: str) -> list:
    """HTMLからog:imageと記事本文内の画像URLを最大4件抽出する。
    Googleデフォルト画像・64px以下の小画像は除外する。"""
    images = []

    # og:image（最優先：記事のメイン画像）
    og_match = re.search(
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']'
        r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        html, re.IGNORECASE,
    )
    if og_match:
        og_url = og_match.group(1) or og_match.group(2) or ""
        if _is_valid_image_url(og_url):
            images.append(og_url)
        elif og_url:
            logger.debug("og:image 除外: %s", og_url)

    # article/main ブロック内の <img src>
    for section_tag in ("article", "main"):
        m = re.search(rf"<{section_tag}[^>]*>([\s\S]*?)</{section_tag}>", html, re.IGNORECASE)
        if m:
            block = m.group(1)
            for img_url in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', block, re.IGNORECASE):
                if not _is_valid_image_url(img_url) or img_url in images:
                    continue
                low = img_url.lower()
                # SVG・1px追跡画像・アイコン系を除外
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


def _save_error(app, article_id: int, message: str) -> None:
    """エラーメッセージをDBに保存するヘルパー。"""
    with app.app_context():
        art = db.session.get(Article, article_id)
        if art:
            art.error_message = message
            db.session.commit()


def summarize_article(app, article_id: int, style: str = "つぶやき型", scheduled_at: str | None = None) -> bool:
    """1 記事の日本語投稿テキストを生成して DB に保存する。成功なら True。"""
    logger.info("[summarize_article] article=%d style=%r scheduled_at=%r", article_id, style, scheduled_at)
    style_conf = _STYLE_PROMPTS.get(style, _STYLE_PROMPTS["つぶやき型"])
    style_tone = style_conf["tone"]
    hooks_text = "\n".join(f"・{h}" for h in _get_time_hooks(scheduled_at))
    logger.info("[summarize_article] hooks_text=%r", hooks_text)
    time_hint  = _get_time_style_hint()

    # ── buzz_posts から AI 分析済みの tips を最大5件取得 ──────────────────
    with app.app_context():
        learned_hints = Setting.get("learned_style_hints", "")
        buzz_tips: list[str] = []
        try:
            buzz_rows = (
                BuzzPost.query
                .filter(BuzzPost.analysis.isnot(None))
                .order_by(BuzzPost.created_at.desc())
                .limit(5)
                .all()
            )
            for row in buzz_rows:
                try:
                    parsed = json.loads(row.analysis)
                    tip = parsed.get("tips", "")
                    if tip:
                        buzz_tips.append(f"・{tip}")
                except Exception:
                    pass
        except Exception:
            pass

    buzz_section = ""
    if buzz_tips:
        buzz_section += "\n━━ バズり投稿から学んだコツ（参考にすること） ━━\n" + "\n".join(buzz_tips)
    if learned_hints:
        buzz_section += f"\n━━ 学習済みインサイト ━━\n{learned_hints}"

    # ── 記事情報取得 ───────────────────────────────────────────────────────
    with app.app_context():
        article = db.session.get(Article, article_id)
        if not article:
            logger.error("article id=%d が見つかりません", article_id)
            return False
        title         = article.title
        stored_body   = (article.raw_content or "")[:3000]
        url           = article.url
        feed_source   = article.feed_source or ""
        thumbnail_url = article.thumbnail_url or ""
        is_ja_src     = _is_japanese_source(feed_source)
        content_type  = article.content_type or "article"

    is_video_post = (content_type == "video")
    body_max = BODY_MAX_VIDEO if is_video_post else BODY_MAX_ARTICLE

    # ── APIキー確認 ────────────────────────────────────────────────────────
    api_key = _get_api_key(app)
    with app.app_context():
        db_key  = Setting.get("anthropic_api_key", "")
    env_key = os.getenv("ANTHROPIC_API_KEY", "")
    logger.info(
        "Anthropic APIキー確認 — DB: %s / ENV: %s",
        f'"{db_key[:12]}..." ({len(db_key)}文字)' if db_key else "未設定",
        f'"{env_key[:12]}..." ({len(env_key)}文字)' if env_key else "未設定",
    )
    if not api_key:
        msg = "Anthropic APIキーが未設定です。管理画面 → 設定 → Anthropic API キーを登録してください。"
        logger.error(msg)
        _save_error(app, article_id, msg)
        return False
    if api_key.endswith("...") or api_key in ("sk-ant-...", "your-api-key-here"):
        msg = f"Anthropic APIキーがプレースホルダーのままです ({api_key!r})。管理画面 → 設定 → Anthropic API キーに本物のキーを入力してください。"
        logger.error(msg)
        _save_error(app, article_id, msg)
        return False

    # ── コンテンツ取得 ─────────────────────────────────────────────────────
    is_youtube = "youtube.com/watch" in url or "youtu.be/" in url

    if is_youtube:
        article_body   = stored_body
        article_images = [thumbnail_url] if thumbnail_url else []
        fetch_ok       = True
    else:
        fresh_body, article_images, fetch_ok = _fetch_article_page(url)
        article_body = fresh_body if fetch_ok else stored_body

    # ── グループ名検出 ─────────────────────────────────────────────────────
    group_name = _detect_group_name(feed_source, title)
    group_hint = f"・「{group_name}」の名前を自然に含めること" if group_name else ""

    # ── 共通ブロック ───────────────────────────────────────────────────────
    HOOK_SECTION = (
        f"━━ 冒頭フック（厳守・最重要） ━━\n"
        f"【必須】必ず以下のフックのいずれかで文章を始めること。\n"
        f"投稿文の1行目はフックの1文だけ。フックより前に何も置かない。\n"
        f"フックなしで始まる投稿文は絶対に生成しないこと。\n\n"
        f"{hooks_text}\n"
        f"※「〇〇」部分は実際のグループ名・曲名・イベント名に置き換えること"
    )

    STRUCTURE_SECTION = (
        "━━ 投稿構造 ━━\n"
        "1行目：フックで引き込む\n"
        "↓本文：グループ名・キーワードを自然に含める\n"
        "↓末尾（任意）：「知ってた？」「どう思う？」などの問いかけ（なくてもOK）"
    )

    COMMON_RULES = (
        f"━━ 絶対ルール ━━\n"
        f"・{body_max}文字以内（厳守）\n"
        f"・絵文字なし\n"
        f"・ハッシュタグなし\n"
        f"・URLなし\n"
        f"・「〜です」「〜ます」禁止。自然な口語体で\n"
        f"・伝聞表現禁止：「〜とのこと」「〜と報じられている」「記事によると」など一切不可\n"
        f"・説明的書き出し禁止：「〜なんだけど」「〜してたんだけど」「ちょっと聞いて」など\n"
        f"・具体的な描写禁止（ダンス・歌声・衣装など直接確認できないもの）\n"
        f"{group_hint}\n"
        f"・出力は投稿文のみ（前置き・説明・スタイル名不要）\n\n"
        f"━━ 言語ルール ━━\n"
        f"・投稿文は必ず日本語のみで書くこと\n"
        f"・韓国語・英語は使わない\n"
        f"・グループ名・メンバー名はアルファベット表記でOK（例：aespa、WINTER）\n"
        f"・曲名もアルファベット表記でOK（例：LEMONADE、WDA）\n"
        f"・それ以外の本文は全て日本語で書くこと\n\n"
        f"━━ 使用禁止ワード ━━\n"
        f"・「興奮」「止まらん」「頭おかしい」→ 誤解を生むため絶対使わない\n"
        f"・「〜やで」「〜やわ」「〜やん」→ 関西弁は使わない\n"
        f"・「マジで」を連発しない（1投稿に1回まで）\n"
        f"・「〜してる。止まらん。」のような語尾の繰り返しパターン禁止\n\n"
        f"━━ 代わりに使う表現（参考） ━━\n"
        f"・「なんか次元が違う」\n"
        f"・「これはやばい」\n"
        f"・「何回見ても飽きない」\n"
        f"・「ずっと見てられる」\n"
        f"・「なんでこんなに強いんだろ」"
    )

    time_section = f"\n━━ 時間帯スタイル ━━\n{time_hint}" if time_hint else ""

    # ── ランキング記事判定・抽出 ───────────────────────────────────────────
    extracted_rankings: list = []
    if _is_ranking_article(title):
        extracted_rankings = _extract_rankings(article_body)
        if extracted_rankings:
            logger.info("ランキング抽出成功: %d件", len(extracted_rankings))
        else:
            logger.info("ランキング抽出失敗: 通常プロンプトで処理")

    # ── Step1 プロンプト組み立て ───────────────────────────────────────────
    PERSONA = (
        "あなたは25歳の日本人女性。KPOPオタク歴8年。"
        "推しはaespa。普段からThreadsでKPOP情報を発信している。"
        "友達にLINEで送るような感覚で書く。"
        "テンションは高すぎず・低すぎず。"
        "語尾は「〜だわ」「〜やん」などの関西弁は使わない。"
        "説明しすぎない。感じたことをそのまま書く。"
    )

    if is_video_post:
        step1_prompt = (
            f"{PERSONA}\n"
            f"この動画を見た瞬間の一言リアクションをそのまま書く。動画の内容説明は絶対にしない。感情だけ。\n\n"
            f"【動画タイトル】{title}\n\n"
            f"{HOOK_SECTION}\n\n"
            f"━━ 出力ルール ━━\n"
            f"・フック（1行目）＋一言だけ。それ以上は書かない\n"
            f"・{body_max}文字以内（厳守）\n"
            f"・絵文字なし・ハッシュタグなし・URLなし\n"
            f"・日本語のみ（グループ名・曲名はアルファベットOK）\n"
            f"・動画が主役なので説明不要。短く言い切る\n"
            f"・例：「待って、これやばい。」「何回見ても飽きない。」「なんかすごい。」「なんで次元が違うんだろ。」\n"
            f"・出力は投稿文のみ（前置き・説明不要）"
        )

    elif is_youtube:
        step1_prompt = (
            f"{PERSONA}\n"
            f"動画の存在を知って「やばい」と思っている自分として書く。内容を詳しく説明せず、グループ名・動画タイトルと感情表現だけで伝える。\n\n"
            f"【動画情報】\nタイトル: {title}\n{article_body[:1000]}\n\n"
            f"{HOOK_SECTION}\n\n"
            f"{STRUCTURE_SECTION}\n\n"
            f"━━ スタイル: {style} ━━\n{style_tone}"
            f"{time_section}\n"
            f"{buzz_section}\n\n"
            f"{COMMON_RULES}"
        )

    elif extracted_rankings:
        top10 = extracted_rankings[:10]
        has_more = len(extracted_rankings) > 10
        ranking_lines = "\n".join(
            f"{rank}位 {name}（{group}）" for rank, name, group in top10
        )
        more_note = "\n他は元記事へ" if has_more else ""
        step1_prompt = (
            f"{PERSONA}\n"
            f"このランキングを自分が直接見つけた情報として書く。「〜と発表された」など伝聞表現は一切不可。\n\n"
            f"{HOOK_SECTION}\n\n"
            f"━━ 出力フォーマット ━━\n"
            f"・1行目: フックで始まる導入一言\n"
            f"・続き: 下記ランキングデータをそのまま全行出力（省略禁止）\n"
            f"・末尾: 感情の一言\n\n"
            f"━━ ルール ━━\n"
            f"・絵文字なし・ハッシュタグなし・URLなし\n"
            f"・伝聞表現禁止\n"
            f"・出力は本文のみ\n\n"
            f"━━ ランキングデータ ━━\n{ranking_lines}{more_note}"
        )

    else:
        step1_prompt = (
            f"{PERSONA}\n"
            f"このニュース・出来事を自分が直接体験・発見したかのように書く。記事・情報源は存在しない。完全に一人称の意見・感情として書く。\n\n"
            f"【情報】\nタイトル: {title}\n{article_body[:2000]}\n\n"
            f"{HOOK_SECTION}\n\n"
            f"{STRUCTURE_SECTION}\n\n"
            f"━━ スタイル: {style} ━━\n{style_tone}"
            f"{time_section}\n"
            f"{buzz_section}\n\n"
            f"{COMMON_RULES}"
        )

    # ── Claude API 呼び出し（2段階生成） ───────────────────────────────────
    try:
        client = anthropic.Anthropic(api_key=api_key)
        summary_text = ""

        # Step1: 初期生成
        msg1 = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": step1_prompt}],
        )
        step1_text = msg1.content[0].text.strip()
        logger.info("Step1生成 (%d文字): article=%d", len(step1_text), article_id)

        # Step2: 人間っぽく変換（リトライ付き）
        if is_video_post:
            step2_base = (
                "この文章から余計な説明を全部削って、感情だけ残してください。\n"
                "【厳守】1行目のフックフレーズは絶対に変えないこと。そのまま残す。\n"
                "一言で言い切る。フック+感情の一言だけ。\n"
                f"絵文字なし・ハッシュタグなし・URLなし。必ず{body_max}文字以内。\n"
                "出力は変換後の文章のみ。\n\n"
            )
        else:
            step2_base = (
                "この文章を25歳の日本人女性が友達にLINEで送るメッセージに変換してください。\n"
                "【厳守】1行目のフックフレーズは絶対に変えないこと。そのまま残す。\n"
                "・説明文を感情に変える\n"
                "・長い文を短く切る\n"
                "・AIっぽい言い回しを口語に変える\n"
                f"・絵文字なし・タグなし・URLなし。必ず{body_max}文字以内。\n"
                "出力は変換後の文章のみ。\n\n"
            )

        for attempt in range(1, BODY_MAX_RETRIES + 1):
            step2_prompt = step2_base + step1_text
            msg2 = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": step2_prompt}],
            )
            summary_text = msg2.content[0].text.strip()
            if len(summary_text) <= body_max:
                break
            logger.warning(
                "Step2 本文 %d文字（上限 %d）→ 再生成 %d/%d: article=%d",
                len(summary_text), body_max, attempt, BODY_MAX_RETRIES, article_id,
            )
        else:
            summary_text = summary_text[:body_max - 1] + "…"
            logger.warning("再生成上限到達、強制切り詰め: article=%d", article_id)

        # ── DB保存（ハッシュタグ・URLなし） ──────────────────────────────────
        post_text = summary_text

        with app.app_context():
            art = db.session.get(Article, article_id)
            if art:
                art.summary       = post_text
                art.post_style    = style
                art.error_message = None
                if article_images:
                    art.image_urls = json.dumps(article_images, ensure_ascii=False)
                db.session.commit()

        logger.info(
            "Summarized article %d (%d文字, %d images, fetch=%s, group=%s)",
            article_id, len(post_text), len(article_images), fetch_ok, group_name or "不明",
        )
        return True

    except Exception as exc:
        logger.error("Summarize error for article %d: %s", article_id, exc, exc_info=True)
        _save_error(app, article_id, f"{type(exc).__name__}: {exc}")
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
