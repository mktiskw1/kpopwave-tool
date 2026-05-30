import json
import logging
import os
import random
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

# ランダム表現選択リスト（カテゴリ別）
EXPRESSIONS_VISUAL = [
    "顔が反則すぎる", "目が離せない", "画面から出てきそう",
    "空気が変わる", "オーラが次元違う", "見るたびに新鮮",
    "こんな顔していていいの", "存在が芸術", "光ってる",
    "カメラが好きすぎる", "角度全部勝ち", "現実にいる人じゃない",
    "スクリーンが狭い", "顔面偏差値がバグってる", "この世に存在していいの",
    "重力に逆らってる", "余白がない", "完成されすぎてて怖い",
    "この子だけ解像度が違う", "見るたびに発見がある",
    "引きでも寄りでも勝ち", "表情の作り方が天才",
    "何着ても着こなす", "髪型変えるたびに正解",
    "笑顔が武器すぎる", "目力で全部持っていく",
]

EXPRESSIONS_PERFORMANCE = [
    "この完成度どうなってるの", "練習量が見える", "ライブでこれは無理",
    "鳥肌が止まらない", "どこで覚えたんこの表現力", "全員主役",
    "センターの引力がやばい", "視線が釘付けになる",
    "この子だけ時間軸が違う", "踊りながら歌えるの普通に無理",
    "ステージが似合いすぎる", "生まれながらのパフォーマー",
    "これを無料で見ていいの", "息の合い方が人間じゃない",
    "指先まで気が抜けてない", "音楽と体が一体化してる",
    "表情管理が完璧すぎる", "キレとしなやかさが共存してる",
    "この子のパート毎回鳥肌", "感情の乗せ方が違う",
    "技術より先に感情が来る", "見てる側が疲れる密度",
    "アドリブっぽいのに完璧", "楽しそうに踊るのが一番強い",
]

EXPRESSIONS_REACTION = [
    "声出た", "二度見した", "スクロール止まった", "これ知らなかった人かわいそう",
    "タイムラインに感謝", "見て後悔しないやつ", "心臓に悪い",
    "見終わった後に放心した", "しばらく他のこと考えられない",
    "これ見た後の現実がつらい", "沼に落ちる音がした",
    "好きになる瞬間ってこういうことか", "また好きが更新された",
    "語彙力が死んだ", "言葉が追いつかない",
    "画面前で固まった", "気づいたら3回見てた",
    "これ布教していいですか", "周りに布教したくなる",
    "一人で抱えるには重い", "好きすぎて語彙力が消えた",
    "見終わった瞬間また見たくなった", "これが無料でいいの本当に",
    "何も言えなくて夏", "感想が出てこないタイプのやつ",
    "ロスになる前に覚悟してください", "これが沼の入り口です",
]

EXPRESSIONS_MONOLOGUE = [
    "なんで知らなかったんだろ", "もっと早く教えてほしかった",
    "布教していい？", "これ好きな人と話したい",
    "一人で抱えるには重い", "誰かに言いたかっただけ", "見てよかった",
]

EXPRESSIONS_QUIRKY = [
    "何も言えなくて夏", "もう優勝でいいよ", "殿堂入りってこういうこと",
    "好きって言っていいですか", "待って心の準備ができてない",
    "これ現実？夢？", "審査員全員10点出してください",
]

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
    all_hooks = [h for hooks in _HOOK_PATTERNS_BY_TIME.values() for h in hooks]
    hooks_text = "\n".join(f"・{h}" for h in all_hooks)
    logger.info("[summarize_article] hooks_text=%r", hooks_text)
    time_hint  = _get_time_style_hint()

    # ── 今回使う表現をランダムに選択 ────────────────────────────────────────
    picked_monologue   = random.choice(EXPRESSIONS_MONOLOGUE)
    picked_visual      = random.choice(EXPRESSIONS_VISUAL)
    picked_performance = random.choice(EXPRESSIONS_PERFORMANCE)
    picked_reaction    = random.choice(EXPRESSIONS_REACTION)
    picked_quirky      = random.choice(EXPRESSIONS_QUIRKY)
    EXPRESSION_PICK_SECTION = (
        f"━━ 今回必ず使う表現 ━━\n"
        f"以下からいずれか1〜2つを本文に自然に組み込むこと：\n"
        f"・{picked_monologue}（独り言系・最優先）\n"
        f"・{picked_visual}（ビジュアル系）\n"
        f"・{picked_performance}（パフォーマンス系）\n"
        f"・{picked_reaction}（感情・反応系）\n"
        f"・{picked_quirky}（ちょっとズレた系）\n"
        f"【必須】上記の「今回必ず使う表現」を本文に自然に組み込むこと"
    )
    logger.info(
        "[summarize_article] 今回の表現: 独り言=%r 視覚=%r パフォーマンス=%r 反応=%r ズレ=%r",
        picked_monologue, picked_visual, picked_performance, picked_reaction, picked_quirky,
    )

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
        f"以下のフック候補の中から、記事の内容に最も合うものを1つ選んで投稿文の1行目に使うこと。\n"
        f"どれも内容に合わない場合は自分でフックを考えてよい。\n"
        f"ただし必ずフックで始めること。フックより前に何も置かない。\n\n"
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
        f"━━ 使える表現集（積極的に活用すること） ━━\n"
        f"以下の表現を記事の内容に合わせて積極的かつランダムに使うこと。\n"
        f"毎回同じ表現を繰り返さず、毎回違う表現を選ぶこと。\n"
        f"前の投稿と違うカテゴリの表現を選ぶこと。\n"
        f"特に「独り言系」は人間っぽさが出るので積極的に使うこと。\n"
        f"中でも「誰かに言いたかっただけ」「布教していい？」は優先的に採用すること。\n\n"
        f"■ ビジュアル・外見\n"
        f"顔が反則すぎる／目が離せない／画面から出てきそう／"
        f"空気が変わる／オーラが次元違う／見るたびに新鮮／"
        f"こんな顔していていいの／存在が芸術／光ってる／"
        f"カメラが好きすぎる／角度全部勝ち／現実にいる人じゃない／"
        f"スクリーンが狭い／顔面偏差値がバグってる／この世に存在していいの／"
        f"重力に逆らってる／余白がない／完成されすぎてて怖い／"
        f"この子だけ解像度が違う／見るたびに発見がある／"
        f"引きでも寄りでも勝ち／表情の作り方が天才／"
        f"何着ても着こなす／髪型変えるたびに正解／"
        f"笑顔が武器すぎる／目力で全部持っていく\n\n"
        f"■ パフォーマンス・実力\n"
        f"この完成度どうなってるの／練習量が見える／ライブでこれは無理／"
        f"鳥肌が止まらない／どこで覚えたんこの表現力／全員主役／"
        f"センターの引力がやばい／視線が釘付けになる／"
        f"この子だけ時間軸が違う／踊りながら歌えるの普通に無理／"
        f"ステージが似合いすぎる／生まれながらのパフォーマー／"
        f"これを無料で見ていいの／息の合い方が人間じゃない／"
        f"指先まで気が抜けてない／音楽と体が一体化してる／"
        f"表情管理が完璧すぎる／キレとしなやかさが共存してる／"
        f"この子のパート毎回鳥肌／感情の乗せ方が違う／"
        f"技術より先に感情が来る／見てる側が疲れる密度／"
        f"アドリブっぽいのに完璧／楽しそうに踊るのが一番強い\n\n"
        f"■ 楽曲・MV\n"
        f"サビで毎回やられる／イントロから引き込まれる／リピートが止まらない／"
        f"世界観が完璧すぎる／この曲に出会えてよかった／また名曲生まれた／"
        f"何回聴いても飽きない／歌詞が刺さりすぎる／MVの世界観に入り込んだ／"
        f"一曲でこんなに感情動かされるの／このメロディー反則／耳から離れない／"
        f"リリースのたびに超えてくる／これが最高傑作は毎回更新される／"
        f"曲の世界観に完全に入り込んでる／映像と音楽が喧嘩してない／"
        f"色使いが頭おかしい（褒め）／カット割りのセンスが好き／"
        f"衣装がMVの一部になってる／背景まで全部計算されてる／"
        f"この曲調でこのダンスは反則／タイトルと中身が完璧にリンクしてる／"
        f"尺が短くて逆に惜しい／フルで聴いたら印象変わった\n\n"
        f"■ 感情・反応\n"
        f"声出た／二度見した／スクロール止まった／これ知らなかった人かわいそう／"
        f"タイムラインに感謝／見て後悔しないやつ／心臓に悪い／"
        f"見終わった後に放心した／しばらく他のこと考えられない／"
        f"これ見た後の現実がつらい／沼に落ちる音がした／"
        f"好きになる瞬間ってこういうことか／また好きが更新された／"
        f"語彙力が死んだ／言葉が追いつかない／"
        f"画面前で固まった／気づいたら3回見てた／"
        f"これ布教していいですか／周りに布教したくなる／"
        f"一人で抱えるには重い／好きすぎて語彙力が消えた／"
        f"見終わった瞬間また見たくなった／これが無料でいいの本当に／"
        f"何も言えなくて夏／感想が出てこないタイプのやつ／"
        f"ロスになる前に覚悟してください／これが沼の入り口です\n\n"
        f"■ グループ・メンバーへの愛\n"
        f"このグループ本当に外れない／誰がセンターでも成立する／"
        f"こんなグループいていいの／末永く応援したい／"
        f"デビューから目が離せない／これからどこまで行くんだろ／"
        f"まだ本気出してないでしょ／ポテンシャルが怖い／"
        f"ファンになってよかった／推してて誇らしい／"
        f"こんなに安定して好きでいられるグループ珍しい／"
        f"新曲出るたびに信頼が増す／期待を裏切らないのが一番すごい／"
        f"この子の良さに気づくのに時間かかった人ほど沼深い／"
        f"ファン歴関係なく刺さる／古参も新規も関係ない強さ／"
        f"応援してる自分を褒めたくなる／ずっと好きでいたい\n\n"
        f"■ 驚き・衝撃系\n"
        f"え、待って／ちょっとちょっと／うそでしょ／"
        f"反則すぎる／こんなのあり？／なんなんこれ／どういうこと\n\n"
        f"■ 感動・共感系\n"
        f"わかってしまう／刺さりすぎた／心に来た／"
        f"ずっと見てられる／何回でも見れる／これが好きなんだよな／たまらん\n\n"
        f"■ 独り言系（最優先で使うこと）\n"
        f"なんで知らなかったんだろ／もっと早く教えてほしかった／"
        f"布教していい？／これ好きな人と話したい／"
        f"一人で抱えるには重い／誰かに言いたかっただけ／見てよかった\n\n"
        f"■ ちょっとズレた表現（たまに使う）\n"
        f"何も言えなくて夏／もう優勝でいいよ／殿堂入りってこういうこと／"
        f"好きって言っていいですか／待って心の準備ができてない／"
        f"これ現実？夢？／審査員全員10点出してください\n\n"
        f"━━ フック後の展開（必須） ━━\n"
        f"・フックの後は必ず具体的な内容（グループ名・曲名・出来事など）を続けること\n"
        f"・悪い例：「待って、これやばい。最高。」→ フックだけで終わっている\n"
        f"・良い例：「待って、これやばい。aespaの新曲、何回聴いても飽きない。」\n\n"
        f"━━ 表現バリエーション（必須） ━━\n"
        f"・「〜すぎる」「〜に震えた」「〜が可愛すぎる」などの決まり文句を毎回使わない\n"
        f"・毎回違う言葉・角度・切り口で表現すること\n\n"
        f"━━ あいまいな感情語（単独使用禁止） ━━\n"
        f"以下の言葉だけで文章を終わらせない：\n"
        f"「やばい」「すごい」「最高」「可愛すぎる」「震えた」\n"
        f"使う場合は必ず「何が」「どのように」かを具体的に続けること。\n"
        f"・悪い例：「これガチなんですけど、やばい。」\n"
        f"・良い例：「これガチなんですけど、aespaの振り付けってどこ切り取っても絵になる。こんなグループいる？」"
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
            f"・出力は投稿文のみ（前置き・説明不要）\n\n"
            f"{EXPRESSION_PICK_SECTION}"
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
            f"{EXPRESSION_PICK_SECTION}\n\n"
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
            f"・{body_max}文字以内（厳守）\n"
            f"・絵文字なし・ハッシュタグなし・URLなし\n"
            f"・伝聞表現禁止\n"
            f"・投稿文は必ず日本語のみで書くこと（韓国語禁止）\n"
            f"・グループ名・メンバー名はアルファベット表記でOK\n"
            f"・出力は本文のみ\n\n"
            f"{EXPRESSION_PICK_SECTION}\n\n"
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
            f"{EXPRESSION_PICK_SECTION}\n\n"
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
                max_tokens=400,
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
