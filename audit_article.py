"""記事1本を全項目1回で判定する。

1つ直しては流すのを繰り返すと、同じ層の残りが次の実行で出てくる。
先に全項目を出してから直す。

使い方:
    python audit_article.py --file article.html --genre 医療脱毛 --type 地域 \\
        --main-kw "医療脱毛 仙台" --clinics 10
    python audit_article.py --row 12 --genre 医療脱毛 --type 地域
"""

import argparse
import json
import sys

import re

from core import article_audit, article_type_db, expression_rules, output_check
from core import site_config_manager
from run_mass import _load_secrets


def load_context(genre: str, article_type: str):
    """型と表現ルールを読み込む。読めなければその項目は判定しない。"""
    secrets = _load_secrets()
    creds = secrets.get("GCP_SERVICE_ACCOUNT_JSON")
    if isinstance(creds, str):
        creds = json.loads(creds)
    url = secrets.get("CLINIC_DB_SHEET_URL")
    if not (creds and url):
        return {}, "", secrets
    record = article_type_db.pick_type(
        article_type_db.load_types(creds, url), article_type, genre)
    # 見本は全列つなげて渡す。紹介ブロックだけ渡すと、比較表や選び方で使う
    # クラス名が全部「見本に無い」と出る。仙台の1本目で red が出た。
    reference = "\n".join(
        article_type_db.get_reference_html(record or {}, column)
        for column in article_type_db.REFERENCE_COLUMNS
    )
    rules = expression_rules.for_genre(
        expression_rules.load_rules(creds, url), genre)
    output_check.set_genre_rules(rules["ng_words"], rules["replacements"])
    return record or {}, reference, secrets


def read_row(secrets: dict, row: int) -> str:
    """量産タブの指定行の本文を読む。"""
    from core.sheets import get_sheet
    creds = secrets.get("GCP_SERVICE_ACCOUNT_JSON")
    if isinstance(creds, str):
        creds = json.loads(creds)
    book = get_sheet(secrets.get("ARTICLE_SHEET_URL"), creds)
    for title in ("量産", "地域", "比較", "商標"):
        try:
            ws = book.worksheet(title)
        except Exception:
            continue
        values = ws.row_values(row)
        for value in values:
            if "<" in value and len(value) > 500:
                return value
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="")
    parser.add_argument("--row", type=int, default=0)
    parser.add_argument("--genre", default="")
    parser.add_argument("--type", dest="article_type", default="地域")
    parser.add_argument("--main-kw", dest="main_kw", default="")
    parser.add_argument("--sub-kw", dest="sub_kw", default="")
    parser.add_argument("--clinics", type=int, default=0)
    parser.add_argument("--site", default="", help="サイト設定のパーツも既知として扱う")
    args = parser.parse_args()

    record, reference, secrets = load_context(args.genre, args.article_type)

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            html = f.read()
    elif args.row:
        html = read_row(secrets, args.row)
    else:
        html = sys.stdin.read()
    if not html.strip():
        print("記事のHTMLが空です", file=sys.stderr)
        return 1

    known = set()
    if args.site:
        creds = secrets.get("GCP_SERVICE_ACCOUNT_JSON")
        if isinstance(creds, str):
            creds = json.loads(creds)
        folder = secrets.get("SITE_CONFIG_FOLDER_ID") or secrets.get("DRIVE_PARENT_FOLDER_ID")
        if secrets.get("SITE_CONFIG_FOLDER_ID"):
            site_config_manager.SITE_CONFIG_FOLDER_ID_OVERRIDE = secrets["SITE_CONFIG_FOLDER_ID"]
        config = site_config_manager.load_site_config(args.site, creds, folder)
        for component in config.get("components", []) or []:
            if not component.get("active", True):
                continue
            for value in re.findall(r'class="([^"]+)"', str(component.get("pattern", ""))):
                known.update(value.split())

    checks = article_audit.audit(
        html,
        known_classes=known,
        main_kw=args.main_kw,
        sub_kw=[x.strip() for x in args.sub_kw.split(",") if x.strip()],
        clinic_count=args.clinics,
        type_record=record,
        reference=reference,
    )
    print(article_audit.format_report(checks))
    return 1 if any(c["判定"] == "NG" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
