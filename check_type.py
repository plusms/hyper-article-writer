"""型タブの見本HTMLを、サイト設定のパーツと突き合わせて検品する。

見本が間違っていると全記事に同じ間違いが複製される。生成後の機械照合は見本を
正としているので、見本自体の誤りは検出できない。実例＝医療脱毛の地域記事の見本に
class="picup" のtypoがあり、モデルは忠実に写し、照合も通り、人が毎回手で直していた。

型を登録したとき・見本を差し替えたときに1回走らせる。ジャンルとサイトが増えても
同じ手順で使える。

使い方:
    python check_type.py --site ノックス --genre 医療脱毛 --type 地域
"""

import argparse
import difflib
import json
import re
import sys

from core import article_type_db, site_config_manager
from run_mass import _load_secrets

# 見本にあってもサイト設定のパーツにする必要がないもの。素のHTMLタグの属性。
IGNORE_CLASSES: set = set()


def reference_classes(record: dict) -> dict:
    """見本HTMLに出てくるクラス名と、その出現回数を列に紐づけて返す。"""
    found: dict = {}
    for col in article_type_db.REFERENCE_COLUMNS:
        html = (record.get(col, "") or "").strip()
        if not html:
            continue
        for value in re.findall(r'class="([^"]+)"', html):
            for name in value.split():
                entry = found.setdefault(name, {"count": 0, "columns": set()})
                entry["count"] += 1
                entry["columns"].add(col)
    return found


def part_classes(components: list) -> set:
    """サイト設定のパーツが持つクラス名。"""
    names = set()
    for component in components or []:
        if not component.get("active", True):
            continue
        for value in re.findall(r'class="([^"]+)"', str(component.get("pattern", ""))):
            names.update(value.split())
    return names


def guess_typo(name: str, known: set) -> str:
    """登録済みのクラス名に1字違いで似ているものがあれば返す。"""
    close = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.85)
    return close[0] if close else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--genre", required=True)
    parser.add_argument("--type", dest="article_type", required=True)
    args = parser.parse_args()

    secrets = _load_secrets()
    creds = secrets.get("GCP_SERVICE_ACCOUNT_JSON")
    if isinstance(creds, str):
        creds = json.loads(creds)
    db_url = secrets.get("CLINIC_DB_SHEET_URL")
    folder = secrets.get("SITE_CONFIG_FOLDER_ID") or secrets.get("DRIVE_PARENT_FOLDER_ID")
    if not (creds and db_url):
        print("鍵かスプレッドシートURLが読めません", file=sys.stderr)
        return 1

    rows = article_type_db.load_types(creds, db_url)
    record = article_type_db.pick_type(rows, args.article_type, args.genre)
    if not record:
        print("型が見つかりません: " + args.genre + " / " + args.article_type, file=sys.stderr)
        return 1

    if secrets.get("SITE_CONFIG_FOLDER_ID"):
        site_config_manager.SITE_CONFIG_FOLDER_ID_OVERRIDE = secrets["SITE_CONFIG_FOLDER_ID"]
    config = site_config_manager.load_site_config(args.site, creds, folder)
    components = config.get("components", []) or []
    known = part_classes(components)
    print("サイト設定のパーツが持つクラス名: " + str(len(known)) + "種")

    found = reference_classes(record)
    print("見本に出てくるクラス名: " + str(len(found)) + "種")

    unregistered = {n: v for n, v in found.items() if n not in known and n not in IGNORE_CLASSES}
    if not unregistered:
        print("")
        print("未登録のクラス名はありません")
        return 0

    print("")
    print("未登録のクラス名: " + str(len(unregistered)) + "種")
    print("パーツに登録するか、見本のほうが間違っているかを1つずつ決める")
    print("")
    for name in sorted(unregistered):
        info = unregistered[name]
        near = guess_typo(name, known)
        note = "　似ている登録済み: " + near if near else ""
        columns = "、".join(sorted(info["columns"]))
        print("- " + name + "（" + str(info["count"]) + "回 / " + columns + "）" + note)

    print("")
    print("見本の役割ごとのクラス名も確認する")
    for role in site_config_manager.PART_ROLE_KEYWORDS:
        names = site_config_manager.part_class_names(components, role)
        state = "、".join(names) if names else "パーツ未登録"
        print("- " + role + ": " + state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
