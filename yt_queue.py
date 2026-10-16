"""YouTubeにアクセスする処理の待ち行列。

YouTubeに続けてアクセスすると「Sign in to confirm you're not a bot」などで止められるため、
yt-dlpを使うYouTubeへのアクセス(情報取得・ダウンロード・音声取得)はすべてこの待ち行列を通す。

- ジョブはDB(youtube_jobs)に保存する。1本ずつ処理し、アクセスの後に 60秒±30%(42〜78秒)空けて次を始める。
  アプリの再起動で消えない。再起動したとき「処理中」のまま残ったものは「待ち」に戻す。
- 止められたと判定したら(is_block_error)、全てのYouTubeアクセスを6時間止める。停止の終わる時刻は
  settings の youtube_paused_until(UTC)に保存し、過ぎたら自動で再開する。処理中だった/待っていたジョブは
  消さずに残す。同じジョブが止められて再試行するのは2回まで。
- 画面が結果を待つ処理(確認モーダルの情報取得など)は priority=0 で、ダウンロード(priority=1)より先に処理する。
- extract_info の結果は動画ごとに6時間キャッシュし(get_info)、後のダウンロードや範囲決定で使い回す。
- 1つのジョブが1回の実行で行うYouTubeアクセスは1回だけ。2回目のアクセスが必要になったら access() が
  YtContinue を投げてジョブを「待ち」に戻し、次の枠(間隔後)で続きを処理する(キャッシュのおかげで
  既に済んだアクセスはやり直さない)。
"""
import json
import logging
import random
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from database import Setting, YoutubeJob, db

logger = logging.getLogger(__name__)

# ── 設定値 ───────────────────────────────────────────────────────────────
INTERVAL_SECONDS = 60          # アクセスの間隔(秒)の中心
INTERVAL_JITTER = 0.30         # ±30%(42〜78秒)
PAUSE_HOURS = 6                # 止められたときの停止時間
MAX_RETRIES = 2                # 止められたジョブを再試行する回数の上限
INFO_CACHE_TTL = 6 * 3600      # extract_info の結果を覚えておく時間
INFO_CACHE_MAX = 300
KEEP_FINISHED = 300            # 履歴として残す完了ジョブの数

PAUSED_UNTIL_KEY = "youtube_paused_until"
PAUSE_REASON_KEY = "youtube_pause_reason"
NEXT_SLOT_KEY = "youtube_next_slot_at"

# ── 「止められた」と判定する文言(判定はここだけ。小文字で比較する) ─────────────────
BLOCK_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "login required",
    "http error 429",
    "too many requests",
)
# 「Sign in to confirm your age」は年齢制限の動画で、アクセス制限ではないので止めない
BLOCK_EXCLUDES = ("confirm your age",)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


# ── 例外 ─────────────────────────────────────────────────────────────────
# BaseExceptionにして、ハンドラ内の `except Exception` に握りつぶされないようにする

class YtBlocked(BaseException):
    """YouTubeに止められた(ボット判定・429など)。メッセージはyt-dlpの元のエラー文。"""


class YtContinue(BaseException):
    """このジョブは1回の実行で2回目のYouTubeアクセスが必要。次の枠で続きを処理する。"""


class YtPaused(BaseException):
    """停止中のためYouTubeにアクセスできない。"""


class NeedNetwork(Exception):
    """キャッシュだけでは答えられない(ネットワークアクセスが必要)。"""


def error_text(exc) -> str:
    """yt-dlpなどの例外の元のエラー文(色コードだけ取り除く)。"""
    return _ANSI_RE.sub("", str(exc)).strip() or exc.__class__.__name__


def is_block_error(exc_or_text) -> bool:
    text = (error_text(exc_or_text) if isinstance(exc_or_text, BaseException) else str(exc_or_text)).lower()
    if any(x in text for x in BLOCK_EXCLUDES):
        return False
    return any(m in text for m in BLOCK_MARKERS)


def extract_video_id(url_or_id: str):
    s = (url_or_id or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = _VIDEO_ID_RE.search(s)
    return m.group(1) if m else None


def is_youtube_url(url: str) -> bool:
    u = (url or "").lower()
    return "youtube.com" in u or "youtu.be" in u


# ── 時刻 ─────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.utcnow()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


_JST = timezone(timedelta(hours=9))


def to_jst_str(dt, with_date: bool = False) -> str:
    """UTCのnaive datetimeをJSTの 'HH:MM'(with_date=Trueなら 'MM/DD HH:MM')にする。"""
    if dt is None:
        return ""
    local = dt.replace(tzinfo=timezone.utc).astimezone(_JST)
    return local.strftime("%m/%d %H:%M" if with_date else "%H:%M")


def _jitter_seconds() -> float:
    return INTERVAL_SECONDS * random.uniform(1 - INTERVAL_JITTER, 1 + INTERVAL_JITTER)


# ── 停止・次の枠(settingsに保存) ────────────────────────────────────────────

def get_paused_until():
    """停止中ならその終了時刻(UTC)、そうでなければNone。app context内で呼ぶこと。"""
    until = _parse(Setting.get(PAUSED_UNTIL_KEY, ""))
    return until if until and until > _now() else None


def get_pause_reason() -> str:
    return Setting.get(PAUSE_REASON_KEY, "") or ""


def pause(reason: str, hours: float = PAUSE_HOURS) -> datetime:
    until = _now() + timedelta(hours=hours)
    Setting.set(PAUSED_UNTIL_KEY, _iso(until))
    Setting.set(PAUSE_REASON_KEY, (reason or "")[:1500])
    logger.error("[yt_queue] YouTubeに止められたため %s 時間停止します(再開 %s JST): %s",
                 hours, to_jst_str(until, True), reason)
    _wake.set()
    return until


def resume_now() -> None:
    """停止を解除して、待っているジョブをすぐ処理し始められるようにする。"""
    Setting.set(PAUSED_UNTIL_KEY, "")
    Setting.set(PAUSE_REASON_KEY, "")
    Setting.set(NEXT_SLOT_KEY, "")
    _mem_slot.update(at=None, dirty=False)
    logger.warning("[yt_queue] 停止を解除しました(手動)")
    _wake.set()


_mem_slot = {"at": None, "dirty": False}   # 次の枠。ハンドラの途中でDBをコミットしないよう、まずメモリに持ち、ジョブの区切りで保存する


def get_next_slot():
    if _mem_slot["at"] is not None:
        return _mem_slot["at"]
    return _parse(Setting.get(NEXT_SLOT_KEY, ""))


def _set_next_slot_after_access() -> None:
    _mem_slot["at"] = _now() + timedelta(seconds=_jitter_seconds())
    _mem_slot["dirty"] = True


def _persist_next_slot() -> None:
    if _mem_slot["dirty"] and _mem_slot["at"] is not None:
        Setting.set(NEXT_SLOT_KEY, _iso(_mem_slot["at"]))
        _mem_slot["dirty"] = False


# ── ジョブの登録・参照 ───────────────────────────────────────────────────────

_handlers = {}
_wake = threading.Event()
_worker_lock = threading.Lock()
_worker_started = False


def register(kind: str, fn, priority: int = 1, multi: bool = False, volatile: bool = False) -> None:
    """kind のジョブを処理する関数 fn(payload: dict) を登録する。
    multi=True: 1回の実行で何度もアクセスする長い処理(その場で間隔を空けて待つ)。
    volatile=True: 再起動で待っている呼び出し元が消えるジョブ(再起動時に取り消す)。"""
    _handlers[kind] = {"fn": fn, "priority": priority, "multi": multi, "volatile": volatile}


def enqueue(kind: str, payload: dict, key: str = None, title: str = None, priority: int = None) -> YoutubeJob:
    """ジョブを待ち行列に入れる。同じ種類・同じ引数のジョブが待ち/処理中ならそれを返す。app context内で呼ぶこと。"""
    h = _handlers.get(kind)
    if h is None:
        raise ValueError(f"未登録のジョブ種別です: {kind}")
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    dup = YoutubeJob.query.filter(
        YoutubeJob.kind == kind, YoutubeJob.payload == body,
        YoutubeJob.status.in_(("waiting", "running")),
    ).first()
    if dup:
        return dup
    job = YoutubeJob(kind=kind, key=(key or "")[:100] or None, title=(title or "")[:300] or None, payload=body,
                     priority=h["priority"] if priority is None else priority)
    db.session.add(job)
    db.session.commit()
    _wake.set()
    return job


def _waiting_ordered():
    return YoutubeJob.query.filter_by(status="waiting").order_by(YoutubeJob.priority, YoutubeJob.id).all()


def _eta_base(running, next_slot, paused_until):
    now = _now()
    base = max(now, next_slot) if next_slot else now
    if running is not None:
        base = max(base, now) + timedelta(seconds=INTERVAL_SECONDS)
    if paused_until:
        base = max(base, paused_until)
    return base


def job_view(job: YoutubeJob, waiting=None, running=None, next_slot=None, paused_until=None) -> dict:
    """画面に返すジョブの状態。position=待ち行列の何件目か(1始まり。処理中は0)、eta=開始予定の目安。"""
    out = {"job_id": job.id, "state": job.status, "kind": job.kind, "title": job.title or job.key or "",
           "position": 0, "eta": "", "eta_iso": None}
    if job.status == "waiting":
        waiting = waiting if waiting is not None else _waiting_ordered()
        ids = [j.id for j in waiting]
        pos = ids.index(job.id) + 1 if job.id in ids else 0
        if running is None:
            running = YoutubeJob.query.filter_by(status="running").first()
        next_slot = next_slot if next_slot is not None else get_next_slot()
        paused_until = paused_until if paused_until is not None else get_paused_until()
        base = _eta_base(running, next_slot, paused_until)
        eta = base + timedelta(seconds=INTERVAL_SECONDS * max(0, pos - 1))
        out.update(position=pos, eta=to_jst_str(eta), eta_iso=_iso(eta))
    paused = get_paused_until() if paused_until is None else paused_until
    if paused:
        out["paused_until"] = to_jst_str(paused, True)
    if job.status in ("done", "failed", "cancelled"):
        res = json.loads(job.result) if job.result else None
        out["result"] = res
        out["error"] = job.error
    return out


def snapshot() -> dict:
    """待ち行列全体の状態(画面の表示用)。"""
    running = YoutubeJob.query.filter_by(status="running").first()
    waiting = _waiting_ordered()
    next_slot = get_next_slot()
    paused_until = get_paused_until()
    base = _eta_base(running, next_slot, paused_until)
    items = []
    for i, j in enumerate(waiting[:30]):
        eta = base + timedelta(seconds=INTERVAL_SECONDS * i)
        items.append({"job_id": j.id, "kind": j.kind, "title": j.title or j.key or "", "position": i + 1,
                      "eta": to_jst_str(eta), "attempts": j.attempts})
    now = _now()
    return {
        "ok": True,
        "waiting_count": len(waiting),
        "running": ({"job_id": running.id, "kind": running.kind, "title": running.title or running.key or ""}
                    if running else None),
        "waiting": items,
        "next_start": to_jst_str(base) if (waiting or running) else "",
        "next_slot": to_jst_str(next_slot) if next_slot and next_slot > now else "",
        "paused": bool(paused_until),
        "paused_until": to_jst_str(paused_until, True) if paused_until else "",
        "pause_reason": get_pause_reason() if paused_until else "",
    }


def cancel(job_id: int) -> bool:
    """待ち行列から外す(待ち中のものだけ)。"""
    job = db.session.get(YoutubeJob, job_id)
    if job is None or job.status != "waiting":
        return False
    job.status = "cancelled"
    job.finished_at = _now()
    job.result = json.dumps({"body": {"ok": False, "error": "待ち行列から外しました"}, "status": 409}, ensure_ascii=False)
    db.session.commit()
    return True


def wait_for_job(job_id: int, timeout: float = None):
    """(バックグラウンドのスレッド用)ジョブが終わるまで待って、結果の本文(body)を返す。失敗・取り消しは例外。
    app context内で呼ぶこと。"""
    start = time.time()
    while True:
        db.session.rollback()
        row = db.session.query(YoutubeJob.status, YoutubeJob.result, YoutubeJob.error).filter_by(id=job_id).first()
        if row is None:
            raise RuntimeError("待ち行列のジョブが見つかりません")
        status, result, error = row
        if status in ("done", "failed", "cancelled"):
            res = json.loads(result) if result else {}
            body = res.get("body")
            if status == "done" and (res.get("status") or 200) < 400:
                return body
            msg = (body or {}).get("error") if isinstance(body, dict) else None
            raise RuntimeError(msg or error or "YouTube処理に失敗しました")
        if timeout is not None and time.time() - start > timeout:
            raise TimeoutError("YouTube処理の待ち時間が上限を超えました")
        time.sleep(2)


# ── YouTubeアクセスの窓口 ─────────────────────────────────────────────────

_ctx = threading.local()


@contextmanager
def access():
    """yt-dlpでYouTubeにアクセスする処理を囲む。待ち行列のワーカーの中でしか使えない(それ以外は例外)。
    アクセスが終わったら次の枠(60秒±30%後)を予約する。止められたエラーはYtBlockedにして送出する。"""
    if getattr(_ctx, "job_id", None) is None:
        raise RuntimeError("YouTubeへのアクセスは待ち行列(yt_queue)の中でのみ行えます")
    multi = getattr(_ctx, "multi", False)
    if getattr(_ctx, "accessed", False) and not multi:
        raise YtContinue()
    if get_paused_until():
        raise YtPaused()
    if multi and getattr(_ctx, "accessed", False):          # 長い処理は、その場で次の枠まで待つ
        while True:
            slot = get_next_slot()
            wait = (slot - _now()).total_seconds() if slot else 0
            if wait <= 0:
                break
            time.sleep(min(wait, 5))
            if get_paused_until():
                raise YtPaused()
    _ctx.accessed = True
    try:
        yield
    except BaseException as exc:
        if isinstance(exc, Exception) and is_block_error(exc):
            raise YtBlocked(error_text(exc)) from exc
        raise
    finally:
        try:
            _set_next_slot_after_access()
        except Exception:
            logger.exception("[yt_queue] 次の枠の保存に失敗")


# ── extract_info のキャッシュ(6時間) ─────────────────────────────────────────

_info_cache = {}
_info_lock = threading.Lock()


def info_cache_get(video_id: str):
    with _info_lock:
        item = _info_cache.get(video_id)
        if item and time.time() - item["ts"] < INFO_CACHE_TTL:
            return item["info"]
    return None


def info_cache_put(video_id: str, info: dict) -> None:
    with _info_lock:
        _info_cache[video_id] = {"ts": time.time(), "info": info}
        if len(_info_cache) > INFO_CACHE_MAX:
            for k in sorted(_info_cache, key=lambda k: _info_cache[k]["ts"])[:INFO_CACHE_MAX // 4]:
                _info_cache.pop(k, None)


def get_info(url_or_id: str, ydl_opts: dict = None, allow_network: bool = True) -> dict:
    """動画の情報(extract_info, ダウンロードなし)。動画ごとに6時間キャッシュするので、同じ動画に何度も
    アクセスしない。allow_network=Falseでキャッシュに無ければNeedNetworkを送出する。"""
    import yt_dlp
    vid = extract_video_id(url_or_id)
    if vid:
        cached = info_cache_get(vid)
        if cached is not None:
            return cached
    if not allow_network:
        raise NeedNetwork()
    url = url_or_id if "/" in url_or_id else f"https://www.youtube.com/watch?v={url_or_id}"
    opts = {"quiet": True, "no_warnings": True, **(ydl_opts or {})}
    with access():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info) if info else None
    if not info:
        raise RuntimeError("動画情報を取得できませんでした")
    key = vid or info.get("id")
    if key:
        info_cache_put(key, info)
    return info


# ── ワーカー ───────────────────────────────────────────────────────────────

def _normalize_result(out):
    """ハンドラの戻り値(Flaskのレスポンス / (レスポンス, ステータス) / 辞書)を {"body", "status"} にする。"""
    status = 200
    if isinstance(out, tuple):
        out, status = out[0], (out[1] if len(out) > 1 else 200)
    if hasattr(out, "get_json"):
        status = status if isinstance(status, int) and status != 200 else getattr(out, "status_code", 200)
        out = out.get_json()
    return {"body": out, "status": status}


def _finish(job, status, result=None, error=None):
    job.status = status
    job.finished_at = _now()
    if result is not None:
        job.result = json.dumps(result, ensure_ascii=False, default=str)
    if error is not None:
        job.error = error[:4000]
    db.session.commit()


def _trim_finished():
    old = YoutubeJob.query.filter(YoutubeJob.status.in_(("done", "failed", "cancelled"))) \
        .order_by(YoutubeJob.id.desc()).offset(KEEP_FINISHED).all()
    for j in old:
        db.session.delete(j)
    if old:
        db.session.commit()


def _run_job(app, job_id: int) -> None:
    job = db.session.get(YoutubeJob, job_id)
    h = _handlers.get(job.kind)
    if h is None:
        _finish(job, "failed", {"body": {"ok": False, "error": f"未対応のジョブ種別: {job.kind}"}, "status": 500},
                f"未対応のジョブ種別: {job.kind}")
        return
    job.status = "running"
    job.started_at = _now()
    db.session.commit()
    _ctx.job_id, _ctx.accessed, _ctx.multi = job_id, False, h["multi"]
    try:
        out = h["fn"](json.loads(job.payload or "{}"))
        db.session.rollback()
        job = db.session.get(YoutubeJob, job_id)
        _finish(job, "done", _normalize_result(out))
    except YtContinue:
        db.session.rollback()
        job = db.session.get(YoutubeJob, job_id)
        job.status = "waiting"
        db.session.commit()
    except YtPaused:
        db.session.rollback()
        job = db.session.get(YoutubeJob, job_id)
        job.status = "waiting"
        db.session.commit()
    except YtBlocked as exc:
        db.session.rollback()
        raw = str(exc)
        pause(raw)
        job = db.session.get(YoutubeJob, job_id)
        job.attempts = (job.attempts or 0) + 1
        if job.attempts > MAX_RETRIES:
            msg = f"YouTubeに止められたため、{MAX_RETRIES}回再試行しましたが処理できませんでした: {raw}"
            _finish(job, "failed", {"body": {"ok": False, "error": msg}, "status": 503}, raw)
        else:
            job.status = "waiting"
            job.error = raw[:4000]
            db.session.commit()
    except Exception as exc:
        db.session.rollback()
        raw = error_text(exc)
        logger.exception("[yt_queue] ジョブ失敗 id=%s kind=%s: %s", job_id, job.kind, raw)
        job = db.session.get(YoutubeJob, job_id)
        _finish(job, "failed", {"body": {"ok": False, "error": raw}, "status": 500}, raw)
    finally:
        _ctx.job_id = None
        _ctx.accessed = False
        _ctx.multi = False


def _tick(app) -> None:
    with app.app_context():
        try:
            paused = Setting.get(PAUSED_UNTIL_KEY, "")
            until = _parse(paused)
            if until:
                if until > _now():
                    _wake.wait(min(30.0, max(1.0, (until - _now()).total_seconds())))
                    _wake.clear()
                    return
                logger.warning("[yt_queue] 停止時間が終わったので自動で再開します")
                Setting.set(PAUSED_UNTIL_KEY, "")
                Setting.set(PAUSE_REASON_KEY, "")
                _set_next_slot_after_access()
                _persist_next_slot()
            slot = get_next_slot()
            now = _now()
            if slot and slot > now:
                _wake.wait(min(5.0, (slot - now).total_seconds()))
                _wake.clear()
                return
            job = YoutubeJob.query.filter_by(status="waiting").order_by(YoutubeJob.priority, YoutubeJob.id).first()
            if job is None:
                _wake.wait(5.0)
                _wake.clear()
                return
            try:
                _run_job(app, job.id)
            finally:
                _persist_next_slot()
            _trim_finished()
        finally:
            db.session.remove()


def _loop(app) -> None:
    while True:
        try:
            _tick(app)
        except Exception:
            logger.exception("[yt_queue] ワーカーでエラー(5秒後に続行)")
            time.sleep(5)


def recover(app) -> int:
    """起動時: 処理中のまま残ったジョブを「待ち」に戻す(再起動で呼び出し元が消えるジョブは取り消す)。"""
    with app.app_context():
        n = 0
        for job in YoutubeJob.query.filter_by(status="running").all():
            h = _handlers.get(job.kind) or {}
            if h.get("volatile"):
                _finish(job, "cancelled", {"body": {"ok": False, "error": "アプリ再起動により中断されました"}, "status": 503},
                        "アプリ再起動により中断されました")
            else:
                job.status = "waiting"
                n += 1
        for job in YoutubeJob.query.filter_by(status="waiting").all():
            if (_handlers.get(job.kind) or {}).get("volatile"):
                _finish(job, "cancelled", {"body": {"ok": False, "error": "アプリ再起動により中断されました"}, "status": 503},
                        "アプリ再起動により中断されました")
        db.session.commit()
        if n:
            logger.warning("[yt_queue] 処理中だったジョブ %d 件を待ちに戻しました", n)
        return n


def start_worker(app) -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    recover(app)
    threading.Thread(target=_loop, args=(app,), name="yt_queue_worker", daemon=True).start()
    logger.info("[yt_queue] ワーカーを開始しました")
