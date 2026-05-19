import logging
import re
from datetime import datetime

from database import Article, Setting, db

logger = logging.getLogger(__name__)

# エンゲージメントスコアの重み（リポストは拡散力が高いので重く）
_WEIGHTS = {"like_count": 1, "reply_count": 2, "repost_count": 3, "quote_count": 2}


def engagement_score(article) -> int:
    return sum(
        (getattr(article, col) or 0) * w for col, w in _WEIGHTS.items()
    )


def _emoji_count(text: str) -> int:
    pattern = re.compile(
        "[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001FA00-\U0001FA9F]",
        re.UNICODE,
    )
    return len(pattern.findall(text))


def _body_length(summary: str) -> int:
    """source行を除いた本文文字数"""
    cut = summary.find("\n\n📎")
    return len(summary[:cut]) if cut != -1 else len(summary)


def analyze_performance(app) -> dict:
    with app.app_context():
        articles = (
            Article.query
            .filter_by(status="posted")
            .filter(Article.engagement_fetched_at.isnot(None))
            .all()
        )
        total_posted = Article.query.filter_by(status="posted").count()

    if not articles:
        return {
            "total_posted": total_posted,
            "with_data": 0,
            "enough_data": False,
            "top_posts": [],
            "style_stats": {},
            "length_stats": {},
            "emoji_stats": {},
            "question_stats": {},
        }

    scored = []
    for a in articles:
        body = a.summary or ""
        scored.append({
            "id": a.id,
            "title": a.title[:60],
            "summary_body": body[:200],
            "score": engagement_score(a),
            "like_count": a.like_count or 0,
            "reply_count": a.reply_count or 0,
            "repost_count": a.repost_count or 0,
            "quote_count": a.quote_count or 0,
            "length": _body_length(body),
            "emoji_count": _emoji_count(body),
            "has_question": ("？" in body or "?" in body),
            "style": a.post_style or "不明",
            "posted_at": a.posted_at,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    with_data = len([s for s in scored if s["score"] > 0])

    def _avg(lst, key="score"):
        return round(sum(x[key] for x in lst) / len(lst), 1) if lst else 0

    n_top = max(3, len(scored) // 4)
    top_group = scored[:n_top]
    bottom_group = scored[-n_top:] if len(scored) > n_top * 2 else []

    # スタイル別
    style_map: dict = {}
    for s in scored:
        style_map.setdefault(s["style"], []).append(s["score"])
    style_stats = {
        st: {"count": len(v), "avg": round(sum(v) / len(v), 1), "max": max(v)}
        for st, v in style_map.items()
    }

    # 文字数バケット別
    buckets = {"〜60字": [], "61〜100字": [], "101〜150字": [], "151字〜": []}
    for s in scored:
        l = s["length"]
        if l <= 60:
            buckets["〜60字"].append(s["score"])
        elif l <= 100:
            buckets["61〜100字"].append(s["score"])
        elif l <= 150:
            buckets["101〜150字"].append(s["score"])
        else:
            buckets["151字〜"].append(s["score"])
    length_stats = {
        k: {"count": len(v), "avg": round(sum(v) / len(v), 1) if v else 0}
        for k, v in buckets.items()
    }

    # 絵文字あり/なし
    with_e = [s for s in scored if s["emoji_count"] > 0]
    wo_e = [s for s in scored if s["emoji_count"] == 0]
    emoji_stats = {
        "あり": {"count": len(with_e), "avg": _avg(with_e)},
        "なし": {"count": len(wo_e), "avg": _avg(wo_e)},
    }

    # 問いかけあり/なし
    with_q = [s for s in scored if s["has_question"]]
    wo_q = [s for s in scored if not s["has_question"]]
    question_stats = {
        "あり": {"count": len(with_q), "avg": _avg(with_q)},
        "なし": {"count": len(wo_q), "avg": _avg(wo_q)},
    }

    return {
        "total_posted": total_posted,
        "with_data": with_data,
        "enough_data": with_data >= 5,
        "top_posts": scored[:10],
        "all_avg_score": _avg(scored),
        "top_length_avg": _avg(top_group, "length"),
        "top_emoji_avg": round(_avg(top_group, "emoji_count"), 1),
        "style_stats": style_stats,
        "length_stats": length_stats,
        "emoji_stats": emoji_stats,
        "question_stats": question_stats,
        "top_group_avg": _avg(top_group),
        "bottom_group_avg": _avg(bottom_group),
    }


def _generate_hints(analysis: dict) -> str:
    """分析結果からプロンプトに挿入する学習ヒントテキストを生成する"""
    if not analysis.get("enough_data"):
        return ""

    lines = []

    # 最良スタイル
    style_stats = analysis.get("style_stats", {})
    best = max(
        ((st, v) for st, v in style_stats.items() if v["count"] >= 2),
        key=lambda x: x[1]["avg"],
        default=None,
    )
    if best:
        lines.append(f"・「{best[0]}」スタイルの平均エンゲージメントが最高 (avg {best[1]['avg']}pt)")

    # 最良文字数帯
    length_stats = analysis.get("length_stats", {})
    best_len = max(
        ((k, v) for k, v in length_stats.items() if v["count"] >= 2),
        key=lambda x: x[1]["avg"],
        default=None,
    )
    if best_len:
        lines.append(f"・{best_len[0]}の投稿が高エンゲージメント傾向 (avg {best_len[1]['avg']}pt)")

    # 絵文字
    e = analysis.get("emoji_stats", {})
    ea, ena = e.get("あり", {}).get("avg", 0), e.get("なし", {}).get("avg", 0)
    ec, enc = e.get("あり", {}).get("count", 0), e.get("なし", {}).get("count", 0)
    if ec >= 2 and enc >= 2:
        if ea > ena * 1.15:
            lines.append(f"・絵文字あり投稿が高エンゲージメント (avg {ea} vs {ena})")
        elif ena > ea * 1.15:
            lines.append(f"・絵文字なし投稿が高エンゲージメント (avg {ena} vs {ea})")

    # 問いかけ
    q = analysis.get("question_stats", {})
    qa, qna = q.get("あり", {}).get("avg", 0), q.get("なし", {}).get("avg", 0)
    qc, qnc = q.get("あり", {}).get("count", 0), q.get("なし", {}).get("count", 0)
    if qc >= 2 and qnc >= 2:
        if qa > qna * 1.15:
            lines.append(f"・問いかけあり投稿が高エンゲージメント (avg {qa} vs {qna})")
        elif qna > qa * 1.15:
            lines.append(f"・問いかけなし投稿が高エンゲージメント (avg {qna} vs {qa})")

    # 上位投稿の平均文字数
    top_len = analysis.get("top_length_avg", 0)
    if top_len:
        lines.append(f"・高エンゲージメント投稿の平均文字数: 約{top_len:.0f}字")

    if not lines:
        return ""

    ts = datetime.utcnow().strftime("%Y-%m-%d更新")
    return "({ts})\n".replace("{ts}", ts) + "\n".join(lines)


def update_learned_hints(app) -> dict:
    """分析して学習ヒントを Setting に保存する。分析結果 dict を返す。"""
    analysis = analyze_performance(app)
    hints = _generate_hints(analysis)

    with app.app_context():
        Setting.set("learned_style_hints", hints)

    if hints:
        logger.info("学習ヒント更新:\n%s", hints)
    else:
        logger.info("データ不足のため学習ヒントをクリア")

    analysis["hints"] = hints
    return analysis
