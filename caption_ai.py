"""承認待ち画面の「候補を生成」: 動画の投稿文の候補を6案、Claude Code の CLI(claude -p)で作る。

- APIキーは使わない。このPCの Claude Code に保存済みのサブスクのログインをそのまま使う
  (認証ファイルやトークンには触れない。子プロセスの環境変数から ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN は外す)。
- ボタンを押したときだけ実行する(スケジューラなどからは呼ばない)。
- 生成は別スレッドで走らせ、画面は結果をポーリングで待つ。
- ログには所要時間と total_cost_usd だけを残し、本文は出さない。
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

logger = logging.getLogger(__name__)

CLI_TIMEOUT_SECONDS = 120
RECENT_POSTS_LIMIT = 15
CANDIDATE_COUNT = 6

# 生成ルールの初期値(設定画面で編集できる。Setting "caption_rules")
DEFAULT_CAPTION_RULES = """\
あなたはKPOP動画をThreadsに投稿するときの、短い投稿文を考える編集者です。
1回につき、型の違う候補をちょうど6案作ります。

【構成】
- 2段構成にする。1行目＝止める一文(スクロールを止める)、2行目＝返信させる一文(コメントしたくなる問いかけ)。
- 6案は、止める一文の型・返信させる一文の型が互いに重ならないようにする。
- 各行は短く、読み上げて自然な日本語にする。
- グループ名と曲名は、指示された範囲で必ず本文のどこかに入れる(メンバー動画はメンバー名も入れる)。
  ただし「書かない」と指示された項目は入れない。推測で補わない。

【書いてはいけないもの】
- 韓国語、URL、ハッシュタグ(#)、絵文字、ネガティブな表現。
- LE SSERAFIMのHONG EUNCHAEは「EUNCHAE」だけで表記する。

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
ユーザーが直近の投稿文を渡す。そこで使われた表現・言い回し・型の連続を避ける。
"""

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "minItems": CANDIDATE_COUNT,
            "maxItems": CANDIDATE_COUNT,
            "items": {
                "type": "object",
                "properties": {
                    "stop_type": {"type": "string", "description": "止める一文の型の名前"},
                    "reply_type": {"type": "string", "description": "返信させる一文の型の名前"},
                    "stop": {"type": "string", "description": "1行目: 止める一文"},
                    "reply": {"type": "string", "description": "2行目: 返信させる一文"},
                },
                "required": ["stop_type", "reply_type", "stop", "reply"],
            },
        },
    },
    "required": ["candidates"],
}

_HANGUL_RE = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
# 絵文字(主な範囲)
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿️⭐❤]")

# ログイン切れなどを見分ける語(画面には元のメッセージをそのまま出し、案内を1行足す)
_LOGIN_HINTS = ("login", "log in", "authenticat", "unauthorized", "401", "oauth", "credential", "api key", "expired")


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


# ───────────── プロンプト作り ─────────────

def build_user_prompt(*, group_name: str, member_name: str, song_title: str, video_title: str,
                      recent_texts: list, group_mode: str, song_mode: str) -> str:
    """group_mode: 'named'(グループ名あり) / 'none'(書かない)。
    song_mode: 'named'(曲名あり) / 'none'(書かない)。"""
    lines = ["次の動画の投稿文の候補を6案作ってください。", "", "【動画の情報】", f"- YouTubeのタイトル: {video_title}"]
    if group_mode == "named":
        lines.append(f"- グループ名: {group_name}(本文に必ず入れる)")
    else:
        lines.append("- グループ名: 不明。グループ名は書かない。推測で書かない。")
    if member_name and group_mode == "named":
        lines.append(f"- メンバー名: {member_name}(メンバー動画なので本文に入れる)")
    if song_mode == "named":
        # 韓国語は本文に入れられないので、英字の部分があればそれだけを使い、無ければ読みで書かせる
        latin = re.sub(r"\s+", " ", _HANGUL_RE.sub(" ", song_title)).strip(" -–—|・()（）[]'\"「」")
        if _HANGUL_RE.search(song_title) and re.search(r"[A-Za-z0-9]", latin):
            song_title = latin
        if _HANGUL_RE.search(song_title):
            lines.append(f"- 曲名: {song_title}(本文に必ず入れる。韓国語は使えないので、カタカナかローマ字に直して書く)")
        else:
            lines.append(f"- 曲名: {song_title}(本文に必ず入れる)")
    else:
        lines.append("- 曲名: 不明。曲名は書かない。推測で書かない。")
    lines.append("")
    if recent_texts:
        lines.append("【直近の投稿文(新しい順)。ここで使った表現・型の連続を避ける】")
        for i, t in enumerate(recent_texts, 1):
            lines.append(f"{i}. " + t.replace("\n", " / "))
    else:
        lines.append("【直近の投稿文】なし")
    return "\n".join(lines)


# ───────────── 実行 ─────────────

class CaptionError(Exception):
    pass


def _explain_failure(message: str) -> str:
    message = (message or "").strip() or "原因不明のエラー"
    low = message.lower()
    if any(h in low for h in _LOGIN_HINTS):
        message += "\n(ログインが切れている可能性があります。VSCodeかターミナルで /login してください)"
    return message


def run_claude(system_prompt: str, user_prompt: str) -> tuple:
    """claude -p を1回呼ぶ。(候補のリスト, total_cost_usd, 所要秒) を返す。失敗時は CaptionError。"""
    exe = find_claude_exe()
    if not exe:
        raise CaptionError("claude の実行ファイルが見つかりません(PATH と %USERPROFILE%\\.local\\bin\\claude.exe を探しました)")
    cmd = [
        exe, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(CAPTION_SCHEMA, ensure_ascii=False),
        "--system-prompt", system_prompt,
        "--tools", "",
        "--model", "sonnet",
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
    logger.info("caption_ai: exit=%s 所要=%.1fs total_cost_usd=%s", proc.returncode, elapsed, cost)

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
    items = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise CaptionError("生成結果に候補がありませんでした")
    return items, cost, elapsed


def clean_candidates(items: list) -> list:
    """候補を {text, stop_type, reply_type} に整える。韓国語・URL・タグ・絵文字を含むものは除く。"""
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        stop = str(it.get("stop") or "").strip()
        reply = str(it.get("reply") or "").strip()
        if not stop or not reply:
            continue
        text = stop + "\n" + reply
        if _HANGUL_RE.search(text) or _URL_RE.search(text) or "#" in text or "＃" in text or _EMOJI_RE.search(text):
            continue
        out.append({"text": text, "stop_type": str(it.get("stop_type") or ""), "reply_type": str(it.get("reply_type") or "")})
    return out


# ───────────── ジョブ(ポーリング用) ─────────────

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _prune_jobs():
    cutoff = time.time() - 3600
    for k in [k for k, v in _JOBS.items() if v["created"] < cutoff]:
        _JOBS.pop(k, None)


def start_job(system_prompt: str, user_prompt: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _prune_jobs()
        _JOBS[job_id] = {"status": "running", "created": time.time(), "candidates": None, "error": None,
                         "cost": None, "seconds": None}

    def _work():
        try:
            items, cost, secs = run_claude(system_prompt, user_prompt)
            cands = clean_candidates(items)
            with _JOBS_LOCK:
                job = _JOBS[job_id]
                job.update(cost=cost, seconds=round(secs, 1))
                if len(cands) < 3:
                    job.update(status="failed", error="ルールに合う候補が十分に作れませんでした(韓国語・URL・タグ・絵文字を含む案を除いたため)。もう一度生成してください")
                else:
                    job.update(status="done", candidates=cands)
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
