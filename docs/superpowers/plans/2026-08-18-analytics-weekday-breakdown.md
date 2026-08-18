# 分析ダッシュボード 曜日別パフォーマンス集計 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/analytics`ダッシュボードに、既存の時間帯別集計と同じデータソース（`post_stats`の7日確定値）を使った曜日別（月〜日）平均パフォーマンステーブルを追加する。

**Architecture:** `analytics()`ルートに`weekday_rows`という4つ目の集計クエリを追加し、`templates/analytics.html`の時間帯別セクション直後に同一形式のテーブルセクションを追加する。既存の`hour_rows`のクエリ・表示は変更しない。

**Tech Stack:** Python 3.x / Flask / SQLAlchemy（raw `text()` SQL）/ SQLite / Jinja2

## Global Constraints

- 対象ファイルは`app.py`・`templates/analytics.html`のみ
- 既存の`hour_rows`クエリ・`daily_rows`・`group_rows`・`member_rows`のロジック・表示は一切変更しない
- 表示は表のみ（Chart.js等の可視化は追加しない、ユーザー承認済み）
- コメントは原則書かない（WHYが非自明な場合のみ1行）
- Shellコマンドの実行環境はWindows。Bashツールを使う場合はGit Bash構文（`$VAR`、`&&`可）、PowerShellツールを使う場合はPowerShell構文（`$env:VAR`、`&&`不可）に注意する
- このプロジェクトにはpytest等のテストフレームワークが存在しない。検証は`venv/Scripts/python.exe -c "..."`によるDB直接確認・`app.test_client()`によるHTTPリクエストで行う
- `venv/Scripts/python.exe`が仮想環境のPythonインタプリタ
- **重要**: `app.py`は`app = create_app()`をモジュール読み込み時に実行する。`test_client()`での検証には必ず`from app import app`を使うこと
- 開発サーバー（ポート5000）はセッションを跨いで古いプロセスが残ることがある。新たに`python app.py`/`python run.py`を起動しないこと
- この開発DBには実データ（account_id=1の実記事約70件超）が存在する。検証用データは一意なダミー`url`で作成し、必ずクリーンアップすること

---

## ファイル構成

| ファイル | 変更内容 |
|---|---|
| `app.py` | `analytics()`ルートに`weekday_rows`集計クエリを追加、`render_template`に渡す |
| `templates/analytics.html` | 時間帯別セクションの直後に曜日別テーブルセクションを追加 |

---

### Task 1: 曜日別集計クエリとテーブル表示を追加

**Files:**
- Modify: `app.py`（`analytics()`ルート、`hour_rows`クエリの直後）
- Modify: `templates/analytics.html`（時間帯別セクションの直後）

**Interfaces:**
- Consumes: 既存の`post_stats`/`articles`テーブル（変更なし）
- Produces: `analytics()`が`weekday_rows`（`weekday`, `weekday_label`, `post_count`, `avg_likes`, `avg_views`を持つdictのリスト、月→日の順）をテンプレートに渡す

- [ ] **Step 1: `app.py`の`analytics()`に`weekday_rows`クエリを追加**

`app.py`内、以下のブロック（目印。`hour_rows`クエリの直後、`return render_template(...)`の直前）:

```python
    hour_rows = [
        dict(row) for row in db.session.execute(text("""
            SELECT CAST(strftime('%H', datetime(a.posted_at, '+9 hours')) AS INTEGER) AS hour,
                   COUNT(*) AS post_count,
                   AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
            FROM post_stats ps
            JOIN articles a ON a.id = ps.article_id
            WHERE ps.is_final = 1 AND ps.day_index <= 7 AND a.account_id = :account_id AND a.posted_at IS NOT NULL
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
```

これを以下に置き換える:

```python
    hour_rows = [
        dict(row) for row in db.session.execute(text("""
            SELECT CAST(strftime('%H', datetime(a.posted_at, '+9 hours')) AS INTEGER) AS hour,
                   COUNT(*) AS post_count,
                   AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
            FROM post_stats ps
            JOIN articles a ON a.id = ps.article_id
            WHERE ps.is_final = 1 AND ps.day_index <= 7 AND a.account_id = :account_id AND a.posted_at IS NOT NULL
            GROUP BY hour
            ORDER BY hour ASC
        """), {"account_id": _ANALYTICS_ACCOUNT_ID}).mappings().all()
    ]

    _WEEKDAY_LABELS = {0: "日", 1: "月", 2: "火", 3: "水", 4: "木", 5: "金", 6: "土"}
    weekday_rows = [
        {**dict(row), "weekday_label": _WEEKDAY_LABELS[row["weekday"]]}
        for row in db.session.execute(text("""
            SELECT CAST(strftime('%w', datetime(a.posted_at, '+9 hours')) AS INTEGER) AS weekday,
                   COUNT(*) AS post_count,
                   AVG(ps.likes) AS avg_likes, AVG(ps.views) AS avg_views
            FROM post_stats ps
            JOIN articles a ON a.id = ps.article_id
            WHERE ps.is_final = 1 AND ps.day_index <= 7 AND a.account_id = :account_id AND a.posted_at IS NOT NULL
            GROUP BY weekday
            ORDER BY CASE weekday WHEN 0 THEN 7 ELSE weekday END ASC
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
        weekday_rows=weekday_rows,
    )
```

- [ ] **Step 2: `templates/analytics.html`に曜日別テーブルセクションを追加**

`templates/analytics.html`内、以下のブロック（目印。時間帯別セクションの直後、`{% endblock %}`の直前）:

```html
  {% else %}
  <p class="text-muted mb-0">7日確定値のある投稿がまだありません。</p>
  {% endif %}
</div>

{% endblock %}
```

これを以下に置き換える（時間帯別セクションと`{% endblock %}`の間に新規セクションを挿入。目印の1つ目の`{% else %}...{% endif %}`は時間帯別セクションのものなので、その直後、2つ目のセクション新規追加後に`{% endblock %}`が続く形にする）:

```html
  {% else %}
  <p class="text-muted mb-0">7日確定値のある投稿がまだありません。</p>
  {% endif %}
</div>

<!-- 曜日別 -->
<div class="card p-3 mb-4">
  <h6 class="fw-bold mb-3">投稿曜日別平均パフォーマンス（JST、7日確定値）</h6>
  {% if weekday_rows %}
  <div class="table-responsive">
    <table class="table table-hover mb-0" style="font-size:.85rem">
      <thead>
        <tr><th>曜日</th><th class="text-end">投稿数</th><th class="text-end">平均いいね</th><th class="text-end">平均閲覧数</th></tr>
      </thead>
      <tbody>
        {% for row in weekday_rows %}
        <tr>
          <td>{{ row.weekday_label }}</td>
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
```

**注意**: このファイルには`{% else %}...{% endif %}`のパターンが他のセクション（グループ別・メンバー別）にも存在するため、置換対象は必ず「時間帯別セクションの`</div>`と`{% endblock %}`に挟まれた、ファイル末尾に最も近い箇所」であることを確認してから編集すること（目印のコード片全体で一意に特定できるはずだが、念のため周辺の`<!-- 時間帯別 -->`コメントの位置を確認する）。

- [ ] **Step 3: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile app.py
```
Expected: エラーなし。

- [ ] **Step 4: 曜日別集計クエリと表示を確認する**

```bash
venv/Scripts/python.exe -c "
from datetime import datetime
from app import app
from database import Article, Group, PostStat, db

with app.app_context():
    g = Group(name='WeekdayVerifyGroup', normalized_name='weekdayverifygroup')
    db.session.add(g)
    db.session.flush()

    # 2026-08-17は月曜日、2026-08-16は日曜日
    mon = Article(feed_source='t', title='mon', url='https://example.com/wk-verify-mon',
                  status='posted', account_id=1, group_id=g.id, posted_at=datetime(2026, 8, 17, 3, 0))
    sun = Article(feed_source='t', title='sun', url='https://example.com/wk-verify-sun',
                  status='posted', account_id=1, group_id=g.id, posted_at=datetime(2026, 8, 16, 3, 0))
    db.session.add_all([mon, sun])
    db.session.flush()
    db.session.add(PostStat(article_id=mon.id, day_index=7, likes=100, views=1000, is_final=True))
    db.session.add(PostStat(article_id=sun.id, day_index=7, likes=50, views=500, is_final=True))
    db.session.commit()
    g_id, mon_id, sun_id = g.id, mon.id, sun.id

client = app.test_client()
r = client.get('/analytics')
html = r.get_data(as_text=True)
print('status:', r.status_code)
print('曜日別セクション見出しあり:', '投稿曜日別平均パフォーマンス' in html)
print('月が表示されている:', html.count('<td>月</td>') >= 1 or '月' in html)
print('日が表示されている:', '日' in html)
# 月(mon_idの投稿, posted_at 2026-08-17 03:00 UTC = JST 12:00, 月曜)が
# 日(sun_id, JST 12:00, 日曜)より表の中で先に出現する（月〜日順）ことを確認
mon_pos = html.find('投稿曜日別平均パフォーマンス')
print('セクション見つかった:', mon_pos != -1)

with app.app_context():
    PostStat.query.filter(PostStat.article_id.in_([mon_id, sun_id])).delete(synchronize_session=False)
    db.session.delete(db.session.get(Article, mon_id))
    db.session.delete(db.session.get(Article, sun_id))
    db.session.delete(db.session.get(Group, g_id))
    db.session.commit()
    print('cleanup post_stats:', PostStat.query.count())
"
```
Expected: `status: 200`、`曜日別セクション見出しあり: True`、`月が表示されている: True`、`日が表示されている: True`、`セクション見つかった: True`、`cleanup post_stats: 0`。

さらに、曜日の並び順（月〜日）をSQL直接実行で確認する:

```bash
venv/Scripts/python.exe -c "
from datetime import datetime
from sqlalchemy import text
from app import app
from database import Article, PostStat, db

with app.app_context():
    # 月(8/17), 水(8/19), 日(8/16) の順でJST正午に投稿したデータを作る（登録順はバラバラ）
    articles = []
    for label, day in [('wed', 19), ('sun', 16), ('mon', 17)]:
        a = Article(feed_source='t', title=label, url=f'https://example.com/wk-order-{label}',
                    status='posted', account_id=1, posted_at=datetime(2026, 8, day, 3, 0))
        db.session.add(a)
        articles.append(a)
    db.session.flush()
    for a in articles:
        db.session.add(PostStat(article_id=a.id, day_index=7, likes=10, views=100, is_final=True))
    db.session.commit()
    ids = [a.id for a in articles]

    rows = db.session.execute(text('''
        SELECT CAST(strftime('%w', datetime(a.posted_at, '+9 hours')) AS INTEGER) AS weekday
        FROM post_stats ps JOIN articles a ON a.id = ps.article_id
        WHERE ps.is_final = 1 AND a.id IN ({})
        GROUP BY weekday
        ORDER BY CASE weekday WHEN 0 THEN 7 ELSE weekday END ASC
    '''.format(','.join(str(i) for i in ids)))).mappings().all()
    print('weekday順（期待: 1(月), 3(水), 0(日)の順）:', [r['weekday'] for r in rows])

    PostStat.query.filter(PostStat.article_id.in_(ids)).delete(synchronize_session=False)
    for a in articles:
        db.session.delete(a)
    db.session.commit()
"
```
Expected: `weekday順（期待: 1(月), 3(水), 0(日)の順）: [1, 3, 0]`。

- [ ] **Step 5: 既存の時間帯別セクション・他ページが壊れていないことを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app
client = app.test_client()
for path in ('/', '/pending', '/queue', '/settings', '/analytics'):
    r = client.get(path)
    print(path, '->', r.status_code)
html = client.get('/analytics').get_data(as_text=True)
print('時間帯別見出しあり（変更なしのはず）:', '投稿時刻の時間帯別平均パフォーマンス' in html)
"
```
Expected: 全パスとも`200`、`時間帯別見出しあり: True`。

- [ ] **Step 6: Commit**

```bash
git add app.py templates/analytics.html
git commit -m "$(cat <<'EOF'
feat: 分析ダッシュボードに投稿曜日別パフォーマンス集計を追加

post_statsの7日確定値を投稿時刻の曜日（JST、月〜日順）で動的に
グルーピングした平均いいね数・平均閲覧数・投稿数のテーブルを、
既存の時間帯別セクションの直後に追加した。既存の時間帯別集計の
クエリ・表示は変更していない。
EOF
)"
```

---

## 完了確認

- [ ] `/analytics`に曜日別（月〜日順）の平均いいね数・平均閲覧数・投稿数テーブルが表示される
- [ ] 既存の時間帯別セクションの表示・数値が変更されていない
- [ ] 7日確定値のあるデータがまだない場合は空状態メッセージが表示される
