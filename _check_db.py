import sqlite3, pathlib
db = pathlib.Path("instance/rock_metal.db")
if not db.exists():
    print("DB なし")
else:
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT key, value FROM settings WHERE key IN ('threads_access_token','threads_user_id')"
    ).fetchall()
    con.close()
    for k, v in rows:
        display = (v[:40] + "...") if v and len(v) > 40 else (v or "(空)")
        print(f"{k} = {display}")
