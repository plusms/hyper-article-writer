"""量産タブをコマンドから回す。画面を開いていなくても動く。

Streamlit の画面から回すと、ブラウザが繋がっている間しか動かない。20本流すと
途中で接続が切れて全部やり直しになる。ここは同じ core のコードを呼ぶだけで、
記事の作り方は画面から回すのと1ミリも変わらない。

使い方:
    python run_mass.py --site ノックス --genre 医療脱毛
    python run_mass.py --site ノックス --genre 医療脱毛 --rows 4,5,6 --workers 6

止まった行は「処理中」「本文まで完了」のまま残る。もう一度同じコマンドを叩けば
その行から拾い直す。完了した行は飛ばす。
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from core import clinic_db_manager, pipeline, site_config_manager
from core.sheets import (
    get_sheet, read_input_rows_mass, write_output_row_mass, write_status_mass,
)

# シートへの書き込みは1本ずつ。並列で同じタブに書くと行がずれる
_sheet_lock = threading.Lock()
_print_lock = threading.Lock()

RESUME_STATES = ("", "処理中", "本文まで完了")


def _say(prefix: str, message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    with _print_lock:
        print(f"[{stamp}] {prefix} {message}", flush=True)


def _load_secrets() -> dict:
    """.streamlit/secrets.toml か環境変数から鍵を読む。

    画面と同じ設定を使う。ここで別の場所に鍵を置くと二重管理になる。
    """
    values = {}
    path = os.path.join(".streamlit", "secrets.toml")
    if os.path.exists(path):
        try:
            import tomllib
            with open(path, "rb") as f:
                values = tomllib.load(f)
        except Exception as e:
            print(f"secrets.toml を読めませんでした: {e}", file=sys.stderr)
    for key in ("CLAUDE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
                "ARTICLE_SHEET_URL", "CLINIC_DB_SHEET_URL", "SITE_INFO_SHEET_URL",
                "DRIVE_PARENT_FOLDER_ID", "SITE_CONFIG_FOLDER_ID",
                "GCP_SERVICE_ACCOUNT_JSON"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def _gcp_creds(secrets: dict):
    """画面と同じ3通りの置き方に合わせる。"""
    nested = secrets.get("gcp_service_account")
    if isinstance(nested, dict) and nested:
        return dict(nested)
    raw = secrets.get("GCP_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    path = os.environ.get("GCP_SERVICE_ACCOUNT_FILE", "service_account.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _build_settings(args, secrets, creds) -> pipeline.Settings:
    site_parts = ""
    site_components = []
    if args.site:
        try:
            folder = secrets.get("DRIVE_PARENT_FOLDER_ID", "0ANR02wEPgx88Uk9PVA")
            direct = secrets.get("SITE_CONFIG_FOLDER_ID", "")
            if direct:
                site_config_manager.SITE_CONFIG_FOLDER_ID_OVERRIDE = direct
            config = site_config_manager.load_site_config(args.site, creds, folder)
            site_components = config.get("components", []) or []
            site_parts = site_config_manager.format_site_parts(site_components)
            link_rule = site_config_manager.format_link_settings(config.get("link_settings", {}))
            if link_rule:
                site_parts = "\n\n".join(filter(None, [site_parts, link_rule]))
        except Exception as e:
            print(f"サイト設定を読めませんでした（{type(e).__name__}）。パーツなしで生成します")
    return pipeline.Settings(
        claude_key=secrets.get("CLAUDE_API_KEY", ""),
        gemini_key=secrets.get("GEMINI_API_KEY", ""),
        article_provider=args.provider,
        research_provider=args.research_provider,
        gcp_creds=creds,
        db_sheet_url=args.db_sheet or secrets.get("CLINIC_DB_SHEET_URL", ""),
        site_info_sheet_url=secrets.get(
            "SITE_INFO_SHEET_URL", "1Mnan9LI3HAwd7n1VABvdTnrYBmpLre2yYWwrtt8PlNk"),
        site_parts=site_parts,
        site_components=site_components,
        site_name=args.site,
        auto_review=not args.no_review,
    )


OUTPUT_DIR = "output"


def save_local_copy(row_num: int, kw: str, result: dict) -> None:
    """記事を手元にも残す。

    シートへの書き込みが失敗すると、生成に何分もかけた記事が消える。
    5万字を超えてセルに入らず落ちた実例がある。
    """
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        safe = "".join(c for c in str(kw) if c.isalnum() or c in " 　-_")[:40].strip()
        path = os.path.join(OUTPUT_DIR, str(row_num) + "_" + (safe or "article") + ".html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(result.get("html", ""))
        note = result.get("todo_list", "")
        if note:
            with open(path.replace(".html", "_要確認.txt"), "w", encoding="utf-8") as f:
                f.write(note)
        _say("", "手元に保存しました: " + path)
    except Exception as e:
        _say("", "手元への保存に失敗しました（" + type(e).__name__ + "）")


def _run_one(row, args, settings, ws) -> tuple:
    row_num = row["row_index"]
    kw = row["main_kw"]
    prefix = f"行{row_num} {kw}"
    started = time.monotonic()

    def log(message: str) -> None:
        _say(prefix, str(message).strip())

    with _sheet_lock:
        write_status_mass(ws, row_num, "処理中")

    inputs = {
        "article_type": row.get("article_type", "地域"),
        "site_name": args.site,
        "main_kw": kw,
        "sub_kw": [k.strip() for k in row.get("sub_kw", "").split(",") if k.strip()],
        "genre": args.genre,
        "recommended": "",
        "custom_block": row.get("custom_block", ""),
        "custom_intent": "",
        "related_kw": "",
        "clinics": [],
        "competitor_urls": [],
        "selected_topics": None,
        "region": row.get("region", ""),
        "slug": row.get("slug", ""),
    }

    def on_body(partial: dict) -> None:
        with _sheet_lock:
            write_output_row_mass(ws, row_num, partial)
            write_status_mass(ws, row_num, "本文まで完了")
        log("本文までシートに保存しました")

    try:
        result = pipeline.generate_article(inputs, settings, log=log, on_body=on_body)
    except Exception as e:
        with _sheet_lock:
            write_status_mass(ws, row_num, f"エラー: {type(e).__name__}")
        log(f"失敗しました: {type(e).__name__}: {e}")
        return (row_num, kw, False, time.monotonic() - started)

    with _sheet_lock:
        save_local_copy(row_num, kw, result)
        write_output_row_mass(ws, row_num, result)
        write_status_mass(ws, row_num, "完了")
    minutes = (time.monotonic() - started) / 60
    log(f"完了しました（{minutes:.1f}分・{len(result['html'])}字）")
    return (row_num, kw, True, time.monotonic() - started)


def main() -> int:
    parser = argparse.ArgumentParser(description="量産タブの記事をまとめて作る")
    parser.add_argument("--site", required=True, help="サイト名。サイト設定のパーツとリンクの規則に使う")
    parser.add_argument("--genre", required=True, help="ジャンル名。案件DBのタブ名と同じもの")
    parser.add_argument("--rows", default="", help="行番号を絞る。例 4,5,6。省略すると未処理の全行")
    parser.add_argument("--workers", type=int, default=4, help="同時に走らせる本数")
    parser.add_argument("--tab", default="量産", help="読む記事タブ")
    parser.add_argument("--sheet", default="", help="記事スプレッドシートのURL")
    parser.add_argument("--db-sheet", default="", help="案件DBのスプレッドシートURL")
    parser.add_argument("--provider", default="claude", choices=["claude", "gemini", "openai"])
    parser.add_argument("--research-provider", default="gemini", choices=["claude", "gemini", "openai"])
    parser.add_argument("--no-review", action="store_true", help="検品を回さない")
    parser.add_argument("--dry-run", action="store_true", help="対象行を出すだけで生成しない")
    args = parser.parse_args()

    secrets = _load_secrets()
    creds = _gcp_creds(secrets)
    if not creds:
        print("GCPのサービスアカウントが見つかりません。.streamlit/secrets.toml か環境変数を確認してください", file=sys.stderr)
        return 1
    sheet_url = args.sheet or secrets.get("ARTICLE_SHEET_URL", "")
    if not sheet_url:
        print("記事スプレッドシートのURLがありません。--sheet で渡してください", file=sys.stderr)
        return 1

    settings = _build_settings(args, secrets, creds)
    if not settings.db_sheet_url:
        print("案件DBのURLがありません。--db-sheet で渡してください", file=sys.stderr)
        return 1

    ws = get_sheet(sheet_url, creds, tab_name=args.tab)
    rows = read_input_rows_mass(ws, site_name=args.site, genre=args.genre)
    pending = [r for r in rows if (r.get("status") or "") in RESUME_STATES]
    if args.rows:
        wanted = {int(x) for x in args.rows.replace(" ", "").split(",") if x}
        pending = [r for r in pending if r["row_index"] in wanted]

    if not pending:
        print("対象の行がありません。ステータスが空・処理中・本文まで完了の行だけを拾います")
        return 0

    print(f"対象 {len(pending)} 行 / 同時 {args.workers} 本 / モデル {args.provider}")
    for row in pending:
        print(f"  行{row['row_index']} {row['main_kw']}（{row.get('status') or '未処理'}）")
    if args.dry_run:
        return 0

    # 案件DBは全行で同じものを読む。先に1回読んで覚えさせ、並列で同時に叩かない
    clinic_db_manager.list_clinics_by_rank(
        args.genre, creds_data=creds, sheet_url=settings.db_sheet_url,
    )

    started = time.monotonic()
    done, failed = [], []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(_run_one, row, args, settings, ws): row for row in pending}
        for future in as_completed(futures):
            row_num, kw, ok, _elapsed = future.result()
            (done if ok else failed).append(f"行{row_num} {kw}")

    minutes = (time.monotonic() - started) / 60
    print(f"\n終わりました。{minutes:.1f}分")
    print(f"完了 {len(done)} 本")
    for item in done:
        print(f"  {item}")
    if failed:
        print(f"失敗 {len(failed)} 本")
        for item in failed:
            print(f"  {item}")
        print("同じコマンドをもう一度叩くと、失敗した行から拾い直します")
    return 0


if __name__ == "__main__":
    sys.exit(main())
