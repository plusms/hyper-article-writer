"""紹介ブロックのHTMLをコードで組み立てる。

モデルにHTMLを書かせると、ジャンルやサイトを変えるたびに崩れ方が変わる。
クラス名の写し間違い・タグの入れ子崩れ・表の欠落・ラベルの取り違えは全部これが原因。
骨格は block_spec が見本から読み取り、ここが値を差し込む。

モデルが書くのは推し文と紹介文の段落だけ。タグは1つも書かせない。

基本情報の行名とデータベースの列名は自動で対応を取る。ジャンルごとに手書きの
マッピングを持つと、ジャンルが増えるたびに書き足しが必要になる。
"""

import difflib
import html as html_lib
import re

from core import block_spec

# 値が無いときにセルへ入れる文字。空欄にすると行が消えたように見える。
EMPTY_CELL = "−"


def _esc(text: str) -> str:
    return html_lib.escape(str(text or ""), quote=False)


def _lines(value: str) -> list:
    """改行区切りの値を行のリストにする。"""
    return [x.strip() for x in str(value or "").replace("\r", "").split("\n") if x.strip()]


def _br(value: str) -> str:
    """改行をbrにする。中身はエスケープする。"""
    return "<br>".join(_esc(x) for x in _lines(value))


def match_column(label: str, columns: list) -> str:
    """行名に対応する列名を返す。見つからなければ空。

    完全一致 → 部分一致 → 近似の順。ジャンルごとの手書きマッピングを持たない。
    """
    label = (label or "").strip()
    if not label:
        return ""
    columns = [c for c in columns if c]
    if label in columns:
        return label
    for column in columns:
        if label in column or column in label:
            return column
    close = difflib.get_close_matches(label, columns, n=1, cutoff=0.5)
    return close[0] if close else ""


def build_label_map(labels: list, clinic_columns: list, facility_columns: list) -> dict:
    """基本情報の行名を、どのタブのどの列から取るかに割り当てる。

    院タブと案件タブの両方の候補を返す。院タブを先に使い、値が空なら案件タブに
    落とす。院タブには地域限定の上書き列があり、普段は空である。片方に決めて
    しまうと、上書き列を掴んだ行が空になる。実例＝割引情報が
    「割引情報（この地域）」に部分一致して常に空になった。
    """
    mapping = {}
    for label in labels:
        mapping[label] = {
            "facility": match_column(label, facility_columns),
            "clinic": match_column(label, clinic_columns),
        }
    return mapping


def parse_plans(value: str) -> list:
    """紹介ブロックに出すプランを行ごとに分解する。

    形式は プラン名｜内容と回数｜総額｜月額。区切りが足りない行は落とさず、
    埋まっている分だけ使う。落とすと料金表から行が消える。
    """
    plans = []
    for line in _lines(value):
        cells = [c.strip() for c in re.split(r"[｜|]", line)]
        while len(cells) < 4:
            cells.append("")
        plans.append({
            "name": cells[0],
            "detail": cells[1],
            "total": cells[2],
            "monthly": cells[3],
        })
    return plans


def build_price_table(plans: list, headers: list, table_classes: list) -> str:
    """料金表を組む。同じプラン名が続く行はまとめる。

    列数は見本の列名の数に合わせる。見本が3列なら月額の列は作らない。
    列数を勝手に増やすと見本と食い違う。
    """
    if not plans or not headers:
        return ""
    width = len(headers)
    cls = " ".join(table_classes) if table_classes else "table"
    rows = []
    index = 0
    while index < len(plans):
        name = plans[index]["name"]
        group = [p for p in plans[index:] if p["name"] == name]
        span = len(group)
        for offset, plan in enumerate(group):
            cells = []
            if offset == 0:
                attr = ' rowspan="' + str(span) + '"' if span > 1 else ""
                cells.append("<th" + attr + ">" + _br(name) + "</th>")
            cells.append("<td>" + _br(plan["detail"] or EMPTY_CELL) + "</td>")
            if width >= 3:
                total = plan["total"] or EMPTY_CELL
                if width >= 4:
                    cells.append("<td>" + _br(total) + "</td>")
                    cells.append("<td>" + _br(plan["monthly"] or EMPTY_CELL) + "</td>")
                else:
                    extra = total
                    if plan["monthly"]:
                        extra += "<br><span class=\"caution\">" + _esc(plan["monthly"]) + "</span>"
                    cells.append("<td>" + extra + "</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
        index += span
    head = "".join("<th" + (' style="width:40%;"' if i == 0 else "") + ">" + _esc(h) + "</th>"
                   for i, h in enumerate(headers))
    return ('<table class="' + cls + '"><thead><tr>' + head + "</tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def build_info_table(labels: list, mapping: dict, clinic_row: dict, facility_row: dict,
                     table_classes: list) -> str:
    """基本情報の表を組む。行を省略しない。

    値が取れない行も出す。行が消えると院ごとに表の形が変わる。
    """
    if not labels:
        return ""
    cls = " ".join(table_classes) if table_classes else "table"
    rows = []
    for i, label in enumerate(labels):
        candidates = mapping.get(label) or {}
        value = ""
        for source, row in (("facility", facility_row), ("clinic", clinic_row)):
            column = candidates.get(source, "")
            if not column:
                continue
            found = (row or {}).get(column, "")
            if str(found).strip():
                value = found
                break
        # 表のセルにも社内向けのメモが混ざる。実例＝麻酔の行に
        # 「※金額はDBに記載なし」が入り、そのまま記事に出た。
        value = "\n".join(reader_facing_lines(value)) if str(value).strip() else value
        attr = ' style="width:40%;"' if i == 0 else ""
        rows.append("<tr><th" + attr + ">" + _esc(label) + "</th><td>"
                    + (_br(value) if str(value).strip() else EMPTY_CELL) + "</td></tr>")
    return '<table class="' + cls + '">' + "".join(rows) + "</table>"


def build_points(title: str, items: list, wrapper_classes: list,
                 title_classes: list, limit: int = 0) -> str:
    """おすすめポイントの囲みを組む。"""
    items = [x for x in items if x]
    if limit:
        items = items[:limit]
    if not items:
        return ""
    wrapper = " ".join(wrapper_classes) if wrapper_classes else "normalBox"
    inner = " ".join(title_classes) if title_classes else "box-ttl"
    lis = "".join("<li>" + _esc(x) + "</li>" for x in items)
    return ('<div class="' + wrapper + '"><div class="' + inner + '">' + _esc(title)
            + "</div><ul>" + lis + "</ul></div>")


def build_pickup(comment: str, name: str, link: str, classes: list) -> str:
    """推し文。クリニック名はリンクがあればリンク、無ければstrong。

    コメントとクリニック名を同じspanに入れない。spanの外に出す。
    """
    cls = " ".join(classes) if classes else "pickup"
    if link:
        label = ('<a href="' + _esc(link) + '" rel="nofollow noopener noreferrer" '
                 'target="_blank">' + _esc(name) + "</a>")
    else:
        label = "<strong>" + _esc(name) + "</strong>"
    return ('<div class="' + cls + '"><span>' + _esc(comment) + "</span>" + label + "</div>")


def build_subtitle(text: str, classes: list) -> str:
    cls = " ".join(classes) if classes else "subTitle"
    return '<div class="' + cls + '">' + _esc(text) + "</div>"


# 切った残りがこれで終わっていたら文になっていない。
DANGLING_ENDINGS = "はがをにでとものへや"

# 社内向けのメモがレギュレーション列に混ざっている。本文に出ると事故になる。
INTERNAL_MARKERS = [
    "DBに記載なし", "データベースに記載", "記載なし", "ASPの禁止表現",
    "LP注記", "要確認", "取得できません", "提供された情報", "別途確認",
]


def reader_facing_lines(value: str) -> list:
    """読者に見せてよい行だけを返す。

    レギュレーション列には社内向けのメモが混ざる。実例＝「ASPの禁止表現はDBに
    記載なし」「【LP注記】」が注記としてそのまま本文に出た。
    """
    kept = []
    for line in _lines(value):
        for word in INTERNAL_MARKERS:
            if word not in line:
                continue
            # 印の直前の区切りから後ろを落とす。行ごと消すと本文まで消える。
            # 「有料で処方※金額はDBに記載なし」は前半を残す。
            cut = line.find(word)
            for mark in ("※", "（", "(", "。", "、"):
                pos = line.rfind(mark, 0, cut)
                if pos > 0:
                    cut = pos
                    break
            line = line[:cut].rstrip("・-※（( 、。")
            # 切った残りが助詞で終わっていたら文になっていない。行ごと落とす。
            # 「ASPの禁止表現はDBに記載なし」を切ると「ASPの禁止表現は」が残る。
            if line and line[-1] in DANGLING_ENDINGS:
                line = ""
            break
        if not line.lstrip("・-※【 ").strip():
            continue
        kept.append(line)
    return kept


def build_caution(text: str) -> str:
    lines = reader_facing_lines(text)
    if not lines:
        return ""
    return '<span class="caution">' + "<br>".join(_esc(x) for x in lines) + "</span>"


def build_paragraphs(texts: list) -> str:
    return "".join("<p>" + _esc(t) + "</p>" for t in texts if str(t or "").strip())


def build_image(url: str, alt: str, link: str, classes: list) -> str:
    if not url:
        return ""
    cls = " ".join(classes) if classes else "full-img"
    img = ('<img decoding="async" src="' + _esc(url) + '" alt="' + _esc(alt) + '">')
    if link:
        img = ('<a href="' + _esc(link) + '" rel="nofollow noopener noreferrer" '
               'target="_blank">' + img + "</a>")
    return '<div class="' + cls + '">' + img + "</div>"


def build_map(embeds: list, classes: list) -> str:
    """地図。院タブの埋め込みをそのまま置く。URLを組み立てない。

    埋め込みが1つも無ければ何も返さない。空の枠を置くと崩れて見える。
    """
    embeds = [x for x in embeds if str(x or "").strip()]
    if not embeds:
        return ""
    cls = " ".join(classes) if classes else "map"
    return "".join('<div class="' + cls + '">' + e + "</div>" for e in embeds)


def build_cta(pattern: str, link: str, name: str, label: str = "") -> str:
    """CTAはサイト設定のパーツをそのまま使う。汎用のaタグで代替しない。

    パーツの {{link}} には、URLではなくaタグ全体が入る。URLだけを入れると
    リンクにならない。ノックスのパーツがこの形だった。
    """
    if not link:
        return ""
    anchor = ('<a href="' + _esc(link) + '" rel="nofollow noopener noreferrer" '
              'target="_blank">' + _esc(label or name) + "</a>")
    if pattern and "{{" in pattern:
        filled = pattern
        for token in re.findall(r"\{\{[^}]+\}\}", pattern):
            key = token.strip("{}").strip().lower()
            if "link" in key or "url" in key or "リンク" in token:
                filled = filled.replace(token, anchor)
            else:
                filled = filled.replace(token, _esc(name))
        return filled
    if pattern:
        return pattern.replace("<a ", '<a href="' + _esc(link) + '" ', 1)
    return '<div class="c-btn">' + anchor + "</div>"


def subtitle_suffix(spec_html: str, reference_clinic: str) -> str:
    """見本の小見出しから、クリニック名を除いた言い回しを取り出す。

    「フレイアクリニックの料金」から「の料金」を取る。ジャンルが変わっても
    見本の言い回しをそのまま使えるので、文言をコードに持たない。
    """
    text = re.sub(r"<[^>]+>", "", spec_html or "").strip()
    if reference_clinic and reference_clinic in text:
        return text.replace(reference_clinic, "").strip()
    return text


def reference_clinic_name(spec: list) -> str:
    """見本がどのクリニックのものかを推し文から取る。"""
    for item in spec:
        if item.get("kind") != block_spec.PICKUP:
            continue
        html = item.get("html", "")
        match = re.search(r"</span>\s*<(?:a|strong)[^>]*>(.*?)</(?:a|strong)>", html, re.S)
        if match:
            return re.sub(r"<[^>]+>", "", match.group(1)).strip()
    return ""


def cta_pattern(components: list) -> str:
    """サイト設定のCTAパーツのHTMLを返す。"""
    for component in components or []:
        if not component.get("active", True):
            continue
        if "CTA" in str(component.get("name", "")) or "ボタン" in str(component.get("name", "")):
            pattern = str(component.get("pattern", "")).strip()
            if pattern:
                return pattern
    return ""


def image_url(spec: list, slug: str, alias: str) -> str:
    """画像のURLを見本の形から組む。

    見本のsrcからファイル名の作りを読み取る。ファイル名の規則をコードに
    持つとサイトごとに書き換えが要る。
    """
    for item in spec:
        if item.get("kind") != block_spec.IMAGE:
            continue
        match = re.search(r'src="([^"]+)"', item.get("html", ""))
        if not match:
            continue
        url = match.group(1)
        base, _, filename = url.rpartition("/")
        stem, dot, ext = filename.partition(".")
        if not (slug and alias):
            return ""
        return base + "/" + slug + "-" + alias + (dot + ext if dot else "")
    return ""


def assemble(spec: list, data: dict, texts: dict) -> str:
    """骨格に値を差し込んでブロックのHTMLを組む。

    data のキー
      name            クリニック名
      is_top          上位院か。送客リンクとCTAを置くか
      link            送客リンクの本体
      link_params     {"txt": ..., "bn": ..., "bt": ...} 用途ごとの完成URL
      clinic_row      案件タブの1行
      facility_rows   その地域の院タブの行
      clinic_columns  案件タブの列名
      facility_columns 院タブの列名
      slug            記事スラッグ
      alias           画像のクリニック略称
      components      サイト設定のパーツ
    texts のキー
      pickup      推し文の1文
      paragraphs  紹介文の段落。spec の paragraphs の数だけ順に使う
    """
    name = data.get("name", "")
    is_top = bool(data.get("is_top"))
    params = data.get("link_params") or {}
    facility_rows = data.get("facility_rows") or []
    facility_row = facility_rows[0] if facility_rows else {}
    clinic_row = data.get("clinic_row") or {}
    reference_clinic = reference_clinic_name(spec)
    paragraphs = list(texts.get("paragraphs") or [])
    out = []

    for item in spec:
        kind = item.get("kind")
        classes = item.get("classes") or []

        if kind == block_spec.PICKUP:
            out.append(build_pickup(
                texts.get("pickup", ""), name,
                params.get("txt", "") if is_top else "", classes))

        elif kind == block_spec.IMAGE:
            if not is_top:
                continue
            url = image_url(spec, data.get("slug", ""), data.get("alias", ""))
            out.append(build_image(url, name, params.get("bn", ""), classes))

        elif kind == block_spec.POINTS:
            title = name + subtitle_suffix_for_points(item, reference_clinic)
            items = _lines(clinic_row.get("おすすめポイント", ""))
            out.append(build_points(title, items, classes,
                                    item.get("title_classes") or [], item.get("items", 0)))

        elif kind == block_spec.SUBTITLE:
            out.append(build_subtitle(name + subtitle_suffix(item.get("html", ""),
                                                             reference_clinic), classes))

        elif kind == block_spec.PRICE_TABLE:
            plans = parse_plans(clinic_row.get("紹介ブロックに出すプラン", ""))
            out.append(build_price_table(plans, item.get("headers") or [], classes))

        elif kind == block_spec.CAUTION:
            out.append(build_caution(clinic_row.get("レギュレーション・禁止表現", "")))

        elif kind == block_spec.PARAGRAPHS:
            count = item.get("count", 1)
            out.append(build_paragraphs(paragraphs[:count]))
            paragraphs = paragraphs[count:]

        elif kind == block_spec.INFO_TABLE:
            labels = item.get("labels") or []
            mapping = build_label_map(labels, data.get("clinic_columns") or [],
                                      data.get("facility_columns") or [])
            out.append(build_info_table(labels, mapping, clinic_row, facility_row, classes))

        elif kind == block_spec.MAP:
            embeds = [r.get("地図の埋め込み", "") for r in facility_rows]
            out.append(build_map(embeds, classes))

        elif kind == block_spec.CTA:
            if not is_top:
                continue
            out.append(build_cta(cta_pattern(data.get("components") or []),
                                 params.get("bt", ""), name,
                                 label=name + data.get("cta_label", "")))

    return "\n".join(drop_orphan_subtitles(spec, out))


def drop_orphan_subtitles(spec: list, parts: list) -> list:
    """中身が空になった部品の直前の見出しを落とす。

    地図の埋め込みが無い院で「所在地とアクセス」の見出しだけが残った。
    見出しだけが浮くと、情報が抜けているのではなく崩れているように見える。
    """
    out = []
    for index, value in enumerate(parts):
        if index < len(spec) and spec[index].get("kind") == block_spec.SUBTITLE:
            if not any(parts[index + 1:index + 2]):
                continue
        if value:
            out.append(value)
    return out


def subtitle_suffix_for_points(item: dict, reference_clinic: str) -> str:
    """おすすめポイントの囲みの見出しから言い回しを取る。"""
    match = re.search(r"<div[^>]*>(.*?)</div>", item.get("html", ""), re.S)
    if not match:
        return "のおすすめポイント"
    return subtitle_suffix(match.group(1), reference_clinic) or "のおすすめポイント"


def missing_data(spec: list, data: dict) -> list:
    """組む前に、値が無くて空になる部品を挙げる。

    空のまま組んで公開するより先に止める。生成後に気づくと作り直しになる。
    """
    clinic_row = data.get("clinic_row") or {}
    facility_rows = data.get("facility_rows") or []
    problems = []
    for item in spec:
        kind = item.get("kind")
        if kind == block_spec.PRICE_TABLE and not parse_plans(
                clinic_row.get("紹介ブロックに出すプラン", "")):
            problems.append("料金表の材料がない（紹介ブロックに出すプラン）")
        if kind == block_spec.POINTS and not _lines(clinic_row.get("おすすめポイント", "")):
            problems.append("おすすめポイントの材料がない")
        if kind == block_spec.MAP and not any(
                str(r.get("地図の埋め込み", "") or "").strip() for r in facility_rows):
            problems.append("地図の埋め込みが院タブに無い")
        if kind == block_spec.IMAGE and data.get("is_top") and not data.get("alias"):
            problems.append("画像のクリニック略称が案件タブに無い")
    return problems
