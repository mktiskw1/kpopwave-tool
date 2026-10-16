"""YouTubeの「よく再生されている部分」(heatmap)や音声から見どころの範囲を決め、その部分だけをダウンロードする。

- 範囲の決め方: heatmap(視聴者が繰り返し見た場所)があれば、目標の長さの窓をずらして値の合計が最大の範囲を選ぶ。
  無ければ音声だけを先にダウンロードし(動画よりずっと軽い)、音の大きさ(RMS)の合計が最大の範囲を「サビ」と推定する。
  どちらでも決められなければ method=None(今までどおり丸ごとダウンロード)。
- 選んだ範囲の前後に余白を付け、動画の最初と最後をはみ出さないようにする。
- 区間ダウンロードは yt-dlp の download_ranges を使い、force_keyframes_at_cuts=True で切れ目を再エンコードして
  キーフレーム位置による前後のずれを防ぐ。
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

import yt_queue
from config import YOUTUBE_DL_FORMAT
from database import Setting

logger = logging.getLogger(__name__)

CLIP_MIN, CLIP_MAX, CLIP_DEFAULT = 15, 180, 45
PAD_MIN, PAD_MAX, PAD_DEFAULT = 0.0, 10.0, 1.5
GRID = 0.5                   # 窓を動かす刻み(秒)
CHART_POINTS = 100           # 画面のグラフ用に保存する点数
CACHE_TTL = yt_queue.INFO_CACHE_TTL   # 6時間(動画ごと。同じ動画に何度もアクセスしない)

METHOD_HEATMAP = "heatmap"   # よく再生された部分
METHOD_AUDIO = "audio"       # 音声からのサビ推定
METHOD_MANUAL = "manual"     # 手動の切り取り
METHOD_LABELS = {METHOD_HEATMAP: "よく再生された部分", METHOD_AUDIO: "サビ推定(音声)", METHOD_MANUAL: "手動切り取り"}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_FFMPEG_DIR = os.path.join(_BASE_DIR, "ffmpeg", "bin")
_FFMPEG_EXE = os.path.join(_FFMPEG_DIR, "ffmpeg.exe")
_COOKIE_FILE = os.path.join(_BASE_DIR, "instance", "youtube_cookies.txt")
_JS_OPTS = {"js_runtimes": {"node": {}}, "remote_components": {"ejs:github"}}

_cache = {}
_cache_lock = threading.Lock()


# ───────────── 設定 ─────────────

def get_settings() -> tuple:
    """(目標の長さ秒, 余白秒)。app context内で呼ぶこと。範囲外は丸める。"""
    try:
        clip = int(float(Setting.get("highlight_clip_seconds", str(CLIP_DEFAULT)) or CLIP_DEFAULT))
    except (TypeError, ValueError):
        clip = CLIP_DEFAULT
    try:
        pad = float(Setting.get("highlight_padding_seconds", str(PAD_DEFAULT)) or PAD_DEFAULT)
    except (TypeError, ValueError):
        pad = PAD_DEFAULT
    return max(CLIP_MIN, min(CLIP_MAX, clip)), max(PAD_MIN, min(PAD_MAX, pad))


def _ydl_opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, **_JS_OPTS}
    if os.path.exists(_COOKIE_FILE):
        opts["cookiefile"] = _COOKIE_FILE
    opts.update(extra)
    return opts


# ───────────── 情報取得(キャッシュつき) ─────────────

def _cache_get(video_id: str):
    with _cache_lock:
        item = _cache.get(video_id)
        if item and time.time() - item["ts"] < CACHE_TTL:
            return item
    return None


def _cache_put(video_id: str, **kw) -> dict:
    """音の大きさの時系列のキャッシュ(情報取得のキャッシュはyt_queue側)。"""
    with _cache_lock:
        item = _cache.get(video_id) or {"ts": time.time()}
        item.update(kw)
        item["ts"] = time.time()
        _cache[video_id] = item
        if len(_cache) > 200:
            for k in sorted(_cache, key=lambda k: _cache[k]["ts"])[:50]:
                _cache.pop(k, None)
        return item


def fetch_info(video_id: str, allow_network: bool = True) -> dict:
    """動画の情報。{"duration", "title", "heatmap": [(start,end,value)] or None}
    yt_queue.get_info の6時間キャッシュを使う(確認モーダルで取った情報をダウンロードや範囲決定でも使い回す)。
    allow_network=Falseでキャッシュに無ければyt_queue.NeedNetworkを送出する。"""
    info = yt_queue.get_info(video_id, _ydl_opts(), allow_network=allow_network)
    hm = info.get("heatmap") or None
    heatmap = None
    if hm:
        heatmap = [(float(s["start_time"]), float(s["end_time"]), float(s["value"])) for s in hm]
    return {"duration": float(info.get("duration") or 0), "title": info.get("title") or "", "heatmap": heatmap}


# ───────────── 範囲の決定 ─────────────

def _best_window(values, step: float, clip_seconds: float):
    """values(step秒刻みの配列)から、clip_seconds長の窓で合計が最大の範囲(開始秒, 終了秒)を返す。"""
    import numpy as np
    n = len(values)
    w = max(1, int(round(clip_seconds / step)))
    if n <= w:
        return 0.0, n * step
    cs = np.concatenate([[0.0], np.cumsum(values)])
    sums = cs[w:] - cs[:n - w + 1]
    i = int(np.argmax(sums))           # 同点なら最初(最も早い)の位置
    return i * step, (i + w) * step


HEAD_SKIP_SECONDS = 10.0      # heatmapは冒頭が高く出やすいので、先頭のこの秒数(最大で動画の長さの10%)は窓の開始位置にしない
HEAD_SKIP_RATIO = 0.10


def _best_window_heatmap(values, step: float, clip_seconds: float, duration: float):
    """heatmap用の窓選び。(開始秒, 終了秒, 方法) を返す。方法は "heatmap_excess"(新) / "heatmap_plain"(従来にフォールバック)。
    - 先頭の min(10秒, 動画の長さの10%) は窓の開始位置にしない
    - 窓の比べ方は「heatmap全体の平均を超えた分の合計」(max(値-平均, 0)の窓内合計)
    - 同点なら、窓の中心が値の最も高い所に近いほうを選ぶ
    - 窓が取れない・全部が平均以下(平ら)など結果がおかしいときは、従来の素の合計(_best_window)に戻す"""
    import numpy as np
    vals = np.asarray(values, dtype=float)
    n = len(vals)
    w = max(1, int(round(clip_seconds / step)))
    plain = _best_window(vals, step, clip_seconds)
    if n <= w:
        return (*plain, "heatmap_plain")
    skip = int(np.ceil(min(HEAD_SKIP_SECONDS, duration * HEAD_SKIP_RATIO) / step))
    last = n - w                                   # 開始位置の最大
    if skip > last:
        return (*plain, "heatmap_plain")
    excess = np.maximum(vals - vals.mean(), 0.0)
    cs = np.concatenate([[0.0], np.cumsum(excess)])
    sums = (cs[w:] - cs[:n - w + 1])[skip:last + 1]      # 開始位置 skip..last の窓の合計
    best = float(sums.max())
    if not best > 1e-9:                                  # 平均を超える所が無い(平ら)
        return (*plain, "heatmap_plain")
    ties = np.flatnonzero(sums >= best - 1e-9 * max(1.0, best)) + skip
    peaks = np.flatnonzero(vals >= vals.max() - 1e-12)   # 値が最も高い位置
    centers = (ties + w / 2.0)
    dist = np.abs(centers[:, None] - (peaks[None, :] + 0.5)).min(axis=1)
    i = int(ties[int(np.argmin(dist))])                  # 距離が同じなら早いほう(argminは最初を返す)
    return i * step, (i + w) * step, "heatmap_excess"


def _heatmap_to_grid(heatmap, duration: float):
    import numpy as np
    n = max(1, int(round(duration / GRID)))
    grid = np.zeros(n)
    for start, end, value in heatmap:
        a, b = int(round(start / GRID)), int(round(end / GRID))
        grid[max(0, a):min(n, max(b, a + 1))] = value
    return grid


def _decode_audio_energy(audio_path: str):
    """音声ファイルを8kHzモノラルにデコードし、GRID秒ごとのRMS(1.5秒の移動平均)を返す。"""
    import numpy as np
    cmd = [_FFMPEG_EXE, "-v", "quiet", "-i", audio_path, "-ac", "1", "-ar", "8000", "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=180).stdout
    samples = np.frombuffer(raw, dtype=np.float32)
    per = int(8000 * GRID)
    n = len(samples) // per
    if n < 4:
        raise RuntimeError("音声をデコードできませんでした")
    rms = np.sqrt(np.mean(samples[:n * per].reshape(n, per) ** 2, axis=1))
    k = 3
    return np.convolve(rms, np.ones(k) / k, mode="same")


def fetch_audio_energy(video_id: str, allow_network: bool = True):
    """音声だけをダウンロードして(動画よりずっと軽い)音の大きさの時系列を返す。結果はキャッシュする。"""
    cached = _cache_get(video_id)
    if cached and cached.get("energy") is not None:
        return cached["energy"]
    if not allow_network:
        raise yt_queue.NeedNetwork()
    import yt_dlp
    from video_collector import _find_downloaded_file
    tmp_dir = tempfile.mkdtemp(prefix="kpopwave_audio_")
    try:
        opts = _ydl_opts(format="bestaudio[ext=m4a]/bestaudio/best", outtmpl=os.path.join(tmp_dir, f"{video_id}.%(ext)s"))
        with yt_queue.access():
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        found = None
        for fn in os.listdir(tmp_dir):
            if video_id in fn:
                found = os.path.join(tmp_dir, fn)
                break
        if not found:
            raise RuntimeError("音声ファイルが見つかりません")
        energy = _decode_audio_energy(found)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    _cache_put(video_id, energy=energy)
    return energy


def _downsample(values, points: int = CHART_POINTS) -> list:
    """グラフ用に points 個へ間引き、0〜1に正規化した値のリストを返す。"""
    import numpy as np
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return []
    idx = np.linspace(0, len(arr), points + 1, dtype=int)
    out = np.array([arr[a:max(b, a + 1)].mean() for a, b in zip(idx[:-1], idx[1:])])
    peak = out.max()
    return [round(float(v / peak), 3) if peak > 0 else 0.0 for v in out]


def decide(video_id: str, clip_seconds: float = None, padding: float = None, use_audio: bool = True,
           cache_only: bool = False) -> dict:
    """見どころの範囲を決める(ダウンロードはしない。heatmapが無い場合は音声のみダウンロードして推定する)。
    app context内で呼ぶこと。戻り値: {video_id, title, duration, method, start, end, length, clip_seconds, padding,
    chart:{kind, values}, note}。method=None は範囲を決められなかった場合(丸ごとダウンロード)。
    cache_only=Trueならキャッシュだけで答える(ネットワークが必要ならyt_queue.NeedNetworkを送出)。"""
    default_clip, default_pad = get_settings()
    clip = max(CLIP_MIN, min(CLIP_MAX, float(clip_seconds if clip_seconds is not None else default_clip)))
    pad = max(PAD_MIN, min(PAD_MAX, float(padding if padding is not None else default_pad)))
    result = {"video_id": video_id, "title": "", "duration": 0, "method": None, "start": None, "end": None,
              "length": None, "clip_seconds": clip, "padding": pad, "chart": None, "note": ""}
    try:
        info = fetch_info(video_id, allow_network=not cache_only)
    except yt_queue.NeedNetwork:
        raise
    except Exception as exc:
        logger.error("[highlight] 動画情報の取得に失敗 %s: %s", video_id, yt_queue.error_text(exc))
        result["note"] = f"動画情報を取得できませんでした: {yt_queue.error_text(exc)}"
        return result
    duration = info["duration"]
    result.update(title=info["title"], duration=duration)
    if duration <= 0:
        result["note"] = "動画の長さを取得できないため、丸ごとダウンロードします"
        return result
    if duration <= clip + 2 * pad:
        result["note"] = f"動画が目標の長さ({int(clip)}秒)以下のため、丸ごとダウンロードします"
        if info.get("heatmap"):
            result["chart"] = {"kind": METHOD_HEATMAP, "values": _downsample(_heatmap_to_grid(info["heatmap"], duration))}
        return result

    start = end = None
    method = None
    if info.get("heatmap"):
        grid = _heatmap_to_grid(info["heatmap"], duration)
        start, end, how = _best_window_heatmap(grid, GRID, clip, duration)
        logger.info("[highlight] heatmapの窓選び %s: %s %.1f〜%.1f", video_id, how, start, end)
        method = METHOD_HEATMAP
        result["chart"] = {"kind": method, "values": _downsample(grid)}
    elif use_audio:
        try:
            energy = fetch_audio_energy(video_id, allow_network=not cache_only)
            start, end = _best_window(energy, GRID, clip)
            method = METHOD_AUDIO
            result["chart"] = {"kind": method, "values": _downsample(energy)}
        except yt_queue.NeedNetwork:
            raise
        except Exception as exc:
            logger.error("[highlight] 音声からの推定に失敗 %s: %s", video_id, yt_queue.error_text(exc))
            result["note"] = f"音声からの推定に失敗しました: {yt_queue.error_text(exc)}。丸ごとダウンロードします"
            return result
    else:
        result["note"] = "よく再生された部分のデータがありません"
        return result

    start = max(0.0, start - pad)
    end = min(duration, end + pad)
    result.update(method=method, start=round(start, 1), end=round(end, 1), length=round(end - start, 1))
    return result


# ───────────── 区間ダウンロード ─────────────

def download_section(video_id: str, start: float, end: float) -> str:
    """start〜end(秒)の区間だけをダウンロードして static/videos へ配置し、'videos/<名前>.mp4' を返す。
    切れ目はキーフレーム位置でずれないよう再エンコードする(force_keyframes_at_cuts)。画質上限などの形式指定は
    通常のダウンロード(YOUTUBE_DL_FORMAT)と同じ。失敗時は例外。"""
    import yt_dlp
    from yt_dlp.utils import download_range_func
    from video_collector import _find_downloaded_file, _probe_video

    tmp_dir = os.path.join(tempfile.gettempdir(), "kpopwave_videos")
    os.makedirs(tmp_dir, exist_ok=True)
    stem = f"{video_id}_hl"
    for fn in os.listdir(tmp_dir):                      # 前回の取り残しを消す
        if fn.startswith(stem):
            try:
                os.remove(os.path.join(tmp_dir, fn))
            except OSError:
                pass
    opts = _ydl_opts(
        format=YOUTUBE_DL_FORMAT, ffmpeg_location=_FFMPEG_DIR, merge_output_format="mp4",
        outtmpl=os.path.join(tmp_dir, f"{stem}.%(ext)s"),
        download_ranges=download_range_func([], [(float(start), float(end))]),
        force_keyframes_at_cuts=True,
    )
    with yt_queue.access():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    found = _find_downloaded_file(tmp_dir, stem)
    if not found:
        raise RuntimeError("ダウンロードファイルが見つかりません")
    local_path, ext = found
    specs = _probe_video(local_path)
    expected = float(end) - float(start)
    if specs and abs(specs.get("duration", 0) - expected) > 2.5:
        logger.warning("[highlight] 切り出し後の長さが想定とずれています: %.1f秒(想定%.1f秒) %s",
                       specs.get("duration", 0), expected, video_id)
    dest_dir = os.path.join(_BASE_DIR, "static", "videos")
    os.makedirs(dest_dir, exist_ok=True)
    name = f"{video_id}_hl_{int(start)}-{int(end)}.{ext}"
    shutil.copy2(local_path, os.path.join(dest_dir, name))
    try:
        os.remove(local_path)
    except OSError:
        pass
    return f"videos/{name}"


def chart_json(chart) -> str:
    """Article.highlight_heatmapに保存する文字列。"""
    return json.dumps(chart, ensure_ascii=False) if chart else None
