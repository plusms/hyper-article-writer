"""院データベース（案件DBスプレッドシートの「ジャンル名_院」タブ）。

案件タブと分けるのは、診療時間・院数・アクセスが地域によって変わるため。
地域で変わる事実を地域をまたいで使うタブに置くと「全院が21時まで診療」のような
嘘が出る。タブの粒度は「その事実が何によって変わるか」で決める。

院という概念があるジャンルだけが使う。オンライン診療だけのジャンルでは不要。
"""

import gspread
from google.oauth2.service_account import Credentials

from core import sheet_cache

TAB_NAME = "院"  # ジャンル別タブが無いときに読む旧来の共通タブ
TAB_SUFFIX = "_院"

REGION_COL = "地域名"
CLINIC_COL = "クリニック名"

# 地域で変わりうる料金の列。全国共通なら空のままにして、案件タブの料金を使う。
# 「調べたら共通だった」と「まだ調べていない」を分けるため、地域料金ありなしの列を持つ。
PRICE_COLS = ["地域料金あり", "料金プラン（この地域）", "割引情報（この地域）", "料金の出典（この地域）"]
PRICE_PLAN_COL = "料金プラン（この地域）"
PRICE_FLAG_COL = "地域料金あり"
# 地域料金ありの列がこの値なら、その地域だけの料金として扱う。
REGIONAL_PRICE_YES = {"あり", "有", "はい", "yes", "YES", "○", "◯"}

# 料金の列は末尾に足す。既存タブの列位置をずらさずに列を増やせる。
# 読み込みはヘッダー名で引くので、並び順は記事の中身に影響しない。
# 地図の埋め込みは人が用意する固定入力。iframeのHTMLかGoogleマップの場所IDを入れる。
# AIに作らせると存在しない座標のURLを出すので、列が空なら記事に地図を出さない。
MAP_COL = "地図の埋め込み"

HEADERS = [
    REGION_COL, CLINIC_COL, "院名", "休診日", "診療時間", "所在地",
    "最寄駅・アクセス", "公式サイトURL", "予約リンク", "取得元URL",
    "人の確認済み", "取得成否", MAP_COL,
] + PRICE_COLS


def tab_name(genre: str = "") -> str:
    """そのジャンルの院タブ名。ジャンル未指定なら旧来の共通タブ。"""
    return f"{genre}{TAB_SUFFIX}" if genre else TAB_NAME


def _resolve_tab(spreadsheet, genre: str = "") -> str:
    """実際にあるタブ名を返す。ジャンル別が無ければ共通タブに落とす。"""
    titles = {ws.title for ws in spreadsheet.worksheets()}
    if genre and tab_name(genre) in titles:
        return tab_name(genre)
    if TAB_NAME in titles:
        return TAB_NAME
    return ""

# 取得成否にこれが入っている行は記事に使わない。
# 取得Taskは項目が1つでも欠けたら「一部失敗」と書く。所在地・アクセス・診療時間が
# 欠けたまま紹介ブロックを書かせるとAIが埋めて嘘になるので、ここで弾いて人のキューへ回す。
FAILURE_MARKERS = ["取得失敗", "一部失敗", "読取失敗", "候補なし", "該当なし", "未取得", "材料不足", "要確認"]
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


def _col_letter(idx: int) -> str:
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def ensure_tab(creds_data, sheet_url, genre: str = "") -> bool:
    """そのジャンルの院タブが無ければヘッダー付きで作る。"""
    if not (creds_data and sheet_url):
        return False
    title = tab_name(genre)
    try:
        spreadsheet = _get_spreadsheet(creds_data, sheet_url)
        try:
            spreadsheet.worksheet(title)
            return True
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=len(HEADERS))
            ws.update(f"A1:{_col_letter(len(HEADERS))}1", [HEADERS])
            return True
    except Exception:
        return False


def load_facilities(creds_data=None, sheet_url=None, genre: str = "") -> list[dict]:
    """院タブの全行を {列名: 値} のリストで返す。記事ごとに読み直さない。"""
    if not (creds_data and sheet_url):
        return []
    return sheet_cache.get(
        ("facilities", sheet_url, genre),
        lambda: _load_facilities_uncached(creds_data, sheet_url, genre),
    )


def _load_facilities_uncached(creds_data=None, sheet_url=None, genre: str = "") -> list[dict]:
    global last_load_error
    last_load_error = ""
    if not (creds_data and sheet_url):
        return []
    try:
        spreadsheet = _get_spreadsheet(creds_data, sheet_url)
        title = _resolve_tab(spreadsheet, genre)
        if not title:
            return []
        ws = spreadsheet.worksheet(title)
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


def has_real_facility(rows: list[dict]) -> bool:
    """その地域に実際の院があるか。院なしの行しかなければ False。

    地域記事は来院が前提なので、院がない案件を紹介しても読者は通えない。
    掲載対象から落とす判断にここを使う。
    """
    for row in rows:
        if row.get("院名", "").strip() in NO_FACILITY_VALUES:
            continue
        if row.get("取得成否", "").strip() in NO_FACILITY_VALUES:
            continue
        return True
    return False


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


def regional_price(rows: list[dict]) -> str:
    """その地域だけの料金があれば整形して返す。無ければ空。

    全国共通かどうかは調べないと分からないので、地域料金ありの列で明示された
    行だけを地域料金として扱う。空欄は「共通」ではなく「この地域では上書きしない」。
    """
    for row in rows:
        flag = row.get(PRICE_FLAG_COL, "").strip()
        plan = row.get(PRICE_PLAN_COL, "").strip()
        if not plan or flag not in REGIONAL_PRICE_YES:
            continue
        parts = [f"料金プラン：{plan}"]
        for col in ("割引情報（この地域）", "料金の出典（この地域）"):
            value = row.get(col, "").strip()
            if value:
                parts.append(f"{col.replace('（この地域）', '')}：{value}")
        return " / ".join(parts)
    return ""


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
        embed = row.get(MAP_COL, "").strip()
        if embed:
            # 地図はこの値をそのまま使わせる。URLを組み立てさせると別の場所が出る。
            parts.append(f"地図の埋め込み（このHTMLをそのまま使う）：{embed}")
        lines.append(" / ".join(parts))
    return "\n".join(lines)


def attach_to_clinic_info(clinic_info: dict, facilities: dict) -> dict:
    """案件情報のテキストに、その地域の院情報を足して返す。

    地域限定の料金が入っていれば、全国共通の料金より優先するよう明示して渡す。
    """
    merged = dict(clinic_info)
    for name, rows in facilities.items():
        text = format_facilities(rows)
        if not text:
            continue
        base = merged.get(name, "")
        block = f"{base}\n\n【この地域の院】\n{text}"
        price = regional_price(rows)
        if price:
            block += (
                f"\n\n【この地域だけの料金（上の全国料金より優先して使う）】\n{price}"
            )
        merged[name] = block.strip()
    return merged


def has_map_embed(rows: list) -> bool:
    """その地域の院に地図の埋め込みが1つでも入っているか。

    入っていない院では、見本に地図があっても足りないと数えない。
    数えるとモデルが架空のGoogleマップURLを作る。
    """
    return any((row.get(MAP_COL, "") or "").strip() for row in rows or [])
