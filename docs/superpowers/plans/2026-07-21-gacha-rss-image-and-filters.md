# ガチャパラRSS収集: 画像取得・除外フィルタ強化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ガチャパラRSS収集（`content_topic`設定済みアカウント向け）で、記事のOGP画像を取得して承認待ち画面に画像プレビューを表示できるようにし、店舗オープン・ゲームセンター・イベント/展示会・カプセルトイ以外のニュースを収集段階で除外する。

**Architecture:** `rss_collector.py`の`collect_articles()`に、`content_topic`が設定されたアカウント（現状ガチャ沼の住人、account_id=2）専用の分岐として、(1) ハードキーワードによる除外フィルタ、(2) AI関連度判定プロンプトへの除外指示追加、(3) AI判定通過後のOGP画像取得、の3点を追加する。KPOPアカウント（`content_topic`未設定）の収集ロジック・画像取得タイミングは一切変更しない。

**Tech Stack:** Python 3.x / Flask / SQLAlchemy / SQLite / feedparser / requests / anthropic SDK（Claude Haiku）

## Global Constraints

- 対象ファイルは`rss_collector.py`のみ（設計書のスコープ通り。`database.py`・`app.py`・テンプレートは変更不要）
- コメントは原則書かない（WHYが非自明な場合のみ1行）
- 既存ファイルを編集する。新規ファイルは作らない
- 必要な変更だけ行う。リファクタリング・クリーンアップは不要
- Shellコマンドの実行環境はWindows。Bashツールを使う場合はGit Bash構文（`$VAR`、`&&`可）、PowerShellツールを使う場合はPowerShell構文（`$env:VAR`、`&&`不可）に注意する
- このプロジェクトにはpytest等のテストフレームワークが存在しない。各タスクの検証は、実際にFlaskアプリを起動し、`venv/Scripts/python.exe -c "..."`によるDB直接確認や`curl`によるHTTPリクエストで動作確認する
- `venv/Scripts/python.exe`が仮想環境のPythonインタプリタ
- KPOPアカウント（`content_topic`未設定）の既存フローの挙動・パフォーマンスを変更しないこと

---

## ファイル構成

| ファイル | 変更内容 |
|---|---|
| `rss_collector.py` | 除外キーワードリスト・判定関数の追加、`collect_articles()`のフィルタ分岐変更、AI判定プロンプトの除外指示追加、OGP画像取得関数の追加と`collect_articles()`への統合 |

---

### Task 1: 除外キーワードフィルタとAI判定プロンプトの強化

**Files:**
- Modify: `rss_collector.py:57-91`（`EXCLUDE_KEYWORDS`定義および`_check_excluded`関数の直後に新規定義を追加）
- Modify: `rss_collector.py:128-134`（`_ai_judge_titles`の汎用プロンプト分岐）
- Modify: `rss_collector.py:258-270`（`collect_articles`のフィルタ分岐）

**Interfaces:**
- Produces: `CONTENT_TOPIC_EXCLUDE_KEYWORDS`（`list[str]`）、`_check_content_topic_excluded(title: str, content: str) -> bool`

- [ ] **Step 1: `rss_collector.py`に除外キーワードリストと判定関数を追加**

`rss_collector.py:88-91`（`_check_excluded`関数の直後、`# ── AI判定（第2フィルター） ──`という区切りコメントの直前）に以下を挿入する。目印:

```python
def _check_excluded(title: str, content: str) -> bool:
    text = (title + " " + content).lower()
    return any(kw in text for kw in EXCLUDE_KEYWORDS)


def _is_recent(published_at) -> bool:
```

これを以下に置き換える（`_check_excluded`と`_is_recent`の間に新規コードを挿入）:

```python
def _check_excluded(title: str, content: str) -> bool:
    text = (title + " " + content).lower()
    return any(kw in text for kw in EXCLUDE_KEYWORDS)


# ── content_topicアカウント（非KPOP）向け除外キーワード ──────────────────────
# 現状ガチャ沼の住人（ガチャガチャ・カプセルトイ）向け。将来別のcontent_topic
# アカウントが増えた場合はアカウント別リストへの汎用化を検討する。
CONTENT_TOPIC_EXCLUDE_KEYWORDS = ["オープン", "ゲームセンター", "ゲーセン"]


def _check_content_topic_excluded(title: str, content: str) -> bool:
    text = title + " " + content
    return any(kw in text for kw in CONTENT_TOPIC_EXCLUDE_KEYWORDS)


def _is_recent(published_at) -> bool:
```

- [ ] **Step 2: 関数を手動確認する**

```bash
venv/Scripts/python.exe -c "
from rss_collector import _check_content_topic_excluded
print(_check_content_topic_excluded('新店舗が渋谷にオープン', ''))
print(_check_content_topic_excluded('新作カプセルトイ発売', ''))
print(_check_content_topic_excluded('ゲームセンターに新台入荷', ''))
"
```
Expected: `True` / `False` / `True` の順で出力される。

- [ ] **Step 3: `_ai_judge_titles`の汎用プロンプトに除外指示を追加**

`rss_collector.py:128-134`（目印）:

```python
    else:
        prompt = (
            f"以下の記事タイトルのうち、{topic_label}に関するものを選び、"
            "番号をカンマ区切りで返してください。\n\n"
            f"{numbered}\n\n"
            "回答は番号のみ（例: 1,3,5）。対象なし→「なし」"
        )
```

これを以下に置き換える:

```python
    else:
        prompt = (
            f"以下の記事タイトルのうち、{topic_label}に関するニュースを選び、"
            "番号をカンマ区切りで返してください。\n"
            f"除外: イベント・展示会情報のみのもの、{topic_label}と直接関係のないニュース\n\n"
            f"{numbered}\n\n"
            "回答は番号のみ（例: 1,3,5）。対象なし→「なし」"
        )
```

- [ ] **Step 4: `collect_articles`のフィルタ分岐を変更**

`rss_collector.py:258-270`（目印）:

```python
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
```

これを以下に置き換える:

```python
                # content_topic 設定済みアカウント（非KPOP）のフィードは、女性KPOP
                # キーワード辞書の代わりにcontent_topic向け除外キーワードを適用する。
                # KPOPアカウント（content_topic未設定）は既存通り、日本語フィードのみスキップ。
                if content_topic:
                    # ── フィルター2': content_topic向け除外キーワード ─────
                    if _check_content_topic_excluded(title, plain_content):
                        skipped_kw += 1
                        continue
                elif not is_ja:
                    # ── フィルター2: 除外キーワード ──────────────────────
                    if _check_excluded(title, plain_content):
                        skipped_kw += 1
                        continue

                    # ── フィルター3: 女性KPOPキーワード ──────────────────
                    if not _check_female_kpop(title, plain_content):
                        skipped_kw += 1
                        continue
```

- [ ] **Step 5: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile rss_collector.py
```
Expected: エラーなし（出力なし）。

- [ ] **Step 6: KPOPフローに影響がないことを確認する**

```bash
venv/Scripts/python.exe -c "
from rss_collector import _check_excluded, _check_female_kpop
# 既存のKPOP専用フィルタが変更されていないことを確認
print(_check_excluded('BTS release new album', ''))
print(_check_female_kpop('BLACKPINK comeback announced', ''))
"
```
Expected: `True` / `True`（変更前と同じ挙動）。

- [ ] **Step 7: Commit**

```bash
git add rss_collector.py
git commit -m "$(cat <<'EOF'
feat: content_topicアカウント向けRSS除外フィルタを追加

店舗オープン・ゲームセンター関連はキーワードで機械的に除外し、
イベント・展示会情報やトピック外ニュースはAI関連度判定プロンプトの
除外指示で弾くハイブリッド方式。KPOPアカウントの既存フローは変更なし。
EOF
)"
```

---

### Task 2: OGP画像取得を収集時に行う

**Files:**
- Modify: `rss_collector.py:1-11`（import追加）
- Modify: `rss_collector.py`（`_check_content_topic_excluded`の直後、または`_strip_html`の直後に新規関数追加）
- Modify: `rss_collector.py`（`collect_articles`のAI判定〜Article作成部分）

**Interfaces:**
- Consumes: Task 1で追加した`content_topic`分岐の考え方
- Produces: `_fetch_ogp_thumbnail(url: str) -> str`。`collect_articles()`実行後、`content_topic`設定済みアカウントの新規`Article`に`thumbnail_url`が設定される

- [ ] **Step 1: `requests`のimportを追加**

`rss_collector.py:1-9`（目印）:

```python
import json
import logging
import os
import re
from datetime import datetime, timedelta

import feedparser

from database import Article, Setting, ThreadsAccount, db
```

これを以下に置き換える:

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

- [ ] **Step 2: `_fetch_ogp_thumbnail`関数を追加**

`rss_collector.py`の`_strip_html`関数（`_check_content_topic_excluded`と`_is_recent`の間に追加済み・元の`_is_recent`関数の直後）の直後、`# ── AI判定（第2フィルター） ──`という区切りコメントの直前に以下を挿入する。目印:

```python
def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


# ── AI判定（第2フィルター） ──────────────────────────────────────────────────
```

これを以下に置き換える:

```python
def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


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

- [ ] **Step 3: 正規表現の抽出ロジックをネットワーク接続なしで確認する**

```bash
venv/Scripts/python.exe -c "
from rss_collector import _OGP_IMAGE_RE

html = '<html><head><meta property=\"og:image\" content=\"https://gachapara.jp/wp-content/uploads/sample.jpg\"></head></html>'
m = _OGP_IMAGE_RE.search(html)
print((m.group(1) or m.group(2)) if m else None)
"
```
Expected: `https://gachapara.jp/wp-content/uploads/sample.jpg`

- [ ] **Step 4: `collect_articles`にOGP画像取得を統合する**

`rss_collector.py`内、フィルタ4（AI判定）のブロックとArticle作成ループの間（目印）:

```python
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
```

これを以下に置き換える:

```python
        # ── フィルター4: Claude Haiku AI判定（タイトルのみ送信） ──────────
        titles_only = [c["title"] for c in candidates]
        topic_label = content_topic or "女性KPOPアイドル"
        approved_indices = _ai_judge_batched(titles_only, api_key, topic_label=topic_label)
        skipped_ai = len(candidates) - len(approved_indices)

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

- [ ] **Step 5: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile rss_collector.py
```
Expected: エラーなし（出力なし）。

- [ ] **Step 6: ガチャパラフィードで実際に収集し、`thumbnail_url`が設定されることを確認する**

（ネットワーク接続が必要。接続できない環境の場合はこのステップをスキップし、Step 5の構文チェックのみで良しとする）

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
        print(a.id, a.title[:30], 'thumbnail=', bool(a.thumbnail_url))
"
```
Expected: エラーなく実行され、新規収集されたガチャパラ記事があれば`thumbnail=True`（OGP画像が取得できなかった記事は`thumbnail=False`のこともあり得るが、大半はTrueになる見込み）。除外キーワード（「オープン」「ゲームセンター」等）を含む記事が一覧に出ないことも確認する。

- [ ] **Step 7: 承認待ち画面で画像プレビューを確認する**

```bash
venv/Scripts/python.exe app.py
```
（バックグラウンド実行）

```bash
curl -s "http://localhost:5000/pending?account_id=2&tab=rss" -o /dev/null -w "%{http_code}\n"
```
Expected: `200`

`thumbnail_url`が設定された記事がある場合、レスポンスHTML内に`<img src="` を含むことを確認する:

```bash
curl -s "http://localhost:5000/pending?account_id=2&tab=rss" | grep -c '<img src='
```
Expected: 1以上（Step 6でOGP画像取得に成功した記事が承認待ちに残っている場合）。

アプリのバックグラウンドタスクを停止する。

- [ ] **Step 8: Commit**

```bash
git add rss_collector.py
git commit -m "$(cat <<'EOF'
feat: content_topicアカウントのRSS収集時にOGP画像を取得する

AI判定通過後の記事ページからog:imageを取得しArticle.thumbnail_urlに
保存する。承認待ち画面の画像プレビューはthumbnail_url/image_urlsを
使う既存の_build_image_listがそのまま拾うためテンプレート変更は不要。
KPOPアカウントは要約生成時に画像取得する既存フローのまま変更なし。
EOF
)"
```

---

## 完了確認

全タスク完了後、以下を満たしていることを確認する:

- [ ] ガチャパラRSS収集で「オープン」「ゲームセンター」「ゲーセン」を含む記事が保存されない
- [ ] AI関連度判定プロンプトに「イベント・展示会情報のみのもの」「トピックと直接関係のないニュース」の除外指示が含まれる
- [ ] `content_topic`設定済みアカウントの新規記事に、取得できた場合は`thumbnail_url`が設定される
- [ ] 承認待ち画面（`/pending?account_id=2`）でガチャ記事に画像プレビューが表示される（YouTube動画タブと同様の`_build_image_list`の仕組みをそのまま利用）
- [ ] KPOPアカウント（account_id=1）のRSS収集ロジック・画像取得タイミングに変更がない
