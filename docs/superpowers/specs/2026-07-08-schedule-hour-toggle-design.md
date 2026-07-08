# 週間スケジュール 時刻選択トグルボタン化 設計書

日付: 2026-07-08

## 背景・目的

`schedule.html`（週間投稿スケジュール画面）では、投稿時刻を `<input type="text">` への手打ち（"HH:MM" 形式、正規表現バリデーション）で入力する形になっている。
これを 0時〜23時の24個のトグルボタンから複数選択する方式に変更し、入力の手間とタイプミスを無くす。

## スコープ

対象:
- `templates/schedule.html` の時刻入力UIをトグルボタン方式に変更
- `app.py` の `schedule()` ルートのGET処理に、現在の設定時刻をトグルボタンの選択状態に変換する処理を追加

対象外（今回は変更しない）:
- `app.py` の `schedule()` ルートのPOST処理（`request.form.getlist(f"times_{day}")` で複数値を受け取るロジックはそのまま）
- `scheduler.py`（`get_weekly_schedule` / `set_weekly_schedule` / ジョブ実行ロジック）
- 分単位の時刻指定機能（今回廃止。24時間単位のみになる）

## 設計

### 1. 送信データの形式（変更なし）

チェックボックスの `value` を `"HH:00"`（例: `"07:00"`）にすることで、既存の `schedule()` POST処理（`app.py:942-965`）が要求する「`times_{day}` という同名フィールドの複数値、各値が"HH:MM"形式」という契約をそのまま満たす。バックエンド側のパース・バリデーション・保存ロジックは無変更。

### 2. GET処理：選択状態の算出（`app.py` の `schedule()` 関数、967行目付近）

現在の週間スケジュール（`current = get_weekly_schedule(app, account_id)`、例: `{"mon": ["07:00", "15:21"], ...}`）から、曜日ごとに「どの時間（0〜23の2桁文字列）が選択済みか」を表す集合を算出し、`checked_hours` としてテンプレートへ渡す：

```python
checked_hours = {
    day: {t[:2] for t in times if len(t) >= 2}
    for day, times in current.items()
}
```

分単位の時刻（例: `"15:21"`）が過去に保存されていた場合、時間部分（`"15"`）が選択済み扱いになる。保存し直すと `"15:00"` に正規化される（分単位入力機能を廃止するための意図的な仕様）。

### 3. テンプレート：トグルボタングリッド（`templates/schedule.html`）

各曜日カード内の自由入力リスト（現在の47-69行目、`time-list-{{ day }}` と「追加」ボタン）を、Bootstrapの「非表示チェックボックス＋隣接labelをボタンとして表示」パターンで置き換える：

```html
<div class="hour-toggle-grid" style="--day-color:{{ color }}">
  {% for h in range(24) %}
  {% set hh = '%02d'|format(h) %}
  <input type="checkbox" class="btn-check hour-check" autocomplete="off"
         id="time-{{ day }}-{{ h }}" name="times_{{ day }}" value="{{ hh }}:00"
         {% if hh in checked_hours.get(day, []) %}checked{% endif %}
         onchange="onHourToggle('{{ day }}')">
  <label class="hour-btn-label" for="time-{{ day }}-{{ h }}">{{ hh }}</label>
  {% endfor %}
</div>
```

CSS（6列×4行グリッド、曜日ごとのアクセントカラーで選択状態を表現）：

```css
.hour-toggle-grid { display:grid; grid-template-columns:repeat(6,1fr); gap:6px; margin-bottom:.5rem; }
.hour-btn-label {
  display:flex; align-items:center; justify-content:center;
  height:34px; border-radius:8px; font-weight:700; font-size:.78rem;
  cursor:pointer; user-select:none; transition:all .15s;
  border:1.5px solid var(--border); color:var(--text-muted); background:var(--surface);
}
.hour-check:checked + .hour-btn-label {
  background:var(--day-color); border-color:var(--day-color); color:#fff;
}
```

### 4. JavaScript の置き換え

既存の `addTime` / `removeTime` / `validateTime` は不要になるため削除する。代わりに：

- `onHourToggle(day)`：チェックボックスの `change` イベントで呼ばれ、(a) `.slot-count-{day}` バッジをそのチェック済み数で更新、(b) `updatePreview(day)` を呼ぶ
- `updatePreview(day)`：`input[name="times_{day}"]:checked` の `value` を昇順ソートしてバッジ表示する（既存のプレビュー表描画ロジックを流用、入力値の正規表現チェックは不要になるので削除）
- `applyAllDays()`：月曜の `input[name="times_mon"]:checked` の `value` 集合を取得し、火〜金の各時間チェックボックスをその集合に応じて `checked` に設定、その後各曜日で `onHourToggle(day)` を呼ぶ

### 5. レスポンシブ対応

現状の曜日カードは `col-6 col-md-4 col-lg-3`（スマホで2カード/行）。24ボタンの6列グリッドが窮屈にならないよう、スマホ幅では1カード/行に広げる（`col-12 col-md-6 col-lg-3` に変更）。

## テスト方針

自動テスト基盤がないプロジェクトのため、開発サーバーでの手動確認を行う：

- 既存の設定時刻（例: 07:00, 15:00, 21:00）に対応するボタンが選択済み表示になっていること
- ボタンのON/OFF切り替えでプレビュー表・件数バッジがリアルタイム更新されること
- 保存後、DBの `weekly_schedule`（または `weekly_schedule_<account_id>`）設定に正しい形式で反映されること
- 「平日すべてに現在の月曜設定を適用」で火〜金のボタン状態が月曜と同期すること
- 保存時に `app.reschedule_post_jobs()` が呼ばれ、スケジューラに新しい時刻設定が反映されること
- スマホ幅（375px程度）でボタングリッドが窮屈にならず操作できること
- 分単位で保存されていた過去データがある場合、対応する時間のボタンが選択済みとして表示され、保存し直すと `HH:00` に正規化されること
