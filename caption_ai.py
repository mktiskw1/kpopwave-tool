"""承認待ち画面の「候補を生成」: 動画の投稿文の候補を6案、Claude Code の CLI(claude -p)で作る。

- APIキーは使わない。このPCの Claude Code に保存済みのサブスクのログインをそのまま使う
  (認証ファイルやトークンには触れない。子プロセスの環境変数から ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN は外す)。
- ボタンを押したときだけ実行する(スケジューラなどからは呼ばない)。
- 生成は別スレッドで走らせ、画面は結果をポーリングで待つ。
- 1回の呼び出しで12案を作らせ、その中から良い案を選ばせる。除外フィルタで6案に満たなければ追加で生成する(最大2回)。
- ログには所要時間・total_cost_usd・除外の件数と理由だけを残し、本文は出さない。
  (環境変数 CAPTION_DEBUG_LOG にファイルを指定したときだけ、検証用に除外した案の本文をそのファイルへ書く)
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid

logger = logging.getLogger(__name__)

CLI_TIMEOUT_SECONDS = 180
RECENT_POSTS_LIMIT = 15
RECENT_GENERATED_LIMIT = 30      # プロンプトに入れる「直近に生成した案」の数
RECENT_GENERATED_KEEP = 60       # 保存しておく数
EXEMPLAR_LIMIT = 10              # お手本(いいねが多かった投稿文)の数
EXEMPLAR_PER_GROUP = 2           # お手本に同じグループを入れる上限
CANDIDATE_COUNT = 6              # 画面に出す案の数
DRAFT_COUNT = 12                 # 1回の呼び出しで作らせる案の数
MAX_EXTRA_ROUNDS = 2             # 足りないときの追加生成の回数

SETTING_MODEL = "caption_model"
SETTING_RECENT_GENERATED = "caption_recent_generated"
DEFAULT_MODEL = "opus"
MODEL_CHOICES = ("opus", "sonnet", "haiku")

# 生成ルールの初期値(設定画面で編集できる。Setting "caption_rules")
DEFAULT_CAPTION_RULES = """\
あなたはKPOP動画をThreadsに投稿するときの、短い投稿文を考える編集者です。

【構成】
- 2段構成にする。1行目＝止める一文(スクロールを止める)、2行目＝返信させる一文(コメントしたくなる問いかけ)。
- 案同士で、止める一文の型・返信させる一文の型・書き出しが重ならないようにする。
- 各行は短く、読み上げて自然な日本語にする。
- グループ名と曲名は、指示された範囲で自然に入れる(メンバー動画はメンバー名も入れる)。
  「〇〇の△△の□□」のように「の」を重ねない。不自然なら1行目と2行目に分けてよい。
  曲名の前に The などの余計な語を付けない。
  ただし「書かない」と指示された項目は入れない。推測で補わない。

【書いてはいけないもの】
- 韓国語、URL、ハッシュタグ(#)、絵文字、ネガティブな表現。
- LE SSERAFIMのHONG EUNCHAEは「EUNCHAE」だけで表記する。

【型の例文について】
下の例文は「雰囲気の参考」です。そのまま使わない。毎回、動画に合わせて新しい言い回しを作る。
例文と同じ言い回しが入った案は、システムが自動で除外します。

【止める一文の型】
- 好奇心ギャップ型: 「ラスト5秒に全部ある」「2回目で気づくやつ」。時間指定は使わない。
- ナゾ型: 「この子だけ時間の流れ方が違う」。「音を消して見てほしい」は使わない。
- 途中から始まる型: 「…いや、〇〇の△△はずるい」。
- 視点指定型: 「今日は目線だけ追ってみて」。
- バンドワゴン型: 「〇〇ペンなら全員保存してる△△」。
- 断言・逆張り型: 「正直これが一番好き」。

【返信させる一文の型】
- 二択型: 「〇〇派？△△派？」
- 参加型: 「この曲で一番好きなパート、どこ？」
- 記憶型: 「〇〇に沼った曲、覚えてる？」
- 仲間探し型: 「〇〇推しの人、いる？」

【直近の投稿文】
ユーザーが直近の投稿文と、直近に生成した案を渡す。同じ書き出し・同じ言い回し・同じ型の組み合わせを避ける。
"""

# ルールを編集しても必ず効かせる固定の指示(システムプロンプトの末尾に付ける)
_FIXED_INSTRUCTIONS = """

【必ず守る指示(固定)】
- 型の例文は雰囲気の参考にすぎない。例文と同じ言い回しは使わず、毎回新しい言い回しを作る。
- お手本の投稿文は「この自然さを目指す」ための見本。ただし表現は真似しない。
- 直近の投稿文・直近に生成した案と、同じ書き出し・同じ言い回し・同じ型の組み合わせを避ける。
- グループ名・メンバー名・曲名は自然に入れる。「〇〇の△△の□□」のように「の」を重ねない。
  不自然なら1行目と2行目に分けてよい。曲名の前に The などの余計な語を付けない。
- タイトル・説明文・選ばれたグループ／メンバー／曲名に書かれていない事実(会場、アンコール、衣装、日付、出来事など)は書かない。
  分からないことは、見た印象や感情の言葉で表す。
- 1回の依頼で12案を作り、その中から指定された数だけ、次の基準で良い案を選んで返す:
  スクロールを止める力があるか / 自然な日本語か / 返信したくなるか / 他の案や直近の投稿と被っていないか。
"""


def build_schema(select_n: int) -> dict:
    item = {
        "type": "object",
        "properties": {
            "stop_type": {"type": "string", "description": "止める一文の型の名前"},
            "reply_type": {"type": "string", "description": "返信させる一文の型の名前"},
            "stop": {"type": "string", "description": "1行目: 止める一文"},
            "reply": {"type": "string", "description": "2行目: 返信させる一文"},
        },
        "required": ["stop_type", "reply_type", "stop", "reply"],
    }
    return {
        "type": "object",
        "properties": {
            "drafts": {"type": "array", "minItems": DRAFT_COUNT, "maxItems": DRAFT_COUNT, "items": item,
                       "description": "作った12案(番号は1から)"},
            "selected": {"type": "array", "minItems": select_n, "maxItems": select_n,
                         "items": {"type": "integer", "minimum": 1, "maximum": DRAFT_COUNT},
                         "description": f"drafts から選んだ良い{select_n}案の番号(1始まり)。良い順"},
        },
        "required": ["drafts", "selected"],
    }


_HANGUL_RE = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿️⭐❤]")
# 「〇〇の△△の□□」のように、読点なしの短い間隔で「の」が重なる
_NO_CHAIN_RE = re.compile(r"の[^の、。？！!?\n]{1,14}の")

# ログイン切れなどを見分ける語(画面には元のメッセージをそのまま出し、案内を1行足す)
_LOGIN_HINTS = ("login", "log in", "authenticat", "unauthorized", "401", "oauth", "credential", "api key", "expired")

# 例文のうち、ルールから自動で拾えない言い回し(短い言い回しや一般的な語は落とさないため、今は空)
_EXTRA_FORBIDDEN = ()

# グループ名のカタカナ表記(「エンミックス」など)。本文にこれが入っていればグループ名ありとみなす
GROUP_ALIASES = {
    "nmixx": ("エンミックス",), "ive": ("アイヴ", "アイブ"), "illit": ("アイリット",), "aespa": ("エスパ",),
    "newjeans": ("ニュージーンズ",), "lesserafim": ("ルセラフィム",), "babymonster": ("ベビーモンスター", "ベビモン"),
    "twice": ("トゥワイス",), "hearts2hearts": ("ハーツトゥハーツ", "ハツハ"), "izone": ("アイズワン",),
    "blackpink": ("ブラックピンク",), "triples": ("トリプルエス",), "kiiikiii": ("キィキィ", "キキ"),
    "rescene": ("リセンヌ",), "itzy": ("イッジ",), "stayc": ("ステイシー",), "fromis9": ("プロミスナイン",),
}


# ───────────── 設定 ─────────────

def get_model() -> str:
    from database import Setting
    v = (Setting.get(SETTING_MODEL, "") or "").strip().lower()
    return v if v in MODEL_CHOICES else DEFAULT_MODEL


# ───────────── 実行ファイル・環境 ─────────────

def find_claude_exe() -> str | None:
    """claude の実行ファイルの絶対パス。PATHで見つからなければ %USERPROFILE%\\.local\\bin\\claude.exe。"""
    found = shutil.which("claude")
    if found:
        return os.path.abspath(found)
    fallback = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
    return fallback if os.path.isfile(fallback) else None


def _work_dir() -> str:
    """リポジトリの外の空の専用フォルダ(CLAUDE.md やフックを読み込ませないため)。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "kpopwave_claude_cwd")
    os.makedirs(path, exist_ok=True)
    return path


def _child_env() -> dict:
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    return env


# ───────────── 曲名(説明文から) ─────────────

_DESC_LABEL_RE = re.compile(r"(?im)^\s*(?:song|title|track|曲名|タイトル|곡명|노래)\s*[:：\-]\s*(.{1,60}?)\s*$")


def find_song_in_description(description: str, group_name: str = "") -> str:
    """取り込み時に取得済みのYouTubeの説明文から曲名を探す(YouTubeへ新たにアクセスしない)。
    「Song: …」等のラベル行、または冒頭の引用符で囲まれた部分だけを採用する。見つからなければ空文字。"""
    if not description:
        return ""
    head = description[:600]
    m = _DESC_LABEL_RE.search(head)
    if m:
        return m.group(1).strip().strip("'\"「」『』“”‘’")
    from summarizer import _extract_song_title
    return _extract_song_title(head[:300], group_name)


# ───────────── プロンプトに入れる材料(DB) ─────────────

def _norm(text: str) -> str:
    """表記ゆれを吸収した比較用の文字列(空白・記号を除き、小文字化)。"""
    t = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s\W_]+", "", t)


def pick_exemplars(limit: int = EXEMPLAR_LIMIT, per_group: int = EXEMPLAR_PER_GROUP) -> list:
    """お手本: 投稿済みでいいねが多かった投稿文(重複を除き、同じグループは per_group 件まで)。
    [(いいね数, 本文)] を返す。"""
    from database import Article
    rows = (Article.query.filter(Article.status == "posted", Article.summary.isnot(None), Article.summary != "",
                                 Article.like_count.isnot(None), Article.like_count > 0)
            .order_by(Article.like_count.desc()).limit(300).all())
    out, seen, per = [], set(), {}
    for a in rows:
        key = _norm(a.summary)
        if not key or key in seen:
            continue
        gkey = a.group_id if a.group_id is not None else "none"
        if per.get(gkey, 0) >= per_group:
            continue
        seen.add(key)
        per[gkey] = per.get(gkey, 0) + 1
        out.append((int(a.like_count), a.summary.strip()))
        if len(out) >= limit:
            break
    return out


def recent_generated(limit: int = RECENT_GENERATED_LIMIT) -> list:
    """直近に「候補を生成」で出した案(選ばれなかったものを含む)。新しい順。"""
    from database import Setting
    try:
        data = json.loads(Setting.get(SETTING_RECENT_GENERATED, "") or "[]")
    except ValueError:
        data = []
    return [t for t in data if isinstance(t, str)][:limit]


def save_generated(texts: list) -> None:
    """表示した案を、直近に生成した案として保存する(新しい順に先頭へ足し、上限で切る)。"""
    from database import Setting
    merged = (list(texts) + recent_generated(RECENT_GENERATED_KEEP))[:RECENT_GENERATED_KEEP]
    Setting.set(SETTING_RECENT_GENERATED, json.dumps(merged, ensure_ascii=False))


# ───────────── プロンプト作り ─────────────

def build_system_prompt(rules: str) -> str:
    return (rules or DEFAULT_CAPTION_RULES).rstrip() + _FIXED_INSTRUCTIONS


def prefer_latin_song(video_title: str, song_title: str, exclude: tuple = ()) -> str:
    """曲名がハングルだけのとき、タイトル中の別の引用符(例: 'Birthday Wish')に英字の曲名があればそれを返す。
    見つからなければ元の曲名をそのまま返す。"""
    if not song_title or not _HANGUL_RE.search(song_title) or re.search(r"[A-Za-z]", song_title):
        return song_title
    from summarizer import _SONG_TITLE_QUOTE_PATTERNS
    skip = {_norm(x) for x in exclude if x}
    for pat in _SONG_TITLE_QUOTE_PATTERNS:
        for m in pat.finditer(video_title or ""):
            cand = m.group(1).strip()
            if cand and re.search(r"[A-Za-z]", cand) and not _HANGUL_RE.search(cand) and _norm(cand) not in skip:
                return cand
    return song_title


def _prepare_song(song_title: str) -> tuple:
    """(プロンプトに書く曲名, 韓国語表記のままか)。韓国語は本文に入れられないので、英字の部分があればそれだけを使う。"""
    latin = re.sub(r"\s+", " ", _HANGUL_RE.sub(" ", song_title)).strip(" -–—|・()（）[]'\"「」")
    if _HANGUL_RE.search(song_title) and re.search(r"[A-Za-z0-9]", latin):
        song_title = latin
    return song_title, bool(_HANGUL_RE.search(song_title))


def build_user_prompt(*, group_name: str, member_name: str, song_title: str, video_title: str,
                      recent_texts: list, group_mode: str, song_mode: str,
                      exemplars: list = (), generated: list = (), accepted: list = (), shown: list = (),
                      select_n: int = CANDIDATE_COUNT) -> str:
    """group_mode: 'named'(グループ名あり) / 'none'(書かない)。 song_mode: 'named'(曲名あり) / 'none'(書かない)。"""
    lines = [f"次の動画の投稿文を{DRAFT_COUNT}案作り、その中から良い{select_n}案を選んでください。", "",
             "【動画の情報】", f"- YouTubeのタイトル: {video_title}"]
    if group_mode == "named":
        lines.append(f"- グループ名: {group_name}(自然に入れる)")
    else:
        lines.append("- グループ名: 不明。グループ名は書かない。推測で書かない。")
    if member_name and group_mode == "named":
        lines.append(f"- メンバー名: {member_name}(メンバー動画なので自然に入れる)")
    if song_mode == "named":
        song_title, still_korean = _prepare_song(song_title)
        if still_korean:
            lines.append(f"- 曲名: {song_title}(自然に入れる。韓国語は使えないので、カタカナかローマ字に直して書く)")
        else:
            lines.append(f"- 曲名: {song_title}(自然に入れる。曲名の前に The などの余計な語を付けない)")
    else:
        lines.append("- 曲名: 不明。曲名は書かない。推測で書かない。")
    lines.append("")
    if exemplars:
        lines += ["【お手本: 過去に投稿して反応が良かった投稿文(いいね数つき)。この自然さを目指す。ただし表現は真似しない】"]
        for likes, text in exemplars:
            lines.append(f"- ({likes}いいね) " + text.replace("\n", " / "))
        lines.append("")
    if recent_texts:
        lines.append("【直近の投稿文(新しい順)。同じ書き出し・言い回し・型の連続を避ける】")
        for i, t in enumerate(recent_texts, 1):
            lines.append(f"{i}. " + t.replace("\n", " / "))
    else:
        lines.append("【直近の投稿文】なし")
    lines.append("")
    if generated:
        lines.append("【直近に生成した案(選ばれなかったものを含む)。動画が違っても、同じ書き出し・同じ言い回し・同じ型の組み合わせを避ける】")
        for i, t in enumerate(generated, 1):
            lines.append(f"{i}. " + t.replace("\n", " / "))
        lines.append("")
    if shown:
        lines.append("【いま画面に出している案。これらとは違う案にする。同じ書き出し・同じ言い回し・同じ型の組み合わせを避ける】")
        for i, t in enumerate(shown, 1):
            lines.append(f"{i}. " + t.replace("\n", " / "))
        lines.append("")
    if accepted:
        lines.append("【今回すでに採用した案。これらとも被らせない】")
        for i, t in enumerate(accepted, 1):
            lines.append(f"{i}. " + t.replace("\n", " / "))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ───────────── 実行 ─────────────

class CaptionError(Exception):
    pass


def _explain_failure(message: str) -> str:
    message = (message or "").strip() or "原因不明のエラー"
    low = message.lower()
    if any(h in low for h in _LOGIN_HINTS):
        message += "\n(ログインが切れている可能性があります。VSCodeかターミナルで /login してください)"
    return message


def run_claude(system_prompt: str, user_prompt: str, model: str = DEFAULT_MODEL, select_n: int = CANDIDATE_COUNT) -> tuple:
    """claude -p を1回呼ぶ。(drafts, selected番号のリスト, total_cost_usd, 所要秒) を返す。失敗時は CaptionError。"""
    exe = find_claude_exe()
    if not exe:
        raise CaptionError("claude の実行ファイルが見つかりません(PATH と %USERPROFILE%\\.local\\bin\\claude.exe を探しました)")
    cmd = [
        exe, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(build_schema(select_n), ensure_ascii=False),
        "--system-prompt", system_prompt,
        "--tools", "",
        "--model", model,
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, input=user_prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CLI_TIMEOUT_SECONDS, cwd=_work_dir(), env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        logger.warning("caption_ai: タイムアウト (%ds)", CLI_TIMEOUT_SECONDS)
        raise CaptionError(f"{CLI_TIMEOUT_SECONDS}秒以内に終わらなかったため中止しました")
    elapsed = time.time() - t0

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    data = None
    if stdout:
        try:
            data = json.loads(stdout)
        except ValueError:
            data = None
    cost = (data or {}).get("total_cost_usd")
    logger.info("caption_ai: model=%s exit=%s 所要=%.1fs total_cost_usd=%s", model, proc.returncode, elapsed, cost)

    if data is None:
        raise CaptionError(_explain_failure(stderr or stdout or f"claude が終了コード {proc.returncode} で終了しました"))
    if data.get("is_error") or proc.returncode != 0:
        raise CaptionError(_explain_failure(str(data.get("result") or stderr or "claude がエラーを返しました")))

    payload = data.get("structured_output")
    if not isinstance(payload, dict):
        try:
            payload = json.loads(data.get("result") or "")
        except ValueError:
            raise CaptionError("生成結果を読み取れませんでした: " + (str(data.get("result") or "")[:300]))
    drafts = payload.get("drafts") if isinstance(payload, dict) else None
    if not isinstance(drafts, list) or not drafts:
        raise CaptionError("生成結果に候補がありませんでした")
    selected = payload.get("selected") if isinstance(payload.get("selected"), list) else []
    return drafts, [s for s in selected if isinstance(s, int)], cost, elapsed


# ───────────── 除外フィルタ ─────────────

def example_phrases_from_rules(rules: str) -> list:
    """ルールの型の例文(「…」で囲まれた部分)から、そのまま使われたら除外する言い回しを取り出す。
    〇・△・□の穴埋め部分で区切った断片のうち、5文字以上のものを対象にする。"""
    phrases = set(_EXTRA_FORBIDDEN)
    for line in (rules or "").splitlines():
        if not re.match(r"^\s*-\s.*型\s*[:：]", line):   # 「- ○○型: 「例文」」の行だけを対象にする
            continue
        for ex in re.findall(r"「([^」]{3,60})」", line):
            for frag in re.split(r"[〇△□○◯]+", ex):
                frag = frag.strip(" 、。？?！!")
                if len(frag) >= 5:
                    phrases.add(frag)
    return sorted(phrases)


def _debug_dump(lines: list) -> None:
    path = os.environ.get("CAPTION_DEBUG_LOG")
    if not path or not lines:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


# 違反の重さ。これらを含む案は、全部の案が落ちたときの注意つき表示でも、できるだけ後回しにする
_HARD_REASONS = ("1行目か2行目が空", "韓国語・URL・タグ・絵文字を含む")


def _name_alternatives(group_name: str, member_name: str) -> list:
    """本文にあれば「グループ名(かメンバー名)が入っている」とみなす表記の一覧(正規化済み)。"""
    alts = [group_name, member_name]
    alts += list(GROUP_ALIASES.get(_norm(group_name), ()))
    return [a for a in (_norm(x) for x in alts) if a]


def evaluate_candidate(it: dict, *, group_name: str, member_name: str, group_mode: str, song_title: str,
                       song_mode: str, forbidden: list, avoid_heads: set, seen: set) -> tuple:
    """1案の違反を全部数える。(本文, 違反の理由のリスト) を返す。リストが空なら合格。"""
    stop = str(it.get("stop") or "").strip()
    reply = str(it.get("reply") or "").strip()
    text = stop + "\n" + reply
    reasons = []
    if not stop or not reply:
        reasons.append("1行目か2行目が空")
    if _HANGUL_RE.search(text) or _URL_RE.search(text) or "#" in text or "＃" in text or _EMOJI_RE.search(text):
        reasons.append("韓国語・URL・タグ・絵文字を含む")
    ntext = _norm(text)
    hit = next((p for p in forbidden if _norm(p) and _norm(p) in ntext), None)
    if hit:
        reasons.append(f"例文と同じ言い回し({hit})")
    # 「の」の重ね: グループ名・メンバー名・曲名のあとに「の」が続き、さらに短い間隔で「の」が来る(〇〇の△△の□□)
    song_p, song_ko = _prepare_song(song_title) if song_mode == "named" else ("", False)
    names = [n for n in (group_name if group_mode == "named" else "", member_name if group_mode == "named" else "",
                         song_p if not song_ko else "") if n]
    if names:
        pat = r"(?:" + "|".join(re.escape(n) for n in names) + r")の[^の、。？！!?\n]{1,14}の"
        if re.search(pat, text):
            reasons.append("「の」を重ねている")
    if song_p and not song_ko and not song_p.lower().startswith("the ") \
            and re.search(r"(?i)(?<![A-Za-z])the\s+" + re.escape(song_p.strip()), text):
        reasons.append("曲名の前に The を付けている")
    if group_mode == "named" and group_name:
        if not any(a in ntext for a in _name_alternatives(group_name, member_name)):
            reasons.append("グループ名が入っていない")
    # 曲名がハングルだけのときは、本文に入っているか確かめられないので判定しない
    if song_mode == "named" and song_p and not song_ko and _norm(song_p) not in ntext:
        reasons.append("曲名が入っていない")
    n_group = _norm(group_name) if group_mode == "named" else ""
    n_song = _norm(song_p) if (song_mode == "named" and not song_ko) else ""
    head = _norm(stop)[:7]
    if head and head in avoid_heads and not ((n_group and _norm(stop).startswith(n_group))
                                             or (n_song and _norm(stop).startswith(n_song))):
        reasons.append("直近の投稿・案と書き出しが同じ")
    if ntext in seen:
        reasons.append("重複")
    return text, reasons


def filter_candidates(items: list, *, group_name: str, group_mode: str, song_title: str, song_mode: str,
                      forbidden: list, avoid_texts: list, accepted: list = (), member_name: str = "") -> tuple:
    """items(順序つき)を評価する。(採用[{text, stop_type, reply_type}], 除外[(理由の文字列, 本文, 理由のリスト)]) を返す。
    書き出しの比較は、グループ名・曲名そのもので始まる案(名前で始めるのは自然)には使わない。"""
    avoid_heads = {_norm(t.split("\n")[0])[:7] for t in avoid_texts if t}
    avoid_heads.discard("")
    seen = {_norm(t) for t in accepted}
    kept, dropped = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        text, reasons = evaluate_candidate(
            it, group_name=group_name, member_name=member_name, group_mode=group_mode, song_title=song_title,
            song_mode=song_mode, forbidden=forbidden, avoid_heads=avoid_heads, seen=seen)
        if reasons:
            dropped.append((reasons[0], text, reasons))
            continue
        seen.add(_norm(text))
        kept.append({"text": text, "stop_type": str(it.get("stop_type") or ""), "reply_type": str(it.get("reply_type") or "")})
    return kept, dropped


def pick_least_violating(pool: list, limit: int = CANDIDATE_COUNT) -> list:
    """全部の案が除外されたときの救済。違反の少ない順に最大 limit 案を、注意(warnings)つきで返す。
    韓国語・URL・タグ・絵文字・空行を含む案は、他に選べる案があるかぎり後回しにする。"""
    seen, uniq = set(), []
    for text, reasons in pool:
        k = _norm(text)
        if k in seen:
            continue
        seen.add(k)
        hard = sum(1 for r in reasons if _reason_key(r) in _HARD_REASONS)
        uniq.append((hard, len(reasons), text, reasons))
    uniq.sort(key=lambda x: (x[0] > 0, x[1], x[0]))
    return [{"text": t, "stop_type": "", "reply_type": "", "warnings": [_reason_key(r) for r in rs]}
            for _, _, t, rs in uniq[:limit]]


# ───────────── 生成の本体・ジョブ(ポーリング用) ─────────────

def _reason_key(reason: str) -> str:
    return re.sub(r"\(.*\)$", "", reason)


def generate_candidates(rules: str, model: str, ctx: dict, on_progress=None) -> dict:
    """生成の本体(スレッド/テストの両方から呼ぶ)。
    ctx: group_name, member_name, song_title, video_title, recent_texts, group_mode, song_mode, exemplars, generated。
    戻り値: {candidates, shortfall, cost, seconds, rounds, dropped}。1回目に失敗したら CaptionError。"""
    system_prompt = build_system_prompt(rules)
    forbidden = example_phrases_from_rules(rules)
    avoid = list(ctx.get("recent_texts") or []) + list(ctx.get("generated") or []) + list(ctx.get("shown") or [])
    accepted: list = []
    pool: list = []          # 除外された案(違反の理由つき)。全部落ちたときの救済に使う
    total_cost, total_secs, rounds = 0.0, 0.0, 0
    drop_counts: dict = {}
    for rnd in range(1 + MAX_EXTRA_ROUNDS):
        need = CANDIDATE_COUNT - len(accepted)
        if need <= 0:
            break
        rounds += 1
        if on_progress:
            on_progress("生成中…" if rnd == 0 else f"足りない案を追加で生成中…({rnd}/{MAX_EXTRA_ROUNDS})")
        select_n = need if rnd == 0 else min(DRAFT_COUNT, need + 2)
        prompt = build_user_prompt(
            group_name=ctx["group_name"], member_name=ctx["member_name"], song_title=ctx["song_title"],
            video_title=ctx["video_title"], recent_texts=ctx["recent_texts"], group_mode=ctx["group_mode"],
            song_mode=ctx["song_mode"], exemplars=ctx.get("exemplars") or [], generated=ctx.get("generated") or [],
            shown=ctx.get("shown") or [],
            accepted=[c["text"] for c in accepted], select_n=select_n)
        try:
            drafts, selected, cost, secs = run_claude(system_prompt, prompt, model, select_n)
        except CaptionError:
            if rnd == 0:
                raise
            logger.warning("caption_ai: 追加生成に失敗したため、そろった案だけを出します")
            break
        total_cost += float(cost or 0)
        total_secs += secs
        picked, seen_idx = [], set()
        for n in selected:
            if 1 <= n <= len(drafts) and n not in seen_idx:
                seen_idx.add(n)
                picked.append(drafts[n - 1])
        if not picked:
            picked = drafts[:select_n]
        kept, dropped = filter_candidates(
            picked, group_name=ctx["group_name"], group_mode=ctx["group_mode"], song_title=ctx["song_title"],
            song_mode=ctx["song_mode"], forbidden=forbidden, avoid_texts=avoid, accepted=[c["text"] for c in accepted],
            member_name=ctx.get("member_name") or "")
        accepted = (accepted + kept)[:CANDIDATE_COUNT]
        round_counts: dict = {}
        for _, text, reasons in dropped:
            pool.append((text, reasons))
            for reason in reasons:
                k = _reason_key(reason)
                round_counts[k] = round_counts.get(k, 0) + 1
                drop_counts[k] = drop_counts.get(k, 0) + 1
        logger.info("caption_ai: round=%d 選定=%d 採用=%d 除外=%d %s", rnd + 1, len(picked), len(kept), len(dropped),
                    json.dumps(round_counts, ensure_ascii=False))
        _debug_dump([f"[round {rnd + 1}] 除外 {' + '.join(reasons)}: " + text.replace("\n", " / ")
                     for _, text, reasons in dropped])
    shortfall = None
    if not accepted:
        # 0案では終わらせない: 違反の一番少ない案を、注意つきで最大6案まで出す(手直しできる)
        accepted = pick_least_violating(pool)
        if not accepted:
            raise CaptionError("候補を作れませんでした。もう一度生成してください")
        shortfall = "ルールに合わない部分があります。各案の注意を見て、手直ししてください"
        logger.warning("caption_ai: 全案が除外されたため、違反の少ない%d案を注意つきで表示", len(accepted))
        return {"candidates": accepted, "shortfall": shortfall, "cost": round(total_cost, 4),
                "seconds": round(total_secs, 1), "rounds": rounds, "dropped": drop_counts, "fallback": True}
    if len(accepted) < CANDIDATE_COUNT:
        shortfall = f"{len(accepted)}案しか作れませんでした"
        logger.warning("caption_ai: %s (追加生成%d回後)", shortfall, rounds - 1)
    return {"candidates": accepted, "shortfall": shortfall, "cost": round(total_cost, 4),
            "seconds": round(total_secs, 1), "rounds": rounds, "dropped": drop_counts}


_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _prune_jobs():
    cutoff = time.time() - 3600
    for k in [k for k, v in _JOBS.items() if v["created"] < cutoff]:
        _JOBS.pop(k, None)


def start_job(app, rules: str, model: str, ctx: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _prune_jobs()
        _JOBS[job_id] = {"status": "running", "created": time.time(), "candidates": None, "error": None,
                         "cost": None, "seconds": None, "shortfall": None, "note": "生成中…"}

    def _progress(msg):
        with _JOBS_LOCK:
            _JOBS[job_id]["note"] = msg

    def _work():
        try:
            res = generate_candidates(rules, model, ctx, _progress)
            with app.app_context():
                save_generated([c["text"] for c in res["candidates"]])
            with _JOBS_LOCK:
                _JOBS[job_id].update(status="done", candidates=res["candidates"], cost=res["cost"],
                                     seconds=res["seconds"], shortfall=res["shortfall"])
        except CaptionError as e:
            with _JOBS_LOCK:
                _JOBS[job_id].update(status="failed", error=str(e))
        except Exception:
            logger.exception("caption_ai: 予期しない例外")
            with _JOBS_LOCK:
                _JOBS[job_id].update(status="failed", error="サーバー内部エラーが発生しました(サーバーログを確認してください)")

    threading.Thread(target=_work, daemon=True, name=f"caption-{job_id}").start()
    return job_id


def get_job(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None
