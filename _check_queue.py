import sqlite3
from datetime import datetime, timezone, timedelta

conn = sqlite3.connect('instance/rock_metal.db')
c = conn.cursor()

JST = timezone(timedelta(hours=9))
now_utc = datetime.now(timezone.utc)

c.execute('SELECT id, status, scheduled_at FROM articles WHERE status=? ORDER BY scheduled_at', ('queued',))
rows = c.fetchall()
print('NOW UTC:', now_utc.strftime('%Y-%m-%d %H:%M'))
print('NOW JST:', now_utc.astimezone(JST).strftime('%Y-%m-%d %H:%M'))
print()
print('QUEUED articles:')
for r in rows:
    if r[2]:
        try:
            dt_utc = datetime.fromisoformat(str(r[2])).replace(tzinfo=timezone.utc)
            dt_jst = dt_utc.astimezone(JST)
            status_label = 'PAST' if dt_utc < now_utc else 'future'
            print('  ID={} UTC={} JST={} [{}]'.format(
                r[0], dt_utc.strftime('%m/%d %H:%M'), dt_jst.strftime('%m/%d %H:%M'), status_label
            ))
        except Exception as e:
            print('  ID={} parse_err={}'.format(r[0], e))
    else:
        print('  ID={} scheduled_at=NULL'.format(r[0]))

conn.close()
