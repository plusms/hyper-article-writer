"""記事の型（案件DBスプレッドシートの「型」タブ）。

型は構成のコピー元ではなく、構成を作るときの制約条件。守るべき骨格・入れるべき
ブロック・文体を型が持ち、構成そのものは記事ごとに生成してよい。
制約は数で縛る。数で縛らないとAIが膨らませて型が溶ける。

見本記事から取り出すものは2系統に分ける。
- 記事ごとに変わるもの（見出し本数・文字数）… 数値の制約として渡す。見本の見出し文言は渡さない
- 記事ごとに変えなくていいもの（紹介ブロックの並び・比較表の列構成・選び方H2の作り）
  … 見本のHTMLをそのまま渡して真似させる
"""

import gspread
from google.oauth2.service_account import Credentials

from core import sheet_cache

TAB_NAME = "型"

TYPE_COL = "記事型"
GENRE_COL = "ジャンル"

# 数値・骨格の制約。構成生成と構成の検品に渡す。
CONSTRAINT_COLUMNS = [
    "H2総数の下限", "H2総数の上限", "H3総数の下限", "H3総数の上限",
    "固定ブロックの順序", "必須ブロック", "可変H2の上限", "地域固有H2の対象",
    "H2あたり文字数の下限", "H2あたり文字数の上限",
]

# 見本のHTMLをそのまま渡す列。本文・紹介ブロックの生成に使う。
REFERENCE_COLUMNS = [
    "紹介ブロックの並び", "比較表の列構成", "選び方H2の作り", "紹介ブロックで削るもの",
]

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

last_load_error: str = ""


def _get_spreadsheet(creds_data: dict, sheet_url: str):
    creds = Credentials.from_service_account_info(creds_data, scopes=_SCOPES)
    gc = gspread.authorize(creds)
    sheet_url = sheet_url.strip()
    if not sheet_url.startswith("http"):
        return gc.open_by_key(sheet_url)
    return gc.open_by_url(sheet_url)


def load_types(creds_data=None, sheet_url=None) -> list[dict]:
    """型タブの全行を {列名: 値} のリストで返す。記事ごとに読み直さない。"""
    if not (creds_data and sheet_url):
        return []
    return sheet_cache.get(
        ("article_types", sheet_url),
        lambda: _load_types_uncached(creds_data, sheet_url),
    )


def _load_types_uncached(creds_data=None, sheet_url=None) -> list[dict]:
    global last_load_error
    last_load_error = ""
    if not (creds_data and sheet_url):
        return []
    try:
        spreadsheet = _get_spreadsheet(creds_data, sheet_url)
        ws = spreadsheet.worksheet(TAB_NAME)
    except gspread.WorksheetNotFound:
        return []
    except Exception as e:
        last_load_error = f"{type(e).__name__}: {e}"
        return []

    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    headers = [h.strip() for h in rows[0]]
    result = []
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        padded = list(row) + [""] * (len(headers) - len(row))
        result.append({h: padded[i].strip() for i, h in enumerate(headers) if h})
    return result


def pick_type(rows: list[dict], article_type: str, genre: str = "") -> dict:
    """記事型とジャンルに一致する型を返す。

    ジャンル列が空の行はどのジャンルにも当てる。ジャンルが一致する行を優先する。
    """
    matched = [r for r in rows if r.get(TYPE_COL, "") == article_type]
    if not matched:
        return {}
    if genre:
        exact = [r for r in matched if r.get(GENRE_COL, "") == genre]
        if exact:
            return exact[0]
    fallback = [r for r in matched if not r.get(GENRE_COL, "")]
    if fallback:
        return fallback[0]
    return matched[0]


def build_constraints(record: dict) -> str:
    """構成生成と構成の検品に渡す制約文を作る。数値と骨格だけを渡す。"""
    if not record:
        return ""
    lines = []
    for col in CONSTRAINT_COLUMNS:
        value = record.get(col, "").strip()
        if value:
            lines.append(f"{col}：{value}")
    if not lines:
        return ""
    return "【型の制約（数を守る）】\n" + "\n".join(lines)


def get_reference_html(record: dict, column: str) -> str:
    """見本のHTMLをそのまま返す。記事ごとに変えなくていいブロックに使う。"""
    if not record:
        return ""
    return record.get(column, "").strip()


def build_reference_block(record: dict, columns: list | None = None, limit: int = 20000, log=None) -> str:
    """本文生成に渡す見本ブロック。長いので使う列を絞れるようにする。

    上限に収まらない列は渡さない。黙って落とすと型を登録したのに効いていない状態に
    気づけないので、落とした列を必ず log に出す。
    """
    if not record:
        return ""
    parts = []
    used = 0
    for col in (columns or REFERENCE_COLUMNS):
        value = record.get(col, "").strip()
        if not value:
            if log:
                log(f"　→ 型タブの見本が空です: {col}")
            continue
        if used + len(value) > limit:
            if log:
                log(f"　→ 型タブの見本を渡していません: {col}（{len(value)}字。上限{limit}字に収まらない）")
            continue
        parts.append(f"■ {col}\n{value}")
        used += len(value)
    if log and parts:
        log(f"　→ 型タブの見本を渡しました: {len(parts)}列・{used}字")
    if not parts:
        return ""
    return (
        "【見本（このHTMLの作りをそのまま真似する）】\n"
        "見出しの文言はコピーしない。ブロックの並び・タグの構造・クラス名・表の列構成を真似する。\n"
        + "\n\n".join(parts)
    )
