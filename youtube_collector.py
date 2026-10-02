import logging
import os
import re

import requests

from database import Article, Setting

logger = logging.getLogger(__name__)

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


# KPOPアーティスト以外の洋楽アーティスト明示ブロックリスト（チャンネル名・タイトル小文字一致）
# 検索クエリのアーティスト名と部分一致してしまうケースを手動で除外する
NON_KPOP_BLOCKLIST = frozenset([
    "tyla",
    "allison russell",
    "kara leona",
    "kara kay",
    "kara winger",
])


def _matches_target_artist(title: str, channel_title: str, artist_name: str) -> bool:
    """タイトルまたはチャンネル名に指定アーティスト名が含まれるか確認。
    単語境界マッチングにより 'IVE' が 'live' に誤検出されるのを防ぐ。
    NON_KPOP_BLOCKLIST に一致する場合は False を返す。
    """
    combined = (title + " " + channel_title).lower()

    # 明示ブロックリスト: KPOPと誤検出しやすい洋楽アーティストを先に除外
    for blocked in NON_KPOP_BLOCKLIST:
        if blocked in combined:
            return False

    # アーティスト名が title または channel_title に（単語として）含まれるか確認
    name_lower = artist_name.lower()
    pattern = r'(?<![a-zA-Z0-9_])' + re.escape(name_lower) + r'(?![a-zA-Z0-9_])'
    return bool(re.search(pattern, combined))


def _best_thumbnail(thumbnails: dict) -> str:
    for quality in ("maxres", "standard", "high", "medium", "default"):
        t = thumbnails.get(quality)
        if t and t.get("url"):
            return t["url"]
    return ""


# ── 番組別グループ検索(Music Bank等) ──────────────────────────────────────────
# 「(グループ名) (番組名)」で番組の公式チャンネル内を検索し、該当グループの出演回だけを
# 候補として返す。番組を増やす場合は同じ形の辞書をPROGRAM_CHANNELSに追加すればよい。

PROGRAM_CHANNELS = {
    "music_bank": {
        # @KBSKPOP はインタビュー・フェイスキャム中心でパフォーマンス映像が少ないため、
        # 完全なステージ映像が多い @kbsworldtv を使う(実検索で比較確認済み)。
        "name": "Music Bank",
        "channel_url": "https://www.youtube.com/@kbsworldtv",
        "handle": "kbsworldtv",
    },
    "inkigayo": {
        "name": "Inkigayo",
        "channel_url": "https://www.youtube.com/@SBSKpop",
        "handle": "SBSKpop",
    },
    "music_core": {
        "name": "Show! Music Core",
        "channel_url": "https://www.youtube.com/@MBCkpop",
        "handle": "MBCkpop",
    },
    "m_countdown": {
        # @MnetKpop は古いProduce 101関連コンテンツの別チャンネルで最新投稿がないため、
        # 実際にM COUNTDOWNの最新動画を投稿している @mnet を使う(実検索で確認済み)。
        "name": "M COUNTDOWN",
        "channel_url": "https://www.youtube.com/@mnet",
        "handle": "mnet",
    },
    "the_show": {
        "name": "THE SHOW",
        "channel_url": "https://www.youtube.com/@thekpop",
        "handle": "thekpop",
    },
}
DEFAULT_PROGRAM_KEY = "music_bank"
DEFAULT_TARGET_GROUP = "aespa"

# パフォーマンス映像ではない動画(インタビュー・授賞式・裏話コンテンツ等)をタイトルから除外する。
# 後から見つかった除外すべきパターンはここに追加すればよい。
PROGRAM_EXCLUDE_KEYWORDS = frozenset([
    "interview", "(interview)",
    "winner's ceremony", "winner ceremony",
    "drama -",
    "behind", "backstage",
    "self-cam diary", "self cam diary",
])

# Shorts判定。video_collector.py の _SHORTS_TITLE_KEYWORDS / _SHORTS_MAX_DURATION と同じ基準を使う。
_PROGRAM_SHORTS_TITLE_KEYWORDS = frozenset(["shorts", "#shorts"])
_PROGRAM_SHORTS_MAX_DURATION = 60


def _is_program_excluded(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in PROGRAM_EXCLUDE_KEYWORDS)


def _is_program_shorts(title: str, duration: int) -> bool:
    t = title.lower()
    if any(kw in t for kw in _PROGRAM_SHORTS_TITLE_KEYWORDS):
        return True
    return 0 < duration <= _PROGRAM_SHORTS_MAX_DURATION


def _orientation_from_embed(embed_html: str) -> str | None:
    """videos.list の player.embedHtml (maxHeight指定時、実アスペクト比にスケールされる)の
    width/height から動画の向きを判定する。"高さ>幅"なら"portrait"、それ以外は"landscape"。
    width/heightが読めなければNone(不明)。"""
    w = re.search(r'width="(\d+)"', embed_html or "")
    h = re.search(r'height="(\d+)"', embed_html or "")
    if not (w and h):
        return None
    return "portrait" if int(h.group(1)) > int(w.group(1)) else "landscape"


def _parse_duration_iso8601(s: str) -> int:
    """PT#H#M#S 形式を秒数に変換する。"""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, sec = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + sec


_CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")


def _normalize_channel_input(raw: str) -> tuple[str, str]:
    """ユーザーが自由入力したチャンネル指定(ハンドル / @ハンドル / チャンネルURL)を
    ("id", channelId) または ("handle", handle) に正規化する。
    - youtube.com/channel/UC... → ("id", "UC...")
    - @handle / youtube.com/@handle / youtube.com/c/x / youtube.com/user/x / 素の文字列
      → ("handle", "handle")  (先頭の @ は除去)
    判定できない場合は ("handle", "") を返す。"""
    s = (raw or "").strip()
    if not s:
        return ("handle", "")

    # URL形式なら末尾のパス要素を取り出す
    if "youtube.com/" in s or "youtu.be/" in s:
        s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        m = re.search(r"youtube\.com/channel/([\w-]+)", s)
        if m:
            cid = m.group(1)
            return ("id", cid) if _CHANNEL_ID_RE.match(cid) else ("handle", "")
        m = re.search(r"youtube\.com/(?:@|c/|user/)([^/]+)", s)
        if m:
            return ("handle", m.group(1).lstrip("@"))
        # それ以外のYouTube URLは末尾要素をハンドル候補として使う
        s = s.rsplit("/", 1)[-1]

    if _CHANNEL_ID_RE.match(s):
        return ("id", s)
    return ("handle", s.lstrip("@"))


def _verify_channel_id(channel_id: str, api_key: str, app) -> bool:
    """channels.list?id= でチャンネルIDが実在するか確認する。結果はSettingにキャッシュ。"""
    cache_key = f"youtube_channel_verified_{channel_id}"
    with app.app_context():
        if Setting.get(cache_key, ""):
            return True
    try:
        resp = requests.get(
            YOUTUBE_CHANNELS_URL,
            params={"part": "id", "id": channel_id, "key": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        exists = bool(resp.json().get("items", []))
    except Exception as exc:
        logger.error("チャンネルID確認エラー(%s): %s", channel_id, exc)
        return False
    if exists:
        with app.app_context():
            Setting.set(cache_key, "1")
    return exists


def _resolve_free_channel_id(raw: str, api_key: str, app) -> str:
    """自由入力されたチャンネル指定からchannelIdを解決する。
    チャンネルID形式なら実在確認したうえでそのまま、ハンドルなら既存の_resolve_channel_id
    (channels.list?forHandle + Settingキャッシュ)で解決する。失敗時は空文字。"""
    kind, value = _normalize_channel_input(raw)
    if not value:
        return ""
    if kind == "id":
        return value if _verify_channel_id(value, api_key, app) else ""
    return _resolve_channel_id(value, api_key, app)


def _resolve_channel_id(handle: str, api_key: str, app) -> str:
    """YouTubeチャンネルのhandleからchannelIdを解決する。チャンネルIDは変わらないため
    Settingにキャッシュし、以後はAPI呼び出しをスキップする。"""
    cache_key = f"youtube_channel_id_{handle}"
    with app.app_context():
        cached = Setting.get(cache_key, "")
    if cached:
        return cached

    try:
        resp = requests.get(
            YOUTUBE_CHANNELS_URL,
            params={"part": "id", "forHandle": handle, "key": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as exc:
        logger.error("チャンネルID解決エラー(%s): %s", handle, exc)
        return ""

    if not items:
        return ""
    channel_id = items[0]["id"]
    with app.app_context():
        Setting.set(cache_key, channel_id)
    return channel_id


def _search_videos(query: str, api_key: str, fetch_count: int, order: str,
                    page_token: str | None = None, channel_id: str | None = None) -> tuple:
    """1ページ分の検索結果を取得する。channel_id を指定すればそのチャンネル内、省略すれば
    YouTube全体を対象にする。戻り値: (items, next_page_token)。
    nextPageToken は order(・channel_id)を含む検索条件に紐づくため、続きのページを取得する際は
    同じ条件を使い続ける必要がある(呼び出し側で保持しておくこと)。"""
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": order,
        "maxResults": fetch_count,
        "key": api_key,
    }
    if channel_id:
        params["channelId"] = channel_id
    if page_token:
        params["pageToken"] = page_token
    resp = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", []), data.get("nextPageToken")


def _existing_article_urls(app) -> set:
    with app.app_context():
        return {
            a.url for a in Article.query.filter(
                Article.url.like("https://www.youtube.com/watch?v=%")
            ).all()
        }


def _official_channel_ids(app, api_key: str) -> set:
    """PROGRAM_CHANNELSに登録済みの公式放送局チャンネルIDの集合を返す(fancam検索での除外用)。
    _resolve_channel_idの結果はSettingにキャッシュされているため、通常は追加API呼び出しなし。"""
    ids = set()
    for channel in PROGRAM_CHANNELS.values():
        cid = _resolve_channel_id(channel["handle"], api_key, app)
        if cid:
            ids.add(cid)
    return ids


def _enrich_and_build_videos(items: list, api_key: str, existing_urls: set, max_results: int) -> list:
    """検索結果アイテムに動画の長さ・再生数を付与し、DB既存分・Shortsを除外して
    候補リストを構築する(search_program_videos / search_fancam_videos で共通)。"""
    video_ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]

    durations = {}
    view_counts = {}
    orientations = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        try:
            vresp = requests.get(
                YOUTUBE_VIDEOS_URL,
                # player パートを含めても videos.list のクォータ消費は1ユニットのまま。
                # maxHeight を指定すると player.embedHtml の width/height が実アスペクト比に
                # スケールされて返る(snippet.thumbnails の width/height は向きに関わらず
                # 固定枠のため判定に使えない)。
                params={"part": "contentDetails,statistics,player",
                        "id": ",".join(batch), "maxHeight": 8192, "key": api_key},
                timeout=15,
            )
            vresp.raise_for_status()
            for item in vresp.json().get("items", []):
                durations[item["id"]] = _parse_duration_iso8601(
                    item.get("contentDetails", {}).get("duration", "")
                )
                try:
                    view_counts[item["id"]] = int(item.get("statistics", {}).get("viewCount", 0))
                except (ValueError, TypeError):
                    view_counts[item["id"]] = 0
                orientations[item["id"]] = _orientation_from_embed(
                    item.get("player", {}).get("embedHtml", "")
                )
        except Exception as exc:
            logger.warning("動画長さ・再生数・向き取得エラー: %s", exc)

    videos = []
    for it in items:
        vid = it.get("id", {}).get("videoId")
        if not vid:
            continue
        url = f"https://www.youtube.com/watch?v={vid}"
        if url in existing_urls:
            continue
        snippet = it.get("snippet", {})
        title = snippet.get("title", "")
        duration = durations.get(vid, 0)
        if _is_program_shorts(title, duration):
            continue
        videos.append({
            "video_id": vid,
            "title": title,
            "url": url,
            "thumbnail": _best_thumbnail(snippet.get("thumbnails", {})),
            "published_at": snippet.get("publishedAt", ""),
            "duration": duration,
            "view_count": view_counts.get(vid, 0),
            "orientation": orientations.get(vid),  # "portrait" / "landscape" / None(不明)
        })
        if len(videos) >= max_results:
            break
    return videos


def search_program_videos(app, program_key: str, target_group: str, max_results: int = 30,
                           fetch_count: int = 30, page_token: str | None = None,
                           order: str = "date", channel_input: str | None = None) -> dict:
    """指定番組(program_key)の公式チャンネル内で「(グループ名) (番組名)」を検索し、
    該当グループのパフォーマンス動画候補を返す。YouTube検索APIの q パラメータは緩い関連性
    マッチングのため、クエリに一致しない(別グループの)動画も混在する。1ページ(fetch_count件)を
    取得したうえで、(1)タイトルに target_group が単語として含まれる (2)インタビュー等の
    非パフォーマンス動画でない (3)Shortsでない、の3条件でフィルタし、最大 max_results 件まで返す。

    order は "date"(新しい順) または "viewCount"(再生数順) をUI側から指定する想定。
    page_token を省略(新規検索)した場合、指定した order でまず検索し、フィルタ後0件なら
    order="relevance" でも探す(休止中グループ等、直近fetch_count件に出演回が無いケースの救済。
    既に order="relevance" 指定時は二重に試さない)。続きのページを取得する「もっと見る」用途
    (page_token を指定する場合)では、初回検索で実際に使われた order(レスポンスの "order")を
    そのまま渡すこと。フォールバックは行わず、指定した order・page_token のその1ページのみを
    取得する(nextPageToken は同じ order の検索条件に紐づくため、pageToken取得中に order を
    変えると正しく継続できない)。DBには保存しない。
    channel_input には @ハンドル / チャンネルURL / チャンネルID を渡せる。指定された場合は
    プリセット(program_key)より優先し、そのチャンネル内を "(グループ名)" だけで検索する。
    戻り値: {"ok", "error", "videos": [...], "next_page_token", "order"}"""
    channel_input = (channel_input or "").strip()

    with app.app_context():
        api_key = Setting.get("youtube_api_key", "") or os.getenv("YOUTUBE_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "YouTube APIキーが設定されていません", "videos": [],
                "next_page_token": None, "order": None}

    if channel_input:
        channel_id = _resolve_free_channel_id(channel_input, api_key, app)
        if not channel_id:
            return {"ok": False,
                    "error": f"チャンネルが見つかりません: {channel_input}"
                             "（@ハンドル または チャンネルURL を確認してください）",
                    "videos": [], "next_page_token": None, "order": None}
        query = target_group
    else:
        channel = PROGRAM_CHANNELS.get(program_key)
        if not channel:
            return {"ok": False, "error": f"未対応の番組です({program_key})", "videos": [],
                    "next_page_token": None, "order": None}
        channel_id = _resolve_channel_id(channel["handle"], api_key, app)
        if not channel_id:
            return {"ok": False, "error": f"チャンネルが見つかりません(@{channel['handle']})", "videos": [],
                    "next_page_token": None, "order": None}
        query = f"{target_group} {channel['name']}"

    def _filtered(raw_items):
        out = []
        for it in raw_items:
            title = it.get("snippet", {}).get("title", "")
            if not _matches_target_artist(title, "", target_group):
                continue
            if _is_program_excluded(title):
                continue
            out.append(it)
        return out

    try:
        if page_token:
            # 「もっと見る」: 呼び出し側が保持している order・page_token で1ページ継続取得
            # (フォールバックはしない。orderを勝手に変えるとページ位置の整合性が崩れるため)
            raw_items, next_token = _search_videos(
                query, api_key, fetch_count, order, page_token, channel_id=channel_id
            )
            items = _filtered(raw_items)
            effective_order = order
        else:
            # 新規検索: 指定されたorder(UIで選んだ並び順)でまず検索し、
            # フィルタ後0件ならrelevanceにフォールバック(既にrelevance指定時は行わない)
            raw_items, next_token = _search_videos(query, api_key, fetch_count, order, channel_id=channel_id)
            items = _filtered(raw_items)
            effective_order = order
            if not items and order != "relevance":
                raw_items, next_token = _search_videos(
                    query, api_key, fetch_count, "relevance", channel_id=channel_id
                )
                items = _filtered(raw_items)
                effective_order = "relevance"
    except Exception as exc:
        logger.error("番組別検索エラー(%s): %s", query, exc)
        return {"ok": False, "error": f"YouTube検索エラー: {str(exc)[:150]}", "videos": [],
                "next_page_token": None, "order": None}

    if not items:
        return {"ok": True, "error": None, "videos": [],
                "next_page_token": next_token, "order": effective_order}

    existing_urls = _existing_article_urls(app)
    videos = _enrich_and_build_videos(items, api_key, existing_urls, max_results)

    return {"ok": True, "error": None, "videos": videos,
            "next_page_token": next_token, "order": effective_order}


# ── fancam検索(グループ横断・チャンネル無指定) ─────────────────────────────────
# 特定チャンネルに絞らずYouTube全体を検索し、PROGRAM_CHANNELSの公式放送局チャンネル
# (著作権リスクが高い)は結果から除外する。個人ファンによる撮影映像を見つけるのが目的。

FANCAM_QUERY_SUFFIXES = ["fancam", "직캠", "stage mix", "ver", "チッケム"]

# タイトルにこれらのいずれかが含まれる場合はレーベル/事務所公式コンテンツとみなして除外する
# (fancam検索専用。PROGRAM_EXCLUDE_KEYWORDSとは目的が異なるため別リストにする)。
FANCAM_EXCLUDE_KEYWORDS = frozenset([
    "official mv", "official m/v",
    # "choreography"単独(ver/videoが付かない"(NewJeans Choreography)"等の表記も含む)で除外
    "choreography",
    "performance video", "special performance video",
    "dance practice",
    "showcase",
    # 放送局系表記(チャンネルID除外をすり抜けた転載・非公式チャンネル対策の多層防御)。
    # "@MusicBank"のようにスペース無し・記号付きで書かれることもあるため両表記を含める
    "music bank", "musicbank", "뮤직뱅크",
    "k-choreo",
    "mcountdown", "mpd직캠",
    "인기가요", "음악중심", "입덕직캠",
])


def _is_fancam_title(title: str) -> bool:
    """タイトルに「직캠」または「fancam」(大文字小文字区別なし)が含まれるかを判定する。"""
    t = title.lower()
    return "직캠" in t or "fancam" in t


def _is_fancam_excluded(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in FANCAM_EXCLUDE_KEYWORDS)


def search_fancam_videos(app, target_group: str, max_results: int = 30, fetch_count: int = 30,
                          page_token: str | None = None, order: str = "date",
                          query_suffix: str | None = None) -> dict:
    """YouTube全体から target_group のfancam(個人ファン撮影映像)を検索する。特定チャンネルには
    絞らず、PROGRAM_CHANNELSに登録済みの公式放送局チャンネル(KBS World TV等)からの結果は
    著作権リスクが高いため除外する。

    query_suffix を省略(新規検索)した場合、FANCAM_QUERY_SUFFIXES を順番に試し、フィルタ後に
    結果が出た最初のsuffixを採用する(全パターンを毎回試すとクォータ消費が5倍になるため、
    search_program_videos の date→relevance フォールバックと同じ「最初に成功したものを採用」
    方式にした。全パターンをマージする実装は行っていない)。全suffixで0件だった場合は、
    最初のsuffixに対してさらに order="relevance" でも試す。
    続きのページを取得する「もっと見る」用途では、初回検索で採用された
    query_suffix・order・page_token を呼び出し側で保持し、そのまま指定して呼び出すこと。
    戻り値: {"ok", "error", "videos": [...], "next_page_token", "order", "query_suffix"}"""
    with app.app_context():
        api_key = Setting.get("youtube_api_key", "") or os.getenv("YOUTUBE_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "YouTube APIキーが設定されていません", "videos": [],
                "next_page_token": None, "order": None, "query_suffix": None}

    exclude_channel_ids = _official_channel_ids(app, api_key)

    def _filtered(raw_items):
        out = []
        for it in raw_items:
            snippet = it.get("snippet", {})
            if snippet.get("channelId") in exclude_channel_ids:
                continue
            title = snippet.get("title", "")
            if not _matches_target_artist(title, "", target_group):
                continue
            if _is_program_excluded(title):
                continue
            if not _is_fancam_title(title):
                continue
            if _is_fancam_excluded(title):
                continue
            out.append(it)
        return out

    try:
        if page_token:
            if not query_suffix:
                return {"ok": False, "error": "query_suffixが指定されていません", "videos": [],
                        "next_page_token": None, "order": None, "query_suffix": None}
            # 「もっと見る」: 呼び出し側が保持しているsuffix・order・page_tokenで1ページ継続取得
            query = f"{target_group} {query_suffix}"
            raw_items, next_token = _search_videos(query, api_key, fetch_count, order, page_token)
            items = _filtered(raw_items)
            effective_order = order
            effective_suffix = query_suffix
        else:
            suffixes = [query_suffix] if query_suffix else FANCAM_QUERY_SUFFIXES
            items, next_token = [], None
            effective_suffix = suffixes[0]
            for suffix in suffixes:
                query = f"{target_group} {suffix}"
                raw_items, next_token = _search_videos(query, api_key, fetch_count, order)
                items = _filtered(raw_items)
                effective_suffix = suffix
                if items:
                    break
            effective_order = order
            if not items and order != "relevance":
                query = f"{target_group} {suffixes[0]}"
                raw_items, next_token = _search_videos(query, api_key, fetch_count, "relevance")
                items = _filtered(raw_items)
                effective_order = "relevance"
                effective_suffix = suffixes[0]
    except Exception as exc:
        logger.error("fancam検索エラー(%s): %s", target_group, exc)
        return {"ok": False, "error": f"YouTube検索エラー: {str(exc)[:150]}", "videos": [],
                "next_page_token": None, "order": None, "query_suffix": None}

    if not items:
        return {"ok": True, "error": None, "videos": [], "next_page_token": next_token,
                "order": effective_order, "query_suffix": effective_suffix}

    existing_urls = _existing_article_urls(app)
    videos = _enrich_and_build_videos(items, api_key, existing_urls, max_results)

    return {"ok": True, "error": None, "videos": videos, "next_page_token": next_token,
            "order": effective_order, "query_suffix": effective_suffix}
