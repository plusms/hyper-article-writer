import json
import os
import time
import streamlit as st

from core.config import TOPICS
from core.researcher import (
    analyze_competitors, collect_clinic_info,
    discover_clinics_from_competitors, auto_discover_clinics,
)
from core.planner import generate_structure
from core.writer import generate_body, quality_check
from core.sheets import (
    read_input_rows, write_output_row, write_status, get_sheet,
    get_settings_sheet, read_defaults,
)

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
    try:
        return dict(st.secrets["gcp_service_account"])
    except Exception:
        pass
    if uploaded_file:
        uploaded_file.seek(0)
        return json.load(uploaded_file)
    return None

# ── APIキー（Secrets優先 → サイドバー入力 fallback）──────────
_gemini_key_default = _secret("GEMINI_API_KEY")
_claude_key_default  = _secret("CLAUDE_API_KEY")

# ── サイドバー：設定 ──────────────────────────────────────
with st.sidebar:
    st.header("設定")
    if _gemini_key_default:
        st.caption("Gemini API Key: Secrets から読込済み")
        gemini_key = _gemini_key_default
    else:
        gemini_key = st.text_input("Gemini API Key", type="password")

    if _claude_key_default:
        st.caption("Claude API Key: Secrets から読込済み")
        claude_key = _claude_key_default
    else:
        claude_key = st.text_input("Claude API Key", type="password")

    if _secret("gcp_service_account.type"):
        st.caption("Google Sheets 認証: Secrets から読込済み")
        sheets_creds_file = None
    else:
        sheets_creds_file = st.file_uploader("Google Sheets 認証JSON", type="json")
        if sheets_creds_file:
            st.success("認証ファイル読み込み済み")

    st.divider()
    st.markdown(
        "**スプシ入力列**\n"
        "A: サイト名　B: ジャンル\n"
        "C: 記事タイプ　D: メインKW\n"
        "E: サブKW\n"
        "F: 掲載クリニック\n"
        "　`TCB::tcb.net, 湘南::s-b-c.net`\n"
        "G: 競合URL（カンマ区切り）\n"
        "H: 追加指示（任意）\n"
        "I: 最訴求プラン（任意）\n"
        "J: 関連KW（任意・改行区切り）\n\n"
        "**自動書き込み**\n"
        "K: ステータス\n"
        "L: タイトル　M: メタ\n"
        "N: HTML　O: 要確認リスト\n\n"
        "**設定タブ**\n"
        "記事タイプ別デフォルト追加指示"
    )

tab1, tab2, tab3 = st.tabs(["📋 スプシ一括", "📝 1記事", "✅ 品質チェック"])


# ════════════════════════════════════════════════════════
#  共通ヘルパー
# ════════════════════════════════════════════════════════
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
#  Tab1: スプシ一括
# ════════════════════════════════════════════════════════
with tab1:
    st.title("📋 スプシ一括実行")
    st.caption("K列（ステータス）が空欄の行だけ処理します。設定タブのデフォルト追加指示＋H列の追記内容を合算します。")

    sheet_url_batch = st.text_input(
        "Google Sheet URL",
        placeholder="https://docs.google.com/spreadsheets/d/...",
        key="batch_sheet_url",
    )
    dry_run = st.checkbox("ドライラン（APIを叩かず対象行だけ確認）")

    if st.button("🚀 実行開始", type="primary", use_container_width=True, key="run_batch"):
        creds_data = _get_gcp_creds(sheets_creds_file)
        errors = []
        if not creds_data:
            errors.append("Google Sheets 認証情報が未設定です")
        if not sheet_url_batch:
            errors.append("スプレッドシートURLを入力してください")
        if not dry_run:
            if not gemini_key: errors.append("Gemini API Key が未設定です")
            if not claude_key:  errors.append("Claude API Key が未設定です")

        if errors:
            for e in errors:
                st.error(e)
        else:
            ws = get_sheet(sheet_url_batch, creds_data)
            rows = read_input_rows(ws)
            pending = [r for r in rows if not r.get("status")]

            try:
                settings_ws = get_settings_sheet(sheet_url_batch, creds_data)
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
                        comp   = analyze_competitors(inputs["competitor_urls"], gemini_key)
                        if inputs["competitor_urls"]:
                            discovered = discover_clinics_from_competitors(
                                comp["raw_pages"], inputs["clinics"], gemini_key
                            )
                        else:
                            discovered = auto_discover_clinics(
                                inputs["main_kw"], inputs["genre"], gemini_key, inputs["clinics"]
                            )
                        inputs["clinics"] = inputs["clinics"] + discovered
                        clinics   = collect_clinic_info(inputs["clinics"], inputs["genre"], gemini_key)
                        structure = generate_structure(inputs, comp, clinics, gemini_key)
                        output    = generate_body(inputs, structure, clinics, claude_key, comp)

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
#  Tab2: 1記事
# ════════════════════════════════════════════════════════
with tab2:
    st.title("📝 1記事")
    st.caption("単発テスト・社員用。設定タブのデフォルト追加指示を自動適用します。")

    article_type = st.radio("記事タイプ", ["地域", "比較", "商標"], horizontal=True, key="test_type")

    sheet_url_single = st.text_input(
        "スプレッドシートURL（設定タブのデフォルトを読む）",
        placeholder="https://docs.google.com/spreadsheets/d/...",
        key="single_sheet_url",
    )
    single_defaults: dict = {}
    if sheet_url_single.strip():
        creds_data_single = _get_gcp_creds(sheets_creds_file)
        if creds_data_single:
            try:
                _sws = get_settings_sheet(sheet_url_single.strip(), creds_data_single)
                single_defaults = read_defaults(_sws)
                st.success("設定タブ読み込み済み")
            except Exception as _e:
                st.warning(f"設定タブ読み込み失敗: {_e}")

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
            st.caption("デフォルト：未設定（スプシURLを入力すると反映）")

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
    sheet_url_out = st.text_input(
        "出力先スプレッドシートURL（任意）",
        placeholder="https://docs.google.com/spreadsheets/d/...",
        key="t_sheet_out",
    )
    output_row_num = st.number_input("書き込み行番号", min_value=2, value=2, step=1, key="t_row_num")

    st.divider()
    if st.button("🚀 実行", type="primary", use_container_width=True, key="run_test"):
        valid_clinics = [c for c in st.session_state.test_clinics if c["name"] and c["domain"]]
        errs = []
        if not gemini_key: errs.append("Gemini API Key 未設定")
        if not claude_key:  errs.append("Claude API Key 未設定")
        if not main_kw:     errs.append("メインKW を入力してください")
        if not genre:       errs.append("ジャンル を入力してください")
        for e in errs:
            st.error(e)

        if not errs:
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
                    comp = analyze_competitors(competitor_urls, gemini_key)
                    st.write("🤖 クリニック自動探索中...")
                    if competitor_urls:
                        discovered = discover_clinics_from_competitors(
                            comp["raw_pages"], valid_clinics, gemini_key
                        )
                    else:
                        discovered = auto_discover_clinics(
                            main_kw, genre, gemini_key, valid_clinics
                        )
                    all_clinics = valid_clinics + discovered
                    if discovered:
                        st.write(f"　→ {len(discovered)} 件を自動追加: {', '.join(c['name'] for c in discovered)}")
                    inputs["clinics"] = all_clinics
                    st.write("🏥 クリニック情報収集中...")
                    clinics = collect_clinic_info(all_clinics, genre, gemini_key)
                    st.write("📐 構成生成中（Gemini）...")
                    structure = generate_structure(inputs, comp, clinics, gemini_key)
                    st.write("✍️ 本文生成中（Claude）...")
                    output = generate_body(inputs, structure, clinics, claude_key, comp)
                    s.update(label="✅ 完了", state="complete")

                    st.markdown(f"**タイトル:** {structure['title']}")
                    st.markdown(f"**メタ:** {structure['meta']}")
                    with st.expander("構成テキスト（デバッグ用）"):
                        st.text(structure["structure_text"])
                        if output.get("debug"):
                            st.warning(f"⚠️ {output['debug']}")
                    if output["todo_list"]:
                        st.warning("**[要確認]リスト**\n" + output["todo_list"])
                    st.code(output["html"], language="html")
                    st.download_button("📥 HTMLをダウンロード", output["html"],
                                       file_name=f"{main_kw.replace(' ','_')}.html",
                                       mime="text/html")

                    if sheet_url_out.strip():
                        creds_out = _get_gcp_creds(sheets_creds_file)
                        if creds_out:
                            st.write("📊 スプレッドシートに書き込み中...")
                            try:
                                ws_out = get_sheet(sheet_url_out.strip(), creds_out)
                                write_output_row(ws_out, int(output_row_num), {
                                    "title":     structure["title"],
                                    "meta":      structure["meta"],
                                    "html":      output["html"],
                                    "todo_list": output["todo_list"],
                                })
                                st.success(f"行{output_row_num}に書き込みました")
                            except Exception as we:
                                st.warning(f"スプシ書き込みエラー: {we}")

                except Exception as e:
                    s.update(label="❌ エラー", state="error")
                    st.error(str(e))


# ════════════════════════════════════════════════════════
#  Tab3: 品質チェック
# ════════════════════════════════════════════════════════
with tab3:
    st.title("✅ 品質チェック")
    check_type    = st.radio("記事タイプ", ["地域", "比較", "商標"], horizontal=True, key="chk_type")
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
