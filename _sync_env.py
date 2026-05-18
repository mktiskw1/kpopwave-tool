import sqlite3, pathlib

base = pathlib.Path(__file__).parent
con = sqlite3.connect(base / "instance" / "rock_metal.db")
rows = {k: v for k, v in con.execute(
    "SELECT key, value FROM settings WHERE key IN ('threads_access_token', 'threads_user_id')"
).fetchall()}
con.close()

token   = rows.get("threads_access_token", "")
user_id = rows.get("threads_user_id", "")

env_path = base / ".env"
lines = env_path.read_text(encoding="utf-8").splitlines()
out = []
for line in lines:
    if line.startswith("THREADS_ACCESS_TOKEN="):
        out.append(f"THREADS_ACCESS_TOKEN={token}")
    elif line.startswith("THREADS_USER_ID="):
        out.append(f"THREADS_USER_ID={user_id}")
    else:
        out.append(line)
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
print("done")
