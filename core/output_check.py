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
    ("を把握しておく", "を確認しておく"),
    ("を把握しやすい", "が分かりやすい"),
    ("を把握できる", "が分かる"),
    ("把握", "確認"),
    ("スムーズな通院計画", "無理のない通院計画"),
    ("スムーズな", "滞りない"),
    ("その活用方法", "その使い方"),
    ("活用方法", "使い方"),
    ("ことが大切です", "ことで判断できます"),
    ("ことが重要であり", "ことで結果が変わり"),
    ("施術の安全性", "施術中の肌への負担"),
    ("安全性の高い", "肌への負担が少ない"),
    ("安全性に配慮", "肌への負担に配慮"),
    ("安全性を重視", "肌への負担の少なさを重視"),
    ("安全性", "施術の質"),
    ("スムーズにストレスなく", "無理なく"),
    ("スムーズでストレスなく", "無理なく"),
    ("を活用することで", "を使うことで"),
    ("割引を活用", "割引を使うこと"),
    ("を活用", "を使うこと"),
    ("スムーズで", "滞りなく"),
    ("最大限に活用し", "無駄なく使い"),
    ("を活用することで", "を使うことで"),
    ("に活用し", "を使い"),
    ("に最適です", "に向いています"),
    ("最適です", "向いています"),
    ("を確認する重要性", "を確認しておく理由"),
    ("する重要性", "しておく理由"),
    ("を有効活用し", "を無駄なく使い"),
    ("有効活用", "無駄なく使うこと"),
    ("の重要性", "が効いてくる理由"),
    ("どの脱毛機が最適か", "どの脱毛機が肌に合うか"),
    ("に活用することで", "を使うことで"),
    ("の活用法", "の使い方"),
    ("最大限に活用する", "使い切る"),
    ("本記事では、", ""),
    ("本記事では", ""),
    ("本記事", "このページ"),
    ("ことが大切です", "ことで選べます"),
    ("が大切です", "で判断できます"),
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
    # 助詞を巻き込まない言い換え。が・も・は・に のどれが前に来ても成立する形にする。
    ("重要です", "大きく効いてきます"),
    ("重要でした", "大きく効いてきました"),
    ("重要になります", "大きく効いてきます"),
    ("重要となります", "大きく効いてきます"),
    ("重要でしょう", "大きく効いてきます"),
    ("重要視されます", "大きく見られます"),
    ("大切です", "判断の分かれ目になります"),
    ("大切でした", "判断の分かれ目でした"),
    ("重要な判断材料になります", "選ぶときの判断材料になります"),
    ("を活用できます", "を使えます"),
    ("を把握できます", "が分かります"),
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

# 置換が日本語を壊した形。置換後にこれが出たら、その1件だけ元に戻す。
# 「も重要です」に「も重要→も効いてきます」が当たって「も効いてきますです」に
# なった実例から作った。置換表を広げるたびに同じ事故が起きるので、表ではなく
# 出口で止める。ますので・ますが・ますと は正しい日本語なので入れない。
BROKEN_PATTERNS = [
    "ますです", "ますでし", "ますな", "ますに", "ますを", "ますは", "ますも",
    "ますだ", "ますある", "ますこと", "ますとき", "ますため", "ますよう",
    "ませんです", "ますました", "ますません",
    "でで", "にに", "をを", "がが", "はは", "のの",
    # 置換後が「で〜」で始まる語と、直前の助詞がぶつかる形
    "はで決まり", "もで決まり", "とで決まり", "にで決まり", "をで決まり",
    "はで差が", "もで差が", "とで差が", "にで差が", "をで差が",
    "はで選べ", "もで選べ", "とで選べ", "にで選べ", "をで選べ",
    "はで判断", "もで判断", "とで判断", "にで判断", "をで判断",
    "はで続け", "もで続け", "とで続け", "にで続け", "をで続け",
]

_MASUNO_OK = re.compile(r"ますの[でにか]")


def _has_broken(text: str) -> bool:
    """置換が壊した日本語が入っているか。"""
    for pattern in BROKEN_PATTERNS:
        if pattern not in text:
            continue
        if pattern == "ますの" and _MASUNO_OK.search(text):
            # ますので・ますのに・ますのか は成立する
            if len(_MASUNO_OK.findall(text)) >= text.count("ますの"):
                continue
        return True
    return False


def _replace_in_text(html: str, pairs: list) -> tuple:
    """タグの外側だけを置き換える。

    タグの中を触るとクラス名やURLが壊れる。置き換えた組み合わせも返す。
    1件ずつ当てて、その置換が日本語を壊したら戻す。表の並び順に頼らない。
    """
    done = []
    parts = _TAG_SPLIT_RE.split(html)
    for i, part in enumerate(parts):
        if part.startswith("<"):
            continue
        for before, after in pairs:
            if not before or before not in part:
                continue
            candidate = part.replace(before, after)
            if _has_broken(candidate) and not _has_broken(part):
                continue
            part = candidate
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
    pairs += list(EXTRA_REPLACEMENTS)
    pairs += [(word, "") for word in DELETABLE_WORDS]
    # 長い言い回しから先に当てる。「傾向」を「傾向があります」より先に当てると
    # 「ことが多い点があります」になる。並び順に頼らず機械で長い順にそろえる。
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    fixed, done = _replace_in_text(html, pairs)
    # タグをまたいで割れている語は上の置換で拾えない。残った分だけもう一度当てる
    plain = _strip_tags(fixed)
    rest = [(b, a) for b, a in pairs if b and b in plain]
    if rest:
        fixed, done2 = replace_across_tags(fixed, rest)
        for pair in done2:
            if pair not in done:
                done.append(pair)
    fixed = drop_empty_inline_tags(fixed)
    return fixed, done


_EMPTY_INLINE_RE = re.compile(r"<(?:span|strong|em|b)(?:\s[^>]*)?>\s*</(?:span|strong|em|b)>", re.I)


def drop_empty_inline_tags(html: str) -> str:
    """中身が空になった装飾タグを消す。

    タグをまたぐ置換のあと、マーカーの中身だけが置換先へ移って
    <span class="marker"></span> が残る。空のマーカーは公開すると崩れて見える。
    """
    for _ in range(3):
        fixed = _EMPTY_INLINE_RE.sub("", html)
        if fixed == html:
            break
        html = fixed
    return html


# 見本に無いクラス名をモデルが作ることがある。h2-title・h2-ttl など。
# 装飾が効かない状態で公開されるので、生成後に機械で拾う。
def find_invented_classes(html: str, reference_html: str, known_extra=None) -> list:
    """見本にもサイト設定のパーツにも無いクラス名を返す。

    見本だけと比べると、本文で使うQ&Aやチェックリストのパーツが全部
    未登録に見える。仙台の1本目で bold・checkList・faq が出た。
    """
    if not reference_html and not known_extra:
        return []
    known = set(re.findall(r'class="([^"]+)"', reference_html or ""))
    known_words = set(known_extra or [])
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
MAX_LINKS_PER_CLINIC = 5

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


def _tagless_positions(html: str):
    """タグの外側の文字だけを抜き、元のHTMLでの位置を対応づける。"""
    chars, index = [], []
    inside = False
    for i, ch in enumerate(html):
        if ch == "<":
            inside = True
        elif ch == ">":
            inside = False
        elif not inside:
            chars.append(ch)
            index.append(i)
    return "".join(chars), index


def replace_across_tags(html: str, pairs: list) -> tuple:
    """タグをまたいだ語も置き換える。

    <span>で途中が割れていると、タグの外側だけを見る置換では拾えない。
    埼玉で「確認することが大<span>切です」の形になっていた。
    置き換えるときは、その語の最初の文字の位置に置換後の文字を入れ、残りを消す。
    タグそのものは動かさないので、装飾の範囲だけがわずかにずれる。
    """
    done = []
    for before, after in pairs:
        while True:
            plain, index = _tagless_positions(html)
            at = plain.find(before)
            if at < 0:
                break
            starts = index[at:at + len(before)]
            keep = set()
            out = []
            for i, ch in enumerate(html):
                if i == starts[0]:
                    out.append(after)
                    keep.add(i)
                elif i in starts:
                    keep.add(i)
                else:
                    out.append(ch)
            candidate = "".join(out)
            # タグの外側だけを見る置換と同じ検査をかける。ここが素通りしていたため、
            # 1回目で弾いた置換が2回目に当たって「効いてきますです」が通っていた。
            if _has_broken(_strip_tags(candidate)) and not _has_broken(_strip_tags(html)):
                break
            html = candidate
            if (before, after) not in done:
                done.append((before, after))
    return html, done


def pull_todo_marks(html: str) -> tuple:
    """本文に残った要確認の印を外し、中身を返す。

    印が本文に出たまま公開されると事故になる。中身は要確認欄へ移して人が見る。
    Returns: (印を外したHTML, [外した印の中身])
    """
    found = _TODO_MARK_RE.findall(html)
    if not found:
        return html, []
    return _TODO_MARK_RE.sub("", html), found


# ── 見本との粒度照合 ─────────────────────────────────────
# 見本にあるパーツが出力に無いと、院ごとに情報の厚みが違う記事になる。
# 「見本を踏襲する」と指示しても守られないので、出た物を数えて突き合わせる。
_ELEMENT_KINDS = [
    ("table", "料金表・基本情報などのテーブル"),
    ("iframe", "地図の埋め込み"),
    ("ul", "おすすめポイントの箇条書き"),
    ("img", "画像"),
    ("h3", "見出し"),
]


def _count_elements(html: str) -> dict:
    counts = {}
    for tag, _label in _ELEMENT_KINDS:
        counts[tag] = len(re.findall("<" + tag + r"[\s>]", html, re.I))
    return counts


def _class_words(html: str) -> set:
    words = set()
    for value in re.findall(r'class="([^"]+)"', html):
        words.update(value.split())
    return words


def missing_reference_parts(html: str, reference_html: str, allow_missing: list | None = None) -> list:
    """見本にあって出力に無いパーツを返す。

    allow_missing は仕様として置かないもの。4位以降のCTAボタンなど。
    """
    if not reference_html:
        return []
    skip = set(allow_missing or [])
    findings = []
    ref_counts = _count_elements(reference_html)
    got_counts = _count_elements(html)
    for tag, label in _ELEMENT_KINDS:
        if tag in skip or ref_counts.get(tag, 0) == 0:
            continue
        if got_counts.get(tag, 0) >= ref_counts[tag]:
            continue
        findings.append({
            "rule": "見本にあるパーツが足りない",
            "text": label,
            "detail": f"<{tag}> が見本に{ref_counts[tag]}個、出力に{got_counts.get(tag, 0)}個",
        })
    ref_classes = _class_words(reference_html)
    got_classes = _class_words(html)
    for name in sorted(ref_classes - got_classes):
        if name in skip:
            continue
        findings.append({
            "rule": "見本にあるパーツが足りない",
            "text": name,
            "detail": f'見本の class="{name}" が出力に無い',
        })
    return findings


def build_reference_fix_instruction(findings: list, reference_html: str) -> str:
    """足りないパーツを見本から足させる指示。文章は触らせない。"""
    if not findings:
        return ""
    lines = [f"- {f['text']}: {f['detail']}" for f in findings]
    return (
        "このブロックには見本にあるパーツが足りていません。"
        "足りないパーツだけを見本と同じHTMLで足してください。" + chr(10)
        + "既にある文章・数値・クラス名は1文字も変更しないでください。" + chr(10)
        + "情報が無い項目は空欄にせず、その行を出したうえで内容を「−」にしてください。" + chr(10) + chr(10)
        + chr(10).join(lines) + chr(10) + chr(10)
        + "【見本HTML】" + chr(10) + reference_html
    )


# ── 出力の壊れを拾う ─────────────────────────────────────
_MD_BOLD_RE = re.compile(r"[*]{2}([^*\n]{1,80}?)[*]{2}")
_MD_HEAD_RE = re.compile(r"^[ ]{0,3}#{1,6}[ ]+(.+)$", re.M)
_H3_SPLIT_RE = re.compile(r"(?=<h3[\s>])", re.I)
_PAIR_TAGS = ("strong", "em", "span", "p", "div", "table", "ul", "li")
_OPEN_TAG_RE = re.compile(r"<(strong|em|span|p|div|table|ul|li)(?:\s[^>]*)?>", re.I)
_CLOSE_TAG_RE = re.compile(r"</(strong|em|span|p|div|table|ul|li)>", re.I)
_INNER_RE = re.compile(r"<(p|span|td|th|li)[^>]*>(.*?)</\1>", re.S | re.I)

# AIが読者ではなく作業者に向けて書いた文。本文に残ると事故になる。
SELF_TALK_WORDS = [
    "提供された情報", "提供されていません", "情報には含まれていません",
    "DBに記載", "データベースに記載", "別途確認が必要", "入力データ",
    "案件DB", "取得できませんでした", "記載がありません",
]

# 文末がこれで終わっていたら文が途中で切れている。「※金額は」で切れていた実例から。
_DANGLING_ENDINGS = "はがをにでとものへや"


def convert_markdown(html: str) -> tuple:
    """HTMLに混ざったMarkdownの太字をタグに直す。

    アスタリスクがそのまま公開される。指示で禁止しても混ざるので機械で直す。
    Returns: (直したHTML, 直した数)
    """
    count = 0

    def _bold(match):
        nonlocal count
        count += 1
        return "<strong>" + match.group(1) + "</strong>"

    return _MD_BOLD_RE.sub(_bold, html), count


def find_markdown(html: str) -> list:
    """タグに直せなかったMarkdown記法を拾う。"""
    findings = []
    for match in _MD_HEAD_RE.finditer(html):
        findings.append({
            "rule": "Markdownの見出し記法",
            "text": match.group(0).strip()[:60],
            "detail": "HTMLの見出しタグに直す",
        })
    left = _MD_BOLD_RE.search(html)
    if left:
        findings.append({
            "rule": "Markdownの太字記法",
            "text": left.group(0)[:60],
            "detail": "strongタグに直す",
        })
    return findings


def find_unclosed_tags(html: str) -> list:
    """開きタグと閉じタグの数が合わないものを拾う。入れ子崩れがここに出る。"""
    opens: dict = {}
    closes: dict = {}
    for match in _OPEN_TAG_RE.finditer(html):
        name = match.group(1).lower()
        opens[name] = opens.get(name, 0) + 1
    for match in _CLOSE_TAG_RE.finditer(html):
        name = match.group(1).lower()
        closes[name] = closes.get(name, 0) + 1
    findings = []
    for name in _PAIR_TAGS:
        if opens.get(name, 0) == closes.get(name, 0):
            continue
        findings.append({
            "rule": "タグの数が合わない",
            "text": name,
            "detail": "開き{0}個、閉じ{1}個".format(opens.get(name, 0), closes.get(name, 0)),
        })
    return findings


def find_truncated_text(html: str) -> list:
    """文が助詞で終わっている箇所を拾う。"""
    findings = []
    for match in _INNER_RE.finditer(html):
        plain = _strip_tags(match.group(2)).strip()
        if len(plain) < 4:
            continue
        if plain[-1] not in _DANGLING_ENDINGS:
            continue
        findings.append({
            "rule": "文が途中で切れている",
            "text": plain[-40:],
            "detail": "文末が助詞で終わっている。書き足すか行を消す",
        })
    return findings


def find_self_talk(html: str) -> list:
    """読者ではなく作業者に向けて書かれた文を拾う。"""
    text = _strip_tags(html)
    findings = []
    for word in SELF_TALK_WORDS:
        if word not in text:
            continue
        findings.append({
            "rule": "読者向けでない文",
            "text": word,
            "detail": "該当箇所: " + _snippet(text, word, 60),
        })
    return findings


def dedupe_markers(html: str) -> tuple:
    """1つのH3の中に2つ以上あるマーカーを、先頭の1つだけ残す。

    各H3に1か所という指示は守られない。装飾を外すだけなので機械でできる。
    Returns: (直したHTML, 外した数)
    """
    count = 0
    out = []
    for part in _H3_SPLIT_RE.split(html):
        found = list(_MARKER_RE.finditer(part))
        if len(found) <= 1:
            out.append(part)
            continue
        for match in reversed(found[1:]):
            part = part[:match.start()] + match.group(2) + part[match.end():]
            count += 1
        out.append(part)
    return "".join(out), count


def find_keyword_stuffing(html: str, main_kw: str = "", sub_kw: list | None = None) -> list:
    """空白区切りのキーワードがそのまま本文に出ているものを拾う。"""
    text = _strip_tags(html)
    findings = []
    seen = set()
    for kw in [main_kw] + list(sub_kw or []):
        kw = (kw or "").strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        if " " not in kw and "　" not in kw:
            continue
        if kw not in text:
            continue
        findings.append({
            "rule": "キーワードのまま詰め込み",
            "text": kw,
            "detail": "自然な日本語に直す／該当箇所: " + _snippet(text, kw, 60),
        })
    return findings


def find_count_mismatch(html: str, clinic_count: int) -> list:
    """見出しのN選と実際に紹介した院数が合わないものを拾う。"""
    if not clinic_count:
        return []
    findings = []
    seen = set()
    for match in re.finditer(r"([0-9]{1,2})\s*選", _strip_tags(html)):
        promised = int(match.group(1))
        if promised == clinic_count or promised in seen:
            continue
        seen.add(promised)
        findings.append({
            "rule": "見出しの院数と紹介数が合わない",
            "text": match.group(0),
            "detail": "見出しは{0}院、紹介ブロックは{1}院".format(promised, clinic_count),
        })
    return findings


def apply_output_fixes(html: str) -> tuple:
    """機械で確実に直せる出力の壊れをまとめて直す。

    Returns: (直したHTML, {項目: 件数})
    """
    counts = {}
    html, n = convert_markdown(html)
    if n:
        counts["Markdownの太字をタグに直した"] = n
    html, n = dedupe_markers(html)
    if n:
        counts["H3内の余分なマーカーを外した"] = n
    html = drop_empty_inline_tags(html)
    return html, counts


def run_article_checks(html: str, main_kw: str = "", sub_kw: list | None = None,
                       clinic_count: int = 0) -> list:
    """記事1本ぶんの検査。人が見る指摘だけを返す。"""
    return (
        find_genre_ng_words(html)
        + find_markdown(html)
        + find_unclosed_tags(html)
        + find_truncated_text(html)
        + find_self_talk(html)
        + find_keyword_stuffing(html, main_kw, sub_kw)
        + find_count_mismatch(html, clinic_count)
    )


def classes_around_tag(html: str, tag: str) -> list:
    """そのタグを包んでいる囲みのクラス名を、見本から集める。

    サイト設定にパーツが登録されていない要素は、役割から引けない。
    見本の中で実際に何に包まれているかを見るほうが、サイトの命名に依存しない。

    囲みの中にある見出しやラベルのクラス名も一緒に返す。地図の囲みは
    タイトルと本体で別のクラスに分かれていることがある。
    ただし表は独立した意味を持つので中身から外す。表が落ちたことを
    地図が無いせいだと見逃すと、料金表が消えたまま公開される。
    """
    if not html or "<" + tag not in html:
        return []
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return []
    soup = BeautifulSoup(html, "html.parser")
    skip_tags = {"table", "thead", "tbody", "tr", "td", "th"}
    names = set()
    for node in soup.find_all(tag):
        chain = [node] + [p for p in node.parents if hasattr(p, "get")]
        outermost = node
        for element in chain:
            if element.get("class"):
                outermost = element
        for element in chain:
            if element.get("class"):
                names.update(element.get("class"))
        for element in outermost.find_all(True):
            if element.name in skip_tags:
                continue
            if element.find_parent(lambda t: t.name in skip_tags):
                continue
            if element.get("class"):
                names.update(element.get("class"))
    return sorted(names)


# ── ジャンル別の禁止表現 ──────────────────────────────────
# 案件DBの表現ルールタブから流し込む。記事1本ごとに入れ替える。
# コードに書くとジャンルが増えるたびに書き足しが必要になる。
EXTRA_NG_WORDS: list = []
EXTRA_REPLACEMENTS: list = []


def set_genre_rules(ng_words=None, replacements=None) -> None:
    """そのジャンルの禁止語と置換を差し替える。"""
    global EXTRA_NG_WORDS, EXTRA_REPLACEMENTS
    EXTRA_NG_WORDS = [w for w in (ng_words or []) if w]
    EXTRA_REPLACEMENTS = [(b, a) for b, a in (replacements or []) if b]


def clear_genre_rules() -> None:
    set_genre_rules([], [])


def find_genre_ng_words(html: str) -> list:
    """ジャンル別の禁止語を拾う。言い換えを持たないので人が直す。"""
    if not EXTRA_NG_WORDS:
        return []
    text = _strip_tags(html)
    findings = []
    for word in EXTRA_NG_WORDS:
        if word not in text:
            continue
        findings.append({
            "rule": "ジャンルの禁止表現",
            "text": word,
            "detail": "該当箇所: " + _snippet(text, word, 50),
        })
    return findings
