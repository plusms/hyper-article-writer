"""UTF-8のテキストファイルの中身を、指定したセルにそのまま書き込む。

日本語をツールの引数としてJSONに載せると、バックスラッシュuのエスケープが混ざる。
コードポイントを1字間違えると別の漢字になり、目視では検証できない。
ファイルから読んで書く経路にすれば、日本語がJSONを通らない。

使い方:
    python push_cell.py --sheet 指示文 --cell C4 --file path/to/text.txt
    python push_cell.py --sheet 指示文 --cell C4 --file text.txt --show

--show は書き込まずに、いま入っている値と書こうとしている値の頭を出すだけ。
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


def open_sheet(url: str, creds_data: dict):
    creds = Credentials.from_service_account_info(creds_data, scopes=SCOPES)
    client = gspread.authorize(creds)
    url = (url or "").strip()
    if url.startswith("http"):
        return client.open_by_url(url)
    return client.open_by_key(url)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet", required=True, help="タブ名")
    parser.add_argument("--cell", required=True, help="A1形式のセル番地")
    parser.add_argument("--file", required=True, help="書き込むUTF-8テキストファイル")
    parser.add_argument("--spreadsheet", default="", help="省略時は案件データベース")
    parser.add_argument("--show", action="store_true", help="書き込まずに確認だけ")
    args = parser.parse_args()

    with open(args.file, encoding="utf-8") as f:
        text = f.read()
    if "\\u" in text:
        print("ファイルにバックスラッシュuが入っています。生の日本語で書き直してください",
              file=sys.stderr)
        return 1

    secrets = _load_secrets()
    creds = secrets.get("GCP_SERVICE_ACCOUNT_JSON")
    if isinstance(creds, str):
        creds = json.loads(creds)
    url = args.spreadsheet or secrets.get("CLINIC_DB_SHEET_URL")
    if not (creds and url):
        print("鍵かスプレッドシートURLが読めません", file=sys.stderr)
        return 1

    ws = open_sheet(url, creds).worksheet(args.sheet)
    before = ws.acell(args.cell).value or ""
    print("いまの値: " + str(len(before)) + "字")
    print("書く値: " + str(len(text)) + "字")
    if args.show:
        return 0

    ws.update_acell(args.cell, text)
    after = ws.acell(args.cell).value or ""
    print("書き込み後: " + str(len(after)) + "字")
    # 書き戻して1字ずつ突き合わせる。エスケープが混ざっていれば長さか中身が変わる
    if after.strip() == text.strip():
        print("読み戻して一致")
        return 0
    print("読み戻した値が一致しません", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
