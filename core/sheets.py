import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# 入力列マッピング（0始まり）— 地域・比較・商標共通
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
COL_OUT_START = 11  # L〜P（タイトル・メタ・HTML・要確認・掲載院）
COL_TITLE     = 11  # L（タイトル、"未処理"チェック用）

# ノウハウ専用 列マッピング（13列: A〜M）
COL_IN_KNOWHOW = {
    "site_name":           0,   # A
    "genre":               1,   # B
    "article_type":        2,   # C
    "main_kw":             3,   # D
    "sub_kw":              4,   # E
    "competitor_urls_raw": 5,   # F
    "custom_block":        6,   # G 追加指示
    "related_kw":          7,   # H
    "status":              8,   # I
}
COL_STATUS_KNOWHOW    = 8   # I（0始まり）
COL_OUT_START_KNOWHOW = 9   # J〜M（タイトル・メタ・HTML・要確認）
COL_TITLE_KNOWHOW     = 9   # J（タイトル、"未処理"チェック用）

_HEADERS: dict[str, list[str]] = {
    "ノウハウ一括": [
        "メインKW*", "サブKW*", "関連KW",
        "追加指示", "スラッグ*", "競合URL",
        "ステータス", "タイトル", "メタ", "HTML", "要確認",
    ],
    "ノウハウ": [
        "サイト名", "ジャンル", "記事タイプ", "メインKW", "サブKW",
        "競合URL", "追加指示（ターゲット・記事のゴールなど）", "関連KW",
        "ステータス", "タイトル", "メタ", "HTML", "要確認",
    ],
    "商標": [
        "サイト名", "ジャンル", "記事タイプ", "メインKW", "サブKW",
        "対象案件", "競合URL（構成参照）", "追加指示", "最訴求プラン", "関連KW",
        "ステータス", "タイトル", "メタ", "HTML", "要確認", "対象案件（出力）",
    ],
    "地域": [
        "サイト名", "ジャンル", "記事タイプ", "メインKW", "サブKW",
        "掲載案件", "競合URL", "追加指示", "最訴求プラン（1院目）", "関連KW",
        "ステータス", "タイトル", "メタ", "HTML", "要確認", "掲載案件一覧",
    ],
    "比較": [
        "サイト名", "ジャンル", "記事タイプ", "メインKW", "サブKW",
        "掲載案件", "競合URL", "追加指示", "最訴求プラン（1院目）", "関連KW",
        "ステータス", "タイトル", "メタ", "HTML", "要確認", "掲載案件一覧",
    ],
}
_HEADER_DEFAULT = [
    "サイト名", "ジャンル", "記事タイプ", "メインKW", "サブKW",
    "掲載案件", "競合URL", "追加指示", "最訴求プラン", "関連KW",
    "ステータス", "タイトル", "メタ", "HTML", "要確認", "掲載案件一覧",
]

ARTICLE_TABS = ["ノウハウ一括", "ノウハウ", "地域", "比較", "商標"]

# ノウハウ一括専用 列マッピング（11列: A〜K）
COL_IN_KNOWHOW_BULK = {
    "main_kw":             0,  # A
    "sub_kw":              1,  # B
    "related_kw":          2,  # C
    "custom_block":        3,  # D
    "slug":                4,  # E
    "competitor_urls_raw": 5,  # F
    "status":              6,  # G
}
COL_STATUS_KNOWHOW_BULK = 6   # G
COL_TITLE_KNOWHOW_BULK  = 7   # H


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
    header = _HEADERS.get(tab_name, _HEADER_DEFAULT)
    _last_col = chr(ord('A') + len(header) - 1)
    try:
        ws = ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=1000, cols=len(header))
    # 旧16列ヘッダーからの移行時に余剰列（N1:P1）を消す
    ws.update("A1:P1", [header + [""] * (16 - len(header))])
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


def _reset_row_height(ws: gspread.Worksheet, row_index: int, pixel_size: int = 21) -> None:
    """書き込み後に行の高さをデフォルトに戻す。"""
    ws.spreadsheet.batch_update({"requests": [{"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "ROWS",
                  "startIndex": row_index - 1, "endIndex": row_index},
        "properties": {"pixelSize": pixel_size},
        "fields": "pixelSize",
    }}]})


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
    _reset_row_height(ws, row_index)


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
    clinic_str = "\n".join(_serialize_clinic(c) for c in clinics if c.get("name"))
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
    _reset_row_height(ws, row_index)


def write_input_only_row(ws: gspread.Worksheet, row_index: int, input_data: dict) -> None:
    """入力データのみをA-K列に書き込む（一時保存用）。出力列（L-P）には触れない。"""
    clinics = input_data.get("clinics", [])
    clinic_str = "\n".join(_serialize_clinic(c) for c in clinics if c.get("name"))
    sub_kw_val = input_data.get("sub_kw", "")
    if isinstance(sub_kw_val, list):
        sub_kw_val = ", ".join(sub_kw_val)
    comp_str = ", ".join(input_data.get("competitor_urls", []))
    ws.update(
        f"A{row_index}:K{row_index}",
        [[
            input_data.get("site_name", ""),
            input_data.get("genre", ""),
            input_data.get("article_type", ""),
            input_data.get("main_kw", ""),
            sub_kw_val,
            clinic_str,
            comp_str,
            input_data.get("custom_block", ""),
            input_data.get("recommended", ""),
            input_data.get("related_kw", ""),
            "入力保存中",
        ]]
    )


def read_row_by_index(ws: gspread.Worksheet, row_index: int) -> dict | None:
    """指定行のデータをread_recent_input_rowsと同じ形式で返す。"""
    all_values = ws.get_all_values()
    if row_index < 2 or row_index > len(all_values):
        return None
    row = all_values[row_index - 1]
    padded = row + [""] * (11 - len(row))
    if not padded[COL_IN["main_kw"]]:
        return None
    return {
        "row_index":           row_index,
        "site_name":           padded[COL_IN["site_name"]],
        "genre":               padded[COL_IN["genre"]],
        "article_type":        padded[COL_IN["article_type"]],
        "main_kw":             padded[COL_IN["main_kw"]],
        "sub_kw":              padded[COL_IN["sub_kw"]],
        "clinics_raw":         padded[COL_IN["clinics_raw"]],
        "competitor_urls_raw": padded[COL_IN["competitor_urls_raw"]],
        "custom_block":        padded[COL_IN["custom_block"]],
        "recommended":         padded[COL_IN["recommended"]],
        "related_kw":          padded[COL_IN["related_kw"]],
        "status":              padded[COL_IN["status"]],
    }


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


def get_worksheet_readonly(sheet_url: str, creds_data: dict, tab_name: str):
    """ヘッダー書き込みなしでタブを取得。存在しない場合はNoneを返す。"""
    creds = Credentials.from_service_account_info(creds_data, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sheet_url = sheet_url.strip()
    if sheet_url.startswith("http"):
        ss = gc.open_by_url(sheet_url)
    else:
        ss = gc.open_by_key(sheet_url)
    try:
        return ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        return None


def read_recent_input_rows(ws: gspread.Worksheet, n: int = 5) -> list[dict]:
    """main_kwが入っている行を新しい順にN件返す。列インデックス読み取りのためヘッダー変更の影響を受けない。"""
    all_values = ws.get_all_values()
    rows = []
    for i, row in enumerate(all_values[1:], start=2):
        padded = row + [""] * (11 - len(row))
        if not padded[COL_IN["main_kw"]]:
            continue
        rows.append({
            "row_index":           i,
            "site_name":           padded[COL_IN["site_name"]],
            "genre":               padded[COL_IN["genre"]],
            "article_type":        padded[COL_IN["article_type"]],
            "main_kw":             padded[COL_IN["main_kw"]],
            "sub_kw":              padded[COL_IN["sub_kw"]],
            "clinics_raw":         padded[COL_IN["clinics_raw"]],
            "competitor_urls_raw": padded[COL_IN["competitor_urls_raw"]],
            "custom_block":        padded[COL_IN["custom_block"]],
            "recommended":         padded[COL_IN["recommended"]],
            "related_kw":          padded[COL_IN["related_kw"]],
            "status":              padded[COL_IN["status"]],
        })
    return list(reversed(rows))[:n]


# ── ノウハウ専用 read/write（13列構成: A〜M）──────────────────────────────

def read_input_rows_knowhow(ws: gspread.Worksheet, default_article_type: str = "ノウハウ") -> list:
    """ノウハウタブ（13列）の入力行を読み込む。"""
    all_values = ws.get_all_values()
    rows = []
    for i, row in enumerate(all_values[1:], start=2):
        padded = row + [""] * (9 - len(row))
        rows.append({
            "row_index":           i,
            "site_name":           padded[COL_IN_KNOWHOW["site_name"]],
            "genre":               padded[COL_IN_KNOWHOW["genre"]],
            "article_type":        padded[COL_IN_KNOWHOW["article_type"]] or default_article_type,
            "main_kw":             padded[COL_IN_KNOWHOW["main_kw"]],
            "sub_kw":              padded[COL_IN_KNOWHOW["sub_kw"]],
            "clinics_raw":         "",
            "competitor_urls_raw": padded[COL_IN_KNOWHOW["competitor_urls_raw"]],
            "custom_block":        padded[COL_IN_KNOWHOW["custom_block"]],
            "recommended":         "",
            "related_kw":          padded[COL_IN_KNOWHOW["related_kw"]],
            "status":              padded[COL_IN_KNOWHOW["status"]],
        })
    return [r for r in rows if r["main_kw"]]


def read_recent_input_rows_knowhow(ws: gspread.Worksheet, n: int = 5) -> list[dict]:
    """ノウハウタブ（13列）の最新N件を返す。"""
    all_values = ws.get_all_values()
    rows = []
    for i, row in enumerate(all_values[1:], start=2):
        padded = row + [""] * (9 - len(row))
        if not padded[COL_IN_KNOWHOW["main_kw"]]:
            continue
        rows.append({
            "row_index":           i,
            "site_name":           padded[COL_IN_KNOWHOW["site_name"]],
            "genre":               padded[COL_IN_KNOWHOW["genre"]],
            "article_type":        padded[COL_IN_KNOWHOW["article_type"]] or "ノウハウ",
            "main_kw":             padded[COL_IN_KNOWHOW["main_kw"]],
            "sub_kw":              padded[COL_IN_KNOWHOW["sub_kw"]],
            "clinics_raw":         "",
            "competitor_urls_raw": padded[COL_IN_KNOWHOW["competitor_urls_raw"]],
            "custom_block":        padded[COL_IN_KNOWHOW["custom_block"]],
            "recommended":         "",
            "related_kw":          padded[COL_IN_KNOWHOW["related_kw"]],
            "status":              padded[COL_IN_KNOWHOW["status"]],
        })
    return list(reversed(rows))[:n]


def write_status_knowhow(ws: gspread.Worksheet, row_index: int, status: str) -> None:
    ws.update_cell(row_index, COL_STATUS_KNOWHOW + 1, status)


def write_output_row_knowhow(ws: gspread.Worksheet, row_index: int, data: dict) -> None:
    """ノウハウタブ出力列（J〜M: タイトル・メタ・HTML・要確認）に書き込む。"""
    ws.update(
        f"J{row_index}:M{row_index}",
        [[
            data.get("title", ""),
            data.get("meta", ""),
            data.get("html", ""),
            data.get("todo_list", ""),
        ]]
    )
    _reset_row_height(ws, row_index)


def read_input_rows_knowhow_bulk(ws: gspread.Worksheet, site_name: str = "", genre: str = "") -> list:
    """ノウハウ一括タブ（10列）の入力行を読み込む。サイト名・ジャンルは呼び出し側から注入。"""
    all_values = ws.get_all_values()
    rows = []
    for i, row in enumerate(all_values[1:], start=2):
        padded = row + [""] * (7 - len(row))
        if not padded[COL_IN_KNOWHOW_BULK["main_kw"]]:
            continue
        rows.append({
            "row_index":           i,
            "site_name":           site_name,
            "genre":               genre,
            "article_type":        "ノウハウ",
            "main_kw":             padded[COL_IN_KNOWHOW_BULK["main_kw"]],
            "sub_kw":              padded[COL_IN_KNOWHOW_BULK["sub_kw"]],
            "related_kw":          padded[COL_IN_KNOWHOW_BULK["related_kw"]],
            "custom_block":        padded[COL_IN_KNOWHOW_BULK["custom_block"]],
            "slug":                padded[COL_IN_KNOWHOW_BULK["slug"]],
            "competitor_urls_raw": padded[COL_IN_KNOWHOW_BULK["competitor_urls_raw"]],
            "clinics_raw":         "",
            "recommended":         "",
            "status":              padded[COL_IN_KNOWHOW_BULK["status"]],
        })
    return rows


def write_status_knowhow_bulk(ws: gspread.Worksheet, row_index: int, status: str) -> None:
    ws.update_cell(row_index, COL_STATUS_KNOWHOW_BULK + 1, status)


def write_output_row_knowhow_bulk(ws: gspread.Worksheet, row_index: int, data: dict) -> None:
    """ノウハウ一括タブ出力列（H〜K: タイトル・メタ・HTML・要確認）に書き込む。"""
    ws.update(
        f"H{row_index}:K{row_index}",
        [[
            data.get("title", ""),
            data.get("meta", ""),
            data.get("html", ""),
            data.get("todo_list", ""),
        ]]
    )
    _reset_row_height(ws, row_index)


def write_full_row_knowhow(ws: gspread.Worksheet, row_index: int, input_data: dict, output_data: dict) -> None:
    """ノウハウタブ全列（A〜M）を一括書き込み。カスタム作成で使用。"""
    sub_kw_val = input_data.get("sub_kw", "")
    if isinstance(sub_kw_val, list):
        sub_kw_val = ", ".join(sub_kw_val)
    comp_str = ", ".join(input_data.get("competitor_urls", []))
    ws.update(
        f"A{row_index}:M{row_index}",
        [[
            input_data.get("site_name", ""),
            input_data.get("genre", ""),
            input_data.get("article_type", ""),
            input_data.get("main_kw", ""),
            sub_kw_val,
            comp_str,
            input_data.get("custom_block", ""),
            input_data.get("related_kw", ""),
            "手動作成",
            output_data.get("title", ""),
            output_data.get("meta", ""),
            output_data.get("html", ""),
            output_data.get("todo_list", ""),
        ]]
    )
    _reset_row_height(ws, row_index)


def read_notation_rules(sheet_url: str, creds_data: dict, site_name: str) -> list:
    """表記ゆれルールシートからサイト名でフィルタして返す。"""
    try:
        ws = get_sheet(sheet_url, creds_data, tab_name="表記ゆれルール")
        rows = ws.get_all_values()
        if len(rows) < 2:
            return []
        rules = []
        for row in rows[1:]:
            row_site = row[0].strip() if len(row) > 0 else ""
            if row_site == site_name:
                rules.append({
                    "ng": row[1].strip() if len(row) > 1 else "",
                    "ok": row[2].strip() if len(row) > 2 else "",
                    "note": row[3].strip() if len(row) > 3 else "",
                })
        return [r for r in rules if r["ng"]]
    except Exception:
        return []


# ── サイト情報シート（新スプシ）────────────────────────────────────────────
# 構造: A=カテゴリ, B=項目, C=値
# カテゴリ種別: 基本情報 / 掲載条件 / 表記ゆれ

_SITE_INFO_HEADER = ["カテゴリ", "項目", "値"]
_SITE_INFO_BASIC_ROWS = [
    ["基本情報", "アフィリURL",      ""],
    ["基本情報", "アフィリ掲載位置", ""],
    ["基本情報", "アフィリ形式",     ""],
    ["基本情報", "画像ベースURL",    ""],
    ["基本情報", "画像拡張子",       ""],
]
_SITE_INFO_PLACEHOLDER_ROWS = [
    ["掲載条件", "NG事項",           ""],
    ["表記ゆれ", "誤表記・ゆれ表記", "正式表記"],
]
# B列のキー → (設定dict名, フィールド名)
_SITE_INFO_WRITE_MAP = {
    "アフィリURL":      ("link_settings",  "affili_base_url"),
    "アフィリ掲載位置": ("link_settings",  "affili_param_positions"),
    "アフィリ形式":     ("link_settings",  "affili_param_formats"),
    "画像ベースURL":    ("image_settings", "base_url"),
    "画像拡張子":       ("image_settings", "ext"),
}


def _get_client(creds_data: dict):
    creds = Credentials.from_service_account_info(creds_data, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_or_create_worksheet(spreadsheet, site_name: str):
    """サイト名のタブを取得または作成する。"""
    try:
        return spreadsheet.worksheet(site_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=site_name, rows=200, cols=10)
        _init_site_tab(ws)
        return ws


def _init_site_tab(ws) -> None:
    """タブにヘッダー + 基本情報行 + プレースホルダー行を書き込む。"""
    rows = [_SITE_INFO_HEADER] + _SITE_INFO_BASIC_ROWS + _SITE_INFO_PLACEHOLDER_ROWS
    ws.update("A1", rows)


def create_site_tab(sheet_url: str, creds_data: dict, site_name: str) -> bool:
    """サイト情報シートに新規タブを作成する。既存タブは上書きしない。"""
    try:
        client = _get_client(creds_data)
        ss = client.open_by_url(sheet_url)
        existing = [ws.title for ws in ss.worksheets()]
        if site_name in existing:
            return True
        ws = ss.add_worksheet(title=site_name, rows=200, cols=10)
        _init_site_tab(ws)
        return True
    except Exception as e:
        print(f"create_site_tab error ({site_name}): {e}")
        return False


def init_site_info_sheet(sheet_url: str, creds_data: dict, site_names: list) -> dict:
    """全サイト分のタブを一括作成する。{site_name: ok/skip/error} を返す。"""
    results = {}
    try:
        client = _get_client(creds_data)
        ss = client.open_by_url(sheet_url)
        existing = {ws.title for ws in ss.worksheets()}
        for name in site_names:
            if name in existing:
                results[name] = "skip"
                continue
            try:
                ws = ss.add_worksheet(title=name, rows=200, cols=10)
                _init_site_tab(ws)
                results[name] = "ok"
            except Exception as e:
                results[name] = f"error: {e}"
    except Exception as e:
        return {n: f"error: {e}" for n in site_names}
    return results


def write_site_info_settings(
    sheet_url: str,
    creds_data: dict,
    site_name: str,
    image_settings: dict,
    link_settings: dict,
) -> bool:
    """画像リンク設定・アフィリリンク設定をサイトタブのC列に書き込む（ツール→シート反映）。
    B列のキーを検索して対応するC列を更新する。
    """
    values = {
        "アフィリURL":      link_settings.get("affili_base_url", ""),
        "アフィリ掲載位置": link_settings.get("affili_param_positions", ""),
        "アフィリ形式":     link_settings.get("affili_param_formats", ""),
        "画像ベースURL":    image_settings.get("base_url", ""),
        "画像拡張子":       image_settings.get("ext", ""),
    }
    try:
        client = _get_client(creds_data)
        ss = client.open_by_url(sheet_url)
        ws = _get_or_create_worksheet(ss, site_name)
        all_rows = ws.get_all_values()
        batch = []
        for i, row in enumerate(all_rows, start=1):
            item = row[1].strip() if len(row) > 1 else ""
            if item in values:
                batch.append({"range": f"C{i}", "values": [[str(values[item])]]})
        if batch:
            ws.batch_update(batch)
        return True
    except Exception as e:
        print(f"write_site_info_settings error ({site_name}): {e}")
        return False


def read_site_info(sheet_url: str, creds_data: dict, site_name: str) -> dict:
    """サイトタブから掲載条件・表記ゆれを読み取る。
    A列カテゴリでフィルタ。
    Returns: {notes: str, notation_rules: list[{ng, ok, note}]}
    """
    try:
        client = _get_client(creds_data)
        ss = client.open_by_url(sheet_url)
        try:
            ws = ss.worksheet(site_name)
        except gspread.exceptions.WorksheetNotFound:
            return {"notes": "", "notation_rules": []}

        all_rows = ws.get_all_values()
        notes_parts = []
        notation_rules = []
        for row in all_rows[1:]:  # 1行目はヘッダー
            cat  = row[0].strip() if len(row) > 0 else ""
            item = row[1].strip() if len(row) > 1 else ""
            val  = row[2].strip() if len(row) > 2 else ""
            if cat == "掲載条件" and item and val:
                notes_parts.append(f"■ {item}：{val}")
            elif cat == "表記ゆれ" and item and "入力" not in item and item != "誤表記・ゆれ表記":
                notation_rules.append({"ng": item, "ok": val, "note": ""})

        return {
            "notes": "\n".join(notes_parts),
            "notation_rules": notation_rules,
        }
    except Exception as e:
        print(f"read_site_info error ({site_name}): {e}")
        return {"notes": "", "notation_rules": []}
