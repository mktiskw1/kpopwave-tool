"""取り込んだ動画の自動カット。

- 人が操作した取り込み(候補の「取り込む」・URL追加・X動画の追加)の直後に、ダウンロード済みの動画の音声・映像から
  開始・終了を推定し(cut_estimate.py)、カットした動画を承認待ちに入れる。範囲を指定した取り込み・チャプター・手動の
  切り取りでできた動画は対象外(既にカットされている)。毎朝の自動取得は通らない(候補のままで、動画は取り込まれない)。
- 元の動画は article.original_video_path に残し、推定が外れていたら画面から元の動画を切り直せる(app.py の recut)。
- 推定できなかった動画は、カットせずにそのまま承認待ちに入れ、その理由(auto_cut_note)を画面に出す。
- 推定値(cut_est_*)と、実際にカットした値(cut_*。切り直したら最終値)を記録して、精度を集計する(/api/cut-accuracy)。
- 処理は専用の1本のスレッドで1本ずつ(ffmpegが重いので並列にしない)。再起動で止まった分は recover で順番待ちに戻す。
- 設定 autocut_enabled を false にすると、新しい取り込みは自動カットしない(既定は true)。
"""
import logging
import os
import queue
import threading
import time

from database import Article, Setting, db

logger = logging.getLogger(__name__)

PROCESSING, AUTO, FULL, SKIPPED, RECUT = "processing", "auto", "full", "skipped", "recut"
NO_CUT_SEC = 3.0   # 切る長さの合計(冒頭+終わり)がこれ未満なら、カット不要とみなす

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_QUEUE: "queue.Queue" = queue.Queue()
_LOCK = threading.Lock()
_THREAD = None


def _abs(rel: str) -> str:
    return os.path.join(_BASE_DIR, "static", rel)


def is_enabled() -> bool:
    return (Setting.get("autocut_enabled", "true") or "true").strip().lower() == "true"


def _eligible(a) -> bool:
    return (a is not None and (a.content_type or "article") == "video" and a.status == "pending"
            and a.clip_method is None and not a.auto_cut_status and bool(a.video_file_path))


def _ensure_thread(app) -> None:
    global _THREAD
    with _LOCK:
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=_worker, args=(app,), daemon=True, name="autocut")
            _THREAD.start()


def enqueue(app, article_ids, force: bool = False) -> int:
    """記事を自動カットの順番待ちに入れる。戻り値は入れた件数。失敗しても取り込み自体には影響させない。
    force=Trueは、画面の「自動カット」ボタン(設定がオフでも、対象の記事なら実行する)。"""
    added = 0
    try:
        with app.app_context():
            if not force and not is_enabled():
                return 0
            for aid in article_ids:
                a = db.session.get(Article, aid)
                if not _eligible(a):
                    continue
                a.auto_cut_status, a.auto_cut_note = PROCESSING, None
                db.session.commit()
                _QUEUE.put(aid)
                added += 1
        if added:
            _ensure_thread(app)
    except Exception:
        logger.exception("autocut: 順番待ちへの追加に失敗")
    return added


def recover(app) -> None:
    """再起動で止まった自動カット(processingのまま残った記事)を、順番待ちに戻す。"""
    with app.app_context():
        ids = [r[0] for r in db.session.query(Article.id).filter(
            Article.auto_cut_status == PROCESSING, Article.status == "pending").all()]
        # 承認待ちでなくなった記事に processing が残っていたら外す
        for a in Article.query.filter(Article.auto_cut_status == PROCESSING, Article.status != "pending").all():
            a.auto_cut_status = None
        db.session.commit()
        if ids:
            for aid in ids:
                _QUEUE.put(aid)
            _ensure_thread(app)
            logger.info("autocut: 再起動で止まった自動カットを再開 %d件", len(ids))


def _worker(app) -> None:
    while True:
        aid = _QUEUE.get()
        try:
            with app.app_context():
                _run_one(aid)
        except Exception:
            logger.exception("autocut: 予期しない例外 article_id=%s", aid)
            try:
                with app.app_context():
                    db.session.rollback()
                    a = db.session.get(Article, aid)
                    if a is not None and a.auto_cut_status == PROCESSING:
                        a.auto_cut_status, a.auto_cut_note = SKIPPED, "サーバー内部エラーのため、カットしていません"
                        db.session.commit()
            except Exception:
                pass


def _set(aid: int, status: str, note: str) -> None:
    a = db.session.get(Article, aid)
    if a is not None and a.auto_cut_status == PROCESSING:
        a.auto_cut_status, a.auto_cut_note = status, note[:300]
        db.session.commit()


def _fmt(sec: float) -> str:
    sec = max(0.0, float(sec))
    return "%d:%02d" % (int(sec // 60), int(sec % 60))


def _run_one(aid: int) -> None:
    import cut_estimate
    from video_collector import ffmpeg_trim_clip, record_video_spec

    a = db.session.get(Article, aid)
    if a is None or a.status != "pending" or a.auto_cut_status != PROCESSING or not a.video_file_path:
        return
    src_rel = a.video_file_path
    src = _abs(src_rel)
    t0 = time.time()
    est = cut_estimate.estimate(src)
    db.session.expire_all()
    a = db.session.get(Article, aid)
    if a is None or a.status != "pending" or a.auto_cut_status != PROCESSING or a.video_file_path != src_rel:
        return   # 推定中に承認・削除・差し替えされた
    if not est.get("ok"):
        reason = "、".join(est.get("notes") or []) or "理由は不明"
        _set(aid, SKIPPED, "推定できなかったため、カットしていません(%s)" % reason)
        logger.info("autocut: 推定できず article_id=%d %s", aid, reason)
        return
    start, end, duration = est["start"], est["end"], est.get("duration") or 0.0
    a.cut_est_start, a.cut_est_end = start, end
    if start + max(0.0, duration - end) < NO_CUT_SEC:
        a.cut_start, a.cut_end = 0.0, duration
        _set(aid, FULL, "カット不要と推定しました(曲が最初から最後まで続いています)")
        logger.info("autocut: カット不要 article_id=%d %.1f-%.1f / %.1f秒", aid, start, end, duration)
        return
    stem = os.path.splitext(os.path.basename(src_rel))[0]
    n, name = 0, "%s_auto.mp4" % stem
    while os.path.exists(_abs("videos/" + name)):
        n += 1
        name = "%s_auto%d.mp4" % (stem, n)
    dest = _abs("videos/" + name)
    try:
        ffmpeg_trim_clip(src, dest, start, end)
        if not os.path.isfile(dest) or os.path.getsize(dest) < 10_000:
            raise RuntimeError("出力ファイルを確認できません")
    except Exception as exc:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        logger.error("autocut: カット失敗 article_id=%d: %s", aid, str(exc)[-200:])
        _set(aid, SKIPPED, "カットに失敗したため、カットしていません")
        return
    db.session.expire_all()
    a = db.session.get(Article, aid)
    if a is None or a.status != "pending" or a.auto_cut_status != PROCESSING or a.video_file_path != src_rel:
        try:
            os.remove(dest)   # 処理中に承認・削除・差し替えされたので、作ったクリップは捨てる
        except OSError:
            pass
        return
    a.original_video_path = src_rel
    a.video_file_path = "videos/" + name
    a.clip_method = "auto"
    a.cut_est_start, a.cut_est_end = start, end
    a.cut_start, a.cut_end = start, end
    a.orientation = a.video_duration = None
    record_video_spec(a)
    a.auto_cut_status = AUTO
    note = "推定どおりにカットしました: %s〜%s(元の長さ %s)" % (_fmt(start), _fmt(end), _fmt(duration))
    if est.get("card_start"):
        note += " / エンドカード %s〜を除外" % _fmt(est["card_start"])
    a.auto_cut_note = note[:300]
    db.session.commit()
    logger.info("autocut: カット済み article_id=%d %.1f-%.1f / %.1f秒 (%.0f秒)", aid, start, end, duration, time.time() - t0)
