#!/usr/bin/env python3
"""
Threads アクセストークン取得スクリプト

【Meta Developer Portal での設定（1回だけ）】
  アプリ → Threads API → 設定 → コールバック URL に以下を追加:
    http://localhost:8888/callback

使い方:
  .\\venv\\Scripts\\python get_token.py
"""

import http.server
import json
import os
import secrets
import sqlite3
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

REDIRECT_URI = "http://localhost:8888/callback"
SCOPE = "threads_basic,threads_content_publish"

_code: str | None = None
_error: str | None = None
_done = threading.Event()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _code, _error
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        qs = urllib.parse.parse_qs(parsed.query)
        if "error" in qs:
            _error = qs.get("error_description", qs.get("error", ["不明なエラー"]))[0]
            body = f"<h2 style='color:red'>認証エラー</h2><p>{_error}</p><p>このウィンドウを閉じてください。</p>"
        elif "code" in qs:
            _code = qs["code"][0]
            body = "<h2 style='color:green'>認証成功！</h2><p>このウィンドウを閉じてターミナルに戻ってください。</p>"
        else:
            body = "<h2>不明なレスポンス</h2>"

        html = f"<!DOCTYPE html><html><head><meta charset='utf-8'></head><body style='font-family:sans-serif;padding:2em'>{body}</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())
        _done.set()

    def log_message(self, *_):
        pass


def _start_server():
    srv = http.server.HTTPServer(("127.0.0.1", 8888), _Handler)
    srv.timeout = 1
    while not _done.is_set():
        srv.handle_request()
    srv.server_close()


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())


def _post(url: str, data: dict) -> dict:
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(body)


def _read_env(path: Path) -> dict:
    result = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _save_env(path: Path, token: str, user_id: str):
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated = {"THREADS_ACCESS_TOKEN": False, "THREADS_USER_ID": False}
    out = []
    for line in lines:
        if line.startswith("THREADS_ACCESS_TOKEN="):
            out.append(f"THREADS_ACCESS_TOKEN={token}")
            updated["THREADS_ACCESS_TOKEN"] = True
        elif line.startswith("THREADS_USER_ID="):
            out.append(f"THREADS_USER_ID={user_id}")
            updated["THREADS_USER_ID"] = True
        else:
            out.append(line)
    if not updated["THREADS_ACCESS_TOKEN"]:
        out.append(f"THREADS_ACCESS_TOKEN={token}")
    if not updated["THREADS_USER_ID"]:
        out.append(f"THREADS_USER_ID={user_id}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _save_db(db_path: Path, token: str, user_id: str):
    if not db_path.exists():
        print(f"  (DB が見つかりません: {db_path} — スキップ)")
        return
    con = sqlite3.connect(db_path)
    try:
        for k, v in [("threads_access_token", token), ("threads_user_id", user_id)]:
            row = con.execute("SELECT id FROM settings WHERE key=?", (k,)).fetchone()
            if row:
                con.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
            else:
                con.execute("INSERT INTO settings (key,value) VALUES (?,?)", (k, v))
        con.commit()
    finally:
        con.close()


def _save_new_account(db_path: Path, account_label: str, token: str, user_id: str) -> bool:
    """threads_accounts テーブルに新規アカウントとしてINSERTする。
    既存のレコード（account_id=1 の KPOP アカウント含む）は一切変更しない。"""
    if not db_path.exists():
        print(f"  (DB が見つかりません: {db_path} — スキップ)")
        return False
    con = sqlite3.connect(db_path)
    try:
        now = datetime.utcnow().isoformat()
        con.execute(
            "INSERT INTO threads_accounts "
            "(account_label, threads_user_id, threads_access_token, token_acquired_at, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (account_label, user_id, token, now, now),
        )
        con.commit()
        return True
    finally:
        con.close()


def main():
    base = Path(__file__).parent
    env = _read_env(base / ".env")

    app_id     = env.get("META_APP_ID", "").strip()
    app_secret = env.get("META_APP_SECRET", "").strip()

    print("=" * 60)
    print("  Threads アクセストークン取得スクリプト")
    print("=" * 60)

    # 認証情報が .env にない場合は入力を求める
    if not app_id:
        app_id = input("\nMeta App ID を入力してください: ").strip()
    else:
        print(f"\nMeta App ID    : {app_id}")

    if not app_secret:
        app_secret = input("Meta App Secret を入力してください: ").strip()
    else:
        print("Meta App Secret: (設定済み)")

    if not app_id or not app_secret:
        sys.exit("エラー: App ID と App Secret が必要です。")

    # .env に保存されていなければ書き込む
    for k, v in [("META_APP_ID", app_id), ("META_APP_SECRET", app_secret)]:
        if not env.get(k):
            _save_env(base / ".env", "", "")   # 後でトークンと一緒に書くので今は skip

    print("\n" + "-" * 60)
    print("【Meta Developer Portal に登録が必要な URL】")
    print(f"  {REDIRECT_URI}")
    print()
    print("  登録先: アプリ → Threads API → 設定 → コールバック URL")
    print("-" * 60)
    print("\nブラウザで認証ページを開きます。")
    print("Threads アカウントでログインして「許可」を押してください。\n")

    # 一時サーバーをバックグラウンドで起動
    threading.Thread(target=_start_server, daemon=True).start()

    state = secrets.token_urlsafe(16)
    auth_url = "https://threads.net/oauth/authorize?" + urllib.parse.urlencode({
        "client_id":    app_id,
        "redirect_uri": REDIRECT_URI,
        "scope":        SCOPE,
        "response_type": "code",
        "state":        state,
    })

    webbrowser.open(auth_url)
    print(f"自動で開かない場合は以下の URL をブラウザに貼ってください:")
    print(f"  {auth_url}\n")

    # 最大 3 分待つ
    if not _done.wait(timeout=180):
        sys.exit("タイムアウト: 3 分以内に認証を完了してください。")

    if _error:
        # error_code 1349187 の場合はコールバック URL 未登録の案内
        if "1349187" in _error or "redirect_uri" in _error.lower():
            print("\n[エラー] コールバック URL が Meta Developer Portal に登録されていません。")
            print(f"  以下を登録して、もう一度スクリプトを実行してください:")
            print(f"  {REDIRECT_URI}")
        else:
            print(f"\n[認証エラー] {_error}")
        sys.exit(1)

    if not _code:
        sys.exit("[エラー] 認証コードを取得できませんでした。")

    print("認証コードを受信しました。トークンを取得中...")

    # 短期トークン取得
    try:
        short_data = _post("https://graph.threads.net/oauth/access_token", {
            "client_id":     app_id,
            "client_secret": app_secret,
            "code":          _code,
            "grant_type":    "authorization_code",
            "redirect_uri":  REDIRECT_URI,
        })
    except RuntimeError as e:
        sys.exit(f"[エラー] 短期トークン取得失敗:\n{e}")

    short_token = short_data.get("access_token")
    if not short_token:
        sys.exit(f"[エラー] 短期トークンが見つかりません: {short_data}")

    # 長期トークン（60日）に交換
    try:
        long_data = _get(
            "https://graph.threads.net/access_token?"
            + urllib.parse.urlencode({
                "grant_type":    "th_exchange_token",
                "client_secret": app_secret,
                "access_token":  short_token,
            })
        )
    except Exception as e:
        sys.exit(f"[エラー] 長期トークン交換失敗: {e}")

    long_token = long_data.get("access_token")
    if not long_token:
        sys.exit(f"[エラー] 長期トークンが見つかりません: {long_data}")

    # ユーザー情報取得
    try:
        me = _get(
            "https://graph.threads.net/v1.0/me?"
            + urllib.parse.urlencode({"fields": "id,username", "access_token": long_token})
        )
    except Exception as e:
        sys.exit(f"[エラー] ユーザー情報取得失敗: {e}")

    user_id  = me.get("id", "")
    username = me.get("username", "")

    print(f"\n認証成功: @{username} (ID: {user_id})")
    print(f"長期トークン取得済み (有効期限: 60 日)\n")

    # app_id / app_secret は .env に書き込む（未設定の場合のみ。複数アカウントで共有するため）
    lines = (base / ".env").read_text(encoding="utf-8").splitlines() if (base / ".env").exists() else []
    keys_present = {l.split("=")[0].strip() for l in lines if "=" in l}
    with open(base / ".env", "a", encoding="utf-8") as f:
        if "META_APP_ID" not in keys_present:
            f.write(f"\nMETA_APP_ID={app_id}\n")
        if "META_APP_SECRET" not in keys_present:
            f.write(f"META_APP_SECRET={app_secret}\n")

    # このアカウントの表示名を入力してもらい、threads_accounts に新規レコードとして保存する。
    # 既存アカウント（account_id=1 の KPOP アカウント含む）は一切変更しない。
    print("-" * 60)
    account_label = ""
    while not account_label:
        account_label = input("このアカウントの表示名を入力してください（例: 田中アカウント）: ").strip()
        if not account_label:
            print("  表示名は必須です。")

    db_path = base / "instance" / "rock_metal.db"
    saved = _save_new_account(db_path, account_label, long_token, user_id)
    if saved:
        print(f"[保存] {db_path} に threads_accounts レコードを新規追加しました（{account_label}）")
    else:
        print("[エラー] DB への保存に失敗しました。")

    print("\n" + "=" * 60)
    print("  完了！")
    print(f"  アカウント「{account_label}」を追加しました。")
    print("  アプリが起動中の場合はブラウザをリロードしてください。")
    print("=" * 60)


if __name__ == "__main__":
    main()
