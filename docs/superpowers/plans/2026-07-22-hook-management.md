# 投稿文フック管理機能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KPOPアカウント（account_id=1）・ガチャ沼の住人アカウント（account_id=2）双方で使える投稿文フック管理機能を実装する。管理画面からフレーズを追加・編集・削除でき、「要約を生成」実行時にアカウントごとのフックをローテーションで1つ取り出し投稿文に付加する。

**Architecture:** 新規`Hook`テーブル（アカウントID・フレーズ本文・表示順・最終使用日時）を追加し、`/hooks/<account_id>`のCRUD画面から管理する。`summarizer.py`の既存フック機構（`_KPOP_HOOKS`辞書からランダム選択しAIプロンプトに注入する方式）を、生成済み本文にフックを機械的に連結する方式に置き換える。これにより、KPOP・ガチャ・AI無効時ルールベースの3つの生成経路すべてで同じフック機構を使い回せる。ローテーションは`last_used_at`昇順（未使用が常に最優先）で管理し、専用のカーソル状態を持たない。

**Tech Stack:** Python 3.x / Flask / SQLAlchemy / SQLite / Jinja2 / vanilla JS（fetch API）/ anthropic SDK

## Global Constraints

- 対象ファイルは`database.py`・`app.py`・`templates/hooks.html`（新規）・`templates/base.html`・`summarizer.py`のみ
- コメントは原則書かない（WHYが非自明な場合のみ1行）
- 既存ファイルを編集する。新規ファイルは`templates/hooks.html`のみ（既存パターンで新規テンプレート作成は許容されている）
- 必要な変更だけ行う。リファクタリング・クリーンアップは不要
- `summarizer.py`の`STRUCTURE_SECTION`・`COMMON_RULES`内の「フックで引き込む」等の一般的なプロンプト文言は変更しない（AIへの一般的な文章指導として引き続き有効であり、変更範囲を最小化してプロンプト全体への影響を抑えるため）
- Shellコマンドの実行環境はWindows。Bashツールを使う場合はGit Bash構文（`$VAR`、`&&`可）、PowerShellツールを使う場合はPowerShell構文（`$env:VAR`、`&&`不可）に注意する
- このプロジェクトにはpytest等のテストフレームワークが存在しない。各タスクの検証は、実際にFlaskアプリを起動し、`venv/Scripts/python.exe -c "..."`によるDB直接確認や`app.test_client()`によるHTTPリクエストで動作確認する
- `venv/Scripts/python.exe`が仮想環境のPythonインタプリタ
- **重要**: このプロジェクトの`app.py`は`app = create_app()`をモジュール読み込み時に実行し（`app.py:200`）、以降の`@app.route`はこのモジュール変数`app`に紐づく。`test_client()`での検証には必ず`from app import app`を使うこと（`from app import create_app; app = create_app()`は無関係な別インスタンスを作るため404になる）
- 開発サーバー（ポート5000）はセッションを跨いで古いプロセスが残ることがある。新たに`python app.py`/`python run.py`を起動しないこと。`app.test_client()`を使えば起動中のサーバーに依存せず検証できる
- 新規テーブル（`Hook`）は`create_app()`内の`db.create_all()`で自動作成されるため、`_migrate_db()`に`ALTER TABLE`は不要（デフォルトデータ投入のみ追記する）

---

## ファイル構成

| ファイル | 変更内容 |
|---|---|
| `database.py` | `Hook`モデルを追加 |
| `app.py` | `_migrate_db()`にデフォルトフック投入処理を追加、`/hooks/<account_id>`系ルート（一覧・追加・編集・削除）を追加 |
| `templates/hooks.html` | 新規作成。フック管理画面 |
| `templates/base.html` | デスクトップサイドバー・モバイルナビバー両方に「フック管理」リンクを追加 |
| `summarizer.py` | `_KPOP_HOOKS`辞書・`HOOK_SECTION`プロンプト注入を削除し、`_get_next_hook`/`_attach_hook`による機械的連結方式に置き換え |

---

### Task 1: `Hook`モデルを追加（`database.py`）

**Files:**
- Modify: `database.py:59-70`（`ThreadsAccount`クラスの直後）

**Interfaces:**
- Produces: `Hook`モデル（`id`, `account_id`, `phrase`, `display_order`, `last_used_at`, `created_at`）。以降の全タスクがこのモデルを読み書きする

- [ ] **Step 1: `database.py`に`Hook`クラスを追加**

`database.py:59-70`（目印）:

```python
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


def get_active_account(app, account_id: int = None) -> dict:
```

これを以下に置き換える（`ThreadsAccount`クラスと`get_active_account`関数の間に新規クラスを挿入）:

```python
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


class Hook(db.Model):
    __tablename__ = "hooks"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("threads_accounts.id"), nullable=False, index=True)
    phrase = db.Column(db.String(200), nullable=False)
    display_order = db.Column(db.Integer, nullable=False, default=0)
    last_used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def get_active_account(app, account_id: int = None) -> dict:
```

- [ ] **Step 2: テーブルが自動作成されることを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app
from database import Hook, db
with app.app_context():
    db.create_all()
    print('hooks table columns:', [c.name for c in Hook.__table__.columns])
    print('count:', Hook.query.count())
"
```
Expected: `hooks table columns: ['id', 'account_id', 'phrase', 'display_order', 'last_used_at', 'created_at']`、`count: 0`（この時点ではまだ何も投入していない）。

- [ ] **Step 3: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile database.py
```
Expected: エラーなし。

- [ ] **Step 4: Commit**

```bash
git add database.py
git commit -m "$(cat <<'EOF'
feat: 投稿文フック管理用のHookテーブルを追加

アカウントID・フレーズ本文・表示順・最終使用日時を持つ新規テーブル。
新規テーブルのためdb.create_all()で自動作成され、ALTER TABLEは不要。
EOF
)"
```

---

### Task 2: フックCRUDルートとデフォルトフック投入（`app.py`）

**Files:**
- Modify: `app.py:16`（import）
- Modify: `app.py:162-173`（`_migrate_db()`末尾、デフォルトフック投入処理を追加）
- Modify: `app.py:1247-1253`（`switch_account`ルートの直後、新規ルートを挿入）

**Interfaces:**
- Consumes: `Hook`モデル（Task 1）
- Produces: `GET /hooks/<int:account_id>`（endpoint名`hooks_page`）、`POST /hooks/<int:account_id>/add`、`POST /hooks/<int:id>/edit`、`POST /hooks/<int:id>/delete`。Task 3・4がこれらを利用する

- [ ] **Step 1: importに`Hook`を追加**

`app.py:16`（目印）:

```python
from database import Article, BuzzPost, Comment, Setting, ThreadsAccount, get_active_account, db
```

これを以下に置き換える:

```python
from database import Article, BuzzPost, Comment, Hook, Setting, ThreadsAccount, get_active_account, db
```

- [ ] **Step 2: `_migrate_db()`末尾にデフォルトフック投入処理を追加**

`app.py:162-173`（目印。`comments`テーブルのマイグレーションブロック、`_migrate_db()`関数の末尾）:

```python
    # comments テーブル
    existing_comments = {c["name"] for c in inspector.get_columns("comments")}
    comment_cols = [
        ("is_liked", "INTEGER DEFAULT 0"),
    ]
    with db.engine.connect() as conn:
        for col, typedef in comment_cols:
            if col not in existing_comments:
                conn.execute(text(f"ALTER TABLE comments ADD COLUMN {col} {typedef}"))
                conn.commit()
                logger.info("DB migration: comments.%s added", col)


def _init_default_settings():
```

これを以下に置き換える（`_migrate_db()`の末尾、`_init_default_settings()`の直前に投入処理を挿入）:

```python
    # comments テーブル
    existing_comments = {c["name"] for c in inspector.get_columns("comments")}
    comment_cols = [
        ("is_liked", "INTEGER DEFAULT 0"),
    ]
    with db.engine.connect() as conn:
        for col, typedef in comment_cols:
            if col not in existing_comments:
                conn.execute(text(f"ALTER TABLE comments ADD COLUMN {col} {typedef}"))
                conn.commit()
                logger.info("DB migration: comments.%s added", col)

    # hooks テーブル: デフォルトフックの初回投入
    if Hook.query.count() == 0:
        default_hooks = {
            1: [
                "待って、これやばい。", "え、この子なに。", "これ知ってる人少ないと思う。",
                "布教させてください。", "好きにならない方が無理じゃない？", "これ好きな人いる？",
                "保存推奨。", "語彙力消えた。", "今のうちに見て。", "なんで知らなかったんだろ。",
            ],
            2: [
                "これマジで欲しい…", "新作きてる…！", "見つけた瞬間テンション上がった。",
                "これは即回さなきゃ。", "ガチャ勢は絶対チェックして。", "今回のクオリティやばい。",
                "うわ、これ欲しすぎる。", "推しキャラのガチャ来た…！", "この造形細かすぎない？",
                "コンプリートしたくなる…",
            ],
        }
        for account_id, phrases in default_hooks.items():
            for order, phrase in enumerate(phrases):
                db.session.add(Hook(account_id=account_id, phrase=phrase, display_order=order))
        db.session.commit()
        logger.info("DB migration: hooks にデフォルトフックを投入 (account_id=1: %d件, account_id=2: %d件)",
                    len(default_hooks[1]), len(default_hooks[2]))


def _init_default_settings():
```

- [ ] **Step 3: フックCRUDルートを追加**

`app.py:1247-1255`（目印）:

```python
@app.route("/accounts/switch/<int:id>")
def switch_account(id):
    """サイドバーのアカウント切り替えドロップダウンから呼ばれる。セッションに保存して元のページへ戻る。"""
    account = ThreadsAccount.query.get_or_404(id)
    session["active_account_id"] = account.id
    return redirect(request.referrer or url_for("index"))


@app.route("/auth/threads/manual")
```

これを以下に置き換える（`switch_account`と`threads_auth_manual`の間に新規ルートを挿入）:

```python
@app.route("/accounts/switch/<int:id>")
def switch_account(id):
    """サイドバーのアカウント切り替えドロップダウンから呼ばれる。セッションに保存して元のページへ戻る。"""
    account = ThreadsAccount.query.get_or_404(id)
    session["active_account_id"] = account.id
    return redirect(request.referrer or url_for("index"))


@app.route("/hooks/<int:account_id>")
def hooks_page(account_id):
    account = ThreadsAccount.query.get_or_404(account_id)
    hooks = (
        Hook.query.filter_by(account_id=account_id)
        .order_by(Hook.last_used_at.asc(), Hook.display_order.asc())
        .all()
    )
    next_hook_id = hooks[0].id if hooks else None
    return render_template("hooks.html", account=account, hooks=hooks, next_hook_id=next_hook_id)


@app.route("/hooks/<int:account_id>/add", methods=["POST"])
def add_hook(account_id):
    ThreadsAccount.query.get_or_404(account_id)
    phrase = (request.form.get("phrase") or "").strip()
    if not phrase:
        return jsonify({"ok": False, "error": "フレーズを入力してください"}), 400
    max_order = db.session.query(db.func.max(Hook.display_order)).filter(Hook.account_id == account_id).scalar()
    hook = Hook(account_id=account_id, phrase=phrase, display_order=(max_order or 0) + 1)
    db.session.add(hook)
    db.session.commit()
    return jsonify({"ok": True, "id": hook.id})


@app.route("/hooks/<int:id>/edit", methods=["POST"])
def edit_hook(id):
    hook = Hook.query.get_or_404(id)
    phrase = (request.form.get("phrase") or "").strip()
    if not phrase:
        return jsonify({"ok": False, "error": "フレーズを入力してください"}), 400
    hook.phrase = phrase
    db.session.commit()
    return jsonify({"ok": True, "phrase": hook.phrase})


@app.route("/hooks/<int:id>/delete", methods=["POST"])
def delete_hook(id):
    hook = Hook.query.get_or_404(id)
    db.session.delete(hook)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/auth/threads/manual")
```

- [ ] **Step 4: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile app.py
```
Expected: エラーなし。

- [ ] **Step 5: マイグレーションとルートの動作を確認する**

```bash
venv/Scripts/python.exe -c "
from app import app
from database import Hook, db

with app.app_context():
    for aid in (1, 2):
        hooks = Hook.query.filter_by(account_id=aid).order_by(Hook.display_order.asc()).all()
        print(f'account_id={aid}: {len(hooks)}件')
        for h in hooks:
            print(' -', h.phrase)
"
```
Expected: `account_id=1: 10件`・`account_id=2: 10件`、それぞれ設計書通りのフレーズが出力される。

続けて`test_client()`でルートの動作を確認する（Global Constraintsに従い`from app import app`を使うこと）:

```bash
venv/Scripts/python.exe -c "
from app import app
from database import Hook, db

client = app.test_client()

r = client.get('/hooks/1')
print('GET /hooks/1:', r.status_code)

r = client.get('/hooks/999')
print('GET /hooks/999 (存在しない):', r.status_code)

r = client.post('/hooks/1/add', data={'phrase': 'テストフック'})
print('POST /hooks/1/add:', r.status_code, r.get_json())
new_id = r.get_json()['id']

r = client.post(f'/hooks/{new_id}/edit', data={'phrase': '編集後フック'})
print('POST edit:', r.status_code, r.get_json())

r = client.post(f'/hooks/{new_id}/delete')
print('POST delete:', r.status_code, r.get_json())

with app.app_context():
    print('削除後の存在確認:', db.session.get(Hook, new_id))
"
```
Expected: `GET /hooks/1`が200、`GET /hooks/999`が404、追加・編集・削除がいずれも`{'ok': True, ...}`、削除後の存在確認が`None`。

- [ ] **Step 6: Commit**

```bash
git add app.py
git commit -m "$(cat <<'EOF'
feat: フック管理のCRUDルートとデフォルトフック投入を追加

/hooks/<account_id>の一覧・追加・編集・削除エンドポイントと、
初回起動時にKPOP・ガチャ両アカウントへ10個ずつデフォルトフックを
投入するマイグレーション処理を追加した。
EOF
)"
```

---

### Task 3: フック管理画面テンプレート（`templates/hooks.html`、新規）

**Files:**
- Create: `templates/hooks.html`

**Interfaces:**
- Consumes: `hooks_page`ルートが渡す`account`（`ThreadsAccount`）、`hooks`（ローテーション優先順の`Hook`リスト）、`next_hook_id`（次に使うフックのid、Task 2）。`add_hook`/`edit_hook`/`delete_hook`エンドポイント（Task 2）をfetch APIで呼ぶ

- [ ] **Step 1: `templates/hooks.html`を新規作成**

```html
{% extends 'base.html' %}
{% block content %}

<div class="d-flex justify-content-between align-items-center mb-3">
  <h4 class="mb-0 fw-bold">フック管理 — {{ account.account_label }}</h4>
</div>

<div class="card p-3 mb-3">
  <form id="add-form" class="row g-2 align-items-end" onsubmit="return addHook(event)">
    <div class="col">
      <label style="font-size:.78rem;color:var(--text-muted);display:block;margin-bottom:2px">新しいフレーズ</label>
      <input type="text" id="add-phrase" class="form-control form-control-sm" placeholder="例: これマジで欲しい…" required maxlength="200">
    </div>
    <div class="col-auto">
      <button type="submit" class="btn btn-sm btn-accent">追加</button>
    </div>
  </form>
</div>

<div class="table-responsive">
  <table class="table table-hover mb-0" style="font-size:.85rem">
    <thead>
      <tr>
        <th style="width:60px"></th>
        <th>フレーズ</th>
        <th style="width:180px">最終使用日時</th>
        <th style="width:110px"></th>
      </tr>
    </thead>
    <tbody>
      {% for h in hooks %}
      <tr>
        <td>
          {% if h.id == next_hook_id %}
          <span class="badge" style="background:var(--accent)">次に使う</span>
          {% endif %}
        </td>
        <td>
          <span id="hook-display-{{ h.id }}">{{ h.phrase }}</span>
          <span id="hook-edit-{{ h.id }}" style="display:none">
            <input type="text" id="hook-input-{{ h.id }}" class="form-control form-control-sm d-inline-block"
                   value="{{ h.phrase }}" maxlength="200" style="width:260px;font-size:.85rem">
            <button type="button" class="btn btn-sm py-0 px-1 btn-success" style="font-size:.72rem"
                    onclick="saveHook({{ h.id }})">✓</button>
            <button type="button" class="btn btn-sm py-0 px-1 btn-outline-secondary" style="font-size:.72rem"
                    onclick="cancelEditHook({{ h.id }})">✕</button>
          </span>
        </td>
        <td style="color:var(--text-muted)">
          {{ h.last_used_at | utc_to_jst if h.last_used_at else "未使用" }}
        </td>
        <td class="text-end">
          <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-2" style="font-size:.75rem"
                  onclick="startEditHook({{ h.id }})">
            <i class="bi bi-pencil"></i>
          </button>
          <button type="button" class="btn btn-sm btn-outline-danger py-0 px-2" style="font-size:.75rem"
                  onclick="deleteHook({{ h.id }})">
            <i class="bi bi-trash3"></i>
          </button>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="4" class="text-muted text-center py-3">フックが登録されていません</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>

{% endblock %}

{% block scripts %}
<script>
function addHook(event) {
  event.preventDefault();
  const input = document.getElementById("add-phrase");
  const phrase = input.value.trim();
  if (!phrase) return false;
  fetch(window.location.pathname + "/add", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ phrase: phrase }).toString(),
  })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        location.reload();
      } else {
        alert(d.error || "追加に失敗しました");
      }
    })
    .catch(() => alert("追加に失敗しました"));
  return false;
}

function startEditHook(id) {
  document.getElementById("hook-display-" + id).style.display = "none";
  document.getElementById("hook-edit-" + id).style.display = "inline-block";
  const input = document.getElementById("hook-input-" + id);
  input.focus();
  input.select();
}
function cancelEditHook(id) {
  document.getElementById("hook-edit-" + id).style.display = "none";
  document.getElementById("hook-display-" + id).style.display = "";
}
function saveHook(id) {
  const input = document.getElementById("hook-input-" + id);
  const phrase = input.value.trim();
  if (!phrase) return;
  fetch(`/hooks/${id}/edit`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ phrase: phrase }).toString(),
  })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        location.reload();
      } else {
        alert(d.error || "更新に失敗しました");
      }
    })
    .catch(() => alert("更新に失敗しました"));
}
function deleteHook(id) {
  if (!confirm("このフックを削除しますか？")) return;
  fetch(`/hooks/${id}/delete`, { method: "POST" })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        location.reload();
      } else {
        alert(d.error || "削除に失敗しました");
      }
    })
    .catch(() => alert("削除に失敗しました"));
}
</script>
{% endblock %}
```

- [ ] **Step 2: テンプレートが正しくレンダリングされることを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app

client = app.test_client()
r = client.get('/hooks/1')
html = r.get_data(as_text=True)
print('status:', r.status_code)
print('has add form:', 'add-form' in html)
print('has next badge:', '次に使う' in html)
print('hook count in table:', html.count('hook-display-'))
"
```
Expected: `status: 200`、`has add form: True`、`has next badge: True`（Task 2で投入した10件が存在するため）、`hook count in table: 10`。

- [ ] **Step 3: Commit**

```bash
git add templates/hooks.html
git commit -m "$(cat <<'EOF'
feat: フック管理画面テンプレートを追加

一覧表示（次に使うフックをバッジ表示）・追加・インライン編集・削除
をfetch APIで行う。既存のfollow_candidates.html等と同じパターン。
EOF
)"
```

---

### Task 4: サイドバーナビゲーション（`templates/base.html`）

**Files:**
- Modify: `templates/base.html:514-518`（デスクトップサイドバー、`設定`リンクの直前）
- Modify: `templates/base.html:433-435`（モバイルナビバー、`設定`リンクの直前）

**Interfaces:**
- Consumes: `nav_active_account_id`（既存のグローバルテンプレート変数、`app.py`の`inject_globals()`が注入済み）、`hooks_page`エンドポイント（Task 2）

- [ ] **Step 1: デスクトップサイドバーにリンクを追加**

`templates/base.html:513-519`（目印）:

```html
        <li class="nav-item">
          <a class="nav-link {% if request.endpoint=='learning' %}active{% endif %}" href="{{ url_for('learning') }}">
            <i class="bi bi-lightning-charge-fill"></i> 学習
          </a>
        </li>
        <li class="nav-item">
          <a class="nav-link {% if request.endpoint=='settings' %}active{% endif %}" href="{{ url_for('settings') }}">
            <i class="bi bi-gear-fill"></i> 設定
          </a>
        </li>
      </ul>
```

これを以下に置き換える（`学習`と`設定`の間に新規リンクを挿入）:

```html
        <li class="nav-item">
          <a class="nav-link {% if request.endpoint=='learning' %}active{% endif %}" href="{{ url_for('learning') }}">
            <i class="bi bi-lightning-charge-fill"></i> 学習
          </a>
        </li>
        <li class="nav-item">
          <a class="nav-link {% if request.endpoint=='hooks_page' %}active{% endif %}" href="{{ url_for('hooks_page', account_id=nav_active_account_id) }}">
            <i class="bi bi-magic"></i> フック管理
          </a>
        </li>
        <li class="nav-item">
          <a class="nav-link {% if request.endpoint=='settings' %}active{% endif %}" href="{{ url_for('settings') }}">
            <i class="bi bi-gear-fill"></i> 設定
          </a>
        </li>
      </ul>
```

- [ ] **Step 2: モバイルナビバーにリンクを追加**

`templates/base.html:430-435`（目印）:

```html
  <a href="{{ url_for('learning') }}" class="{% if request.endpoint=='learning' %}active{% endif %}">
    <i class="bi bi-lightning-charge-fill"></i> 学習
  </a>
  <a href="{{ url_for('settings') }}" class="{% if request.endpoint=='settings' %}active{% endif %}">
    <i class="bi bi-gear-fill"></i> 設定
  </a>
</nav>
```

これを以下に置き換える:

```html
  <a href="{{ url_for('learning') }}" class="{% if request.endpoint=='learning' %}active{% endif %}">
    <i class="bi bi-lightning-charge-fill"></i> 学習
  </a>
  <a href="{{ url_for('hooks_page', account_id=nav_active_account_id) }}" class="{% if request.endpoint=='hooks_page' %}active{% endif %}">
    <i class="bi bi-magic"></i> フック
  </a>
  <a href="{{ url_for('settings') }}" class="{% if request.endpoint=='settings' %}active{% endif %}">
    <i class="bi bi-gear-fill"></i> 設定
  </a>
</nav>
```

- [ ] **Step 3: サイドバーリンクが選択中アカウントに応じて切り替わることを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app

client = app.test_client()

r = client.get('/?account_id=1')
html = r.get_data(as_text=True)
print('account_id=1のとき /hooks/1 リンクあり:', '/hooks/1' in html)

r = client.get('/?account_id=2')
html = r.get_data(as_text=True)
print('account_id=2のとき /hooks/2 リンクあり:', '/hooks/2' in html)
"
```
Expected: 両方とも`True`。

- [ ] **Step 4: Commit**

```bash
git add templates/base.html
git commit -m "$(cat <<'EOF'
feat: サイドバーにフック管理へのリンクを追加

デスクトップ・モバイル両方のナビに追加。選択中アカウントに応じて
/hooks/1または/hooks/2に自動的に遷移する。
EOF
)"
```

---

### Task 5: 投稿文生成へのフック統合（`summarizer.py`）

**Files:**
- Modify: `summarizer.py:12`（import）
- Modify: `summarizer.py:53-90`（`_KPOP_HOOKS`辞書を削除）
- Modify: `summarizer.py:209-214`（`_title_only_summary`の直後に新規ヘルパー2つを追加）
- Modify: `summarizer.py:403-411`（`summarize_article`冒頭、旧フック選択ロジックを削除）
- Modify: `summarizer.py:463-484`（記事情報取得ブロック、フック取得を追加）
- Modify: `summarizer.py:485-499`（AI無効時のルールベース経路、フック連結を追加）
- Modify: `summarizer.py:533-572`（ガチャ汎用生成経路、フック連結を追加）
- Modify: `summarizer.py:574-586`（`HOOK_SECTION`定義を削除）
- Modify: `summarizer.py:748-827`（KPOP Step1プロンプト4パターンから`HOOK_SECTION`注入を削除）
- Modify: `summarizer.py:843-865`（Step2プロンプト2パターンから「フックを変えないこと」指示を削除）
- Modify: `summarizer.py:867-886`（最終アセンブリ、フック連結を追加）

**Interfaces:**
- Consumes: `Hook`モデル（Task 1）
- Produces: `_get_next_hook(app, account_id) -> str | None`、`_attach_hook(hook, body, body_max) -> str`。3つの生成経路すべての最終`post_text`にフックが連結される

- [ ] **Step 1: importに`Hook`を追加**

`summarizer.py:12`（目印）:

```python
from database import Article, BuzzPost, Setting, ThreadsAccount, db
```

これを以下に置き換える:

```python
from database import Article, BuzzPost, Hook, Setting, ThreadsAccount, db
```

- [ ] **Step 2: `_KPOP_HOOKS`辞書を削除**

`summarizer.py:53-92`（目印。ファイル内で`_KPOP_HOOKS`が定義されている箇所、直後の`EXPRESSIONS_VISUAL`定義の直前まで）:

```python
_KPOP_HOOKS: dict[str, list[str]] = {
    "衝撃・驚き型": [
        "待って、これやばい。", "ちょっと待って、これ見て。", "え、この子なに。",
        "は？天才なんだけど。", "待って無理。", "え、待って。", "ちょっとこれ見て。",
        "やば、これは。", "うわ、やばいの見つけた。", "これ、レベルが違いすぎる。",
        "ちょっと落ち着いて見て。", "待って、完成度が高すぎる。",
    ],
    "こっそり・共有型": [
        "これ知ってる人少ないと思う。", "みんなにも見てほしい。", "布教させてください。",
        "一人で見るのもったいない。", "こっそり共有。", "これ広まってほしい。",
        "みんな見た？", "内緒で教える。", "これ埋もれてるのもったいない。",
        "もっと評価されるべき。",
    ],
    "問いかけ型": [
        "この子やばくない？", "これ好きな人いる？", "私だけじゃないよね？",
        "なんでこんなに上手いの。", "これ見て何も思わない人いる？",
        "好きにならない方が無理じゃない？", "これ反則じゃない？",
        "こんなのずるくない？", "みんなどう思う？", "これやばいって思うの私だけ？",
    ],
    "保存促進型": [
        "これは保存案件。", "保存して何回も見て。", "見返したくなるやつ。",
        "保存推奨。", "あとで見返すやつ。", "保存しないと損。", "これはフォルダ行き。",
    ],
    "感情爆発型": [
        "語彙力消えた。", "もう無理、好き。", "尊すぎる。", "何回見ても飽きない。",
        "好きすぎてしんどい。", "沼確定。", "優勝。", "もう優勝でいい。",
        "ぐうの音も出ない。", "完全にやられた。", "降参です。", "好きが止まらない。",
    ],
    "限定・希少型": [
        "今のうちに見て。", "これ伸びる前に保存して。", "これは絶対見てほしい。",
        "今見とくべき。", "後で絶対話題になる。", "見逃したら後悔する。", "今が旬。",
    ],
    "独り言・つぶやき型": [
        "なんで知らなかったんだろ。", "もっと早く出会いたかった。", "今日のハイライトこれ。",
        "これ見れただけで満足。", "今日もありがとう。", "だから推しはやめられない。",
        "これだから沼から出られない。",
    ],
}

# ランダム表現選択リスト（カテゴリ別）
EXPRESSIONS_VISUAL = [
```

これを以下に置き換える（`_KPOP_HOOKS`辞書全体を削除）:

```python
# ランダム表現選択リスト（カテゴリ別）
EXPRESSIONS_VISUAL = [
```

- [ ] **Step 3: `_get_next_hook`/`_attach_hook`ヘルパーを追加**

`summarizer.py:209-217`（目印。`_title_only_summary`関数の直後、`_detect_group_name`関数の直前）:

```python
def _title_only_summary(title: str, body_max: int) -> str:
    """AI無効時: タイトルをそのまま投稿文にする（上限超過時は末尾を切り詰める）。"""
    title = (title or "").strip()
    if len(title) <= body_max:
        return title
    return title[: body_max - 1] + "…"


def _detect_group_name(feed_source: str, title: str) -> str:
```

これを以下に置き換える:

```python
def _title_only_summary(title: str, body_max: int) -> str:
    """AI無効時: タイトルをそのまま投稿文にする（上限超過時は末尾を切り詰める）。"""
    title = (title or "").strip()
    if len(title) <= body_max:
        return title
    return title[: body_max - 1] + "…"


def _get_next_hook(app, account_id: int) -> str | None:
    """アカウントのフックをローテーションで1件取得し、last_used_atを更新する。
    未使用のフックが常に最優先（last_used_at IS NULLはASC順で先頭に来る）。"""
    with app.app_context():
        hook = (
            Hook.query.filter_by(account_id=account_id)
            .order_by(Hook.last_used_at.asc(), Hook.display_order.asc())
            .first()
        )
        if not hook:
            return None
        hook.last_used_at = datetime.utcnow()
        db.session.commit()
        return hook.phrase


def _attach_hook(hook: str | None, body: str, body_max: int) -> str:
    """フックを本文の先頭に連結する。上限超過時は安全側で切り詰める。"""
    if not hook:
        return body
    combined = hook + body
    if len(combined) > body_max:
        combined = combined[:body_max - 1] + "…"
    return combined


def _detect_group_name(feed_source: str, title: str) -> str:
```

- [ ] **Step 4: `summarize_article`冒頭の旧フック選択ロジックを削除**

`summarizer.py:403-412`（目印）:

```python
def summarize_article(app, article_id: int, style: str = "つぶやき型", scheduled_at: str | None = None) -> bool:
    """1 記事の日本語投稿テキストを生成して DB に保存する。成功なら True。"""
    logger.info("[summarize_article] article=%d style=%r scheduled_at=%r", article_id, style, scheduled_at)
    style_conf = _STYLE_PROMPTS.get(style, _STYLE_PROMPTS["つぶやき型"])
    style_tone = style_conf["tone"]
    all_hooks = [h for hooks in _KPOP_HOOKS.values() for h in hooks]
    selected_hook = random.choice(all_hooks)
    logger.info("[summarize_article] selected_hook=%r", selected_hook)
    time_hint  = _get_time_style_hint()
```

これを以下に置き換える:

```python
def summarize_article(app, article_id: int, style: str = "つぶやき型", scheduled_at: str | None = None) -> bool:
    """1 記事の日本語投稿テキストを生成して DB に保存する。成功なら True。"""
    logger.info("[summarize_article] article=%d style=%r scheduled_at=%r", article_id, style, scheduled_at)
    style_conf = _STYLE_PROMPTS.get(style, _STYLE_PROMPTS["つぶやき型"])
    style_tone = style_conf["tone"]
    time_hint  = _get_time_style_hint()
```

- [ ] **Step 5: 記事情報取得ブロックでフックを取得する**

`summarizer.py:463-484`（目印）:

```python
    # ── 記事情報取得 ───────────────────────────────────────────────────────
    with app.app_context():
        article = db.session.get(Article, article_id)
        if not article:
            logger.error("article id=%d が見つかりません", article_id)
            return False
        title         = article.title
        stored_body   = (article.raw_content or "")[:3000]
        url           = article.url
        feed_source   = article.feed_source or ""
        thumbnail_url = article.thumbnail_url or ""
        is_ja_src     = _is_japanese_source(feed_source)
        content_type  = article.content_type or "article"
        content_topic = ""
        if article.account_id:
            acc = db.session.get(ThreadsAccount, article.account_id)
            if acc and acc.content_topic:
                content_topic = acc.content_topic.strip()

    is_video_post = (content_type == "video")
    body_max = BODY_MAX_VIDEO if is_video_post else BODY_MAX_ARTICLE
```

これを以下に置き換える:

```python
    # ── 記事情報取得 ───────────────────────────────────────────────────────
    with app.app_context():
        article = db.session.get(Article, article_id)
        if not article:
            logger.error("article id=%d が見つかりません", article_id)
            return False
        title         = article.title
        stored_body   = (article.raw_content or "")[:3000]
        url           = article.url
        feed_source   = article.feed_source or ""
        thumbnail_url = article.thumbnail_url or ""
        is_ja_src     = _is_japanese_source(feed_source)
        content_type  = article.content_type or "article"
        article_account_id = article.account_id
        content_topic = ""
        if article.account_id:
            acc = db.session.get(ThreadsAccount, article.account_id)
            if acc and acc.content_topic:
                content_topic = acc.content_topic.strip()

    is_video_post = (content_type == "video")
    body_max = BODY_MAX_VIDEO if is_video_post else BODY_MAX_ARTICLE
    hook = _get_next_hook(app, article_account_id) if article_account_id else None
```

- [ ] **Step 6: AI無効時のルールベース経路にフックを連結する**

`summarizer.py:485-499`（目印）:

```python
    # ── AI生成フラグ確認（無効ならタイトルそのままで即保存して終了） ──────────
    if not _ai_summary_enabled(app):
        post_text = _title_only_summary(title, body_max)
        with app.app_context():
```

これを以下に置き換える:

```python
    # ── AI生成フラグ確認（無効ならタイトルそのままで即保存して終了） ──────────
    if not _ai_summary_enabled(app):
        post_text = _attach_hook(hook, _title_only_summary(title, body_max), body_max)
        with app.app_context():
```

- [ ] **Step 7: ガチャ汎用生成経路にフックを連結する**

`summarizer.py`内、以下のブロック（目印）:

```python
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": generic_prompt}],
            )
            post_text = msg.content[0].text.strip()
            if len(post_text) > body_max:
                post_text = post_text[:body_max - 1] + "…"
        except Exception as exc:
            logger.error("Generic summarize error for article %d: %s", article_id, exc, exc_info=True)
```

これを以下に置き換える:

```python
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": generic_prompt}],
            )
            post_text = _attach_hook(hook, msg.content[0].text.strip(), body_max)
        except Exception as exc:
            logger.error("Generic summarize error for article %d: %s", article_id, exc, exc_info=True)
```

- [ ] **Step 8: `HOOK_SECTION`定義を削除する**

`summarizer.py`内、以下のブロック（目印。グループ名検出の直後）:

```python
    # ── グループ名検出 ─────────────────────────────────────────────────────
    group_name = _detect_group_name(feed_source, title)
    group_hint = f"・「{group_name}」の名前を自然に含めること" if group_name else ""

    # ── 共通ブロック ───────────────────────────────────────────────────────
    HOOK_SECTION = (
        f"━━ 冒頭フック（厳守・最重要） ━━\n"
        f"今回の冒頭フック: 「{selected_hook}」\n"
        f"このフックで投稿文を必ず始めること。フックより前に何も置かない。\n"
        f"記事の内容にどうしても合わない場合のみ自分でフックを考えてよい。\n"
        f"ただし必ずフックで始めること。"
    )

    STRUCTURE_SECTION = (
```

これを以下に置き換える:

```python
    # ── グループ名検出 ─────────────────────────────────────────────────────
    group_name = _detect_group_name(feed_source, title)
    group_hint = f"・「{group_name}」の名前を自然に含めること" if group_name else ""

    # ── 共通ブロック ───────────────────────────────────────────────────────
    STRUCTURE_SECTION = (
```

- [ ] **Step 9: Step1プロンプト4パターンから`HOOK_SECTION`注入を削除する**

video用（目印）:

```python
            f"【動画タイトル】{title}\n\n"
            f"{HOOK_SECTION}\n\n"
            f"━━ 出力ルール ━━\n"
```

これを以下に置き換える:

```python
            f"【動画タイトル】{title}\n\n"
            f"━━ 出力ルール ━━\n"
```

youtube用（目印）:

```python
            f"【動画情報】\nタイトル: {title}\n{article_body[:1000]}\n\n"
            f"{HOOK_SECTION}\n\n"
            f"{STRUCTURE_SECTION}\n\n"
```

これを以下に置き換える:

```python
            f"【動画情報】\nタイトル: {title}\n{article_body[:1000]}\n\n"
            f"{STRUCTURE_SECTION}\n\n"
```

ranking用（目印）:

```python
            f"このランキングを自分が直接見つけた情報として書く。「〜と発表された」など伝聞表現は一切不可。\n\n"
            f"{HOOK_SECTION}\n\n"
            f"━━ 出力フォーマット ━━\n"
```

これを以下に置き換える:

```python
            f"このランキングを自分が直接見つけた情報として書く。「〜と発表された」など伝聞表現は一切不可。\n\n"
            f"━━ 出力フォーマット ━━\n"
```

デフォルト用（目印）:

```python
            f"【情報】\nタイトル: {title}\n{article_body[:2000]}\n\n"
            f"{HOOK_SECTION}\n\n"
            f"{STRUCTURE_SECTION}\n\n"
```

これを以下に置き換える:

```python
            f"【情報】\nタイトル: {title}\n{article_body[:2000]}\n\n"
            f"{STRUCTURE_SECTION}\n\n"
```

- [ ] **Step 10: Step2プロンプト2パターンから「フックを変えないこと」指示を削除する**

video用のstep2_base（目印）:

```python
            step2_base = (
                "この文章から余計な説明を全部削って、感情だけ残してください。\n"
                "【厳守】1行目のフックフレーズは絶対に変えないこと。そのまま残す。\n"
                "一言で言い切る。フック+感情の一言だけ。\n"
```

これを以下に置き換える:

```python
            step2_base = (
                "この文章から余計な説明を全部削って、感情だけ残してください。\n"
                "一言で言い切る。フック+感情の一言だけ。\n"
```

デフォルトのstep2_base（目印）:

```python
            step2_base = (
                "この文章を25歳の日本人女性が友達にLINEで送るメッセージに変換してください。\n"
                "【厳守】1行目のフックフレーズは絶対に変えないこと。そのまま残す。\n"
                "・説明文を感情に変える\n"
```

これを以下に置き換える:

```python
            step2_base = (
                "この文章を25歳の日本人女性が友達にLINEで送るメッセージに変換してください。\n"
                "・説明文を感情に変える\n"
```

- [ ] **Step 11: 最終アセンブリでフックを連結する**

`summarizer.py`内、以下のブロック（目印。Step2リトライループの直後）:

```python
        else:
            summary_text = summary_text[:body_max - 1] + "…"
            logger.warning("再生成上限到達、強制切り詰め: article=%d", article_id)

        # ── DB保存（ハッシュタグ・URLなし） ──────────────────────────────────
        post_text = summary_text
```

これを以下に置き換える:

```python
        else:
            summary_text = summary_text[:body_max - 1] + "…"
            logger.warning("再生成上限到達、強制切り詰め: article=%d", article_id)

        # ── DB保存（ハッシュタグ・URLなし） ──────────────────────────────────
        post_text = _attach_hook(hook, summary_text, body_max)
```

- [ ] **Step 12: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile summarizer.py
```
Expected: エラーなし。

- [ ] **Step 13: `_KPOP_HOOKS`・`HOOK_SECTION`・`selected_hook`への参照が残っていないことを確認する**

```bash
grep -n "_KPOP_HOOKS\|HOOK_SECTION\|selected_hook\|all_hooks" summarizer.py
```
Expected: 出力なし（該当なし）。

- [ ] **Step 14: `_get_next_hook`/`_attach_hook`を単体で確認する**

```bash
venv/Scripts/python.exe -c "
from app import app
from database import Hook, db
from summarizer import _get_next_hook, _attach_hook

with app.app_context():
    hooks_before = [(h.id, h.phrase, h.last_used_at) for h in Hook.query.filter_by(account_id=1).order_by(Hook.display_order.asc()).all()]
    print('投入直後（last_used_at全件None想定）:', hooks_before[:3])

# 1回目: 未使用の中で最もdisplay_orderが若いものが選ばれるはず
hook1 = _get_next_hook(app, 1)
print('1回目に選ばれたフック:', hook1)
hook2 = _get_next_hook(app, 1)
print('2回目に選ばれたフック（1回目と違うはず）:', hook2)

print('連結（超過なし）:', repr(_attach_hook('待って、これやばい。', 'テスト本文です', 100)))
print('連結（超過あり、切り詰め）:', repr(_attach_hook('待って、これやばい。', 'あ' * 20, 15)))
print('フックなし:', repr(_attach_hook(None, '本文そのまま', 100)))

with app.app_context():
    h = db.session.get(Hook, hooks_before[0][0])
    print('last_used_at更新確認:', h.last_used_at is not None)
"
```
Expected: 1回目・2回目で異なるフレーズが選ばれる（ローテーションが機能している）。連結結果は`フック+本文`（超過時は`…`付き15文字）。フックなしの場合は本文そのまま。`last_used_at`が更新されている。

- [ ] **Step 15: 実際に3つの生成経路で投稿文が生成され、先頭にフックが付くことを確認する**

（Anthropic APIキーが設定済み・クレジット残高がある場合のみ有効な検証。エラーになる場合はStep12〜14のオフライン確認のみで良しとする）

```bash
venv/Scripts/python.exe -c "
from app import app
from database import Article, db
from summarizer import summarize_article
import json

with app.app_context():
    art = Article(
        feed_source='テスト', title='BLACKPINKの新曲が話題',
        url='https://example.com/hook-integration-test',
        raw_content='BLACKPINKが新曲をリリースした。', status='pending', account_id=1,
    )
    db.session.add(art)
    db.session.commit()
    article_id = art.id

ok = summarize_article(app, article_id)
print('生成成功:', ok)

with app.app_context():
    a = db.session.get(Article, article_id)
    print('投稿文:', repr(a.summary))
    db.session.delete(a)
    db.session.commit()
"
```
Expected: `生成成功: True`、`投稿文`の先頭がフック管理画面で「次に使う」表示されていたフレーズと一致する。

- [ ] **Step 16: Commit**

```bash
git add summarizer.py
git commit -m "$(cat <<'EOF'
feat: 投稿文生成をDBベースのフックローテーションに統合する

_KPOP_HOOKSのランダム選択・AIプロンプトへの注入方式（HOOK_SECTION）
を廃止し、Hookテーブルからローテーションで取得したフレーズを生成済み
本文の先頭に機械的に連結する方式に置き換えた。KPOP・ガチャ・AI無効時
ルールベースの3経路すべてで共通のヘルパー（_get_next_hook /
_attach_hook）を通す。STRUCTURE_SECTION/COMMON_RULES内の一般的な
プロンプト文言は変更していない。
EOF
)"
```

---

## 完了確認

全タスク完了後、以下を満たしていることを確認する:

- [ ] `Hook`テーブルが作成され、KPOP・ガチャ両アカウントにデフォルトフックが10個ずつ投入されている
- [ ] `/hooks/1`・`/hooks/2`それぞれでフックの一覧表示・追加・編集・削除ができる
- [ ] 一覧で「次に使う」フックがバッジで示され、最終使用日時が表示される
- [ ] サイドバー（デスクトップ・モバイル）の「フック管理」リンクが選択中アカウントに応じて`/hooks/1`または`/hooks/2`に切り替わる
- [ ] 「要約を生成」実行時、KPOP・ガチャ・AI無効時ルールベースいずれの経路でも生成済み投稿文の先頭にローテーション順のフックが付加される
- [ ] フックを使い切ると最も古く使われたものから再度使われる（ローテーションが一巡する）
- [ ] `_KPOP_HOOKS`辞書・`HOOK_SECTION`・`selected_hook`への参照がコード上に残っていない
- [ ] `STRUCTURE_SECTION`・`COMMON_RULES`内の一般的なプロンプト文言（「フックで引き込む」等）は変更されていない
