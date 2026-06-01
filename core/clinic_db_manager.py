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
_HEADERS = ["name", "domain", "info", "lp_info", "affili_filename", "updated_at"]
_SYSTEM_TABS = {"clinic_db"}  # old single-tab name; skip when listing genre tabs


def _get_spreadsheet(creds_data: dict, sheet_url: str):
    creds = Credentials.from_service_account_info(creds_data, scopes=_SCOPES)
    gc = gspread.authorize(creds)
    sheet_url = sheet_url.strip()
    if not sheet_url.startswith("http"):
        return gc.open_by_key(sheet_url)
    return gc.open_by_url(sheet_url)


def _get_or_create_tab(spreadsheet, genre: str) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(genre)
        return ws
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=genre, rows=1000, cols=len(_HEADERS))
        ws.update("A1:F1", [_HEADERS])
        return ws


def _parse_worksheet(ws: gspread.Worksheet) -> dict:
    rows = ws.get_all_values()
    result = {}
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        padded = row + [""] * (6 - len(row))
        name, domain, info, lp_info, affili_filename, updated_at = padded[:6]
        result[name] = {"domain": domain, "info": info, "updated_at": updated_at,
                        "affili_filename": affili_filename, "lp_info": lp_info}
    return result


def list_genre_tabs(creds_data=None, sheet_url=None) -> list[str]:
    """登録済みジャンル名のリストを返す。"""
    if creds_data and sheet_url:
        try:
            spreadsheet = _get_spreadsheet(creds_data, sheet_url)
            return [ws.title for ws in spreadsheet.worksheets() if ws.title not in _SYSTEM_TABS]
        except Exception:
            pass
    db = _load_local()
    return sorted(db.keys())


last_load_error: str = ""


def load_db(creds_data=None, sheet_url=None, genre: str = "") -> dict:
    """
    genre指定あり → {name: {domain, info, updated_at}} のフラットdict
    genre指定なし → {genre: {name: {domain, info, updated_at}}} のネストdict
    """
    global last_load_error
    last_load_error = ""
    if creds_data and sheet_url:
        try:
            spreadsheet = _get_spreadsheet(creds_data, sheet_url)
            tabs = [ws.title for ws in spreadsheet.worksheets() if ws.title not in _SYSTEM_TABS]
            if genre:
                if genre not in tabs:
                    return {}
                return _parse_worksheet(spreadsheet.worksheet(genre))
            else:
                return {tab: _parse_worksheet(spreadsheet.worksheet(tab)) for tab in tabs}
        except Exception as e:
            last_load_error = f"{type(e).__name__}: {e}"
    db = _load_local()
    if genre:
        return db.get(genre, {})
    return db


def upsert_clinic(name: str, domain: str, genre: str, info: str, affili_filename: str = "", lp_info: str = "", creds_data=None, sheet_url=None) -> bool:
    """指定ジャンルのタブに1件upsert。affili_filename・lp_info未指定（""）の場合は既存値を保持。"""
    today = str(date.today())
    if creds_data and sheet_url:
        spreadsheet = _get_spreadsheet(creds_data, sheet_url)
        ws = _get_or_create_tab(spreadsheet, genre)
        all_values = ws.get_all_values()
        all_names = [r[0] for r in all_values[1:] if r]
        if name in all_names:
            row_idx = all_names.index(name) + 2
            existing_row = all_values[row_idx - 1] if row_idx - 1 < len(all_values) else []
            existing_lp_info = existing_row[3] if len(existing_row) > 3 else ""
            existing_affili  = existing_row[4] if len(existing_row) > 4 else ""
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
    existing_affili  = db.get(genre, {}).get(name, {}).get("affili_filename", "")
    existing_lp_info = db.get(genre, {}).get(name, {}).get("lp_info", "")
    db[genre][name] = {
        "domain": domain, "info": info, "updated_at": today,
        "affili_filename": affili_filename if affili_filename else existing_affili,
        "lp_info": lp_info if lp_info else existing_lp_info,
    }
    return _save_local(db)


def delete_clinic(name: str, genre: str = "", creds_data=None, sheet_url=None) -> bool:
    """
    genre指定あり → そのタブからのみ削除
    genre指定なし → 全タブから削除
    """
    if creds_data and sheet_url:
        spreadsheet = _get_spreadsheet(creds_data, sheet_url)
        tabs = [ws.title for ws in spreadsheet.worksheets() if ws.title not in _SYSTEM_TABS]
        target_tabs = [genre] if genre and genre in tabs else tabs
        for tab_name in target_tabs:
            ws = spreadsheet.worksheet(tab_name)
            all_values = ws.get_all_values()
            for i, row in enumerate(all_values[1:], start=2):
                if row and row[0] == name:
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


def build_db_cache(clinic_names: list, genre: str = "", creds_data=None, sheet_url=None) -> dict:
    """指定ジャンルのDBから案件名リストに一致するものだけ {name: info_str} で返す。
    ジャンル指定があれば先にそのタブを検索し、ヒットしなかった院は全ジャンル横断で再検索する。
    """
    result: dict = {}
    remaining = list(clinic_names)

    if genre:
        flat = load_db(creds_data, sheet_url, genre=genre)
        for name in clinic_names:
            if name in flat and flat[name].get("info"):
                result[name] = flat[name]["info"]
                remaining.remove(name)

    if remaining:
        nested = load_db(creds_data, sheet_url)
        for name in remaining:
            for genre_entries in nested.values():
                if isinstance(genre_entries, dict) and name in genre_entries:
                    info = genre_entries[name].get("info", "")
                    if info:
                        result[name] = info
                        break

    return result


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
    """旧 {name: {domain, genres, info, updated_at}} → 新 {genre: {name: {domain, info, updated_at}}}"""
    new: dict = {}
    for name, entry in old.items():
        domain = entry.get("domain", "")
        info = entry.get("info", "")
        updated_at = entry.get("updated_at", "")
        genres = entry.get("genres") or ["未分類"]
        for g in genres:
            new.setdefault(g, {})[name] = {"domain": domain, "info": info, "updated_at": updated_at}
    return new


def _save_local(db: dict) -> bool:
    try:
        os.makedirs("config", exist_ok=True)
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
