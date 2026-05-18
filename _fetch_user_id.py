import json, sqlite3, urllib.parse, urllib.request, pathlib

base = pathlib.Path(__file__).parent
db_path = base / "instance" / "rock_metal.db"
env_path = base / ".env"

# DB からトークン取得
con = sqlite3.connect(db_path)
row = con.execute("SELECT value FROM settings WHERE key='threads_access_token'").fetchone()
token = row[0] if row else ""
if not token:
    raise SystemExit("threads_access_token が DB に見つかりません")

# Threads API でユーザー情報取得
url = "https://graph.threads.net/v1.0/me?" + urllib.parse.urlencode(
    {"fields": "id,username", "access_token": token}
)
with urllib.request.urlopen(url, timeout=15) as r:
    me = json.loads(r.read())

user_id  = me["id"]
username = me.get("username", "")
print(f"ユーザー名: @{username}")
print(f"ユーザー ID: {user_id}")

# DB に保存
row2 = con.execute("SELECT id FROM settings WHERE key='threads_user_id'").fetchone()
if row2:
    con.execute("UPDATE settings SET value=? WHERE key='threads_user_id'", (user_id,))
else:
    con.execute("INSERT INTO settings (key,value) VALUES ('threads_user_id',?)", (user_id,))
con.commit()
con.close()
print("DB に保存しました")

# .env に保存
lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
out, saved = [], False
for line in lines:
    if line.startswith("THREADS_USER_ID="):
        out.append(f"THREADS_USER_ID={user_id}")
        saved = True
    else:
        out.append(line)
if not saved:
    out.append(f"THREADS_USER_ID={user_id}")
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
print(".env に保存しました")
