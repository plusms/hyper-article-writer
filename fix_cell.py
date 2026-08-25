"""セルの中の文字列を置き換える。読んで直して書き戻し、読み戻して確かめる。

見本HTMLのように長いセルを、丸ごと書き直さずに一箇所だけ直したいときに使う。
丸ごと書き直すと、日本語をツールの引数に載せる経路を通り、エスケープが混ざる。

使い方:
    python fix_cell.py --sheet 型 --cell R2 --before picup --after pickup
    python fix_cell.py --sheet 型 --cell R2 --before picup --after pickup --show
"""

import argparse
import json
import sys

import gspread
from google.oauth2.service_account import Credentials

from run_mass import _load_secrets

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet", required=True)
    parser.add_argument("--cell", required=True)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--spreadsheet", default="")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    secrets = _load_secrets()
    creds = secrets.get("GCP_SERVICE_ACCOUNT_JSON")
    if isinstance(creds, str):
        creds = json.loads(creds)
    url = args.spreadsheet or secrets.get("CLINIC_DB_SHEET_URL")
    if not (creds and url):
        print("鍵かスプレッドシートURLが読めません", file=sys.stderr)
        return 1

    client = gspread.authorize(Credentials.from_service_account_info(creds, scopes=SCOPES))
    book = client.open_by_url(url) if url.strip().startswith("http") else client.open_by_key(url)
    ws = book.worksheet(args.sheet)

    before = ws.acell(args.cell).value or ""
    hits = before.count(args.before)
    print("セルの長さ: " + str(len(before)) + "字")
    print(args.before + " の出現回数: " + str(hits))
    if hits == 0:
        print("置き換える対象がありません")
        return 0
    after = before.replace(args.before, args.after)
    print("置き換え後の長さ: " + str(len(after)) + "字")
    if args.show:
        return 0

    ws.update_acell(args.cell, after)
    back = ws.acell(args.cell).value or ""
    if back.strip() != after.strip():
        print("読み戻した値が一致しません", file=sys.stderr)
        return 1
    print("読み戻して一致。残った " + args.before + ": " + str(back.count(args.before)) + "件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
