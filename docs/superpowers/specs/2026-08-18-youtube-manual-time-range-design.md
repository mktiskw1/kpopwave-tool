# YouTube手動追加の時間範囲指定ダウンロード 設計書

## 背景・目的

承認待ち画面ではなく、ダッシュボード（`templates/index.html`）の「YouTube動画URLを追加」フォーム（`POST /api/videos/add-manual`）で、長尺動画（例: 2時間22分）を丸ごとダウンロードしようとすると「ダウンロードファイルが見つかりません」エラーになる。任意で開始・終了時刻を指定し、その範囲だけをyt-dlpでダウンロードできるようにすることで、長尺動画を分割して収集できるようにする。

## yt-dlp実装方式

CLIの`--download-sections`はPython API上では`ydl_opts["download_ranges"]`キーに、`yt_dlp.utils.download_range_func`のインスタンス（呼び出し可能オブジェクト）を渡す形で実現する:

```python
from yt_dlp.utils import download_range_func
dl_opts["download_ranges"] = download_range_func([], [(start_seconds, end_seconds)])
```

`chapters`引数は空リスト（チャプター指定は使わない）、`ranges`引数は`(start, end)`の秒数タプルのリスト。`--force-keyframes-at-cuts`に相当するオプション（`force_keyframes_at_cuts`）は設定しない（ご指示通り、キーフレーム単位の多少のズレを許容する）。範囲を指定しない場合は`download_ranges`キー自体を`dl_opts`に含めない（既存動作を完全に維持）。

## 時刻入力パース

`"H:MM:SS"`・`"MM:SS"`・`"SS"`いずれの形式も受理する汎用パーサーを新設する:

```python
def _parse_time_input(s: str) -> float | None:
    """"H:MM:SS" / "MM:SS" / "SS" 形式の文字列を秒数(float)に変換する。空文字列はNone。"""
    s = (s or "").strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError("時刻の形式が不正です")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds
```

## 一意制約・ファイル名衝突への対応

`Article.url`にはDBの一意制約があり、同一動画URLに対して範囲違いで複数回追加すると2回目以降が一意制約違反になる。また保存先ファイル名`{video_id}.{ext}`は範囲指定の有無に関わらず動画IDのみで決まるため、範囲違いの2回目のダウンロードが1回目のファイルを上書きしてしまう。

**対応**: 範囲指定がある場合のみ、以下を行う（範囲未指定時は完全に既存動作のまま）:
- 保存する`Article.url`に`#t={start}-{end}`（開始・終了秒数を整数化した値、終了未指定なら`{start}-`）を付与し、元動画URLと衝突しないようにする
- 保存先ファイル名を`{video_id}_{start}-{end}.{ext}`とし、同一動画の異なる範囲ダウンロードが互いのファイルを上書きしないようにする
- 一時ダウンロードファイル名（`outtmpl`）にも同じサフィックスを付ける（`_find_downloaded_file`は`vid_id`の部分一致でファイルを検索するため、動画IDがファイル名に含まれてさえいれば検索は引き続き機能する）
- 既存の重複チェック（同一URLがpending/queued中なら拒否）は、範囲指定がある場合はスキップする（範囲違いは別物のクリップとして扱うため）

## フロントエンド変更（`templates/index.html`）

「YouTube動画URLを追加」カードのURL入力欄の下に、開始・終了の任意入力欄（`<input type="text">`、プレースホルダー「開始 (任意, 例: 1:00:00)」「終了 (任意, 例: 2:22:00)」）を追加する。`addVideoManual()`が両方の値を読み取り、空でなければ`fetch`のbodyに`start_time`・`end_time`として含める（空文字列のままなら送らない、または空文字列を送ってサーバー側で無視——既存の`data.get(...)`パターンに合わせて後者を採用）。

## バックエンド変更（`app.py`の`add_video_manual()`）

- `start_time`・`end_time`をリクエストボディから取得し`_parse_time_input`でパースする。パース失敗時は400エラー。両方指定時は`end > start`を検証する（不正なら400）
- 範囲指定がある場合、重複チェックをスキップする
- 範囲指定がある場合、`outtmpl`・`dest_filename`・保存する`Article.url`にサフィックスを付ける（上記参照）
- `dl_opts["download_ranges"]`を範囲指定時のみ設定する

## 影響を受けないもの

- 範囲未指定時の動作（URL・ファイル名・重複チェックとも完全に現状維持）
- `video_collector.py`の自動収集ロジック（チャンネル巡回収集は対象外、今回変更するのは手動追加フォームのみ）
- 承認待ち画面の動画トリミング機能（今回追加する範囲指定ダウンロードとは別の既存機能。粗い範囲指定はダウンロード段階、精密なカットは承認画面のトリミングで行うという役割分担を維持する）

## テスト・検証方針

pytest等のフレームワークがないため手動検証を行う:

1. `_parse_time_input`の単体動作をベンチスクリプトで確認（"1:00:00" → 3600.0、"2:22:00" → 8520.0、"90" → 90.0、空文字列 → None、不正値でValueError）
2. `download_range_func`が正しく呼び出されることをモックで確認
3. 実際に2時間22分程度の長尺動画で、範囲A（0:00〜1:00:00）→ 範囲B（1:00:00〜2:22:00）の順に2回追加を行い、両方とも「ダウンロードファイルが見つかりません」エラーにならず承認待ちに追加されること、2つの動画ファイルが互いに上書きされず両方とも承認待ち画面で確認できることを確認する
4. 範囲未指定での従来通りの動画追加（短い動画）が引き続き問題なく動作することを確認する

## スコープ外（今回やらないこと）

- 自動収集（`video_collector.py`のチャンネル巡回収集）への同様の範囲指定機能の追加
- 長尺動画の全体ダウンロード失敗自体の根本原因調査・修正（分割ダウンロードによる回避が今回の対応方針）
- 精密なカット処理（`force_keyframes_at_cuts`等）の追加
