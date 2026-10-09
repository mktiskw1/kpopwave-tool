from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine


db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """SQLite接続ごとにWALモード・busy_timeoutを設定する。
    複数プロセス(app.py本体+run.pyの監視・再起動)からの同時アクセスで
    'database is locked'エラーが起きるのを防ぐため。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.close()


class Article(db.Model):
    __tablename__ = "articles"

    id = db.Column(db.Integer, primary_key=True)
    feed_source = db.Column(db.String(200))
    title = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(1000), unique=True, nullable=False)
    published_at = db.Column(db.DateTime)
    raw_content = db.Column(db.Text)
    summary = db.Column(db.Text)          # Threads 投稿テキスト (要約 + URL)
    status = db.Column(db.String(20), default="pending", index=True)
    # pending → queued → posted
    # pending → rejected
    # queued  → failed
    thumbnail_url = db.Column(db.String(500), nullable=True)
    scheduled_at = db.Column(db.DateTime, nullable=True)
    posted_at = db.Column(db.DateTime, nullable=True)
    threads_post_id = db.Column(db.String(200), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # エンゲージメント指標
    like_count = db.Column(db.Integer, nullable=True)
    view_count = db.Column(db.Integer, nullable=True)
    reply_count = db.Column(db.Integer, nullable=True)
    repost_count = db.Column(db.Integer, nullable=True)
    quote_count = db.Column(db.Integer, nullable=True)
    engagement_fetched_at = db.Column(db.DateTime, nullable=True)
    post_style = db.Column(db.String(20), nullable=True)
    # 複数画像URL（JSON配列テキスト）
    image_urls = db.Column(db.Text, nullable=True)
    # 動画投稿用
    content_type = db.Column(db.String(20), nullable=True, default='article')  # 'article' or 'video'
    video_file_path = db.Column(db.String(500), nullable=True)  # static/videos/xxxx.mp4 形式
    is_fancam = db.Column(db.Boolean, nullable=True, default=False)
    # マルチアカウント対応
    account_id = db.Column(db.Integer, db.ForeignKey("threads_accounts.id"), nullable=True)
    # KPOP分析機能: グループ・メンバータグ付け
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)
    # お気に入り(誤削除防止用のマーク)
    is_favorite = db.Column(db.Boolean, nullable=False, default=False)
    # バズ動画自動再キュー機能
    # 「Threads側のリポスト数」を表す既存のrepost_countと紛らわしいため別名にしている。
    # 通算の投稿回数を表す(初回投稿=1、1回目の再投稿=2、2回目の再投稿=3、…)。
    buzz_repost_count = db.Column(db.Integer, nullable=False, default=1)
    # 再投稿2・3回目のいいね未達を1回だけ猶予するための直近判定フラグ(scheduler._video_cleanup_job参照)。
    buzz_low_streak = db.Column(db.Boolean, nullable=False, default=False)
    # バズ判定(buzz_threshold_likes)を達成し「永久保存」と確定した日時(そのサイクルのposted_at
    # 以降の値なら確定済み扱い)。設定のbuzz_threshold_likesを後から引き上げても、既にこの
    # サイクルで確定済みの動画が遡って削除対象にならないようにするためのスナップショット
    # (scheduler._video_cleanup_job参照)。再投稿されposted_atが更新されると、この値は
    # 古いサイクルのものとみなされ再評価の対象に戻る。
    buzz_threshold_confirmed_at = db.Column(db.DateTime, nullable=True)
    # ツリー2件目(アフィリエイトリンク等)機能: 任意設定。両方Noneなら従来通り1件のみ投稿する。
    thread_reply_text = db.Column(db.Text, nullable=True)
    thread_reply_url = db.Column(db.String(1000), nullable=True)
    # カムバック曲の手動タグ(自動判定はしない)。アフィリ付け忘れ防止のUIヒント用。
    is_comeback = db.Column(db.Boolean, nullable=False, default=False)
    # summaryが手動編集されたかどうか。Trueの間は承認時の自動再組み立て(動画投稿文の
    # グループ名→メンバー名→曲名→フック組み立て)をスキップし、手動編集内容を尊重する。
    # 「要約を生成」(summarize_article)が成功するたびFalseに戻る(自動生成に戻った扱い)。
    summary_is_manual = db.Column(db.Boolean, nullable=False, default=False)
    # YouTube由来の動画の元チャンネル(取り込み時に保存。X等YouTube以外はNone)。
    # チャンネル別の成績集計・削除記録(DeletedPostLog)で使う。
    channel_id = db.Column(db.String(64), nullable=True, index=True)
    channel_name = db.Column(db.String(200), nullable=True)
    # チャンネル監視(WatchedChannel)経由で取り込んだ記事の監視チャンネルID。承認待ちの絞り込みと
    # 「📡 監視」バッジ用。監視チャンネルを削除しても記事側の印は残す(参照制約なし)。
    watched_channel_id = db.Column(db.Integer, nullable=True, index=True)
    # 「優先」を押した日時。キューの記事のうち、これが設定されたものはグループ調整より先に(押した順に)投稿される
    # (group_balance.select_for_slot参照)。投稿・再キュー・取り消しでNULLに戻す。
    priority_requested_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "status": self.status,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "created_at": self.created_at.isoformat(),
        }


class VideoTrimJob(db.Model):
    __tablename__ = "video_trim_jobs"

    id = db.Column(db.Integer, primary_key=True)
    source_article_id = db.Column(db.Integer, db.ForeignKey("articles.id"), nullable=False, index=True)
    start = db.Column(db.Float, nullable=False)
    end = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="processing")
    result_article_id = db.Column(db.Integer, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ThreadsAccount(db.Model):
    __tablename__ = "threads_accounts"

    id = db.Column(db.Integer, primary_key=True)
    account_label = db.Column(db.String(100), nullable=False)
    threads_user_id = db.Column(db.String(100), nullable=True)
    threads_access_token = db.Column(db.Text, nullable=True)
    token_acquired_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    content_topic = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TextPostStock(db.Model):
    """テキスト投稿の一時保管(下書き)。キューには一切影響せず、
    「キューに追加」操作時にArticleへ変換され本レコードは削除される。"""
    __tablename__ = "text_post_stocks"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("threads_accounts.id"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Hook(db.Model):
    __tablename__ = "hooks"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("threads_accounts.id"), nullable=False, index=True)
    phrase = db.Column(db.String(200), nullable=False)
    display_order = db.Column(db.Integer, nullable=False, default=0)
    last_used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def get_active_account(app, account_id: int = None) -> dict:
    """Threadsアカウント情報を取得する。

    account_id 指定時はそのアカウント、省略時は is_active=True のアカウントのうち
    最も id が小さいもの（＝従来の唯一アカウント）を返す。
    見つからない場合は None。
    """
    with app.app_context():
        if account_id is not None:
            acc = ThreadsAccount.query.get(account_id)
        else:
            acc = (
                ThreadsAccount.query
                .filter_by(is_active=True)
                .order_by(ThreadsAccount.id.asc())
                .first()
            )
        if not acc:
            return None
        return {
            "id": acc.id,
            "account_label": acc.account_label,
            "threads_user_id": acc.threads_user_id,
            "threads_access_token": acc.threads_access_token,
        }


class Setting(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, default="")

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key: str, value: str) -> None:
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(cls(key=key, value=value))
        db.session.commit()


class FollowCandidate(db.Model):
    __tablename__ = "follow_candidates"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    display_name = db.Column(db.String(200))
    followers_count = db.Column(db.Integer)          # None = 未取得
    bio = db.Column(db.String(500))
    source = db.Column(db.String(20), default="curated")  # curated / reddit / engagement
    follow_status = db.Column(db.String(20), nullable=True)   # unfollowed / followed
    priority = db.Column(db.String(10), nullable=True)        # high / medium / low
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Comment(db.Model):
    __tablename__ = "comments"

    id = db.Column(db.String(100), primary_key=True)
    post_id = db.Column(db.String(100), nullable=True)   # root post の Threads ID
    username = db.Column(db.String(200), nullable=True)
    text = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.String(50), nullable=True)
    is_read = db.Column(db.Integer, default=0)
    is_replied = db.Column(db.Integer, default=0)
    is_liked = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class BuzzPost(db.Model):
    __tablename__ = "buzz_posts"

    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(50))
    url = db.Column(db.String(1000), nullable=True)
    content = db.Column(db.Text, nullable=False)
    likes = db.Column(db.Integer, default=0)
    comments = db.Column(db.Integer, default=0)
    shares = db.Column(db.Integer, default=0)
    memo = db.Column(db.Text, nullable=True)
    analysis = db.Column(db.Text, nullable=True)  # JSON文字列
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Group(db.Model):
    __tablename__ = "groups"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    normalized_name = db.Column(db.String(100), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Member(db.Model):
    __tablename__ = "members"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    normalized_name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("group_id", "normalized_name", name="uq_member_group_normalized"),)


class PostStat(db.Model):
    __tablename__ = "post_stats"

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey("articles.id"), nullable=False, index=True)
    day_index = db.Column(db.Integer, nullable=False)
    # 投稿直後(60分以内)の初速記録のみ設定する経過分数(15/30/60)。日次記録はNULLのまま。
    minute_offset = db.Column(db.Integer, nullable=True)
    likes = db.Column(db.Integer, default=0)
    views = db.Column(db.Integer, default=0)
    replies = db.Column(db.Integer, default=0)
    reposts = db.Column(db.Integer, default=0)
    quotes = db.Column(db.Integer, default=0)
    is_final = db.Column(db.Boolean, nullable=False, default=False)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)


class EarlyAdvanceLog(db.Model):
    """初速0いいね検出による前倒し投稿の実行履歴(連鎖上限到達・キュー枯渇も記録する)。"""
    __tablename__ = "early_advance_logs"

    id = db.Column(db.Integer, primary_key=True)
    # article削除時はレコード自体は残し、参照だけNULL化する(_cleanup_article_related_records参照)。
    source_article_id = db.Column(db.Integer, nullable=True)
    target_article_id = db.Column(db.Integer, nullable=True)
    chain_position = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(20), nullable=False)  # advanced / limit_reached / no_queue
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class DailyStat(db.Model):
    __tablename__ = "daily_stats"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, nullable=False, index=True)
    stat_date = db.Column(db.Date, nullable=False)
    followers_count = db.Column(db.Integer, nullable=True)
    views_count = db.Column(db.Integer, nullable=True)
    source = db.Column(db.String(10), nullable=False, default="api")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("account_id", "stat_date", name="uq_daily_stat_account_date"),)


class ChapterJob(db.Model):
    __tablename__ = "chapter_jobs"

    id = db.Column(db.Integer, primary_key=True)
    source_url = db.Column(db.String(1000), nullable=False)
    video_id = db.Column(db.String(50), nullable=False)
    video_title = db.Column(db.String(500), nullable=True)
    thumbnail_url = db.Column(db.String(500), nullable=True)
    account_id = db.Column(db.Integer, nullable=True)
    # 既存のダウンロード済みファイルから開始した場合、static/配下の相対パス(Article.video_file_pathと同形式)。
    # 設定されている場合、_run_chapter_jobは再ダウンロードせずこのファイルを使う(処理後も削除しない)。
    source_local_path = db.Column(db.String(500), nullable=True)
    # 元動画のチャンネル。確認時に生成するArticleへ引き継ぐ。
    channel_id = db.Column(db.String(64), nullable=True)
    channel_name = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="processing")  # processing / done / failed
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChapterClip(db.Model):
    __tablename__ = "chapter_clips"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("chapter_jobs.id"), nullable=False, index=True)
    chapter_index = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(500), nullable=False)
    start_time = db.Column(db.Float, nullable=False)
    end_time = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending / processing / done / failed
    video_file_path = db.Column(db.String(500), nullable=True)
    duration = db.Column(db.Float, nullable=True)
    guessed_group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# 削除理由(DeletedPostLog.delete_reason)。scheduler._video_cleanup_jobの判定分岐と対応する。
DELETE_REASON_INITIAL_BELOW_THRESHOLD = "initial_below_threshold"      # 初回でバズ判定未満
DELETE_REASON_REPOST_KILL_LINE = "repost_kill_line"                    # 再投稿で即削除ライン以下
DELETE_REASON_GRACE_EXPIRED = "grace_expired"                          # 2・3回目とも未達(猶予ルール)
DELETE_REASON_REPOST_BELOW_THRESHOLD = "repost_below_threshold"        # 4回目以降で未達(猶予なし)
DELETE_REASON_MANUAL = "manual_delete"                                 # 画面から個別削除
DELETE_REASON_MANUAL_BULK = "manual_bulk_delete"                       # 画面から一括削除


class DeletedPostLog(db.Model):
    """投稿済み記事を削除する際に残す成績の記録(動画ファイルや記事本体は削除してよい)。
    チャンネル別の当たり率・放送局チャンネルのブロック率などを後から集計するために使う。
    記事削除と同じトランザクションで書き込む(record_deleted_post参照)。"""
    __tablename__ = "deleted_post_log"

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, nullable=True, index=True)   # 削除済みのため参照制約なし
    account_id = db.Column(db.Integer, nullable=True)
    title = db.Column(db.String(500), nullable=True)
    video_url = db.Column(db.String(1000), nullable=True)
    youtube_video_id = db.Column(db.String(20), nullable=True)
    channel_id = db.Column(db.String(64), nullable=True, index=True)
    channel_name = db.Column(db.String(200), nullable=True)
    group_name = db.Column(db.String(100), nullable=True)
    member_name = db.Column(db.String(100), nullable=True)
    song_title = db.Column(db.String(200), nullable=True)   # タイトルから推測できた場合のみ
    posted_at = db.Column(db.DateTime, nullable=True)
    buzz_repost_count = db.Column(db.Integer, nullable=True)  # 通算の投稿回数(初回=1)
    final_likes = db.Column(db.Integer, nullable=True)
    final_views = db.Column(db.Integer, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    delete_reason = db.Column(db.String(40), nullable=False)


def _was_posted(article) -> bool:
    """一度でもThreadsへ投稿された記事か(承認前のpending/rejectedは対象外)。
    再投稿待ちでqueuedに戻っている記事も、過去の投稿実績があるため対象に含める。"""
    if article.status == "posted":
        return True
    return article.status == "queued" and article.posted_at is not None


def record_deleted_post(article, reason: str) -> bool:
    """articleの成績をdeleted_post_logへ1行書き込む。呼び出し側のセッション内で実行され、
    記事削除と同じcommitで確定する(commit自体はしない)。

    記録対象外(承認前の記事)ならFalseを返す。書き込みに失敗してもlogにエラーを残して
    Falseを返すだけで例外は投げない(記録失敗で削除処理を止めないため)。
    flushを伴わないsession.executeで書くので、失敗してもセッションは使い続けられる。"""
    try:
        if not _was_posted(article):
            return False

        from channel_info import extract_youtube_video_id

        group_name = member_name = None
        if article.group_id:
            group = db.session.get(Group, article.group_id)
            group_name = group.name if group else None
        if article.member_id:
            member = db.session.get(Member, article.member_id)
            member_name = member.name if member else None

        song_title = None
        try:
            from summarizer import _extract_song_title
            song_title = (_extract_song_title(article.title or "", group_name or "") or None)
        except Exception:
            pass

        likes, views = article.like_count, article.view_count
        if likes is None or views is None:
            latest = (
                PostStat.query.filter_by(article_id=article.id)
                .order_by(PostStat.fetched_at.desc()).first()
            )
            if latest:
                likes = latest.likes if likes is None else likes
                views = latest.views if views is None else views

        db.session.execute(DeletedPostLog.__table__.insert().values(
            article_id=article.id,
            account_id=article.account_id,
            title=(article.title or "")[:500],
            video_url=(article.url or "")[:1000],
            youtube_video_id=extract_youtube_video_id(article.url or "") or None,
            channel_id=article.channel_id,
            channel_name=article.channel_name,
            group_name=group_name,
            member_name=member_name,
            song_title=(song_title or "")[:200] or None,
            posted_at=article.posted_at,
            buzz_repost_count=article.buzz_repost_count or 1,
            final_likes=likes,
            final_views=views,
            deleted_at=datetime.utcnow(),
            delete_reason=reason,
        ))
        return True
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "deleted_post_log書き込み失敗(削除は続行): article_id=%s reason=%s",
            getattr(article, "id", None), reason,
        )
        return False


class WatchedChannel(db.Model):
    """実績のあるYouTubeチャンネルの新着を毎日自動で承認待ちに取り込むための監視対象
    (channel_watcher.run_watch参照)。"""
    __tablename__ = "watched_channel"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, nullable=False, index=True)
    channel_id = db.Column(db.String(64), nullable=False)
    channel_name = db.Column(db.String(200), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    # Trueの間はタイトルにfancam系キーワードを含む動画だけを取り込む
    fancam_required = db.Column(db.Boolean, nullable=False, default=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_fetched_at = db.Column(db.DateTime, nullable=True)
    # アップロード再生リストID(channels.listで取得してキャッシュ。playlistItems.listが1ユニットで済む)
    uploads_playlist_id = db.Column(db.String(64), nullable=True)
    # 次回取得の起点(これより後に公開された動画だけを対象にする)。NULLなら初回(直近7日)。
    # 1回の取り込み上限で打ち切った場合は、最後に処理した動画の公開日時で止めて翌日に続きを処理する。
    cursor_published_at = db.Column(db.DateTime, nullable=True)
    memo = db.Column(db.Text, nullable=True)

    __table_args__ = (db.UniqueConstraint("account_id", "channel_id", name="uq_watched_channel_account_channel"),)


class WatchedCandidate(db.Model):
    """チャンネル監視で見つけた動画の「候補」。ダウンロードせず情報だけを持ち、ユーザーが選んだものだけを
    取り込んでArticle(承認待ち)にする。集計・クリーンアップ・再投稿などArticleを対象とする処理に
    混ざらないよう、Articleとは別テーブルにしている(channel_watcher参照)。
    status: new(未確認) / imported(取り込み済み) / skipped(見送り) / expired(期限切れ)。
    expiredは重複防止の対象外で、再スキャンで再び見つかるとnewに戻る(skippedは戻らない)。"""
    __tablename__ = "watched_candidate"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, nullable=False, index=True)
    watched_channel_id = db.Column(db.Integer, nullable=True, index=True)
    # 状態に関わらず1動画1行(見送り・期限切れも残して二度と候補に出さない)
    video_id = db.Column(db.String(20), nullable=False, unique=True)
    title = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text, nullable=True)       # 取り込み時にArticle.raw_contentへ引き継ぐ
    channel_id = db.Column(db.String(64), nullable=True)
    channel_name = db.Column(db.String(200), nullable=True)
    thumbnail_url = db.Column(db.String(500), nullable=True)
    published_at = db.Column(db.DateTime, nullable=True)  # YouTube上の公開日時(UTC)
    duration = db.Column(db.Integer, nullable=True)       # 秒
    view_count = db.Column(db.Integer, nullable=True)     # YouTube上の再生数(取得時点)
    guessed_group = db.Column(db.String(200), nullable=True)
    guessed_group_id = db.Column(db.Integer, nullable=True)  # 一意に決まった場合のみ(取り込み時のタグ付けに使う)
    # 動画の向き: landscape(横) / portrait(縦) / unknown(判定できなかった)。NULLは未判定(起動時に補完する)。
    orientation = db.Column(db.String(10), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="new", index=True)
    found_at = db.Column(db.DateTime, default=datetime.utcnow)
    status_changed_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)        # 直近の取り込み失敗理由(失敗時は未確認のまま残す)
    imported_article_id = db.Column(db.Integer, nullable=True)


class GroupPlanSetting(db.Model):
    """グループごとの手動補正(投稿頻度の倍率)とカムバック補正の期限(group_balance参照)。
    group_id=NULLは「グループなし」。"""
    __tablename__ = "group_plan_setting"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, nullable=True, unique=True)
    manual_factor = db.Column(db.Float, nullable=False, default=1.0)   # 0〜3。0なら出さない
    comeback_until = db.Column(db.DateTime, nullable=True)             # この日時までカムバック補正をかける
