"""動画投稿の投稿文(グループ名→メンバー名→曲名→フック)の組み立て。

ニュース記事(RSS記事)用のAI要約・投稿文生成は廃止した。動画投稿の投稿文はAIを使わず決定的に
組み立てる。"""
import logging
import re
from datetime import datetime

from database import Article, Group, Hook, Member, db

logger = logging.getLogger(__name__)

BODY_MAX_VIDEO = 50   # 動画投稿の文字数上限


def _get_next_hook(app, account_id: int) -> str | None:
    """アカウントのフックをローテーションで1件取得し、last_used_atを更新する。
    未使用のフックが常に最優先（last_used_at IS NULLはASC順で先頭に来る）。"""
    with app.app_context():
        hook = (
            Hook.query.filter_by(account_id=account_id)
            .order_by(Hook.last_used_at.asc(), Hook.display_order.asc())
            .first()
        )
        if not hook:
            return None
        hook.last_used_at = datetime.utcnow()
        db.session.commit()
        return hook.phrase


# 動画タイトルの曲名抽出用: KPOPのMVタイトルは曲名を引用符で囲む慣習が強いため、
# 最初に見つかった引用符内テキストを曲名候補として採用する（優先度順）。
# ' と " は英語の曲名中の短縮形（例: 'It's Me'、'Don't Say Love'）にも使われるため、
# 単純に「引用符以外の文字」で内容を区切ると短縮形のアポストロフィを閉じ引用符と
# 誤認識してしまう（例: 'It's Me' → 'It' だけを抽出してしまう）。
# そのため閉じ引用符の直後に英字が続く場合はその引用符を無視する
# (負の先読み)ことで、短縮形を含む曲名も正しく1つの塊として抽出する。
_SONG_TITLE_QUOTE_PATTERNS = [
    re.compile(r"'(.{1,60}?)'(?![a-zA-Z])"),
    re.compile(r'"(.{1,60}?)"(?![a-zA-Z])'),
    re.compile(r'「([^」]{1,60})」'),
    re.compile(r'『([^』]{1,60})』'),
    re.compile(r'“([^”]{1,60})”'),
    re.compile(r'‘([^’]{1,60})’'),
    # 開き引用符と閉じ引用符の種類が不一致な表記ゆれ対策(例: 'COME OVER’ のように
    # 半角'で開いて全角’で閉じるケース)。上記の厳密なパターンで抽出できない場合のみ
    # フォールバックとして試す(誤爆を避けるため優先度は最後)。
    re.compile(r"['‘’](.{1,60}?)['‘’](?![a-zA-Z])"),
]

# ── ハイフン区切りの曲名抽出(グループ名タグ付き記事専用) ──────────────────────
# DBの実タイトルを分析した結果、引用符なしで「グループ名(+メンバー名/他言語表記)
# - 曲名」の順にハイフンで区切られるケースが一定数あることが分かった
# (例: "LE SSERAFIM - HOT | Show! MusicCore..."、
#      "aespa KARINA (에스파 카리나) – LEMONADE | ...")。
# 一方でハイフンは撮影日・チャンネル名・「曲名 - グループ名」の逆順など曲名以外にも
# 多用されるため、以下の条件をすべて満たす場合のみ曲名候補として採用する
# (満たさない場合は誤検出のリスクを避け、これまで通り抽出しない):
#   1. 記事に確定したグループ名(タグ付け済み)があること
#   2. そのグループ名の直後 _GROUP_HYPHEN_WINDOW 文字以内にハイフンが現れること
#      (メンバー名や他言語での重複表記が間に挟まる程度は許容するが、離れすぎている
#      ハイフンは無関係な区切りである可能性が高いため対象外とする)
#   3. ハイフン後の区切り文字(| ( ) [ ] @ 次のハイフン 全角パイプ代用字 など)までを
#      候補とし、「직캠」「Fancam」等の撮影クレジット語を末尾から除去すること
#   4. 除去後も _GROUP_HYPHEN_MAX_LEN 文字を超える場合は区切り文字を検出できていない
#      (曲名ではなく後続の説明文を丸ごと拾っている)とみなし、採用しない
_GROUP_HYPHEN_WINDOW = 40
_GROUP_HYPHEN_MAX_LEN = 30
_GROUP_HYPHEN_STOP_RE = re.compile(r"[|()\[\]@–—\-ㅣ]|\s[lI]\s")
_GROUP_HYPHEN_NOISE_WORDS = (
    "fancam", "facecam", "stagecam", "stage cam",
    "직캠", "얼빡직캠", "페이스캠", "m/v", "mv", "live", "ver.", "ver", "cover", "cam",
)


def _strip_trailing_noise_words(text: str) -> str:
    text = text.strip()
    changed = True
    while changed:
        changed = False
        for word in _GROUP_HYPHEN_NOISE_WORDS:
            pattern = re.compile(r"\s*\b" + re.escape(word) + r"\b\s*$", re.IGNORECASE)
            stripped = pattern.sub("", text)
            if stripped != text:
                text = stripped.strip()
                changed = True
    return text


def _extract_song_title_after_group_hyphen(title: str, group_name: str) -> str:
    """タグ付け済みグループ名の直後(_GROUP_HYPHEN_WINDOW文字以内)に現れるハイフンから、
    次の区切り文字までを曲名候補として抽出する。条件を満たさなければ空文字を返す。"""
    if not group_name:
        return ""
    idx = title.lower().find(group_name.lower())
    if idx < 0:
        return ""
    search_start = idx + len(group_name)
    window = title[search_start:search_start + _GROUP_HYPHEN_WINDOW]
    hyphen_m = re.search(r"[\-–—]", window)
    if not hyphen_m:
        return ""
    hyphen_pos = search_start + hyphen_m.start()
    rest = title[hyphen_pos + 1:]
    stop_m = _GROUP_HYPHEN_STOP_RE.search(rest)
    candidate = rest[:stop_m.start()] if stop_m else rest
    candidate = _strip_trailing_noise_words(candidate)
    if not candidate or len(candidate) > _GROUP_HYPHEN_MAX_LEN:
        return ""
    return candidate


def _extract_song_title(title: str, group_name: str = "") -> str:
    """動画タイトルから曲名らしき部分を抽出する。抽出できなければ空文字を返す
    (呼び出し側でスキップする)。

    1. まず引用符（'…'・"…"・「…」等、表記ゆれ含む）で囲まれた部分を最優先で試す。
    2. 引用符で見つからず、かつタグ付け済みのgroup_nameが分かっている場合のみ、
       「グループ名の直後のハイフン区切り」パターンを試す（詳細は_GROUP_HYPHEN_*の
       コメント参照）。group_name未指定時はこれまで通り引用符のみで判定する。
    """
    if not title:
        return ""
    for pattern in _SONG_TITLE_QUOTE_PATTERNS:
        m = pattern.search(title)
        if m:
            candidate = m.group(1).strip()
            if candidate:
                return candidate
    return _extract_song_title_after_group_hyphen(title, group_name)


def _build_video_post_text(
    group_name: str, member_name: str, song_title: str, hook: str | None, body_max: int,
) -> str:
    """動画投稿文を「グループ名→メンバー名→曲名→フック」の順で組み立てる。
    タグ付けされていない・抽出できない要素はスキップする。
    文字数オーバー時は 曲名 → メンバー名 の順に削って再構築し、それでも収まらない場合は
    グループ名+フックを優先して残し、フック自体を末尾から切り詰める(_attach_hookと同じ安全策)。"""
    hook = hook or ""

    def _compose(use_song: bool, use_member: bool) -> str:
        tags = [group_name or ""]
        if use_member:
            tags.append(member_name or "")
        if use_song and song_title:
            tags.append(f"「{song_title}」")
        tag_text = " ".join(t for t in tags if t)
        return f"{tag_text} {hook}".strip() if hook else tag_text

    for use_song, use_member in ((True, True), (False, True), (False, False)):
        combined = _compose(use_song, use_member)
        if len(combined) <= body_max:
            return combined

    # ここまで削ってもオーバー: グループ名を残し、フック自体を切り詰める
    prefix = f"{group_name} " if group_name else ""
    if hook:
        remaining = body_max - len(prefix)
        if remaining >= 2:
            hook_fit = hook if len(hook) <= remaining else hook[:remaining - 1] + "…"
            return prefix + hook_fit
    combined = (prefix + hook).strip() or group_name or hook
    return combined[:body_max - 1] + "…" if len(combined) > body_max else combined


def _save_error(app, article_id: int, message: str) -> None:
    """エラーメッセージをDBに保存するヘルパー。"""
    with app.app_context():
        art = db.session.get(Article, article_id)
        if art:
            art.error_message = message
            db.session.commit()


def summarize_article(
    app, article_id: int, style: str = "つぶやき型", scheduled_at: str | None = None,
    preview_group_name: str | None = None, preview_member_name: str | None = None,
) -> bool:
    """動画投稿の投稿文を「グループ名→メンバー名→曲名→フック」で組み立てて DB に保存する。成功なら True。

    動画以外(ニュース記事など)のAI要約・投稿文生成は廃止したため、動画以外の記事に対してはエラー
    メッセージを保存して False を返す(投稿文は手入力する)。scheduled_at は互換のために残している(未使用)。

    preview_group_name/preview_member_name: 承認前プレビュー用の一時的なタグ指定
    (Noneでなければ、article.group_id/member_idの代わりにこちらを使って動画投稿文を
    組み立てる。DBのgroup_id/member_idは変更しない — 承認モーダル入力中の「要約を生成」
    プレビュー専用)。
    """
    logger.info(
        "[summarize_article] article=%d style=%r preview_group=%r preview_member=%r",
        article_id, style, preview_group_name, preview_member_name,
    )

    with app.app_context():
        article = db.session.get(Article, article_id)
        if not article:
            logger.error("article id=%d が見つかりません", article_id)
            return False
        title         = article.title
        content_type  = article.content_type or "article"
        article_account_id = article.account_id
        tagged_group_name  = ""
        tagged_member_name = ""
        if preview_group_name is not None:
            # 承認モーダル入力中のプレビュー: DBのgroup_id/member_idは参照・変更しない。
            tagged_group_name = preview_group_name.strip()
            tagged_member_name = (preview_member_name or "").strip() if tagged_group_name else ""
        else:
            if article.group_id:
                g = db.session.get(Group, article.group_id)
                if g:
                    tagged_group_name = g.name
                else:
                    logger.warning(
                        "article=%d: group_id=%d が groups マスタに存在しません(削除済み参照?)。"
                        "グループ名なしで組み立てます。", article_id, article.group_id,
                    )
            if article.member_id:
                m = db.session.get(Member, article.member_id)
                if m:
                    tagged_member_name = m.name
                else:
                    logger.warning(
                        "article=%d: member_id=%d が members マスタに存在しません(削除済み参照?)。"
                        "メンバー名なしで組み立てます。", article_id, article.member_id,
                    )

    if content_type != "video":
        msg = "ニュース記事のAI要約は廃止されました。投稿文は手入力してください（動画投稿のみ自動で組み立てます）。"
        logger.info("[summarize_article] article=%d content_type=%s は自動生成の対象外", article_id, content_type)
        _save_error(app, article_id, msg)
        return False

    body_max = BODY_MAX_VIDEO
    hook = _get_next_hook(app, article_account_id) if article_account_id else None

    # 動画投稿: AIを使わず「グループ名→メンバー名→曲名→フック」で決定的に組み立てる
    # (承認モーダルでタグ付けされたgroup_id/member_idと、タイトルから抽出した曲名を使う。
    # タグ付けされていない・抽出できない要素はスキップし、残りの要素だけで組み立てる)
    song_title = _extract_song_title(title, tagged_group_name)
    post_text = _build_video_post_text(tagged_group_name, tagged_member_name, song_title, hook, body_max)
    with app.app_context():
        art = db.session.get(Article, article_id)
        if art:
            art.summary       = post_text
            art.post_style    = style
            art.error_message = None
            # 自動組み立てで上書きするため、手動編集フラグは解除する
            # (このパス自体はapprove_article側でsummary_is_manual=Trueなら呼ばれない)。
            art.summary_is_manual = False
            db.session.commit()
    logger.info(
        "動画投稿テキストを組み立て: article=%d group=%r member=%r song=%r hook=%r (%d文字)",
        article_id, tagged_group_name or None, tagged_member_name or None,
        song_title or None, hook, len(post_text),
    )
    return True
