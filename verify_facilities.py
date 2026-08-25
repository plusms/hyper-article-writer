"""院タブの所在地が、取得元URLのページに実際に書かれているかを機械で照合する。

取得Taskは「公式サイトのページを開いて読む」「取得元URLを記録する」と指示されている。
だが、記録したURLのページに書かれていない住所が入っていた。指示を強めても、
書いた値がそのURLに載っているかを誰も確かめていないので同じことが起きる。

使い方:
    python verify_facilities.py                     読むだけ。食い違いを一覧で出す
    python verify_facilities.py --write             食い違った行の取得成否を要確認にする
    python verify_facilities.py --genre 医療脱毛 --region 仙台

取得成否を要確認にすると facility_db がその行を弾くので、間違った住所が記事に出ない。
"""

import argparse
import re
import sys
import unicodedata

import requests

from core import facility_db
from run_mass import _load_secrets

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 25
# 建物名・階数は公式の表記とゆれるので、突き合わせるのは番地までにする。
# 「久茂地3丁目3-20」と「久茂地3-3-20」が同じだと分かる形に寄せる。
_TRIM = str.maketrans("", "", " 　-‐‑‒–—―−ー－()（）")
_UNIT_RE = re.compile(r"[都道府県市区町村郡]|丁目|番地|番|号")
_PREF_CUT_RE = re.compile(r"(北海道|東京都|京都府|大阪府|..[県]|[^\s]{2,4}?[市区町村])")


def normalize(text: str) -> str:
    """全角と半角・区切り記号のゆれを消して比べられる形にする。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_TRIM)
    return _UNIT_RE.sub("", text)


def address_core(address: str) -> str:
    """住所から番地までを取り出す。建物名と階数は落とす。"""
    norm = normalize(address)
    # 数字とハイフンが続く最後のかたまりまでを番地とみなす
    match = re.search(r"^(.*?\d[\d]*)(?=[^\d]|$)", norm)
    core = match.group(1) if match else norm
    # 数字が1つしか無い住所は短すぎて誤判定するので、もう少し取る
    if len(core) < 8:
        core = norm[:14]
    return core


def fetch(url: str) -> str:
    try:
        res = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        res.encoding = res.apparent_encoding or res.encoding
        return res.text
    except Exception as e:
        return "__FETCH_ERROR__" + type(e).__name__


def page_text(html: str) -> str:
    """ページ全文をタグを外して比べられる形にする。"""
    plain = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    plain = re.sub(r"<[^>]+>", " ", plain)
    return normalize(plain)


def address_variants(address: str) -> list:
    """住所の番地までを、前を削った形もあわせて返す。

    公式ページは都道府県や市名を省いて書くことがある。全部を要求すると
    正しい住所を食い違いと判定してしまう。
    """
    core = address_core(address)
    variants = [core]
    # 都道府県ぶん・市区町村ぶんを前から削った形も試す
    raw = unicodedata.normalize("NFKC", address or "")
    for cut in _PREF_CUT_RE.finditer(raw):
        rest = raw[cut.end():]
        trimmed = address_core(rest)
        if len(trimmed) >= 8 and trimmed not in variants:
            variants.append(trimmed)
    return variants


def verify_row(row: dict) -> tuple:
    """1行を照合する。Returns: (判定, 説明)"""
    status = (row.get("取得成否", "") or "").strip()
    if status.startswith("院なし") or (row.get("院名", "") or "").strip() in facility_db.NO_FACILITY_VALUES:
        return "skip", "院なし"
    address = (row.get("所在地", "") or "").strip()
    url = (row.get("取得元URL", "") or "").strip().split(chr(10))[0]
    if not address or address == "公式に記載なし":
        return "skip", "所在地なし"
    if not url:
        return "ng", "取得元URLが空"
    html = fetch(url)
    if html.startswith("__FETCH_ERROR__"):
        return "error", "取得元URLを開けない（" + html.replace("__FETCH_ERROR__", "") + "）"
    text = page_text(html)
    for variant in address_variants(address):
        if variant and variant in text:
            return "ok", ""
    return "ng", "所在地が取得元ページに無い（" + address[:40] + " / " + url + "）"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--genre", default="医療脱毛")
    parser.add_argument("--region", default="")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    secrets = _load_secrets()
    creds = secrets.get("GCP_SERVICE_ACCOUNT_JSON")
    sheet = secrets.get("CLINIC_DB_SHEET_URL")
    if isinstance(creds, str):
        import json
        creds = json.loads(creds)
    if not (creds and sheet):
        print("鍵かスプレッドシートURLが読めません", file=sys.stderr)
        return 1

    rows = facility_db.load_facilities_with_rows(creds, sheet, args.genre)
    if args.region:
        rows = [(n, r) for n, r in rows if (r.get("地域名", "") or "").strip() == args.region]
    print("照合する行: " + str(len(rows)))

    ng = []
    for i, row in rows:
        verdict, note = verify_row(row)
        label = (row.get("地域名", "") or "") + " / " + (row.get("クリニック名", "") or "") \
            + " / " + (row.get("院名", "") or "")
        if verdict == "ok":
            continue
        if verdict == "skip":
            continue
        ng.append((i, label, note))
        print("NG " + label + " → " + note)

    print("")
    print("食い違い: " + str(len(ng)) + "件")
    if not args.write or not ng:
        return 0

    updated = facility_db.mark_rows_unverified(creds, sheet, args.genre, [r[0] for r in ng])
    print("取得成否を要確認にした行: " + str(updated))
    return 0


if __name__ == "__main__":
    sys.exit(main())
