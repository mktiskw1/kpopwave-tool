"""動画ファイルの削除ヘルパー。

記事(Article)を削除するときに、その記事の動画ファイルと派生ファイル(<元ID>_clip_N.mp4 / <元ID>_original.mp4)を
消すが、**他の記事・チャプター処理から参照されているファイルは消さない**。
以前は「ベース名が前方一致するファイルを無条件に削除」していたため、元動画の記事を削除すると、同じ元動画から
作られた別記事のクリップ(例: vxnGh3NYcmA_clip_1.mp4)まで消えてしまい、そのクリップの記事を投稿するときに
動画ファイルが無い状態になっていた(2026-10-07の動画なし投稿の原因)。
"""
import logging
import os

from database import Article, ChapterClip, ChapterJob, db

logger = logging.getLogger(__name__)


def referenced_video_basenames(exclude_article_ids=()) -> set:
    """DBのどこかから参照されている動画ファイル名(ベース名)の集合。exclude_article_idsの記事は参照元として数えない
    (これから削除する記事自身)。app contextの中で呼ぶこと。"""
    exclude = set(exclude_article_ids or ())
    refs = set()
    for aid, path in db.session.query(Article.id, Article.video_file_path).filter(Article.video_file_path.isnot(None)):
        if aid not in exclude:
            refs.add(os.path.basename(path))
    for (path,) in db.session.query(ChapterClip.video_file_path).filter(ChapterClip.video_file_path.isnot(None)):
        refs.add(os.path.basename(path))
    for (path,) in db.session.query(ChapterJob.source_local_path).filter(ChapterJob.source_local_path.isnot(None)):
        refs.add(os.path.basename(path))
    return refs


def delete_article_video_files(video_file_path: str, static_dir: str, exclude_article_ids=(), referenced=None) -> int:
    """記事の動画ファイル本体と派生ファイル(_clip_*/_original*)を削除する。削除した件数を返す。
    他の記事等から参照されているファイルは削除しない。referencedを渡すと参照集合の再計算を省ける。"""
    if not video_file_path:
        return 0
    if referenced is None:
        referenced = referenced_video_basenames(exclude_article_ids)
    base_name = os.path.splitext(os.path.basename(video_file_path))[0]
    videos_dir = os.path.join(static_dir, "videos")
    deleted = 0

    main_path = os.path.join(static_dir, video_file_path)
    if os.path.exists(main_path):
        if os.path.basename(main_path) in referenced:
            logger.info("動画ファイルは他の記事から参照されているため削除しません: %s", video_file_path)
        else:
            try:
                os.remove(main_path)
                deleted += 1
            except OSError:
                pass

    if os.path.isdir(videos_dir):
        for fname in os.listdir(videos_dir):
            if not (fname.endswith(".mp4") and (
                fname.startswith(base_name + "_clip_") or fname.startswith(base_name + "_original")
            )):
                continue
            if fname in referenced:
                logger.info("派生ファイルは他の記事から参照されているため削除しません: %s", fname)
                continue
            try:
                os.remove(os.path.join(videos_dir, fname))
                deleted += 1
            except OSError:
                pass
    return deleted
