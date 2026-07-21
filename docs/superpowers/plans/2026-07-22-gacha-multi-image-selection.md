# ガチャパラ記事の複数画像取得・選択機能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ガチャパラ（gachapara.jp）のRSS収集で、OGP画像に加えて記事本文内の商品画像を最大6枚（合計最大7枚）取得し、承認待ち画面でどの画像を投稿に使うかチェックボックスで選択できるようにする。

**Architecture:** `rss_collector.py`の`_fetch_ogp_thumbnail`を、OGP画像＋本文画像（`gachapara.jp`ドメインのみ、広告/バナー/アイコン等を除外）を1回のHTTPリクエストで取得する`_fetch_article_images`に置き換える。取得した画像リストの先頭を既存の`Article.thumbnail_url`、残りを`Article.image_urls`に保存する（スキーマ変更なし）。新規`POST /articles/<id>/update-images`エンドポイントが、承認待ち画面のチェックボックス操作を受けて同じ2フィールドを上書きする。既存の`_build_image_list`（プレビュー表示）・`threads_api.py`（投稿時カルーセル生成）はこの2フィールドを読むだけなので変更不要。選択UI自体は`pending.html`の共通テンプレート部分に実装するため、KPOPアカウントの記事（要約生成時に取得される複数画像）でも同じ選択機能が使えるようになる。

**Tech Stack:** Python 3.x / Flask / SQLAlchemy / SQLite / requests / Jinja2 / vanilla JS（fetch API）

## Global Constraints

- 対象ファイルは`rss_collector.py`・`app.py`・`templates/pending.html`のみ（設計書のスコープ通り。`database.py`・`threads_api.py`・`templates/queue.html`は変更不要）
- コメントは原則書かない（WHYが非自明な場合のみ1行）
- 既存ファイルを編集する。新規ファイルは作らない
- 必要な変更だけ行う。リファクタリング・クリーンアップは不要
- Shellコマンドの実行環境はWindows。Bashツールを使う場合はGit Bash構文（`$VAR`、`&&`可）、PowerShellツールを使う場合はPowerShell構文（`$env:VAR`、`&&`不可）に注意する
- このプロジェクトにはpytest等のテストフレームワークが存在しない。各タスクの検証は、実際にFlaskアプリを起動し、`venv/Scripts/python.exe -c "..."`によるDB直接確認や`curl`によるHTTPリクエストで動作確認する
- `venv/Scripts/python.exe`が仮想環境のPythonインタプリタ
- KPOPアカウント（`content_topic`未設定）のスクレイピングロジック・画像取得タイミングを変更しないこと（選択UIのみ共通化し、取得ロジックはガチャアカウント専用のまま）
- 画像選択保存後も、既存の`_build_image_list`（app.py）・`threads_api.py`のカルーセル生成ロジックは変更しない（`thumbnail_url`/`image_urls`の2フィールドへの保存だけで選択結果が自動的に反映される設計のため）

---

## ファイル構成

| ファイル | 変更内容 |
|---|---|
| `rss_collector.py` | `_fetch_ogp_thumbnail`を`_fetch_article_images`に置き換え（本文画像取得・ドメイン制限・除外フィルタを追加）、`collect_articles()`の呼び出し元を更新 |
| `app.py` | 新規ルート`POST /articles/<id>/update-images`を追加 |
| `templates/pending.html` | 画像プレビューにチェックボックスを追加、選択変更時に自動保存するJSを追加 |

---

### Task 1: 記事本文からの複数画像取得（`rss_collector.py`）

**Files:**
- Modify: `rss_collector.py:1-10`（import追加）
- Modify: `rss_collector.py`（`_fetch_ogp_thumbnail`関数を`_fetch_article_images`に置き換え）
- Modify: `rss_collector.py`（`collect_articles`のArticle作成部分）

**Interfaces:**
- Produces: `_is_gachapara_content_image(url: str) -> bool`、`_fetch_article_images(url: str, max_body_images: int = 6) -> list`（戻り値は画像URLのリスト、先頭がOGP画像）
- Consumes: なし（Task 2・Task 3はこのタスクの出力である`Article.thumbnail_url`/`Article.image_urls`のデータを前提にするが、コード上の直接依存はない）

- [ ] **Step 1: `urljoin`/`urlparse`のimportを追加**

`rss_collector.py:1-10`（目印）:

```python
import json
import logging
import os
import re
from datetime import datetime, timedelta

import feedparser
import requests

from database import Article, Setting, ThreadsAccount, db
```

これを以下に置き換える:

```python
import json
import logging
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import feedparser
import requests

from database import Article, Setting, ThreadsAccount, db
```

- [ ] **Step 2: `_fetch_ogp_thumbnail`を`_fetch_article_images`に置き換える**

`rss_collector.py`内、以下のブロック（目印。`_strip_html`関数の直後、`# ── AI判定（第2フィルター） ──`という区切りコメントの直前）:

```python
_OGP_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.IGNORECASE,
)


def _fetch_ogp_thumbnail(url: str) -> str:
    """記事ページのOGP画像（og:image）を取得する。取得失敗時は空文字を返す。"""
    try:
        resp = requests.get(url, headers={"User-Agent": "KpopWaveBot/1.0"}, timeout=10)
        if resp.status_code != 200:
            return ""
        m = _OGP_IMAGE_RE.search(resp.text)
        if not m:
            return ""
        img = (m.group(1) or m.group(2) or "").strip()
        return img if img.startswith("http") else ""
    except Exception as exc:
        logger.warning("OGP画像取得エラー [%s]: %s", url, exc)
        return ""


# ── AI判定（第2フィルター） ──────────────────────────────────────────────────
```

これを以下に置き換える:

```python
_OGP_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.IGNORECASE,
)

_ARTICLE_IMG_EXCLUDE_HINTS = [
    "banner", "/ads/", "ad-", "-ad.", "sponsor", "widget", "spacer",
    "btn-", "share", "logo", "icon", "avatar", "sns",
    "wp-content/themes", ".svg", "1x1", "pixel", "tracking",
]


def _is_gachapara_content_image(url: str) -> bool:
    """gachapara.jpドメインの画像で、広告・バナー・アイコン等でないものだけ許可する。"""
    if not url or not url.startswith("http"):
        return False
    host = urlparse(url).netloc.lower()
    if host != "gachapara.jp" and not host.endswith(".gachapara.jp"):
        return False
    low = url.lower()
    return not any(h in low for h in _ARTICLE_IMG_EXCLUDE_HINTS)


def _fetch_article_images(url: str, max_body_images: int = 6) -> list:
    """記事ページのOGP画像＋本文内画像（gachapara.jpドメインのみ）を取得する。
    先頭がOGP画像、以降が本文画像。取得失敗時は空リストを返す。"""
    try:
        resp = requests.get(url, headers={"User-Agent": "KpopWaveBot/1.0"}, timeout=10)
        if resp.status_code != 200:
            return []

        html = resp.text
        images = []

        m = _OGP_IMAGE_RE.search(html)
        if m:
            og_img = (m.group(1) or m.group(2) or "").strip()
            if _is_gachapara_content_image(og_img):
                images.append(og_img)

        for section_tag in ("article", "main"):
            sm = re.search(rf"<{section_tag}[^>]*>([\s\S]*?)</{section_tag}>", html, re.IGNORECASE)
            if not sm:
                continue
            for img_src in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', sm.group(1), re.IGNORECASE):
                resolved = urljoin(url, img_src)
                if _is_gachapara_content_image(resolved) and resolved not in images:
                    images.append(resolved)
                    if len(images) >= max_body_images + 1:
                        break
            break

        return images
    except Exception as exc:
        logger.warning("記事画像取得エラー [%s]: %s", url, exc)
        return []


# ── AI判定（第2フィルター） ──────────────────────────────────────────────────
```

- [ ] **Step 3: `_is_gachapara_content_image`をオフラインで確認する**

```bash
venv/Scripts/python.exe -c "
from rss_collector import _is_gachapara_content_image
print(_is_gachapara_content_image('https://gachapara.jp/wp-content/uploads/2026/07/product.jpg'))
print(_is_gachapara_content_image('https://ads.example.com/banner.jpg'))
print(_is_gachapara_content_image('https://gachapara.jp/wp-content/themes/gachapara/img/logo.png'))
print(_is_gachapara_content_image('https://gachapara.jp/wp-content/uploads/2026/07/share-icon.png'))
"
```
Expected: `True` / `False` / `False` / `False` の順で出力される（1件目のみドメイン一致かつ除外語を含まず許可、2件目はドメイン不一致、3件目は`wp-content/themes`を含む、4件目は`icon`を含む）。

- [ ] **Step 4: `_fetch_article_images`の正規表現抽出ロジックをネットワーク接続なしで確認する**

```bash
venv/Scripts/python.exe -c "
from rss_collector import _fetch_article_images, _OGP_IMAGE_RE
import re
html = '''
<html><head><meta property=\"og:image\" content=\"https://gachapara.jp/wp-content/uploads/og.jpg\"></head>
<body><article>
<img src=\"/wp-content/uploads/2026/07/product1.jpg\">
<img src=\"https://gachapara.jp/wp-content/uploads/2026/07/product2.jpg\">
<img src=\"https://gachapara.jp/wp-content/themes/x/logo.png\">
<img src=\"https://ads.example.com/banner.jpg\">
</article></body></html>
'''
m = _OGP_IMAGE_RE.search(html)
print('og:', (m.group(1) or m.group(2)) if m else None)
imgs = re.findall(r'<img[^>]+src=[\"\\']([^\"\\']+)[\"\\']', html, re.IGNORECASE)
print('raw img srcs:', imgs)
"
```
Expected: `og:`の後に`https://gachapara.jp/wp-content/uploads/og.jpg`、`raw img srcs:`に4件のsrcが表示される（フィルタ適用前の生の抽出結果であることの確認）。

- [ ] **Step 5: `collect_articles`の呼び出し元を更新する**

`rss_collector.py`内、以下のブロック（目印。フィルタ4のAI判定ブロックとArticle作成ループの間）:

```python
        # content_topicアカウント（非KPOP）は承認待ち画面での画像プレビュー用に
        # OGP画像を収集時点で取得する（KPOPアカウントは要約生成時に取得する既存フローのまま）
        thumbnails = {}
        if content_topic:
            for idx in approved_indices:
                thumbnails[idx] = _fetch_ogp_thumbnail(candidates[idx]["url"])

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
                    thumbnail_url=thumbnails.get(idx) or None,
                )
                db.session.add(article)
                seen_urls.add(c["url"])
                added += 1
                new_count += 1
            db.session.commit()
```

これを以下に置き換える:

```python
        # content_topicアカウント（非KPOP）は承認待ち画面での画像プレビュー用に
        # OGP画像＋本文内商品画像を収集時点で取得する
        # （KPOPアカウントは要約生成時に取得する既存フローのまま）
        article_images = {}
        if content_topic:
            for idx in approved_indices:
                article_images[idx] = _fetch_article_images(candidates[idx]["url"])

        with app.app_context():
            for idx in approved_indices:
                c = candidates[idx]
                imgs = article_images.get(idx, [])
                article = Article(
                    feed_source=c["feed_source"],
                    title=c["title"][:500] or "No Title",
                    url=c["url"],
                    published_at=c["published_at"],
                    raw_content=c["raw_content"],
                    status="pending",
                    account_id=c["account_id"],
                    thumbnail_url=imgs[0] if imgs else None,
                    image_urls=json.dumps(imgs[1:], ensure_ascii=False) if len(imgs) > 1 else None,
                )
                db.session.add(article)
                seen_urls.add(c["url"])
                added += 1
                new_count += 1
            db.session.commit()
```

- [ ] **Step 6: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile rss_collector.py
```
Expected: エラーなし（出力なし）。

- [ ] **Step 7: ガチャパラフィードで実際に収集し、複数画像が取得されることを確認する**

（ネットワーク接続が必要。接続できない環境の場合はこのステップをスキップし、Step 6の構文チェックのみで良しとする）

```bash
venv/Scripts/python.exe -c "
from app import create_app
from rss_collector import collect_articles
from database import Article
import json

app = create_app()
new_count = collect_articles(app)
print('新規収集件数:', new_count)

with app.app_context():
    gacha_articles = Article.query.filter_by(account_id=2).order_by(Article.id.desc()).limit(5).all()
    for a in gacha_articles:
        imgs = json.loads(a.image_urls) if a.image_urls else []
        print(a.id, a.title[:30], 'thumbnail=', bool(a.thumbnail_url), 'body_images=', len(imgs))
        if a.thumbnail_url:
            print('  thumbnail domain ok:', 'gachapara.jp' in a.thumbnail_url)
        for u in imgs:
            print('  body image domain ok:', 'gachapara.jp' in u)
"
```
Expected: エラーなく実行され、新規収集されたガチャパラ記事があれば`thumbnail=True`、`body_images`が0〜6件、全ての画像URLが`gachapara.jp`ドメインであることが確認できる。

- [ ] **Step 8: Commit**

```bash
git add rss_collector.py
git commit -m "$(cat <<'EOF'
feat: ガチャパラRSS収集で記事本文内の商品画像も取得する

OGP画像1枚だけだった取得を、gachapara.jpドメイン限定・広告/バナー/
アイコン等除外の本文内画像（最大6枚）取得に拡張した。取得結果は
先頭をthumbnail_url、残りをimage_urlsに保存する（スキーマ変更なし）。
EOF
)"
```

---

### Task 2: 画像選択の保存エンドポイント（`app.py`）

**Files:**
- Modify: `app.py:679-688`（`edit_article`ルートの直後に新規ルートを挿入）

**Interfaces:**
- Consumes: `Article.thumbnail_url`/`Article.image_urls`（既存カラム、Task 1で複数画像が保存されるようになる）
- Produces: `POST /articles/<int:id>/update-images`エンドポイント（JSONリクエスト`{urls: [...]}`→JSONレスポンス`{ok: bool, count: int}`）。Task 3のJSがこのエンドポイントを呼び出す

- [ ] **Step 1: `app.py`に新規ルートを追加**

`app.py:679-690`（目印）:

```python
@app.route("/articles/<int:id>/edit", methods=["POST"])
def edit_article(id):
    article = Article.query.get_or_404(id)
    summary = (request.form.get("summary") or "").strip()
    if not summary:
        return jsonify({"success": False, "error": "要約が空です"})
    article.summary = summary
    db.session.commit()
    return jsonify({"success": True, "summary": summary, "length": len(summary)})


@app.route("/articles/<int:id>/resummary", methods=["POST"])
```

これを以下に置き換える（`edit_article`と`resummary_article`の間に新規ルートを挿入）:

```python
@app.route("/articles/<int:id>/edit", methods=["POST"])
def edit_article(id):
    article = Article.query.get_or_404(id)
    summary = (request.form.get("summary") or "").strip()
    if not summary:
        return jsonify({"success": False, "error": "要約が空です"})
    article.summary = summary
    db.session.commit()
    return jsonify({"success": True, "summary": summary, "length": len(summary)})


@app.route("/articles/<int:id>/update-images", methods=["POST"])
def update_article_images(id):
    article = Article.query.get_or_404(id)
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])
    if not isinstance(urls, list):
        return jsonify({"ok": False, "error": "urls must be a list"}), 400
    urls = [u for u in urls if isinstance(u, str) and u.strip()]
    article.thumbnail_url = urls[0] if urls else None
    article.image_urls = json.dumps(urls[1:], ensure_ascii=False) if len(urls) > 1 else None
    db.session.commit()
    return jsonify({"ok": True, "count": len(urls)})


@app.route("/articles/<int:id>/resummary", methods=["POST"])
```

- [ ] **Step 2: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile app.py
```
Expected: エラーなし。

- [ ] **Step 3: エンドポイントの動作を確認する**

（`run.py`によりFlask開発サーバーがすでにポート5000で起動中の想定。新たに`python app.py`/`python run.py`を起動しないこと。`netstat -ano | grep ":5000" | grep LISTENING`で確認できる場合はそのサーバーに対してcurlする）

まずテスト用の記事を1件作成し、`update-images`を呼び出して`thumbnail_url`/`image_urls`が更新されることを確認する:

```bash
venv/Scripts/python.exe -c "
from app import create_app
from database import Article, db
import requests
import json

app = create_app()
with app.app_context():
    art = Article(
        feed_source='テスト', title='画像選択テスト記事',
        url='https://example.com/update-images-test',
        status='pending', account_id=2,
        thumbnail_url='https://gachapara.jp/a.jpg',
        image_urls=json.dumps(['https://gachapara.jp/b.jpg', 'https://gachapara.jp/c.jpg']),
    )
    db.session.add(art)
    db.session.commit()
    article_id = art.id

r = requests.post(
    f'http://localhost:5000/articles/{article_id}/update-images',
    json={'urls': ['https://gachapara.jp/b.jpg']},
)
print('status:', r.status_code, 'body:', r.json())

with app.app_context():
    a = db.session.get(Article, article_id)
    print('thumbnail_url:', a.thumbnail_url)
    print('image_urls:', a.image_urls)
    db.session.delete(a)
    db.session.commit()
"
```
Expected: `status: 200`、`body`が`{'ok': True, 'count': 1}`、更新後`thumbnail_url`が`https://gachapara.jp/b.jpg`、`image_urls`が`null`（1件のみなので`None`）。もしFlask開発サーバーが起動していない場合はこのステップをスキップし、Step 2の構文チェックのみで良しとする。

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "$(cat <<'EOF'
feat: 記事画像の選択状態を保存するAPIを追加

POST /articles/<id>/update-images。選択中の画像URL配列を受け取り、
先頭をthumbnail_url、残りをimage_urlsに上書き保存する。既存の
プレビュー・投稿時カルーセル生成ロジックはこの2フィールドを読む
だけなので変更不要。
EOF
)"
```

---

### Task 3: 承認待ち画面の画像選択UI（`templates/pending.html`）

**Files:**
- Modify: `templates/pending.html:249-269`（画像プレビュー部分）
- Modify: `templates/pending.html`（JS: `resummary`関数の直後に新規関数を追加）

**Interfaces:**
- Consumes: `POST /articles/<int:id>/update-images`（Task 2）
- Produces: 承認待ち画面上でのチェックボックス操作による画像選択・自動保存

- [ ] **Step 1: 画像プレビュー部分にチェックボックスを追加**

`templates/pending.html:249-269`（目印）:

```html
      <!-- 画像プレビュー（記事投稿のみ: thumbnail→image_urls全件、最大20枚） -->
      {% else %}
      {% set imgs = images_map.get(a.id, []) %}
      {% if imgs %}
      <div class="mb-1" style="display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;-webkit-overflow-scrolling:touch">
        {% for img_url in imgs %}
        <a href="{{ a.url }}" target="_blank" rel="noopener" style="flex:0 0 auto">
          <img src="{{ img_url }}"
               style="height:100px;width:auto;max-width:160px;border-radius:6px;display:block;object-fit:cover">
        </a>
        {% endfor %}
      </div>
      <div class="mb-2" style="font-size:.72rem;color:var(--text-muted)">
        {% if imgs|length >= 2 %}
          <i class="bi bi-images me-1"></i>カルーセル投稿（{{ imgs|length }}枚）
        {% else %}
          <i class="bi bi-image me-1"></i>画像投稿（1枚）
        {% endif %}
      </div>
      {% endif %}
      {% endif %}
```

これを以下に置き換える:

```html
      <!-- 画像プレビュー（記事投稿のみ: thumbnail→image_urls全件、最大20枚、チェックボックスで選択可能） -->
      {% else %}
      {% set imgs = images_map.get(a.id, []) %}
      {% if imgs %}
      <div class="mb-1" id="img-row-{{ a.id }}" style="display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;-webkit-overflow-scrolling:touch">
        {% for img_url in imgs %}
        <div class="position-relative" style="flex:0 0 auto">
          <a href="{{ a.url }}" target="_blank" rel="noopener">
            <img src="{{ img_url }}"
                 style="height:100px;width:auto;max-width:160px;border-radius:6px;display:block;object-fit:cover">
          </a>
          <input type="checkbox" class="img-select-cb" data-url="{{ img_url }}" checked
                 onchange="updateImageSelection({{ a.id }})"
                 style="position:absolute;top:4px;left:4px;width:18px;height:18px;cursor:pointer;accent-color:var(--accent);box-shadow:0 0 3px rgba(0,0,0,.6)">
        </div>
        {% endfor %}
      </div>
      <div class="mb-2" style="font-size:.72rem;color:var(--text-muted)">
        {% if imgs|length >= 2 %}
          <i class="bi bi-images me-1"></i>カルーセル投稿（{{ imgs|length }}枚）
        {% else %}
          <i class="bi bi-image me-1"></i>画像投稿（1枚）
        {% endif %}
      </div>
      {% endif %}
      {% endif %}
```

- [ ] **Step 2: 選択保存用JSを追加**

`templates/pending.html`内、以下のブロック（目印。`resummary`関数の直後、`document.addEventListener('DOMContentLoaded', ...)`の直前）:

```javascript
function resummary(id, btn) {
  const style = 'つぶやき型';
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  fetch('/articles/' + id + '/resummary', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: 'style=' + encodeURIComponent(style)
  })
    .then(r => r.json()).then(d => {
      if (d.success) {
        const disp = document.getElementById('display-' + id);
        if (disp) {
          disp.textContent = d.summary;
          const ta = document.getElementById('textarea-' + id);
          if (ta) ta.value = getEditInitialValue(id, d.summary);
          updateCount(id);
          const cc = document.querySelector('#card-' + id + ' .char-count');
          if (cc) {
            cc.textContent = d.length + ' / 500 文字';
            cc.className = 'char-count ' + (d.length > 500 ? 'over' : 'ok');
          }
          enterEditMode(id);
          const card = document.getElementById('card-' + id);
          if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else {
          sessionStorage.setItem('pendingEditId', id);
          location.reload();
        }
      } else {
        alert('要約生成失敗: ' + d.error);
      }
      btn.disabled = false;
      btn.innerHTML = orig;
    });
}

document.addEventListener('DOMContentLoaded', function() {
```

これを以下に置き換える（`resummary`関数と`document.addEventListener`の間に新規関数を挿入）:

```javascript
function resummary(id, btn) {
  const style = 'つぶやき型';
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  fetch('/articles/' + id + '/resummary', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: 'style=' + encodeURIComponent(style)
  })
    .then(r => r.json()).then(d => {
      if (d.success) {
        const disp = document.getElementById('display-' + id);
        if (disp) {
          disp.textContent = d.summary;
          const ta = document.getElementById('textarea-' + id);
          if (ta) ta.value = getEditInitialValue(id, d.summary);
          updateCount(id);
          const cc = document.querySelector('#card-' + id + ' .char-count');
          if (cc) {
            cc.textContent = d.length + ' / 500 文字';
            cc.className = 'char-count ' + (d.length > 500 ? 'over' : 'ok');
          }
          enterEditMode(id);
          const card = document.getElementById('card-' + id);
          if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else {
          sessionStorage.setItem('pendingEditId', id);
          location.reload();
        }
      } else {
        alert('要約生成失敗: ' + d.error);
      }
      btn.disabled = false;
      btn.innerHTML = orig;
    });
}

function updateImageSelection(articleId) {
  const row = document.getElementById('img-row-' + articleId);
  if (!row) return;
  const urls = Array.from(row.querySelectorAll('.img-select-cb:checked')).map(cb => cb.dataset.url);
  fetch('/articles/' + articleId + '/update-images', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ urls: urls }),
  })
    .then(r => r.json())
    .then(function(d) {
      if (!d.ok) alert(d.error || '画像選択の保存に失敗しました');
    })
    .catch(function() {
      alert('画像選択の保存に失敗しました');
    });
}

document.addEventListener('DOMContentLoaded', function() {
```

- [ ] **Step 3: テンプレートの構文確認**

Flaskアプリが起動中であれば、承認待ち画面を取得してチェックボックスが描画されていることを確認する（`run.py`がファイル変更を検知して自動リロードする。新たにサーバーを起動しないこと）:

```bash
curl -s "http://localhost:5000/pending?account_id=2&tab=rss" -o /dev/null -w "%{http_code}\n"
```
Expected: `200`

```bash
curl -s "http://localhost:5000/pending?account_id=2&tab=rss" | grep -c 'img-select-cb'
```
Expected: 画像が存在する記事の枚数分（1以上）。0件の場合は、承認待ちにガチャパラ記事が現在ないことが原因の可能性があるため、Task 1のStep 7で収集した記事が承認待ち（`status=pending`）のまま残っているか確認する。

- [ ] **Step 4: チェックボックス操作→保存が実際に機能することを確認する**

Task 2のStep 3で使ったテスト記事作成パターンを流用し、承認待ち記事を1件作成した上で、ブラウザ操作の代わりに`update-images`への直接POSTで一連の流れ（表示→選択変更→DB反映）が繋がっていることを確認する:

```bash
venv/Scripts/python.exe -c "
from app import create_app
from database import Article, db
import requests
import json

app = create_app()
with app.app_context():
    art = Article(
        feed_source='テスト', title='UI選択テスト記事',
        url='https://example.com/ui-select-test',
        status='pending', account_id=2,
        thumbnail_url='https://gachapara.jp/x.jpg',
        image_urls=json.dumps(['https://gachapara.jp/y.jpg', 'https://gachapara.jp/z.jpg']),
    )
    db.session.add(art)
    db.session.commit()
    article_id = art.id

html = requests.get('http://localhost:5000/pending?account_id=2&tab=rss').text
print('チェックボックス描画確認:', 'img-select-cb' in html and 'https://gachapara.jp/x.jpg' in html)

r = requests.post(
    f'http://localhost:5000/articles/{article_id}/update-images',
    json={'urls': ['https://gachapara.jp/y.jpg', 'https://gachapara.jp/z.jpg']},
)
print('保存結果:', r.json())

with app.app_context():
    a = db.session.get(Article, article_id)
    print('保存後 thumbnail_url:', a.thumbnail_url)
    print('保存後 image_urls:', a.image_urls)
    db.session.delete(a)
    db.session.commit()
"
```
Expected: `チェックボックス描画確認: True`、`保存結果`が`{'ok': True, 'count': 2}`、保存後`thumbnail_url`が`https://gachapara.jp/y.jpg`（1枚目を除外して2枚目・3枚目を選択したケース）。Flask開発サーバーが起動していない場合はこのステップをスキップし、Step 3で代替する。

- [ ] **Step 5: Commit**

```bash
git add templates/pending.html
git commit -m "$(cat <<'EOF'
feat: 承認待ち画面で投稿画像をチェックボックスで選択できるようにする

画像プレビューにチェックボックスをオーバーレイし、選択変更のたびに
/articles/<id>/update-imagesへ自動保存する。全アカウント共通の
テンプレート部分に実装したため、KPOP記事の複数画像でも選択可能。
EOF
)"
```

---

## 完了確認

全タスク完了後、以下を満たしていることを確認する:

- [ ] ガチャパラRSS収集で、OGP画像に加えて記事本文内の商品画像（最大6枚、`gachapara.jp`ドメインのみ）が`Article.thumbnail_url`/`image_urls`に保存される
- [ ] 広告・バナー・アイコン等が除外キーワードで除外され、`gachapara.jp`以外のドメインの画像が保存されない
- [ ] `POST /articles/<id>/update-images`が選択中の画像URLを受け取り、`thumbnail_url`/`image_urls`を正しく上書きする
- [ ] 承認待ち画面で画像プレビューにチェックボックスが表示され、操作すると自動保存される
- [ ] 選択UIはKPOPアカウントの記事（複数画像を持つ場合）でも同様に機能する
- [ ] KPOPアカウントのスクレイピングロジック・画像取得タイミングに変更がない
- [ ] `templates/queue.html`・`threads_api.py`は変更されていない（既存の`thumbnail_url`/`image_urls`読み取りロジックがそのまま選択結果を反映する）
