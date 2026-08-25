"""見本の紹介ブロックから、組み立ての骨格を読み取る。

モデルにHTMLを書かせると、ジャンルやサイトを変えるたびに崩れ方が変わる。
見本を「真似させる対象」ではなく「値を差し込む骨格」として扱うために、
まず見本を部品の並びに分解する。

判定は構造でやる。見出しの文言で判定するとジャンルごとに書き換えが要る。
- thead を持つ表 → 料金表
- thead が無い2列の表 → 基本情報
- iframe を含む囲み → 地図
- 箇条書きを含む囲み → おすすめポイント
- 段落の連なり → 紹介文
- サイト設定のCTAのクラス名を持つ囲み → CTA
- サイト設定の画像のクラス名を持つ囲み → 画像
- span と a を並べて持つ先頭の囲み → 推し文
"""

import re

from bs4 import BeautifulSoup

# 部品の種類
PICKUP = "pickup"
IMAGE = "image"
POINTS = "points"
PRICE_TABLE = "price_table"
PARAGRAPHS = "paragraphs"
INFO_TABLE = "info_table"
MAP = "map"
CTA = "cta"
CAUTION = "caution"
SUBTITLE = "subtitle"

# モデルが中身を書く部品。ここ以外はコードが組み立てる。
MODEL_OWNED = {PICKUP, PARAGRAPHS}


def _classes(node) -> list:
    value = node.get("class") if hasattr(node, "get") else None
    return list(value or [])


def _has_class(node, names: set) -> bool:
    return bool(set(_classes(node)) & names)


def _table_kind(table) -> str:
    """表を構造で見分ける。文言では見分けない。"""
    if table.find("thead"):
        return PRICE_TABLE
    widths = []
    for row in table.find_all("tr"):
        widths.append(len(row.find_all(["td", "th"], recursive=False)))
    if widths and max(widths) <= 2:
        return INFO_TABLE
    return PRICE_TABLE


def table_headers(table) -> list:
    """料金表の列名。"""
    head = table.find("thead")
    if not head:
        return []
    return [c.get_text(strip=True) for c in head.find_all(["th", "td"])]


def table_row_labels(table) -> list:
    """基本情報の行名。順番も返す。"""
    labels = []
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        label = cells[0].get_text(strip=True)
        if label and label not in labels:
            labels.append(label)
    return labels


def derive(reference_html: str, cta_classes=None, image_classes=None) -> list:
    """見本を部品の並びに分解する。

    Returns: [{"kind": ..., "classes": [...], "html": 元のHTML, ...}]
    """
    if not reference_html:
        return []
    cta_names = set(cta_classes or [])
    image_names = set(image_classes or [])
    soup = BeautifulSoup(reference_html, "html.parser")
    root = soup.body or soup
    spec = []
    paragraph_run = 0

    for node in root.find_all(recursive=False):
        name = getattr(node, "name", "")
        if not name:
            continue

        if name == "p":
            paragraph_run += 1
            if spec and spec[-1]["kind"] == PARAGRAPHS:
                spec[-1]["count"] += 1
            else:
                spec.append({"kind": PARAGRAPHS, "count": 1, "classes": []})
            continue

        if name == "span" and _has_class(node, {"caution"}):
            spec.append({"kind": CAUTION, "classes": _classes(node)})
            continue

        if name == "table":
            kind = _table_kind(node)
            entry = {"kind": kind, "classes": _classes(node), "html": str(node)}
            if kind == PRICE_TABLE:
                entry["headers"] = table_headers(node)
            else:
                entry["labels"] = table_row_labels(node)
            spec.append(entry)
            continue

        if node.find("iframe"):
            spec.append({"kind": MAP, "classes": _classes(node), "html": str(node)})
            continue

        if cta_names and _has_class(node, cta_names):
            spec.append({"kind": CTA, "classes": _classes(node), "html": str(node)})
            continue

        if image_names and _has_class(node, image_names):
            spec.append({"kind": IMAGE, "classes": _classes(node), "html": str(node)})
            continue

        if node.find("ul") or node.find("ol"):
            titles = [t.get_text(strip=True) for t in node.find_all(True, recursive=False)
                      if t.name != "ul" and t.name != "ol"]
            spec.append({
                "kind": POINTS,
                "classes": _classes(node),
                "title_classes": [c for t in node.find_all(True, recursive=False)
                                  for c in _classes(t) if t.name not in ("ul", "ol")],
                "items": len(node.find_all("li")),
                "html": str(node),
            })
            continue

        # 推し文は小見出しより先に見る。どちらも表や箇条書きを持たないので、
        # 小見出しの判定を先に置くと推し文が小見出しになる。
        if not spec and node.find("span") and (node.find("a") or node.find("strong")):
            spec.append({"kind": PICKUP, "classes": _classes(node), "html": str(node)})
            continue

        text_only = node.find_all(["table", "iframe", "ul", "ol"])
        if not text_only and node.get_text(strip=True):
            # 小見出しのような1行の囲み。次の部品の見出しになる
            spec.append({"kind": SUBTITLE, "classes": _classes(node), "html": str(node)})
            continue

        spec.append({"kind": "unknown", "classes": _classes(node), "html": str(node)[:200]})

    return spec


def summarize(spec: list) -> str:
    """並びを1行で書き出す。人が見て確かめるため。"""
    parts = []
    for item in spec:
        label = item["kind"]
        if label == PARAGRAPHS:
            label += "×" + str(item.get("count", 1))
        cls = ".".join(item.get("classes", []))
        parts.append(label + ("(" + cls + ")" if cls else ""))
    return " > ".join(parts)


def model_owned_count(spec: list) -> dict:
    """モデルが書く部品と、コードが組む部品の数を返す。"""
    model = sum(1 for i in spec if i["kind"] in MODEL_OWNED)
    return {"model": model, "code": len(spec) - model, "total": len(spec)}
