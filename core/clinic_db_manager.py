import json
import os
from datetime import date
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

DB_PATH = "config/clinic_db.json"
DB_SHEET_TAB = "clinic_db"
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_HEADERS = ["name", "domain", "genres", "info", "updated_at"]


def _get_worksheet(creds_data: dict, sheet_url: str) -> gspread.Worksheet:
    creds = Credentials.from_service_account_info(creds_data, scopes=_SCOPES)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_url(sheet_url)
    try:
        return spreadsheet.worksheet(DB_SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=DB_SHEET_TAB, rows=1000, cols=len(_HEADERS))
        ws.update("A1:E1", [_HEADERS])
        return ws


def load_db(creds_data=None, sheet_url=None) -> dict:
    if creds_data and sheet_url:
        try:
            ws = _get_worksheet(creds_data, sheet_url)
            rows = ws.get_all_values()
            result = {}
            for row in rows[1:]:
                if not row or not row[0]:
                    continue
                padded = row + [""] * (5 - len(row))
                name, domain, genres_str, info, updated_at = padded[:5]
                result[name] = {
                    "domain": domain,
                    "genres": [g.strip() for g in genres_str.split(",") if g.strip()],
                    "info": info,
                    "updated_at": updated_at,
                }
            return result
        except Exception:
            pass
    # fallback: ローカルJSON
    if not os.path.exists(DB_PATH):
        return {}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def upsert_clinic(name: str, domain: str, genres: list, info: str, creds_data=None, sheet_url=None) -> bool:
    today = str(date.today())
    if creds_data and sheet_url:
        try:
            ws = _get_worksheet(creds_data, sheet_url)
            all_values = ws.get_all_values()
            all_names = [r[0] for r in all_values[1:] if r]
            row_data = [name, domain, ", ".join(genres), info, today]
            if name in all_names:
                row_idx = all_names.index(name) + 2
                ws.update(f"A{row_idx}:E{row_idx}", [row_data])
            else:
                ws.append_row(row_data)
            return True
        except Exception:
            pass
    # fallback: ローカルJSON
    db = load_db()
    db[name] = {"domain": domain, "genres": genres, "info": info, "updated_at": today}
    return _save_local(db)


def delete_clinic(name: str, creds_data=None, sheet_url=None) -> bool:
    if creds_data and sheet_url:
        try:
            ws = _get_worksheet(creds_data, sheet_url)
            all_values = ws.get_all_values()
            for i, row in enumerate(all_values[1:], start=2):
                if row and row[0] == name:
                    ws.delete_rows(i)
                    return True
            return True
        except Exception:
            pass
    db = load_db()
    if name in db:
        del db[name]
    return _save_local(db)


def build_db_cache(clinic_names: list, creds_data=None, sheet_url=None) -> dict:
    db = load_db(creds_data, sheet_url)
    return {
        name: db[name]["info"]
        for name in clinic_names
        if name in db and db[name].get("info")
    }


def _save_local(db: dict) -> bool:
    try:
        os.makedirs("config", exist_ok=True)
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
