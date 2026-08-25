"""ジャンルごとの禁止表現（案件DBスプレッドシートの「表現ルール」タブ）。

医療広告で問題になる語はジャンルで変わる。医療脱毛の「最短6ヶ月」「高い脱毛効果」は
コードに書くとジャンルが増えるたびに書き足しが必要になる。データで持つ。

タブの列
  ジャンル  空ならすべてのジャンルに当てる
  種別      禁止 か 置換
  語        検出する語
  言い換え  置換のときだけ。空なら削除
  理由      人が読むためのメモ。処理には使わない
"""

import gspread
from google.oauth2.service_account import Credentials

from core import sheet_cache

TAB_NAME = "表現ルール"

GENRE_COL = "ジャンル"
KIND_COL = "種別"
WORD_COL = "語"
REPLACE_COL = "言い換え"
REASON_COL = "理由"

KIND_BAN = "禁止"
KIND_REPLACE = "置換"

HEADERS = [GENRE_COL, KIND_COL, WORD_COL, REPLACE_COL, REASON_COL]

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

last_load_error: str = ""


def _get_spreadsheet(creds_data: dict, sheet_url: str):
    creds = Credentials.from_service_account_info(creds_data, scopes=_SCOPES)
    client = gspread.authorize(creds)
    sheet_url = (sheet_url or "").strip()
    if not sheet_url.startswith("http"):
        return client.open_by_key(sheet_url)
    return client.open_by_url(sheet_url)


def load_rules(creds_data=None, sheet_url=None) -> list:
    """表現ルールの全行を返す。タブが無ければ空。"""
    if not (creds_data and sheet_url):
        return []
    return sheet_cache.get(
        ("expression_rules", sheet_url),
        lambda: _load_uncached(creds_data, sheet_url),
    )


def _load_uncached(creds_data: dict, sheet_url: str) -> list:
    global last_load_error
    last_load_error = ""
    try:
        book = _get_spreadsheet(creds_data, sheet_url)
        titles = {ws.title for ws in book.worksheets()}
        if TAB_NAME not in titles:
            return []
        rows = book.worksheet(TAB_NAME).get_all_values()
    except Exception as e:
        last_load_error = type(e).__name__ + ": " + str(e)
        return []
    if len(rows) < 2:
        return []
    headers = [h.strip() for h in rows[0]]
    out = []
    for row in rows[1:]:
        padded = list(row) + [""] * (len(headers) - len(row))
        record = {h: padded[i].strip() for i, h in enumerate(headers) if h}
        if record.get(WORD_COL):
            out.append(record)
    return out


def for_genre(rows: list, genre: str) -> dict:
    """そのジャンルに当てる禁止語と置換の組を返す。

    ジャンル欄が空の行はすべてのジャンルに当てる。
    Returns: {"ng_words": [...], "replacements": [(前, 後), ...]}
    """
    genre = (genre or "").strip()
    words = []
    pairs = []
    for row in rows or []:
        target = row.get(GENRE_COL, "").strip()
        if target and genre and target != genre:
            continue
        word = row.get(WORD_COL, "").strip()
        if not word:
            continue
        if row.get(KIND_COL, "").strip() == KIND_REPLACE:
            pairs.append((word, row.get(REPLACE_COL, "").strip()))
        else:
            words.append(word)
    return {"ng_words": words, "replacements": pairs}
