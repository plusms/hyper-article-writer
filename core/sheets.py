import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# 入力列マッピング（0始まり）
COL_IN = {
    "site_name":           0,   # A
    "genre":               1,   # B
    "article_type":        2,   # C
    "main_kw":             3,   # D
    "sub_kw":              4,   # E
    "clinics_raw":         5,   # F  例: TCB::tcb.net, 湘南::s-b-c.net
    "competitor_urls_raw": 6,   # G
    "custom_block":        7,   # H（追加指示）
    "recommended":         8,   # I（最訴求プラン）
    "related_kw":          9,   # J
    "status":              10,  # K
}

COL_STATUS    = 10  # K（0始まり）
COL_OUT_START = 11  # L〜O（タイトル・メタ・HTML・要確認）


_HEADER = ["サイト名", "ジャンル *", "記事タイプ", "メインKW *", "サブKW",
           "掲載案件", "競合URL", "追加指示", "最訴求プラン", "関連KW",
           "ステータス", "タイトル", "メタ", "HTML", "要確認", "掲載院一覧"]

ARTICLE_TABS = ["ノウハウ", "地域", "比較", "商標"]


def get_sheet(sheet_url: str, creds_data: dict, tab_name: str = "") -> gspread.Worksheet:
    creds = Credentials.from_service_account_info(creds_data, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sheet_url = sheet_url.strip()
    if sheet_url.startswith("http"):
        ss = gc.open_by_url(sheet_url)
    else:
        ss = gc.open_by_key(sheet_url)
    if not tab_name:
        return ss.sheet1
    try:
        return ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=1000, cols=len(_HEADER))
        ws.update("A1:O1", [_HEADER])
        return ws


def read_input_rows(ws: gspread.Worksheet, default_article_type: str = "") -> list:
    """2行目以降を読み込んでdictのリストで返す（1行目はヘッダー）。"""
    all_values = ws.get_all_values()
    rows = []
    for i, row in enumerate(all_values[1:], start=2):
        padded = row + [""] * (11 - len(row))
        rows.append({
            "row_index":           i,
            "site_name":           padded[0],
            "genre":               padded[1],
            "article_type":        padded[2] or default_article_type,
            "main_kw":             padded[3],
            "sub_kw":              padded[4],
            "clinics_raw":         padded[5],
            "competitor_urls_raw": padded[6],
            "custom_block":        padded[7],
            "recommended":         padded[8],
            "related_kw":          padded[9],
            "status":              padded[10],
        })
    return [r for r in rows if r["main_kw"]]  # 空行を除外


def write_status(ws: gspread.Worksheet, row_index: int, status: str) -> None:
    ws.update_cell(row_index, COL_STATUS + 1, status)  # gspreadは1始まり


def write_output_row(ws: gspread.Worksheet, row_index: int, data: dict) -> None:
    clinics = data.get("clinics", [])
    clinic_list_str = ", ".join(
        f"{c['name']}::{c.get('domain', '')}" for c in clinics if c.get("name")
    )
    ws.update(
        f"L{row_index}:P{row_index}",
        [[
            data.get("title", ""),
            data.get("meta", ""),
            data.get("html", ""),
            data.get("todo_list", ""),
            clinic_list_str,
        ]]
    )


def _serialize_clinic(c: dict) -> str:
    """クリニック1件を name::domain::recommended::appeal 形式にシリアライズ。末尾の空フィールドは省略。"""
    parts = [
        c.get("name", ""),
        c.get("domain", ""),
        c.get("recommended", ""),
        c.get("appeal", ""),
    ]
    while parts and not parts[-1]:
        parts.pop()
    return "::".join(parts)


def write_full_row(ws: gspread.Worksheet, row_index: int, input_data: dict, output_data: dict) -> None:
    """入力情報（A-K）と出力情報（L-P）を一括書き込み。カスタム作成で使用。"""
    clinics = output_data.get("clinics") or input_data.get("clinics", [])
    clinic_str = ", ".join(_serialize_clinic(c) for c in clinics if c.get("name"))
    sub_kw_val = input_data.get("sub_kw", "")
    if isinstance(sub_kw_val, list):
        sub_kw_val = ", ".join(sub_kw_val)
    comp_str = ", ".join(input_data.get("competitor_urls", []))

    # 商標の強み①〜③をH列（追加指示）末尾に追記
    custom_block = input_data.get("custom_block", "")
    tm_strengths = input_data.get("tm_strengths", [])
    if tm_strengths and any(s.get("point") for s in tm_strengths):
        _lines = []
        for _i, _s in enumerate(tm_strengths, 1):
            if _s.get("point"):
                _ln = f"強み{_i}: {_s['point']}"
                if _s.get("basis"):
                    _ln += f"（根拠: {_s['basis']}）"
                _lines.append(_ln)
        if _lines:
            custom_block = "\n\n".join(filter(None, [custom_block, "【比較優位性】\n" + "\n".join(_lines)]))

    ws.update(
        f"A{row_index}:P{row_index}",
        [[
            input_data.get("site_name", ""),
            input_data.get("genre", ""),
            input_data.get("article_type", ""),
            input_data.get("main_kw", ""),
            sub_kw_val,
            clinic_str,
            comp_str,
            custom_block,
            input_data.get("recommended", ""),
            input_data.get("related_kw", ""),
            "手動作成",
            output_data.get("title", ""),
            output_data.get("meta", ""),
            output_data.get("html", ""),
            output_data.get("todo_list", ""),
            clinic_str,
        ]]
    )


def get_settings_sheet(sheet_url: str, creds_data: dict) -> gspread.Worksheet:
    """設定タブを取得。存在しない場合は作成する。"""
    creds = Credentials.from_service_account_info(creds_data, scopes=SCOPES)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_url(sheet_url)
    try:
        return spreadsheet.worksheet("設定")
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="設定", rows=10, cols=2)
        ws.update("A1:B4", [
            ["記事タイプ", "デフォルト追加指示"],
            ["地域", ""],
            ["比較", ""],
            ["商標", ""],
        ])
        return ws


def read_defaults(ws: gspread.Worksheet) -> dict:
    """設定タブから {記事タイプ: デフォルト追加指示} を返す。"""
    rows = ws.get_all_values()
    return {r[0]: r[1] for r in rows[1:] if len(r) >= 2 and r[0]}
