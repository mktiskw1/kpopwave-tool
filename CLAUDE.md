# 作業ルール(kpopwave-tool)

このリポジトリは、実機のWindows PC(`C:\Users\mktis\kpopwave-tool`)で動く Flask + SQLite のアプリです。
本番DBは `instance/rock_metal.db`、起動は `run.py`(.py / .html が変わると自動で再起動します)。

## 作業の始め
- 作業の最初に、実機のWindows PC(本番DB `instance/rock_metal.db` にアクセスできる環境)であることを確認する
  (ホスト名と作業フォルダを確認する)。

## DB
- DBを変える作業の前に、`instance/rock_metal.db` のバックアップを取る。
  SQLite の backup API を使う(ファイルを単純にコピーしない。WAL に未反映の最近のデータが抜けるため)。
  保存先は `instance/backup/` に日時つきの名前で置く。

## Jinjaテンプレート(.html)
- JSのコメントや文字列に `{%` や `{{` をそのまま書かない(Jinja に解釈されて壊れる)。
- 編集後に、アプリの Jinja 環境で全テンプレートをコンパイルして確認する
  (独自フィルターがあるので、素の Jinja2 ではなく `app.jinja_env` を使う)。

## 動作確認
- 動作確認は本番DBのコピーで行う(コピーも backup API で作る)。
- 検証のログは本番のログ(`logs/scheduler.log`)に書き込まない。
  `app` を import する前に `logging.handlers.RotatingFileHandler` を差し替えて、別ファイルへ出す。
- 検証中に、本物の Threads API や YouTube に偽の値(偽のトークンなど)でアクセスしない。

## 報告
- 報告に秘密の値(APIキー・トークン)を書かない。必要なら末尾4文字だけ書く。

## コードの変更の進め方
- 本番のフォルダ(`C:\Users\mktis\kpopwave-tool`)は、run.py が .py とテンプレートの変更で自動再起動する。
  作業途中のファイルを直接編集しない。
- コードの変更は git worktree(例: `C:\Users\mktis\kpopwave-tool-work`)で行い、本番DBのコピーで確認してからコミットする。
  最後に本番のフォルダで master に取り込み(fast-forward)、push する。
- 本番のフォルダに取り込む前に、全 .py の構文チェックとテンプレートのコンパイルを通す。
- 取り込みは、投稿の時刻(12:00・19:00・22:00 JST)の前後10分を避ける。

## 作業の終わり
- 作業の最後に必ずコミットと push をする。
- 報告の最後に、コミットのハッシュと push の結果を書く。
- コミットしなかった場合は、その理由を書く。
