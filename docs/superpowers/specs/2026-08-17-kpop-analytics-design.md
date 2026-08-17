# KPOPアカウント分析機能 設計書

## 背景・目的

KPOPアカウント（account_id=1）の投稿について、グループ・メンバー単位、投稿時間帯単位でパフォーマンス（いいね数・閲覧数など）を可視化し、今後の投稿方針の判断材料にする。ガチャアカウント（account_id=2）は対象外。

## Threads API 事前調査結果

**投稿単位（Media Insights）** `GET /{threads-media-id}/insights?metric=...`
- 取得可能: `views`（閲覧数）, `likes`, `replies`, `reposts`, `quotes`, `shares`
- **`saved`（保存数）に相当する指標は存在しない** → 今回の要件から保存数は除外する

**アカウント単位（User Insights）** `GET /{threads-user-id}/threads_insights?metric=...&since=...&until=...`
- 取得可能: `views`（プロフィール閲覧数、期間指定の時系列）, `likes`/`replies`/`reposts`/`quotes`（累計）, `clicks`, `followers_count`, `follower_demographics`
- `since`/`until`はUnixタイムスタンプ。`views`は日単位の期間指定で取得する想定

既存の`engagement_tracker.py`は`_fetch_insights()`で`views,likes,replies,reposts,quotes`を取得しているが、`refresh_engagement()`は`views`をArticleに保存していない（likes/replies/reposts/quotesのみ）。このモジュールは全アカウント・無期限で動き続ける既存機能であり、本設計では**変更しない**（後述の新規`post_stats`トラッキングと役割が異なるため併存させる）。

## データモデル

```python
class Group(db.Model):
    __tablename__ = "groups"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    normalized_name = db.Column(db.String(100), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Member(db.Model):
    __tablename__ = "members"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    normalized_name = db.Column(db.String(100), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("group_id", "normalized_name"),)


class PostStat(db.Model):
    __tablename__ = "post_stats"
    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey("articles.id"), nullable=False, index=True)
    day_index = db.Column(db.Integer, nullable=False)        # 投稿からの経過日数（0〜7）
    likes = db.Column(db.Integer, default=0)
    views = db.Column(db.Integer, default=0)
    replies = db.Column(db.Integer, default=0)
    reposts = db.Column(db.Integer, default=0)
    quotes = db.Column(db.Integer, default=0)
    is_final = db.Column(db.Boolean, nullable=False, default=False)  # 7日確定値フラグ
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)


class DailyStat(db.Model):
    __tablename__ = "daily_stats"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, nullable=False, index=True)
    stat_date = db.Column(db.Date, nullable=False)
    followers_count = db.Column(db.Integer, nullable=True)
    views_count = db.Column(db.Integer, nullable=True)   # その日のプロフィール閲覧数（API由来）
    source = db.Column(db.String(10), nullable=False, default="api")  # "api" / "manual"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("account_id", "stat_date"),)
```

`Group`/`Member`/`PostStat`/`DailyStat`は新規テーブルのため`db.create_all()`で自動作成される。`articles`への追加カラムは既存の`_migrate_db()`パターンに1行ずつ追加する:

```python
("group_id", "INTEGER"),
("member_id", "INTEGER"),
```

FK制約は付けない（`account_id`など既存カラムと同じ緩い結合の慣習に合わせる）。

## グループ・メンバー正規化ルール

`normalized_name`は「前後空白除去 → `unicodedata.normalize("NFKC", ...)`（全角/半角統一）→ 小文字化」した文字列。表示用の`name`は最初に登録された表記をそのまま保持する（後から表記ゆれで入力されても`name`は上書きしない、`normalized_name`が一致すれば既存レコードを再利用する）。

`member.normalized_name`の一意性は`group_id`単位（同名メンバーが別グループに存在してもよい）。

## グループ・メンバータグ付けフロー

対象は**account_id=1（KPOPアカウント）の承認待ち記事のみ**。`pending.html`側の判定は既存の`nav_is_kpop_account`（`content_topic`未設定＝KPOPアカウントという既存規約）をそのまま使う。

1. 「承認」ボタン押下時、KPOPアカウントであれば軽量モーダル（Bootstrap modal）を表示する。グループ名・メンバー名の2つの自由入力欄（`<input list="group-suggestions">`でこれまで登録済みのグループ名を`<datalist>`としてサジェスト表示。選択式ではなく引き続き自由入力可能）。両方とも空欄可。
2. モーダルの「承認してキューへ」ボタンで既存の`fetch('/articles/'+id+'/approve', ...)`を、`group_name`・`member_name`をJSON bodyに含めて送信するよう拡張する。
3. サーバ側`approve_article()`:
   - `group_name`が空文字なら`article.group_id = None`のまま（メンバーも同様）
   - `group_name`があれば正規化キーで`Group`を検索、なければ新規作成して`article.group_id`にセット
   - `member_name`がある場合は`group_name`も必須（`group_name`が空なのに`member_name`だけある場合は`member_name`を無視し警告ログのみ、エラーにはしない）。グループ確定後、そのグループ配下で`Member`を検索、なければ新規作成して`article.member_id`にセット
   - `member_name`が空欄の場合は`article.member_id = None`のまま（＝グループ単位集計のみ対象、個人集計には含めない、の要件を`member_id IS NULL`で自然に表現する）
4. Gachaアカウント（`nav_is_kpop_account`がFalse）の場合はモーダルを出さず、従来通り即Ajax承認する（変更なし）。

## 投稿ごとの7日間パフォーマンス追跡

新規モジュール`post_stats_tracker.py`。

```python
def track_post_stats(app) -> dict:
    """account_id=1 の posted 記事のうち、7日確定値がまだ出ていないものを対象に
    Threads Media Insights を取得し post_stats に1行追加する。"""
```

- 対象クエリ: `Article.account_id == 1`, `Article.status == "posted"`, `Article.posted_at IS NOT NULL`, かつその`article_id`に`is_final=True`の`PostStat`行がまだ存在しない記事
- 各対象記事について:
  - `day_index = (datetime.utcnow() - article.posted_at).days`
  - Media Insights（`views,likes,replies,reposts,quotes`）を取得
  - `PostStat`に新規行を追加（`day_index`, 取得した指標, `is_final = day_index >= 7`）
  - `time.sleep(0.3)`でレート制限考慮（`engagement_tracker.py`と同じ作法）
- `is_final=True`が一度付いた記事は、上記の対象クエリの絞り込み条件（`is_final=True`の行が存在しない）により自動的に以後の対象から外れる＝APIを叩かなくなる
- 1記事1日1回の実行前提のため、同日に複数回ジョブが走っても`day_index`が同じ行が重複しうる点は許容する（日次cronの想定実行回数は1回のみのため実運用上は発生しない。念のため、同日重複防止はスコープ外とする）

`scheduler.py`に日次ジョブを追加（既存の`engagement_daily`2:00 JST、`video_cleanup`3:00 JSTと重複しない2:30 JST）:

```python
scheduler.add_job(
    _post_stats_job,
    CronTrigger(hour=2, minute=30, timezone="Asia/Tokyo"),
    args=[app], id="post_stats_daily", replace_existing=True,
)
```

この履歴テーブルは記事ごとに追記され続ける設計のため、要件にある「7日間確定値データが投稿のたびに蓄積されていく」を自然に満たす（`is_final=True`の行が全記事分、時系列で溜まっていく）。

## 日次フォロワー・閲覧数スナップショット

同じく`post_stats_tracker.py`内に追加:

```python
def snapshot_daily_stats(app) -> dict:
    """account_id=1 の User Insights から本日分の followers_count・views を取得し
    daily_stats に upsert する（同日重複は更新で上書き）。"""
```

- User Insights取得: `metric=followers_count,views`、`views`は`since`=当日0:00 JST、`until`=翌0:00 JST（Unixタイムスタンプ）
- `(account_id=1, stat_date=today)`で既存行があれば`source="api"`の行のみ上書き更新（手動投入した`source="manual"`行は上書きしない。日付が衝突した場合はAPI取得値を優先せず手動値を保持する＝過去の手動バックフィルを壊さない）
- ジョブは3:30 JSTに追加（`post_stats_daily`2:30、`engagement_daily`2:00、`video_cleanup`3:00と重複しない）

**過去分の手動投入**: 分析ダッシュボード内に簡易フォーム（日付・フォロワー数・閲覧数〈任意〉）を設置し、`POST /analytics/daily-stats/add`で`source="manual"`として`DailyStat`に追加する。正確な日付が不明な場合はユーザーが大まかな日付を入力する運用とする（バリデーションで日付の過去/未来制限はしない）。

## 分析ダッシュボード

新規ルート`/analytics`（GET）。**account_id=1固定**で動作する（アカウント切り替えの影響を受けない。ガチャアカウント選択中でもURLを直接叩けば表示できるが、サイドバーのリンクは`nav_is_kpop_account`のときのみ表示するため通常は迷わない）。

サイドバー・モバイルナビバー両方に「分析」リンクを追加（`{% if nav_is_kpop_account %}`条件、`hooks_page`と同じ場所に配置）。

表示内容:

1. **フォロワー数・累計閲覧数の推移グラフ**: `daily_stats`を`stat_date`昇順で取得し、Chart.jsの折れ線グラフ2本（フォロワー数、閲覧数）。閲覧数は日別値をそのまま折れ線にする（「累計」の解釈は日別推移として表現し、必要なら画面側で日別値の累積和も併記する）
2. **グループ別・メンバー別の平均いいね数・投稿数**: `PostStat.is_final == True`の行を`Article`経由で`Group`/`Member`にJOINし、グループ単位（`article.group_id`でGROUP BY）・メンバー単位（`article.member_id IS NOT NULL`のものだけを`member_id`でGROUP BY）それぞれで「平均いいね数・平均閲覧数・投稿数」のテーブルを表示。平均いいね数の降順でソート
3. **投稿時刻の時間帯別平均パフォーマンス**: `Article.posted_at`（UTC naive保存）をJST時刻に変換した上で「時」（0〜23）だけを動的に抽出しGROUP BY（`strftime('%H', datetime(posted_at, '+9 hours'))`をSQLiteで使用）。`PostStat.is_final == True`の行の平均いいね数・平均閲覧数を時間帯別の棒グラフ・テーブルで表示。投稿スケジュール変更に対しても再集計だけで追従する（ハードコードされた時間帯ラベルは持たない）

Chart.jsはCDN経由で読み込む（プロジェクトに現状チャートライブラリがなく、Bootstrap/Bootstrap Iconsと同じCDN読み込みパターンに揃える）。

## ルーティング一覧（新規・変更分）

| メソッド・パス | 内容 |
|---|---|
| `POST /articles/<int:id>/approve`（既存を拡張） | JSON body に`group_name`・`member_name`（任意）を追加受理。マスタ自動作成ロジックを実行してから`queued`へ |
| `GET /analytics` | 分析ダッシュボード表示（account_id=1固定） |
| `POST /analytics/daily-stats/add` | `daily_stats`への手動データ投入（`source="manual"`） |

## 影響を受けないもの

- `engagement_tracker.py` / `_engagement_job`（全アカウント・無期限で稼働する既存の指標取得。今回の`post_stats_tracker.py`とは完全に独立）
- `video_cleanup`ジョブ（`article.like_count`を参照する既存ロジックのまま。`post_stats`テーブルとは無関係）
- ガチャアカウント（account_id=2）の承認フロー（モーダルを出さず現行のまま即Ajax承認）
- 既存の`Article.view_count`（YouTube動画の再生数フィルタ専用として現状の用途のまま。`post_stats.views`とは別物）

## テスト・検証方針

pytest等のフレームワークがないため、`venv/Scripts/python.exe`での手動検証と、Flask開発サーバー起動しての画面確認を行う。

1. マイグレーション実行後、`groups`/`members`/`post_stats`/`daily_stats`テーブルが作成され、`articles.group_id`/`member_id`が追加されることをDBスキーマ確認で検証
2. `Group`/`Member`の正規化・自動作成ロジックを単体で実行し、「IVE」→「ive 」のような表記ゆれ入力が同一`group_id`に解決されることを確認
3. テスト記事を1件作成し承認モーダル経由でグループ・メンバーを入力→`article.group_id`/`member_id`が正しくセットされること、メンバー未入力時は`member_id`がNoneのままであることを確認
4. `post_stats_tracker.track_post_stats()`をテスト記事（`posted_at`を7日以上前に偽装したもの）に対して実行し、1回の実行で`is_final=True`の行が作成され、2回目の実行では対象から除外される（API呼び出しが発生しない）ことをログで確認
5. `snapshot_daily_stats()`を実行し`daily_stats`に本日分の行が作成されること、既存の`source="manual"`行を上書きしないことを確認
6. `/analytics`にアクセスし、テストデータに基づくグループ別・時間帯別の集計値が期待通り計算されることを確認（特に投稿時刻のJST変換・時間帯グルーピングが正しいか）

## 既知の環境上の注意点

- `app.py`は`app = create_app()`をモジュール読み込み時に実行する。`test_client()`での検証には`from app import app`を使うこと
- 開発サーバー（ポート5000）はセッションを跨いで古いプロセスが残り続けることがある。新たに`python app.py`/`python run.py`を起動しないこと

## スコープ外（今回やらないこと）

- 保存数（Threads APIに存在しないため）
- ガチャアカウント（account_id=2）向けの同等機能
- グループ・メンバーマスタの一覧編集・削除・統合（表記ゆれで誤って別グループが立ってしまった場合の手動マージ機能は将来検討）
- `daily_stats`の`views_count`を「投稿ごとの累計閲覧数の合計」として厳密に検算する仕組み（Threads Account Insightsの`views`をそのまま日次値として採用する）
