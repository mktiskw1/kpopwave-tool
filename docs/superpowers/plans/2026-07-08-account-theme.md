# アカウント別カラーテーマ切り替え Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `kpopwave.daily`（account_id=1）選択時にサイドバー等の主要UI要素をピンク〜ラベンダー系テーマに切り替え、他のアカウント選択時は現行の緑テーマを維持する。

**Architecture:** `templates/base.html` の `:root` に集約されたCSSカスタムプロパティを唯一の配色情報源とし、ハードコードされた緑色値をすべて変数参照に整理したうえで、`.theme-kpop` クラスがそれらを上書きする。`<body>` タグに `nav_active_account_id`（既存のcontext processor値）に応じてJinjaで条件付きクラスを付与する。

**Tech Stack:** Flask, Jinja2, 素のCSSカスタムプロパティ（ビルドツールなし）

## Global Constraints

- このプロジェクトにはpytest等の自動テストフレームワークが存在しない。検証は一時スクリプト（curl等でレンダリング結果を確認）または手動ブラウザ確認で行う
- コメントは書かない（WHYが非自明な場合のみ1行）
- Windows環境。シェルコマンドは `venv/Scripts/python.exe` を使う
- `.alert-danger` / `.alert-info` の文字色 `#1B5E20` は意図的に変数化しない（テーマに関わらず据え置き。設計書「スコープ外」参照）
- `--surface`（白）と `--peach`（オレンジ）はテーマ共通で変更しない

---

## ファイル構成

- 変更: `templates/base.html` のみ（`<style>` 内のCSSカスタムプロパティ・個別ルール、および `<body>` タグ）

新規ファイルは作成しない。

---

### Task 1: CSS変数の追加とハードコード値の置き換え（見た目は変えない下準備）

**Files:**
- Modify: `templates/base.html`（`<style>` 内、複数箇所）

**Interfaces:**
- Produces: `:root` に新規追加される3変数 `--accent-rgb`（`74, 158, 107`）, `--accent2-rgb`（`46, 125, 50`）, `--accent-dark`（`#1B5E20`）。Task 2はこれらを `.theme-kpop` で上書きする前提で使う。

このタスクは**見た目を一切変えない**リファクタリング。既存の緑ハードコード値を、新しく追加する変数の参照に置き換えるだけ。

- [ ] **Step 1: `:root` に3つの変数を追加する**

`templates/base.html:11-27` の現在のコード:
```css
    :root {
      --accent:        #4a9e6b;
      --accent-hover:  #3a8558;
      --accent-light:  #a8d5b5;
      --accent-soft:   #e8f5ed;
      --accent2:       #2e7d32;
      --accent2-light: #81c784;
      --accent3:       #26A69A;
      --accent3-light: #80CBC4;
      --peach:         #FFA726;
      --bg:            #f0f9f4;
      --surface:       #FFFFFF;
      --surface2:      #e8f5ed;
      --border:        #b8ddc8;
      --text:          #1a3a2a;
      --text-muted:    #5a8a6a;
    }
```

これを以下に置き換える:
```css
    :root {
      --accent:        #4a9e6b;
      --accent-hover:  #3a8558;
      --accent-light:  #a8d5b5;
      --accent-soft:   #e8f5ed;
      --accent2:       #2e7d32;
      --accent2-light: #81c784;
      --accent3:       #26A69A;
      --accent3-light: #80CBC4;
      --accent-dark:   #1B5E20;
      --accent-rgb:    74, 158, 107;
      --accent2-rgb:   46, 125, 50;
      --peach:         #FFA726;
      --bg:            #f0f9f4;
      --surface:       #FFFFFF;
      --surface2:      #e8f5ed;
      --border:        #b8ddc8;
      --text:          #1a3a2a;
      --text-muted:    #5a8a6a;
    }
```

- [ ] **Step 2: `.nav-link:hover` / `.nav-link.active` を変数参照に置き換える**

`templates/base.html:68-76` の現在のコード:
```css
    .nav-link:hover {
      color: var(--accent);
      background: rgba(74, 158, 107, 0.08);
    }
    .nav-link.active {
      color: var(--accent);
      background: linear-gradient(135deg, rgba(74,158,107,.13), rgba(46,125,50,.13));
      box-shadow: inset 2px 0 0 var(--accent);
    }
```

これを以下に置き換える:
```css
    .nav-link:hover {
      color: var(--accent);
      background: rgba(var(--accent-rgb), 0.08);
    }
    .nav-link.active {
      color: var(--accent);
      background: linear-gradient(135deg, rgba(var(--accent-rgb),.13), rgba(var(--accent2-rgb),.13));
      box-shadow: inset 2px 0 0 var(--accent);
    }
```

- [ ] **Step 3: `.card` の box-shadow を変数参照に置き換える**

`templates/base.html:80-85` の現在のコード:
```css
    .card {
      background: var(--surface);
      border: 1.5px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 3px 18px rgba(74, 158, 107, 0.06);
    }
```

これを以下に置き換える:
```css
    .card {
      background: var(--surface);
      border: 1.5px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 3px 18px rgba(var(--accent-rgb), 0.06);
    }
```

- [ ] **Step 4: `.table-hover` を変数参照に置き換える**

`templates/base.html:106-109` の現在のコード:
```css
    .table-hover > tbody > tr:hover {
      background: rgba(74, 158, 107, 0.03);
      --bs-table-bg-state: rgba(74, 158, 107, 0.03);
    }
```

これを以下に置き換える:
```css
    .table-hover > tbody > tr:hover {
      background: rgba(var(--accent-rgb), 0.03);
      --bs-table-bg-state: rgba(var(--accent-rgb), 0.03);
    }
```

- [ ] **Step 5: `.article-card:hover` を変数参照に置き換える**

`templates/base.html:141-144` の現在のコード:
```css
    .article-card:hover {
      border-left-color: var(--accent);
      box-shadow: 0 4px 20px rgba(74, 158, 107, 0.1);
    }
```

これを以下に置き換える:
```css
    .article-card:hover {
      border-left-color: var(--accent);
      box-shadow: 0 4px 20px rgba(var(--accent-rgb), 0.1);
    }
```

- [ ] **Step 6: `.btn-accent` とその `:hover` を変数参照に置き換える**

`templates/base.html:170-181` の現在のコード:
```css
    .btn-accent {
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent2) 100%);
      border: none;
      color: #fff;
      box-shadow: 0 3px 14px rgba(74, 158, 107, 0.28);
    }
    .btn-accent:hover {
      background: linear-gradient(135deg, var(--accent-hover) 0%, #1B5E20 100%);
      color: #fff;
      box-shadow: 0 5px 18px rgba(74, 158, 107, 0.4);
      transform: translateY(-1px);
    }
```

これを以下に置き換える:
```css
    .btn-accent {
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent2) 100%);
      border: none;
      color: #fff;
      box-shadow: 0 3px 14px rgba(var(--accent-rgb), 0.28);
    }
    .btn-accent:hover {
      background: linear-gradient(135deg, var(--accent-hover) 0%, var(--accent-dark) 100%);
      color: #fff;
      box-shadow: 0 5px 18px rgba(var(--accent-rgb), 0.4);
      transform: translateY(-1px);
    }
```

- [ ] **Step 7: `.btn-primary:hover` を変数参照に置き換える**

`templates/base.html:205-208` の現在のコード:
```css
    .btn-primary:hover {
      background: linear-gradient(135deg, var(--accent2), #1B5E20);
      border: none; color: #fff;
    }
```

これを以下に置き換える:
```css
    .btn-primary:hover {
      background: linear-gradient(135deg, var(--accent2), var(--accent-dark));
      border: none; color: #fff;
    }
```

- [ ] **Step 8: フォームフォーカスリングを変数参照に置き換える**

`templates/base.html:283-287` の現在のコード:
```css
    input:focus, textarea:focus,
    .form-control:focus, .form-select:focus {
      border-color: var(--accent-light) !important;
      box-shadow: 0 0 0 3px rgba(74, 158, 107, 0.12) !important;
    }
```

これを以下に置き換える:
```css
    input:focus, textarea:focus,
    .form-control:focus, .form-select:focus {
      border-color: var(--accent-light) !important;
      box-shadow: 0 0 0 3px rgba(var(--accent-rgb), 0.12) !important;
    }
```

- [ ] **Step 9: モバイルナビの `.mobile-nav-bar a.active` を変数参照に置き換える**

`templates/base.html:365-369` の現在のコード:
```css
    .mobile-nav-bar a.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
      background: rgba(74, 158, 107, .06);
    }
```

これを以下に置き換える:
```css
    .mobile-nav-bar a.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
      background: rgba(var(--accent-rgb), .06);
    }
```

- [ ] **Step 10: サイドバー内「動画収集」ボタンのインラインstyleを変数参照に置き換える**

`templates/base.html:495-500` の現在のコード:
```html
      <form action="{{ url_for('collect_videos') }}" method="post" class="mt-2">
        <button type="submit" class="btn btn-sm w-100"
                style="background:linear-gradient(135deg,#4a9e6b,#2e7d32);border:none;color:#fff;border-radius:16px;font-weight:700;box-shadow:0 2px 10px rgba(74,158,107,.25)">
          <i class="bi bi-camera-video-fill"></i> 動画収集
        </button>
      </form>
```

これを以下に置き換える:
```html
      <form action="{{ url_for('collect_videos') }}" method="post" class="mt-2">
        <button type="submit" class="btn btn-sm w-100"
                style="background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;border-radius:16px;font-weight:700;box-shadow:0 2px 10px rgba(var(--accent-rgb),.25)">
          <i class="bi bi-camera-video-fill"></i> 動画収集
        </button>
      </form>
```

- [ ] **Step 11: サイドバー内「再生数フィルタ」枠のインラインstyleを変数参照に置き換える**

`templates/base.html:502-503` の現在のコード:
```html
      <!-- 再生数フィルタ -->
      <div class="mt-2 px-1" style="background:rgba(74,158,107,.06);border:1px solid rgba(74,158,107,.25);border-radius:10px;padding:8px 10px!important">
```

これを以下に置き換える:
```html
      <!-- 再生数フィルタ -->
      <div class="mt-2 px-1" style="background:rgba(var(--accent-rgb),.06);border:1px solid rgba(var(--accent-rgb),.25);border-radius:10px;padding:8px 10px!important">
```

- [ ] **Step 12: 検証（見た目が変わっていないことを確認する）**

Run: `cd "C:\Users\mktis\kpopwave-tool" && venv\Scripts\python.exe -c "
import app as app_module
client = app_module.app.test_client()
r = client.get('/')
body = r.get_data(as_text=True)
assert r.status_code == 200
assert 'rgba(74, 158, 107' not in body, '置き換え漏れがあります'
assert 'rgba(74,158,107' not in body, '置き換え漏れがあります（スペースなし）'
assert '#1B5E20' in body, 'alert-danger/alert-info用の#1B5E20は残っているはず'
assert body.count('#1B5E20') == 2, f'#1B5E20は2箇所(alert-danger, alert-info)だけ残るはず。実際: {body.count(chr(35)+\"1B5E20\")}箇所'
assert '--accent-rgb:    74, 158, 107' in body
assert '--accent2-rgb:   46, 125, 50' in body
assert '--accent-dark:   #1B5E20' in body
print('ALL PASS')
"`

Expected: `ALL PASS` が出力される（`run.py` のファイル監視により、保存時点で自動的にサーバーが再起動されているはず。反映されない場合は `venv\Scripts\python.exe app.py` を一度手動起動してから再度確認する）

- [ ] **Step 13: Commit**

```bash
git add templates/base.html
git commit -m "refactor: ハードコードされた緑色コードをCSS変数参照に整理"
```

---

### Task 2: `.theme-kpop` テーマの適用

**Files:**
- Modify: `templates/base.html`（`<style>` 内に新規ブロック追加、`<body>` タグ）

**Interfaces:**
- Consumes: Task 1で追加された `--accent-rgb` / `--accent2-rgb` / `--accent-dark` 変数。既存の `nav_active_account_id`（`app.py` の `inject_globals()` が全ページへ注入済み、`int | None`）

- [ ] **Step 1: `.theme-kpop` ブロックを `:root` の直後に追加する**

`templates/base.html` の `:root { ... }` ブロック（Task 1 Step 1で更新済み、28行目付近の閉じ `}` の直後）に、次のブロックを追加する。追加前後の文脈:

現在のコード（Task 1完了後の状態）:
```css
      --text:          #1a3a2a;
      --text-muted:    #5a8a6a;
    }

    /* ─── Base ─────────────────────────────────────────────────── */
```

これを以下に置き換える:
```css
      --text:          #1a3a2a;
      --text-muted:    #5a8a6a;
    }

    .theme-kpop {
      --accent:        #c9699f;
      --accent-hover:  #a84f81;
      --accent-light:  #e6a9c9;
      --accent-soft:   #fbeef6;
      --accent2:       #8f6bb8;
      --accent2-light: #bfa3dd;
      --accent3:       #7b93cf;
      --accent3-light: #aebde3;
      --accent-dark:   #7a3f60;
      --accent-rgb:    201, 105, 159;
      --accent2-rgb:   143, 107, 184;
      --bg:            #fdf6fb;
      --surface2:      #fbeef6;
      --border:        #e6c6db;
      --text:          #4a2a42;
      --text-muted:    #a97a97;
    }

    /* ─── Base ─────────────────────────────────────────────────── */
```

- [ ] **Step 2: サイドバー背景グラデーションと `.btn-secondary` の `.theme-kpop` 個別上書きを追加する**

`templates/base.html` の `.mobile-nav-bar a.active { ... }` ブロック（Task 1 Step 9で更新済み）の直後、`</style>` の直前に追加する。追加前後の文脈:

現在のコード（Task 1完了後の状態）:
```css
    .mobile-nav-bar a.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
      background: rgba(var(--accent-rgb), .06);
    }
  </style>
```

これを以下に置き換える:
```css
    .mobile-nav-bar a.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
      background: rgba(var(--accent-rgb), .06);
    }

    /* ─── kpopwave.daily テーマの個別上書き ───────────────────────── */
    .theme-kpop .sidebar {
      background: linear-gradient(175deg, #fdf7fb 0%, #fbeef6 55%, #f7e3f0 100%);
    }
    .theme-kpop .btn-secondary {
      background: linear-gradient(135deg, #e6a9c9, #c9699f);
    }
    .theme-kpop .btn-secondary:hover {
      background: linear-gradient(135deg, #c9699f, #a84f81);
    }
  </style>
```

- [ ] **Step 3: `<body>` タグに条件付きクラスを追加する**

`templates/base.html:372` の現在のコード:
```html
<body>
```

これを以下に置き換える:
```html
<body class="{% if nav_active_account_id == 1 %}theme-kpop{% endif %}">
```

- [ ] **Step 4: 検証スクリプトを書いて実行する（一時ファイル、コミットしない）**

`C:\Users\mktis\AppData\Local\Temp\claude\verify_theme.py` に保存:
```python
from flask import session
import app as app_module

flask_app = app_module.app
flask_app.config["TESTING"] = True
client = flask_app.test_client()

# account_id=1 (kpopwave.daily) 選択時: theme-kpopクラスが付与される
with client.session_transaction() as sess:
    sess["active_account_id"] = 1
r1 = client.get("/")
body1 = r1.get_data(as_text=True)
assert r1.status_code == 200
assert '<body class="theme-kpop">' in body1, "account_id=1でtheme-kpopクラスが付与されていません"
assert "--accent:        #c9699f;" in body1, ".theme-kpopブロックのピンク色定義が見つかりません"
print("OK: account_id=1 -> theme-kpop クラス付与を確認")

# 田中（仮）など account_id != 1 選択時: theme-kpopクラスは付与されない
with client.session_transaction() as sess:
    sess["active_account_id"] = 2
r2 = client.get("/")
body2 = r2.get_data(as_text=True)
assert r2.status_code == 200
assert '<body class="theme-kpop">' not in body2, "account_id=2でtheme-kpopクラスが付与されてしまっています"
assert '<body class="">' in body2, "account_id=2でbodyタグの形式が想定と異なります"
print("OK: account_id=2 -> theme-kpop クラス非付与を確認")

print("ALL PASS")
```

- [ ] **Step 5: 検証スクリプトを実行する**

Run: `cd "C:\Users\mktis\kpopwave-tool" && venv\Scripts\python.exe "C:\Users\mktis\AppData\Local\Temp\claude\verify_theme.py"`

Expected: `ALL PASS` が最後に出力される

Note: `account_id=2` が実際に存在しない場合（DBのアカウント構成によって異なる）でも、`_selected_account_id()` はセッション上のIDが `ThreadsAccount.query.get()` で見つからない場合はレガシーデフォルトにフォールバックする（`app.py` の既存ロジック）。その場合は `body2` の中身がレガシーアカウント（`account_id=1`のkpopwave.daily自体である可能性がある）を指す可能性があるため、事前に `ThreadsAccount` に `account_id=1` 以外の有効なアカウントが存在することを以下で確認してから実行する:

```bash
cd "C:\Users\mktis\kpopwave-tool" && venv\Scripts\python.exe -c "
import app as app_module
from database import ThreadsAccount
with app_module.app.app_context():
    ids = [a.id for a in ThreadsAccount.query.filter_by(is_active=True).all()]
    print('active account ids:', ids)
"
```
2件以上あることを確認したうえで、上記検証スクリプトの `sess[\"active_account_id\"] = 2` の `2` を、実際に存在する「kpopwave.daily以外」のIDに置き換えて実行する。

- [ ] **Step 6: 検証スクリプトを削除する**

Run: `rm "C:\Users\mktis\AppData\Local\Temp\claude\verify_theme.py"`

- [ ] **Step 7: アプリを起動してブラウザで手動確認する**

Run: `cd "C:\Users\mktis\kpopwave-tool" && venv\Scripts\python.exe app.py`（`run.py` 経由で既に起動中の場合は不要）

ブラウザで `http://localhost:5000/` を開き、以下を確認する:
- サイドバーのアカウント切り替えドロップダウンで `kpopwave.daily` を選択する
- サイドバー背景、アクティブなナビリンク、「RSS収集」「動画収集」ボタン、「再生数フィルタ」枠がピンク〜ラベンダー系に変わることを確認
- ブラウザの開発者ツールで `<body>` タグに `class="theme-kpop"` が付いていることを確認
- ドロップダウンで「田中（仮）」に戻すと、緑テーマに戻ることを確認
- 承認待ち・投稿キュー・スケジュールなど他のページに移動しても、選択中のテーマが維持されることを確認
- アラート（成功/エラーメッセージ）やステータスバッジの色が、どちらのテーマでも変化しないことを確認（意図的にスコープ外）

アプリを手動起動した場合は `Ctrl+C` で停止する。

- [ ] **Step 8: Commit**

```bash
git add templates/base.html
git commit -m "feat: kpopwave.daily選択時にピンク〜ラベンダー系テーマを適用"
```
