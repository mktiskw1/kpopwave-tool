import sqlite3
conn = sqlite3.connect("instance/rock_metal.db")
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("tables:", [t[0] for t in tables])

for t in tables:
    if "schedule" in t[0].lower():
        cols = [d[0] for d in conn.execute(f"PRAGMA table_info({t[0]})").fetchall()]
        print(f"\n{t[0]} columns:", cols)
        rows = conn.execute(f"SELECT * FROM {t[0]}").fetchall()
        for r in rows:
            print(" ", r)
conn.close()
