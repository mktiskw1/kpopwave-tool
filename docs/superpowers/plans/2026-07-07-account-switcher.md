# アカウント切り替えUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** サイドバーにセッションベースのアカウント切り替えドロップダウンを追加し、ダッシュボード・承認待ち・投稿キュー・スケジュールの全ページで選択中アカウントが一貫して反映されるようにする。

**Architecture:** Flaskセッション（`session["active_account_id"]`）を単一の真実源とし、既存の `_selected_account_id()` ヘルパーを拡張してクエリパラメータ→セッション→レガシーデフォルトの順で解決する。`inject_globals()` context processorでサイドバーに必要な情報を全ページへ自動注入し、新規ルート `/accounts/switch/<id>` でセッションを更新する。

**Tech Stack:** Flask, Flask-SQLAlchemy, Jinja2, Bootstrap 5.3.2（JS bundle読み込み済み）

## Global Constraints

- このプロジェクトにはpytest等の自動テストフレームワークが存在しない（`find`で確認済み、`tests/`や`conftest.py`は無し）。そのため各タスクの検証は、コミットしない一時スクリプト（Flask test client / アプリコンテキストを直接使う）またはブラウザでの手動確認で行う。これはこのリポジトリの既存の検証スタイル（`_check_*.py` 等のワンオフスクリプト）と一致する
- コメントは書かない（WHYが非自明な場合のみ1行）— `SKILL.md` の既存ルール
- Windows環境。シェルコマンドは `venv/Scripts/python.exe` を使う
- 日時はUTC保存・JST表示のルールに影響しない変更のみ

---

## ファイル構成

- 変更: `app.py` — `_selected_account_id()`拡張、`inject_globals()`拡張、新規ルート`switch_account`追加、`index()`/`pending()`/`queue()`/`schedule()`のアカウント絞り込み・テンプレート変数整理
- 変更: `templates/base.html` — サイドバーにアカウント切り替えドロップダウン追加
- 変更: `templates/pending.html` — 既存のページ内アカウント選択タブを削除
- 変更: `templates/queue.html` — 既存のページ内アカウント選択タブを削除
- 変更: `templates/schedule.html` — 既存のページ内アカウント選択タブを削除

新規ファイルは作成しない。

---

### Task 1: セッションベースのアカウント解決 + 切り替えルート

**Files:**
- Modify: `app.py:268-277`（`_selected_account_id()`）
- Modify: `app.py:1069-1080` 付近（`toggle_account_active` の後に新規ルートを追加）

**Interfaces:**
- Consumes: `database.ThreadsAccount`（既にimport済み）, `database.get_active_account(app)`（既にimport済み、`{"id": int, ...}` または `None` を返す）, Flask `session`（既にimport済み）
- Produces: `_selected_account_id() -> int | None`（変更なしのシグネチャ。呼び出し側の`pending()`/`queue()`/`schedule()`は無変更で自動的にセッション対応する）。新規エンドポイント名 `switch_account`（`url_for("switch_account", id=<int>)` で参照可能）

- [ ] **Step 1: `_selected_account_id()` をセッション対応に書き換える**

`app.py:268-277` の現在のコード:
```python
def _selected_account_id():
    """クエリパラメータ account_id を解決する。省略時はレガシー（最古の）アクティブアカウント。"""
    raw = request.args.get("account_id")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    legacy = get_active_account(app)
    return legacy["id"] if legacy else None
```

これを以下に置き換える:
```python
def _selected_account_id():
    """account_id解決順序: クエリパラメータ → セッション → レガシー（最古の）アクティブアカウント。
    解決したIDは常にセッションへ書き戻し、以降のリクエストに引き継ぐ。"""
    raw = request.args.get("account_id")
    if raw:
        try:
            resolved = int(raw)
        except ValueError:
            resolved = None
        if resolved is not None and ThreadsAccount.query.get(resolved):
            session["active_account_id"] = resolved
            return resolved

    session_id = session.get("active_account_id")
    if session_id is not None:
        if ThreadsAccount.query.get(session_id):
            return session_id
        session.pop("active_account_id", None)

    legacy = get_active_account(app)
    legacy_id = legacy["id"] if legacy else None
    if legacy_id is not None:
        session["active_account_id"] = legacy_id
    return legacy_id
```

- [ ] **Step 2: `switch_account` ルートを追加する**

`app.py:1069-1080` 付近（`toggle_account_active` ルートの直後）に追加:
```python
@app.route("/accounts/switch/<int:id>")
def switch_account(id):
    """サイドバーのアカウント切り替えドロップダウンから呼ばれる。セッションに保存して元のページへ戻る。"""
    account = ThreadsAccount.query.get_or_404(id)
    session["active_account_id"] = account.id
    return redirect(request.referrer or url_for("index"))
```

- [ ] **Step 3: 検証スクリプトを書く（一時ファイル、コミットしない）**

この時点では `index()` など既存ルートはまだ `_selected_account_id()` を呼ぶように変更されていない
（それはTask 2/3で行う）ため、HTTP経由ではなく `test_request_context` で直接関数を検証する。
`switch_account` はTask 1で追加した実ルートなので、そちらは test client で検証する。

`C:\Users\mktis\AppData\Local\Temp\claude\verify_account_switch.py` に保存:
```python
from flask import session
import app as app_module
from database import ThreadsAccount

flask_app = app_module.app
flask_app.config["TESTING"] = True
client = flask_app.test_client()

with flask_app.app_context():
    accounts = ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all()
    assert len(accounts) >= 1, "有効なThreadsAccountが最低1件必要です"
    account_ids = [a.id for a in accounts]

# _selected_account_id() 単体の挙動（レガシーデフォルト解決 → セッションへの書き戻し）
with flask_app.test_request_context("/"):
    resolved = app_module._selected_account_id()
    assert resolved in account_ids, f"解決結果が想定外: {resolved}"
    assert session.get("active_account_id") == resolved, "セッションに書き戻されていません"
    print("OK: _selected_account_id() が account_id =", resolved, "を解決しセッションに書き戻した")

# switch_account ルート（実HTTPエンドポイント）
if len(account_ids) >= 2:
    first_resolved = account_ids[0]
    other_id = [i for i in account_ids if i != first_resolved][0]
    r2 = client.get(f"/accounts/switch/{other_id}", follow_redirects=False)
    assert r2.status_code == 302, f"想定: 302, 実際: {r2.status_code}"
    with client.session_transaction() as sess:
        assert sess.get("active_account_id") == other_id, "切り替え後のセッションが更新されていません"
    print("OK: switch_account でセッションが", other_id, "に切り替わった")
else:
    print("SKIP: アクティブアカウントが1件のみのため切り替えテストは省略")

r3 = client.get("/accounts/switch/999999")
assert r3.status_code == 404, f"存在しないIDは404になるべき。実際: {r3.status_code}"
print("OK: 存在しないaccount_idは404")

print("ALL PASS")
```

- [ ] **Step 4: 検証スクリプトを実行する**

Run: `cd "C:\Users\mktis\kpopwave-tool" && venv\Scripts\python.exe "C:\Users\mktis\AppData\Local\Temp\claude\verify_account_switch.py"`

Expected: `ALL PASS` が最後に出力される（途中の `OK:` / `SKIP:` 行はアクティブアカウント数により変わる）

- [ ] **Step 5: 検証スクリプトを削除する**

Run: `rm "C:\Users\mktis\AppData\Local\Temp\claude\verify_account_switch.py"`

- [ ] **Step 6: Commit**

```bash
git add app.py
git commit -m "feat: アカウントIDをセッションで保持し、切り替えルートを追加"
```

---

### Task 2: サイドバーのアカウント切り替えドロップダウン

**Files:**
- Modify: `app.py:198-206`（`inject_globals()`）
- Modify: `templates/base.html:406-410`（サイドバー、ブランドロゴ直下）

**Interfaces:**
- Consumes: Task 1の `_selected_account_id()`, `switch_account` エンドポイント
- Produces: 全テンプレートから参照可能な `nav_accounts`（`ThreadsAccount`のリスト）, `nav_active_account_id`（`int | None`）

- [ ] **Step 1: `inject_globals()` にアカウント情報を追加する**

`app.py:198-206` の現在のコード:
```python
@app.context_processor
def inject_globals():
    return {
        "pending_count": Article.query.filter_by(status="pending").count(),
        "queued_count": Article.query.filter_by(status="queued").count(),
        "unread_comments_count": Comment.query.filter_by(is_read=0).count(),
        "youtube_min_view_count": Setting.get("youtube_min_view_count", "5000000"),
        "youtube_max_view_count": Setting.get("youtube_max_view_count", "0"),
    }
```

これを以下に置き換える:
```python
@app.context_processor
def inject_globals():
    return {
        "pending_count": Article.query.filter_by(status="pending").count(),
        "queued_count": Article.query.filter_by(status="queued").count(),
        "unread_comments_count": Comment.query.filter_by(is_read=0).count(),
        "youtube_min_view_count": Setting.get("youtube_min_view_count", "5000000"),
        "youtube_max_view_count": Setting.get("youtube_max_view_count", "0"),
        "nav_accounts": ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all(),
        "nav_active_account_id": _selected_account_id(),
    }
```

- [ ] **Step 2: `base.html` にドロップダウンを追加する**

`templates/base.html:406-410` の現在のコード:
```html
    <div class="col-auto sidebar d-none d-md-block p-3" style="width:225px">
      <div class="brand mb-4 ps-1">
        <i class="bi bi-music-note-beamed me-1" style="-webkit-text-fill-color:var(--accent);background:none;background-clip:unset"></i>Content<br><span style="font-size:1rem">Wave</span>
      </div>
      <ul class="nav flex-column mb-3">
```

これを以下に置き換える（ブランドロゴと`<ul>`の間にドロップダウンを挿入）:
```html
    <div class="col-auto sidebar d-none d-md-block p-3" style="width:225px">
      <div class="brand mb-4 ps-1">
        <i class="bi bi-music-note-beamed me-1" style="-webkit-text-fill-color:var(--accent);background:none;background-clip:unset"></i>Content<br><span style="font-size:1rem">Wave</span>
      </div>
      {% if nav_accounts|length > 1 %}
      <div class="dropdown mb-3">
        <button class="btn btn-outline-secondary btn-sm w-100 dropdown-toggle text-truncate" type="button" data-bs-toggle="dropdown" aria-expanded="false">
          <i class="bi bi-person-circle me-1"></i>{% for acc in nav_accounts %}{% if acc.id == nav_active_account_id %}{{ acc.account_label }}{% endif %}{% endfor %}
        </button>
        <ul class="dropdown-menu w-100">
          {% for acc in nav_accounts %}
          <li>
            <a class="dropdown-item d-flex justify-content-between align-items-center {% if acc.id == nav_active_account_id %}active{% endif %}"
               href="{{ url_for('switch_account', id=acc.id) }}">
              {{ acc.account_label }}
              {% if acc.id == nav_active_account_id %}<i class="bi bi-check-lg"></i>{% endif %}
            </a>
          </li>
          {% endfor %}
        </ul>
      </div>
      {% endif %}
      <ul class="nav flex-column mb-3">
```

- [ ] **Step 3: アプリを起動してブラウザで手動確認する**

Run: `cd "C:\Users\mktis\kpopwave-tool" && venv\Scripts\python.exe app.py`

ブラウザで `http://localhost:5000/` を開き、以下を確認する:
- `threads_accounts` にアクティブなアカウントが2件以上ある場合、サイドバーのブランドロゴ直下にドロップダウンボタンが表示され、現在のアカウント名が表示されている
- ボタンをクリックするとアカウント一覧が展開し、現在選択中のアカウントにチェックマークが付いている
- 別のアカウントをクリックすると、元のページに戻り、ボタンの表示名が切り替わったアカウントに変わる
- ダッシュボード・承認待ち・投稿キュー・スケジュールのどのページに移動しても、選択中のアカウント名がボタンに表示され続けている

アプリを `Ctrl+C` で停止する。

- [ ] **Step 4: Commit**

```bash
git add app.py templates/base.html
git commit -m "feat: サイドバーにアカウント切り替えドロップダウンを追加"
```

---

### Task 3: ダッシュボードのアカウント絞り込み

**Files:**
- Modify: `app.py:255-262`（`index()`）

**Interfaces:**
- Consumes: `_selected_account_id()`, `_account_query_scope(query, model_cls, account_id, legacy_id)`（`app.py:280-286`、既存）, `get_active_account(app)`
- Produces: 変更なし（`index.html` に渡す `stats` / `recent` の中身が絞り込まれるだけで、変数名・型は同じ）

- [ ] **Step 1: `index()` をアカウント絞り込み対応にする**

`app.py:255-262` の現在のコード:
```python
@app.route("/")
def index():
    stats = {
        s: Article.query.filter_by(status=s).count()
        for s in ("pending", "queued", "posted", "rejected", "failed")
    }
    recent = Article.query.order_by(Article.created_at.desc()).limit(15).all()
    return render_template("index.html", stats=stats, recent=recent)
```

これを以下に置き換える:
```python
@app.route("/")
def index():
    account_id = _selected_account_id()
    legacy = get_active_account(app)
    legacy_id = legacy["id"] if legacy else None

    def _scope(query):
        return _account_query_scope(query, Article, account_id, legacy_id)

    stats = {
        s: _scope(Article.query.filter_by(status=s)).count()
        for s in ("pending", "queued", "posted", "rejected", "failed")
    }
    recent = _scope(Article.query).order_by(Article.created_at.desc()).limit(15).all()
    return render_template("index.html", stats=stats, recent=recent)
```

- [ ] **Step 2: 検証スクリプトを書く（一時ファイル、コミットしない）**

`C:\Users\mktis\AppData\Local\Temp\claude\verify_dashboard_scope.py` に保存:
```python
import app as app_module
from database import db, Article, ThreadsAccount

flask_app = app_module.app
flask_app.config["TESTING"] = True
client = flask_app.test_client()

with flask_app.app_context():
    accounts = ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all()
    assert len(accounts) >= 1
    acc_a = accounts[0].id
    acc_b = accounts[1].id if len(accounts) >= 2 else None

    a1 = Article(feed_source="VerifyScope", title="口座A用テスト記事", url="https://example.com/verify-scope-a", status="pending", account_id=acc_a)
    db.session.add(a1)
    db.session.commit()
    a1_id = a1.id

with client.session_transaction() as sess:
    sess["active_account_id"] = acc_a
r = client.get("/")
assert r.status_code == 200
body = r.get_data(as_text=True)
assert "口座A用テスト記事" in body, "account_id一致時はダッシュボードのrecentに表示されるはず"
print("OK: 選択中アカウントの記事はダッシュボードに表示される")

if acc_b:
    with client.session_transaction() as sess:
        sess["active_account_id"] = acc_b
    r2 = client.get("/")
    body2 = r2.get_data(as_text=True)
    assert "口座A用テスト記事" not in body2, "account_id不一致時はダッシュボードのrecentに表示されないはず"
    print("OK: 別アカウント選択時は表示されない")
else:
    print("SKIP: アカウントが1件のみのため除外テストは省略")

with flask_app.app_context():
    db.session.delete(db.session.get(Article, a1_id))
    db.session.commit()
print("cleaned up")
print("ALL PASS")
```

- [ ] **Step 3: 検証スクリプトを実行する**

Run: `cd "C:\Users\mktis\kpopwave-tool" && venv\Scripts\python.exe "C:\Users\mktis\AppData\Local\Temp\claude\verify_dashboard_scope.py"`

Expected: `ALL PASS` が最後に出力される。テスト用記事はスクリプトの最後で自動的に削除される

- [ ] **Step 4: 検証スクリプトを削除する**

Run: `rm "C:\Users\mktis\AppData\Local\Temp\claude\verify_dashboard_scope.py"`

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "feat: ダッシュボードの統計・最近の記事を選択中アカウントで絞り込む"
```

---

### Task 4: 既存のページ内アカウント選択タブを削除

**Files:**
- Modify: `templates/pending.html:11-21`
- Modify: `templates/queue.html:6-16`
- Modify: `templates/schedule.html:13-23`
- Modify: `app.py`（`pending()` の `render_template` 呼び出し, `queue()` の `accounts` 変数と `render_template` 呼び出し, `schedule()` の `accounts` 変数と `render_template` 呼び出し）

**Interfaces:**
- Consumes: Task 2で追加したサイドバードロップダウン（これがタブの代替になる）
- Produces: なし（UI整理のみ。ルート関数のシグネチャ・エンドポイント名に変更なし）

- [ ] **Step 1: `queue.html` からアカウント選択タブを削除する**

`templates/queue.html:1-18` の現在のコード:
```html
{% extends 'base.html' %}
{% block content %}

<h4 class="mb-4 fw-bold">投稿キュー</h4>

<!-- アカウント選択タブ -->
{% if accounts and accounts|length > 1 %}
<div class="d-flex gap-2 mb-3 flex-wrap">
  {% for acc in accounts %}
  <a href="{{ url_for('queue', account_id=acc.id) }}"
     class="btn btn-sm {% if acc.id == active_account_id %}btn-accent{% else %}btn-outline-secondary{% endif %}">
    {{ acc.account_label }}
  </a>
  {% endfor %}
</div>
{% endif %}

<!-- キュー中 -->
<div class="card mb-4">
```

これを以下に置き換える:
```html
{% extends 'base.html' %}
{% block content %}

<h4 class="mb-4 fw-bold">投稿キュー</h4>

<!-- キュー中 -->
<div class="card mb-4">
```

- [ ] **Step 2: `queue()` ルートから不要になった `accounts` を削除する**

`app.py:683-712` の `queue()` 内、現在のコード（該当部分のみ抜粋）:
```python
    accounts = ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all()

    return render_template("queue.html", queued=queued, posted=posted, failed=failed, images_map=images_map,
                           accounts=accounts, active_account_id=account_id)
```

これを以下に置き換える:
```python
    return render_template("queue.html", queued=queued, posted=posted, failed=failed, images_map=images_map)
```

- [ ] **Step 3: `schedule.html` からアカウント選択タブを削除する**

`templates/schedule.html:1-23` の現在のコード:
```html
{% extends 'base.html' %}
{% block content %}

<div class="d-flex justify-content-between align-items-center mb-4">
  <h4 class="mb-0 fw-bold">
    <i class="bi bi-calendar-week-fill me-2" style="color:var(--accent)"></i>週間投稿スケジュール
  </h4>
  <span class="text-muted" style="font-size:.8rem">
    <i class="bi bi-shuffle me-1"></i>±30分のランダムゆらぎが自動で適用されます
  </span>
</div>

<!-- アカウント選択タブ -->
{% if accounts and accounts|length > 1 %}
<div class="d-flex gap-2 mb-3 flex-wrap">
  {% for acc in accounts %}
  <a href="{{ url_for('schedule', account_id=acc.id) }}"
     class="btn btn-sm {% if acc.id == active_account_id %}btn-accent{% else %}btn-outline-secondary{% endif %}">
    {{ acc.account_label }}
  </a>
  {% endfor %}
</div>
{% endif %}

<!-- 説明バー -->
```

これを以下に置き換える:
```html
{% extends 'base.html' %}
{% block content %}

<div class="d-flex justify-content-between align-items-center mb-4">
  <h4 class="mb-0 fw-bold">
    <i class="bi bi-calendar-week-fill me-2" style="color:var(--accent)"></i>週間投稿スケジュール
  </h4>
  <span class="text-muted" style="font-size:.8rem">
    <i class="bi bi-shuffle me-1"></i>±30分のランダムゆらぎが自動で適用されます
  </span>
</div>

<!-- 説明バー -->
```

- [ ] **Step 4: `schedule()` ルートから不要になった `accounts` を削除する**

`app.py:949-962` の現在のコード:
```python
    _DAY_LABELS = {
        "mon": "月", "tue": "火", "wed": "水", "thu": "木",
        "fri": "金", "sat": "土", "sun": "日",
    }
    current = get_weekly_schedule(app, account_id)
    accounts = ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all()
    return render_template(
        "schedule.html",
        schedule=current,
        day_keys=_DAY_KEYS,
        day_labels=_DAY_LABELS,
        accounts=accounts,
        active_account_id=account_id,
    )
```

これを以下に置き換える:
```python
    _DAY_LABELS = {
        "mon": "月", "tue": "火", "wed": "水", "thu": "木",
        "fri": "金", "sat": "土", "sun": "日",
    }
    current = get_weekly_schedule(app, account_id)
    return render_template(
        "schedule.html",
        schedule=current,
        day_keys=_DAY_KEYS,
        day_labels=_DAY_LABELS,
    )
```

- [ ] **Step 5: `pending.html` からアカウント選択タブのみを削除する（コンテンツタブは残す）**

`templates/pending.html:1-22` の現在のコード:
```html
{% extends 'base.html' %}
{% block content %}

<!-- ヘッダー -->
<div class="d-flex justify-content-between align-items-center mb-3">
  <h4 class="mb-0 fw-bold">承認待ち記事
    <span class="badge badge-pending ms-2" style="font-size:.7rem">{{ counts.all }}</span>
  </h4>
</div>

<!-- アカウント選択タブ -->
{% if accounts and accounts|length > 1 %}
<div class="d-flex gap-2 mb-3 flex-wrap">
  {% for acc in accounts %}
  <a href="{{ url_for('pending', tab=active_tab, account_id=acc.id) }}"
     class="btn btn-sm {% if acc.id == active_account_id %}btn-accent{% else %}btn-outline-secondary{% endif %}">
    {{ acc.account_label }}
  </a>
  {% endfor %}
</div>
{% endif %}

<!-- フィルタータブ -->
```

これを以下に置き換える（`active_account_id` を使う「フィルタータブ」以降はそのまま残す）:
```html
{% extends 'base.html' %}
{% block content %}

<!-- ヘッダー -->
<div class="d-flex justify-content-between align-items-center mb-3">
  <h4 class="mb-0 fw-bold">承認待ち記事
    <span class="badge badge-pending ms-2" style="font-size:.7rem">{{ counts.all }}</span>
  </h4>
</div>

<!-- フィルタータブ -->
```

- [ ] **Step 6: `pending()` ルートから不要になった `accounts` のみを削除する（`active_account_id` は残す）**

`app.py:423-427` の現在のコード:
```python
    accounts = ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).all()

    return render_template("pending.html", articles=articles, images_map=images_map,
                           active_tab=tab, counts=counts, now_utc=datetime.utcnow(),
                           accounts=accounts, active_account_id=account_id)
```

これを以下に置き換える（`pending.html` のフィルタータブが `active_account_id` を使い続けるため、これは残す）:
```python
    return render_template("pending.html", articles=articles, images_map=images_map,
                           active_tab=tab, counts=counts, now_utc=datetime.utcnow(),
                           active_account_id=account_id)
```

- [ ] **Step 7: アプリを起動してブラウザで手動確認する**

Run: `cd "C:\Users\mktis\kpopwave-tool" && venv\Scripts\python.exe app.py`

ブラウザで以下を確認する:
- `http://localhost:5000/pending` — ページ内のアカウント選択タブ（ボタン列）が消えている。中身のフィルタータブ（すべて/RSS記事/YouTube/動画/投稿済み動画）は正常に動作する
- `http://localhost:5000/queue` — ページ内のアカウント選択タブが消えている
- `http://localhost:5000/schedule` — ページ内のアカウント選択タブが消えている
- いずれのページも、サイドバーのドロップダウンでアカウントを切り替えると内容が正しく絞り込まれる

アプリを `Ctrl+C` で停止する。

- [ ] **Step 8: Commit**

```bash
git add app.py templates/pending.html templates/queue.html templates/schedule.html
git commit -m "refactor: ページ内アカウント選択タブを削除しサイドバーのドロップダウンに一本化"
```
