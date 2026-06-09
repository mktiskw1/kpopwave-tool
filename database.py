from datetime import datetime
from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


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
