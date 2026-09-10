# mktiskw.com に認証（Cloudflare Access / Zero Trust）を追加する手順

作成日: 2026-09-09
対象: ContentWave 管理画面（https://mktiskw.com）/ Cloudflare アカウント `Mktiskw1@gmail.com`

---

## ✅ 実施済み（2026-09-09、API 経由で設定完了）

| 項目 | 内容 |
|---|---|
| Zero Trust 組織 | **`mktiskw-contentwave`**（ログイン URL: `https://mktiskw-contentwave.cloudflareaccess.com`） |
| ログイン方法 | One-time PIN（メールに 6 桁コード） |
| Application ① | `ContentWave Admin` → `mktiskw.com`（全体）→ ポリシー `Allow` / email `mktiskw1@gmail.com` |
| Application ② | `ContentWave video (public)` → `mktiskw.com/video` → ポリシー `Bypass` / Everyone |
| Session Duration | ① `720h`（30日、2026-09-10 に 24h から延長 / API 経由）・② `24h` |

**動作確認結果:**
- `https://mktiskw.com/` → 302 で Access ログイン画面へリダイレクト（"Sign in ・ Cloudflare Access"）
- `https://mktiskw.com/settings` → 同上（ログイン要求）
- `https://mktiskw.com/video/<file>.mp4` → **200 OK（ログイン不要で素通り）**、`Content-Type: video/mp4`
- HEAD リクエスト（Threads の到達確認と同じ）→ 200 OK

**未実施（必要なら手動で）:**
- `/privacy` の Bypass（Meta アプリ審査に出す場合のみ）
- 許可メールアドレスの追加（`mktiskw1@gmail.com` 以外に増やす場合）
- CLOUDFLARE_API_TOKEN は `2026-09-16` 失効。以後の変更はダッシュボードか新トークンで。

> 以下は設定内容の背景説明とダッシュボードでの同等手順（参考）。

---

## 0. 結論（先に要点）

| 項目 | 結論 |
|---|---|
| ContentWave 本体のコード変更 | **不要** |
| Cloudflare トンネル設定（`config.yml`）の変更 | **不要** |
| ダッシュボードのトンネル / DNS ルーティング変更 | **不要** |
| 事前に必要な対応 | Access のポリシーで **`/video/*` を認証除外（Bypass）にする**（唯一の必須事項） |
| 作業場所 | すべて Cloudflare Zero Trust の Web ダッシュボード |

Cloudflare Access は Cloudflare のエッジ（`cloudflared` より手前）で動作するため、
トンネルの中身（`localhost:5000` へのルーティング）には一切影響しません。

---

## 1. 現在のトンネル設定の確認結果

### 1-1. `~/.cloudflared/config.yml`

```yaml
tunnel: feeb1c2e-21b0-4f3d-b325-3a58630c5602
credentials-file: C:\Users\mktis\.cloudflared\feeb1c2e-21b0-4f3d-b325-3a58630c5602.json

ingress:
  - hostname: mktiskw.com
    service: http://localhost:5000
  - service: http_status:404
```

- 名前付きトンネル（`contentwave` / ID `feeb1c2e-…`）。
- `mktiskw.com` → ローカル `http://localhost:5000`（Flask アプリ）へ素通し。
- **Access を入れてもこの設定は変更不要。** Access は「誰が `mktiskw.com` に到達できるか」をエッジで制御するだけで、
  到達後にトンネルへ流す部分（この YAML）は無関係。

### 1-2. `start_cloudflare.bat`

- `TUNNEL_NAME=contentwave` を `cloudflared tunnel run` で起動。
- 起動時に DB の `settings.app_base_url` を `https://mktiskw.com` に固定書き込み。
- **`--metrics` を指定していない** ため cloudflared のメトリクスポートはランダム
  （アプリ側は 2480 / 2000 / 20241 を順に走査。`threads_api.py:145`）。
  → このメトリクスポートは **localhost 限定でトンネル外には出ない**ので Access 除外の対象外。

### 1-3. アプリ側の認証状況

- `grep` で確認した結果、**アプリケーションレベルの認証は一切なし**
  （`login_required` / BASIC 認証 / `before_request` ガード等が存在しない）。
- 現状 URL を知っていれば誰でも全 API（削除・投稿・設定変更含む）を操作可能。
  → Access によるログインゲート追加は妥当かつ有効。

### 1-4. Access 追加で影響が出る箇所（重要）

| 経路 | 誰がアクセスするか | Access の影響 | 対応 |
|---|---|---|---|
| `GET /video/<filename>` (`app.py:1136`) | **Threads / Meta のサーバー**が動画投稿時に直接ダウンロード（`threads_api.py:462`） | Access が全体に掛かると Meta がログインできず **動画取得失敗 → 投稿スキップ** | **`/video` を Bypass（必須）** |
| `requests.head(video_url)` (`threads_api.py:478`) | 投稿前の到達確認（自分のPCから外→CF経由で戻る） | Access のログイン 302 が返り警告ログが出る（投稿自体は継続するが確認が無意味化） | 同上の `/video` Bypass で解決 |
| カルーセル / 単一画像投稿の `image_url` | Meta のサーバー | 画像URLは**外部（RSS・YouTubeサムネ等）**で `mktiskw.com` からは配信していない | **対応不要** |
| `GET /auth/threads/callback` (`app.py:2065`) | **自分のブラウザ**が Threads からリダイレクトされる | Access ログイン済みなら普通に通過。`code` パラメータもリダイレクトを跨いで保持される | 保護したまま可（除外不要） |
| `GET /privacy` (`app.py:2259`) | Meta アプリ審査担当（審査に出す場合のみ） | 非ログインで到達できなくなる | 審査予定があるなら `/privacy` も Bypass（任意） |
| cloudflared トンネルのヘルスチェック | Cloudflare ↔ cloudflared 間 | HTTP パスを叩かず制御コネクション上で完結 | **対応不要**（そもそもヘルスチェック用エンドポイントは未実装） |
| Flask セッション Cookie | ― | Access は独自の `CF_Authorization` Cookie を足すだけでアプリの Cookie に干渉しない | **対応不要** |

---

## 2. ContentWave 側で事前にやっておくこと

### 2-1. 必須：なし（コード変更ゼロ）

コードやトンネル設定の変更は不要。**必須なのは Access 側で `/video/*` を Bypass にすること**だけ
（手順は §3-6）。

### 2-2. 確認しておくと安心なこと

- **`app_base_url` は `https://mktiskw.com` のまま**でよい（変更しない）。
  `_try_refresh_tunnel_url()` は URL に `trycloudflare.com` を含むときだけ動くため、
  名前付きトンネル運用では非稼働。Access とは無関係。
- **OAuth 再認証は Access ログインを先に済ませてから**行うと確実。
  （手順：シークレットでない普段のブラウザで `https://mktiskw.com` を開く → Access ログイン →
  そのまま設定画面から Threads の認証をやり直す）
- 将来 `/healthz` などヘルスチェック用エンドポイントを追加した場合は、
  合わせて Access で Bypass にする（監視サービスがログインできないため）。

---

## 3. Zero Trust（Cloudflare Access）設定手順

> すべて https://one.dash.cloudflare.com（Zero Trust ダッシュボード）での作業。
> cloudflared の再起動もトンネル設定の編集も不要。

### 3-1. Zero Trust の初期化とチーム名の決定

1. Cloudflare ダッシュボード → 左メニュー **Zero Trust** を開く。
2. 初回のみオンボーディングで **チーム名（Team name）** を決める。
   - これが `https://<チーム名>.cloudflareaccess.com` というログイン用ドメインになる。
   - 例：`mktiskw` → ログインは `https://mktiskw.cloudflareaccess.com`
   - 後から変更可能だが、変更するとログインURLが変わるので最初に決めておく。
3. プラン選択で **Free**（50ユーザーまで無料）を選択。支払い方法の登録を求められるが課金は発生しない。

### 3-2. ログイン方法（identity）の確認

1. Zero Trust → **Settings → Authentication**。
2. **Login methods** に **One-time PIN** が最初から有効になっていることを確認。
   - これでメールアドレスに 6 桁コードが届く方式が使える（追加設定不要）。
   - Google ログイン等を使いたい場合はここで IdP を追加できるが、まずは One-time PIN で十分。

### 3-3. メインの Access Application（管理画面を保護）

1. Zero Trust → **Access → Applications → Add an application**。
2. **Self-hosted** を選択。
3. 設定：
   - **Application name**: `ContentWave Admin`
   - **Session Duration**: `24 hours`（好みで。長いほどログイン頻度が減る）
   - **Application domain**:
     - Subdomain: （空欄）
     - Domain: `mktiskw.com`
     - Path: （空欄）
   - **Identity providers**: `One-time PIN` にチェック
   - （その他はデフォルトのまま）
4. **Next** でポリシー作成へ。

### 3-4. アクセスポリシー（許可するメールアドレス）

1. **Policy name**: `Allow owner`
2. **Action**: `Allow`
3. **Configure rules** → **Include**:
   - Selector: `Emails`
   - Value: 許可したいメールアドレスを追加
     - 例：`mktiskw1@gmail.com`
     - 複数人に許可するなら 1 行ずつ追加。
     - ドメイン全体を許可したい場合は Selector を `Emails ending in` にして `@example.com`。
4. **Next → Add application** で保存。

> この時点で `https://mktiskw.com` 全体がログインゲートの内側に入る。

### 3-5. 動作確認（メイン）

1. シークレットウィンドウで `https://mktiskw.com` を開く。
2. Cloudflare Access のログイン画面 → メールアドレス入力 → 届いた 6 桁コード入力。
3. ContentWave の管理画面が表示されれば成功。
4. 許可していないメールアドレスでログインすると弾かれることも確認。

### 3-6. 【必須】動画配信パスを認証除外（Bypass）

Threads/Meta のサーバーが `https://mktiskw.com/video/<ファイル名>` を
ログインなしでダウンロードできるようにする。

1. Zero Trust → **Access → Applications → Add an application** → **Self-hosted**。
2. 設定：
   - **Application name**: `ContentWave video (public)`
   - **Application domain**:
     - Subdomain: （空欄）
     - Domain: `mktiskw.com`
     - Path: `video`  ← これで `mktiskw.com/video/*` が対象
3. ポリシー：
   - **Policy name**: `Bypass video`
   - **Action**: `Bypass`
   - **Include** → Selector: `Everyone`
4. 保存。

> **評価順について**：Cloudflare Access は「よりパス指定が具体的なアプリケーション」を優先して
> 評価する。`mktiskw.com/video` は `mktiskw.com`（パスなし）より具体的なので、
> `/video/*` へのリクエストは Bypass アプリが先に一致し、認証なしで通る。
> それ以外のパスはメインアプリ（§3-3）が一致してログインを要求する。

### 3-7. 動作確認（Bypass）

1. シークレットウィンドウで、実在する動画ファイルの URL を開く
   （例：`static/videos/` にあるファイル名を使って `https://mktiskw.com/video/xxxx.mp4`）。
   → **ログイン画面が出ずに動画がそのまま再生／ダウンロードされれば成功。**
2. `https://mktiskw.com/` や `https://mktiskw.com/settings` はログインが要求されることを再確認。
3. 管理画面から**動画投稿を 1 件テスト実行**し、Threads 側に動画付きで投稿されることを確認。
   （ログに「動画URL到達確認: HTTP 200」と出れば OK）

### 3-8. （任意）審査用に `/privacy` を公開

Meta アプリを審査に提出する予定がある場合のみ：

- §3-6 と同じ手順で、Path を `privacy` にした Bypass アプリ
  （`ContentWave privacy (public)` / `Bypass` / `Everyone`）を追加。

---

## 4. まとめ / チェックリスト

- [ ] Zero Trust 初期化・チーム名決定（例：`mktiskw`）
- [ ] Login method に One-time PIN が有効
- [ ] Application ①：`mktiskw.com`（パスなし）→ Allow / 自分のメール
- [ ] Application ②：`mktiskw.com/video` → **Bypass / Everyone**（必須）
- [ ] （任意）Application ③：`mktiskw.com/privacy` → Bypass / Everyone（審査提出時のみ）
- [ ] シークレットウィンドウで管理画面がログインゲートされることを確認
- [ ] `/video/xxx.mp4` がログインなしで取得できることを確認
- [ ] 動画投稿を 1 件テストして成功することを確認

**コード変更・トンネル設定変更・cloudflared 再起動：いずれも不要。**
