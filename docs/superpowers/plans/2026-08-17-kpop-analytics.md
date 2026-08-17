# KPOPアカウント分析機能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KPOPアカウント（account_id=1）専用の分析機能を実装する。承認時にグループ・メンバーを自由入力でタグ付けし、投稿から7日間のパフォーマンス（いいね数・閲覧数等）を日次追跡して確定値として蓄積し、アカウント全体のフォロワー数・閲覧数を日次スナップショットし、それらを「分析」ダッシュボードでグループ別・メンバー別・時間帯別に可視化する。

**Architecture:** 新規`groups`/`members`マスタテーブルと、承認モーダルからの自由入力→正規化キーでの自動マスタ解決。新規`post_stats`（記事ごと7日間の日次スナップショット、`is_final`で確定値管理）・`daily_stats`（アカウント全体の日次フォロワー数・閲覧数、手動投入分は`source="manual"`で保護）テーブルを新設し、専用モジュール`analytics_tracker.py`と2つの日次スケジューラジョブ（2:30/3:30 JST）で継続的に蓄積する。`/analytics`ルートはaccount_id=1固定でSQL集計を行い、Chart.js（CDN）で可視化する。既存の`engagement_tracker.py`（全アカウント・無期限）とは完全に独立して併存させる。

**Tech Stack:** Python 3.x / Flask / SQLAlchemy / SQLite / Jinja2 / vanilla JS（fetch API）/ Chart.js（CDN）/ APScheduler

## Global Constraints

- 対象は**account_id=1（KPOPアカウント）のみ**。ガチャアカウント（account_id=2）の承認フロー・投稿フローは一切変更しない
- 「保存数」はThreads APIに指標が存在しないため実装しない
- コメントは原則書かない（WHYが非自明な場合のみ1行）
- 必要な変更だけ行う。リファクタリング・クリーンアップは不要
- Shellコマンドの実行環境はWindows。Bashツールを使う場合はGit Bash構文（`$VAR`、`&&`可）、PowerShellツールを使う場合はPowerShell構文（`$env:VAR`、`&&`不可）に注意する
- このプロジェクトにはpytest等のテストフレームワークが存在しない。各タスクの検証は、`venv/Scripts/python.exe -c "..."`によるDB直接確認・`app.test_client()`によるHTTPリクエスト・`unittest.mock.patch`によるネットワーク呼び出しのモック化で行う
- `venv/Scripts/python.exe`が仮想環境のPythonインタプリタ
- **重要**: `app.py`は`app = create_app()`をモジュール読み込み時に実行する。`test_client()`での検証には必ず`from app import app`を使うこと（`from app import create_app; app = create_app()`は無関係な別インスタンスを作るため404になる）
- 開発サーバー（ポート5000）はセッションを跨いで古いプロセスが残ることがある。新たに`python app.py`/`python run.py`を起動しないこと。`app.test_client()`を使えば起動中のサーバーに依存せず検証できる
- 新規テーブル（`groups`/`members`/`post_stats`/`daily_stats`）は`create_app()`内の`db.create_all()`で自動作成される。`articles`への新規カラム（`group_id`/`member_id`）のみ`_migrate_db()`に`ALTER TABLE`を追記する

---

## ファイル構成

| ファイル | 変更内容 |
|---|---|
| `database.py` | `Group`/`Member`/`PostStat`/`DailyStat`モデルを追加、`Article`に`group_id`/`member_id`カラムを追加 |
| `app.py` | `_migrate_db()`にALTER TABLE追加、タグ正規化・解決ヘルパー追加、`approve_article`拡張、`pending`が`all_groups`を渡すよう変更、`/analytics`・`/analytics/daily-stats/add`ルート追加 |
| `analytics_tracker.py` | 新規作成。7日間投稿パフォーマンス追跡（`track_post_stats`）、日次アカウントスナップショット（`snapshot_daily_stats`）、それぞれのパース用ピュア関数 |
| `scheduler.py` | 2つの新規日次ジョブ（`post_stats_daily` 2:30 JST、`daily_snapshot` 3:30 JST）を追加 |
| `templates/pending.html` | 承認モーダル（グループ・メンバー自由入力、KPOPアカウントのみ表示）を追加、`approveArticle`のJSを拡張 |
| `templates/analytics.html` | 新規作成。分析ダッシュボード（推移グラフ・グループ別/メンバー別テーブル・時間帯別テーブル、手動データ投入フォーム） |
| `templates/base.html` | デスクトップサイドバー・モバイルナビバー両方に「分析」リンクを追加（KPOPアカウントのみ表示） |

---

### Task 1: データモデル追加（`database.py`）

**Files:**
- Modify: `database.py:44`（`Article`クラスに`group_id`/`member_id`カラムを追加）
- Modify: `database.py:187`（ファイル末尾、`BuzzPost`クラスの後に新規モデル4つを追加）

**Interfaces:**
- Produces: `Group`（id, name, normalized_name, created_at）、`Member`（id, group_id, name, normalized_name, created_at）、`PostStat`（id, article_id, day_index, likes, views, replies, reposts, quotes, is_final, fetched_at）、`DailyStat`（id, account_id, stat_date, followers_count, views_count, source, created_at）。`Article.group_id`/`Article.member_id`。以降の全タスクがこれらを利用する

- [ ] **Step 1: `Article`クラスに`group_id`/`member_id`カラムを追加**

`database.py:42-45`（目印）:

```python
    # マルチアカウント対応
    account_id = db.Column(db.Integer, db.ForeignKey("threads_accounts.id"), nullable=True)

    def to_dict(self):
```

これを以下に置き換える:

```python
    # マルチアカウント対応
    account_id = db.Column(db.Integer, db.ForeignKey("threads_accounts.id"), nullable=True)
    # KPOP分析機能: グループ・メンバータグ付け
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)

    def to_dict(self):
```

- [ ] **Step 2: ファイル末尾に新規モデル4つを追加**

`database.py`末尾（`BuzzPost`クラス、目印）:

```python
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
```

これを以下に置き換える（末尾に追記）:

```python
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
    likes = db.Column(db.Integer, default=0)
    views = db.Column(db.Integer, default=0)
    replies = db.Column(db.Integer, default=0)
    reposts = db.Column(db.Integer, default=0)
    quotes = db.Column(db.Integer, default=0)
    is_final = db.Column(db.Boolean, nullable=False, default=False)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)


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
```

- [ ] **Step 3: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile database.py
```
Expected: エラーなし。

- [ ] **Step 4: テーブルが自動作成されることを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app
from database import Group, Member, PostStat, DailyStat, Article, db
with app.app_context():
    db.create_all()
    print('groups columns:', [c.name for c in Group.__table__.columns])
    print('members columns:', [c.name for c in Member.__table__.columns])
    print('post_stats columns:', [c.name for c in PostStat.__table__.columns])
    print('daily_stats columns:', [c.name for c in DailyStat.__table__.columns])
    print('article has group_id:', hasattr(Article, 'group_id'))
    print('article has member_id:', hasattr(Article, 'member_id'))
"
```
Expected: 4テーブルそれぞれの列名一覧が出力され、`article has group_id: True`・`article has member_id: True`。

- [ ] **Step 5: Commit**

```bash
git add database.py
git commit -m "$(cat <<'EOF'
feat: KPOP分析機能用のGroup/Member/PostStat/DailyStatモデルを追加

グループ・メンバーマスタ、投稿別7日間パフォーマンス履歴、
アカウント全体の日次フォロワー・閲覧数スナップショットの
4テーブルを新設。articlesにgroup_id/member_idを追加した。
新規テーブルはdb.create_all()で自動作成される。
EOF
)"
```

---

### Task 2: グループ・メンバータグ付け（`app.py`）

**Files:**
- Modify: `app.py:14`（sqlalchemyインポートに`text`を追加）
- Modify: `app.py:1-9`（先頭に`import unicodedata`を追加）
- Modify: `app.py:18`（databaseインポートに`Group`, `Member`を追加）
- Modify: `app.py:83-97`（`_migrate_db()`の`article_cols`に`group_id`/`member_id`を追加）
- Modify: `app.py:519-521`（`pending()`が`all_groups`を渡すよう変更）
- Modify: `app.py:561-563`（タグ正規化・解決ヘルパーを追加）
- Modify: `app.py:563-583`（`approve_article`を拡張）

**Interfaces:**
- Consumes: `Group`/`Member`モデル（Task 1）
- Produces: `_normalize_tag_name(name: str) -> str`、`_resolve_group_and_member(group_name: str, member_name: str) -> tuple[int|None, int|None]`。`approve_article`が`group_name`/`member_name`（JSON body、任意）を受理する。Task 3（テンプレート）がこの拡張されたAPIを呼ぶ

- [ ] **Step 1: `sqlalchemy`インポートに`text`を追加**

`app.py:14`（目印）:

```python
from sqlalchemy import or_
```

これを以下に置き換える:

```python
from sqlalchemy import or_, text
```

- [ ] **Step 2: `unicodedata`をインポートに追加**

`app.py:1-9`（目印）:

```python
import json
import logging
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qs
```

これを以下に置き換える:

```python
import json
import logging
import os
import re
import secrets
import threading
import unicodedata
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qs
```

- [ ] **Step 3: `database`インポートに`Group`・`Member`を追加**

`app.py:18`（目印）:

```python
from database import Article, BuzzPost, Comment, Hook, Setting, ThreadsAccount, VideoTrimJob, get_active_account, db
```

これを以下に置き換える:

```python
from database import (
    Article, BuzzPost, Comment, DailyStat, Group, Hook, Member, PostStat,
    Setting, ThreadsAccount, VideoTrimJob, get_active_account, db,
)
```

- [ ] **Step 4: `_migrate_db()`の`article_cols`に`group_id`/`member_id`を追加**

`app.py:83-97`（目印）:

```python
    article_cols = [
        ("thumbnail_url", "VARCHAR(500)"),
        ("like_count", "INTEGER"),
        ("reply_count", "INTEGER"),
        ("repost_count", "INTEGER"),
        ("quote_count", "INTEGER"),
        ("engagement_fetched_at", "DATETIME"),
        ("post_style", "VARCHAR(20)"),
        ("image_urls", "TEXT"),
        ("content_type", "VARCHAR(20) DEFAULT 'article'"),
        ("video_file_path", "VARCHAR(500)"),
        ("is_fancam", "INTEGER DEFAULT 0"),
        ("view_count", "INTEGER"),
        ("account_id", "INTEGER"),
    ]
```

これを以下に置き換える:

```python
    article_cols = [
        ("thumbnail_url", "VARCHAR(500)"),
        ("like_count", "INTEGER"),
        ("reply_count", "INTEGER"),
        ("repost_count", "INTEGER"),
        ("quote_count", "INTEGER"),
        ("engagement_fetched_at", "DATETIME"),
        ("post_style", "VARCHAR(20)"),
        ("image_urls", "TEXT"),
        ("content_type", "VARCHAR(20) DEFAULT 'article'"),
        ("video_file_path", "VARCHAR(500)"),
        ("is_fancam", "INTEGER DEFAULT 0"),
        ("view_count", "INTEGER"),
        ("account_id", "INTEGER"),
        ("group_id", "INTEGER"),
        ("member_id", "INTEGER"),
    ]
```

- [ ] **Step 5: `pending()`が`all_groups`をテンプレートに渡すよう変更**

`app.py:519-521`（目印）:

```python
    return render_template("pending.html", articles=articles, images_map=images_map,
                           active_tab=tab, counts=counts, now_utc=datetime.utcnow(),
                           active_trim_jobs=active_trim_jobs)
```

これを以下に置き換える:

```python
    all_groups = Group.query.order_by(Group.name.asc()).all()

    return render_template("pending.html", articles=articles, images_map=images_map,
                           active_tab=tab, counts=counts, now_utc=datetime.utcnow(),
                           active_trim_jobs=active_trim_jobs, all_groups=all_groups)
```

- [ ] **Step 6: タグ正規化・解決ヘルパーを追加**

`app.py:559-563`（目印。`delete_all_pending`の末尾、`approve_article`の直前）:

```python
    return redirect(url_for("pending", account_id=account_id) if account_id else url_for("pending"))


@app.route("/articles/<int:id>/approve", methods=["POST"])
def approve_article(id):
```

これを以下に置き換える:

```python
    return redirect(url_for("pending", account_id=account_id) if account_id else url_for("pending"))


def _normalize_tag_name(name: str) -> str:
    """グループ・メンバー名の表記ゆれ（前後空白・全角/半角・大文字小文字）を吸収する正規化キーを作る。"""
    return unicodedata.normalize("NFKC", (name or "").strip()).lower()


def _resolve_group_and_member(group_name: str, member_name: str) -> tuple:
    """自由入力のグループ名・メンバー名からgroup_id・member_idを解決する。
    マスタに存在しなければ自動作成する。group_nameが空なら (None, None)。
    group_nameが空でmember_nameだけある場合はmember_nameを無視する。"""
    group_name = (group_name or "").strip()
    member_name = (member_name or "").strip()

    if not group_name:
        if member_name:
            logger.warning("グループ名なしでメンバー名のみ指定されたため無視: member_name=%r", member_name)
        return None, None

    norm = _normalize_tag_name(group_name)
    group = Group.query.filter_by(normalized_name=norm).first()
    if not group:
        group = Group(name=group_name, normalized_name=norm)
        db.session.add(group)
        db.session.flush()

    if not member_name:
        return group.id, None

    mnorm = _normalize_tag_name(member_name)
    member = Member.query.filter_by(group_id=group.id, normalized_name=mnorm).first()
    if not member:
        member = Member(group_id=group.id, name=member_name, normalized_name=mnorm)
        db.session.add(member)
        db.session.flush()

    return group.id, member.id


@app.route("/articles/<int:id>/approve", methods=["POST"])
def approve_article(id):
```

- [ ] **Step 7: `approve_article`が`group_name`/`member_name`を受理するよう拡張**

`app.py`内、以下のブロック（目印。Step 6で挿入した直後）:

```python
@app.route("/articles/<int:id>/approve", methods=["POST"])
def approve_article(id):
    from scheduler import next_post_slot
    from datetime import timedelta

    article = Article.query.get_or_404(id)
    article.status = "queued"

    slot_utc = next_post_slot(app, account_id=article.account_id)
```

これを以下に置き換える:

```python
@app.route("/articles/<int:id>/approve", methods=["POST"])
def approve_article(id):
    from scheduler import next_post_slot
    from datetime import timedelta

    article = Article.query.get_or_404(id)
    article.status = "queued"

    tag_data = request.get_json(silent=True) or {}
    group_name = tag_data.get("group_name", "")
    member_name = tag_data.get("member_name", "")
    if group_name or member_name:
        group_id, member_id = _resolve_group_and_member(group_name, member_name)
        article.group_id = group_id
        article.member_id = member_id

    slot_utc = next_post_slot(app, account_id=article.account_id)
```

- [ ] **Step 8: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile app.py
```
Expected: エラーなし。

- [ ] **Step 9: `_resolve_group_and_member`の正規化・自動作成ロジックを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app, _resolve_group_and_member
from database import Group, Member, db

with app.app_context():
    # 前後空白・全角/半角・大文字小文字ゆれが同一グループに解決されることを確認
    gid1, mid1 = _resolve_group_and_member('IVE', 'ウォニョン')
    gid2, mid2 = _resolve_group_and_member('ive ', 'ウォニョン')
    gid3, mid3 = _resolve_group_and_member('ＩＶＥ', '')
    print('gid1==gid2==gid3:', gid1 == gid2 == gid3)
    print('mid1==mid2:', mid1 == mid2)
    print('member未入力時 mid3 is None:', mid3 is None)
    print('groups件数（IVEは1件のみのはず）:', Group.query.filter_by(normalized_name='ive').count())

    # グループ名なしでメンバー名だけ指定した場合は両方Noneになることを確認
    gid4, mid4 = _resolve_group_and_member('', 'なんとか')
    print('group空でmember指定時、両方None:', gid4 is None and mid4 is None)

    db.session.rollback()
"
```
Expected: `gid1==gid2==gid3: True`、`mid1==mid2: True`、`member未入力時 mid3 is None: True`、`groups件数（IVEは1件のみのはず）: 1`、`group空でmember指定時、両方None: True`。

- [ ] **Step 10: `approve_article`にタグ情報を渡した場合の動作を確認する**

```bash
venv/Scripts/python.exe -c "
from app import app
from database import Article, Group, Member, db

with app.app_context():
    art = Article(
        feed_source='テスト', title='タグ付けテスト記事',
        url='https://example.com/tag-test-1', status='pending', account_id=1,
        summary='テスト本文',
    )
    db.session.add(art)
    db.session.commit()
    article_id = art.id

client = app.test_client()
r = client.post(f'/articles/{article_id}/approve',
                 json={'group_name': 'aespa', 'member_name': 'ウィンター'},
                 headers={'X-Requested-With': 'fetch'})
print('status:', r.status_code, r.get_json())

with app.app_context():
    a = db.session.get(Article, article_id)
    print('status→queued:', a.status)
    print('group_id設定済み:', a.group_id is not None)
    print('member_id設定済み:', a.member_id is not None)
    g = db.session.get(Group, a.group_id)
    m = db.session.get(Member, a.member_id)
    print('group.name:', g.name)
    print('member.name:', m.name)
    db.session.delete(a)
    db.session.commit()

# タグ情報なしでの承認（ガチャアカウント等の従来経路）が壊れていないことも確認
with app.app_context():
    art2 = Article(
        feed_source='テスト', title='タグなし承認テスト',
        url='https://example.com/tag-test-2', status='pending', account_id=2,
        summary='テスト本文',
    )
    db.session.add(art2)
    db.session.commit()
    article_id2 = art2.id

r2 = client.post(f'/articles/{article_id2}/approve', headers={'X-Requested-With': 'fetch'})
print('タグなし承認 status:', r2.status_code, r2.get_json())
with app.app_context():
    a2 = db.session.get(Article, article_id2)
    print('タグなし承認 group_id is None:', a2.group_id is None)
    db.session.delete(a2)
    db.session.commit()
"
```
Expected: 1件目は`status→queued: queued`、`group_id設定済み: True`、`member_id設定済み: True`、`group.name: aespa`、`member.name: ウィンター`。2件目（JSON body なしの従来呼び出し）も`200`で成功し、`group_id is None: True`（既存呼び出し元との後方互換を破壊していない）。

- [ ] **Step 11: Commit**

```bash
git add app.py
git commit -m "$(cat <<'EOF'
feat: 承認時のグループ・メンバータグ付けを追加

approve_articleがJSON bodyのgroup_name/member_name（任意）を受理し、
正規化キーでマスタを自動解決・自動作成してarticle.group_id/member_id
にセットするようにした。タグ情報を渡さない従来の呼び出しは影響を
受けない。
EOF
)"
```

---

### Task 3: 承認モーダルUI（`templates/pending.html`）

**Files:**
- Modify: `templates/pending.html:353-359`（記事一覧`</form>`の直後にタグ付けモーダルを追加）
- Modify: `templates/pending.html:479-518`（`approveArticle`のJSを拡張）

**Interfaces:**
- Consumes: `nav_is_kpop_account`（既存グローバル変数）、`all_groups`（Task 2、`pending()`が渡す`Group`一覧）、`POST /articles/<id>/approve`（Task 2で拡張済み、JSON body `{group_name, member_name}`を受理）

- [ ] **Step 1: タグ付けモーダルを追加**

`templates/pending.html:353-360`（目印）:

```html
  {% endfor %}
</form>

<!-- 全削除用の隠しフォーム -->
<form id="delete-all-form" action="{{ url_for('delete_all_pending') }}" method="post">
  <input type="hidden" name="account_id" value="{{ nav_active_account_id or '' }}">
</form>

{% else %}
```

これを以下に置き換える:

```html
  {% endfor %}
</form>

<!-- 全削除用の隠しフォーム -->
<form id="delete-all-form" action="{{ url_for('delete_all_pending') }}" method="post">
  <input type="hidden" name="account_id" value="{{ nav_active_account_id or '' }}">
</form>

{% if nav_is_kpop_account %}
<!-- グループ・メンバータグ付けモーダル -->
<div id="tag-modal" class="d-none"
     style="position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;"
     onclick="if(event.target===this)closeTagModal()">
  <div style="background:#fff;border-radius:16px;width:min(400px,94vw);padding:20px;position:relative;"
       onclick="event.stopPropagation()">
    <button type="button" onclick="closeTagModal()"
            style="position:absolute;top:12px;right:16px;background:none;border:none;font-size:1.3rem;cursor:pointer;color:#888;line-height:1;padding:0">✕</button>
    <h6 class="fw-bold mb-3" style="color:#000">グループ・メンバーを入力（任意）</h6>
    <div class="mb-2">
      <label class="form-label" style="font-size:.8rem">グループ名</label>
      <input type="text" id="tag-group-input" class="form-control form-control-sm"
             list="tag-group-suggestions" placeholder="例: IVE">
      <datalist id="tag-group-suggestions">
        {% for g in all_groups %}
        <option value="{{ g.name }}">
        {% endfor %}
      </datalist>
    </div>
    <div class="mb-3">
      <label class="form-label" style="font-size:.8rem">メンバー名（空欄可）</label>
      <input type="text" id="tag-member-input" class="form-control form-control-sm" placeholder="例: ウォニョン">
    </div>
    <div class="d-flex gap-2 justify-content-end">
      <button type="button" class="btn btn-sm btn-outline-secondary" onclick="closeTagModal()">キャンセル</button>
      <button type="button" class="btn btn-sm btn-success" onclick="confirmApprove()">承認してキューへ</button>
    </div>
  </div>
</div>
{% endif %}

{% else %}
```

- [ ] **Step 2: `approveArticle`のJSを拡張（モーダル経由 or 即時承認に分岐）**

`templates/pending.html:479-518`（目印）:

```javascript
// ── 承認（Ajax：ページ遷移なし） ───────────────────────────────────────
function approveArticle(id, btn) {
  btn.disabled = true;
  const orig = btn.innerHTML;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  fetch('/articles/' + id + '/approve', {
    method: 'POST',
    headers: { 'X-Requested-With': 'fetch' }
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.ok) {
      var card = document.getElementById('card-' + id);
      if (card) card.remove();
      // ヘッダーのカウンターを減らす
      var badge = document.querySelector('h4 .badge-pending');
      if (badge) {
        var n = parseInt(badge.textContent, 10) - 1;
        badge.textContent = n;
      }
      // サイドバーのカウンターも減らす
      var sbBadge = document.querySelector('.nav-link[href*="pending"] .badge-pending');
      if (sbBadge) {
        var n2 = parseInt(sbBadge.textContent, 10) - 1;
        if (n2 <= 0) sbBadge.remove(); else sbBadge.textContent = n2;
      }
      // 記事が0件になったらリロード（空メッセージを表示するため）
      if (!document.querySelector('[id^="card-"]')) location.reload();
    } else {
      btn.disabled = false;
      btn.innerHTML = orig;
      alert('承認に失敗しました');
    }
  })
  .catch(function() {
    btn.disabled = false;
    btn.innerHTML = orig;
    alert('通信エラーが発生しました');
  });
}
```

これを以下に置き換える:

```javascript
// ── 承認（Ajax：ページ遷移なし） ───────────────────────────────────────
let pendingApproveId = null;
let pendingApproveBtn = null;

function approveArticle(id, btn) {
  var modal = document.getElementById('tag-modal');
  if (modal) {
    pendingApproveId = id;
    pendingApproveBtn = btn;
    document.getElementById('tag-group-input').value = '';
    document.getElementById('tag-member-input').value = '';
    modal.classList.remove('d-none');
    return;
  }
  doApprove(id, btn, '', '');
}

function closeTagModal() {
  document.getElementById('tag-modal').classList.add('d-none');
  pendingApproveId = null;
  pendingApproveBtn = null;
}

function confirmApprove() {
  var groupName = document.getElementById('tag-group-input').value.trim();
  var memberName = document.getElementById('tag-member-input').value.trim();
  var id = pendingApproveId;
  var btn = pendingApproveBtn;
  document.getElementById('tag-modal').classList.add('d-none');
  doApprove(id, btn, groupName, memberName);
}

function doApprove(id, btn, groupName, memberName) {
  btn.disabled = true;
  const orig = btn.innerHTML;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  fetch('/articles/' + id + '/approve', {
    method: 'POST',
    headers: { 'X-Requested-With': 'fetch', 'Content-Type': 'application/json' },
    body: JSON.stringify({ group_name: groupName, member_name: memberName })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.ok) {
      var card = document.getElementById('card-' + id);
      if (card) card.remove();
      // ヘッダーのカウンターを減らす
      var badge = document.querySelector('h4 .badge-pending');
      if (badge) {
        var n = parseInt(badge.textContent, 10) - 1;
        badge.textContent = n;
      }
      // サイドバーのカウンターも減らす
      var sbBadge = document.querySelector('.nav-link[href*="pending"] .badge-pending');
      if (sbBadge) {
        var n2 = parseInt(sbBadge.textContent, 10) - 1;
        if (n2 <= 0) sbBadge.remove(); else sbBadge.textContent = n2;
      }
      // 記事が0件になったらリロード（空メッセージを表示するため）
      if (!document.querySelector('[id^="card-"]')) location.reload();
    } else {
      btn.disabled = false;
      btn.innerHTML = orig;
      alert('承認に失敗しました');
    }
  })
  .catch(function() {
    btn.disabled = false;
    btn.innerHTML = orig;
    alert('通信エラーが発生しました');
  });
}
```

- [ ] **Step 3: テンプレートが正しくレンダリングされることを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app

client = app.test_client()

# KPOPアカウント（account_id=1）: モーダルが存在すること
r = client.get('/pending?account_id=1')
html = r.get_data(as_text=True)
print('account_id=1: status', r.status_code)
print('account_id=1: tag-modal あり', 'id=\"tag-modal\"' in html)
print('account_id=1: confirmApprove関数あり', 'function confirmApprove' in html)

# ガチャアカウント（account_id=2、content_topic設定済み想定）: モーダルが存在しないこと
r2 = client.get('/pending?account_id=2')
html2 = r2.get_data(as_text=True)
print('account_id=2: status', r2.status_code)
print('account_id=2: tag-modal なし', 'id=\"tag-modal\"' not in html2)
"
```
Expected: `account_id=1`側で`status 200`・`tag-modal あり True`・`confirmApprove関数あり True`。`account_id=2`側で`status 200`・`tag-modal なし True`（`ThreadsAccount(id=2).content_topic`が未設定の場合は`nav_is_kpop_account`がTrueになりモーダルが出る点に注意。既存の`content_topic`設定次第で結果が変わるため、DBの実際の設定値を先に確認してから期待値を判断すること）。

- [ ] **Step 4: Commit**

```bash
git add templates/pending.html
git commit -m "$(cat <<'EOF'
feat: 承認待ち画面にグループ・メンバータグ付けモーダルを追加

KPOPアカウントのときのみ「承認」押下時にモーダルを表示し、
グループ名・メンバー名（自由入力、datalistで既存グループをサジェスト）
を入力してから承認できるようにした。ガチャアカウントは従来通り
即Ajax承認のまま。
EOF
)"
```

---

### Task 4: 投稿別7日間追跡・日次スナップショット取得（`analytics_tracker.py`、新規）

**Files:**
- Create: `analytics_tracker.py`

**Interfaces:**
- Consumes: `Article`/`PostStat`/`DailyStat`モデル（Task 1）、`get_active_account`（`database.py`既存）
- Produces: `track_post_stats(app, account_id=1) -> dict`、`snapshot_daily_stats(app, account_id=1) -> dict`。Task 5（scheduler.py）がこれらを呼ぶ。パース用ピュア関数`_parse_media_insights`・`_parse_account_insights`・`_compute_day_index`も公開（ネットワーク不要でテスト可能）

- [ ] **Step 1: `analytics_tracker.py`を新規作成**

```python
import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from database import Article, DailyStat, PostStat, get_active_account, db

logger = logging.getLogger(__name__)

THREADS_API = "https://graph.threads.net/v1.0"
_JST = ZoneInfo("Asia/Tokyo")


def _get_credentials(app, account_id: int = 1) -> tuple:
    """(threads_user_id, access_token) を返す。取得できなければ (None, None)。"""
    account = get_active_account(app, account_id)
    if not account:
        return None, None
    return account["threads_user_id"], account["threads_access_token"]


def _parse_media_insights(data: dict) -> dict:
    """投稿単位Insights APIレスポンスから {metric_name: value} を作る。"""
    result = {}
    for item in data.get("data", []):
        name = item.get("name")
        values = item.get("values", [])
        if name and values:
            result[name] = values[0].get("value", 0)
    return result


def _fetch_media_insights(post_id: str, token: str) -> dict:
    try:
        resp = requests.get(
            f"{THREADS_API}/{post_id}/insights",
            params={"metric": "views,likes,replies,reposts,quotes", "access_token": token},
            timeout=15,
        )
        if not resp.ok:
            logger.warning("Post insights HTTP %d [%s]: %s", resp.status_code, post_id, resp.text[:200])
            return {}
        return _parse_media_insights(resp.json())
    except Exception as exc:
        logger.error("Post insights fetch error [%s]: %s", post_id, exc)
        return {}


def _compute_day_index(posted_at: datetime, now: datetime = None) -> int:
    """投稿からの経過日数を返す。"""
    now = now or datetime.utcnow()
    return (now - posted_at).days


def track_post_stats(app, account_id: int = 1) -> dict:
    """account_id の posted 記事のうち、7日確定値がまだ出ていないものを対象に
    Threads Media Insights を取得し post_stats に1行追加する。
    day_index >= 7 に達した回で is_final=True を立て、以後その記事は対象から外れる。"""
    _, token = _get_credentials(app, account_id)
    if not token:
        return {"error": "Threadsアクセストークン未設定", "updated": 0, "total": 0}

    with app.app_context():
        finalized_ids = {
            row[0] for row in
            db.session.query(PostStat.article_id).filter(PostStat.is_final.is_(True)).distinct()
        }
        candidates = (
            Article.query
            .filter(Article.account_id == account_id)
            .filter(Article.status == "posted")
            .filter(Article.posted_at.isnot(None))
            .filter(Article.threads_post_id.isnot(None))
            .filter(Article.threads_post_id != "")
            .with_entities(Article.id, Article.threads_post_id, Article.posted_at)
            .all()
        )
        targets = [row for row in candidates if row[0] not in finalized_ids]

    updated = errors = skipped = 0
    now = datetime.utcnow()

    for article_id, post_id, posted_at in targets:
        if post_id.startswith("test_"):
            skipped += 1
            continue

        insights = _fetch_media_insights(post_id, token)
        if not insights:
            errors += 1
            continue

        day_index = _compute_day_index(posted_at, now)
        is_final = day_index >= 7

        with app.app_context():
            db.session.add(PostStat(
                article_id=article_id,
                day_index=day_index,
                likes=insights.get("likes", 0),
                views=insights.get("views", 0),
                replies=insights.get("replies", 0),
                reposts=insights.get("reposts", 0),
                quotes=insights.get("quotes", 0),
                is_final=is_final,
            ))
            db.session.commit()
        updated += 1
        time.sleep(0.3)

    result = {"updated": updated, "skipped": skipped, "errors": errors, "total": len(targets)}
    logger.info("投稿別7日間パフォーマンス取得完了: %s", result)
    return result


def _parse_account_insights(data: dict) -> tuple:
    """アカウント単位Insights APIレスポンスから (followers_count, views_count) を作る。
    views_count は期間内の値を合算する。取得できない指標は None。"""
    followers_count = None
    views_count = None
    for item in data.get("data", []):
        name = item.get("name")
        if name == "followers_count":
            total = item.get("total_value") or {}
            if "value" in total:
                followers_count = total["value"]
            elif item.get("values"):
                followers_count = item["values"][-1].get("value")
        elif name == "views":
            values = item.get("values", [])
            if values:
                views_count = sum(v.get("value", 0) for v in values)
            else:
                total = item.get("total_value") or {}
                views_count = total.get("value")
    return followers_count, views_count


def snapshot_daily_stats(app, account_id: int = 1) -> dict:
    """account_id の User Insights から本日（JST）分の followers_count・views を取得し
    daily_stats に反映する。source="manual" の既存行は上書きしない。"""
    user_id, token = _get_credentials(app, account_id)
    if not token:
        return {"ok": False, "error": "Threadsアクセストークン未設定"}

    now_jst = datetime.now(_JST)
    today = now_jst.date()
    since_dt = datetime(today.year, today.month, today.day, 0, 0, tzinfo=_JST)
    until_dt = since_dt + timedelta(days=1)

    try:
        resp = requests.get(
            f"{THREADS_API}/{user_id}/threads_insights",
            params={
                "metric": "followers_count,views",
                "since": int(since_dt.timestamp()),
                "until": int(until_dt.timestamp()),
                "access_token": token,
            },
            timeout=15,
        )
    except Exception as exc:
        logger.error("Account insights fetch error: %s", exc)
        return {"ok": False, "error": str(exc)}

    if not resp.ok:
        logger.warning("Account insights HTTP %d: %s", resp.status_code, resp.text[:200])
        return {"ok": False, "error": resp.text[:200]}

    followers_count, views_count = _parse_account_insights(resp.json())

    with app.app_context():
        row = DailyStat.query.filter_by(account_id=account_id, stat_date=today).first()
        if row and row.source == "manual":
            logger.info("daily_stats %s は手動入力済みのためAPI値で上書きしない", today)
            return {"ok": True, "skipped": "manual_exists"}
        if row:
            row.followers_count = followers_count
            row.views_count = views_count
        else:
            db.session.add(DailyStat(
                account_id=account_id, stat_date=today,
                followers_count=followers_count, views_count=views_count, source="api",
            ))
        db.session.commit()

    result = {"ok": True, "followers_count": followers_count, "views_count": views_count}
    logger.info("日次スナップショット完了: %s", result)
    return result
```

- [ ] **Step 2: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile analytics_tracker.py
```
Expected: エラーなし。

- [ ] **Step 3: パース用ピュア関数を単体で確認する（ネットワーク不要）**

```bash
venv/Scripts/python.exe -c "
from datetime import datetime
from analytics_tracker import _parse_media_insights, _parse_account_insights, _compute_day_index

media_response = {'data': [
    {'name': 'views', 'values': [{'value': 1234}]},
    {'name': 'likes', 'values': [{'value': 56}]},
    {'name': 'replies', 'values': [{'value': 3}]},
    {'name': 'reposts', 'values': [{'value': 2}]},
    {'name': 'quotes', 'values': [{'value': 1}]},
]}
print('media insights:', _parse_media_insights(media_response))

account_response = {'data': [
    {'name': 'followers_count', 'total_value': {'value': 332}},
    {'name': 'views', 'values': [{'value': 100}, {'value': 50}]},
]}
print('account insights:', _parse_account_insights(account_response))

print('day_index 0日目:', _compute_day_index(datetime(2026, 8, 17, 10, 0), now=datetime(2026, 8, 17, 15, 0)))
print('day_index 7日目:', _compute_day_index(datetime(2026, 8, 10, 10, 0), now=datetime(2026, 8, 17, 15, 0)))
"
```
Expected: `media insights: {'views': 1234, 'likes': 56, 'replies': 3, 'reposts': 2, 'quotes': 1}`、`account insights: (332, 150)`、`day_index 0日目: 0`、`day_index 7日目: 7`。

- [ ] **Step 4: `track_post_stats`のDB更新ロジックをモックで確認する（is_final判定・追跡除外）**

```bash
venv/Scripts/python.exe -c "
from datetime import datetime, timedelta
from unittest.mock import patch
from app import app
from database import Article, PostStat, db
import analytics_tracker

with app.app_context():
    # 8日前に投稿済み（7日超過、今回の取得で is_final=True になるはず）
    old = Article(
        feed_source='テスト', title='7日超過テスト', url='https://example.com/track-test-1',
        status='posted', account_id=1, posted_at=datetime.utcnow() - timedelta(days=8),
        threads_post_id='real_post_id_1',
    )
    # 昨日投稿済み（まだ確定していないはず）
    recent = Article(
        feed_source='テスト', title='追跡中テスト', url='https://example.com/track-test-2',
        status='posted', account_id=1, posted_at=datetime.utcnow() - timedelta(days=1),
        threads_post_id='real_post_id_2',
    )
    db.session.add_all([old, recent])
    db.session.commit()
    old_id, recent_id = old.id, recent.id

with patch('analytics_tracker._get_credentials', return_value=('u123', 'token123')), \
     patch('analytics_tracker._fetch_media_insights', return_value={'views': 100, 'likes': 10, 'replies': 1, 'reposts': 0, 'quotes': 0}):
    result1 = analytics_tracker.track_post_stats(app, account_id=1)
    print('1回目実行:', result1)

with app.app_context():
    old_stats = PostStat.query.filter_by(article_id=old_id).all()
    recent_stats = PostStat.query.filter_by(article_id=recent_id).all()
    print('old is_final:', old_stats[0].is_final, 'day_index:', old_stats[0].day_index)
    print('recent is_final:', recent_stats[0].is_final, 'day_index:', recent_stats[0].day_index)

# 2回目実行: old は is_final=True 済みなので対象から外れ、fetch が呼ばれないはず
with patch('analytics_tracker._get_credentials', return_value=('u123', 'token123')), \
     patch('analytics_tracker._fetch_media_insights', return_value={'views': 200, 'likes': 20, 'replies': 2, 'reposts': 0, 'quotes': 0}) as mock_fetch:
    result2 = analytics_tracker.track_post_stats(app, account_id=1)
    print('2回目実行:', result2)
    print('2回目のfetch呼び出し回数（recentのみのはず=1）:', mock_fetch.call_count)

with app.app_context():
    print('old のPostStat行数（1回目のみで増えていないはず）:', PostStat.query.filter_by(article_id=old_id).count())
    print('recent のPostStat行数（2回分になっているはず）:', PostStat.query.filter_by(article_id=recent_id).count())
    PostStat.query.filter(PostStat.article_id.in_([old_id, recent_id])).delete(synchronize_session=False)
    db.session.delete(db.session.get(Article, old_id))
    db.session.delete(db.session.get(Article, recent_id))
    db.session.commit()
"
```
Expected: 1回目実行で`old is_final: True day_index: 8`、`recent is_final: False day_index: 1`。2回目実行で`fetch呼び出し回数: 1`（`old`は対象から除外されAPIを叩かない）、`old のPostStat行数: 1`（増えない）、`recent のPostStat行数: 2`。

- [ ] **Step 5: `snapshot_daily_stats`の手動データ保護ロジックをモックで確認する**

```bash
venv/Scripts/python.exe -c "
from datetime import date
from unittest.mock import patch
from app import app
from database import DailyStat, db
import analytics_tracker

today = date.today()

# ケース1: 行が存在しない → 新規作成される
with app.app_context():
    DailyStat.query.filter_by(account_id=1, stat_date=today).delete()
    db.session.commit()

account_response = {'data': [
    {'name': 'followers_count', 'total_value': {'value': 500}},
    {'name': 'views', 'values': [{'value': 300}]},
]}
class FakeResp:
    ok = True
    def json(self): return account_response

with patch('analytics_tracker._get_credentials', return_value=('u123', 'token123')), \
     patch('analytics_tracker.requests.get', return_value=FakeResp()):
    r1 = analytics_tracker.snapshot_daily_stats(app, account_id=1)
    print('ケース1 結果:', r1)

with app.app_context():
    row = DailyStat.query.filter_by(account_id=1, stat_date=today).first()
    print('ケース1 保存内容:', row.followers_count, row.views_count, row.source)

# ケース2: 既存が source=manual → 上書きされない
with app.app_context():
    row = DailyStat.query.filter_by(account_id=1, stat_date=today).first()
    row.source = 'manual'
    row.followers_count = 999
    db.session.commit()

with patch('analytics_tracker._get_credentials', return_value=('u123', 'token123')), \
     patch('analytics_tracker.requests.get', return_value=FakeResp()):
    r2 = analytics_tracker.snapshot_daily_stats(app, account_id=1)
    print('ケース2 結果:', r2)

with app.app_context():
    row = DailyStat.query.filter_by(account_id=1, stat_date=today).first()
    print('ケース2 手動値が保持されている:', row.followers_count == 999 and row.source == 'manual')
    db.session.delete(row)
    db.session.commit()
"
```
Expected: ケース1で`結果: {'ok': True, 'followers_count': 500, 'views_count': 300}`、`保存内容: 500 300 api`。ケース2で`結果: {'ok': True, 'skipped': 'manual_exists'}`、`手動値が保持されている: True`。

- [ ] **Step 6: Commit**

```bash
git add analytics_tracker.py
git commit -m "$(cat <<'EOF'
feat: 投稿別7日間パフォーマンス追跡と日次アカウントスナップショットを追加

analytics_tracker.pyを新設。track_post_statsはaccount_idの投稿記事を
日次でMedia Insights取得しpost_statsに追記し、day_index>=7でis_final
を立てて以後の追跡対象から除外する。snapshot_daily_statsはUser
Insightsからfollowers_count/viewsを取得しdaily_statsにupsertするが、
手動投入済み(source=manual)の日付は上書きしない。
EOF
)"
```

---

### Task 5: スケジューラジョブ登録（`scheduler.py`）

**Files:**
- Modify: `scheduler.py:482-485`（ジョブ関数定義。`_video_cleanup_job`の後に2つ追加）
- Modify: `scheduler.py:523-535`（`setup_scheduler`にジョブ登録2件を追加）

**Interfaces:**
- Consumes: `analytics_tracker.track_post_stats` / `analytics_tracker.snapshot_daily_stats`（Task 4）
- Produces: `post_stats_daily`（2:30 JST）・`daily_snapshot`（3:30 JST）ジョブID。手動実行用に`_post_stats_job(app)` / `_daily_snapshot_job(app)`関数も公開

- [ ] **Step 1: ジョブ関数を追加**

`scheduler.py:478-485`（目印）:

```python
        logger.info("動画クリーンアップ: %d件対象 %dファイル削除 %dレコード削除", len(targets), deleted_files, len(targets))


def setup_scheduler(app):
    """スケジューラを初期化して起動する。"""
    _setup_weekly_post_jobs(app)
```

これを以下に置き換える:

```python
        logger.info("動画クリーンアップ: %d件対象 %dファイル削除 %dレコード削除", len(targets), deleted_files, len(targets))


def _post_stats_job(app):
    """KPOPアカウント（account_id=1）の投稿別7日間パフォーマンスを日次取得する。"""
    from analytics_tracker import track_post_stats
    result = track_post_stats(app, account_id=1)
    logger.info("投稿別7日間パフォーマンス定期取得: %s", result)


def _daily_snapshot_job(app):
    """KPOPアカウント（account_id=1）のフォロワー数・閲覧数を日次スナップショットする。"""
    from analytics_tracker import snapshot_daily_stats
    result = snapshot_daily_stats(app, account_id=1)
    logger.info("日次フォロワー・閲覧数スナップショット: %s", result)


def setup_scheduler(app):
    """スケジューラを初期化して起動する。"""
    _setup_weekly_post_jobs(app)
```

- [ ] **Step 2: ジョブ登録を追加**

`scheduler.py`内、以下のブロック（目印。`video_cleanup`ジョブ登録の直後）:

```python
    scheduler.add_job(
        _video_cleanup_job,
        CronTrigger(hour=3, minute=0, timezone="Asia/Tokyo"),
        args=[app],
        id="video_cleanup",
        replace_existing=True,
    )

    app.reschedule_post_jobs = lambda: _setup_weekly_post_jobs(app)

    scheduler.start()
    logger.info("Scheduler started (post backup 5min, comments/rollover 30min, engagement 2:00 JST, video cleanup 3:00 JST)")
    return scheduler
```

これを以下に置き換える:

```python
    scheduler.add_job(
        _video_cleanup_job,
        CronTrigger(hour=3, minute=0, timezone="Asia/Tokyo"),
        args=[app],
        id="video_cleanup",
        replace_existing=True,
    )

    scheduler.add_job(
        _post_stats_job,
        CronTrigger(hour=2, minute=30, timezone="Asia/Tokyo"),
        args=[app],
        id="post_stats_daily",
        replace_existing=True,
    )

    scheduler.add_job(
        _daily_snapshot_job,
        CronTrigger(hour=3, minute=30, timezone="Asia/Tokyo"),
        args=[app],
        id="daily_snapshot",
        replace_existing=True,
    )

    app.reschedule_post_jobs = lambda: _setup_weekly_post_jobs(app)

    scheduler.start()
    logger.info(
        "Scheduler started (post backup 5min, comments/rollover 30min, engagement 2:00 JST, "
        "video cleanup 3:00 JST, post stats 2:30 JST, daily snapshot 3:30 JST)"
    )
    return scheduler
```

- [ ] **Step 3: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile scheduler.py
```
Expected: エラーなし。

- [ ] **Step 4: ジョブが正しく登録されることを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app
import scheduler as sched_module

sched = sched_module.setup_scheduler(app)
job_ids = sorted(j.id for j in sched.get_jobs())
print('post_stats_daily 登録済み:', 'post_stats_daily' in job_ids)
print('daily_snapshot 登録済み:', 'daily_snapshot' in job_ids)
sched.shutdown(wait=False)
"
```
Expected: 両方とも`True`。

- [ ] **Step 5: Commit**

```bash
git add scheduler.py
git commit -m "$(cat <<'EOF'
feat: 投稿別7日間追跡・日次スナップショットのスケジューラジョブを追加

post_stats_daily（2:30 JST）とdaily_snapshot（3:30 JST）を新設。
既存のengagement_daily（2:00 JST）・video_cleanup（3:00 JST）とは
時刻が重複しない。
EOF
)"
```

---

### Task 6: 分析ダッシュボード・手動データ投入ルート（`app.py`）

**Files:**
- Modify: `app.py:1413-1421`（`delete_hook`の後、`/auth/threads/manual`の前に新規ルートを追加）

**Interfaces:**
- Consumes: `Group`/`Member`/`PostStat`/`DailyStat`（Task 1）
- Produces: `GET /analytics`（endpoint名`analytics`）、`POST /analytics/daily-stats/add`（endpoint名`add_daily_stat`）。Task 7（`analytics.html`）がこのルートの戻り値を消費する

- [ ] **Step 1: `/analytics`・`/analytics/daily-stats/add`ルートを追加**

`app.py:1413-1421`（目印）:

```python
@app.route("/hooks/<int:id>/delete", methods=["POST"])
def delete_hook(id):
    hook = Hook.query.get_or_404(id)
    db.session.delete(hook)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/auth/threads/manual")
```

これを以下に置き換える:

```python
@app.route("/hooks/<int:id>/delete", methods=["POST"])
def delete_hook(id):
    hook = Hook.query.get_or_404(id)
    db.session.delete(hook)
    db.session.commit()
    return jsonify({"ok": True})


_ANALYTICS_ACCOUNT_ID = 1


@app.route("/analytics")
def analytics():
    daily_rows = (
        DailyStat.query
        .filter_by(account_id=_ANALYTICS_ACCOUNT_ID)
        .order_by(DailyStat.stat_date.asc())
        .all()
    )
    daily_labels = [d.stat_date.strftime("%Y-%m-%d") for d in daily_rows]
    daily_followers = [d.followers_count for d in daily_rows]
    daily_views = [d.views_count for d in daily_rows]

    group_rows = [
        dict(row) for row in db.session.execute(text("""
            SELECT g.name AS name, COUNT(*) AS post_count,
                   AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
            FROM post_stats ps
            JOIN articles a ON a.id = ps.article_id
            JOIN groups g ON g.id = a.group_id
            WHERE ps.is_final = 1 AND a.account_id = :account_id
            GROUP BY g.id
            ORDER BY avg_likes DESC
        """), {"account_id": _ANALYTICS_ACCOUNT_ID}).mappings().all()
    ]

    member_rows = [
        dict(row) for row in db.session.execute(text("""
            SELECT g.name AS group_name, m.name AS member_name, COUNT(*) AS post_count,
                   AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
            FROM post_stats ps
            JOIN articles a ON a.id = ps.article_id
            JOIN members m ON m.id = a.member_id
            JOIN groups g ON g.id = m.group_id
            WHERE ps.is_final = 1 AND a.account_id = :account_id AND a.member_id IS NOT NULL
            GROUP BY m.id
            ORDER BY avg_likes DESC
        """), {"account_id": _ANALYTICS_ACCOUNT_ID}).mappings().all()
    ]

    hour_rows = [
        dict(row) for row in db.session.execute(text("""
            SELECT CAST(strftime('%H', datetime(a.posted_at, '+9 hours')) AS INTEGER) AS hour,
                   COUNT(*) AS post_count,
                   AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
            FROM post_stats ps
            JOIN articles a ON a.id = ps.article_id
            WHERE ps.is_final = 1 AND a.account_id = :account_id AND a.posted_at IS NOT NULL
            GROUP BY hour
            ORDER BY hour ASC
        """), {"account_id": _ANALYTICS_ACCOUNT_ID}).mappings().all()
    ]

    return render_template(
        "analytics.html",
        daily_labels=daily_labels,
        daily_followers=daily_followers,
        daily_views=daily_views,
        daily_rows=daily_rows,
        group_rows=group_rows,
        member_rows=member_rows,
        hour_rows=hour_rows,
    )


@app.route("/analytics/daily-stats/add", methods=["POST"])
def add_daily_stat():
    date_str = (request.form.get("stat_date") or "").strip()
    followers_str = (request.form.get("followers_count") or "").strip()
    views_str = (request.form.get("views_count") or "").strip()

    if not date_str or not followers_str:
        return jsonify({"ok": False, "error": "日付とフォロワー数は必須です"}), 400
    try:
        stat_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        followers_count = int(followers_str)
        views_count = int(views_str) if views_str else None
    except ValueError:
        return jsonify({"ok": False, "error": "入力値が不正です"}), 400

    row = DailyStat.query.filter_by(account_id=_ANALYTICS_ACCOUNT_ID, stat_date=stat_date).first()
    if row:
        row.followers_count = followers_count
        row.views_count = views_count
        row.source = "manual"
    else:
        db.session.add(DailyStat(
            account_id=_ANALYTICS_ACCOUNT_ID, stat_date=stat_date,
            followers_count=followers_count, views_count=views_count, source="manual",
        ))
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/auth/threads/manual")
```

- [ ] **Step 2: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile app.py
```
Expected: エラーなし。

- [ ] **Step 3: 集計SQLとルートの動作をテストデータで確認する**

（`templates/analytics.html`はTask 7で作成するため、このタスクの時点で`GET /analytics`を呼ぶと500エラーになる。ここでは集計クエリ自体が正しい値を返すことをDB直接実行で確認し、`/analytics/daily-stats/add`のみHTTPで確認する）

```bash
venv/Scripts/python.exe -c "
from datetime import datetime, date
from sqlalchemy import text
from app import app
from database import Article, Group, Member, PostStat, DailyStat, db

with app.app_context():
    g = Group(name='IVE', normalized_name='ive')
    db.session.add(g)
    db.session.flush()
    m = Member(group_id=g.id, name='ウォニョン', normalized_name='うぉにょん')
    db.session.add(m)
    db.session.flush()

    # JST 21時（UTC 12時）投稿の確定済み記事
    a1 = Article(feed_source='t', title='t1', url='https://example.com/agg-1', status='posted',
                 account_id=1, group_id=g.id, member_id=m.id,
                 posted_at=datetime(2026, 8, 1, 12, 0))
    db.session.add(a1)
    db.session.flush()
    db.session.add(PostStat(article_id=a1.id, day_index=7, likes=100, views=1000, is_final=True))

    db.session.commit()
    a1_id = a1.id

    group_rows = [dict(r) for r in db.session.execute(text('''
        SELECT g.name AS name, COUNT(*) AS post_count, AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
        FROM post_stats ps JOIN articles a ON a.id = ps.article_id JOIN groups g ON g.id = a.group_id
        WHERE ps.is_final = 1 AND a.account_id = 1 GROUP BY g.id
    ''')).mappings().all()]
    print('group_rows:', group_rows)

    hour_rows = [dict(r) for r in db.session.execute(text('''
        SELECT CAST(strftime('%H', datetime(a.posted_at, '+9 hours')) AS INTEGER) AS hour,
               COUNT(*) AS post_count, AVG(ps.likes) AS avg_likes
        FROM post_stats ps JOIN articles a ON a.id = ps.article_id
        WHERE ps.is_final = 1 AND a.account_id = 1 AND a.posted_at IS NOT NULL GROUP BY hour
    ''')).mappings().all()]
    print('hour_rows（UTC12時→JST21時のはず）:', hour_rows)

    PostStat.query.filter_by(article_id=a1_id).delete()
    db.session.delete(a1)
    db.session.delete(m)
    db.session.delete(g)
    db.session.commit()
"
```
Expected: `group_rows: [{'name': 'IVE', 'post_count': 1, 'avg_likes': 100.0, 'avg_views': 1000.0}]`、`hour_rows`の`hour`が`21`（UTC 12時→JST 21時への変換が正しく行われている）。

```bash
venv/Scripts/python.exe -c "
from app import app
from database import DailyStat, db

client = app.test_client()
r = client.post('/analytics/daily-stats/add', data={'stat_date': '2025-01-01', 'followers_count': '182'})
print('手動追加:', r.status_code, r.get_json())

r2 = client.post('/analytics/daily-stats/add', data={'stat_date': '', 'followers_count': ''})
print('バリデーション（未入力）:', r2.status_code, r2.get_json())

with app.app_context():
    from datetime import date
    row = DailyStat.query.filter_by(account_id=1, stat_date=date(2025, 1, 1)).first()
    print('保存確認:', row.followers_count, row.source)
    db.session.delete(row)
    db.session.commit()
"
```
Expected: `手動追加: 200 {'ok': True}`、`バリデーション（未入力）: 400 {'ok': False, 'error': '日付とフォロワー数は必須です'}`、`保存確認: 182 manual`。

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "$(cat <<'EOF'
feat: 分析ダッシュボードのルートと手動データ投入APIを追加

/analyticsはaccount_id=1固定で日次推移・グループ別/メンバー別
平均パフォーマンス・時間帯別平均パフォーマンスを集計する。
/analytics/daily-stats/addはdaily_statsへの手動投入（過去分の
フォロワー数バックフィル用）で、既存行があればsource=manualとして
upsertする。
EOF
)"
```

---

### Task 7: 分析ダッシュボードテンプレート（`templates/analytics.html`、新規）

**Files:**
- Create: `templates/analytics.html`

**Interfaces:**
- Consumes: `analytics`ルート（Task 6）が渡す`daily_labels`/`daily_followers`/`daily_views`/`daily_rows`/`group_rows`/`member_rows`/`hour_rows`、`add_daily_stat`エンドポイント（Task 6）

- [ ] **Step 1: `templates/analytics.html`を新規作成**

```html
{% extends 'base.html' %}
{% block content %}

<div class="d-flex justify-content-between align-items-center mb-3">
  <h4 class="mb-0 fw-bold">分析</h4>
</div>

<!-- フォロワー数・閲覧数の推移 -->
<div class="card p-3 mb-4">
  <h6 class="fw-bold mb-3">フォロワー数・閲覧数の推移</h6>
  {% if daily_rows %}
  <canvas id="daily-chart" height="90"></canvas>
  {% else %}
  <p class="text-muted mb-0">データがありません。下のフォームから過去分を投入するか、日次スナップショットの実行を待ってください。</p>
  {% endif %}
</div>

<!-- 手動データ投入フォーム -->
<div class="card p-3 mb-4">
  <h6 class="fw-bold mb-3">過去分データの手動投入</h6>
  <form id="daily-stat-form" class="row g-2 align-items-end" onsubmit="return submitDailyStat(event)">
    <div class="col-auto">
      <label class="form-label" style="font-size:.78rem">日付（大まかでよい）</label>
      <input type="date" id="stat-date" class="form-control form-control-sm" required>
    </div>
    <div class="col-auto">
      <label class="form-label" style="font-size:.78rem">フォロワー数</label>
      <input type="number" id="stat-followers" class="form-control form-control-sm" min="0" required style="width:120px">
    </div>
    <div class="col-auto">
      <label class="form-label" style="font-size:.78rem">閲覧数（任意）</label>
      <input type="number" id="stat-views" class="form-control form-control-sm" min="0" style="width:120px">
    </div>
    <div class="col-auto">
      <button type="submit" class="btn btn-sm btn-accent">投入</button>
    </div>
    <div class="col-auto" id="daily-stat-result" style="font-size:.8rem"></div>
  </form>
</div>

<!-- グループ別・メンバー別 -->
<div class="row g-3 mb-4">
  <div class="col-md-6">
    <div class="card p-3 h-100">
      <h6 class="fw-bold mb-3">グループ別平均パフォーマンス（7日確定値）</h6>
      {% if group_rows %}
      <div class="table-responsive">
        <table class="table table-hover mb-0" style="font-size:.85rem">
          <thead>
            <tr><th>グループ</th><th class="text-end">投稿数</th><th class="text-end">平均いいね</th><th class="text-end">平均閲覧数</th></tr>
          </thead>
          <tbody>
            {% for row in group_rows %}
            <tr>
              <td>{{ row.name }}</td>
              <td class="text-end">{{ row.post_count }}</td>
              <td class="text-end">{{ "%.1f"|format(row.avg_likes or 0) }}</td>
              <td class="text-end">{{ "%.1f"|format(row.avg_views or 0) }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <p class="text-muted mb-0">7日確定値のあるグループ付き投稿がまだありません。</p>
      {% endif %}
    </div>
  </div>
  <div class="col-md-6">
    <div class="card p-3 h-100">
      <h6 class="fw-bold mb-3">メンバー別平均パフォーマンス（7日確定値）</h6>
      {% if member_rows %}
      <div class="table-responsive">
        <table class="table table-hover mb-0" style="font-size:.85rem">
          <thead>
            <tr><th>グループ</th><th>メンバー</th><th class="text-end">投稿数</th><th class="text-end">平均いいね</th><th class="text-end">平均閲覧数</th></tr>
          </thead>
          <tbody>
            {% for row in member_rows %}
            <tr>
              <td>{{ row.group_name }}</td>
              <td>{{ row.member_name }}</td>
              <td class="text-end">{{ row.post_count }}</td>
              <td class="text-end">{{ "%.1f"|format(row.avg_likes or 0) }}</td>
              <td class="text-end">{{ "%.1f"|format(row.avg_views or 0) }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <p class="text-muted mb-0">7日確定値のあるメンバー付き投稿がまだありません。</p>
      {% endif %}
    </div>
  </div>
</div>

<!-- 時間帯別 -->
<div class="card p-3 mb-4">
  <h6 class="fw-bold mb-3">投稿時刻の時間帯別平均パフォーマンス（JST、7日確定値）</h6>
  {% if hour_rows %}
  <div class="table-responsive">
    <table class="table table-hover mb-0" style="font-size:.85rem">
      <thead>
        <tr><th>時間帯</th><th class="text-end">投稿数</th><th class="text-end">平均いいね</th><th class="text-end">平均閲覧数</th></tr>
      </thead>
      <tbody>
        {% for row in hour_rows %}
        <tr>
          <td>{{ "%02d"|format(row.hour) }}時台</td>
          <td class="text-end">{{ row.post_count }}</td>
          <td class="text-end">{{ "%.1f"|format(row.avg_likes or 0) }}</td>
          <td class="text-end">{{ "%.1f"|format(row.avg_views or 0) }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <p class="text-muted mb-0">7日確定値のある投稿がまだありません。</p>
  {% endif %}
</div>

{% endblock %}

{% block scripts %}
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script>
{% if daily_rows %}
new Chart(document.getElementById('daily-chart'), {
  type: 'line',
  data: {
    labels: {{ daily_labels | tojson }},
    datasets: [
      {
        label: 'フォロワー数',
        data: {{ daily_followers | tojson }},
        borderColor: '#c9699f',
        backgroundColor: 'rgba(201,105,159,.15)',
        yAxisID: 'y',
        tension: .25,
      },
      {
        label: '閲覧数',
        data: {{ daily_views | tojson }},
        borderColor: '#7b93cf',
        backgroundColor: 'rgba(123,147,207,.15)',
        yAxisID: 'y1',
        tension: .25,
      },
    ],
  },
  options: {
    responsive: true,
    interaction: { mode: 'index', intersect: false },
    scales: {
      y:  { type: 'linear', position: 'left', title: { display: true, text: 'フォロワー数' } },
      y1: { type: 'linear', position: 'right', title: { display: true, text: '閲覧数' }, grid: { drawOnChartArea: false } },
    },
  },
});
{% endif %}

function submitDailyStat(event) {
  event.preventDefault();
  const stat_date = document.getElementById('stat-date').value;
  const followers_count = document.getElementById('stat-followers').value;
  const views_count = document.getElementById('stat-views').value;
  const resultEl = document.getElementById('daily-stat-result');
  fetch('/analytics/daily-stats/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ stat_date, followers_count, views_count }).toString(),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      location.reload();
    } else {
      resultEl.textContent = d.error || '投入に失敗しました';
      resultEl.style.color = 'var(--accent)';
    }
  })
  .catch(() => { resultEl.textContent = '通信エラーが発生しました'; });
  return false;
}
</script>
{% endblock %}
```

- [ ] **Step 2: `/analytics`が正しくレンダリングされることを確認する（データなし・ありの両方）**

```bash
venv/Scripts/python.exe -c "
from datetime import datetime, date
from app import app
from database import Article, Group, Member, PostStat, DailyStat, db

client = app.test_client()

# データなしの状態
r = client.get('/analytics')
print('空状態 status:', r.status_code)
html = r.get_data(as_text=True)
print('空状態メッセージあり:', 'データがありません' in html)

# テストデータ投入
with app.app_context():
    g = Group(name='IVE', normalized_name='ive')
    db.session.add(g)
    db.session.flush()
    a1 = Article(feed_source='t', title='t1', url='https://example.com/tmpl-agg-1', status='posted',
                 account_id=1, group_id=g.id, posted_at=datetime(2026, 8, 1, 12, 0))
    db.session.add(a1)
    db.session.flush()
    db.session.add(PostStat(article_id=a1.id, day_index=7, likes=100, views=1000, is_final=True))
    db.session.add(DailyStat(account_id=1, stat_date=date(2026, 8, 1), followers_count=300, views_count=500, source='manual'))
    db.session.commit()
    a1_id, g_id = a1.id, g.id

r2 = client.get('/analytics')
html2 = r2.get_data(as_text=True)
print('データあり status:', r2.status_code)
print('グループ名表示:', 'IVE' in html2)
print('Chart.js canvas あり:', 'daily-chart' in html2)

with app.app_context():
    PostStat.query.filter_by(article_id=a1_id).delete()
    db.session.delete(db.session.get(Article, a1_id))
    db.session.delete(db.session.get(Group, g_id))
    DailyStat.query.filter_by(account_id=1, stat_date=date(2026, 8, 1)).delete()
    db.session.commit()
"
```
Expected: 空状態`status 200`・`空状態メッセージあり: True`。データあり`status 200`・`グループ名表示: True`・`Chart.js canvas あり: True`。

- [ ] **Step 3: Commit**

```bash
git add templates/analytics.html
git commit -m "$(cat <<'EOF'
feat: 分析ダッシュボードテンプレートを追加

Chart.js（CDN）でフォロワー数・閲覧数の推移を折れ線グラフ表示し、
グループ別・メンバー別・時間帯別の平均パフォーマンステーブルと
過去分データの手動投入フォームを追加した。
EOF
)"
```

---

### Task 8: サイドバーナビゲーション（`templates/base.html`）

**Files:**
- Modify: `templates/base.html`（デスクトップサイドバー、`nav_is_kpop_account`ブロック内にリンクを追加）
- Modify: `templates/base.html`（モバイルナビバー、同条件でリンクを追加）

**Interfaces:**
- Consumes: `nav_is_kpop_account`（既存グローバル変数）、`analytics`エンドポイント（Task 6）

- [ ] **Step 1: デスクトップサイドバーにリンクを追加**

`templates/base.html:519-526`（目印。`nav_is_kpop_account`のときのみ表示される「フック管理」リンクの直後）:

```html
        {% if nav_active_account_id %}
        <li class="nav-item">
          <a class="nav-link {% if request.endpoint=='hooks_page' %}active{% endif %}" href="{{ url_for('hooks_page', account_id=nav_active_account_id) }}">
            <i class="bi bi-magic"></i> フック管理
          </a>
        </li>
        {% endif %}
        <li class="nav-item">
          <a class="nav-link {% if request.endpoint=='settings' %}active{% endif %}" href="{{ url_for('settings') }}">
```

これを以下に置き換える:

```html
        {% if nav_active_account_id %}
        <li class="nav-item">
          <a class="nav-link {% if request.endpoint=='hooks_page' %}active{% endif %}" href="{{ url_for('hooks_page', account_id=nav_active_account_id) }}">
            <i class="bi bi-magic"></i> フック管理
          </a>
        </li>
        {% endif %}
        {% if nav_is_kpop_account %}
        <li class="nav-item">
          <a class="nav-link {% if request.endpoint=='analytics' %}active{% endif %}" href="{{ url_for('analytics') }}">
            <i class="bi bi-bar-chart-fill"></i> 分析
          </a>
        </li>
        {% endif %}
        <li class="nav-item">
          <a class="nav-link {% if request.endpoint=='settings' %}active{% endif %}" href="{{ url_for('settings') }}">
```

- [ ] **Step 2: モバイルナビバーにリンクを追加**

`templates/base.html:433-438`（目印）:

```html
  {% if nav_active_account_id %}
  <a href="{{ url_for('hooks_page', account_id=nav_active_account_id) }}" class="{% if request.endpoint=='hooks_page' %}active{% endif %}">
    <i class="bi bi-magic"></i> フック
  </a>
  {% endif %}
  <a href="{{ url_for('settings') }}" class="{% if request.endpoint=='settings' %}active{% endif %}">
```

これを以下に置き換える:

```html
  {% if nav_active_account_id %}
  <a href="{{ url_for('hooks_page', account_id=nav_active_account_id) }}" class="{% if request.endpoint=='hooks_page' %}active{% endif %}">
    <i class="bi bi-magic"></i> フック
  </a>
  {% endif %}
  {% if nav_is_kpop_account %}
  <a href="{{ url_for('analytics') }}" class="{% if request.endpoint=='analytics' %}active{% endif %}">
    <i class="bi bi-bar-chart-fill"></i> 分析
  </a>
  {% endif %}
  <a href="{{ url_for('settings') }}" class="{% if request.endpoint=='settings' %}active{% endif %}">
```

- [ ] **Step 3: リンクの表示条件と、他ページが壊れていないことを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app

client = app.test_client()

r = client.get('/?account_id=1')
html = r.get_data(as_text=True)
print('account_id=1で /analytics リンクあり:', '/analytics' in html)

for path in ('/', '/pending', '/queue', '/settings', '/analytics'):
    r2 = client.get(path)
    print(path, '->', r2.status_code)
"
```
Expected: `account_id=1で /analytics リンクあり: True`。全パスとも`200`。

- [ ] **Step 4: Commit**

```bash
git add templates/base.html
git commit -m "$(cat <<'EOF'
feat: サイドバーに分析ダッシュボードへのリンクを追加

KPOPアカウント選択時のみデスクトップ・モバイル両方のナビに
「分析」リンクを表示する。
EOF
)"
```

---

## 完了確認

全タスク完了後、以下を満たしていることを確認する:

- [ ] `groups`/`members`/`post_stats`/`daily_stats`テーブルが作成され、`articles.group_id`/`member_id`が追加されている
- [ ] KPOPアカウントの承認待ち記事で「承認」を押すとモーダルが出て、グループ名・メンバー名（自由入力、既存グループ名はdatalistでサジェスト）を入力・省略どちらでも承認できる
- [ ] 表記ゆれ（前後空白・全角半角・大文字小文字）のあるグループ名・メンバー名が同一マスタレコードに解決される
- [ ] メンバー名を空欄で承認した記事は`member_id`がNoneのままになる（グループ単位集計のみ対象）
- [ ] ガチャアカウント（account_id=2）の承認フローはモーダルなしの従来動作のまま
- [ ] `track_post_stats`が投稿から7日経過した記事に`is_final=True`を立て、以後その記事に対してAPIを叩かない
- [ ] `snapshot_daily_stats`が日次でフォロワー数・閲覧数を取得し、手動投入済み（`source=manual`）の日付を上書きしない
- [ ] `/analytics`にアクセスすると、フォロワー数・閲覧数の推移グラフ、グループ別・メンバー別・時間帯別（JST、動的グルーピング）の平均パフォーマンステーブルが表示される
- [ ] `/analytics`内のフォームから過去分のフォロワー数を手動投入できる
- [ ] サイドバー（デスクトップ・モバイル）の「分析」リンクがKPOPアカウント選択時のみ表示される
- [ ] `post_stats_daily`（2:30 JST）・`daily_snapshot`（3:30 JST）ジョブがスケジューラに登録されている
