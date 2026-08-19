"""案件データベース。

事実は記事ごとに取らない。案件ごとに1回取ってこのDBに置き、記事生成は
DBから読んだ値しか使わない。ツールはDBを読むだけで、公式サイトの取得はしない
（requests + BeautifulSoup ではタブ切り替えの料金表・画像化された料金表が
原理的に取れないため）。DBに無い案件は記事に載せず人のキューへ回す。

タブは2形式が混在する。どちらもヘッダー行を読んで解釈し、列の位置は固定しない。
- 列形式（新）… 1列1項目。捏造は列の不足で起きるので、書く場所を列で用意する
- info形式（旧）… info の1セルに自由文。移行前のジャンルはこちら
"""

import json
import os
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

DB_PATH = "config/clinic_db.json"
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── 列定義 ───────────────────────────────────────────────
# 共通列＝どのジャンルでも記事に出る。運用列＝値ではなく状態。生成前の照合に使う。
# ジャンル固有列は業態が決めるのでここには置かない。タブに足せばヘッダー駆動で拾う。
NAME_COL = "クリニック名"
DOMAIN_COL = "公式サイトURL"

COMMON_COLUMNS = [
    NAME_COL, "推し順位", DOMAIN_COL, "送客リンク",
    "比較優位性1", "比較優位性2", "比較優位性3", "比較優位性4", "比較優位性5",
    "料金プラン", "支払い方法", "予約方法", "おすすめポイント", "割引情報",
    "比較表に出すプラン", "紹介ブロックに出すプラン", "画像のクリニック略称",
    "レギュレーション・禁止表現", "料金の出典", "LPスクショのDrive URL",
]
OPERATION_COLUMNS = [
    "料金確認済み", "優位性の人承認", "料金の検算結果", "転記元DBの更新日",
]
_HEADERS = COMMON_COLUMNS + OPERATION_COLUMNS

# 旧形式のヘッダー。既存ジャンルのタブはこの並びで入っている。
LEGACY_HEADERS = ["name", "domain", "info", "lp_info", "affili_filename", "updated_at"]

# 生成前の照合で「入っていること」を求める列。
# 送客リンク・画像の略称はサイト側で決まるのでここには入れない。
REQUIRED_FOR_GENERATION = [
    DOMAIN_COL, "料金プラン", "おすすめポイント",
    "比較優位性1", "比較優位性2", "比較優位性3",
]
# 空でないことを成功にしない。状態を表す運用列のうち、これが通らない行は生成に使わない。
# 「未」「要確認。〜」のように、埋まっていても通過していない値が入るため、
# 合格を表す語で始まるかどうかで判定する。
REQUIRED_OPERATION = ["料金確認済み", "優位性の人承認", "料金の検算結果"]
OPERATION_PASS_PREFIXES = ("済", "OK", "ok", "完了", "承認", "正常", "確認済", "合格", "問題なし")
# 値としてこれが入っていたら失敗として弾く。
# 「不可」「なし」のような本文で普通に使う語は入れない。誤って弾くほうが害が大きい。
FAILURE_MARKERS = [
    "取得失敗", "読取失敗", "候補なし", "該当なし", "未取得", "材料不足",
    "要確認", "情報取得失敗",
]

_SYSTEM_TABS = {"clinic_db", "院", "シート1"}  # 案件タブとして列挙しない
_ARCHIVE_SUFFIX = "_旧info"  # 列形式へ移す前の退避タブ。ジャンルとして扱わない


def _is_genre_tab(title: str) -> bool:
    return title not in _SYSTEM_TABS and not title.endswith(_ARCHIVE_SUFFIX)


def _get_spreadsheet(creds_data: dict, sheet_url: str):
    creds = Credentials.from_service_account_info(creds_data, scopes=_SCOPES)
    gc = gspread.authorize(creds)
    sheet_url = sheet_url.strip()
    if not sheet_url.startswith("http"):
        return gc.open_by_key(sheet_url)
    return gc.open_by_url(sheet_url)


def _get_or_create_tab(spreadsheet, genre: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(genre)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=genre, rows=1000, cols=max(len(_HEADERS), 30))
        ws.update(f"A1:{_col_letter(len(_HEADERS))}1", [_HEADERS])
        return ws


def _col_letter(idx: int) -> str:
    """1始まりの列番号をA1記法の列名に変換する。"""
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def is_column_format(headers: list) -> bool:
    """列形式のタブか。クリニック名の列があれば列形式とみなす。"""
    return NAME_COL in headers


def _parse_worksheet(ws: gspread.Worksheet) -> dict:
    """1タブを {案件名: {列名: 値}} に変換する。

    列形式・info形式のどちらでも、記事生成側が使う name / domain / info /
    lp_info / updated_at のキーは必ず埋めて返す。
    """
    rows = ws.get_all_values()
    if not rows:
        return {}
    headers = [h.strip() for h in rows[0]]
    if not headers or not any(headers):
        headers = LEGACY_HEADERS
    column_format = is_column_format(headers)
    # 旧タブはヘッダー行が4列しかないのに実データは6列入っている。並びは固定なので補う。
    if not column_format and headers[0] == "name" and len(headers) < len(LEGACY_HEADERS):
        headers = LEGACY_HEADERS
    name_key = NAME_COL if column_format else headers[0]

    result: dict = {}
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        padded = list(row) + [""] * (len(headers) - len(row))
        record = {h: padded[i].strip() for i, h in enumerate(headers) if h}
        name = record.get(name_key, "").strip()
        if not name:
            continue
        record["name"] = name
        record["_column_format"] = column_format
        if column_format:
            record.setdefault("domain", record.get(DOMAIN_COL, ""))
            record.setdefault("info", "")
            record.setdefault("lp_info", record.get("LPスクショのDrive URL", ""))
            record.setdefault("updated_at", record.get("転記元DBの更新日", ""))
            record.setdefault("affili_filename", "")
        else:
            record.setdefault("domain", record.get("domain", ""))
            record.setdefault("info", record.get("info", ""))
            record.setdefault("lp_info", record.get("lp_info", ""))
            record.setdefault("updated_at", record.get("updated_at", ""))
            record.setdefault("affili_filename", record.get("affili_filename", ""))
        result[name] = record
    return result


def list_genre_tabs(creds_data=None, sheet_url=None) -> list[str]:
    """登録済みジャンル名のリストを返す。"""
    if creds_data and sheet_url:
        try:
            spreadsheet = _get_spreadsheet(creds_data, sheet_url)
            return [ws.title for ws in spreadsheet.worksheets() if _is_genre_tab(ws.title)]
        except Exception:
            pass
    db = _load_local()
    return sorted(db.keys())


last_load_error: str = ""


def load_db(creds_data=None, sheet_url=None, genre: str = "") -> dict:
    """
    genre指定あり → {name: {列名: 値, ...}} のフラットdict
    genre指定なし → {genre: {name: {...}}} のネストdict
    """
    global last_load_error
    last_load_error = ""
    if creds_data and sheet_url:
        try:
            spreadsheet = _get_spreadsheet(creds_data, sheet_url)
            tabs = [ws.title for ws in spreadsheet.worksheets() if _is_genre_tab(ws.title)]
            if genre:
                if genre not in tabs:
                    return {}
                return _parse_worksheet(spreadsheet.worksheet(genre))
            return {tab: _parse_worksheet(spreadsheet.worksheet(tab)) for tab in tabs}
        except Exception as e:
            last_load_error = f"{type(e).__name__}: {e}"
    db = _load_local()
    if genre:
        return db.get(genre, {})
    return db


def get_genre_headers(genre: str, creds_data=None, sheet_url=None) -> list[str]:
    """指定ジャンルのタブのヘッダー行を返す。列形式かの判定に使う。"""
    if not (creds_data and sheet_url and genre):
        return []
    try:
        spreadsheet = _get_spreadsheet(creds_data, sheet_url)
        ws = spreadsheet.worksheet(genre)
        return [h.strip() for h in ws.row_values(1)]
    except Exception:
        return []


def upsert_clinic(name: str, domain: str, genre: str, info: str, affili_filename: str = "",
                  lp_info: str = "", creds_data=None, sheet_url=None, values: dict | None = None) -> bool:
    """指定ジャンルのタブに1件upsert。

    列形式のタブには values（{列名: 値}）を書く。info形式のタブには従来どおり
    name/domain/info/lp_info/affili_filename/updated_at を書く。
    未指定（""）の項目は既存値を保持する。
    """
    today = str(date.today())
    if creds_data and sheet_url:
        spreadsheet = _get_spreadsheet(creds_data, sheet_url)
        ws = _get_or_create_tab(spreadsheet, genre)
        all_values = ws.get_all_values()
        headers = [h.strip() for h in all_values[0]] if all_values else _HEADERS
        column_format = is_column_format(headers)
        name_idx = headers.index(NAME_COL) if column_format else 0
        existing_names = [r[name_idx].strip() for r in all_values[1:] if len(r) > name_idx]

        if column_format:
            payload = dict(values or {})
            payload[NAME_COL] = name
            if domain:
                payload.setdefault(DOMAIN_COL, domain)
            payload.setdefault("転記元DBの更新日", today)
            if name in existing_names:
                row_idx = existing_names.index(name) + 2
                current = all_values[row_idx - 1]
                current += [""] * (len(headers) - len(current))
                row_data = [
                    payload.get(h, current[i]) if payload.get(h, "") != "" else current[i]
                    for i, h in enumerate(headers)
                ]
                ws.update(f"A{row_idx}:{_col_letter(len(headers))}{row_idx}", [row_data])
            else:
                ws.append_row([payload.get(h, "") for h in headers])
            return True

        if name in existing_names:
            row_idx = existing_names.index(name) + 2
            existing_row = all_values[row_idx - 1] if row_idx - 1 < len(all_values) else []
            existing_lp_info = existing_row[3] if len(existing_row) > 3 else ""
            existing_affili = existing_row[4] if len(existing_row) > 4 else ""
            row_data = [name, domain, info,
                        lp_info if lp_info else existing_lp_info,
                        affili_filename if affili_filename else existing_affili,
                        today]
            ws.update(f"A{row_idx}:F{row_idx}", [row_data])
        else:
            ws.append_row([name, domain, info, lp_info, affili_filename, today])
        return True

    db = _load_local()
    if genre not in db:
        db[genre] = {}
    existing = db.get(genre, {}).get(name, {})
    record = dict(existing)
    record.update(values or {})
    record.update({
        "name": name, "domain": domain, "info": info, "updated_at": today,
        "affili_filename": affili_filename or existing.get("affili_filename", ""),
        "lp_info": lp_info or existing.get("lp_info", ""),
    })
    db[genre][name] = record
    return _save_local(db)


def delete_clinic(name: str, genre: str = "", creds_data=None, sheet_url=None) -> bool:
    """
    genre指定あり → そのタブからのみ削除
    genre指定なし → 全タブから削除
    """
    if creds_data and sheet_url:
        spreadsheet = _get_spreadsheet(creds_data, sheet_url)
        tabs = [ws.title for ws in spreadsheet.worksheets() if _is_genre_tab(ws.title)]
        target_tabs = [genre] if genre and genre in tabs else tabs
        for tab_name in target_tabs:
            ws = spreadsheet.worksheet(tab_name)
            all_values = ws.get_all_values()
            headers = [h.strip() for h in all_values[0]] if all_values else []
            name_idx = headers.index(NAME_COL) if is_column_format(headers) else 0
            for i, row in enumerate(all_values[1:], start=2):
                if len(row) > name_idx and row[name_idx].strip() == name:
                    ws.delete_rows(i)
                    break
        return True
    db = _load_local()
    if genre:
        if genre in db and name in db[genre]:
            del db[genre][name]
    else:
        for g in list(db.keys()):
            if name in db[g]:
                del db[g][name]
    return _save_local(db)


def get_clinic_lp_info(name: str, creds_data=None, sheet_url=None) -> tuple:
    """1院のlp_infoを (lp_plan, appeal) に分割して返す。見つからなければ ('', '')。

    列形式のタブでは「紹介ブロックに出すプラン」と「比較優位性1」を返す。
    """
    all_data = load_db(creds_data, sheet_url)
    for genre_entries in all_data.values():
        if not isinstance(genre_entries, dict) or name not in genre_entries:
            continue
        rec = genre_entries[name]
        if rec.get("_column_format"):
            plan = rec.get("紹介ブロックに出すプラン", "") or rec.get("比較表に出すプラン", "")
            appeal = rec.get("比較優位性1", "")
            if plan or appeal:
                return plan, appeal
            continue
        raw = rec.get("lp_info", "")
        if raw:
            parts = raw.split("---", 1)
            return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    return "", ""


# ── 生成に渡すテキストへの整形 ────────────────────────────
_SKIP_IN_TEXT = {
    "name", "info", "lp_info", "affili_filename", "updated_at", "domain",
    "_column_format", "推し順位", "画像のクリニック略称", "LPスクショのDrive URL",
}


def format_record(record: dict) -> str:
    """1件のDBレコードを記事生成に渡すテキストに整形する。

    列形式では「列名：値」を並べる。AIが使ってよい事実の範囲がここに閉じる。
    """
    if not record.get("_column_format"):
        return record.get("info", "")
    lines = []
    for key, value in record.items():
        if key in _SKIP_IN_TEXT or key in OPERATION_COLUMNS:
            continue
        if not str(value).strip():
            continue
        lines.append(f"{key}：{value}")
    return "\n".join(lines)


# ── 生成前の照合 ─────────────────────────────────────────
def _has_failure_marker(value: str) -> str:
    for marker in FAILURE_MARKERS:
        if marker in value:
            return marker
    return ""


def validate_record(record: dict, required: list | None = None) -> list[str]:
    """1件が記事生成に使える状態かを見る。使えない理由のリストを返す。

    値が入っていることを成功にしない。取得失敗・材料不足などが入っていたら弾く。
    info形式のレコードは列そのものが無いので、info の有無だけを見る。
    """
    if not record.get("_column_format"):
        info = record.get("info", "").strip()
        if not info:
            return ["info が空"]
        return []

    reasons = []
    for col in (required or REQUIRED_FOR_GENERATION):
        value = str(record.get(col, "")).strip()
        if col not in record:
            reasons.append(f"{col} の列がない")
        elif not value:
            reasons.append(f"{col} が空")
        else:
            marker = _has_failure_marker(value)
            if marker:
                reasons.append(f"{col} に「{marker}」が入っている")
    for col in REQUIRED_OPERATION:
        value = str(record.get(col, "")).strip()
        if not value:
            reasons.append(f"{col} が未記入")
        elif not value.startswith(OPERATION_PASS_PREFIXES):
            reasons.append(f"{col} が未通過（{value[:40]}）")
    return reasons


def validate_for_generation(records: dict, names: list, required: list | None = None) -> tuple[dict, dict]:
    """生成に使える案件と、弾いた案件の理由を返す。

    Returns: ({name: record}, {name: [理由]})
    """
    ok: dict = {}
    blocked: dict = {}
    for name in names:
        record = records.get(name)
        if record is None:
            blocked[name] = ["案件DBに未登録"]
            continue
        reasons = validate_record(record, required=required)
        if reasons:
            blocked[name] = reasons
        else:
            ok[name] = record
    return ok, blocked


def build_db_cache(clinic_names: list, genre: str = "", creds_data=None, sheet_url=None) -> dict:
    """案件名リストに一致するDBレコードを {name: 生成に渡すテキスト} で返す。

    ジャンル指定があれば先にそのタブを検索し、ヒットしなかった院は全ジャンル横断で
    再検索する（案件の実態はジャンル横断で共通のため）。
    """
    records = build_db_records(clinic_names, genre=genre, creds_data=creds_data, sheet_url=sheet_url)
    return {name: format_record(rec) for name, rec in records.items() if format_record(rec)}


def build_db_records(clinic_names: list, genre: str = "", creds_data=None, sheet_url=None) -> dict:
    """案件名リストに一致するDBレコードを {name: record} で返す。"""
    result: dict = {}
    remaining = list(clinic_names)

    if genre:
        flat = load_db(creds_data, sheet_url, genre=genre)
        for name in clinic_names:
            if name in flat:
                result[name] = flat[name]
                remaining.remove(name)

    if remaining:
        nested = load_db(creds_data, sheet_url)
        for name in remaining:
            for genre_entries in nested.values():
                if isinstance(genre_entries, dict) and name in genre_entries:
                    result[name] = genre_entries[name]
                    break

    return result


def collect_via_db(
    clinics: list,
    genre: str,
    claude_api_key: str = "",
    db_type: str = "",
    gemini_api_key: str = "",
    research_provider: str = "claude",
    creds_data=None,
    sheet_url=None,
    progress=None,
) -> tuple[dict, list, list]:
    """案件DBを唯一の事実の入口として案件情報を返す。

    ツールは取得しない。DBに無い案件・照合を通らない案件は記事に載せず人のキューへ
    回す。取得はブラウザを持つAIが行い、案件DBに列で入れる。

    Returns: ({name: info_text}, [], [弾いた案件名と理由の文字列])
    """
    def _log(msg: str) -> None:
        if progress:
            progress(msg)

    names = [c["name"] for c in clinics]
    records = build_db_records(names, genre=genre, creds_data=creds_data, sheet_url=sheet_url)
    ok, blocked = validate_for_generation(records, names)

    for name, reasons in blocked.items():
        _log(f"　→ 記事に載せません: {name}（{' / '.join(reasons)}）")

    usable = {name: format_record(rec) for name, rec in ok.items()}
    usable = {name: text for name, text in usable.items() if text.strip()}
    failed = [f"{name}（{' / '.join(reasons)}）" for name, reasons in blocked.items()]
    return usable, [], failed


def _load_local() -> dict:
    if not os.path.exists(DB_PATH):
        return {}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data:
            return {}
        first_val = next(iter(data.values()), {})
        if isinstance(first_val, dict) and ("genres" in first_val or ("domain" in first_val and "info" in first_val)):
            return _migrate_old_format(data)
        return data
    except Exception:
        return {}


def _migrate_old_format(old: dict) -> dict:
    """旧 {name: {domain, genres, info, updated_at}} → 新 {genre: {name: {...}}}"""
    new: dict = {}
    for name, entry in old.items():
        domain = entry.get("domain", "")
        info = entry.get("info", "")
        updated_at = entry.get("updated_at", "")
        genres = entry.get("genres") or ["未分類"]
        for g in genres:
            new.setdefault(g, {})[name] = {
                "name": name, "domain": domain, "info": info,
                "updated_at": updated_at, "_column_format": False,
            }
    return new


def _save_local(db: dict) -> bool:
    try:
        os.makedirs("config", exist_ok=True)
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
