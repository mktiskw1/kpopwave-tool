"""承認待ちに入った動画の投稿文の候補を、裏で先に作っておく(承認モーダルを開いたらすぐ出せるように)。

- 人が操作した取り込み(候補の「取り込む」・URL追加・切り取り・チャプター分割)の直後だけ呼ぶ。毎朝の自動取得からは呼ばない。
- 生成は専用の1本のスレッドで1本ずつ順番に行う(並列にしない)。
- 何を材料に作るか(グループ・メンバー・曲名)はapp.py側の関数(set_builder)が決める。決められなければ作らない。
- 結果はDB(caption_prefetch)に保存する。承認・削除された記事の分は purge_orphans で消す。
"""
import json
import logging
import queue
import threading
from datetime import datetime

from database import Article, CaptionPrefetch, db

logger = logging.getLogger(__name__)

_QUEUE: "queue.Queue" = queue.Queue()
_LOCK = threading.Lock()
_THREAD = None
_BUILDER = None   # fn(article) -> {"group_name", "member_name", "song_title", "ctx"} | None


def set_builder(fn) -> None:
    global _BUILDER
    _BUILDER = fn


def _ensure_thread(app) -> None:
    global _THREAD
    with _LOCK:
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=_worker, args=(app,), daemon=True, name="caption-prefetch")
            _THREAD.start()


def enqueue(app, article_ids) -> int:
    """記事を生成の順番待ちに入れる。戻り値は入れた件数。失敗しても取り込み自体には影響させない。"""
    added = 0
    try:
        with app.app_context():
            for aid in article_ids:
                row = db.session.get(CaptionPrefetch, aid)
                if row is not None and row.status == "running":
                    continue
                if row is None:
                    row = CaptionPrefetch(article_id=aid)
                    db.session.add(row)
                row.status, row.error, row.candidates, row.shortfall = "running", None, None, None
                row.updated_at = datetime.utcnow()
                db.session.commit()
                _QUEUE.put(aid)
                added += 1
        if added:
            _ensure_thread(app)
    except Exception:
        logger.exception("caption_prefetch: 順番待ちへの追加に失敗")
    return added


def recover(app) -> None:
    """再起動で止まった生成(running のまま残った行)を、順番待ちに戻す。"""
    with app.app_context():
        ids = [r.article_id for r in CaptionPrefetch.query.filter_by(status="running").all()]
        if ids:
            for aid in ids:
                _QUEUE.put(aid)
            _ensure_thread(app)
            logger.info("caption_prefetch: 再起動で止まった生成を再開 %d件", len(ids))
        purge_orphans()


def delete_for(article_id: int) -> None:
    CaptionPrefetch.query.filter_by(article_id=article_id).delete(synchronize_session=False)


def purge_orphans() -> int:
    """承認待ち(pending)でなくなった記事・無くなった記事の分を消す。app context内で呼ぶ。"""
    pending_ids = db.session.query(Article.id).filter(Article.status == "pending")
    n = CaptionPrefetch.query.filter(~CaptionPrefetch.article_id.in_(pending_ids)).delete(synchronize_session=False)
    db.session.commit()
    return n


def _worker(app) -> None:
    import caption_ai
    while True:
        aid = _QUEUE.get()
        try:
            with app.app_context():
                _run_one(app, aid, caption_ai)
        except Exception:
            logger.exception("caption_prefetch: 予期しない例外 article_id=%s", aid)
            try:
                with app.app_context():
                    db.session.rollback()
                    row = db.session.get(CaptionPrefetch, aid)
                    if row is not None and row.status == "running":
                        row.status, row.error = "failed", "サーバー内部エラー"
                        db.session.commit()
            except Exception:
                pass


def _run_one(app, aid: int, caption_ai) -> None:
    from database import Setting
    row = db.session.get(CaptionPrefetch, aid)
    article = db.session.get(Article, aid)
    if row is None:
        return
    if article is None or article.status != "pending":
        db.session.delete(row)
        db.session.commit()
        return
    built = _BUILDER(article) if _BUILDER else None
    if not built:   # グループが分からない・曲名が分からない等: 作らない(承認モーダルで手動の「候補を生成」)
        db.session.delete(row)
        db.session.commit()
        logger.info("caption_prefetch: 材料が足りないため作りません article_id=%d", aid)
        return
    row.group_name, row.member_name, row.song_title = built["group_name"], built["member_name"], built["song_title"]
    db.session.commit()
    rules = (Setting.get("caption_rules", "") or "").strip() or caption_ai.DEFAULT_CAPTION_RULES
    model = caption_ai.get_model()
    try:
        res = caption_ai.generate_candidates(rules, model, built["ctx"])
        caption_ai.save_generated([c["text"] for c in res["candidates"]])
        status, error = "done", None
    except caption_ai.CaptionError as e:
        res, status, error = None, "failed", str(e)
    db.session.expire_all()
    row = db.session.get(CaptionPrefetch, aid)
    if row is None:   # 生成中に承認・削除された
        return
    article = db.session.get(Article, aid)
    if article is None or article.status != "pending":
        db.session.delete(row)
        db.session.commit()
        return
    row.status, row.error = status, error
    if res:
        row.candidates = json.dumps(res["candidates"], ensure_ascii=False)
        row.shortfall, row.seconds = res["shortfall"], res["seconds"]
    row.updated_at = datetime.utcnow()
    db.session.commit()
    logger.info("caption_prefetch: %s article_id=%d model=%s %s秒", status, aid, model,
                res["seconds"] if res else "-")
