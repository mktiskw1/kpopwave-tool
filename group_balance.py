"""キューの投稿順をグループごとに自動調整する(偏り防止＋閲覧数に応じた頻度調整)と、グループ別のストック計画。

- 評価値: 直近N日に初回投稿した動画の閲覧数の中央値。本数が少ないグループは全体の中央値に寄せる(平滑化)。
- 配分の重み: 評価値の平方根(差を弱めに反映)。目標の割合 = 重み×手動補正×カムバック補正 を合計で割り、
  手動補正が0でないグループには最低割合を保証する。
- 選び方(投稿ジョブが次の記事を決めるとき): 優先 → バズ再投稿(従来どおり) → 直近14日の実際の割合が
  目標より最も足りないグループの、キューで最も古い記事。直近のグループ連続は避ける。
- 動画以外(テキスト投稿など)・動画以外のアカウント(田中アカウント等)は対象外で、従来どおりの順番で投稿する。
"""
import logging
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import or_

from database import (
    Article, DeletedPostLog, Group, GroupPlanSetting, PostStat, Setting, ThreadsAccount, db,
)

logger = logging.getLogger(__name__)

NONE_LABEL = "グループなし"
CATCHALL_NAME = "その他"          # 名簿の受け皿グループ。「グループなし」と同様に評価値の重み付けの対象外(最低割合で固定)
ACTUAL_WINDOW_DAYS = 14          # 「実際の割合」を数える期間
FINAL_DAYS = 7                   # 閲覧数が確定するまでの日数

# 設定キーと既定値(文字列で保存する)
DEFAULTS = {
    "group_balance_enabled": "true",
    "group_score_lookback_days": "60",
    "group_score_smoothing": "5",
    "group_score_cap_ratio": "3",      # 評価値の上限 = 全体の中央値 × この倍率
    "group_min_share": "5",            # パーセント
    "group_spacing": "2",
    "comeback_boost_factor": "1.5",
    "comeback_boost_days": "14",
    "stock_plan_posts_per_day": "3",
    "stock_plan_days": "7",
}


def key_to_str(key) -> str:
    return "none" if key is None else str(key)


def str_to_key(value):
    if value is None or str(value).lower() in ("none", "null", ""):
        return None
    return int(value)


def _num(name: str, cast, lo=None, hi=None):
    raw = Setting.get(name, DEFAULTS[name])
    try:
        v = cast(raw)
    except (TypeError, ValueError):
        v = cast(DEFAULTS[name])
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def get_config() -> dict:
    """設定値(app context内で呼ぶこと)。不正値は既定値/範囲内に丸める。"""
    return {
        "enabled": (Setting.get("group_balance_enabled", DEFAULTS["group_balance_enabled"]) or "true").strip().lower() != "false",
        "lookback_days": _num("group_score_lookback_days", int, 1, 365),
        "smoothing": _num("group_score_smoothing", float, 0.0, 1000.0),
        "cap_ratio": _num("group_score_cap_ratio", float, 1.0, 100.0),
        "min_share": _num("group_min_share", float, 0.0, 50.0) / 100.0,
        "spacing": _num("group_spacing", int, 0, 10),
        "boost_factor": _num("comeback_boost_factor", float, 1.0, 10.0),
        "boost_days": _num("comeback_boost_days", int, 1, 90),
        "posts_per_day": _num("stock_plan_posts_per_day", int, 1, 20),
        "plan_days": _num("stock_plan_days", int, 1, 60),
    }


def is_balance_account(account_id) -> bool:
    """グループ調整の対象アカウント(content_topic未設定=KPOPアカウント)か。"""
    acc = db.session.get(ThreadsAccount, account_id) if account_id else None
    return bool(acc) and not (acc.content_topic or "").strip()


def _account_filter(query, account_id):
    legacy = (
        ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).first()
    )
    if legacy is not None and legacy.id == account_id:
        return query.filter(or_(Article.account_id == account_id, Article.account_id.is_(None)))
    return query.filter(Article.account_id == account_id)


# ───────────── 評価値 ─────────────

def _initial_cycle_samples(account_id: int, now: datetime, lookback_days: int, name_to_id: dict) -> dict:
    """グループごとの「初回投稿サイクルの閲覧数(確定値)」のリスト。再投稿のサイクルは使わない。
    - 残っている記事: 最初の確定値(post_stats is_final)。再投稿済みの記事も、再投稿より前に確定した
      初回サイクルの値だけを使う。
    - 削除された記事(deleted_post_log): buzz_repost_count=1 の記録(手動削除で7日未満のものは除外)。"""
    cutoff = now - timedelta(days=lookback_days)
    out = defaultdict(list)

    arts = {a.id: a for a in _account_filter(
        Article.query.filter(Article.content_type == "video"), account_id).all()}
    first_final = {}
    if arts:
        rows = (
            db.session.query(PostStat.article_id, PostStat.fetched_at, PostStat.day_index, PostStat.views)
            .filter(PostStat.is_final.is_(True), PostStat.article_id.in_(list(arts)))
            .order_by(PostStat.fetched_at.asc()).all()
        )
        for aid, fetched_at, day_index, views in rows:
            first_final.setdefault(aid, (fetched_at, day_index or FINAL_DAYS, views))

    for aid, art in arts.items():
        repost = art.buzz_repost_count or 1
        ff = first_final.get(aid)
        if repost <= 1:
            if art.posted_at is None or art.posted_at < cutoff:
                continue
            if ff is not None and ff[2] is not None:
                views = ff[2]
            elif art.view_count is not None and art.posted_at <= now - timedelta(days=FINAL_DAYS):
                views = art.view_count
            else:
                continue                              # まだ確定していない
            out[art.group_id].append(int(views))
        else:
            if ff is None or ff[2] is None or art.posted_at is None:
                continue
            if ff[0] >= art.posted_at:
                continue                              # 確定値が現在(再投稿後)のサイクルのもの
            initial_posted = ff[0] - timedelta(days=ff[1])
            if initial_posted >= cutoff:
                out[art.group_id].append(int(ff[2]))

    logs = DeletedPostLog.query.filter(
        DeletedPostLog.youtube_video_id.isnot(None),
        DeletedPostLog.posted_at.isnot(None),
        DeletedPostLog.posted_at >= cutoff,
        DeletedPostLog.final_views.isnot(None),
    )
    legacy = ThreadsAccount.query.filter_by(is_active=True).order_by(ThreadsAccount.id.asc()).first()
    for lg in logs.all():
        if (lg.buzz_repost_count or 1) != 1:
            continue
        if lg.account_id not in (account_id, None) and not (legacy and lg.account_id == legacy.id and account_id == legacy.id):
            continue
        if (lg.delete_reason or "").startswith("manual") and lg.deleted_at and lg.deleted_at - lg.posted_at < timedelta(days=FINAL_DAYS):
            continue
        out[name_to_id.get(lg.group_name) if lg.group_name else None].append(int(lg.final_views))
    return out


def _compute_targets(raws: dict, eligible: set, min_share: float, fixed_keys=()) -> dict:
    """raws(重み×補正)から目標の割合を作る。eligible(手動補正≠0)のグループには min_share を保証する。
    fixed_keys(「その他」「グループなし」)は重み付けの対象外で、目標の割合を min_share に固定する。"""
    shares = {k: 0.0 for k in raws}
    elig = [k for k in raws if k in eligible]
    if not elig:
        return shares
    floor = min(min_share, 1.0 / len(elig))
    fixed = {k for k in fixed_keys if k in eligible}
    free = set(elig) - fixed
    while True:
        remaining = 1.0 - floor * len(fixed)
        total = sum(raws[k] for k in free)
        if not free:
            break
        below = []
        for k in free:
            s = remaining * (raws[k] / total) if total > 0 else remaining / len(free)
            if s < floor - 1e-12:
                below.append(k)
        if not below:
            for k in free:
                shares[k] = remaining * (raws[k] / total) if total > 0 else remaining / len(free)
            break
        for k in below:
            fixed.add(k)
            free.discard(k)
    for k in fixed:
        shares[k] = floor
    return shares


# ───────────── 実績(直近14日) ─────────────

def _recent_group_posts(account_id: int, since: datetime, name_to_id: dict) -> list:
    """sinceより後に投稿された動画の [(posted_at, グループキー)] (削除済みも含む)。"""
    rows = []
    for a in _account_filter(Article.query.filter(
            Article.content_type == "video", Article.status == "posted",
            Article.posted_at.isnot(None), Article.posted_at >= since), account_id).all():
        rows.append((a.posted_at, a.group_id))
    for lg in DeletedPostLog.query.filter(
            DeletedPostLog.youtube_video_id.isnot(None), DeletedPostLog.posted_at.isnot(None),
            DeletedPostLog.posted_at >= since).all():
        if lg.account_id not in (account_id, None):
            continue
        rows.append((lg.posted_at, name_to_id.get(lg.group_name) if lg.group_name else None))
    rows.sort(key=lambda r: r[0])
    return rows


def _recent_keys(account_id: int, n: int, name_to_id: dict) -> list:
    """直近n回の投稿のグループキー(新しい順)。"""
    if n <= 0:
        return []
    rows = _recent_group_posts(account_id, datetime.utcnow() - timedelta(days=60), name_to_id)
    return [k for _, k in reversed(rows)][:n]


# ───────────── 状態(評価値・目標・実際) ─────────────

def compute_state(account_id: int, now: datetime = None) -> dict:
    """全グループ(名簿＋グループなし)の評価値・目標の割合・直近14日の実際の割合を計算する(app context内で呼ぶ)。"""
    now = now or datetime.utcnow()
    cfg = get_config()
    roster = [(g.id, g.name) for g in Group.query.order_by(Group.name.asc()).all()] + [(None, NONE_LABEL)]
    name_to_id = {name: gid for gid, name in roster if gid is not None}

    samples = _initial_cycle_samples(account_id, now, cfg["lookback_days"], name_to_id)
    all_vals = [v for vs in samples.values() for v in vs]
    overall = float(statistics.median(all_vals)) if all_vals else 0.0
    k = cfg["smoothing"]

    plan_settings = {s.group_id: s for s in GroupPlanSetting.query.all()}
    rows = []
    for gid, name in roster:
        vals = samples.get(gid, [])
        n = len(vals)
        med = float(statistics.median(vals)) if vals else None
        if n + k > 0:
            score = ((n * med if n else 0.0) + k * overall) / (n + k)
        else:
            score = med if med is not None else overall
        # 少数のバズ動画に引っ張られないよう、評価値は「全体の中央値×上限倍率」までに抑えてから平方根をとる
        capped = min(score, overall * cfg["cap_ratio"]) if overall > 0 else score
        ps = plan_settings.get(gid)
        manual = float(ps.manual_factor) if ps is not None and ps.manual_factor is not None else 1.0
        manual = max(0.0, min(3.0, manual))
        until = ps.comeback_until if ps is not None else None
        boosted = bool(until and until > now)
        rows.append({
            "key": gid, "id": key_to_str(gid), "name": name, "n": n, "median": med, "score": score,
            "score_capped": capped, "fixed": gid is None or name == CATCHALL_NAME,
            "weight": math.sqrt(capped) if capped > 0 else 0.0, "manual": manual,
            "comeback_active": boosted, "comeback_until": until if boosted else None,
            "comeback_factor": cfg["boost_factor"] if boosted else 1.0,
        })

    # 全グループの重みが0(データなし)の場合は、重みを均等にして手動補正・カムバック補正だけを反映する
    if not any(r["weight"] > 0 for r in rows):
        for r in rows:
            r["weight"] = 1.0
    raws = {r["key"]: r["weight"] * r["manual"] * r["comeback_factor"] for r in rows}
    eligible = {r["key"] for r in rows if r["manual"] > 0}
    fixed_keys = {r["key"] for r in rows if r["fixed"]}
    targets = _compute_targets(raws, eligible, cfg["min_share"], fixed_keys)

    recent = _recent_group_posts(account_id, now - timedelta(days=ACTUAL_WINDOW_DAYS), name_to_id)
    counts = defaultdict(int)
    for _, key in recent:
        counts[key] += 1
    total_recent = len(recent)
    for r in rows:
        r["target"] = targets.get(r["key"], 0.0)
        r["actual_count"] = counts.get(r["key"], 0)
        r["actual"] = (counts.get(r["key"], 0) / total_recent) if total_recent else 0.0
    return {"cfg": cfg, "rows": rows, "overall_median": overall, "sample_total": len(all_vals),
            "recent_total": total_recent, "name_to_id": name_to_id}


# ───────────── 次に投稿する記事の選択 ─────────────

def _video_file_ok(article) -> bool:
    if not article.video_file_path:
        return False
    return os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", article.video_file_path))


def _queue_sort_key(a):
    return (a.scheduled_at is not None, a.scheduled_at or datetime.min, a.created_at or datetime.min)


def choose_balanced(pool: list, state: dict, recent_keys: list, spacing: int):
    """pool(記事のリスト、各要素は .group_id を持つ)から、目標に対して最も足りないグループの
    キューで最も古い記事を選ぶ。(記事, 理由) を返す。選べなければ (None, 理由)。"""
    rows = {r["key"]: r for r in state["rows"]}
    by_group = defaultdict(list)
    for a in pool:
        by_group[a.group_id].append(a)
    for lst in by_group.values():
        lst.sort(key=_queue_sort_key)

    def _eligible(groups, use_spacing):
        out = []
        for g in groups:
            r = rows.get(g)
            if r is None or r["manual"] <= 0:
                continue                       # 手動補正0のグループは出さない
            if use_spacing and g in recent_keys[:spacing]:
                continue
            out.append(g)
        return out

    candidates = _eligible(list(by_group), True)
    relaxed = False
    if not candidates:
        candidates = _eligible(list(by_group), False)
        relaxed = True                        # 連続を避けるルールを緩める(投稿は止めない)
    if not candidates:
        return None, "no_eligible_group"

    def _deficit(g):
        r = rows[g]
        return r["target"] - r["actual"]

    # 不足が最大のグループ。同点ならキューで最も古い記事を持つグループ
    rank = {a.id: i for i, a in enumerate(sorted(pool, key=_queue_sort_key))}
    best = max(candidates, key=lambda g: (round(_deficit(g), 9), -rank[by_group[g][0].id]))
    return by_group[best][0], ("balanced_relaxed" if relaxed else "balanced")


def select_for_slot(account_id: int, head, now: datetime = None):
    """今の枠(headの記事)に投稿する記事を決める。(選んだ記事, 理由) を返す(app context内で呼ぶ)。
    順序: 優先(押した順) → バズ再投稿(headがそれならそのまま) → グループ調整 → 従来どおり(head)。"""
    now = now or datetime.utcnow()
    queued = _account_filter(Article.query.filter(Article.status == "queued"), account_id).all()
    queued.sort(key=_queue_sort_key)

    prio = sorted([a for a in queued if a.priority_requested_at is not None], key=lambda a: a.priority_requested_at)
    if prio:
        return prio[0], "priority"

    cfg = get_config()
    if not cfg["enabled"]:
        return head, "balance_off"
    if not is_balance_account(account_id):
        return head, "not_balance_account"
    if (head.content_type or "article") != "video" or (head.buzz_repost_count or 1) > 1:
        return head, "head_fixed"             # バズ再投稿・動画以外は従来どおりの順番・時刻

    pool = [a for a in queued if (a.content_type or "article") == "video"
            and (a.buzz_repost_count or 1) <= 1 and a.priority_requested_at is None and _video_file_ok(a)]
    clean = [a for a in pool if not a.error_message]      # 直前に投稿中止になった記事は、他に選べるものが無いときだけ
    pool = clean or pool
    if not pool:
        return head, "no_pool"

    state = compute_state(account_id, now)
    recent = _recent_keys(account_id, max(cfg["spacing"], 1), state["name_to_id"])
    chosen, reason = choose_balanced(pool, state, recent, cfg["spacing"])
    if chosen is None:
        return head, reason
    return chosen, reason


def apply_slot_choice(app, account_id: int, head, chosen) -> None:
    """chosen を head の枠で投稿できるよう、scheduled_at を入れ替える(app context内で呼ぶ)。
    時間枠自体は変えず、ゼロ反応時の繰り上げ(scheduled_at=None)・バズ再投稿の差し込みとも矛盾しない。"""
    if chosen.id == head.id:
        return
    head_slot, chosen_slot = head.scheduled_at, chosen.scheduled_at
    if chosen_slot is None:
        # 両方「時間未設定」だと入れ替えても順序が変わらないため、headを次の空き枠へ送る
        from scheduler import next_post_slot
        chosen_slot = next_post_slot(app, head.account_id) or (datetime.utcnow() + timedelta(days=1))
    head.scheduled_at, chosen.scheduled_at = chosen_slot, head_slot
    db.session.commit()
    logger.info("[group_balance] 枠の入れ替え: head id=%d(→%s) ⇄ chosen id=%d(→%s)",
                head.id, head.scheduled_at, chosen.id, chosen.scheduled_at)


# ───────────── バズ再投稿の予測・ストック計画 ─────────────

def forecast_reposts(app, account_id: int, days: int, now: datetime = None) -> dict:
    """期間中に再キューされる見込みのバズ再投稿の本数を {グループキー: 本数} で返す。
    今のバックログ＋期間中に再投稿の対象になる記事(通常間隔・ファストトラックの日付から計算)を、
    1日の処理件数のルール(バックログ>10件で2件/日、それ以外1件/日、投稿日が古い順)で消化する。"""
    from scheduler import (
        _BUZZ_REQUEUE_BACKLOG_BATCH_SIZE, _BUZZ_REQUEUE_BACKLOG_THRESHOLD, _BUZZ_REQUEUE_NORMAL_BATCH_SIZE,
        _JST, _DAY_KEYS, _buzz_fasttrack_settings, _buzz_requeue_interval_days_for,
        _latest_final_likes_by_article, get_weekly_schedule,
    )
    now = now or datetime.utcnow()
    interval_days = int(Setting.get("buzz_requeue_interval_days", "60") or "60")
    ft_interval, ft_min = _buzz_fasttrack_settings()

    base = (
        _account_filter(Article.query.filter(
            Article.status == "posted", Article.content_type == "video",
            Article.video_file_path.isnot(None), Article.posted_at.isnot(None)), account_id)
        .all()
    )
    likes = _latest_final_likes_by_article([(a.id, a.posted_at) for a in base])
    items = []
    for a in base:
        if not _video_file_ok(a):
            continue                                       # ファイルが無い記事は再キューされない
        d = _buzz_requeue_interval_days_for(likes.get(a.id, 0), interval_days, ft_interval, ft_min)
        items.append({"due": a.posted_at + timedelta(days=d), "posted_at": a.posted_at, "group": a.group_id})

    schedule = get_weekly_schedule(app, account_id)
    now_jst = now.replace(tzinfo=None) + timedelta(hours=9)
    counts = defaultdict(int)
    done = set()
    for offset in range(days):
        date = (now_jst + timedelta(days=offset)).date()
        times = [t for t in (schedule.get(_DAY_KEYS[date.weekday()]) or []) if str(t).strip()]
        if not times:
            continue
        try:
            first = min(tuple(int(x) for x in t.strip().split(":")) for t in times)
        except ValueError:
            continue
        run_jst = datetime(date.year, date.month, date.day, first[0], first[1]) - timedelta(hours=1)
        if run_jst <= now_jst:
            continue                                       # 今日の再キューは実行済み(キューに入っている)
        run_utc = run_jst - timedelta(hours=9)
        backlog = sorted((i for i in range(len(items)) if i not in done and items[i]["due"] <= run_utc),
                         key=lambda i: items[i]["posted_at"])
        batch = (_BUZZ_REQUEUE_BACKLOG_BATCH_SIZE if len(backlog) > _BUZZ_REQUEUE_BACKLOG_THRESHOLD
                 else _BUZZ_REQUEUE_NORMAL_BATCH_SIZE)
        for i in backlog[:batch]:
            done.add(i)
            counts[items[i]["group"]] += 1
    return dict(counts)


def compute_stock_plan(app, account_id: int, now: datetime = None) -> dict:
    """グループ別のストック計画(app context内で呼ぶ)。"""
    now = now or datetime.utcnow()
    state = compute_state(account_id, now)
    cfg = state["cfg"]
    slots = cfg["posts_per_day"] * cfg["plan_days"]
    reposts = forecast_reposts(app, account_id, cfg["plan_days"], now)

    stock = defaultdict(int)
    for a in _account_filter(Article.query.filter(
            Article.status == "queued", Article.content_type == "video"), account_id).all():
        stock[a.group_id] += 1

    rows = []
    for r in state["rows"]:
        turns = slots * r["target"]
        repost = reposts.get(r["key"], 0)
        # 「その他」「グループなし」は集める対象ではないので、新しく必要な本数は0(不足の表示は出ない)
        needed = 0 if r["fixed"] else max(0, math.ceil(round(turns - repost, 6)))
        have = stock.get(r["key"], 0)
        balance = have - needed
        rows.append({
            "id": r["id"], "name": r["name"],
            "turns": round(turns, 2), "repost": repost, "needed": needed, "stock": have, "balance": balance,
            "score": round(r["score_capped"]), "score_raw": round(r["score"]), "fixed": r["fixed"], "median": None if r["median"] is None else round(r["median"]),
            "n": r["n"], "target": r["target"], "actual": r["actual"], "actual_count": r["actual_count"],
            "manual": r["manual"], "comeback_active": r["comeback_active"],
            "comeback_until": (r["comeback_until"] + timedelta(hours=9)).strftime("%m/%d") if r["comeback_until"] else None,
            "comeback_factor": r["comeback_factor"],
        })
    rows.sort(key=lambda x: (x["balance"], x["name"]))
    totals = {"needed": sum(x["needed"] for x in rows), "stock": sum(x["stock"] for x in rows),
              "balance": sum(x["balance"] for x in rows)}
    return {"posts_per_day": cfg["posts_per_day"], "days": cfg["plan_days"], "slots": slots,
            "enabled": cfg["enabled"], "overall_median": round(state["overall_median"]), "cap_ratio": cfg["cap_ratio"],
            "min_share": cfg["min_share"],
            "sample_total": state["sample_total"], "recent_total": state["recent_total"],
            "lookback_days": cfg["lookback_days"], "rows": rows, "totals": totals}


def shortage_by_group_id(plan: dict) -> dict:
    """不足している(過不足<0)グループの {グループID(数値): 不足本数}。"""
    return {int(r["id"]): -r["balance"] for r in plan["rows"] if r["balance"] < 0 and r["id"] != "none"}


# ───────────── カムバック補正 ─────────────

def mark_comeback(group_id, now: datetime = None) -> None:
    """カムバック曲が承認された: そのグループの補正期間を承認日から設定日数にする
    (期間中に別のカムバック曲が承認されたら、そこから延長する)。グループなしは対象外。"""
    if group_id is None:
        return
    now = now or datetime.utcnow()
    cfg = get_config()
    ps = GroupPlanSetting.query.filter_by(group_id=group_id).first()
    if ps is None:
        ps = GroupPlanSetting(group_id=group_id, manual_factor=1.0)
        db.session.add(ps)
    ps.comeback_until = now + timedelta(days=cfg["boost_days"])


def set_manual_factor(group_id, factor: float) -> None:
    ps = GroupPlanSetting.query.filter_by(group_id=group_id).first() if group_id is not None \
        else GroupPlanSetting.query.filter(GroupPlanSetting.group_id.is_(None)).first()
    if ps is None:
        ps = GroupPlanSetting(group_id=group_id)
        db.session.add(ps)
    ps.manual_factor = max(0.0, min(3.0, float(factor)))
    db.session.commit()


# ───────────── 予測(次のN回の投稿) ─────────────

def simulate_next_posts(app, account_id: int, n: int = 10, now: datetime = None) -> list:
    """今のキューで次のn回の投稿が、どのグループのどの記事になるかを予測する(DBは変更しない)。
    各枠でキューの先頭を「今の枠の記事」として、実際のジョブと同じ選び方を適用する。"""
    from scheduler import _DAY_KEYS, get_weekly_schedule
    now = now or datetime.utcnow()
    cfg = get_config()
    state = compute_state(account_id, now)
    rows = {r["key"]: r for r in state["rows"]}
    name_by_key = {r["key"]: r["name"] for r in state["rows"]}

    queued = _account_filter(Article.query.filter(Article.status == "queued"), account_id).all()
    queued.sort(key=_queue_sort_key)
    recent_posts = _recent_group_posts(account_id, now - timedelta(days=ACTUAL_WINDOW_DAYS), state["name_to_id"])
    counts = defaultdict(int)
    for _, k in recent_posts:
        counts[k] += 1
    total = len(recent_posts)
    recent_keys = _recent_keys(account_id, max(cfg["spacing"], 1), state["name_to_id"])

    # 次のn個の投稿枠(週間スケジュール)
    schedule = get_weekly_schedule(app, account_id)
    now_jst = now + timedelta(hours=9)
    slots = []
    for offset in range(30):
        date = (now_jst + timedelta(days=offset)).date()
        for t in sorted(schedule.get(_DAY_KEYS[date.weekday()]) or []):
            try:
                h, m = map(int, t.strip().split(":"))
            except ValueError:
                continue
            slot = datetime(date.year, date.month, date.day, h, m)
            if slot > now_jst:
                slots.append(slot)
        if len(slots) >= n:
            break
    slots = slots[:n]

    result = []
    remaining = list(queued)
    for slot in slots:
        if not remaining:
            break
        head = remaining[0]
        prio = sorted([a for a in remaining if a.priority_requested_at is not None], key=lambda a: a.priority_requested_at)
        chosen, reason = None, None
        if prio:
            chosen, reason = prio[0], "priority"
        elif not cfg["enabled"]:
            chosen, reason = head, "balance_off"
        elif (head.content_type or "article") != "video" or (head.buzz_repost_count or 1) > 1:
            chosen, reason = head, "head_fixed"
        else:
            pool = [a for a in remaining if (a.content_type or "article") == "video" and (a.buzz_repost_count or 1) <= 1
                    and a.priority_requested_at is None and _video_file_ok(a)]
            pool = [a for a in pool if not a.error_message] or pool
            # 実際の割合は、これまでの予測投稿も加えて更新する
            for r in state["rows"]:
                r["actual"] = counts.get(r["key"], 0) / total if total else 0.0
            chosen, reason = (choose_balanced(pool, state, recent_keys, cfg["spacing"]) if pool else (None, "no_pool"))
            if chosen is None:
                chosen, reason = head, reason
        remaining.remove(chosen)
        counts[chosen.group_id] += 1
        total += 1
        recent_keys = [chosen.group_id] + recent_keys
        result.append({"slot_jst": slot.strftime("%m/%d %H:%M"), "article_id": chosen.id, "title": chosen.title,
                       "group": name_by_key.get(chosen.group_id, NONE_LABEL), "reason": reason,
                       "repost": (chosen.buzz_repost_count or 1) > 1})
    return result
