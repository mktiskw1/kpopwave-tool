# ガチャ沼の住人アカウント向けRSS収集 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `account_id=2`（アカウント名「ガチャ沼の住人」）向けにガチャパラRSSフィード（`https://gachapara.jp/feed/`）を収集源として追加し、KPOPアカウント（account_id=1）と同じRSS収集→承認待ち→投稿の流れで使えるようにする。

**Architecture:** `ThreadsAccount`に`content_topic`列を追加し、これが設定されているアカウントは「非KPOP汎用フロー」（キーワードフィルタなし・汎用AI関連度判定・汎用投稿文プロンプト）を、未設定（空文字/NULL）のアカウントは既存のKPOP専用フローをそのまま使う、という1つのフラグでの分岐方式を取る。フィード設定JSON（`rss_feeds`）に`account_id`を追加し、収集記事にアカウントを紐付ける。

**Tech Stack:** Python 3.x / Flask / SQLAlchemy / SQLite / feedparser / anthropic SDK（Claude Haiku）

## Global Constraints

- DBファイルは`instance/rock_metal.db`（SQLite）。既存DBへの列追加は必ず`app.py`の`_migrate_db()`に`ALTER TABLE ADD COLUMN`で追記する（SQLiteは`ADD COLUMN`以外のALTER構文に対応していないため）
- コメントは原則書かない（WHYが非自明な場合のみ1行）
- 既存ファイルを編集する（新規ファイル作成は最後の手段）。今回は既存ファイル（`database.py`, `app.py`, `rss_collector.py`, `summarizer.py`, `templates/settings.html`）のみを編集し、新規ファイルは作らない
- 必要な変更だけ行う。リファクタリング・クリーンアップは不要
- Shellコマンドの実行環境はWindows。Bashツールを使う場合はGit Bash構文（`$VAR`、`&&`可）、PowerShellツールを使う場合はPowerShell構文（`$env:VAR`、`&&`不可）に注意する
- JSはフォームsubmitよりfetch APIを使う（画面遷移なしのUX）。ただし設定画面全体の保存フォーム（`<form method="post">`）自体は既存通りの通常submitのままでよい（今回変更しない）
- 投稿文の文字数上限: 記事=150文字（`BODY_MAX_ARTICLE`）。動画=50文字（今回のRSS収集は動画ではないため対象外）
- このプロジェクトにはpytest等のテストフレームワークが存在しない。各タスクの検証は、実際にFlaskアプリを起動し、`venv/Scripts/python.exe -c "..."`によるDB直接確認や`curl`によるHTTPリクエストで動作確認する（このセッション内で確立された検証方法）
- `venv/Scripts/python.exe`が仮想環境のPythonインタプリタ

---

## ファイル構成

| ファイル | 変更内容 |
|---|---|
| `database.py` | `ThreadsAccount`に`content_topic`列を追加 |
| `app.py` | `_migrate_db()`にALTER TABLE追記、`/accounts/<id>/content-topic`ルート追加、`/settings` POSTハンドラのフィード再構築ロジックに`account_id`/`lang`保持を追加 |
| `rss_collector.py` | アカウント別`content_topic`解決、キーワードフィルタのアカウント別スキップ、AI関連度判定プロンプトの汎用化、Article作成時の`account_id`セット |
| `summarizer.py` | `content_topic`が設定されたアカウントの記事向けに汎用シンプル生成パスを追加 |
| `templates/settings.html` | アカウント管理テーブルに`content_topic`表示・インライン編集欄、フィードフォームにアカウント選択`<select>`とlang保持用hidden inputを追加 |

---

### Task 1: DBスキーマ — `ThreadsAccount.content_topic`列を追加

**Files:**
- Modify: `database.py:59-69`（`ThreadsAccount`クラス）
- Modify: `app.py:74-135`（`_migrate_db()`）

**Interfaces:**
- Produces: `ThreadsAccount.content_topic`（`str | None`）— 以降の全タスクがこの属性を読み書きする

- [ ] **Step 1: `database.py`の`ThreadsAccount`に列を追加**

`database.py:59-69`の`ThreadsAccount`クラスを以下のように変更する（`is_active`の直後に追加）:

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
```

- [ ] **Step 2: `app.py`の`_migrate_db()`にALTER TABLEを追記**

`app.py:74-102`（`_migrate_db()`冒頭の`articles`テーブル用ALTER TABLEブロックの直後、`# threads_accounts テーブル: 既存の単一アカウント設定を初期レコードとして移行`という既存コメント行の**直前**）に、新しいブロックを挿入する。

挿入位置の目印（`app.py:101-103`）:
```python
                conn.commit()
                logger.info("DB migration: articles.%s added", col)

    # threads_accounts テーブル: 既存の単一アカウント設定を初期レコードとして移行
```

この`# threads_accounts テーブル: 既存の単一アカウント設定を初期レコードとして移行`の直前に以下を挿入する:

```python
    # threads_accounts テーブル: content_topic 列
    existing_accounts_cols = {c["name"] for c in inspector.get_columns("threads_accounts")}
    account_cols = [
        ("content_topic", "VARCHAR(200)"),
    ]
    with db.engine.connect() as conn:
        for col, typedef in account_cols:
            if col not in existing_accounts_cols:
                conn.execute(text(f"ALTER TABLE threads_accounts ADD COLUMN {col} {typedef}"))
                conn.commit()
                logger.info("DB migration: threads_accounts.%s added", col)

```

このブロックを、続く`if ThreadsAccount.query.count() == 0:`（既存アカウント初期化ロジック）より**前**に置くこと。理由: `ThreadsAccount.query.filter_by(...)`はSQLAlchemyのORM経由で`content_topic`列を含む全カラムをSELECTするため、列が存在しない状態でこのクエリが先に走ると既存DBで例外になる。

- [ ] **Step 3: マイグレーションが正常に動くことを確認する**

```bash
venv/Scripts/python.exe -c "
from app import create_app
from database import ThreadsAccount
app = create_app()
with app.app_context():
    for a in ThreadsAccount.query.all():
        print(a.id, repr(a.account_label), repr(a.content_topic))
"
```

Expected: エラーなく全アカウントが出力され、`content_topic`は全件`None`。

- [ ] **Step 4: Commit**

```bash
git add database.py app.py
git commit -m "$(cat <<'EOF'
feat: ThreadsAccountにcontent_topic列を追加

非KPOPアカウント向けのRSS収集・投稿文生成の分岐に使うフラグ兼
プロンプト材料。未設定なら既存のKPOP専用フローを継続する。
EOF
)"
```

---

### Task 2: `content_topic`編集API・設定画面UI

**Files:**
- Modify: `app.py:1173-1181`（`rename_account`ルートの直後）
- Modify: `templates/settings.html:136-182`（アカウント管理テーブル）
- Modify: `templates/settings.html:378-411`（アカウント名編集JS群の直後）

**Interfaces:**
- Consumes: `ThreadsAccount.content_topic`（Task 1）
- Produces: `POST /accounts/<id>/content-topic` エンドポイント（JSON `{ok: bool, content_topic: str}`）

- [ ] **Step 1: `app.py`に新規ルートを追加**

`app.py`の`rename_account`ルート（`app.py:1173-1181`）の直後に挿入する。目印:

```python
@app.route("/accounts/<int:id>/rename", methods=["POST"])
def rename_account(id):
    account = ThreadsAccount.query.get_or_404(id)
    label = (request.form.get("account_label") or "").strip()
    if not label:
        return jsonify({"ok": False, "error": "アカウント名を入力してください"}), 400
    account.account_label = label
    db.session.commit()
    return jsonify({"ok": True, "account_label": account.account_label})
```

この直後（`@app.route("/accounts/<int:id>/toggle-active"...)`より前）に追加:

```python
@app.route("/accounts/<int:id>/content-topic", methods=["POST"])
def update_account_content_topic(id):
    account = ThreadsAccount.query.get_or_404(id)
    topic = (request.form.get("content_topic") or "").strip()
    account.content_topic = topic or None
    db.session.commit()
    return jsonify({"ok": True, "content_topic": account.content_topic or ""})
```

- [ ] **Step 2: `templates/settings.html`のアカウント管理テーブルに列を追加**

`templates/settings.html:136-182`を以下の内容に置き換える（既存の`account_label`列はそのまま、`threads_user_id`列の前に`content_topic`列を追加）:

```html
        <div class="table-responsive mb-3">
          <table class="table table-sm align-middle mb-0" style="font-size:.85rem">
            <thead>
              <tr>
                <th>アカウント名</th>
                <th>コンテンツトピック</th>
                <th>Threads ユーザーID</th>
                <th>状態</th>
                <th class="text-end">操作</th>
              </tr>
            </thead>
            <tbody>
              {% for acc in accounts %}
              <tr>
                <td class="fw-semibold">
                  <span id="acc-label-display-{{ acc.id }}">{{ acc.account_label }}</span>
                  <span id="acc-label-edit-{{ acc.id }}" style="display:none">
                    <input type="text" id="acc-label-input-{{ acc.id }}" class="form-control form-control-sm d-inline-block"
                           value="{{ acc.account_label }}" style="width:180px;font-size:.85rem">
                    <button type="button" class="btn btn-sm py-0 px-1 btn-success" style="font-size:.72rem"
                            onclick="saveAccountLabel({{ acc.id }})">✓</button>
                    <button type="button" class="btn btn-sm py-0 px-1 btn-outline-secondary" style="font-size:.72rem"
                            onclick="cancelEditAccountLabel({{ acc.id }})">✕</button>
                  </span>
                  <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-2 ms-1" style="font-size:.72rem"
                          onclick="startEditAccountLabel({{ acc.id }})">編集</button>
                </td>
                <td>
                  <span id="acc-topic-display-{{ acc.id }}" style="color:var(--text-muted)">{{ acc.content_topic or "（KPOP専用ロジック）" }}</span>
                  <span id="acc-topic-edit-{{ acc.id }}" style="display:none">
                    <input type="text" id="acc-topic-input-{{ acc.id }}" class="form-control form-control-sm d-inline-block"
                           value="{{ acc.content_topic or '' }}" placeholder="例: ガチャガチャ・カプセルトイに関する記事" style="width:260px;font-size:.85rem">
                    <button type="button" class="btn btn-sm py-0 px-1 btn-success" style="font-size:.72rem"
                            onclick="saveContentTopic({{ acc.id }})">✓</button>
                    <button type="button" class="btn btn-sm py-0 px-1 btn-outline-secondary" style="font-size:.72rem"
                            onclick="cancelEditContentTopic({{ acc.id }})">✕</button>
                  </span>
                  <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-2 ms-1" style="font-size:.72rem"
                          onclick="startEditContentTopic({{ acc.id }})">編集</button>
                </td>
                <td class="text-muted">{{ acc.threads_user_id or '未設定' }}</td>
                <td>
                  {% if acc.is_active %}
                    <span class="badge bg-success">有効</span>
                  {% else %}
                    <span class="badge bg-secondary">無効</span>
                  {% endif %}
                </td>
                <td class="text-end">
                  <form method="post" action="{{ url_for('toggle_account_active', id=acc.id) }}" class="d-inline">
                    <button type="submit" class="btn btn-sm btn-outline-secondary py-0 px-2" style="font-size:.75rem">
                      {% if acc.is_active %}無効化{% else %}有効化{% endif %}
                    </button>
                  </form>
                </td>
              </tr>
              {% else %}
              <tr><td colspan="5" class="text-muted text-center py-3">登録済みアカウントがありません</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
```

（変更点: `<th>`が4個→5個に、`colspan="4"`→`colspan="5"`に、`account_label`セル内の「編集」ボタンをセル内固定表示に変更、新しい`content_topic`セルを追加）

- [ ] **Step 3: JSを追加**

`templates/settings.html`の`saveAccountLabel`関数（`templates/settings.html:389-411`)の直後、`function addChannel()`より前に追加:

```javascript
function startEditContentTopic(id) {
  document.getElementById("acc-topic-display-" + id).style.display = "none";
  document.getElementById("acc-topic-edit-" + id).style.display = "inline-block";
  const input = document.getElementById("acc-topic-input-" + id);
  input.focus();
  input.select();
}
function cancelEditContentTopic(id) {
  document.getElementById("acc-topic-edit-" + id).style.display = "none";
  document.getElementById("acc-topic-display-" + id).style.display = "";
}
function saveContentTopic(id) {
  const input = document.getElementById("acc-topic-input-" + id);
  const topic = input.value.trim();
  fetch(`/accounts/${id}/content-topic`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ content_topic: topic }).toString(),
  })
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        document.getElementById("acc-topic-display-" + id).textContent = data.content_topic || "（KPOP専用ロジック）";
        cancelEditContentTopic(id);
      } else {
        alert(data.error || "更新に失敗しました");
      }
    })
    .catch(() => alert("更新に失敗しました"));
}
```

- [ ] **Step 4: 構文チェックと動作確認**

```bash
venv/Scripts/python.exe -m py_compile app.py
```
Expected: エラーなし（出力なし）。

アプリを起動して確認する:
```bash
venv/Scripts/python.exe app.py
```
(バックグラウンド実行。起動後に以下を実行)

```bash
curl -s -X POST http://localhost:5000/accounts/2/content-topic -d "content_topic=テストトピック"
```
Expected: `{"content_topic":"テストトピック","ok":true}`

```bash
curl -s http://localhost:5000/settings | grep -o 'acc-topic-display-2'
```
Expected: `acc-topic-display-2`が出力される。

確認後、テスト値を空に戻す:
```bash
curl -s -X POST http://localhost:5000/accounts/2/content-topic -d "content_topic="
```

アプリを停止する（バックグラウンドタスクIDを`TaskStop`で停止するか、起動していたプロセスを終了する）。

- [ ] **Step 5: Commit**

```bash
git add app.py templates/settings.html
git commit -m "$(cat <<'EOF'
feat: アカウント管理画面でcontent_topicを編集可能にする

非KPOPアカウント向けのRSS収集・投稿文生成分岐に使うトピック説明文を
設定画面から編集できるようにした。新規ルート POST /accounts/<id>/content-topic。
EOF
)"
```

---

### Task 3: RSS収集のアカウント別分岐（`rss_collector.py`）

**Files:**
- Modify: `rss_collector.py`（全体、特に`collect_articles()`と`_ai_judge_titles()`/`_ai_judge_batched()`）

**Interfaces:**
- Consumes: `ThreadsAccount.content_topic`（Task 1）、フィードJSONの`account_id`フィールド（本タスクで導入、未指定時は`1`扱い）
- Produces: `Article.account_id`が収集時にセットされる。`_ai_judge_titles(titles, api_key, topic_label="女性KPOPアイドル")` — `topic_label`引数を追加

- [ ] **Step 1: importに`ThreadsAccount`を追加**

`rss_collector.py:9`を変更:

```python
from database import Article, Setting, ThreadsAccount, db
```

- [ ] **Step 2: `_ai_judge_titles`に`topic_label`引数を追加**

`rss_collector.py:105-152`の`_ai_judge_titles`関数を以下に置き換える:

```python
def _ai_judge_titles(titles: list, api_key: str, topic_label: str = "女性KPOPアイドル") -> list:
    """
    タイトルリストをClaude Haikuに送り、指定トピックに関連するインデックス(0始まり)を返す。
    APIエラー・キー未設定時はフォールバックとして全インデックスを返す。
    """
    if not titles:
        return []
    if not api_key:
        logger.warning("AI判定スキップ: Anthropic APIキー未設定")
        return list(range(len(titles)))

    import anthropic

    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    if topic_label == "女性KPOPアイドル":
        prompt = (
            "以下の記事タイトルのうち、女性KPOPアイドル（グループ・ソロ）に関する"
            "ニュース・レビュー・インタビュー・カムバック・コンサート情報のものを選び、"
            "番号をカンマ区切りで返してください。\n"
            "除外: 男性アイドル・韓国ドラマ・映画・スポーツ・政治・一般音楽\n\n"
            f"{numbered}\n\n"
            "回答は番号のみ（例: 1,3,5）。対象なし→「なし」"
        )
    else:
        prompt = (
            f"以下の記事タイトルのうち、{topic_label}に関するものを選び、"
            "番号をカンマ区切りで返してください。\n\n"
            f"{numbered}\n\n"
            "回答は番号のみ（例: 1,3,5）。対象なし→「なし」"
        )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        response = msg.content[0].text.strip()
        logger.info("AI判定レスポンス: [%s]", response)

        if not response or response == "なし":
            return []

        approved = []
        for part in response.replace("、", ",").split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(titles):
                    approved.append(idx)
        return approved

    except Exception as exc:
        logger.error("AI判定エラー（フォールバック: 全件通過）: %s", exc)
        return list(range(len(titles)))
```

- [ ] **Step 3: `_ai_judge_batched`に`topic_label`引数を追加**

`rss_collector.py:155-165`の`_ai_judge_batched`関数を以下に置き換える:

```python
def _ai_judge_batched(titles: list, api_key: str, topic_label: str = "女性KPOPアイドル") -> list:
    """AI_BATCH_SIZE を超える場合は分割して判定する。"""
    if len(titles) <= AI_BATCH_SIZE:
        return _ai_judge_titles(titles, api_key, topic_label=topic_label)

    approved = []
    for offset in range(0, len(titles), AI_BATCH_SIZE):
        batch = titles[offset:offset + AI_BATCH_SIZE]
        indices = _ai_judge_titles(batch, api_key, topic_label=topic_label)
        approved.extend(i + offset for i in indices)
    return approved
```

- [ ] **Step 4: `collect_articles`をアカウント別分岐対応にする**

`rss_collector.py:188-306`の`collect_articles`関数全体を以下に置き換える:

```python
def collect_articles(app) -> int:
    """全RSSフィードから新着記事を取得してDBに保存する。新記事数を返す。"""
    feeds = get_feed_list(app)
    with app.app_context():
        api_key = Setting.get("anthropic_api_key", "") or os.getenv("ANTHROPIC_API_KEY", "")
        topic_by_account = {
            acc.id: (acc.content_topic or "").strip()
            for acc in ThreadsAccount.query.all()
        }

    new_count = 0
    seen_urls = set()  # 今回の収集内での重複防止

    for feed_info in feeds:
        url  = feed_info.get("url", "") if isinstance(feed_info, dict) else str(feed_info)
        name = feed_info.get("name", url) if isinstance(feed_info, dict) else url
        is_ja = _is_japanese_feed(feed_info)
        account_id = feed_info.get("account_id", 1) if isinstance(feed_info, dict) else 1
        content_topic = topic_by_account.get(account_id, "")
        if not url:
            continue

        try:
            parsed = feedparser.parse(url, request_headers={"User-Agent": "KpopWaveBot/1.0"})
            entries = parsed.entries[:50]
        except Exception as exc:
            logger.error("Feed fetch error [%s]: %s", name, exc)
            continue

        skipped_date = skipped_kw = skipped_dup = skipped_ai = added = 0
        candidates = []  # (entry_data_dict) キーワード通過済み候補

        with app.app_context():
            for entry in entries:
                article_url = entry.get("link", "").strip()
                if not article_url:
                    continue

                # 公開日時
                published_at = None
                if getattr(entry, "published_parsed", None):
                    try:
                        published_at = datetime(*entry.published_parsed[:6])
                    except Exception:
                        pass

                # ── フィルター1: 日付 ──────────────────────────────────────
                if not _is_recent(published_at):
                    skipped_date += 1
                    continue

                # コンテンツ取得
                content = ""
                if hasattr(entry, "content") and entry.content:
                    content = entry.content[0].get("value", "")
                elif hasattr(entry, "summary"):
                    content = entry.summary
                elif hasattr(entry, "description"):
                    content = entry.description
                plain_content = _strip_html(content)
                title = (entry.get("title") or "")

                # content_topic 設定済みアカウント（非KPOP）のフィードは
                # 女性KPOPキーワード辞書と無関係なためキーワードフィルタを丸ごとスキップする。
                # KPOPアカウント（content_topic未設定）は既存通り、日本語フィードのみスキップ。
                if not content_topic and not is_ja:
                    # ── フィルター2: 除外キーワード ──────────────────────
                    if _check_excluded(title, plain_content):
                        skipped_kw += 1
                        continue

                    # ── フィルター3: 女性KPOPキーワード ──────────────────
                    if not _check_female_kpop(title, plain_content):
                        skipped_kw += 1
                        continue

                # ── 重複チェック（DB + 今回収集分） ───────────────────────
                if article_url in seen_urls or Article.query.filter_by(url=article_url).first():
                    skipped_dup += 1
                    continue

                candidates.append({
                    "title":        title,
                    "url":          article_url,
                    "published_at": published_at,
                    "raw_content":  plain_content[:5000],
                    "feed_source":  name,
                    "lang":         "ja" if is_ja else "en",
                    "account_id":   account_id,
                })

        if not candidates:
            logger.info(
                "[%s] 追加:0 除外(日付):%d 除外(KW):%d 重複:%d",
                name, skipped_date, skipped_kw, skipped_dup,
            )
            continue

        # ── フィルター4: Claude Haiku AI判定（タイトルのみ送信） ──────────
        titles_only = [c["title"] for c in candidates]
        topic_label = content_topic or "女性KPOPアイドル"
        approved_indices = _ai_judge_batched(titles_only, api_key, topic_label=topic_label)
        skipped_ai = len(candidates) - len(approved_indices)

        with app.app_context():
            for idx in approved_indices:
                c = candidates[idx]
                article = Article(
                    feed_source=c["feed_source"],
                    title=c["title"][:500] or "No Title",
                    url=c["url"],
                    published_at=c["published_at"],
                    raw_content=c["raw_content"],
                    status="pending",
                    account_id=c["account_id"],
                )
                db.session.add(article)
                seen_urls.add(c["url"])
                added += 1
                new_count += 1
            db.session.commit()

        logger.info(
            "[%s][%s] 追加:%d 除外(日付):%d 除外(KW):%d 除外(AI):%d 重複:%d",
            name, "ja" if is_ja else "en", added, skipped_date, skipped_kw, skipped_ai, skipped_dup,
        )

    logger.info("収集完了 — 新規追加合計: %d 件", new_count)
    return new_count
```

- [ ] **Step 5: 構文チェックと単体動作確認**

```bash
venv/Scripts/python.exe -m py_compile rss_collector.py
```
Expected: エラーなし。

```bash
venv/Scripts/python.exe -c "
from app import create_app
from rss_collector import _ai_judge_titles
app = create_app()
with app.app_context():
    from database import Setting
    key = Setting.get('anthropic_api_key', '')
print('APIキー設定済み:', bool(key))
result = _ai_judge_titles(['テスト記事タイトル'], '', topic_label='ガチャガチャ')
print('APIキー空の場合のフォールバック（全件通過）:', result)
"
```
Expected: `result`は`[0]`（APIキーなし時は全件通過のフォールバック）。

- [ ] **Step 6: Commit**

```bash
git add rss_collector.py
git commit -m "$(cat <<'EOF'
feat: RSS収集をアカウント別content_topicで分岐対応にする

content_topic設定済みアカウントのフィードはKPOPキーワードフィルタを
スキップし、AI関連度判定プロンプトもトピックに応じて汎用化。
収集記事にaccount_idをセットするようにした（従来は未設定だった）。
EOF
)"
```

---

### Task 4: 設定画面のフィードフォームにアカウント選択を追加

**Files:**
- Modify: `app.py:871-901`（`settings()`のPOSTハンドラ、フィード再構築ロジック）
- Modify: `templates/settings.html:268-297`（RSSフィードカード）
- Modify: `templates/settings.html:362-376`（`addFeed()`関数）

**Interfaces:**
- Consumes: `accounts`（`settings()`ルートが既に`render_template`に渡している`ThreadsAccount`一覧）
- Produces: `rss_feeds` Setting値の各フィードエントリに`account_id`（int）と`lang`（保存されていれば維持）が入る

- [ ] **Step 1: `app.py`のフィード再構築ロジックを修正**

`app.py:883-887`を以下に置き換える。目印（`app.py:871-901`の`settings()`関数内）:

```python
        feed_names = request.form.getlist("feed_name")
        feed_urls = request.form.getlist("feed_url")
        feeds = [{"name": n.strip(), "url": u.strip()}
                 for n, u in zip(feed_names, feed_urls) if u.strip()]
        Setting.set("rss_feeds", json.dumps(feeds))
```

置き換え後:

```python
        feed_names = request.form.getlist("feed_name")
        feed_urls = request.form.getlist("feed_url")
        feed_langs = request.form.getlist("feed_lang")
        feed_account_ids = request.form.getlist("feed_account_id")
        feeds = []
        for i, (n, u) in enumerate(zip(feed_names, feed_urls)):
            if not u.strip():
                continue
            feed = {"name": n.strip(), "url": u.strip()}
            lang = feed_langs[i].strip() if i < len(feed_langs) else ""
            if lang:
                feed["lang"] = lang
            try:
                acc_id = int(feed_account_ids[i]) if i < len(feed_account_ids) and feed_account_ids[i] else 1
            except ValueError:
                acc_id = 1
            feed["account_id"] = acc_id
            feeds.append(feed)
        Setting.set("rss_feeds", json.dumps(feeds))
```

- [ ] **Step 2: `templates/settings.html`のRSSフィードカードにアカウント選択とlang保持用hiddenを追加**

`templates/settings.html:278-291`を以下に置き換える:

```html
        <div id="feeds-container">
          {% for feed in settings.rss_feeds %}
          <div class="input-group mb-2 feed-row">
            <input type="text" class="form-control" name="feed_name"
                   placeholder="名前" value="{{ feed.name }}" style="max-width:110px;font-size:.82rem">
            <input type="text" class="form-control" name="feed_url"
                   placeholder="RSS URL" value="{{ feed.url }}" style="font-size:.82rem">
            <select class="form-select form-select-sm" name="feed_account_id" style="max-width:150px;font-size:.78rem">
              {% for acc in accounts %}
              <option value="{{ acc.id }}" {% if (feed.account_id or 1) == acc.id %}selected{% endif %}>{{ acc.account_label }}</option>
              {% endfor %}
            </select>
            <input type="hidden" name="feed_lang" value="{{ feed.lang or '' }}">
            <button type="button" class="btn btn-outline-danger btn-sm px-2"
                    onclick="this.closest('.feed-row').remove()">
              <i class="bi bi-trash3"></i>
            </button>
          </div>
          {% endfor %}
        </div>
```

- [ ] **Step 3: `addFeed()`のJSにアカウント選択を追加**

`templates/settings.html:362-376`を以下に置き換える:

```javascript
const FEED_ACCOUNT_OPTIONS = `{% for acc in accounts %}<option value="{{ acc.id }}">{{ acc.account_label }}</option>{% endfor %}`;

function addFeed() {
  const c = document.getElementById('feeds-container');
  const div = document.createElement('div');
  div.className = 'input-group mb-2 feed-row';
  div.innerHTML = `
    <input type="text" class="form-control" name="feed_name" placeholder="名前" style="max-width:110px;font-size:.82rem">
    <input type="text" class="form-control" name="feed_url" placeholder="RSS URL" style="font-size:.82rem">
    <select class="form-select form-select-sm" name="feed_account_id" style="max-width:150px;font-size:.78rem">${FEED_ACCOUNT_OPTIONS}</select>
    <input type="hidden" name="feed_lang" value="">
    <button type="button" class="btn btn-outline-danger btn-sm px-2" onclick="this.closest('.feed-row').remove()">
      <i class="bi bi-trash3"></i>
    </button>`;
  c.appendChild(div);
  div.querySelector('input[name=feed_name]').focus();
}
```

- [ ] **Step 4: 構文チェックと動作確認**

```bash
venv/Scripts/python.exe -m py_compile app.py
```
Expected: エラーなし。

アプリを起動し、設定画面が正しくレンダリングされることと、フィード保存後に`account_id`が保持されることを確認する:

```bash
venv/Scripts/python.exe app.py
```
（バックグラウンド実行、起動後に以下）

```bash
curl -s http://localhost:5000/settings | grep -c 'name="feed_account_id"'
```
Expected: `settings.rss_feeds`の件数と同じ数以上（0件でもエラーにならないこと）。

- [ ] **Step 5: Commit**

```bash
git add app.py templates/settings.html
git commit -m "$(cat <<'EOF'
feat: RSSフィード設定にアカウント選択を追加

フィードごとにaccount_idを紐付けられるようにした。設定保存時に
langフィールドが失われていた既存の抜け漏れもあわせて修正。
EOF
)"
```

---

### Task 5: 投稿文生成の汎用シンプルパス（`summarizer.py`）

**Files:**
- Modify: `summarizer.py:12`（import）
- Modify: `summarizer.py:463-527`付近（`summarize_article`のコンテンツ取得部分の直後に分岐を挿入）

**Interfaces:**
- Consumes: `ThreadsAccount.content_topic`（Task 1）、`Article.account_id`
- Produces: `content_topic`設定済みアカウントの記事は、KPOP専用プロンプトを経由せず`Article.summary`が生成される

- [ ] **Step 1: importに`ThreadsAccount`を追加**

`summarizer.py:12`を変更:

```python
from database import Article, BuzzPost, Setting, ThreadsAccount, db
```

- [ ] **Step 2: 記事情報取得部分で`content_topic`を解決する**

`summarizer.py:463-477`（目印）:

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

    is_video_post = (content_type == "video")
    body_max = BODY_MAX_VIDEO if is_video_post else BODY_MAX_ARTICLE
```

を以下に置き換える:

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

- [ ] **Step 3: コンテンツ取得直後に汎用生成パスの分岐を挿入**

`summarizer.py:517-529`（目印。「コンテンツ取得」ブロックと「グループ名検出」ブロックの間）:

```python
    # ── コンテンツ取得 ─────────────────────────────────────────────────────
    is_youtube = "youtube.com/watch" in url or "youtu.be/" in url or "youtube.com/shorts/" in url

    if is_youtube:
        article_body   = stored_body
        article_images = [thumbnail_url] if thumbnail_url else []
        fetch_ok       = True
    else:
        fresh_body, article_images, fetch_ok = _fetch_article_page(url)
        article_body = fresh_body if fetch_ok else stored_body

    # ── グループ名検出 ─────────────────────────────────────────────────────
    group_name = _detect_group_name(feed_source, title)
```

の`article_body = fresh_body if fetch_ok else stored_body`の直後、`# ── グループ名検出 ──`の直前に、以下の分岐ブロックを挿入する:

```python
    # ── 非KPOPアカウント（content_topic設定済み）: 汎用シンプル生成パス ──────
    if content_topic:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            generic_prompt = (
                f"あなたは{content_topic}が好きな人です。"
                "以下の記事を読んで、友達にLINEで一言伝えるような自然な口語体で"
                f"感想を書いてください。{body_max}文字以内。\n"
                "絵文字なし・ハッシュタグなし・URLなし。「〜です」「〜ます」ではなく口語体で。\n"
                "伝聞表現（〜とのこと、〜と報じられている）は使わない。\n"
                "出力は投稿文のみ（前置き・説明不要）。\n\n"
                f"【記事タイトル】{title}\n【本文】{article_body[:2000]}"
            )
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
            _save_error(app, article_id, f"{type(exc).__name__}: {exc}")
            return False

        with app.app_context():
            art = db.session.get(Article, article_id)
            if art:
                art.summary       = post_text
                art.post_style    = style
                art.error_message = None
                if article_images:
                    art.image_urls = json.dumps(article_images, ensure_ascii=False)
                db.session.commit()
        logger.info(
            "Summarized(generic) article %d topic=%s (%d文字, %d images)",
            article_id, content_topic, len(post_text), len(article_images),
        )
        return True

    # ── グループ名検出 ─────────────────────────────────────────────────────
    group_name = _detect_group_name(feed_source, title)
```

- [ ] **Step 4: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile summarizer.py
```
Expected: エラーなし。

- [ ] **Step 5: 実際にガチャ記事を1件作って生成を確認する**

（Anthropic APIキーが設定済みであることが前提。未設定ならこのステップはスキップし、Step 4の構文チェックのみで良しとする）

```bash
venv/Scripts/python.exe -c "
from app import create_app
from database import Article, ThreadsAccount, db
from summarizer import summarize_article

app = create_app()
with app.app_context():
    acc = db.session.get(ThreadsAccount, 2)
    acc.content_topic = 'ガチャガチャ・カプセルトイに関する記事'
    db.session.commit()

    art = Article(
        feed_source='ガチャパラ',
        title='新作カプセルトイ「テスト商品」が発売開始',
        url='https://example.com/test-gacha-article',
        raw_content='テスト用の本文です。新作のカプセルトイが発売されました。',
        status='pending',
        account_id=2,
    )
    db.session.add(art)
    db.session.commit()
    article_id = art.id

ok = summarize_article(app, article_id)
print('生成成功:', ok)

with app.app_context():
    art = db.session.get(Article, article_id)
    print('投稿文:', repr(art.summary))
    print('文字数:', len(art.summary or ''))
    db.session.delete(art)
    db.session.commit()
"
```
Expected: `生成成功: True`、`投稿文`にKPOP用語（グループ名等）が含まれず、150文字以内の口語体の文章が出力される。

- [ ] **Step 6: Commit**

```bash
git add summarizer.py
git commit -m "$(cat <<'EOF'
feat: 非KPOPアカウント向けの汎用シンプル投稿文生成パスを追加

content_topicが設定されたアカウントの記事は、KPOP専用のフック集・
固有名詞強制ルールを使わず、記事内容を自然な口語体に変換するだけの
汎用プロンプトで1段階生成する。
EOF
)"
```

---

### Task 6: ガチャパラフィードの登録とエンドツーエンド確認

**Files:**
- なし（コード変更なし。DBへのデータ投入とエンドツーエンド検証のみ）

**Interfaces:**
- Consumes: Task 1〜5で実装した全機能

- [ ] **Step 1: account_id=2のcontent_topicとガチャパラフィードを登録する**

```bash
venv/Scripts/python.exe -c "
import json
from app import create_app
from database import Setting, ThreadsAccount, db

app = create_app()
with app.app_context():
    acc = db.session.get(ThreadsAccount, 2)
    acc.content_topic = 'ガチャガチャ・カプセルトイに関する記事'
    db.session.commit()
    print('account_id=2 content_topic:', acc.content_topic)

    feeds = json.loads(Setting.get('rss_feeds', '[]') or '[]')
    if not any(f.get('url') == 'https://gachapara.jp/feed/' for f in feeds):
        feeds.append({'name': 'ガチャパラ', 'url': 'https://gachapara.jp/feed/', 'account_id': 2})
        Setting.set('rss_feeds', json.dumps(feeds, ensure_ascii=False))
        print('ガチャパラフィードを追加しました')
    else:
        print('ガチャパラフィードは既に登録済みです')
    print(json.dumps(feeds, ensure_ascii=False, indent=2))
"
```
Expected: `account_id=2 content_topic: ガチャガチャ・カプセルトイに関する記事`、フィード一覧に`gachapara.jp`のエントリが1件だけ存在する。

- [ ] **Step 2: 実際にRSS収集を実行してアカウント紐付けを確認する**

```bash
venv/Scripts/python.exe -c "
from app import create_app
from rss_collector import collect_articles
from database import Article

app = create_app()
new_count = collect_articles(app)
print('新規収集件数:', new_count)

with app.app_context():
    gacha_articles = Article.query.filter_by(account_id=2).order_by(Article.id.desc()).limit(5).all()
    for a in gacha_articles:
        print(a.id, a.account_id, a.feed_source, a.title[:40])
"
```
Expected: エラーなく実行され、`ガチャパラ`由来の記事が収集されていれば`account_id=2`で保存されている（RSSフィードの更新頻度によっては新規記事が0件のこともあるが、その場合もエラーは出ない）。

- [ ] **Step 3: 承認待ち画面でアカウント2に切り替えて表示確認する**

```bash
venv/Scripts/python.exe app.py
```
（バックグラウンド実行）

```bash
curl -s "http://localhost:5000/pending?account_id=2" -o /dev/null -w "%{http_code}\n"
```
Expected: `200`

```bash
curl -s "http://localhost:5000/settings" | grep -A1 'acc-topic-display-2'
```
Expected: `ガチャガチャ・カプセルトイに関する記事`が含まれる。

アプリのバックグラウンドタスクを停止する。

- [ ] **Step 4: Commit**

（コード変更はないため、DB内のSetting/ThreadsAccountデータのみの変更。`instance/rock_metal.db`はgit管理対象外の想定のため、コミットは不要。`.gitignore`を確認し、DBファイルが追跡対象になっていないことだけ確認する）

```bash
git status --short
```
Expected: `instance/rock_metal.db`（またはDBファイル）が変更点として出てこない（`.gitignore`で除外されている）こと。コード変更がなければ、このタスクではコミットを作成しない。

---

## 完了確認

全タスク完了後、以下を満たしていることを確認する:

- [ ] `ThreadsAccount`に`content_topic`列が追加され、既存DBでもマイグレーションエラーが起きない
- [ ] 設定画面でアカウント名・コンテンツトピックの両方をインライン編集できる
- [ ] RSSフィード設定でフィードごとにアカウントを選択でき、保存後も`account_id`と`lang`が保持される
- [ ] `content_topic`が設定されたアカウントのフィードは、KPOPキーワードフィルタをスキップし、汎用AI関連度判定を使う
- [ ] 収集された記事に正しい`account_id`がセットされる
- [ ] `content_topic`設定済みアカウントの記事は、KPOP専用プロンプトを経由せず汎用プロンプトで投稿文が生成される
- [ ] ガチャパラフィード（`https://gachapara.jp/feed/`）がaccount_id=2向けに登録されている
