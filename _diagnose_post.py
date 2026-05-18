"""
Threads 投稿診断スクリプト
APIの各ステップのレスポンスを詳細表示し、投稿が表示されない原因を特定する。
"""
import json, sqlite3, time, urllib.parse, urllib.request, urllib.error, pathlib

base = pathlib.Path(__file__).parent
con = sqlite3.connect(base / "instance" / "rock_metal.db")
rows = {k: v for k, v in con.execute(
    "SELECT key, value FROM settings WHERE key IN "
    "('threads_access_token','threads_user_id','test_mode')"
).fetchall()}
con.close()

token   = rows.get("threads_access_token", "")
user_id = rows.get("threads_user_id", "")
test_mode = rows.get("test_mode", "true")

print("=" * 60)
print("  Threads 投稿診断")
print("=" * 60)
print(f"  test_mode  : {test_mode}")
print(f"  user_id    : {user_id}")
print(f"  token (先頭): {token[:30]}..." if token else "  token: (未設定)")
print()

if not token or not user_id:
    raise SystemExit("[エラー] token または user_id が未設定です。")

API = "https://graph.threads.net/v1.0"

def api_post(path, data):
    url = f"{API}{path}"
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp_body = r.read()
            return r.status, json.loads(resp_body)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def api_get(path, params):
    url = f"{API}{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            resp_body = r.read()
            return r.status, json.loads(resp_body)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

# ── Step 0: 自分のプロフィール確認 ─────────────────────────────────────────
print("[Step 0] プロフィール確認...")
status, me = api_get("/me", {"fields": "id,username,threads_profile_picture_url", "access_token": token})
print(f"  HTTP {status}")
print(f"  {json.dumps(me, ensure_ascii=False, indent=2)}")
print()

# ── Step 1: コンテナ作成 ───────────────────────────────────────────────────
TEST_TEXT = "[診断テスト] このメッセージは投稿診断スクリプトからの送信です。 #テスト"
print("[Step 1] メディアコンテナ作成...")
status1, data1 = api_post(f"/{user_id}/threads", {
    "media_type": "TEXT",
    "text": TEST_TEXT,
    "access_token": token,
})
print(f"  HTTP {status1}")
print(f"  {json.dumps(data1, ensure_ascii=False, indent=2)}")
print()

if status1 != 200 or "id" not in data1:
    raise SystemExit(f"[失敗] コンテナ作成エラー: {data1}")

container_id = data1["id"]

# ── Step 2: コンテナのステータス確認 ──────────────────────────────────────
print("[Step 2] コンテナ ステータス確認...")
status2, data2 = api_get(f"/{container_id}", {
    "fields": "id,status,error_type",
    "access_token": token,
})
print(f"  HTTP {status2}")
print(f"  {json.dumps(data2, ensure_ascii=False, indent=2)}")
print()

container_status = data2.get("status", "UNKNOWN")
if container_status == "ERROR":
    error_type = data2.get("error_type", "不明")
    raise SystemExit(f"[失敗] コンテナがエラー状態です。error_type: {error_type}")

if container_status not in ("FINISHED", "PUBLISHED"):
    print(f"  コンテナがまだ処理中 ({container_status})。3秒待機...")
    time.sleep(3)
    status2b, data2b = api_get(f"/{container_id}", {
        "fields": "id,status,error_type",
        "access_token": token,
    })
    print(f"  再確認: HTTP {status2b}")
    print(f"  {json.dumps(data2b, ensure_ascii=False, indent=2)}")
    if data2b.get("status") == "ERROR":
        raise SystemExit(f"[失敗] コンテナエラー: {data2b}")
    print()

# ── Step 3: 公開 ───────────────────────────────────────────────────────────
print("[Step 3] 投稿を公開...")
status3, data3 = api_post(f"/{user_id}/threads_publish", {
    "creation_id": container_id,
    "access_token": token,
})
print(f"  HTTP {status3}")
print(f"  {json.dumps(data3, ensure_ascii=False, indent=2)}")
print()

if status3 != 200 or "id" not in data3:
    raise SystemExit(f"[失敗] 公開エラー: {data3}")

post_id = data3["id"]

# ── Step 4: 投稿内容を取得して確認 ────────────────────────────────────────
print("[Step 4] 公開済み投稿を取得して確認...")
time.sleep(2)
status4, data4 = api_get(f"/{post_id}", {
    "fields": "id,text,timestamp,permalink",
    "access_token": token,
})
print(f"  HTTP {status4}")
print(f"  {json.dumps(data4, ensure_ascii=False, indent=2)}")
print()

# ── 結果サマリ ─────────────────────────────────────────────────────────────
print("=" * 60)
print("  診断結果")
print("=" * 60)
if status4 == 200 and data4.get("id"):
    print(f"  投稿 ID   : {post_id}")
    permalink = data4.get("permalink", "(なし)")
    print(f"  パーマリンク: {permalink}")
    print()
    print("  投稿は API 上では成功しています。")
    print("  Threads に表示されない場合の主な原因:")
    print("  1. Meta アプリが「開発モード」のため、アプリ管理者・テスターにのみ表示される")
    print("     → developers.facebook.com でアプリを「ライブ」に切り替える")
    print("  2. Threads の審査待ち（稀）")
    print(f"  3. 上記パーマリンクを直接ブラウザで開いて確認してください")
else:
    print(f"  [要確認] 投稿ID {post_id} の取得に失敗: {data4}")
