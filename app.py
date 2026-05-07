import json
import os
import pathlib
import datetime
import time
import contextlib
import streamlit as st

from core.config import TOPICS
from core.researcher import (
    analyze_competitors, collect_clinic_info,
    discover_clinics_from_competitors, auto_discover_clinics,
    crawl_site, extract_clinic_info_from_content,
    DB_TYPE_CLINIC, DB_TYPE_LIFESTYLE,
)
from core.planner import generate_structure
from core.writer import generate_body, quality_check
from core.sheets import (
    read_input_rows, write_output_row, write_status, get_sheet,
    get_settings_sheet, read_defaults, ARTICLE_TABS,
)
from core import site_config_manager, image_generator, drive_uploader, clinic_block_writer, clinic_db_manager

st.set_page_config(page_title="CV Article Writer", layout="wide", page_icon="✍️")

# ── パスワードゲート ──────────────────────────────────────
def _secret(key: str, default: str = "") -> str:
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, default)

_app_password = _secret("APP_PASSWORD")
if _app_password:
    _entered = st.sidebar.text_input("パスワード", type="password", key="_pw")
    if _entered != _app_password:
        st.sidebar.warning("パスワードを入力してください")
        st.stop()

# ── GCP認証（Secrets優先 → ファイルアップロード fallback）──
def _get_gcp_creds(uploaded_file) -> dict | None:
    # 方式1: TOML ネスト形式 [gcp_service_account]
    try:
        return dict(st.secrets["gcp_service_account"])
    except Exception:
        pass
    # 方式2: JSON文字列 GCP_SERVICE_ACCOUNT_JSON = '''...'''
    try:
        return json.loads(st.secrets["GCP_SERVICE_ACCOUNT_JSON"])
    except Exception:
        pass
    # 方式3: ファイルアップロード
    if uploaded_file:
        uploaded_file.seek(0)
        return json.load(uploaded_file)
    return None

# ── APIキー（Secrets優先 → サイドバー入力 fallback）──────────
_claude_key_default  = _secret("CLAUDE_API_KEY")
_gemini_key_default  = _secret("GEMINI_API_KEY")
_drive_folder_id          = _secret("DRIVE_PARENT_FOLDER_ID", "1CHqNruWiOVdeJPs7Nyd3Nfjt3sLxMc2c")
_article_sheet_url_default    = _secret("ARTICLE_SHEET_URL")
_db_sheet_url_default         = _secret("CLINIC_DB_SHEET_URL")
_lifestyle_sheet_url_default  = _secret("LIFESTYLE_DB_SHEET_URL")

# ── サイドバー：設定 ──────────────────────────────────────
with st.sidebar:
    st.header("設定")
    st.radio(
        "セクション",
        ["📝 コンテンツ作成", "🗄️ データ・設定"],
        key="main_nav",
        label_visibility="collapsed",
        horizontal=True,
    )
    st.divider()
    if _claude_key_default:
        st.caption("Claude API Key: Secrets から読込済み")
        claude_key = _claude_key_default
    else:
        claude_key = st.text_input("Claude API Key", type="password")

    if _gemini_key_default:
        st.caption("Gemini API Key: Secrets から読込済み")
        gemini_key = _gemini_key_default
    else:
        gemini_key = st.text_input("Gemini API Key（画像生成用）", type="password")

    _gcp_in_secrets = _secret("gcp_service_account.type") or _secret("GCP_SERVICE_ACCOUNT_JSON")
    if _gcp_in_secrets:
        st.caption("Google Sheets 認証: Secrets から読込済み")
        sheets_creds_file = None
    else:
        sheets_creds_file = st.file_uploader("Google Sheets 認証JSON", type="json")
        if sheets_creds_file:
            st.success("認証ファイル読み込み済み")

    st.divider()
    if _article_sheet_url_default:
        st.caption("記事スプシ: Secrets から読込済み")
        article_sheet_url = _article_sheet_url_default
    else:
        article_sheet_url = st.text_input(
            "記事スプレッドシートURL",
            placeholder="https://docs.google.com/spreadsheets/d/...",
            key="article_sheet_url_input",
        )

    if _db_sheet_url_default:
        st.caption("クリニックDB スプシ: Secrets から読込済み")
        db_sheet_url = _db_sheet_url_default
    else:
        db_sheet_url = st.text_input(
            "クリニックDB スプレッドシートURL",
            placeholder="https://docs.google.com/spreadsheets/d/...",
            key="db_sheet_url_input",
        )

    if _lifestyle_sheet_url_default:
        st.caption("ライフスタイルDB スプシ: Secrets から読込済み")
        lifestyle_sheet_url = _lifestyle_sheet_url_default
    else:
        lifestyle_sheet_url = st.text_input(
            "ライフスタイルDB スプレッドシートURL",
            placeholder="https://docs.google.com/spreadsheets/d/...",
            key="lifestyle_sheet_url_input",
        )



if st.session_state.get("main_nav", "📝 コンテンツ作成") == "📝 コンテンツ作成":
    tab_batch, tab_custom, tab_rank, tab_qual = st.tabs([
        "📋 一括作成", "📝 カスタム作成", "🏥 ランキングブロック", "✅ 品質チェック",
    ])
    tab_cases = None
    tab_settings = None
else:
    tab_cases, tab_settings = st.tabs(["🗄️ 商品データベース", "⚙️ サイト設定"])
    tab_batch = tab_custom = tab_rank = tab_qual = None


# ════════════════════════════════════════════════════════
#  共通ヘルパー
# ════════════════════════════════════════════════════════
_OUTPUT_CACHE_DIR = pathlib.Path("output_cache")


@contextlib.contextmanager
def _safe_tab(tab):
    """アクティブセクション外のタブを空プレースホルダーに差し替えて非表示にする。"""
    if tab is not None:
        with tab:
            yield
    else:
        _ph = st.empty()
        with _ph.container():
            yield
        _ph.empty()


def _save_output_cache(kw: str, data: dict) -> None:
    _OUTPUT_CACHE_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_kw = kw[:30].replace(" ", "_").replace("/", "_")
    fname = f"{ts}_{safe_kw}.json"
    (_OUTPUT_CACHE_DIR / fname).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    files = sorted(_OUTPUT_CACHE_DIR.glob("*.json"))
    for old in files[:-20]:
        old.unlink()


def _load_output_cache() -> list[dict]:
    if not _OUTPUT_CACHE_DIR.exists():
        return []
    files = sorted(_OUTPUT_CACHE_DIR.glob("*.json"), reverse=True)[:10]
    results = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_cache_file"] = f.name
            results.append(data)
        except Exception:
            pass
    return results


def build_inputs_from_row(row: dict, defaults: dict | None = None) -> dict:
    clinics_raw = row.get("clinics_raw", "")
    clinics = []
    for item in clinics_raw.split(","):
        item = item.strip()
        if "::" in item:
            name, domain = item.split("::", 1)
            clinics.append({"name": name.strip(), "domain": domain.strip()})

    article_type = row.get("article_type", "地域")
    default_block = (defaults or {}).get(article_type, "")
    row_block = row.get("custom_block", "")
    combined = "\n".join(filter(None, [default_block, row_block]))

    return {
        "article_type":    article_type,
        "site_name":       row.get("site_name", ""),
        "main_kw":         row.get("main_kw", ""),
        "sub_kw":          [k.strip() for k in row.get("sub_kw", "").split(",") if k.strip()],
        "genre":           row.get("genre", ""),
        "recommended":     row.get("recommended", ""),
        "custom_block":    combined,
        "related_kw":      row.get("related_kw", ""),
        "clinics":         clinics,
        "competitor_urls": [u.strip() for u in row.get("competitor_urls_raw", "").split(",") if u.strip()],
        "selected_topics": None,  # バッチは全トピック使用
    }


def _render_topic_checkboxes(article_type: str, key_prefix: str) -> list[str]:
    """トピック選択チェックボックスを描画し、選択されたキーリストを返す。"""
    topics = TOPICS.get(article_type, [])
    selected = []
    cols = st.columns(3)
    for i, t in enumerate(topics):
        col = cols[i % 3]
        if t["fixed"]:
            col.checkbox(t["label"], value=True, disabled=True, key=f"{key_prefix}_topic_{t['key']}")
            selected.append(t["key"])
        else:
            checked = col.checkbox(t["label"], value=t["default"], key=f"{key_prefix}_topic_{t['key']}")
            if checked:
                selected.append(t["key"])
    return selected


# ════════════════════════════════════════════════════════
#  Tab1: 一括作成
# ════════════════════════════════════════════════════════
with _safe_tab(tab_batch):
    st.title("📋 一括作成")
    st.caption("K列（ステータス）が空欄の行を対象に一括生成します。設定タブのデフォルト追加指示＋H列の追記内容を合算します。")

    if not article_sheet_url:
        st.warning("サイドバーで「記事スプレッドシートURL」を設定してください。")

    _batch_col1, _batch_col2, _batch_col3 = st.columns([2, 1, 1])
    batch_tab_sel = _batch_col1.selectbox(
        "処理するタブ", ARTICLE_TABS, key="batch_tab_sel",
    )
    batch_db_type = _batch_col2.selectbox(
        "DBタイプ", [DB_TYPE_CLINIC, DB_TYPE_LIFESTYLE], key="batch_db_type",
    )
    dry_run = _batch_col3.checkbox("ドライラン", help="APIを叩かず対象行だけ確認")

    if st.button("🚀 実行開始", type="primary", use_container_width=True, key="run_batch"):
        creds_data = _get_gcp_creds(sheets_creds_file)
        errors = []
        if not creds_data:
            errors.append("Google Sheets 認証情報が未設定です")
        if not article_sheet_url:
            errors.append("記事スプレッドシートURLが未設定です（サイドバーまたはSecrets）")
        if not dry_run:
            if not claude_key:  errors.append("Claude API Key が未設定です")

        if errors:
            for e in errors:
                st.error(e)
        else:
            ws = get_sheet(article_sheet_url, creds_data, tab_name=batch_tab_sel)
            rows = read_input_rows(ws, default_article_type=batch_tab_sel)
            pending = [r for r in rows if not r.get("status")]

            try:
                settings_ws = get_settings_sheet(article_sheet_url, creds_data)
                defaults = read_defaults(settings_ws)
            except Exception:
                defaults = {}

            st.info(f"処理対象: **{len(pending)} 行** / 全 {len(rows)} 行")

            if dry_run:
                for r in pending:
                    atype = r["article_type"]
                    st.write(f"- 行{r['row_index']}: [{atype}] {r['main_kw']}")
            else:
                progress   = st.progress(0)
                status_msg = st.empty()

                for i, row in enumerate(pending):
                    row_num = row["row_index"]
                    kw = row["main_kw"]
                    status_msg.info(f"処理中 ({i+1}/{len(pending)}): {kw}")
                    write_status(ws, row_num, "処理中")

                    try:
                        inputs = build_inputs_from_row(row, defaults)

                        # サイトパーツ読み込み（site_nameが登録済みサイトと一致する場合のみ）
                        _batch_site_parts = ""
                        _batch_site_name = inputs.get("site_name", "")
                        if _batch_site_name and _batch_site_name in site_config_manager.list_sites():
                            _sc = site_config_manager.load_site_config(_batch_site_name)
                            _batch_site_parts = site_config_manager.format_site_parts(_sc.get("components", []))

                        comp   = analyze_competitors(inputs["competitor_urls"], claude_key)
                        if inputs["competitor_urls"]:
                            discovered = discover_clinics_from_competitors(
                                comp["raw_pages"], inputs["clinics"], claude_key
                            )
                        else:
                            discovered = auto_discover_clinics(
                                inputs["main_kw"], inputs["genre"], claude_key, inputs["clinics"]
                            )
                        inputs["clinics"] = inputs["clinics"] + discovered
                        _batch_active_db_url = db_sheet_url if batch_db_type == DB_TYPE_CLINIC else lifestyle_sheet_url
                        _batch_db_cache = clinic_db_manager.build_db_cache([c["name"] for c in inputs["clinics"]], genre=inputs.get("genre", ""), creds_data=creds_data, sheet_url=_batch_active_db_url)
                        clinics   = collect_clinic_info(inputs["clinics"], inputs["genre"], claude_key, inputs.get("article_type", ""), db_cache=_batch_db_cache, db_type=batch_db_type)
                        structure = generate_structure(inputs, comp, clinics, claude_key)
                        output    = generate_body(inputs, structure, clinics, claude_key, comp,
                                                  site_parts=_batch_site_parts)

                        write_output_row(ws, row_num, {
                            "title":     structure["title"],
                            "meta":      structure["meta"],
                            "html":      output["html"],
                            "todo_list": output["todo_list"],
                        })
                        write_status(ws, row_num, "完了")

                    except Exception as e:
                        write_status(ws, row_num, f"エラー: {e}")
                        st.warning(f"行{row_num} ({kw}) でエラー: {e}")

                    progress.progress((i + 1) / len(pending))
                    time.sleep(1)

                status_msg.success(f"✅ {len(pending)} 記事の処理が完了しました")


# ════════════════════════════════════════════════════════
#  Tab2: カスタム作成
# ════════════════════════════════════════════════════════
with _safe_tab(tab_custom):
    st.title("📝 カスタム作成")
    st.caption("CV記事（地域・比較・商標）およびノウハウの単発生成。設定タブのデフォルト追加指示を自動適用します。")

    # ── サイトパーツ選択 ──────────────────────────────────
    _registered_sites = site_config_manager.list_sites()
    _site_options = ["（なし）"] + _registered_sites
    selected_site_for_parts = st.selectbox(
        "サイトパーツを使用する",
        _site_options,
        key="t_site_parts_sel",
        help="登録済みサイトを選ぶと、そのサイトのHTMLパーツを記事生成に使用します。",
    )
    if selected_site_for_parts != "（なし）":
        _preview_cfg = site_config_manager.load_site_config(selected_site_for_parts)
        _active_count = sum(1 for c in _preview_cfg.get("components", []) if c.get("active", True))
        st.caption(f"✅ {selected_site_for_parts}：有効パーツ {_active_count} 件")

    st.divider()

    _t2_type_col, _t2_db_col = st.columns([3, 1])
    article_type = _t2_type_col.radio("記事タイプ", ["地域", "比較", "商標", "ノウハウ"], horizontal=True, key="test_type")
    custom_db_type = _t2_db_col.selectbox("DBタイプ", [DB_TYPE_CLINIC, DB_TYPE_LIFESTYLE], key="custom_db_type")

    single_defaults: dict = {}
    if article_sheet_url:
        _cds = _get_gcp_creds(sheets_creds_file)
        if _cds:
            try:
                _sws = get_settings_sheet(article_sheet_url, _cds)
                single_defaults = read_defaults(_sws)
                st.caption("✅ 設定タブ読み込み済み（記事スプシより）")
            except Exception:
                pass

    st.divider()

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("基本情報")
        site_name = st.text_input("サイト名", key="t_site")
        genre     = st.text_input("ジャンル", key="t_genre", placeholder="クマ取り / AGA治療 / 医療ダイエット")
        main_kw   = st.text_input("メインKW", key="t_main_kw")
        sub_kw    = st.text_input("サブKW（カンマ区切り）", key="t_sub_kw")
        related_kw = st.text_area(
            "関連KW（任意・改行区切り）",
            key="t_related_kw",
            placeholder="シミ取り 大阪 口コミ\nシミ取り 皮膚科 大阪 保険適用\nシミ取り 大阪 一回で",
            help="構成の網羅性チェックに使用。検索面からそのままコピペできます。",
            height=120,
        )
        recommended = st.text_input("最訴求プラン（任意）", key="t_rec",
                                    placeholder="TCB / セマグルチド0.5mg")

    with col_right:
        st.subheader("追加指示")
        default_block_val = single_defaults.get(article_type, "")
        if default_block_val:
            st.caption("デフォルト（設定タブより）")
            st.text_area(
                "", value=default_block_val, height=60,
                disabled=True, key="t_default_preview",
                label_visibility="collapsed",
            )
        else:
            st.caption("デフォルト：未設定（サイドバーで記事スプシを設定すると反映）")

        additional_block = st.text_area(
            "追加指示（任意）",
            height=100, key="t_custom",
            placeholder="例：GLP-1の仕組みを解説するセクションを追加してほしい",
        )

    st.divider()
    st.subheader("含めるセクション")
    selected_topics = _render_topic_checkboxes(article_type, key_prefix="t")

    st.divider()
    st.subheader("掲載クリニック")
    if "test_clinics" not in st.session_state:
        st.session_state.test_clinics = [{"name": "", "domain": ""}]

    c_h = st.columns([3, 3, 1])
    c_h[0].caption("クリニック名")
    c_h[1].caption("ドメイン（例: tcb.net）")

    to_remove = []
    for i, c in enumerate(st.session_state.test_clinics):
        c0, c1, c2 = st.columns([3, 3, 1])
        n = c0.text_input("", value=c["name"],   key=f"tcn_{i}", placeholder="TCB", label_visibility="collapsed")
        d = c1.text_input("", value=c["domain"], key=f"tcd_{i}", placeholder="tcb.net または https://lp.example.com/...", label_visibility="collapsed")
        if c2.button("✕", key=f"trm_{i}") and len(st.session_state.test_clinics) > 1:
            to_remove.append(i)
        st.session_state.test_clinics[i] = {"name": n, "domain": d}
    for idx in reversed(to_remove):
        st.session_state.test_clinics.pop(idx)
    if st.button("＋ クリニックを追加", key="t_add"):
        st.session_state.test_clinics.append({"name": "", "domain": ""})
        st.rerun()

    st.subheader("競合URL")
    competitor_urls = []
    for i in range(5):
        u = st.text_input(
            f"競合URL {i+1}" if i == 0 else "",
            key=f"t_comp_{i}",
            label_visibility="visible" if i == 0 else "collapsed",
        )
        if u.strip():
            competitor_urls.append(u.strip())

    st.divider()
    _out_c1, _out_c2 = st.columns([2, 1])
    output_tab_sel = _out_c1.selectbox(
        "スプシ書き込み先タブ（任意）", ["（書き込まない）"] + ARTICLE_TABS, key="t_out_tab",
    )
    output_row_num = _out_c2.number_input("行番号", min_value=2, value=2, step=1, key="t_row_num")

    st.divider()
    if st.button("🚀 実行", type="primary", use_container_width=True, key="run_test"):
        valid_clinics = [c for c in st.session_state.test_clinics if c["name"] and c["domain"]]
        errs = []
        if not claude_key:  errs.append("Claude API Key 未設定")
        if not main_kw:     errs.append("メインKW を入力してください")
        if not genre:       errs.append("ジャンル を入力してください")
        for e in errs:
            st.error(e)

        if not errs:
            # サイトパーツ構築
            _single_site_parts = ""
            _single_site_config = {}
            if selected_site_for_parts != "（なし）":
                _sc = site_config_manager.load_site_config(selected_site_for_parts)
                _single_site_parts = site_config_manager.format_site_parts(_sc.get("components", []))
                _single_site_config = _sc

            combined_block = "\n".join(filter(None, [default_block_val, additional_block]))
            inputs = {
                "article_type":    article_type,
                "site_name":       site_name,
                "main_kw":         main_kw,
                "sub_kw":          [k.strip() for k in sub_kw.split(",") if k.strip()],
                "genre":           genre,
                "recommended":     recommended,
                "custom_block":    combined_block,
                "related_kw":      related_kw,
                "clinics":         valid_clinics,
                "competitor_urls": competitor_urls,
                "selected_topics": selected_topics,
            }
            with st.status("生成中...", expanded=True) as s:
                try:
                    st.write("🔍 競合分析中...")
                    comp = analyze_competitors(competitor_urls, claude_key)
                    st.write("🤖 クリニック自動探索中...")
                    if competitor_urls:
                        discovered = discover_clinics_from_competitors(
                            comp["raw_pages"], valid_clinics, claude_key
                        )
                    else:
                        discovered = auto_discover_clinics(
                            main_kw, genre, claude_key, valid_clinics
                        )
                    all_clinics = valid_clinics + discovered
                    if discovered:
                        st.write(f"　→ {len(discovered)} 件を自動追加: {', '.join(c['name'] for c in discovered)}")
                    inputs["clinics"] = all_clinics
                    st.write("🏥 クリニック情報収集中...")
                    _t2_db_creds = _get_gcp_creds(sheets_creds_file)
                    _t2_active_db_url = db_sheet_url if custom_db_type == DB_TYPE_CLINIC else lifestyle_sheet_url
                    _t2_db_cache = clinic_db_manager.build_db_cache([c["name"] for c in all_clinics], genre=genre, creds_data=_t2_db_creds, sheet_url=_t2_active_db_url)
                    if _t2_db_cache:
                        st.write(f"　→ DB参照: {len(_t2_db_cache)} 案件（スクレイピングスキップ）")
                    clinics = collect_clinic_info(all_clinics, genre, claude_key, article_type, db_cache=_t2_db_cache, db_type=custom_db_type)
                    st.write("📐 構成生成中...")
                    structure = generate_structure(inputs, comp, clinics, claude_key)
                    st.write("✍️ 本文生成中（Claude）...")
                    output = generate_body(inputs, structure, clinics, claude_key, comp,
                                          site_parts=_single_site_parts)
                    st.session_state["t2_last"] = {
                        "html":           output["html"],
                        "title":          structure["title"],
                        "meta":           structure["meta"],
                        "todo_list":      output["todo_list"],
                        "structure_text": structure["structure_text"],
                        "site_config":    _single_site_config,
                        "site_name":      site_name or (selected_site_for_parts if selected_site_for_parts != "（なし）" else ""),
                        "main_kw":        main_kw,
                        "debug":          output.get("debug"),
                        "clinics":        all_clinics,
                    }
                    s.update(label="✅ 完了", state="complete")
                    _save_output_cache(main_kw, st.session_state["t2_last"])

                    if output_tab_sel != "（書き込まない）" and article_sheet_url:
                        creds_out = _get_gcp_creds(sheets_creds_file)
                        if creds_out:
                            st.write(f"📊 [{output_tab_sel}] タブ 行{output_row_num} に書き込み中...")
                            try:
                                ws_out = get_sheet(article_sheet_url, creds_out, tab_name=output_tab_sel)
                                write_output_row(ws_out, int(output_row_num), {
                                    "title":     structure["title"],
                                    "meta":      structure["meta"],
                                    "html":      output["html"],
                                    "todo_list": output["todo_list"],
                                })
                                st.success(f"[{output_tab_sel}] 行{output_row_num}に書き込みました")
                            except Exception as we:
                                st.warning(f"スプシ書き込みエラー: {we}")

                except Exception as e:
                    s.update(label="❌ エラー", state="error")
                    st.error(str(e))

    # ── 過去の生成結果（リロード対策）────────────────────────────
    _cache_hist = _load_output_cache()
    if _cache_hist:
        with st.expander(f"📂 過去の生成結果（最新 {len(_cache_hist)} 件）", expanded=False):
            _cache_labels = [
                f"{d.get('main_kw', '(不明)')}  —  {d['_cache_file'][:15]}"
                for d in _cache_hist
            ]
            _cache_sel_idx = st.selectbox(
                "読み込む記事を選択", range(len(_cache_labels)),
                format_func=lambda i: _cache_labels[i],
                key="cache_hist_sel",
            )
            if st.button("⬆️ この記事を読み込む", key="cache_hist_load"):
                _loaded = {k: v for k, v in _cache_hist[_cache_sel_idx].items() if k != "_cache_file"}
                st.session_state["t2_last"] = _loaded
                st.rerun()

    # ── 生成結果表示（session_stateから常時表示）────────────────
    _t2_last = st.session_state.get("t2_last")
    if _t2_last:
        st.divider()
        st.markdown(f"**タイトル:** {_t2_last['title']}")
        st.markdown(f"**メタ:** {_t2_last['meta']}")
        with st.expander("構成テキスト（デバッグ用）"):
            st.text(_t2_last["structure_text"])
            if _t2_last.get("debug"):
                st.warning(f"⚠️ {_t2_last['debug']}")
        if _t2_last["todo_list"]:
            st.warning("**[要確認]リスト**\n" + _t2_last["todo_list"])
        st.code(_t2_last["html"], language="html")
        st.download_button(
            "📥 HTMLをダウンロード", _t2_last["html"],
            file_name=f"{_t2_last['main_kw'].replace(' ','_')}.html",
            mime="text/html",
            key="t2_dl",
        )

    # 掲載院一覧（クリニックブロックタブ用）
    if _t2_last and _t2_last.get("clinics"):
        st.divider()
        st.subheader("掲載院一覧（クリニックブロック用コピペ）")
        _clinic_lines = []
        for _i, _c in enumerate(_t2_last["clinics"]):
            _url = _c.get("domain", "[要確認]")
            if _url and not _url.startswith("http") and _url not in ("[要確認]", "unknown", ""):
                _url = f"https://{_url}"
            _clinic_lines.append(f"{_i+1}. {_c['name']}::{_url or '[要確認]'}")
        _clinic_list_text = "\n".join(_clinic_lines)
        st.code(_clinic_list_text, language="text")
        st.download_button(
            "📋 一覧をダウンロード",
            _clinic_list_text,
            file_name="clinic_list.txt",
            mime="text/plain",
            key="t2_clinic_list_dl",
        )

    # ── 画像生成セクション（前回生成した記事に対して実行）────────────
    if _t2_last and _t2_last.get("site_config", {}).get("image_templates"):
        st.divider()
        st.subheader("🖼️ 画像生成")
        st.caption(f"対象記事: {_t2_last['main_kw']}")

        st.caption(f"画像生成モデル（デフォルト）: `{image_generator._IMAGE_MODEL}`")
        _img_model_override = st.text_input(
            "モデルを変更する場合は入力（空欄でデフォルト使用）",
            key="t2_model_override",
            placeholder="例: imagen-3.0-generate-001",
        )

        _img_slug = st.text_input(
            "スラッグ（ファイル名の接頭辞・英数字ハイフンのみ）",
            key="t2_img_slug",
            placeholder="例: aga-treatment-tokyo",
            help="画像ファイル名: スラッグ-英単語.webp",
        )

        if st.button("🖼️ 画像を生成してDriveにアップロード", key="t2_img_gen", type="primary"):
            errs_img = []
            if not gemini_key:
                errs_img.append("Gemini API Key が未設定です（サイドバーから入力してください）")
            if not _img_slug.strip():
                errs_img.append("スラッグを入力してください")
            if not claude_key:
                errs_img.append("Claude API Key が未設定です")
            for e in errs_img:
                st.error(e)

            if not errs_img:
                _creds_img = _get_gcp_creds(sheets_creds_file)
                if not _creds_img:
                    st.error("Google Sheets 認証情報が未設定です（Drive アップロードにも使用）")
                else:
                    with st.status("画像生成中...", expanded=True) as img_status:
                        try:
                            st.write("💡 画像プロンプト生成中（Claude）...")
                            prompts = image_generator.generate_image_prompts(
                                _t2_last["structure_text"],
                                _t2_last["site_config"],
                                claude_key,
                                _img_slug.strip(),
                            )
                            st.write(f"　→ {len(prompts)} 枚分のプロンプトを生成しました")

                            _img_results = []
                            for i, p in enumerate(prompts):
                                st.write(f"🎨 画像生成中 ({i+1}/{len(prompts)}): {p['filename']}...")
                                img_bytes = image_generator.generate_image_bytes(
                                    p["prompt"], gemini_key,
                                    model_override=_img_model_override.strip() or None,
                                )
                                if img_bytes:
                                    drive_url = drive_uploader.upload_image(
                                        img_bytes,
                                        p["filename"],
                                        _t2_last["site_name"] or "default",
                                        _img_slug.strip(),
                                        _creds_img,
                                        _drive_folder_id,
                                    )
                                    _img_results.append({**p, "drive_url": drive_url})
                                    st.write(f"　→ アップロード完了")
                                else:
                                    st.warning(f"　→ {p['filename']} の画像生成に失敗しました")

                            img_status.update(label=f"✅ {len(_img_results)} 枚アップロード完了", state="complete")

                            st.markdown("### アップロード結果")
                            for r in _img_results:
                                st.markdown(
                                    f"**{r['position']}**  \n"
                                    f"ファイル名: `{r['filename']}`  \n"
                                    f"alt: {r['alt']}  \n"
                                    f"[Driveで開く]({r['drive_url']})"
                                )
                                st.divider()

                        except Exception as e:
                            img_status.update(label="❌ エラー", state="error")
                            st.error(str(e))


# ════════════════════════════════════════════════════════
#  Tab3: 品質チェック
# ════════════════════════════════════════════════════════
with _safe_tab(tab_qual):
    st.title("✅ 品質チェック")
    check_type    = st.radio("記事タイプ", ["地域", "比較", "商標", "ノウハウ"], horizontal=True, key="chk_type")
    check_main_kw = st.text_input("メインKW", key="chk_kw")
    check_sub_kw  = st.text_input("サブKW（カンマ区切り）", key="chk_sub")
    html_input    = st.text_area("HTMLを貼り付け", height=300, key="chk_html")

    if st.button("チェック実行", type="primary", key="run_check"):
        if not claude_key:
            st.error("Claude API Key が未設定です")
        elif not html_input.strip():
            st.error("HTMLを貼り付けてください")
        else:
            with st.spinner("チェック中..."):
                result = quality_check(
                    html_input, check_type, check_main_kw,
                    [k.strip() for k in check_sub_kw.split(",") if k.strip()],
                    claude_key,
                )
                st.markdown(result)


# ════════════════════════════════════════════════════════
#  Tab4: サイト設定
# ════════════════════════════════════════════════════════
with _safe_tab(tab_settings):
    st.title("⚙️ サイト設定")
    st.caption("サイト別のカラー・トンマナ・画像テンプレート・HTMLパーツを登録します。")

    sites_list = site_config_manager.list_sites()
    col_left4, col_right4 = st.columns([1, 2])

    with col_left4:
        st.subheader("サイト一覧")
        _site_opts = ["-- 新規作成 --"] + sites_list
        _selected4 = st.selectbox("サイトを選択", _site_opts, key="cfg_site_sel")

        if _selected4 == "-- 新規作成 --":
            _new_name = st.text_input("新規サイト名（半角英数字推奨）", placeholder="example-com", key="cfg_new_name")
            _current_site4 = _new_name.strip() if _new_name.strip() else None
            _config4 = site_config_manager.get_default_site_config()
        else:
            _current_site4 = _selected4
            _config4 = site_config_manager.load_site_config(_current_site4)
            st.markdown("---")
            if st.button("🗑️ このサイトを削除", key="cfg_del"):
                site_config_manager.delete_site_config(_current_site4)
                st.success(f"「{_current_site4}」を削除しました")
                st.rerun()

    with col_right4:
        if not _current_site4:
            st.info("左側でサイトを選択するか、新規サイト名を入力してください。")
        else:
            st.subheader(f"「{_current_site4}」の設定")

            with st.form(f"site_form_{_current_site4}"):

                # ── 1. カラー設定 ────────────────────────────────────
                st.markdown("### 🎨 1. カラー設定")
                st.caption("画像プロンプト内で使用する色。デフォルトはティール系カラー。")
                _colors4 = _config4.get("design_rules", {}).get("colors", {})
                _cc1, _cc2, _cc3, _cc4 = st.columns(4)
                with _cc1:
                    _color_main  = st.color_picker("メイン",       value=_colors4.get("main",          "#47c1d3"), key=f"color_main_{_current_site4}")
                    _color_text  = st.color_picker("テキスト",     value=_colors4.get("text",          "#333333"), key=f"color_text_{_current_site4}")
                with _cc2:
                    _color_acc_r = st.color_picker("アクセント赤", value=_colors4.get("accent_red",    "#fe766b"), key=f"color_acc_r_{_current_site4}")
                    _color_bg_w  = st.color_picker("背景白",       value=_colors4.get("bg_white",      "#FFFFFF"), key=f"color_bg_w_{_current_site4}")
                with _cc3:
                    _color_acc_y = st.color_picker("アクセント黄", value=_colors4.get("accent_yellow", "#ffd711"), key=f"color_acc_y_{_current_site4}")
                    _color_bg_g  = st.color_picker("背景グレー",   value=_colors4.get("bg_gray",       "#eeeeee"), key=f"color_bg_g_{_current_site4}")
                with _cc4:
                    _color_acc_o = st.color_picker("アクセント橙", value=_colors4.get("accent_orange", "#fd9b23"), key=f"color_acc_o_{_current_site4}")
                st.markdown("---")

                # ── 2. トンマナ ──────────────────────────────────────
                st.markdown("### 📝 2. トンマナ")
                _tone4 = st.text_input(
                    "画像トンマナ（AIへの指示）",
                    value=_config4.get("design_rules", {}).get("tone", ""),
                    placeholder="医療的でクリーン、ビジネスライク、など",
                    key=f"tone_{_current_site4}",
                )
                st.markdown("---")

                # ── 3. 画像テンプレート管理 ──────────────────────────
                st.markdown("### 🖼️ 3. 画像テンプレート管理")
                st.caption("AIに渡す画像生成プロンプトのテンプレートを管理します。")
                _existing_tmpls = _config4.get("image_templates", [])
                _updated_tmpls = []
                for _ti, _tmpl in enumerate(_existing_tmpls):
                    with st.expander(f"テンプレート {_ti+1}: {_tmpl.get('name', '(無名)')}", expanded=False):
                        _t_name  = st.text_input("テンプレート名",  value=_tmpl.get("name", ""),       key=f"tname_{_current_site4}_{_ti}")
                        _t_scene = st.text_input("使用シーン説明",  value=_tmpl.get("usage_scene", ""), key=f"tscene_{_current_site4}_{_ti}")
                        _t_base  = st.text_area("ベースプロンプト", value=_tmpl.get("base_prompt", ""), key=f"tbase_{_current_site4}_{_ti}", height=200)
                        _t_keep  = st.checkbox("このテンプレートを保持", value=True,                   key=f"tkeep_{_current_site4}_{_ti}")
                        if _t_keep:
                            _updated_tmpls.append({"name": _t_name, "usage_scene": _t_scene, "base_prompt": _t_base})

                st.markdown("**＋ 新規テンプレートを追加**（名前を入力して保存するだけでOK）")
                _new_tname  = st.text_input("新テンプレート名", key=f"new_tname_{_current_site4}",  placeholder="カスタム型名")
                _new_tscene = st.text_input("新使用シーン",     key=f"new_tscene_{_current_site4}", placeholder="使用するシーンの説明")
                _default_tbase = st.session_state.get("t4_generated_tmpl", "")
                _new_tbase  = st.text_area("新ベースプロンプト", key=f"new_tbase_{_current_site4}", height=200, value=_default_tbase)
                if _new_tname.strip():
                    _updated_tmpls.append({"name": _new_tname.strip(), "usage_scene": _new_tscene, "base_prompt": _new_tbase})
                st.markdown("---")

                # ── 4. HTMLパーツ ────────────────────────────────────
                st.markdown("### 🧩 4. HTMLパーツ")
                st.caption("パーツ置き場のHTMLファイルをアップすると自動でパーツ一覧を取り込めます。")
                _parts_upload = st.file_uploader(
                    "パーツ置き場HTML（.html / .htm）",
                    type=["html", "htm"],
                    key=f"parts_html_upload_{_current_site4}",
                )
                if _parts_upload is not None:
                    _parts_upload.seek(0)
                    _parts_html_bytes = _parts_upload.read()
                    _parts_html_str = _parts_html_bytes.decode("utf-8", errors="replace")
                    _parsed_components = site_config_manager.parse_parts_page(_parts_html_str)
                    st.caption(f"📋 {len(_parsed_components)} 件のパーツを検出しました")
                    with st.expander("検出内容を確認する"):
                        for _pc in _parsed_components:
                            st.markdown(f"- **{_pc['name']}**")
                    _import_mode = st.radio(
                        "インポート方式",
                        ["上書き（既存パーツをすべて置き換え）", "追記（既存パーツに追加）"],
                        key=f"parts_import_mode_{_current_site4}",
                        horizontal=True,
                    )
                    if st.button("✅ このパーツ一覧をインポートする", key=f"parts_import_btn_{_current_site4}", type="primary"):
                        _cfg_now = site_config_manager.load_site_config(_current_site4)
                        if "追記" in _import_mode:
                            _existing_names = {c["name"] for c in _cfg_now.get("components", [])}
                            _merged = _cfg_now.get("components", []) + [c for c in _parsed_components if c["name"] not in _existing_names]
                            _cfg_now["components"] = _merged
                        else:
                            _cfg_now["components"] = _parsed_components
                        if site_config_manager.save_site_config(_current_site4, _cfg_now):
                            st.success(f"{len(_parsed_components)} 件をインポートしました。ページをリロードして確認してください。")
                            st.rerun()
                        else:
                            st.error("保存に失敗しました。")
                st.markdown("---")
                st.caption("各パーツの {{変数名}} は記事生成時にAIが実際の内容に置き換えます。有効チェックを外すと使用されません。")
                _existing_comps = _config4.get("components", [])
                _updated_comps = []
                for _ci, _comp in enumerate(_existing_comps):
                    _is_active = _comp.get("active", True)
                    _clabel = f"{'✅' if _is_active else '❌'} {_comp.get('name', f'パーツ{_ci+1}')}"
                    with st.expander(_clabel, expanded=False):
                        _c_active  = st.checkbox("このサイトで有効にする", value=_is_active,               key=f"comp_active_{_current_site4}_{_ci}")
                        _c_name    = st.text_input("パーツ名",             value=_comp.get("name", ""),    key=f"comp_name_{_current_site4}_{_ci}")
                        _c_pattern = st.text_area("HTMLパターン",          value=_comp.get("pattern", ""), key=f"comp_pattern_{_current_site4}_{_ci}", height=120)
                        _comp_keep = st.checkbox("このパーツを保持",        value=True,                    key=f"comp_keep_{_current_site4}_{_ci}")
                        if _comp_keep:
                            _updated_comps.append({"name": _c_name, "pattern": _c_pattern, "active": _c_active})

                st.markdown("**＋ 新規パーツを追加**")
                _new_comp_name    = st.text_input("新パーツ名",           key=f"new_comp_name_{_current_site4}",    placeholder="例: normalBox")
                _new_comp_pattern = st.text_area("新パーツ HTMLパターン", key=f"new_comp_pattern_{_current_site4}", height=100,
                                                 placeholder='<div class="normalBox">{{content}}</div>')
                if _new_comp_name.strip():
                    _updated_comps.append({"name": _new_comp_name.strip(), "pattern": _new_comp_pattern, "active": True})

                _submitted4 = st.form_submit_button("💾 設定を保存する", type="primary")

            if _submitted4:
                _new_config4 = {
                    "design_rules": {
                        "tone": _tone4,
                        "colors": {
                            "main":          _color_main,
                            "accent_red":    _color_acc_r,
                            "accent_yellow": _color_acc_y,
                            "accent_orange": _color_acc_o,
                            "bg_white":      _color_bg_w,
                            "bg_gray":       _color_bg_g,
                            "text":          _color_text,
                        },
                    },
                    "image_templates": _updated_tmpls,
                    "components": _updated_comps,
                    "clinic_block_templates": _config4.get("clinic_block_templates", []),  # 既存値を保持
                }
                if site_config_manager.save_site_config(_current_site4, _new_config4):
                    st.session_state.pop("t4_generated_tmpl", None)
                    st.success(f"「{_current_site4}」の設定を保存しました。")
                    st.rerun()
                else:
                    st.error("保存に失敗しました。")

            # ── 画像からプロンプト自動生成 ──────────────────────────
            st.markdown("---")
            st.markdown("### 📷 画像からプロンプト自動生成")
            st.caption("型となる画像をアップすると構造を解析してプロンプトを生成します。生成後、上の「新ベースプロンプト」に貼り付けて保存してください。")

            _t4_img_upload = st.file_uploader(
                "画像をアップ（jpg / png / webp）",
                type=["jpg", "jpeg", "png", "webp"],
                key=f"t4_img_upload_{_current_site4}",
            )
            if _t4_img_upload is not None:
                _t4_col_img, _t4_col_btn = st.columns([2, 1])
                with _t4_col_img:
                    st.image(_t4_img_upload, use_container_width=True)
                with _t4_col_btn:
                    st.caption(f"解析モデル: `{image_generator._VISION_MODEL}`")
                    if st.button("🔍 プロンプト自動生成", key=f"btn_gen_tmpl_{_current_site4}", type="primary"):
                        if not gemini_key:
                            st.error("Gemini API Key が未設定です（サイドバーから入力してください）")
                        else:
                            with st.spinner("画像を解析中..."):
                                try:
                                    _t4_img_upload.seek(0)
                                    _t4_mime = _t4_img_upload.type or "image/png"
                                    _t4_img_bytes = _t4_img_upload.read()
                                    _t4_generated = image_generator.generate_template_from_image(
                                        _t4_img_bytes, _t4_mime, _config4, gemini_key
                                    )
                                    st.session_state["t4_generated_tmpl"] = _t4_generated
                                    st.success("生成完了！下のテキストをコピーして「新ベースプロンプト」に貼り付け、保存してください。")
                                except Exception as _t4_e:
                                    st.error(f"生成エラー: {_t4_e}")

            if st.session_state.get("t4_generated_tmpl"):
                st.text_area(
                    "生成されたプロンプト（コピーして上の「新ベースプロンプト」に貼り付け）",
                    value=st.session_state["t4_generated_tmpl"],
                    height=300,
                    key="t4_gen_result_display",
                )

            # ── 画像プレビュー生成 ───────────────────────────────────
            st.markdown("---")
            st.markdown("### 🎨 画像プレビュー生成")
            st.caption("テンプレートのプロンプトで実際の画像をプレビューできます。{{変数}} は実際の値に書き換えてから生成してください。")

            _preview_config = site_config_manager.load_site_config(_current_site4)
            _preview_tmpls = _preview_config.get("image_templates", [])
            if not _preview_tmpls:
                st.info("テンプレートがまだ登録されていません。上の設定から追加・保存してください。")
            else:
                _preview_names = [t.get("name", f"テンプレート{i+1}") for i, t in enumerate(_preview_tmpls)]
                _preview_sel = st.selectbox(
                    "テンプレートを選択",
                    range(len(_preview_names)),
                    format_func=lambda i: _preview_names[i],
                    key=f"preview_tmpl_sel_{_current_site4}",
                )
                _preview_prompt = st.text_area(
                    "プロンプト（変数を実際の値に書き換えてから生成）",
                    value=_preview_tmpls[_preview_sel].get("base_prompt", ""),
                    height=300,
                    key=f"preview_prompt_{_current_site4}_{_preview_sel}",
                )
                _col_prev_btn, _col_prev_info = st.columns([1, 3])
                with _col_prev_btn:
                    _run_preview = st.button("🎨 プレビュー生成", key=f"btn_preview_{_current_site4}", type="primary")
                with _col_prev_info:
                    st.caption(f"生成モデル: `{image_generator._IMAGE_MODEL}`")

                if _run_preview:
                    if not gemini_key:
                        st.error("Gemini API Key が未設定です（サイドバーから入力してください）")
                    elif not _preview_prompt.strip():
                        st.error("プロンプトを入力してください")
                    else:
                        with st.spinner("画像生成中..."):
                            try:
                                _prev_bytes = image_generator.generate_image_preview(_preview_prompt, gemini_key)
                                if _prev_bytes:
                                    st.image(_prev_bytes, caption="生成プレビュー", use_container_width=True)
                                else:
                                    st.error("画像データが取得できませんでした")
                            except Exception as _prev_e:
                                st.error(f"生成エラー: {_prev_e}")

            # ── クリニックブロックテンプレート管理 ─────────────────────
            st.markdown("---")
            st.markdown("### 🏥 6. クリニックブロックテンプレート")
            st.caption("おすすめクリニック紹介ブロックの構成・形式をテンプレートとして登録します。")

            _existing_cb_tmpls = _config4.get("clinic_block_templates", [])

            with st.form(f"cb_tmpl_form_{_current_site4}"):
                _updated_cb_tmpls = []

                for _cbi, _cbt in enumerate(_existing_cb_tmpls):
                    with st.expander(f"テンプレート {_cbi+1}: {_cbt.get('name', '(無名)')}", expanded=False):
                        _cbt_name = st.text_input("テンプレート名", value=_cbt.get("name", ""), key=f"cbt_name_{_current_site4}_{_cbi}")
                        _cbt_heading = st.selectbox(
                            "見出しタイプ",
                            options=list(clinic_block_writer.HEADING_TYPE_OPTIONS.keys()),
                            format_func=lambda x: clinic_block_writer.HEADING_TYPE_OPTIONS[x],
                            index=list(clinic_block_writer.HEADING_TYPE_OPTIONS.keys()).index(_cbt.get("heading_type", 1)),
                            key=f"cbt_heading_{_current_site4}_{_cbi}",
                        )

                        st.caption("コンポーネント順序（数字＝表示順、0＝非表示）")
                        _cbt_existing_order = _cbt.get("component_order", [])
                        _cbt_comp_nums = {}
                        _cbt_cols = st.columns(4)
                        for _cbi2, _ck in enumerate(clinic_block_writer.ALL_COMPONENTS):
                            _col = _cbt_cols[_cbi2 % 4]
                            _default_order = (_cbt_existing_order.index(_ck) + 1) if _ck in _cbt_existing_order else 0
                            _cbt_comp_nums[_ck] = _col.number_input(
                                clinic_block_writer.COMPONENT_LABELS[_ck],
                                min_value=0, max_value=20, value=_default_order,
                                key=f"cbt_comp_{_current_site4}_{_cbi}_{_ck}",
                            )
                        _cbt_order = [k for k, v in sorted(_cbt_comp_nums.items(), key=lambda x: x[1]) if v > 0]

                        st.caption("基本情報テーブルの項目")
                        _cbt_existing_bi = _cbt.get("basic_info_fields", [])
                        _cbt_bi_fields = []
                        _cbt_bi_cols = st.columns(4)
                        for _bfi, _bfk in enumerate(clinic_block_writer.ALL_BASIC_INFO_FIELDS):
                            _bc = _cbt_bi_cols[_bfi % 4]
                            if _bc.checkbox(
                                clinic_block_writer.BASIC_INFO_FIELD_LABELS[_bfk],
                                value=_bfk in _cbt_existing_bi,
                                key=f"cbt_bi_{_current_site4}_{_cbi}_{_bfk}",
                            ):
                                _cbt_bi_fields.append(_bfk)

                        st.caption("上位3院のリンク設置箇所")
                        _cbt_existing_links = _cbt.get("top3_link_placements", [])
                        _cbt_links = []
                        for _lk, _ll in [("heading", "見出しクリニック名"), ("spec_image", "スペック画像"), ("cta_button", "CTAボタン")]:
                            if st.checkbox(_ll, value=_lk in _cbt_existing_links, key=f"cbt_link_{_current_site4}_{_cbi}_{_lk}"):
                                _cbt_links.append(_lk)

                        st.caption("料金テーブルHTMLテンプレート")
                        _cbt_existing_pts = _cbt.get("price_table_templates", [])
                        _cbt_pts = []
                        for _pti, _pt in enumerate(_cbt_existing_pts):
                            _pt_name = st.text_input("テンプレート名", value=_pt.get("name", ""), key=f"cbt_pt_name_{_current_site4}_{_cbi}_{_pti}")
                            _pt_html = st.text_area("HTML", value=_pt.get("html", ""), height=150, key=f"cbt_pt_html_{_current_site4}_{_cbi}_{_pti}")
                            _pt_keep = st.checkbox("保持", value=True, key=f"cbt_pt_keep_{_current_site4}_{_cbi}_{_pti}")
                            if _pt_keep and _pt_name.strip():
                                _cbt_pts.append({"name": _pt_name.strip(), "html": _pt_html})
                        _new_pt_name = st.text_input("＋ 料金テーブル名", key=f"cbt_pt_new_name_{_current_site4}_{_cbi}", placeholder="GLP-1用量別タブ")
                        _new_pt_html = st.text_area("＋ HTML", key=f"cbt_pt_new_html_{_current_site4}_{_cbi}", height=150, placeholder="<table>{{plan_name}} {{price}}</table>")
                        if _new_pt_name.strip():
                            _cbt_pts.append({"name": _new_pt_name.strip(), "html": _new_pt_html})

                        _cbt_keep = st.checkbox("このテンプレートを保持", value=True, key=f"cbt_keep_{_current_site4}_{_cbi}")
                        if _cbt_keep:
                            _updated_cb_tmpls.append({
                                "name": _cbt_name,
                                "heading_type": _cbt_heading,
                                "component_order": _cbt_order,
                                "basic_info_fields": _cbt_bi_fields,
                                "top3_link_placements": _cbt_links,
                                "price_table_templates": _cbt_pts,
                            })

                st.markdown("**＋ 新規クリニックブロックテンプレート**")
                _new_cbt_name = st.text_input("テンプレート名", key=f"new_cbt_name_{_current_site4}", placeholder="地域記事クリニックブロック")
                _new_cbt_heading = st.selectbox(
                    "見出しタイプ",
                    options=list(clinic_block_writer.HEADING_TYPE_OPTIONS.keys()),
                    format_func=lambda x: clinic_block_writer.HEADING_TYPE_OPTIONS[x],
                    key=f"new_cbt_heading_{_current_site4}",
                )
                st.caption("コンポーネント順序（数字＝表示順、0＝非表示）")
                _new_cbt_comp_nums = {}
                _new_cbt_cols = st.columns(4)
                for _nci, _ck in enumerate(clinic_block_writer.ALL_COMPONENTS):
                    _col = _new_cbt_cols[_nci % 4]
                    _new_cbt_comp_nums[_ck] = _col.number_input(
                        clinic_block_writer.COMPONENT_LABELS[_ck],
                        min_value=0, max_value=20, value=0,
                        key=f"new_cbt_comp_{_current_site4}_{_ck}",
                    )
                _new_cbt_order = [k for k, v in sorted(_new_cbt_comp_nums.items(), key=lambda x: x[1]) if v > 0]

                if _new_cbt_name.strip():
                    _updated_cb_tmpls.append({
                        "name": _new_cbt_name.strip(),
                        "heading_type": _new_cbt_heading,
                        "component_order": _new_cbt_order,
                        "basic_info_fields": [],
                        "top3_link_placements": [],
                        "price_table_templates": [],
                    })

                _cb_submitted = st.form_submit_button("💾 クリニックブロックテンプレートを保存", type="primary")

            if _cb_submitted:
                _cb_save_config = site_config_manager.load_site_config(_current_site4)
                _cb_save_config["clinic_block_templates"] = _updated_cb_tmpls
                if site_config_manager.save_site_config(_current_site4, _cb_save_config):
                    st.success("クリニックブロックテンプレートを保存しました。")
                    st.rerun()
                else:
                    st.error("保存に失敗しました。")


# ════════════════════════════════════════════════════════
#  Tab5: ランキングブロック
# ════════════════════════════════════════════════════════
with _safe_tab(tab_rank):
    st.title("🏥 ランキングブロック")
    st.caption("おすすめ紹介ブロックのHTMLを案件ごとに生成します。「カスタム記事作成」タブの「掲載院一覧」をコピペして使ってください。")

    _cb_sites = site_config_manager.list_sites()
    _cb_site_opts = ["（なし）"] + _cb_sites
    _cb_sel_site = st.selectbox("サイトを選択（テンプレート読込）", _cb_site_opts, key="cb_site_sel")

    _cb_site_cfg = {}
    _cb_templates = []
    _cb_template_names = []
    if _cb_sel_site != "（なし）":
        _cb_site_cfg = site_config_manager.load_site_config(_cb_sel_site)
        _cb_templates = _cb_site_cfg.get("clinic_block_templates", [])
        _cb_template_names = [t.get("name", f"テンプレート{i+1}") for i, t in enumerate(_cb_templates)]

    _cb_sel_tmpl = None
    if _cb_templates:
        _cb_tmpl_idx = st.selectbox(
            "ブロックテンプレートを選択",
            range(len(_cb_template_names)),
            format_func=lambda i: _cb_template_names[i],
            key="cb_tmpl_idx",
        )
        _cb_sel_tmpl = _cb_templates[_cb_tmpl_idx]
    else:
        st.info("サイトにクリニックブロックテンプレートが登録されていません。先にサイト設定タブで登録してください。")

    st.divider()

    _cb_col1, _cb_col2, _cb_col3 = st.columns([2, 2, 1])
    with _cb_col1:
        _cb_main_kw = st.text_input("メインKW", key="cb_main_kw")
        _cb_sub_kw = st.text_input("サブKW（カンマ区切り）", key="cb_sub_kw")
    with _cb_col3:
        _cb_db_type = st.selectbox("DBタイプ", [DB_TYPE_CLINIC, DB_TYPE_LIFESTYLE], key="cb_db_type")
    with _cb_col2:
        _cb_criteria = st.text_area(
            "選び方コンテンツ（全文ペースト）",
            height=120, key="cb_criteria",
            placeholder="記事内の「○○の選び方」セクションの文章をそのまま貼り付けてください。",
        )

    st.divider()
    st.subheader("掲載院一覧")
    _cb_clinic_paste = st.text_area(
        "Tab2の「掲載院一覧」をペースト",
        height=150, key="cb_clinic_paste",
        placeholder="1. TCB東京中央美容外科 大阪院::https://tcb.net/osaka\n2. 湘南美容クリニック 梅田院::https://s-b-c.net/\n3. 品川スキンクリニック 大阪院::[要確認]",
    )

    if st.button("📋 院一覧をパース", key="cb_parse_btn"):
        if _cb_clinic_paste.strip():
            st.session_state["cb_clinics"] = clinic_block_writer.parse_clinic_list(_cb_clinic_paste)
            st.rerun()
        else:
            st.warning("院一覧を入力してください")

    _cb_clinics = st.session_state.get("cb_clinics", [])

    if _cb_clinics:
        st.caption(f"パース結果: {len(_cb_clinics)} 院")
        st.divider()
        st.subheader("各院の入力情報")

        for _cbc in _cb_clinics:
            _r = _cbc["rank"]
            _is_top3 = _r <= 3
            with st.expander(f"{'⭐' if _is_top3 else ''} {_r}位: {_cbc['name']}", expanded=_is_top3):
                _cbc_url = st.text_input(
                    "公式URL",
                    value=_cbc.get("url", ""),
                    key=f"cb_url_{_r}",
                    placeholder="https://example.com",
                )
                st.session_state[f"cb_url_{_r}"] = _cbc_url

                if _is_top3:
                    _cbc_link = st.text_input(
                        "リンクURL（LP等）",
                        value=st.session_state.get(f"cb_link_{_r}", _cbc_url),
                        key=f"cb_link_{_r}",
                        placeholder="CTAボタン・見出しリンクのリンク先URL",
                    )
                    _cbc_lp = st.text_area(
                        "LP掲載プラン",
                        value=st.session_state.get(f"cb_lp_{_r}", ""),
                        key=f"cb_lp_{_r}",
                        height=80,
                        placeholder="例: セマグルチド0.5mg 週1回 9,800円（税込）",
                    )
                else:
                    _cbc_link = ""
                    _cbc_lp = ""

                _cbc_price = st.text_area(
                    "料金データ（フリーテキスト）",
                    value=st.session_state.get(f"cb_price_{_r}", ""),
                    key=f"cb_price_{_r}",
                    height=100,
                    placeholder="例: 0.5mg週1回 / 9,800円（税込）/ 初回限定\n1mg週1回 / 14,800円（税込）/ -",
                )
                _cbc_notes = st.text_area(
                    "追加メモ・補足情報（任意）",
                    value=st.session_state.get(f"cb_notes_{_r}", ""),
                    key=f"cb_notes_{_r}",
                    height=80,
                    placeholder="公式HPに載っていない特記事項など",
                )

        st.divider()
        _cb_gen_all = st.button("🚀 全院のブロックを生成", type="primary", use_container_width=True, key="cb_gen_all")

        if _cb_gen_all:
            errs_cb = []
            if not claude_key:
                errs_cb.append("Claude API Key が未設定です")
            if not _cb_main_kw:
                errs_cb.append("メインKWを入力してください")
            if not _cb_sel_tmpl:
                errs_cb.append("テンプレートを選択してください")
            for _e in errs_cb:
                st.error(_e)

            if not errs_cb:
                _cb_sub_kw_list = [k.strip() for k in _cb_sub_kw.split(",") if k.strip()]
                _cb_site_parts = ""
                if _cb_sel_site != "（なし）":
                    _cb_site_parts = site_config_manager.format_site_parts(_cb_site_cfg.get("components", []))

                _cb_results = []
                with st.status("クリニックブロック生成中...", expanded=True) as _cb_status:
                    for _cbc in _cb_clinics:
                        _r = _cbc["rank"]
                        _clinic_url = st.session_state.get(f"cb_url_{_r}", _cbc.get("url", ""))
                        _link_url = st.session_state.get(f"cb_link_{_r}", _clinic_url)
                        _lp_plan = st.session_state.get(f"cb_lp_{_r}", "")
                        _price_data = st.session_state.get(f"cb_price_{_r}", "")
                        _extra_notes = st.session_state.get(f"cb_notes_{_r}", "")

                        st.write(f"🔍 {_r}位: {_cbc['name']} の情報を収集中...")
                        try:
                            _t5_db_creds = _get_gcp_creds(sheets_creds_file)
                            _t5_active_db_url = db_sheet_url if _cb_db_type == DB_TYPE_CLINIC else lifestyle_sheet_url
                            _t5_db_cache = clinic_db_manager.build_db_cache([_cbc["name"]], genre="", creds_data=_t5_db_creds, sheet_url=_t5_active_db_url)
                            if _t5_db_cache:
                                st.write(f"　→ DB参照")
                            _scraped = collect_clinic_info(
                                [{"name": _cbc["name"], "domain": _clinic_url or _cbc["name"]}],
                                "", claude_key, db_cache=_t5_db_cache, db_type=_cb_db_type,
                            )
                            _scraped_text = _scraped.get(_cbc["name"], "（取得失敗）")
                        except Exception:
                            _scraped_text = "（取得失敗）"

                        st.write(f"✍️ {_r}位: {_cbc['name']} のブロックを生成中...")
                        try:
                            _html = clinic_block_writer.generate_clinic_block(
                                name=_cbc["name"],
                                rank=_r,
                                scraped_info=_scraped_text,
                                price_data=_price_data,
                                extra_notes=_extra_notes,
                                link_url=_link_url,
                                lp_plan=_lp_plan,
                                template=_cb_sel_tmpl,
                                main_kw=_cb_main_kw,
                                sub_kw=_cb_sub_kw_list,
                                criteria_text=_cb_criteria,
                                claude_api_key=claude_key,
                                site_parts=_cb_site_parts,
                            )
                            _cb_results.append({"rank": _r, "name": _cbc["name"], "html": _html})
                        except Exception as _e:
                            st.warning(f"{_r}位 ({_cbc['name']}) でエラー: {_e}")

                    _cb_status.update(label=f"✅ {len(_cb_results)} 院分のブロックを生成しました", state="complete")

                st.session_state["cb_results"] = _cb_results

    _cb_results = st.session_state.get("cb_results", [])
    if _cb_results:
        st.divider()
        st.subheader("生成結果")
        for _res in _cb_results:
            st.markdown(f"**{_res['rank']}位: {_res['name']}**")
            st.code(_res["html"], language="html")
            st.download_button(
                f"📥 {_res['rank']}位HTMLをダウンロード",
                _res["html"],
                file_name=f"clinic_block_{_res['rank']}_{_res['name'].replace(' ', '_')}.html",
                mime="text/html",
                key=f"cb_dl_{_res['rank']}",
            )
            st.divider()

        _all_html = "\n\n".join(f"<!-- {r['rank']}位: {r['name']} -->\n{r['html']}" for r in _cb_results)
        st.download_button(
            "📥 全院まとめてダウンロード",
            _all_html,
            file_name="clinic_blocks_all.html",
            mime="text/html",
            key="cb_dl_all",
        )


# ════════════════════════════════════════════════════════
#  Tab6: 商品データベース
# ════════════════════════════════════════════════════════
with _safe_tab(tab_cases):
    st.title("🗄️ 商品データベース")
    st.caption("量産ジャンルで使う案件を事前収集して蓄積します。ジャンルごとにタブ分けされ、DB登録済みの案件は記事生成時にスクレイピングをスキップします。")

    _db_type_sel = st.radio("DBタイプ", [DB_TYPE_CLINIC, DB_TYPE_LIFESTYLE], horizontal=True, key="db_tab_type")
    _active_db_url = db_sheet_url if _db_type_sel == DB_TYPE_CLINIC else lifestyle_sheet_url

    _db_creds = _get_gcp_creds(sheets_creds_file)
    if not _active_db_url:
        _url_label = "クリニックDB" if _db_type_sel == DB_TYPE_CLINIC else "ライフスタイルDB"
        st.warning(f"サイドバーで「{_url_label} スプレッドシートURL」を入力してください。未設定の場合はローカルJSONに保存されます（Streamlit Cloud再起動で消えます）。")
    elif not _db_creds:
        st.warning("Google Sheets 認証が未設定です。ローカルJSONにフォールバックします。")
    else:
        st.caption("✅ Google Sheets DB に接続中")

    # ── 新規追加フォーム ──────────────────────────────────
    st.subheader("＋ 新規追加")
    st.caption("複数ジャンル指定時はサイトを1回クロールして、ジャンルごとに個別抽出します。")
    with st.form("db_add_form"):
        _db_fa, _db_fb, _db_fc = st.columns([2, 2, 2])
        _db_new_name   = _db_fa.text_input("案件名（クリニック名・商品名等）", placeholder="TCB東京中央美容外科")
        _db_new_domain = _db_fb.text_input("URL（ドメイン or パス指定）", placeholder="tcb.net  または  tcb.net/osaka/umeda/")
        _db_new_genres = _db_fc.text_input("ジャンル（カンマ区切り）", placeholder="美容外科, 二重")
        _db_btn_a, _db_btn_b = st.columns(2)
        _db_add_now  = _db_btn_a.form_submit_button("追加してサイトをクロール取得", type="primary")
        _db_add_only = _db_btn_b.form_submit_button("登録のみ（後で取得）")

    def _db_parse_genres(raw: str) -> list:
        return [g.strip() for g in raw.split(",") if g.strip()]

    if _db_add_now or _db_add_only:
        _errs_db = []
        if not _db_new_name.strip():
            _errs_db.append("案件名を入力してください")
        if not _db_new_domain.strip():
            _errs_db.append("URL / ドメインを入力してください")
        if _db_add_now and not claude_key:
            _errs_db.append("Claude API Key が未設定です")
        for _e in _errs_db:
            st.error(_e)

        if not _errs_db:
            _g_list = _db_parse_genres(_db_new_genres) or ["未分類"]
            _name_new = _db_new_name.strip()
            _domain_new = _db_new_domain.strip()

            if _db_add_only:
                for _g in _g_list:
                    clinic_db_manager.upsert_clinic(_name_new, _domain_new, _g, "", creds_data=_db_creds, sheet_url=_active_db_url)
                st.success(f"「{_name_new}」を {', '.join(_g_list)} に登録しました。後で「再クロール」してください。")
                st.rerun()
            else:
                with st.status(f"{_name_new} のサイトをクロール中（最大20ページ）...", expanded=True) as _add_status:
                    try:
                        _start_url = _domain_new if _domain_new.startswith("http") else f"https://{_domain_new}"
                        st.write("🔍 クロール中...")
                        _content_new = crawl_site(_start_url, _g_list[0], max_pages=20)
                        for _g in _g_list:
                            st.write(f"🤖 「{_g}」向けに情報抽出中...")
                            _info_g = extract_clinic_info_from_content(_content_new, _name_new, _g, claude_key, db_type=_db_type_sel)
                            clinic_db_manager.upsert_clinic(_name_new, _domain_new, _g, _info_g, creds_data=_db_creds, sheet_url=_active_db_url)
                        _add_status.update(label=f"✅ 「{_name_new}」を {len(_g_list)} ジャンルに追加しました", state="complete")
                        st.rerun()
                    except Exception as _e_new:
                        _add_status.update(label="❌ エラー", state="error")
                        st.error(f"取得エラー: {_e_new}")

    # ── ジャンル別タブ表示 ──────────────────────────────────
    st.divider()
    _db_nested = clinic_db_manager.load_db(creds_data=_db_creds, sheet_url=_active_db_url)
    _all_genre_names = list(_db_nested.keys())

    if not _all_genre_names:
        st.info("まだ登録されていません。上のフォームから追加してください。")
    else:
        _genre_ui_tabs = st.tabs(_all_genre_names)
        for _g_tab_ui, _g_name in zip(_genre_ui_tabs, _all_genre_names):
            with _g_tab_ui:
                _g_entries = _db_nested.get(_g_name, {})
                _g_c1, _g_c2 = st.columns([4, 1])
                with _g_c2:
                    st.metric("登録件数", len(_g_entries))

                # ── 一括再取得 ────────────────────────────────
                if _g_entries:
                    if st.button(f"🔄 「{_g_name}」を一括再取得（{len(_g_entries)} 件・クロール）", key=f"db_batch_{_g_name}"):
                        if not claude_key:
                            st.error("Claude API Key が未設定です")
                        else:
                            with st.status("一括取得中...", expanded=True) as _batch_st:
                                _full_db_now = clinic_db_manager.load_db(creds_data=_db_creds, sheet_url=_active_db_url)
                                for _dn in sorted(_g_entries):
                                    _de = _g_entries[_dn]
                                    st.write(f"🔍 {_dn} をクロール中...")
                                    try:
                                        _clinic_genres_all = [g for g, ge in _full_db_now.items() if _dn in ge]
                                        _dom = _de.get("domain", _dn)
                                        _start = _dom if _dom.startswith("http") else f"https://{_dom}"
                                        _content_b = crawl_site(_start, _clinic_genres_all[0] if _clinic_genres_all else "", max_pages=20)
                                        for _cg in _clinic_genres_all:
                                            _ci = extract_clinic_info_from_content(_content_b, _dn, _cg, claude_key, db_type=_db_type_sel)
                                            clinic_db_manager.upsert_clinic(_dn, _dom, _cg, _ci, creds_data=_db_creds, sheet_url=_active_db_url)
                                        st.write("　→ ✅ 完了")
                                    except Exception as _be:
                                        st.write(f"　→ ❌ エラー: {_be}")
                                _batch_st.update(label="✅ 一括取得完了", state="complete")
                            st.rerun()

                # ── 登録済み一覧 ──────────────────────────────
                st.divider()
                if not _g_entries:
                    st.info("このジャンルに案件がありません。上のフォームから追加してください。")
                else:
                    for _dn in sorted(_g_entries):
                        _de = _g_entries[_dn]
                        _d_updated = _de.get("updated_at", "未取得")
                        _d_has_info = bool(_de.get("info"))
                        _d_label = f"{'🟢' if _d_has_info else '🟡'} {_dn}　｜　更新: {_d_updated}"
                        with st.expander(_d_label, expanded=False):
                            st.caption(f"URL: {_de.get('domain', '')}")
                            _d_info = _de.get("info", "（未取得）")
                            st.text_area(
                                "取得済み情報",
                                value=_d_info[:2000] + ("..." if len(_d_info) > 2000 else ""),
                                height=200, disabled=True,
                                key=f"db_info_{_g_name}_{_dn}",
                            )
                            _rc1, _rc2 = st.columns(2)
                            if _rc1.button("🔄 再クロール（全ジャンル更新）", key=f"db_refresh_{_g_name}_{_dn}"):
                                if not claude_key:
                                    st.error("Claude API Key が未設定です")
                                else:
                                    with st.spinner(f"{_dn} を再クロール中..."):
                                        try:
                                            _full_db2 = clinic_db_manager.load_db(creds_data=_db_creds, sheet_url=_active_db_url)
                                            _clinic_genres2 = [g for g, ge in _full_db2.items() if _dn in ge]
                                            _dom2 = _de.get("domain", _dn)
                                            _start2 = _dom2 if _dom2.startswith("http") else f"https://{_dom2}"
                                            _content2 = crawl_site(_start2, _clinic_genres2[0] if _clinic_genres2 else "", max_pages=20)
                                            for _cg2 in _clinic_genres2:
                                                _ci2 = extract_clinic_info_from_content(_content2, _dn, _cg2, claude_key, db_type=_db_type_sel)
                                                clinic_db_manager.upsert_clinic(_dn, _dom2, _cg2, _ci2, creds_data=_db_creds, sheet_url=_active_db_url)
                                            st.success(f"再取得完了（{len(_clinic_genres2)} ジャンル更新）")
                                            st.rerun()
                                        except Exception as _rr_e:
                                            st.error(f"エラー: {_rr_e}")
                            if _rc2.button(f"🗑️ このジャンルから削除", key=f"db_del_{_g_name}_{_dn}"):
                                clinic_db_manager.delete_clinic(_dn, genre=_g_name, creds_data=_db_creds, sheet_url=_active_db_url)
                                st.success(f"「{_dn}」を「{_g_name}」から削除しました")
                                st.rerun()
