"""分析画面の「時間帯別」「曜日別」「グループ別」「メンバー別」の集計。

判断に使えるように、次の点を整えている:
- 初回投稿と再投稿(バズ再投稿)を「投稿サイクル」単位で分けて集計する。
- 削除した投稿(deleted_post_log の最終いいね・閲覧数)も含められる。
- 平均に加えて中央値を出す。
- 投稿数が少ない行(MIN_POSTS本未満)は表示側でグレーにする。

サンプル(1サイクル=1件)の作り方:
- 残っている記事: post_stats の確定記録(is_final)をサイクルごとに分け、day_index=7 の記録があればそれ、
  無ければそのサイクルの最新の記録を使う(後者は fallback=True。表の * の意味)。
  再投稿された記事の「初回サイクル」の投稿時刻は記録が残っていない(posted_at は最新サイクルの時刻に更新される)
  ため、時間帯別・曜日別には入れない(グループ別・メンバー別には入れる)。
- 削除した投稿: deleted_post_log の final_likes / final_views。buzz_repost_count=1 なら初回、2以上なら再投稿。
  手動削除で投稿から7日未満のものは確定値ではないので除く(group_balance と同じ扱い)。
"""
import statistics
from collections import defaultdict
from datetime import timedelta

MIN_POSTS = 5               # これ未満の行は「本数が少ない」表示にする
FINAL_DAYS = 7
CYCLE_GAP_HOURS = 36        # 確定記録の「推定投稿時刻」がこれ以内なら同じサイクルとみなす

CYCLE_CHOICES = ("initial", "repost", "all")
WEEKDAY_LABELS = {0: "日", 1: "月", 2: "火", 3: "水", 4: "木", 5: "金", 6: "土"}


def _clusters(rows):
    """1記事の確定記録 [(fetched_at, day_index, likes, views, id)] をサイクルごとのリストに分ける(古い順)。"""
    rows = sorted(rows, key=lambda r: (r[0], r[4]))
    out, cur, cur_start = [], [], None
    for r in rows:
        start = r[0] - timedelta(days=r[1] or FINAL_DAYS)
        if cur and abs((start - cur_start).total_seconds()) <= CYCLE_GAP_HOURS * 3600:
            cur.append(r)
            continue
        if cur:
            out.append(cur)
        cur, cur_start = [r], start
    if cur:
        out.append(cur)
    return out


def _pick(cluster):
    """サイクル内の代表記録。day_index=7 があればそれ(同率は最新)、無ければ最新。(記録, fallback) を返す。"""
    seven = [r for r in cluster if r[1] == FINAL_DAYS]
    if seven:
        return max(seven, key=lambda r: (r[0], r[4])), False
    return max(cluster, key=lambda r: (r[0], r[4])), True


def build_samples(db, models, account_id: int) -> list:
    """分析用のサンプルを全部作る。各要素:
    {cycle: initial|repost, posted_at(UTC。分からなければNone), likes, views, group, member, fallback, deleted}"""
    Article, PostStat, Group, Member, DeletedPostLog = (
        models["Article"], models["PostStat"], models["Group"], models["Member"], models["DeletedPostLog"])
    groups = {g.id: g.name for g in Group.query.all()}
    members = {m.id: m.name for m in Member.query.all()}
    arts = {a.id: a for a in Article.query.filter(Article.account_id == account_id, Article.posted_at.isnot(None)).all()}

    by_article = defaultdict(list)
    if arts:
        q = (db.session.query(PostStat.article_id, PostStat.fetched_at, PostStat.day_index, PostStat.likes,
                              PostStat.views, PostStat.id)
             .filter(PostStat.is_final.is_(True), PostStat.article_id.in_(list(arts))))
        for aid, fetched, day_index, likes, views, pid in q.all():
            if fetched is not None:
                by_article[aid].append((fetched, day_index or FINAL_DAYS, likes or 0, views or 0, pid))

    samples = []
    for aid, rows in by_article.items():
        art = arts[aid]
        repost_count = art.buzz_repost_count or 1
        for idx, cluster in enumerate(_clusters(rows)):
            is_current = cluster[-1][0] >= art.posted_at          # 現在(最新)の投稿サイクルの確定記録
            if repost_count <= 1:
                cycle = "initial"
            elif is_current:
                cycle = "repost"
            else:
                cycle = "initial" if idx == 0 else "repost"
            row, fallback = _pick(cluster)
            # 投稿時刻が正確に分かるのは、現在のサイクルだけ。再投稿済み記事の過去サイクルは時刻なし
            exact = is_current or repost_count <= 1
            samples.append({
                "cycle": cycle, "posted_at": art.posted_at if exact else None, "likes": row[2], "views": row[3],
                "group": groups.get(art.group_id), "member": members.get(art.member_id), "fallback": fallback,
                "deleted": False,
            })

    logs = DeletedPostLog.query.filter(
        DeletedPostLog.posted_at.isnot(None), DeletedPostLog.final_views.isnot(None))
    for lg in logs.all():
        if lg.account_id not in (account_id, None):
            continue
        if (lg.delete_reason or "").startswith("manual") and lg.deleted_at \
                and lg.deleted_at - lg.posted_at < timedelta(days=FINAL_DAYS):
            continue
        samples.append({
            "cycle": "initial" if (lg.buzz_repost_count or 1) <= 1 else "repost", "posted_at": lg.posted_at,
            "likes": lg.final_likes or 0, "views": lg.final_views or 0, "group": lg.group_name,
            "member": lg.member_name, "fallback": False, "deleted": True,
        })
    return samples


def select(samples: list, cycle: str, include_deleted: bool) -> list:
    out = []
    for s in samples:
        if cycle != "all" and s["cycle"] != cycle:
            continue
        if s["deleted"] and not include_deleted:
            continue
        out.append(s)
    return out


def _stats(items: list) -> dict:
    likes = [s["likes"] for s in items]
    views = [s["views"] for s in items]
    n = len(items)
    return {
        "post_count": n, "deleted_count": sum(1 for s in items if s["deleted"]),
        "fallback_count": sum(1 for s in items if s["fallback"]),
        "avg_likes": sum(likes) / n, "median_likes": statistics.median(likes),
        "avg_views": sum(views) / n, "median_views": statistics.median(views),
        "low": n < MIN_POSTS,
    }


def aggregate(samples: list, key_fn, sort_key=None, require_time=False) -> list:
    """key_fn(サンプル)->キー(Noneなら除外)で集計し、行のリストを返す。
    並び順: sort_key(行) があればそれ、無ければ「本数が足りている行→中央値いいねの多い順」。"""
    buckets = defaultdict(list)
    for s in samples:
        if require_time and s["posted_at"] is None:
            continue
        k = key_fn(s)
        if k is None:
            continue
        buckets[k].append(s)
    rows = [{"key": k, **_stats(items)} for k, items in buckets.items()]
    if sort_key:
        rows.sort(key=sort_key)
    else:
        rows.sort(key=lambda r: (r["low"], -r["median_likes"], -r["avg_likes"]))
    return rows


def build_report(samples: list, cycle: str, include_deleted: bool) -> dict:
    sel = select(samples, cycle, include_deleted)
    jst = lambda s: s["posted_at"] + timedelta(hours=9)
    group_rows = [{"name": r["key"], **r} for r in aggregate(sel, lambda s: s["group"])]
    member_rows = []
    for r in aggregate(sel, lambda s: (s["group"], s["member"]) if s["member"] else None):
        member_rows.append({"group_name": r["key"][0] or "", "member_name": r["key"][1], **r})
    hour_rows = [{"hour": r["key"], **r} for r in aggregate(
        sel, lambda s: jst(s).hour, sort_key=lambda r: r["key"], require_time=True)]
    weekday_order = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 0: 6}   # 月〜日
    # python の weekday(): 月=0 … 日=6 → 表示用(日=0,月=1…)に直す
    weekday_rows = [{"weekday": r["key"], "weekday_label": WEEKDAY_LABELS[r["key"]], **r} for r in aggregate(
        sel, lambda s: (jst(s).weekday() + 1) % 7, sort_key=lambda r: weekday_order[r["key"]], require_time=True)]
    return {
        "group_rows": group_rows, "member_rows": member_rows, "hour_rows": hour_rows, "weekday_rows": weekday_rows,
        "sample_count": len(sel), "deleted_count": sum(1 for s in sel if s["deleted"]),
        "no_time_count": sum(1 for s in sel if s["posted_at"] is None),
        "has_fallback": any(s["fallback"] for s in sel),
    }
