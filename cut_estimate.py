"""切り取りの開始・終了の推定(承認待ちの切り取り欄の初期値)。ダウンロード済みの動画ファイルだけを使う(YouTubeには触れない)。

誠さんのカットのルール:
  1. 冒頭: 曲が始まる前の部分をカットする
  2. 終わり: 歌い終わって曲が止まったあとに続いている部分をカットする
  3. エンドカード: 最後に出るチャンネル名の表示をカットする

推定の方法:
  - 音声(16kHz モノラル)の音量と、映像の動きから、演奏している区間(一番長い連続区間)を決める。
    音声が途切れる区間(消音など)でも、映像が動いていれば演奏が続いているとみなす。開始は音が鳴り始めた位置、終了は音も映像の動きも止まった位置
  - エンドカードは、末尾の「静止した画面」「急な場面転換のあとの動きの少ない画面」で検出する
    (KBS Kpop・M2 は静止、SBS 系は場面転換のあとほぼ動かない)。個人チャンネルの動画は端カードが無いことが多い
  - 終了は min(曲の終わり + 余白, エンドカードの開始 - 余白)
推定は初期値で、誠さんが画面で確認して直す前提。失敗しても例外は投げず、理由つきで空の結果を返す。
"""
import logging
import os
import subprocess

import numpy as np

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_FFMPEG = os.path.join(_BASE_DIR, "ffmpeg", "bin", "ffmpeg.exe")
_FFPROBE = os.path.join(_BASE_DIR, "ffmpeg", "bin", "ffprobe.exe")

SR = 16000
HOP = 0.1            # 音量を見る刻み(秒)
WIN = 0.2
ACTIVE_BELOW_REF_DB = 14.0   # 「曲が鳴っている」とみなす、基準の音量からの下がり幅(dB)
SMOOTH_SEC = 1.0
MERGE_GAP_SEC = 2.0          # この秒数までの小さな途切れは同じ曲として扱う
SILENCE_DB = -70.0           # これより小さい音量は無音(エンドカードの無音の判定)

# 誠さんの手動カット(35本)から測った余白の中央値(秒)。曲が鳴り始めてからの冒頭の余白と、曲が止まってからの終わりの余白
LEAD_MARGIN = 0.2
TAIL_MARGIN = 0.0

MOTION_ACTIVE = 10.0         # 映像の動き(フレーム間の平均差)がこれ以上なら、演奏中とみなす(演奏中は25〜50、終演後は1〜8)
MOTION_SMOOTH_SEC = 5.0
MOTION_FPS = 2.0

CARD_MIN_SEC = 3.0           # これ以上続く末尾の静止・動き小を、エンドカードとみなす
STATIC_DIFF = 1.5            # 画面の変化が小さい(静止)とみなすフレーム間の平均差(0〜255)
CUT_SPIKE = 80.0             # 急な場面転換とみなすフレーム間の差
LOWMOTION_DIFF = 20.0        # 場面転換のあと、これ以下の動きしか無ければエンドカード
VIDEO_WINDOW_SEC = 45.0


def _duration(path):
    r = subprocess.run([_FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=30)
    return float(r.stdout.strip())


def _load_audio(path):
    cmd = [_FFMPEG, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=120).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _levels_db(x):
    h, w = int(SR * HOP), int(SR * WIN)
    n = max(0, (len(x) - w) // h + 1)
    db = np.empty(n, dtype=np.float32)
    for i in range(n):
        f = x[i * h:i * h + w]
        db[i] = 20.0 * np.log10(np.sqrt(np.mean(f * f)) + 1e-9)
    return db


def _smooth(a, n):
    if n <= 1 or len(a) == 0:
        return a
    pad = n // 2
    p = np.pad(a, (pad, n - 1 - pad), mode="edge")
    k = np.ones(n) / n
    return np.convolve(p, k, mode="valid")


def _motion_in_range(path, a, b):
    """区間[a, b]秒の映像の動き(0.5秒ごとのフレーム間の平均差)。取れなければNone。"""
    cmd = [_FFMPEG, "-v", "error", "-ss", "%.2f" % max(0.0, a), "-i", path, "-t", "%.2f" % (b - a), "-an",
           "-vf", "fps=%g,scale=160:90,format=gray" % MOTION_FPS, "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    fr = np.frombuffer(raw, dtype=np.uint8)
    fr = fr[:len(fr) // (160 * 90) * (160 * 90)].reshape(-1, 160 * 90).astype(np.float32)
    if len(fr) < 3:
        return None
    return np.mean(np.abs(np.diff(fr, axis=0)), axis=1)


MOTION_BUDGET_SEC = 200.0   # 映像の動きを調べる区間の合計の上限(AV1など重い動画で時間がかかりすぎないように)


def _motion_grid(path, audio_grid, step, duration):
    """音声が無い区間(曲の途中の消音・曲の後)だけ映像の動きを調べて、動きがある0.5秒刻みの真偽値を返す。
    音声が鳴っている区間は調べない(デコードに時間がかかる動画があるため)。"""
    n = len(audio_grid)
    mm = np.zeros(n, dtype=bool)
    idx = np.where(audio_grid)[0]
    if len(idx) == 0:
        return mm
    first = int(idx[0])
    gaps = _runs(~audio_grid[first:], 0)       # 音声の無い区間(0.5秒刻みの番号)
    gaps = [[g[0] + first, g[1] + first] for g in gaps if g[1] - g[0] >= int(MERGE_GAP_SEC / step)]
    budget = MOTION_BUDGET_SEC
    for g0, g1 in sorted(gaps, key=lambda g: g[0]):
        a, b = max(0.0, g0 * step - 1.0), min(duration, g1 * step + 1.0)
        if b - a < 2.0 or budget < (b - a):
            continue
        budget -= (b - a)
        d = _motion_in_range(path, a, b)
        if d is None:
            continue
        act = _smooth(d, max(1, int(MOTION_SMOOTH_SEC * MOTION_FPS))) >= MOTION_ACTIVE
        k0 = int(round(a / step))
        for i, v in enumerate(act):
            if 0 <= k0 + i < n and v:
                mm[k0 + i] = True
    return mm


def _runs(mask, gap):
    runs = []
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    if not runs:
        return []
    merged = [runs[0]]
    for a, b in runs[1:]:
        if a - merged[-1][1] <= gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return merged


def _song_run(db, path=None, duration=None):
    """演奏している一番長い連続区間(開始秒, 終了秒)。決められなければNone。
    音声: 基準の音量から14dB以内。映像: 音声が無い区間でも動きがあれば(消音など)演奏中とみなす。
    開始は音が鳴り始めた位置、終了は音も動きも止まった位置。"""
    loud = db[db > -60.0]
    if len(loud) < 10:
        return None
    ref = float(np.percentile(loud, 90))
    sm = _smooth(db, max(1, int(SMOOTH_SEC / HOP)))
    audio_active = sm >= ref - ACTIVE_BELOW_REF_DB
    step = 0.5
    n = int(len(db) * HOP / step)
    grid = np.zeros(n, dtype=bool)
    per = int(step / HOP)
    for k in range(n):
        grid[k] = bool(audio_active[k * per:(k + 1) * per].any())
    union = grid.copy()
    if path:
        try:
            union = grid | _motion_grid(path, grid, step, duration if duration else n * step)
        except Exception as exc:
            logger.warning("映像の動きの取得に失敗(音声だけで続行): %s", exc)
    runs = _runs(union, int(MERGE_GAP_SEC / step))
    if not runs:
        return None
    best = max(runs, key=lambda r: r[1] - r[0])
    run_start, run_end = best[0] * step, best[1] * step
    # 開始: 演奏区間の中で一番長く続く音の区間の始まり(曲の前の話し声・歓声の短い音は無視する)
    inner = [r for r in _runs(grid, int(MERGE_GAP_SEC / step)) if r[0] >= best[0] - 1 and r[1] <= best[1] + 1]
    start = max(inner, key=lambda r: r[1] - r[0])[0] * step if inner else run_start
    return max(start, run_start), run_end + WIN


def _card_start_from_video(path, duration):
    """末尾のエンドカードの開始秒。無ければNone。"""
    ss = max(0.0, duration - VIDEO_WINDOW_SEC)
    cmd = [_FFMPEG, "-v", "error", "-ss", str(ss), "-i", path, "-an",
           "-vf", "fps=5,scale=160:90,format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=120).stdout
    if len(raw) < 160 * 90 * 4:
        return None
    fr = np.frombuffer(raw, dtype=np.uint8)
    fr = fr[:len(fr) // (160 * 90) * (160 * 90)].reshape(-1, 160 * 90).astype(np.float32)
    d = np.mean(np.abs(np.diff(fr, axis=0)), axis=1)
    fps = 5.0
    total = len(fr) / fps
    # (1) 静止した末尾: 最後に画面が動いた(STATIC_DIFF超)あとの静止区間
    big = np.where(d > STATIC_DIFF)[0]
    last_change = (big[-1] + 1) / fps if len(big) else 0.0
    static_len = total - last_change
    cand = []
    if static_len >= CARD_MIN_SEC and static_len < VIDEO_WINDOW_SEC - 1.0:
        cand.append(ss + last_change)
    # (2) 急な場面転換のあと、最後まで動きが小さい
    spikes = np.where(d > CUT_SPIKE)[0]
    for k in spikes[::-1]:
        after = d[k + 1:]
        if (len(d) - k - 1) / fps >= CARD_MIN_SEC and (len(after) == 0 or float(after.max()) <= LOWMOTION_DIFF):
            cand.append(ss + (k + 1) / fps)
            break
    return min(cand) if cand else None


def estimate(path):
    """{"ok", "start", "end", "duration", "song_start", "song_end", "card_start", "notes": [...]}。
    startとendは0.5秒単位に丸めた秒(切り取り欄の初期値)。決められないときは ok=False と理由。"""
    out = {"ok": False, "start": None, "end": None, "duration": None, "song_start": None, "song_end": None,
           "card_start": None, "notes": []}
    try:
        if not os.path.isfile(path):
            out["notes"].append("動画ファイルが見つかりません")
            return out
        duration = _duration(path)
        out["duration"] = round(duration, 1)
        if duration > 900:
            out["notes"].append("長い動画(15分超)は推定しません")
            return out
        db = _levels_db(_load_audio(path))
        run = _song_run(db, path, duration)
        if run is None:
            out["notes"].append("音声から曲の区間を決められませんでした")
            return out
        song_start, song_end = run
        out["song_start"], out["song_end"] = round(song_start, 1), round(song_end, 1)
        limit = duration
        card = None
        try:
            card = _card_start_from_video(path, duration)
        except Exception as exc:
            logger.warning("エンドカードの検出に失敗(続行): %s", exc)
        # 末尾の無音(音声が最後まで無い)も、曲の終わりの上限にする
        idx = np.where(db > SILENCE_DB)[0]
        sound_end = (idx[-1] + 1) * HOP + WIN if len(idx) else duration
        if card is not None:
            out["card_start"] = round(card, 1)
            limit = min(limit, card)
            out["notes"].append("末尾にエンドカードを検出(%.1f秒〜)" % card)
        limit = min(limit, sound_end)
        start = max(0.0, song_start - LEAD_MARGIN)
        end = min(limit, song_end + TAIL_MARGIN)
        if end - start < 5.0:
            out["notes"].append("推定した範囲が短すぎます")
            return out
        out["start"] = round(start * 2) / 2
        out["end"] = round(end * 2) / 2
        out["ok"] = True
        return out
    except Exception as exc:
        logger.warning("切り取り範囲の推定に失敗: %s", exc)
        out["notes"].append("推定に失敗しました")
        return out
