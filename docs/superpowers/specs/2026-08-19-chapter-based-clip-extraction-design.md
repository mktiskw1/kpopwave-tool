# 長尺コンピレーション動画のチャプター単位自動クリップ抽出 設計書

## 背景・目的

KPOPアカウントのYouTube動画手動追加フォーム（`templates/index.html`の「YouTube動画URLを追加」、`app.py`の`add_video_manual()`）は、既にCookie認証・`js_runtimes`(node)・`remote_components`(ejs:github)込みの範囲指定ダウンロード（`download_ranges`）に対応済みである。複数曲を収録した長尺コンピレーション/メドレー動画（説明欄・チャプターに曲ごとのタイムスタンプが列挙されているもの）から、曲ごとに自動でクリップを切り出せるようにする。

## 事前検証結果

実際の長尺動画（`uaglTQ7aAoQ`、2時間22分）で`yt_dlp.YoutubeDL(...).extract_info()`の戻り値`chapters`キーを確認したところ、45個のチャプターが取得でき、`start_time`・`end_time`・`title`を持つ辞書のリストであることを確認した。タイトル形式は`"Red Velvet (레드벨벳) - Cosmic"`のように「グループ名（ハングル表記）- 曲名」がほとんどで、想定通りだった。

## 優先順位（ユーザー確認済み）

手入力の開始・終了時刻欄の**どちらかにでも値がある場合は、従来通りの単一範囲（またはフィールドが空欄の方は無制限側）ダウンロードを行い、チャプター検出は行わない**。**両方空欄の場合のみ**チャプター検出を試み、見つかればチャプター分割フロー、見つからなければ従来通り動画全体をダウンロードする。

## データモデル

```python
class ChapterJob(db.Model):
    __tablename__ = "chapter_jobs"
    id = db.Column(db.Integer, primary_key=True)
    source_url = db.Column(db.String(1000), nullable=False)
    video_id = db.Column(db.String(50), nullable=False)
    video_title = db.Column(db.String(500), nullable=True)
    thumbnail_url = db.Column(db.String(500), nullable=True)
    account_id = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="processing")  # processing / done / failed
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChapterClip(db.Model):
    __tablename__ = "chapter_clips"
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("chapter_jobs.id"), nullable=False, index=True)
    chapter_index = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(500), nullable=False)
    start_time = db.Column(db.Float, nullable=False)
    end_time = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending / downloading / done / failed
    video_file_path = db.Column(db.String(500), nullable=True)
    duration = db.Column(db.Float, nullable=True)
    guessed_group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
```

新規テーブルのため`db.create_all()`で自動作成される（`ALTER TABLE`不要）。

## 既存範囲ダウンロード処理の共通化

`add_video_manual()`内の範囲指定ダウンロード（`outtmpl`構築〜`ydl.download()`〜`_find_downloaded_file()`)を、新規ヘルパー関数に切り出す:

```python
def _download_youtube_range(yt_url: str, vid_id: str, start_time: float, end_time: float | None, suffix: str) -> tuple[str, str]:
    """指定範囲をダウンロードし、(ローカルの一時ファイルパス, 拡張子) を返す。失敗時は例外を送出する。"""
```

このヘルパーは`_YOUTUBE_COOKIE_FILE`・`_YT_DLP_JS_OPTS`（Cookie・js_runtimes・remote_components）を内部で使用する。`add_video_manual()`の既存の範囲指定分岐（`has_range`時）をこのヘルパー呼び出しに置き換え、新規のチャプタージョブからも同じヘルパーを呼ぶ。範囲未指定時（全体ダウンロード）の分岐は変更しない。置き換え後、既存の全モック検証・実動画検証を再実行し、退行がないことを確認する。

## グループ名の自動照合（既存マスタとの照合のみ、新規作成はしない）

```python
def _guess_group_id(chapter_title: str) -> int | None:
    """"グループ名 (ハングル等) - 曲名" 形式のチャプタータイトルから、既存groupsマスタと
    正規化キーで完全一致するものがあればそのidを返す。一致しなければNone（新規作成はしない）。"""
```

`" - "`で分割した前半部分を候補とし、末尾の`(...)`を除去した文字列・除去しない文字列の両方を、既存の`_normalize_tag_name`で正規化して`Group.normalized_name`と照合する（除去した方を優先的に試す）。どちらも一致しなければ`None`を返し、`ChapterClip.guessed_group_id`は`NULL`のままとする（承認モーダルでの手動タグ付けに委ねる）。メンバー名の推測は行わない。

## バックグラウンドジョブ

```python
def _run_chapter_job(app, job_id: int):
```

- `with app.app_context():`で全体を囲む
- `job_id`に紐づく`ChapterClip`を`chapter_index`順に1件ずつ処理する
- 各クリップについて: `status="downloading"`に更新 → `_download_youtube_range(...)`を呼ぶ → 成功時は`status="done"`・`video_file_path`・`duration`（ffprobeで取得）・`guessed_group_id`（`_guess_group_id(clip.title)`）を設定、失敗時は`status="failed"`・`error_message`を設定
- 各クリップ処理後（最後のクリップの後も含む）に`time.sleep(1.5)`を挟み、YouTube側への連続リクエストを緩和する
- 全クリップ処理後、`job.status = "done"`（個別クリップの失敗があってもジョブ自体は"done"とする。全クリップが失敗した場合のみ`job.status = "failed"`とする）

## ルーティング

| メソッド・パス | 内容 |
|---|---|
| `POST /api/videos/chapters/start` | `{url, account_id}`を受理。手入力なし前提。動画情報取得→チャプター有無判定。チャプターありなら`ChapterJob`/`ChapterClip`作成しバックグラウンドジョブ起動、`{ok: true, has_chapters: true, job_id}`を返す。チャプターなしなら`{ok: true, has_chapters: false}`を返す（フロントは既存の`/api/videos/add-manual`を呼ぶ）。動画情報取得自体が失敗すれば`{ok: false, error}` |
| `GET /api/videos/chapters/<int:job_id>/status` | ポーリング用。`{status, total, completed, failed}`を返す |
| `GET /videos/chapters/<int:job_id>` | 進捗表示＋選択画面（新規テンプレート`templates/chapter_clips.html`）。`job.status == "processing"`なら進捗表示＋自動ポーリングで完了時にリロード。完了していればチャプター一覧をチェックボックス付きで表示（タイトル・動画全体のサムネイル・長さ・グループ自動タグ付け結果） |
| `POST /videos/chapters/<int:job_id>/confirm` | 選択されたクリップIDのリストを受理。選択分は`Article(status="pending", ...)`を作成（`group_id`は`guessed_group_id`を引き継ぐ）。非選択・失敗分はファイル削除＋`ChapterClip`行削除。処理後`ChapterJob`と残った`ChapterClip`も削除し、`/pending`へリダイレクト |

`Article.url`にはDB一意制約があるため、同一動画から複数チャプターを登録する際、既存の範囲指定ダウンロード機能と同じ規約（`#t={start}-{end}`をURLフラグメントとして付与）で一意化する（`{source_url}#t={start}-{end}`）。ファイル名も同様に`{video_id}_{start}-{end}.{ext}`とする。

## フロントエンド変更（`templates/index.html`）

`addVideoManual()`を変更: 開始・終了欄が両方空欄の場合のみ、まず`POST /api/videos/chapters/start`を呼ぶ。`has_chapters: true`ならレスポンスの`job_id`で`/videos/chapters/<job_id>`へ遷移する。`has_chapters: false`なら（レスポンスに含まれる動画情報を再利用せず）そのまま既存の`/api/videos/add-manual`を呼ぶ従来の処理に進む。開始・終了のいずれかに値がある場合は、この判定を一切行わず既存の`/api/videos/add-manual`を直接呼ぶ（変更なし）。

## 影響を受けないもの

- `video_collector.py`のチャンネル自動巡回収集
- `add_video_manual()`の「範囲未指定（動画全体ダウンロード）」分岐そのもの
- 承認待ち画面のグループ・メンバー手動タグ付けモーダル（チャプター経由で登録された記事も、グループが空欄なら通常通りこのモーダルでタグ付けできる）

## テスト・検証方針

pytest等のフレームワークがないため手動検証を行う。前回までの調査でYouTube側のセッション認識が短時間の大量リクエストで不安定になる可能性が確認されているため、実際のダウンロードを伴う検証は必要最小限（実際に数チャプターだけ検証する等）に留める:

1. `_guess_group_id`の単体動作を、実際に取得済みのチャプタータイトル例（"Red Velvet (레드벨벳) - Cosmic"等）と、DBに存在する/しないグループ名で確認する
2. `_download_youtube_range`への置き換え後、既存の`add_video_manual`のモック検証（範囲あり・範囲なし双方）を再実行し、退行がないことを確認する
3. `POST /api/videos/chapters/start`をモックで検証し、チャプターありなら`ChapterJob`/`ChapterClip`が正しい件数・内容で作成されること、チャプターなしなら`has_chapters: false`が返ることを確認する
4. バックグラウンドジョブのロジックを、実ダウンロードをモックした状態で検証し、各クリップの状態遷移・ディレイ呼び出し・グループ自動タグ付けが正しく行われることを確認する
5. 実際の長尺コンピレーション動画で、チャプターのうち2〜3個程度に限定した軽量な実動作確認を行う（全45チャプターの実ダウンロードは行わない）。選択画面の表示・チェックボックスでの選択・確定後の承認待ち登録・非選択分のファイル削除を確認する

## スコープ外（今回やらないこと）

- メンバー名の自動推測（グループ名のみ自動化、ご指示通り）
- チャプターごとの個別サムネイル抽出（動画全体のサムネイルを全クリップで共通使用する）
- チャプター数の上限設定・大量チャプター動画への特別な最適化
- `video_collector.py`側（自動収集）への同様の機能追加
