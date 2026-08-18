# 動画トリミング中の他操作ブロック解消 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** トリミング実行中・動画収集実行中でも他のHTTPリクエスト（ページ遷移、承認操作、トリミング進捗ポーリング）がブロックされないようにし、トリミング完了時の挙動を強制ページ遷移から非破壊的な通知に変更する。

**Architecture:** 根本原因はFlask開発サーバーが`threaded=False`（Werkzeugのデフォルト）で動作しており、同期的な収集ルート（`/collect`・`/collect-youtube`・`/collect-videos`）実行中は他の全リクエストが待たされること。`app.run(..., threaded=True)`を追加して解消する。トリミング完了時のUIは、`location.href`による強制遷移をやめ、既存の`trim-msg`要素に「一覧を更新」リンクを表示するだけに変更し、ボタンを再度有効化する。

**Tech Stack:** Python 3.x / Flask / Werkzeug / vanilla JS

## Global Constraints

- 対象ファイルは`app.py`・`templates/pending.html`のみ
- `_run_trim_job`・`trim_video`・`trim_job_status`ルート・`VideoTrimJob`モデル・同一記事への並行トリムジョブ防止ガードは変更しない
- 収集ルート（`/collect`等）自体の非同期化は今回のスコープ外
- 失敗時（`status === 'failed'`）の`alert()`表示は変更しない
- コメントは原則書かない（WHYが非自明な場合のみ1行）
- Shellコマンドの実行環境はWindows。Bashツールを使う場合はGit Bash構文（`$VAR`、`&&`可）、PowerShellツールを使う場合はPowerShell構文（`$env:VAR`、`&&`不可）に注意する
- このプロジェクトにはpytest等のテストフレームワークが存在しない。検証は`venv/Scripts/python.exe -c "..."`によるDB直接確認・`app.test_client()`によるHTTPリクエストで行う
- **重要**: `app.py`は`app = create_app()`をモジュール読み込み時に実行する。`test_client()`での検証には必ず`from app import app`を使うこと
- 開発サーバー（ポート5000）はセッションを跨いで既に起動中のプロセスが存在する可能性が高い。新たに`python app.py`/`python run.py`を起動しないこと（`run.py`のファイルウォッチャーがコード変更を検知して自動再起動するため、変更をコミット・保存するだけでよい）

---

## ファイル構成

| ファイル | 変更内容 |
|---|---|
| `app.py` | `app.run(...)`に`threaded=True`を追加 |
| `templates/pending.html` | `pollTrimJob`の完了時挙動を強制ページ遷移からボタン再有効化＋リンク表示に変更 |

---

### Task 1: `threaded=True`の追加とトリミング完了時UIの変更

**Files:**
- Modify: `app.py:2738`
- Modify: `templates/pending.html`（`pollTrimJob`関数内）

**Interfaces:**
- Consumes: 既存の`trim_job_status`エンドポイント（`GET /api/videos/trim-jobs/<job_id>`、変更なし）
- Produces: 変更なし（新規インターフェースは追加しない、既存動作の修正のみ）

- [ ] **Step 1: `app.py`の`app.run()`に`threaded=True`を追加**

`app.py:2738`（目印）:

```python
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000)
```

これを以下に置き換える:

```python
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000, threaded=True)
```

- [ ] **Step 2: `templates/pending.html`の`pollTrimJob`完了時挙動を変更**

`templates/pending.html`内、以下のブロック（目印。`pollTrimJob`関数内、`status === 'done'`の分岐）:

```javascript
        } else if (d.status === 'done') {
          var msg = document.getElementById('trim-msg-' + articleId);
          if (msg) msg.style.display = 'block';
          setTimeout(function() { location.href = '/pending?tab=video'; }, 1000);
        } else {
```

これを以下に置き換える:

```javascript
        } else if (d.status === 'done') {
          var msg = document.getElementById('trim-msg-' + articleId);
          if (msg) {
            msg.innerHTML = '✅ 完了！新しいクリップが承認待ちに追加されました　' +
              '<a href="/pending?tab=video" style="color:#2e7d32;text-decoration:underline;font-weight:700">一覧を更新</a>';
            msg.style.display = 'block';
          }
          if (btn) {
            btn.disabled = false;
            btn.innerHTML = origBtnHtml;
          }
        } else {
```

- [ ] **Step 3: 構文チェック**

```bash
venv/Scripts/python.exe -m py_compile app.py
```
Expected: エラーなし。

- [ ] **Step 4: `threaded=True`が渡っていることを確認する**

```bash
venv/Scripts/python.exe -c "
import re
with open('app.py', encoding='utf-8') as f:
    content = f.read()
m = re.search(r'app\.run\(([^)]*)\)', content)
print('app.run引数:', m.group(1))
print('threaded=True含む:', 'threaded=True' in m.group(1))
"
```
Expected: `threaded=True含む: True`。

- [ ] **Step 5: `pending.html`の変更内容を確認する**

```bash
venv/Scripts/python.exe -c "
from app import app

client = app.test_client()
r = client.get('/pending?tab=video')
html = r.get_data(as_text=True)
print('status:', r.status_code)
print('新しい完了メッセージのJSコードあり:', '完了！新しいクリップが承認待ちに追加されました' in html)
print('一覧を更新リンクのJSコードあり:', '一覧を更新' in html)
print('旧: location.hrefによる強制遷移コードが消えている:', \"location.href = '/pending?tab=video'\" not in html)
print('ボタン再有効化コードあり:', 'btn.disabled = false' in html)
"
```
Expected: `status: 200`、`新しい完了メッセージのJSコードあり: True`、`一覧を更新リンクのJSコードあり: True`、`旧: location.hrefによる強制遷移コードが消えている: True`（このプロジェクトの他のJS関数にも`btn.disabled = false`は複数箇所あるため、最後の確認は目視で`pollTrimJob`関数内に実際に存在することも確認すること）。

- [ ] **Step 6: 他ページが壊れていないことを確認する**

```bash
venv/Scripts/python.exe -c "
from app import app
client = app.test_client()
for path in ('/', '/pending', '/queue', '/settings', '/analytics'):
    r = client.get(path)
    print(path, '->', r.status_code)
"
```
Expected: 全パスとも`200`。

- [ ] **Step 7: 実際の動画トリミングで完了時の挙動を確認する（手動・目視）**

このプロジェクトには既に起動中の開発サーバー（`run.py`のファイルウォッチャー経由）が存在する前提のため、新規プロセスは起動しない。コミット後、ファイルウォッチャーが自動的に`app.py`を再起動する（2秒のデバウンス）。ブラウザで承認待ち画面の動画タブを開き、実際に動画を1本トリミングし、以下を確認する:

1. トリミング開始直後、他のページ（動画収集画面など）に問題なく遷移できること
2. トリミング処理中に「動画収集」ボタンを押しても、収集処理・トリミングの両方が滞りなく進行すること（`threaded=True`の効果確認）
3. トリミング完了時に強制的なページ遷移が発生せず、「✅ 完了！新しいクリップが承認待ちに追加されました　一覧を更新」というメッセージが表示され、切り取りボタンが再度押せる状態に戻ること
4. 「一覧を更新」リンクをクリックすると、`/pending?tab=video`に遷移し新しいクリップが表示されること

この手動確認はブラウザ操作が必要なため、実施できない場合はその旨を報告に明記し、Step 3〜6の自動検証結果をもって完了とする。

- [ ] **Step 8: Commit**

```bash
git add app.py templates/pending.html
git commit -m "$(cat <<'EOF'
fix: トリミング中に他の操作がブロックされる問題を解消

Flask開発サーバーがthreaded=False（Werkzeugのデフォルト）で
動作しており、同期的な収集ルート（RSS/YouTube/動画収集）実行中は
トリミングの進捗ポーリングやページ遷移を含む他の全リクエストが
待たされていたことが根本原因だった。threaded=Trueを追加して解消。
あわせてトリミング完了時の挙動を、強制的なページ全体遷移から、
ボタン再有効化＋「一覧を更新」リンク表示（手動遷移）に変更し、
他の記事を編集中のユーザーの作業が中断されないようにした。
EOF
)"
```

---

## 完了確認

- [ ] `app.run()`に`threaded=True`が渡っている
- [ ] トリミング完了時に強制的なページ遷移が発生しない
- [ ] トリミング完了時に「✅ 完了！新しいクリップが承認待ちに追加されました」＋「一覧を更新」リンクが表示され、切り取りボタンが再有効化される
- [ ] （可能であれば）実際のブラウザ操作で、トリミング中の動画収集・他ページ遷移がブロックされないことを確認済み
