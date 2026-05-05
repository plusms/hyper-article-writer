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
from core import site_config_manager, image_generator, drive_uploader, clinic_block_writer

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
_drive_folder_id     = _secret("DRIVE_PARENT_FOLDER_ID", "1CHqNruWiOVdeJPs7Nyd3Nfjt3sLxMc2c")

# ── サイドバー：設定 ──────────────────────────────────────
with st.sidebar:
    st.header("設定")
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

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📋 スプシ一括", "📝 1記事", "✅ 品質チェック", "⚙️ サイト設定", "🏥 クリニックブロック"])


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
                        clinics   = collect_clinic_info(inputs["clinics"], inputs["genre"], claude_key, inputs.get("article_type", ""))
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
#  Tab2: 1記事
# ════════════════════════════════════════════════════════
with tab2:
    st.title("📝 1記事")
    st.caption("単発テスト・社員用。設定タブのデフォルト追加指示を自動適用します。")

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

    article_type = st.radio("記事タイプ", ["地域", "比較", "商標", "ノウハウ"], horizontal=True, key="test_type")

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
                    clinics = collect_clinic_info(all_clinics, genre, claude_key, article_type)
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
with tab3:
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
with tab4:
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
#  Tab5: クリニックブロック
# ════════════════════════════════════════════════════════
with tab5:
    st.title("🏥 クリニックブロック")
    st.caption("おすすめクリニック紹介ブロックのHTMLを院ごとに生成します。Tab2の「掲載院一覧」をコピペして使ってください。")

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

    _cb_col1, _cb_col2 = st.columns(2)
    with _cb_col1:
        _cb_main_kw = st.text_input("メインKW", key="cb_main_kw")
        _cb_sub_kw = st.text_input("サブKW（カンマ区切り）", key="cb_sub_kw")
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
                            from core.researcher import collect_clinic_info
                            _scraped = collect_clinic_info(
                                [{"name": _cbc["name"], "domain": _clinic_url or _cbc["name"]}],
                                "", claude_key,
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
