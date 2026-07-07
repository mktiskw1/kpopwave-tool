# アカウント別カラーテーマ切り替え 設計書

日付: 2026-07-08

## 背景・目的

マルチアカウント対応（[[2026-07-07-account-switcher-design]]で実装済み）により、サイドバーの
ドロップダウンで「田中（仮）」「kpopwave.daily」を切り替えられるようになった。
現在はどちらのアカウントを選んでも同じ緑系デザインだが、`kpopwave.daily`（KPOP専用アカウント）
選択時は、薄いピンク〜ラベンダー系のやさしいカラーテーマに変更したい。

## 判定方法

- `nav_active_account_id`（`app.py` の `inject_globals()` context processor が全ページへ注入済み）が
  `kpopwave.daily` の `account_id`（現在は `1`）と一致する場合、`<body>` タグに `class="theme-kpop"` を付与する
- account_id での判定を採用する理由: `account_label`（表示名）は設定画面から変更され得るため、
  文字列一致より安定している
- `1` 以外の account_id（現在の「田中（仮）」、将来追加される他アカウントすべて）は
  クラス無指定＝現行の緑テーマがデフォルトとして適用される

## 現状のCSS構造（`templates/base.html`）

パレットは `:root` の CSS カスタムプロパティに集約されており、`.sidebar` `.nav-link` `.btn-*`
`.badge-*` など大半のスタイルはこれらの変数を `var(--accent)` 等で参照している。
一方で、変数を経由せず緑の16進色コード・rgba値を直接埋め込んでいる箇所が複数ある：

- `#1B5E20`（4箇所）: `.btn-accent:hover`, `.btn-primary:hover`, `.alert-danger` の文字色, `.alert-info` の文字色
- `rgba(74, 158, 107, X)`（`--accent` のRGB値、約10箇所）: `.nav-link:hover`, `.nav-link.active`,
  `.card` の box-shadow, `.table-hover`, `.article-card:hover`, `.btn-accent` の box-shadow,
  `.btn-accent:hover` の box-shadow, フォームフォーカスリング, `.mobile-nav-bar a.active`,
  サイドバー内「再生数フィルタ」枠のインラインstyle
- `rgba(46, 125, 50, .13)`（`--accent2` のRGB値、1箇所）: `.nav-link.active` のグラデーション
- サイドバー背景グラデーション `linear-gradient(175deg, #f5fbf7, #ecf6ef, #e2f0e7)`: 変数を一切使わない直書き
- サイドバー内「動画収集」ボタンのインライン `style="background:linear-gradient(135deg,#4a9e6b,#2e7d32)..."`
- `.btn-secondary` とその `:hover`: `#a8d5b5`/`#6db88a`/`#3a8558` を直書き

これらは `:root` の変数を上書きするだけではテーマが切り替わらないため、個別対応が必要。

## 設計

### 1. 新規CSS変数の追加（`:root` 内、既存変数の直後）

```css
--accent-rgb:  74, 158, 107;
--accent2-rgb: 46, 125, 50;
--accent-dark: #1B5E20;
```

既存の緑テーマの見た目は変えず、以下の置き換えのための土台とする：
- `rgba(74, 158, 107, X)` → `rgba(var(--accent-rgb), X)`（該当箇所すべて。`.alert-*` は
  背景色にこのrgbaを使っていないため対象なし）
- `rgba(46, 125, 50, .13)` → `rgba(var(--accent2-rgb), .13)`
- `#1B5E20` → `var(--accent-dark)`（**`.btn-accent:hover` と `.btn-primary:hover` の2箇所のみ**。
  `.alert-danger` / `.alert-info` の文字色に使われている `#1B5E20` は、後述のスコープ外の方針により
  意図的に据え置き、変数化しない）
- サイドバー内「動画収集」ボタンのインラインstyle・「再生数フィルタ」枠のインラインstyleも
  同様に `var(--accent)` / `var(--accent2)` / `rgba(var(--accent-rgb), X)` を使うよう書き換える

これにより、`.theme-kpop` でパレット変数一式を上書きするだけで大半の要素が自動的に再配色される。

### 2. `.theme-kpop` クラスの追加

`:root` の直後に、以下のパレット全体を上書きするブロックを追加する：

```css
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
```

`--surface`（カード背景の白）と `--peach`（pending系バッジのオレンジ）はテーマ共通のまま変更しない。

### 3. 変数化だけでは対応できない個別上書き

以下は変数を経由しない、または配色差が大きく専用の値が必要なため、`.theme-kpop` スコープの
セレクタとして個別に追加する：

```css
.theme-kpop .sidebar {
  background: linear-gradient(175deg, #fdf7fb 0%, #fbeef6 55%, #f7e3f0 100%);
}
.theme-kpop .btn-secondary {
  background: linear-gradient(135deg, #e6a9c9, #c9699f);
}
.theme-kpop .btn-secondary:hover {
  background: linear-gradient(135deg, #c9699f, #a84f81);
}
```

### 4. `<body>` タグへのクラス付与（`templates/base.html`）

`<body>` タグを以下のように変更する（Jinja条件式）：

```html
<body class="{% if nav_active_account_id == 1 %}theme-kpop{% endif %}">
```

account_id の `1` は将来的にマジックナンバー化を避けたい場合は `Setting` 等での設定化も
検討できるが、今回のスコープでは「kpopwave.daily = account_id 1」に限定した実装で十分とする
（YAGNI）。

## スコープ外（今回は変更しない）

意味を持つ状態色・他ブランドの色であり、サイトのブランドカラーではないため、テーマに関わらず統一する：
- アラート（`.alert-success` / `.alert-danger` / `.alert-warning` / `.alert-info`）の背景・枠線・文字色
- ステータスバッジ（`.badge-pending` / `.badge-queued` / `.badge-posted` / `.badge-rejected` / `.badge-failed`）
- YouTube収集ボタン（YouTube公式のブランドカラー、サイトテーマと無関係）

上記は `#1B5E20` を使っていても、意図的に変数化・上書き対象から外す（`.alert-danger` /
`.alert-info` の文字色に使われている `#1B5E20` は `var(--accent-dark)` に変数化しない）。

**実装後の追記（最終レビューで判明・修正済み）:** 当初 `.badge-queued` / `.badge-posted` /
`.badge-failed` / `.badge.bg-success` / `.badge.bg-danger` / `.alert-success`（枠線）/
`.alert-danger`（背景・枠線）/ `.alert-info`（背景・枠線）/ `.alert-secondary` は
`--accent2-light` 等の共有パレット変数を使っており、`.theme-kpop` 適用時に意図せず一部だけ
ピンク化されてしまっていた。上記「テーマに関わらず統一する」の方針に沿い、これらすべてを
現在の緑の実値でハードコードし直した（`.badge-pending` / `.badge-rejected` 等と同じ扱いに統一）。

一方、テーブルhover・フォームフォーカスリング・カードのbox-shadowなど、`rgba(74,158,107,X)` を
使った低opacityの装飾的な色味は、いずれも同じ `--accent` ブランドカラーの表現の一部であるため
`--accent-rgb` 変数化の対象に含め、「主要UI要素は完全にピンク化」の方針どおり `.theme-kpop`
選択時にはピンク寄りの色味に変わる（個別の上書きルールは不要で、変数の上書きだけで自動的に反映される）。

## テスト方針

- 手動確認: サイドバーのアカウント切り替えドロップダウンで `kpopwave.daily` を選択し、
  サイドバー背景・アクティブナビリンク・「RSS収集」「動画収集」ボタン・「再生数フィルタ」枠の
  配色がすべてピンク〜ラベンダー系に変わることを確認する
- 「田中（仮）」に戻すと緑テーマに戻ることを確認する
- スコープ外と定めたアラート・バッジ・YouTube収集ボタンは、どちらのテーマでも変化しないことを確認する
- ブラウザの開発者ツールで `<body>` のクラスと、実際に適用されているCSS変数の値
  （Computed styles）を確認する
