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
    fix = find_ng_words(html) + find_notation_violations(html) + find_marker_violations(html)
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
    pairs = [(word, replacement) for word, replacement, _reason in NOTATION_RULES]
    pairs += [(word, "") for word in DELETABLE_WORDS]
    return _replace_in_text(html, pairs)
