"""院データベース（案件DBスプレッドシートの「院」タブ）。

案件タブと分けるのは、診療時間・院数・アクセスが地域によって変わるため。
地域で変わる事実を地域をまたいで使うタブに置くと「全院が21時まで診療」のような
嘘が出る。タブの粒度は「その事実が何によって変わるか」で決める。

院という概念があるジャンルだけが使う。オンライン診療だけのジャンルでは不要。
"""

import gspread
from google.oauth2.service_account import Credentials

TAB_NAME = "院"

REGION_COL = "地域名"
CLINIC_COL = "クリニック名"

HEADERS = [
    REGION_COL, CLINIC_COL, "院名", "休診日", "診療時間", "所在地",
    "最寄駅・アクセス", "公式サイトURL", "予約リンク", "取得元URL",
    "人の確認済み", "取得成否",
]

# 取得成否にこれが入っている行は記事に使わない。
FAILURE_MARKERS = ["取得失敗", "読取失敗", "候補なし", "該当なし", "未取得", "材料不足", "要確認"]
# 院そのものが無い地域は失敗ではない。記事側で「この地域に院はない」と書ける。
NO_FACILITY_VALUES = {"院なし", "なし"}

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


def ensure_tab(creds_data, sheet_url) -> bool:
    """院タブが無ければヘッダー付きで作る。"""
    if not (creds_data and sheet_url):
        return False
    try:
        spreadsheet = _get_spreadsheet(creds_data, sheet_url)
        try:
            spreadsheet.worksheet(TAB_NAME)
            return True
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=TAB_NAME, rows=2000, cols=len(HEADERS))
            ws.update("A1:L1", [HEADERS])
            return True
    except Exception:
        return False


def load_facilities(creds_data=None, sheet_url=None) -> list[dict]:
    """院タブの全行を {列名: 値} のリストで返す。"""
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


def list_regions(rows: list[dict]) -> list[str]:
    seen = []
    for row in rows:
        region = row.get(REGION_COL, "")
        if region and region not in seen:
            seen.append(region)
    return seen


def pick_region(main_kw: str, regions: list[str]) -> str:
    """メインキーワードに含まれる地域名を返す。複数当たれば長いほうを採る。"""
    hits = [r for r in regions if r and r in main_kw]
    if not hits:
        return ""
    return max(hits, key=len)


def _is_failed(row: dict) -> str:
    value = row.get("取得成否", "").strip()
    if value in NO_FACILITY_VALUES:
        return ""
    for marker in FAILURE_MARKERS:
        if marker in value:
            return marker
    return ""


def select_for_article(rows: list[dict], region: str, clinic_names: list) -> tuple[dict, dict]:
    """記事に使う院を案件ごとにまとめる。

    Returns: ({クリニック名: [行]}, {クリニック名: [弾いた理由]})
    """
    ok: dict = {}
    blocked: dict = {}
    for name in clinic_names:
        matched = [
            r for r in rows
            if r.get(CLINIC_COL, "") == name and (not region or r.get(REGION_COL, "") == region)
        ]
        if not matched:
            blocked[name] = ["院タブに行がない"]
            continue
        usable = []
        reasons = []
        for row in matched:
            marker = _is_failed(row)
            if marker:
                reasons.append(f"{row.get('院名', '（院名なし）')} の取得成否が「{row.get('取得成否', '')}」")
                continue
            usable.append(row)
        if usable:
            ok[name] = usable
        if reasons:
            blocked[name] = reasons
    return ok, blocked


def format_facilities(rows: list[dict]) -> str:
    """1案件ぶんの院情報を記事生成に渡すテキストに整形する。"""
    lines = []
    for row in rows:
        name = row.get("院名", "").strip()
        if name in NO_FACILITY_VALUES:
            lines.append(f"{row.get(REGION_COL, '')}に院なし")
            continue
        parts = [f"院名：{name or '（記載なし）'}"]
        for col in ("所在地", "最寄駅・アクセス", "診療時間", "休診日", "予約リンク"):
            value = row.get(col, "").strip()
            if value:
                parts.append(f"{col}：{value}")
        lines.append(" / ".join(parts))
    return "\n".join(lines)


def attach_to_clinic_info(clinic_info: dict, facilities: dict) -> dict:
    """案件情報のテキストに、その地域の院情報を足して返す。"""
    merged = dict(clinic_info)
    for name, rows in facilities.items():
        text = format_facilities(rows)
        if not text:
            continue
        base = merged.get(name, "")
        merged[name] = f"{base}\n\n【この地域の院】\n{text}".strip()
    return merged
