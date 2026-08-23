"""生成後のHTMLを機械照合する検出層。

禁止リストをプロンプトに並べても守られないため（NGワードの大半はすでに
WRITING_RULES に登録済みなのに出力に混入している）、出力側を機械で見る。

指摘の扱いは2系統に分ける。
- 表現・表記・マーカー … AIに差分修正させてよい
- 事実・出典        … 入力データとのズレなのでAIに直させず人のキューへ回す
  （直させると辻褄合わせで嘘を作る）
"""

import re

# ── 禁止ワード ───────────────────────────────────────────
# core/writer.py の WRITING_RULES「絶対NGワード」と同一の語を機械検出用に持つ。
# writer.py 側を変えたらここも揃えること。
NG_WORDS = [
    # 抽象語
    "傾向", "設計", "前提", "把握", "整理", "方向性", "実態", "重要",
    # 結論をぼかす
    "現実的な選択肢", "後悔しにくい", "失敗しにくい", "納得感のある",
    # テンプレ語
    "もちろん", "大切です", "なお、", "順番に", "動線", "糸抜き",
    # 不自然な強調
    "救世主", "味方", "第一歩", "近道", "スムーズ", "最適", "最もふさわしい", "活用",
    # 断定・最上級
    "唯一の", "唯一無二", "全員", "必ず誰でも", "どんな方でも",
    "最大の要因", "最も重要な", "最大の特徴", "失敗しない", "絶対に",
    "最高の", "最善の", "最もおすすめ", "誰でも", "誰もが",
    # 記事内参照
    "上記", "下記", "以下のとおり", "次のとおり", "本記事", "この記事", "先ほど", "前述",
    # 定型パターン
    "多くの方が迷うのが", "迷う方も少なくありません", "というケースが多く", "広告料金",
]

# ── 表記ルール ───────────────────────────────────────────
# 提携申請で表記修正を依頼される型。サイトを問わず共通なので固定で持つ。
# (検出する語, 言い換え, 理由)
NOTATION_RULES = [
    ("安全性", "品質性", "提携申請で修正依頼が来る"),
    ("購入", "入手", "提携申請で修正依頼が来る"),
    ("オンライン完結", "オンラインで手続きができる", "対面診療との組み合わせが基本のため完結表現は不可"),
    ("オンラインで完結", "オンラインで手続きができる", "対面診療との組み合わせが基本のため完結表現は不可"),
    ("電話のみ", "音声と映像による診療", "音声と映像が原則のため電話だけで診療できる表現は不可"),
    ("電話だけで", "音声と映像による診療", "音声と映像が原則のため電話だけで診療できる表現は不可"),
]

MARKER_MAX_CHARS = 30

_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_MARKER_RE = re.compile(
    r'<(\w+)[^>]*class="[^"]*mark[^"]*"[^>]*>(.*?)</\1>', re.S | re.I
)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
_PRICE_RE = re.compile(r"([0-9][0-9,]*)\s*円")
_PERCENT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*[%％]")


def _strip_tags(html: str) -> str:
    return _TAG_RE.sub("", _COMMENT_RE.sub("", html))


def _snippet(text: str, keyword: str, width: int = 30) -> str:
    idx = text.find(keyword)
    if idx < 0:
        return text[:width]
    start = max(0, idx - width // 2)
    return text[start:idx + len(keyword) + width // 2].replace("\n", " ")


def find_ng_words(html: str) -> list[dict]:
    """禁止ワードの混入を拾う。"""
    text = _strip_tags(html)
    findings = []
    for word in NG_WORDS:
        if word in text:
            findings.append({
                "rule": "禁止ワード",
                "text": word,
                "detail": f"該当箇所: {_snippet(text, word)}",
            })
    return findings


def find_notation_violations(html: str) -> list[dict]:
    """提携申請で修正依頼が来る表記を拾う。"""
    text = _strip_tags(html)
    findings = []
    for ng, ok, note in NOTATION_RULES:
        if ng in text:
            findings.append({
                "rule": "表記ルール",
                "text": ng,
                "detail": f"{ng} → {ok}（{note}）／該当箇所: {_snippet(text, ng)}",
            })
    return findings


def find_marker_violations(html: str) -> list[dict]:
    """マーカーの字数超過と句点混入を拾う。"""
    findings = []
    for match in _MARKER_RE.finditer(html):
        inner = _strip_tags(match.group(2)).strip()
        if not inner:
            continue
        if len(inner) > MARKER_MAX_CHARS:
            findings.append({
                "rule": "マーカー字数超過",
                "text": inner,
                "detail": f"{len(inner)}字。{MARKER_MAX_CHARS}字以内に縮める",
            })
        if "。" in inner:
            findings.append({
                "rule": "マーカー内の句点",
                "text": inner,
                "detail": "マーカー内に句点を入れない。囲む範囲を核心フレーズだけに縮める",
            })
    return findings


def find_unverified_numbers(html: str, source_text: str) -> list[dict]:
    """記事の金額・割合が入力データに存在するかを照合する。

    入力データに無い数値は人のキューへ回す。AIに直させない。
    """
    if not source_text.strip():
        return []
    text = _strip_tags(html)
    source = source_text.replace(",", "")
    findings = []
    seen = set()
    for regex, unit, label in ((_PRICE_RE, "円", "金額"), (_PERCENT_RE, "%", "割合")):
        for match in regex.finditer(text):
            raw = match.group(1)
            key = f"{raw}{unit}"
            if key in seen:
                continue
            seen.add(key)
            if raw.replace(",", "") in source:
                continue
            findings.append({
                "rule": f"入力データにない{label}",
                "text": key,
                "detail": f"案件DBに {key} が見つからない／該当箇所: {_snippet(text, match.group(0))}",
            })
    return findings


def find_unsourced_claims(html: str) -> list[dict]:
    """割合・倍率を含む段落に出典リンクが無いものを拾う。"""
    findings = []
    for match in _P_RE.finditer(html):
        block = match.group(1)
        plain = _strip_tags(block)
        has_claim = bool(_PERCENT_RE.search(plain)) or "人に1人" in plain or re.search(r"[0-9]+\s*倍", plain)
        if not has_claim:
            continue
        if "<a " in block.lower():
            continue
        findings.append({
            "rule": "出典なしの数値・医学的主張",
            "text": plain[:60],
            "detail": "公的機関・学会などの出典URLを添える",
        })
    return findings


def run_checks(html: str, source_text: str = "") -> dict:
    """全チェックを回して、AIに直させるものと人が見るものに分けて返す。

    Returns: {"fix": [...], "human": [...]}
    """
    fix = (
        find_ng_words(html)
        + find_notation_violations(html)
        + find_marker_violations(html)
        + find_long_paragraphs(html)
    )
    human = find_unverified_numbers(html, source_text) + find_unsourced_claims(html)
    return {"fix": fix, "human": human}


def format_findings(findings: list[dict]) -> str:
    """指摘一覧を読める形に整える。"""
    if not findings:
        return "指摘なし"
    return "\n".join(f"- [{f['rule']}] {f['text']}\n　{f['detail']}" for f in findings)


def build_fix_instruction(findings: list[dict]) -> str:
    """AIに渡す差分修正の指示文を作る。全文再生成させない。"""
    if not findings:
        return ""
    return (
        "以下の指摘箇所だけを書き換えてください。指摘のない箇所は1文字も変更しないでください。\n"
        "全文を作り直すと直っていない箇所が壊れるため、差分修正に限定します。\n\n"
        + format_findings(findings)
    )


# 言い換え先が日本語として成立しない組み合わせ。検出はするが機械で置き換えない。
# 品質性は辞書にない語なので、置き換えると文章が壊れる。人かモデルが直す。
NO_AUTO_REPLACE = {"安全性"}

# 禁止ワードのうち、文脈に関係なく同じ言い換えで通るもの。
# 実際に出た使われ方から作った。1語だけを置き換えると助詞が壊れるので、
# 前後を含んだ形で持つ。ここに無い語はモデルか人が直す。
NG_PHRASE_REPLACEMENTS = [
    ("が前提となります", "が必要になります"),
    ("が前提となるため", "が必要になるため"),
    ("が前提の", "が必要な"),
    ("を前提に", "をもとに"),
    ("を前提としない", "を必要としない"),
    ("を前提とせず", "を必要とせず"),
    ("前提とした", "を必要とした"),
    ("で整理しました", "でまとめました"),
    ("を整理します", "をまとめます"),
    ("を整理しました", "をまとめました"),
    ("整理しました", "まとめました"),
    ("整理します", "まとめます"),
    ("コース契約が前提", "コース契約のみ"),
    ("正確に把握できます", "正確に分かります"),
    ("を把握できます", "が分かります"),
    ("把握しておく", "確認しておく"),
    ("柔軟なプラン設計", "選べるプラン構成"),
    ("オーダーメイド設計", "オーダーメイドの組み方"),
    ("立てにくい設計", "立てにくい仕組み"),
    ("設計になっています", "仕組みになっています"),
    ("設計を採用", "組み方を採用"),
    ("最適な機種", "肌質に合う機種"),
    ("最適な", "合った"),
    ("施術の安全性に関わります", "施術中の肌への負担に関わります"),
    ("プラン設計", "プラン構成"),
    ("重要な判断材料になります", "選ぶときの判断材料になります"),
    ("が重要です", "で選べます"),
    ("を把握しておく", "を確認しておく"),
    ("を把握しやすい", "が分かりやすい"),
    ("を把握できる", "が分かる"),
    ("把握", "確認"),
    ("ことが大切です", "ことで選べます"),
    ("が大切です", "で判断できます"),
    ("は大切です", "で決まります"),
    ("大切です", "判断の分かれ目になります"),
    ("ご活用ください", "お使いください"),
    ("活用して", "使って"),
    ("活用できます", "使えます"),
    ("を活用", "を利用"),
    ("を整理しています", "をまとめています"),
    ("を整理することで", "をそろえることで"),
    ("を整理して", "をそろえて"),
    ("整理する", "そろえる"),
    ("重要な判断材料", "選ぶときの判断材料"),
    ("が重要な判断軸", "が選ぶときの判断軸"),
    ("重要な", "見落とせない"),
    ("を整理することで", "をそろえることで"),
    ("整理して", "まとめて"),
    ("スムーズに進められます", "迷わず進められます"),
    ("スムーズに", "滞りなく"),
    ("実態", "実際の状況"),
    ("傾向があります", "ことが多いです"),
    ("傾向が強く", "ことが多く"),
    ("傾向", "ことが多い点"),
]

# 削るだけで文が成立する語。置き換え先を考える必要がないのでここで消す。
DELETABLE_WORDS = ["もちろん", "なお、", "順番に"]

_TAG_SPLIT_RE = re.compile(r"(<[^>]*>)")


def _replace_in_text(html: str, pairs: list) -> tuple:
    """タグの外側だけを置き換える。

    タグの中を触るとクラス名やURLが壊れる。置き換えた組み合わせも返す。
    """
    done = []
    parts = _TAG_SPLIT_RE.split(html)
    for i, part in enumerate(parts):
        if part.startswith("<"):
            continue
        for before, after in pairs:
            if before and before in part:
                part = part.replace(before, after)
                if before not in [d[0] for d in done]:
                    done.append((before, after))
        parts[i] = part
    return "".join(parts), done


def apply_mechanical_fixes(html: str) -> tuple:
    """置き換え先が決まっている違反を機械で直す。

    表記ルールは言い換えが定義済み。削るだけで通る語も一緒に処理する。
    禁止ワードのうち文の作り直しが要るものはここでは触らない。
    Returns: (直したHTML, [(直した語, 直した後)])
    """
    pairs = [
        (word, replacement)
        for word, replacement, _reason in NOTATION_RULES
        if word not in NO_AUTO_REPLACE
    ]
    # 長い言い回しから先に当てる。「傾向があります」より先に「傾向」を当てると
    # 「ことが多い点があります」になって日本語が壊れる。
    pairs += NG_PHRASE_REPLACEMENTS
    pairs += [(word, "") for word in DELETABLE_WORDS]
    return _replace_in_text(html, pairs)


# 見本に無いクラス名をモデルが作ることがある。h2-title・h2-ttl など。
# 装飾が効かない状態で公開されるので、生成後に機械で拾う。
def find_invented_classes(html: str, reference_html: str) -> list:
    """見本に出てこないクラス名を返す。"""
    if not reference_html:
        return []
    known = set(re.findall(r'class="([^"]+)"', reference_html))
    known_words = set()
    for value in known:
        known_words.update(value.split())
    findings = []
    seen = set()
    for value in re.findall(r'class="([^"]+)"', html):
        for word in value.split():
            if word in known_words or word in seen:
                continue
            seen.add(word)
            findings.append({
                "rule": "見本に無いクラス名",
                "text": word,
                "detail": "見本に出てこないクラス名。装飾が効かない可能性がある",
            })
    return findings


MAX_SENTENCES_PER_PARAGRAPH = 3


def find_long_paragraphs(html: str) -> list:
    """1段落4文以上を拾う。ライティングルールは3文以内。"""
    findings = []
    for match in _P_RE.finditer(html):
        plain = _strip_tags(match.group(1)).strip()
        if plain.count("。") <= MAX_SENTENCES_PER_PARAGRAPH:
            continue
        findings.append({
            "kind": "表現",
            "rule": f"1段落{MAX_SENTENCES_PER_PARAGRAPH}文以内",
            "text": plain,
            "detail": f"{plain.count('。')}文ある。段落を分けるか箇条書きにする",
        })
    return findings


# 仕様として置かないと決めたものは、指摘が出ても意味がない。
# 4位以降にリンクとCTAを置かないのは決定事項なので、その指摘は捨てる。
_DROP_TODO_PATTERNS = [
    "送客リンク", "CTAボタン", "CTA設置", "リンク・CTA",
]


def strip_useless_todo(todo_text: str) -> tuple:
    """要確認欄から、仕様として対応不要な行を落とす。

    Returns: (残した本文, 落とした件数)
    """
    if not todo_text:
        return todo_text, 0
    kept, dropped = [], 0
    for line in todo_text.split(chr(10)):
        body = line.strip().lstrip("-").strip()
        if body and any(word in body for word in _DROP_TODO_PATTERNS):
            dropped += 1
            continue
        kept.append(line)
    return chr(10).join(kept).strip(), dropped


def split_long_paragraphs(html: str) -> tuple:
    """4文以上の段落を句点で分けて別々の段落にする。

    モデルに分けさせると、直したそばから別の違反が出る。ここは文を触らず
    段落タグを増やすだけなので機械でできる。
    Returns: (直したHTML, 分けた段落の数)
    """
    count = 0

    def _split(match):
        nonlocal count
        opening, inner = match.group(1), match.group(2)
        plain = _strip_tags(inner)
        if plain.count("。") <= MAX_SENTENCES_PER_PARAGRAPH:
            return match.group(0)
        # タグをまたぐ段落は触らない。壊す危険のほうが大きい
        if "<" in inner:
            return match.group(0)
        parts = [x for x in inner.split("。") if x.strip()]
        if len(parts) <= MAX_SENTENCES_PER_PARAGRAPH:
            return match.group(0)
        half = (len(parts) + 1) // 2
        first = "。".join(parts[:half]) + "。"
        second = "。".join(parts[half:]) + "。"
        count += 1
        return f"{opening}{first}</p>{chr(10)}{opening}{second}</p>"

    # 1回の分割で半分にしても、元が7文なら4文の段落が残る。残らなくなるまで繰り返す。
    fixed = html
    for _ in range(4):
        before = fixed
        fixed = re.sub(r"(<p[^>]*>)(.*?)</p>", _split, fixed, flags=re.S)
        if fixed == before:
            break
    return fixed, count


_CELL_RE = re.compile(r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.S)
_TODO_MARK_RE = re.compile(r"\[要確認[^\]]*\]")


def replace_todo_in_cells(html: str) -> tuple:
    """テーブルのセルに残った要確認の印を「−」に置き換える。

    セルは空欄か「−」で意味が通る。印が残ったまま公開されると事故になる。
    本文の文章に出る印はそのまま残す。人が後で埋める目印として要る。
    Returns: (直したHTML, 置き換えた数)
    """
    count = 0

    def _fix(match):
        nonlocal count
        opening, inner, closing = match.groups()
        if not _TODO_MARK_RE.search(inner):
            return match.group(0)
        cleaned = _TODO_MARK_RE.sub("", inner).strip()
        count += len(_TODO_MARK_RE.findall(inner))
        return f"{opening}{cleaned or '−'}{closing}"

    return _CELL_RE.sub(_fix, html), count


# 1案件あたりの送客リンクの上限。埼玉で1案件14本張られた。
# 同じリンクを本文に繰り返すと読者にも検索エンジンにも不自然になる。
MAX_LINKS_PER_CLINIC = 6

_ANCHOR_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)


def limit_affiliate_links(html: str, max_per_clinic: int = MAX_LINKS_PER_CLINIC) -> tuple:
    """1案件あたりの送客リンクを上限までに減らす。

    超えた分はリンクを外してテキストだけ残す。画像を包むリンクは外さない。
    前から順に残すので、冒頭の比較表と紹介ブロックのリンクが優先される。
    Returns: (直したHTML, 外した本数)
    """
    counts: dict = {}
    removed = 0

    def _fix(match):
        nonlocal removed
        url, inner = match.group(1), match.group(2)
        if "noxclinic" not in url:
            return match.group(0)
        # 画像リンクは残す。外すと画像だけが浮く
        if "<img" in inner.lower():
            return match.group(0)
        key = url.split("/")[-1].split(".html")[0]
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= max_per_clinic:
            return match.group(0)
        removed += 1
        return inner

    return _ANCHOR_RE.sub(_fix, html), removed
