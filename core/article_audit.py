"""記事1本を1回で全項目判定する。

1つ直しては流すのを繰り返すと、同じ層の残りが次の実行で出てくる。仙台で6本
作り直したのはこれが原因。判定項目を先に全部列挙して、1回で見る。

項目を足すときはここに足す。散らばると「確認するたびに新しい不備が出る」状態に戻る。
"""

import re

from core import article_type_db, block_spec, output_check

# 表記のゆれを見る対象。1つの記事で2つ以上の書き方が混ざったら指摘する。
_TIME_STYLES = [
    ("半角コロン", re.compile(r"\d{1,2}:\d{2}")),
    ("全角コロン", re.compile(r"\d{1,2}：\d{2}")),
    ("時分の漢字", re.compile(r"\d{1,2}時\d{0,2}分?")),
    ("午前午後", re.compile(r"(?:午前|午後|AM|PM)\s*\d{1,2}")),
]
# 曜日の書き方は、範囲や並びになっているときだけ比べる。
# 「水曜定休」は単独の言い回しで、揃える対象ではない。
_DAY_STYLES = [
    ("曜日を省く", re.compile(r"[月火水木金土日][～〜\-–・、][月火水木金土日](?!曜)")),
    ("曜日を書く", re.compile(r"[月火水木金土日]曜日?[～〜\-–・、][月火水木金土日]")),
]

# かっこの多用。1000字あたりの数で見る。
BRACKET_LIMIT_PER_1000 = 12

_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)
_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S | re.I)
_PICKUP_SPLIT = "<div class=\"pickup\""


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "")


_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_PARA_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)


def _cells(html: str) -> str:
    """表のセルの文字だけ。表記の統一は表の中で見る。

    本文の「平日は21時まで診療」は自然な日本語で、24時間表記に直すほうがおかしい。
    揃っている必要があるのは、院ごとに並ぶ表の中である。
    """
    return " ".join(_text(m.group(1)) for m in _CELL_RE.finditer(html or ""))


def _paragraphs(html: str) -> str:
    """段落の文字だけ。かっこの多用は本文で見る。

    表の「（税込）」は必要な補足で、多いことが問題にならない。
    """
    return " ".join(_text(m.group(1)) for m in _PARA_RE.finditer(html or ""))


def _result(name: str, ok: bool, count: int = 0, detail: str = "") -> dict:
    return {"項目": name, "判定": "OK" if ok else "NG", "件数": count, "詳細": detail}


def check_notation_drift(html: str) -> list:
    """時刻と曜日の書き方が混ざっていないか。表のセルだけを見る。"""
    text = _cells(html)
    out = []
    for label, styles in (("時刻", _TIME_STYLES), ("曜日", _DAY_STYLES)):
        found = {name: len(pattern.findall(text)) for name, pattern in styles}
        used = {n: c for n, c in found.items() if c}
        if len(used) <= 1:
            out.append(_result(label + "の表記ゆれ", True))
            continue
        detail = "、".join(n + str(c) + "件" for n, c in used.items())
        out.append(_result(label + "の表記ゆれ", False, len(used), detail))
    return out


def check_brackets(html: str) -> list:
    """かっこの多用。本文の段落だけを見る。"""
    text = _paragraphs(html)
    if not text:
        return [_result("かっこの多用", True)]
    count = text.count("（") + text.count("「")
    per = count * 1000 / max(len(text), 1)
    ok = per <= BRACKET_LIMIT_PER_1000
    return [_result("かっこの多用", ok, count,
                    "1000字あたり" + str(round(per, 1)) + "個。上限"
                    + str(BRACKET_LIMIT_PER_1000) + "個")]


def clinic_blocks(html: str) -> list:
    """紹介ブロックを院ごとに切り出す。

    最後の院のあとには次のH2が続く。そこで切らないと、FAQやまとめの段落まで
    最後の院のものとして数えてしまう。仙台の1本目で段落が12本と出た。
    """
    parts = (html or "").split(_PICKUP_SPLIT)
    blocks = parts[1:]
    if blocks:
        tail = blocks[-1]
        cut = re.search(r"<h2[\s>]", tail)
        if cut:
            blocks[-1] = tail[:cut.start()]
    return blocks


def check_block_uniformity(html: str) -> list:
    """院ごとに部品の数がそろっているか。

    上位院と4位以降で画像とCTAの有無は変わってよい。表と段落の数は変わらない。
    """
    blocks = clinic_blocks(html)
    if not blocks:
        return [_result("紹介ブロックの粒度", False, 0, "紹介ブロックが1つも無い")]
    tables = [b.count("<table") for b in blocks]
    paragraphs = [b.count("<p>") for b in blocks]
    problems = []
    if len(set(tables)) > 1:
        problems.append("表の数が院で違う: " + str(tables))
    if len(set(paragraphs)) > 1:
        problems.append("段落の数が院で違う: " + str(paragraphs))
    return [_result("紹介ブロックの粒度", not problems, len(problems), " / ".join(problems)
                    or str(len(blocks)) + "院すべて表" + str(tables[0]) + "・段落"
                    + str(paragraphs[0]))]


def check_heading_counts(html: str, type_record: dict) -> list:
    """H2とH3の本数が型の範囲に収まっているか。"""
    if not type_record:
        return []
    out = []
    for label, regex, low_col, high_col in (
        ("H2の本数", _H2_RE, "H2総数の下限", "H2総数の上限"),
        ("H3の本数", _H3_RE, "H3総数の下限", "H3総数の上限"),
    ):
        low = str(type_record.get(low_col, "")).strip()
        high = str(type_record.get(high_col, "")).strip()
        if not (low or high):
            continue
        count = len(regex.findall(html or ""))
        ok = True
        note = str(count) + "本"
        if low.isdigit() and count < int(low):
            ok = False
            note += "。下限" + low + "本に足りない"
        if high.isdigit() and count > int(high):
            ok = False
            note += "。上限" + high + "本を超えている"
        out.append(_result(label, ok, count, note))
    return out


def check_required_blocks(html: str, type_record: dict) -> list:
    rules = article_type_db.required_rules(type_record or {})
    if not rules:
        return []
    lacking = article_type_db.missing_required(html, rules)
    return [_result("必須ブロック", not lacking, len(lacking),
                    article_type_db.format_missing(lacking) or str(len(rules)) + "件すべて有り")]


def check_reference_classes(html: str, reference: str, known_extra=None) -> list:
    if not reference and not known_extra:
        return []
    invented = output_check.find_invented_classes(html, reference, known_extra=known_extra)
    return [_result("見本に無いクラス名", not invented, len(invented),
                    "、".join(f["text"] for f in invented[:8]))]


def _group(name: str, findings: list, note_ok: str = "") -> dict:
    return _result(name, not findings, len(findings),
                   "、".join(f.get("text", "")[:24] for f in findings[:8]) or note_ok)


def audit(html: str, main_kw: str = "", sub_kw=None, clinic_count: int = 0,
          type_record: dict | None = None, reference: str = "",
          source_text: str = "", known_classes=None) -> list:
    """全項目を1回で判定して並べて返す。"""
    html = html or ""
    checks = []

    checks += check_heading_counts(html, type_record or {})
    checks += check_required_blocks(html, type_record or {})
    checks += check_block_uniformity(html)
    checks += check_reference_classes(html, reference, known_extra=known_classes)

    checks.append(_group("タグの数の不一致", output_check.find_unclosed_tags(html)))
    checks.append(_group("Markdownの混入", output_check.find_markdown(html)))
    checks.append(_group("禁止ワード", output_check.find_ng_words(html)))
    checks.append(_group("ジャンルの禁止表現", output_check.find_genre_ng_words(html)))
    checks.append(_group("提携申請の表記ルール", output_check.find_notation_violations(html)))
    checks.append(_group("マーカー", output_check.find_marker_violations(html)))
    checks.append(_group("長い段落", output_check.find_long_paragraphs(html)))
    checks.append(_group("読者向けでない文", output_check.find_self_talk(html)))
    checks.append(_group("途中で切れた文", output_check.find_truncated_text(html)))
    checks.append(_group("キーワードのまま詰め込み",
                         output_check.find_keyword_stuffing(html, main_kw, sub_kw)))
    checks.append(_group("見出しの院数と紹介数",
                         output_check.find_count_mismatch(html, clinic_count)))
    checks.append(_group("出典なしの数値",
                         output_check.find_unsourced_claims(html)))
    if source_text:
        checks.append(_group("入力データに無い金額",
                             output_check.find_unverified_numbers(html, source_text)))

    checks += check_notation_drift(html)
    checks += check_brackets(html)
    return checks


def format_report(checks: list) -> str:
    """判定を1行1項目で書き出す。OKも出す。何を見たかが残る。"""
    if not checks:
        return "判定項目がありません"
    width = max(len(c["項目"]) for c in checks)
    lines = []
    for check in checks:
        mark = "OK " if check["判定"] == "OK" else "NG "
        name = check["項目"].ljust(width, "　")
        detail = check["詳細"]
        lines.append(mark + name + "  " + (detail if detail else ""))
    ng = sum(1 for c in checks if c["判定"] == "NG")
    lines.append("")
    lines.append("判定項目 " + str(len(checks)) + "件 / NG " + str(ng) + "件")
    return "\n".join(lines)
