import json
import os
from datetime import date
from typing import Optional

DB_PATH = "config/clinic_db.json"


def load_db() -> dict:
    if not os.path.exists(DB_PATH):
        return {}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_db(db: dict) -> bool:
    try:
        os.makedirs("config", exist_ok=True)
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def get_clinic_info(name: str) -> Optional[str]:
    db = load_db()
    entry = db.get(name)
    return entry.get("info") if entry else None


def upsert_clinic(name: str, domain: str, genres: list, info: str) -> bool:
    db = load_db()
    db[name] = {
        "domain": domain,
        "genres": genres,
        "info": info,
        "updated_at": str(date.today()),
    }
    return save_db(db)


def delete_clinic(name: str) -> bool:
    db = load_db()
    if name in db:
        del db[name]
    return save_db(db)


def build_db_cache(clinic_names: list) -> dict:
    """院名リストに対してDBから情報を引いたキャッシュdictを返す。"""
    db = load_db()
    return {
        name: db[name]["info"]
        for name in clinic_names
        if name in db and db[name].get("info")
    }
