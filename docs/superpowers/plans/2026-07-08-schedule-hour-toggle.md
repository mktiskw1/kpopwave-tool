# 週間スケジュール時刻選択トグルボタン化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `templates/schedule.html` の投稿時刻入力を、自由テキスト入力（"HH:MM"手打ち）から0〜23時のトグルボタン（複数選択可）に置き換える。既存の保存・スケジューラ機能はそのまま動かす。

**Architecture:** チェックボックスの `value` を `"HH:00"` にすることで、既存の `app.py` `schedule()` ルートのPOST処理（`request.form.getlist(f"times_{day}")` で複数値を受け取り"HH:MM"としてパースする既存契約）をそのまま満たす。バックエンドの保存・スケジューラロジックは無変更。GET処理側に、現在の設定時刻からどの時間ボタンをチェック済み表示にするかを算出する処理だけを追加する。

**Tech Stack:** Flask / Jinja2 / Bootstrap 5.3（`.btn-check` パターンを流用したチェックボックス→ボタン風トグル）/ 素のJavaScript（既存の `queue.html` 等と同様、フレームワークなし）

## Global Constraints

- コメントは原則書かない（WHYが非自明な場合のみ1行）
- 既存ファイルを編集する（新ファイル作成なし。今回は `app.py` と `templates/schedule.html` の2ファイルのみ）
- 必要な変更だけ行う（`scheduler.py` および `schedule()` のPOST処理は無変更）
- Shell: WindowsなのでPowerShell構文。ただし動作確認コマンドはGit Bash経由の `curl` を使う
- このプロジェクトに自動テスト基盤（pytest等）は存在しない。各タスクの検証は開発サーバーを起動しての手動確認（`curl` / DB直接確認）で行う
- 開発サーバーは `run.py`（ファイル変更を検知して `app.py` を自動再起動するウォッチャー）経由で起動する。`app.py` や `templates/*.html` を編集すると自動的に再起動される（デバウンス2秒）ため、編集のたびに手動で再起動する必要はない
- 分単位の時刻指定機能は今回廃止し、24時間単位のみになる。既存の分単位データ（例: `"15:21"`）は時間部分（`"15"`）が選択済み扱いになり、保存し直すと `"15:00"` に正規化される

---

## File Structure

- **Modify: `app.py:966-977`** — `schedule()` ルートのGET処理に、曜日ごとの「選択済み時間（0〜23の2桁文字列の集合）」を算出する `checked_hours` を追加してテンプレートに渡す
- **Modify: `templates/schedule.html`** — 曜日カード内の自由入力リストをトグルボタングリッドに置き換え、関連JS（`addTime`/`removeTime`/`validateTime` を削除し `onHourToggle`/`updateCount`/`updatePreview`/`applyAllDays` に置き換え）、レスポンシブ列指定を `col-6 col-md-4 col-lg-3` から `col-12 col-md-6 col-lg-3` に変更

---

### Task 1: バックエンドGET処理に `checked_hours` を追加する

**Files:**
- Modify: `app.py:966-977`

**Interfaces:**
- Consumes: `scheduler.get_weekly_schedule(app, account_id) -> dict`（既存関数。例: `{"mon": ["12:00", "18:00", "21:00"], ...}`）
- Produces: テンプレート変数 `checked_hours: dict[str, set[str]]`（例: `{"mon": {"12", "18", "21"}, ...}`）。Task 2 のテンプレートはこの変数を `checked_hours.get(day, [])` として参照する

- [ ] **Step 1: `app.py` の `schedule()` 関数のGET処理部分を修正する**

`app.py` の以下のブロック（967-977行目付近）：

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

を、以下に置き換える：

```python
    _DAY_LABELS = {
        "mon": "月", "tue": "火", "wed": "水", "thu": "木",
        "fri": "金", "sat": "土", "sun": "日",
    }
    current = get_weekly_schedule(app, account_id)
    checked_hours = {
        day: {t[:2] for t in times if len(t) >= 2}
        for day, times in current.items()
    }
    return render_template(
        "schedule.html",
        schedule=current,
        day_keys=_DAY_KEYS,
        day_labels=_DAY_LABELS,
        checked_hours=checked_hours,
    )
```

- [ ] **Step 2: 開発サーバーが起動していることを確認する（未起動なら起動する）**

Run: `netstat -ano | grep ":5000" | grep LISTENING`

すでに何か表示されればサーバーは起動中（このプランの以降のタスクでも同じサーバーを使い回す）。何も表示されない場合は起動する：

Run（バックグラウンド実行。末尾に手動で `&` を付けない）:
```bash
source venv/Scripts/activate && python run.py > /tmp/run_watcher.log 2>&1
```

Run（起動待ち）:
```bash
for i in $(seq 1 15); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/schedule 2>/dev/null)
  if [ "$code" != "000" ]; then echo "ready: $code"; break; fi
  sleep 1
done
```

Expected: `ready: 200`

起動後、プロセスが1つだけであることを確認する：

Run: `netstat -ano | grep ":5000" | grep LISTENING`

Expected: 1行のみ表示される（複数行表示された場合は片方を `taskkill //PID <pid> //F` で停止してから再確認する）

- [ ] **Step 3: この時点ではテンプレートが `checked_hours` を使っていないため、既存のスケジュール画面が引き続き正常表示されることだけを確認する**

Run: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5000/schedule`

Expected: `200`

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "$(cat <<'EOF'
feat: 週間スケジュールGET処理に時間チェック状態(checked_hours)を追加

トグルボタンUI化の前段として、既存の設定時刻からどの時間(0-23)が
選択済みかを算出する処理を追加する。POST処理・scheduler.pyは無変更。
EOF
)"
```

---

### Task 2: `templates/schedule.html` をトグルボタングリッドUIに置き換える

**Files:**
- Modify: `templates/schedule.html`（全体）

**Interfaces:**
- Consumes: Task 1 で追加された `checked_hours: dict[str, set[str]]`。既存の `schedule: dict`, `day_keys: list[str]`, `day_labels: dict[str, str]`（`app.py` `schedule()` から渡される、無変更）
- Produces: フォーム送信時のフィールド名は既存と同じ `times_{day}`（`day` は `mon`/`tue`/.../`sun`）。値は `"HH:00"` 形式の文字列（例: `"07:00"`）。この契約は `app.py` の `schedule()` POST処理（Task対象外・無変更）が既に正しく処理する

- [ ] **Step 1: `templates/schedule.html` を全面的に書き換える**

ファイル全体を以下の内容に置き換える：

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
<div class="alert alert-secondary py-2 mb-4" style="font-size:.82rem">
  <i class="bi bi-info-circle me-1"></i>
  曜日ごとに投稿時刻を設定します。承認した記事は次の空きスロットに自動でスケジュールされます。
  実際の投稿時刻は設定値から <strong>±30分</strong> ずれます（人間っぽい投稿タイミング）。
</div>

<form method="post" id="schedule-form">
<div class="row g-3 mb-4">

  {% set day_colors = {
    "mon": "#AB47BC", "tue": "#F06292", "wed": "#26A69A",
    "thu": "#FFA726", "fri": "#CE93D8",
    "sat": "#4FC3F7", "sun": "#E91E8C"
  } %}

  {% for day in day_keys %}
  {% set label = day_labels[day] %}
  {% set color = day_colors[day] %}
  {% set times = schedule.get(day, []) %}

  <div class="col-12 col-md-6 col-lg-3" id="day-col-{{ day }}">
    <div class="card h-100" style="border-color:{{ color }}40">
      <!-- 曜日ヘッダー -->
      <div class="card-header d-flex justify-content-between align-items-center py-2"
           style="background:{{ color }}18;border-bottom-color:{{ color }}40">
        <span class="fw-bold" style="font-size:1.1rem;color:{{ color }}">{{ label }}曜日</span>
        <span class="badge" style="background:{{ color }}33;color:{{ color }};font-size:.72rem">
          <span class="slot-count-{{ day }}">{{ times|length }}</span>件
        </span>
      </div>

      <div class="card-body p-2">
        <!-- 時刻トグルボタン -->
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
      </div>
    </div>
  </div>
  {% endfor %}

</div><!-- /row -->

<!-- 保存ボタン -->
<div class="d-flex align-items-center gap-3">
  <button type="submit" class="btn btn-accent px-4">
    <i class="bi bi-save-fill me-1"></i>スケジュールを保存
  </button>
  <button type="button" class="btn btn-outline-secondary btn-sm" onclick="applyAllDays()">
    <i class="bi bi-arrow-repeat me-1"></i>平日すべてに現在の月曜設定を適用
  </button>
  <span class="text-muted" style="font-size:.78rem">保存後すぐに反映されます</span>
</div>

</form>

<!-- 今週のプレビュー -->
<div class="card mt-4">
  <div class="card-header" style="font-size:.85rem">
    <i class="bi bi-eye me-1"></i>今後のスケジュールプレビュー（ゆらぎ前）
  </div>
  <div class="card-body p-0">
    <div class="table-responsive">
      <table class="table table-sm mb-0" style="font-size:.82rem">
        <thead>
          <tr>
            {% for day in day_keys %}
            <th class="text-center py-2" style="min-width:80px">{{ day_labels[day] }}</th>
            {% endfor %}
          </tr>
        </thead>
        <tbody>
          <tr id="preview-row">
            {% for day in day_keys %}
            <td class="text-center align-top py-2" id="preview-{{ day }}">
              {% for t in schedule.get(day, []) %}
              <div class="badge mb-1" style="background:var(--surface2);color:var(--text);border:1px solid var(--border);display:block;font-size:.75rem">{{ t }}</div>
              {% else %}
              <span class="text-muted" style="font-size:.75rem">—</span>
              {% endfor %}
            </td>
            {% endfor %}
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

{% endblock %}

{% block scripts %}
<style>
  .hour-toggle-grid { display:grid; grid-template-columns:repeat(6,1fr); gap:6px; }
  .hour-btn-label {
    display:flex; align-items:center; justify-content:center;
    height:34px; border-radius:8px; font-weight:700; font-size:.78rem;
    cursor:pointer; user-select:none; transition:all .15s;
    border:1.5px solid var(--border); color:var(--text-muted); background:var(--surface);
  }
  .hour-check:checked + .hour-btn-label {
    background:var(--day-color); border-color:var(--day-color); color:#fff;
  }
</style>
<script>
function onHourToggle(day) {
  updateCount(day);
  updatePreview(day);
}

function updateCount(day) {
  const checked = document.querySelectorAll(`input[name="times_${day}"]:checked`);
  document.querySelector(`.slot-count-${day}`).textContent = checked.length;
}

function updatePreview(day) {
  const checked = document.querySelectorAll(`input[name="times_${day}"]:checked`);
  const cell = document.getElementById(`preview-${day}`);
  const times = Array.from(checked).map(i => i.value).sort();
  if (times.length === 0) {
    cell.innerHTML = '<span class="text-muted" style="font-size:.75rem">—</span>';
  } else {
    cell.innerHTML = times.map(t =>
      `<div class="badge mb-1" style="background:var(--surface2);color:var(--text);border:1px solid var(--border);display:block;font-size:.75rem">${t}</div>`
    ).join('');
  }
}

function applyAllDays() {
  const monChecked = new Set(
    Array.from(document.querySelectorAll('input[name="times_mon"]:checked')).map(i => i.value)
  );
  ['tue','wed','thu','fri'].forEach(day => {
    document.querySelectorAll(`input[name="times_${day}"]`).forEach(input => {
      input.checked = monChecked.has(input.value);
    });
    updateCount(day);
    updatePreview(day);
  });
}
</script>
{% endblock %}
```

- [ ] **Step 2: 自動再起動を待ち、ページが200で返ることを確認する**

`run.py` のファイル監視により2秒程度で `app.py`（Task 1で編集済み）は既に反映済み、`templates/schedule.html` の変更もテンプレートはリクエストの都度読み込まれるため即座に反映される。

Run: `sleep 2; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5000/schedule`

Expected: `200`

- [ ] **Step 3: 既存の設定時刻に対応するボタンが選択済み（`checked`属性あり）で描画されていることを確認する**

現在DBには `weekly_schedule` として全曜日 `["12:00", "18:00", "21:00"]` が保存されている（`app.py` の `_migrate_db`/初期設定または過去の保存操作による）。月曜の12時・18時・21時のチェックボックスに `checked` が付いていることを確認する：

Run:
```bash
curl -s http://localhost:5000/schedule | grep 'id="time-mon-12"'
curl -s http://localhost:5000/schedule | grep 'id="time-mon-18"'
curl -s http://localhost:5000/schedule | grep 'id="time-mon-21"'
curl -s http://localhost:5000/schedule | grep 'id="time-mon-9"'
```

Expected: 最初の3つの出力に `checked` が含まれる。最後（9時、非選択のはず）の出力には `checked` が含まれない

- [ ] **Step 4: 24個すべてのボタンが各曜日に描画されていることを確認する**

Run: `curl -s http://localhost:5000/schedule | grep -o 'class="btn-check hour-check"' | wc -l`

Expected: `168`（24時間 × 7曜日）

- [ ] **Step 5: 削除したはずの旧UI要素（自由入力・追加ボタン）が残っていないことを確認する**

Run: `curl -s http://localhost:5000/schedule | grep -c 'time-input\|addTime(\|removeTime('`

Expected: `0`

- [ ] **Step 6: Commit**

```bash
git add templates/schedule.html
git commit -m "$(cat <<'EOF'
feat: 週間スケジュールの時刻入力をトグルボタン方式に変更

0〜23時のボタンから複数選択する方式に変更し、自由テキスト入力
(HH:MM手打ち)を廃止する。送信するvalueは既存と同じ"HH:00"形式の
ためバックエンド(schedule()のPOST処理・scheduler.py)は無変更。
分単位の時刻指定は廃止(既存データは時間部分のみ選択済み扱いになり、
保存し直すとHH:00に正規化される)。
EOF
)"
```

---

### Task 3: エンドツーエンドの手動確認

**Files:**
- なし（コード変更なし。Task 1〜2 の統合動作確認のみ）

**Interfaces:**
- Consumes: Task 1（`checked_hours`）, Task 2（トグルボタンUI）

- [ ] **Step 1: 保存前のDB状態を記録する**

Run:
```bash
SCRATCH="C:\Users\mktis\AppData\Local\Temp\claude\C--Users-mktis-kpopwave-tool\985dfedf-e68b-4779-889b-1bd32bd3a15a\scratchpad"
python -c "
import sqlite3
conn = sqlite3.connect('instance/rock_metal.db')
c = conn.cursor()
c.execute(\"SELECT value FROM settings WHERE key='weekly_schedule'\")
with open(r'$SCRATCH\before_save.txt','w',encoding='utf-8') as f:
    f.write(repr(c.fetchone()))
"
cat "$SCRATCH/before_save.txt"
```

Expected: `weekly_schedule` に現在の値（全曜日 `["12:00", "18:00", "21:00"]`）が表示される

- [ ] **Step 2: フォーム送信をcurlでシミュレートし、月曜だけ時刻を変更して保存する（火〜日は現状維持のため既存の値をそのまま送る）**

`schedule()` のPOST処理は `times_{day}` の複数値を受け取る。月曜は `07:00, 12:00` の2つ、他の曜日は既存通り `12:00, 18:00, 21:00` を送信する：

Run:
```bash
curl -s -D - -o /dev/null -X POST http://localhost:5000/schedule \
  -d "times_mon=07:00" -d "times_mon=12:00" \
  -d "times_tue=12:00" -d "times_tue=18:00" -d "times_tue=21:00" \
  -d "times_wed=12:00" -d "times_wed=18:00" -d "times_wed=21:00" \
  -d "times_thu=12:00" -d "times_thu=18:00" -d "times_thu=21:00" \
  -d "times_fri=12:00" -d "times_fri=18:00" -d "times_fri=21:00" \
  -d "times_sat=12:00" -d "times_sat=18:00" -d "times_sat=21:00" \
  -d "times_sun=12:00" -d "times_sun=18:00" -d "times_sun=21:00" \
  | grep -i "^location"
```

Expected: `Location: /schedule`

- [ ] **Step 3: DBに月曜だけ変更された内容が保存されていることを確認する**

Run:
```bash
SCRATCH="C:\Users\mktis\AppData\Local\Temp\claude\C--Users-mktis-kpopwave-tool\985dfedf-e68b-4779-889b-1bd32bd3a15a\scratchpad"
python -c "
import sqlite3, json
conn = sqlite3.connect('instance/rock_metal.db')
c = conn.cursor()
c.execute(\"SELECT value FROM settings WHERE key='weekly_schedule'\")
data = json.loads(c.fetchone()[0])
with open(r'$SCRATCH\after_save.txt','w',encoding='utf-8') as f:
    f.write(repr(data['mon']) + chr(10))
    f.write(repr(data['tue']) + chr(10))
"
cat "$SCRATCH/after_save.txt"
```

Expected: 1行目 `['07:00', '12:00']`、2行目 `['12:00', '18:00', '21:00']`

- [ ] **Step 4: 保存後の画面で、月曜が新しい選択状態（7時・12時が選択済み、18時・21時は非選択）で描画されることを確認する**

Run:
```bash
curl -s http://localhost:5000/schedule | grep 'id="time-mon-7"'
curl -s http://localhost:5000/schedule | grep 'id="time-mon-12"'
curl -s http://localhost:5000/schedule | grep 'id="time-mon-18"'
```

Expected: 1・2行目の出力に `checked` が含まれ、3行目の出力には含まれない

- [ ] **Step 5: DBの状態をTask開始前の値に戻す（テスト用の変更を残さない）**

Run:
```bash
SCRATCH="C:\Users\mktis\AppData\Local\Temp\claude\C--Users-mktis-kpopwave-tool\985dfedf-e68b-4779-889b-1bd32bd3a15a\scratchpad"
python -c "
import sqlite3
conn = sqlite3.connect('instance/rock_metal.db')
c = conn.cursor()
c.execute('''UPDATE settings SET value=? WHERE key='weekly_schedule' ''',
          ('{\"mon\": [\"12:00\", \"18:00\", \"21:00\"], \"tue\": [\"12:00\", \"18:00\", \"21:00\"], \"wed\": [\"12:00\", \"18:00\", \"21:00\"], \"thu\": [\"12:00\", \"18:00\", \"21:00\"], \"fri\": [\"12:00\", \"18:00\", \"21:00\"], \"sat\": [\"12:00\", \"18:00\", \"21:00\"], \"sun\": [\"12:00\", \"18:00\", \"21:00\"]}',))
conn.commit()
c.execute(\"SELECT value FROM settings WHERE key='weekly_schedule'\")
with open(r'$SCRATCH\restored.txt','w',encoding='utf-8') as f:
    f.write(repr(c.fetchone()))
"
cat "$SCRATCH/restored.txt"
```

Expected: Step 1 で記録した値と同じ内容が表示される

- [ ] **Step 6: 「平日すべてに現在の月曜設定を適用」ボタンのJSロジックをコード確認する（この環境にはブラウザ操作ツールが無いため、クリック動作そのものは目視確認が必要）**

Run: `curl -s http://localhost:5000/schedule | grep -A8 "function applyAllDays"`

Expected: `monChecked` を `Set` として取得し、火〜金の各チェックボックスの `checked` をその集合の有無で設定するロジックが出力される（Task 2 Step 1 で書いたコードと一致することの確認）

- [ ] **Step 7: ブラウザで `http://localhost:5000/schedule` を開き、以下を目視確認する（このプランのコード変更はここまでで完了しているため、Step 7はコード変更を伴わない最終確認）**
  - 各曜日カードに0〜23の24個のボタンが6列で並んでいること
  - 既存の選択済み時間（12時・18時・21時、月曜のみ7時・12時）がアクセントカラーで塗りつぶされていること
  - ボタンをクリックするとON/OFFが切り替わり、右下の件数バッジと下部プレビュー表がリアルタイムに更新されること
  - 「平日すべてに現在の月曜設定を適用」を押すと火〜金のボタン状態が月曜と同期すること
  - ブラウザの画面幅を375px程度に狭めても、ボタングリッドが窮屈にならず操作できること
  - 「スケジュールを保存」を押すと保存され、リロード後も選択状態が維持されること

このTaskはコード変更を伴わないため、Step 5でのDB復元以外のコミットは不要
