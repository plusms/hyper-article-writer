import json
import os
import re
import hashlib
import pathlib
import datetime
import time
import contextlib
import streamlit as st

from core.config import TOPICS
from core.researcher import (
    analyze_competitors, collect_clinic_info,
    discover_clinics_from_competitors, auto_discover_clinics,
    crawl_site, fetch_page_text, extract_clinic_info_from_content,
    DB_TYPE_CLINIC, DB_TYPE_LIFESTYLE,
)
from core.planner import generate_structure
from core.writer import generate_body, quality_check
from core.sheets import (
    read_input_rows, write_output_row, write_full_row, write_status, get_sheet,
    get_settings_sheet, read_defaults, ARTICLE_TABS,
    get_worksheet_readonly, read_recent_input_rows,
    read_input_rows_knowhow, read_recent_input_rows_knowhow,
    write_status_knowhow, write_output_row_knowhow, write_full_row_knowhow,
    read_input_rows_knowhow_bulk, write_status_knowhow_bulk, write_output_row_knowhow_bulk,
    COL_TITLE, COL_TITLE_KNOWHOW,
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
_edit_logs_folder_id      = "0AFZI9kNsa56QUk9PVA"
_site_cfg_parent_folder   = _secret("SITE_CONFIG_FOLDER_ID") or _drive_folder_id
_article_sheet_url_default    = _secret("ARTICLE_SHEET_URL")
_db_sheet_url_default         = _secret("CLINIC_DB_SHEET_URL")
_lifestyle_sheet_url_default  = _secret("LIFESTYLE_DB_SHEET_URL")

# ── サイドバー：設定 ──────────────────────────────────────
with st.sidebar:
    st.header("設定")
    st.radio(
        "セクション",
        ["📝 コンテンツ作成", "🗄️ データ・設定", "❓ ヘルプ"],
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

    _openai_key_default = _secret("OPENAI_API_KEY")
    if _openai_key_default:
        st.caption("OpenAI API Key: Secrets から読込済み")
        openai_key = _openai_key_default
    else:
        openai_key = st.text_input("OpenAI API Key（DALL-E画像生成用）", type="password")

    image_provider = st.radio(
        "画像生成AI",
        ["gemini", "dalle"],
        format_func=lambda x: "Gemini" if x == "gemini" else "DALL-E 3 (ChatGPT)",
        horizontal=True,
        key="image_provider",
    )
    research_provider = st.radio(
        "リサーチAI（競合分析・案件収集）",
        ["claude", "gemini"],
        format_func=lambda x: "Claude (Haiku)" if x == "claude" else "Gemini Flash",
        horizontal=True,
        key="research_provider",
    )
    article_provider = st.radio(
        "記事生成AI（構成・本文）",
        ["claude", "gemini"],
        format_func=lambda x: "Claude (Sonnet)" if x == "claude" else "Gemini Flash",
        horizontal=True,
        key="article_provider",
    )

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
        st.caption("案件DB スプシ: Secrets から読込済み")
        db_sheet_url = _db_sheet_url_default
    else:
        db_sheet_url = st.text_input(
            "案件DB スプレッドシートURL",
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



# サイト設定の永続化に使うクレデンシャル（Drive保存用）
_site_cfg_creds = _get_gcp_creds(sheets_creds_file)

_main_nav = st.session_state.get("main_nav", "📝 コンテンツ作成")
if _main_nav == "📝 コンテンツ作成":
    tab_batch, tab_custom, tab_rank, tab_qual, tab_writing_chat = st.tabs([
        "📋 一括作成", "📝 カスタム作成", "🏥 ランキングブロック", "✅ 品質チェック", "✍️ 執筆チャット",
    ])
    tab_cases = tab_settings = tab_help = None
elif _main_nav == "🗄️ データ・設定":
    tab_cases, tab_settings = st.tabs(["🗄️ 商品データベース", "⚙️ サイト設定"])
    tab_batch = tab_custom = tab_rank = tab_qual = tab_writing_chat = tab_help = None
else:  # ❓ ヘルプ
    tab_help = True  # タブなしで直接描画
    tab_batch = tab_custom = tab_rank = tab_qual = tab_writing_chat = tab_cases = tab_settings = None


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


def _split_html_by_h2(html: str) -> list[dict]:
    """HTMLをH2単位に分割。
    優先①: <!-- H2_BLOCK_START:{title} --> コメントマーカー（サイトパーツ使用時）
    優先②: 標準 <h2> タグ（パーツなし時）
    どちらもなければ1ブロックとして返す。
    """
    def _make_block(title, html_str):
        return {"title": title, "html": html_str, "original_html": html_str, "confirmed": False, "instruction": "", "modified": False}

    # ── 優先①: コメントマーカーで分割 ──────────────────────────
    # lookahead splitでマーカーを消費せずブロック先頭で分割（capturing groupによる本文消失を防ぐ）
    marker_pattern = r'<!--\s*H2_BLOCK_START:([^-]*?)-->'
    if re.search(marker_pattern, html):
        parts = re.split(r'(?=<!--\s*H2_BLOCK_START:)', html)
        result = []
        pre_h2 = parts[0].strip()
        for part in parts[1:]:
            m = re.match(marker_pattern, part)
            if m:
                title = m.group(1).strip() or f"セクション {len(result) + 1}"
                result.append(_make_block(title, part.strip()))
        if pre_h2 and result:
            result[0]["html"] = pre_h2 + "\n" + result[0]["html"]
            result[0]["original_html"] = result[0]["html"]
        elif pre_h2:
            result.insert(0, _make_block("（冒頭）", pre_h2))
        if result:
            return result

    # ── 優先②: 標準 <h2> タグで分割 ────────────────────────────
    parts = re.split(r'(?=<h2[\s>])', html, flags=re.IGNORECASE)
    result = []
    pre_h2 = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not re.match(r'<h2[\s>]', part, re.IGNORECASE):
            pre_h2 = part
            continue
        m = re.search(r'<h2[^>]*>(.*?)</h2>', part, re.IGNORECASE | re.DOTALL)
        raw_title = m.group(1) if m else ""
        title = re.sub(r'<[^>]+>', '', raw_title).strip() or f"セクション {len(result) + 1}"
        result.append(_make_block(title, part))
    if pre_h2 and result:
        result[0]["html"] = pre_h2 + "\n" + result[0]["html"]
        result[0]["original_html"] = result[0]["html"]
    elif pre_h2:
        result.insert(0, _make_block("（冒頭）", pre_h2))
    if result:
        return result

    # ── フォールバック: 全体を1ブロック ─────────────────────────
    return [_make_block("全体（H2分割不可）", html)]


def _regenerate_h2_block(
    h2_index: int,
    blocks: list[dict],
    instruction: str,
    inputs: dict,
    structure_text: str,
    claude_key: str,
) -> str:
    """指定インデックスのH2を修正指示に従って再生成する。前後H2をコンテキストとして渡す。"""
    import anthropic as _ant
    current = blocks[h2_index]
    prev_html = blocks[h2_index - 1]["html"][:1500] if h2_index > 0 else ""
    next_html = blocks[h2_index + 1]["html"][:1000] if h2_index < len(blocks) - 1 else ""
    sub_kw_str = ", ".join(inputs.get("sub_kw", [])) if isinstance(inputs.get("sub_kw"), list) else inputs.get("sub_kw", "")
    prompt = f"""あなたはSEO記事の編集者です。
以下の条件に従い、指定H2セクションを修正・再生成してください。

【記事の条件】
メインKW: {inputs.get('main_kw', '')}
サブKW: {sub_kw_str}
記事種別: {inputs.get('article_type', '')}
ジャンル: {inputs.get('genre', '')}

【記事全体の構成】
{structure_text[:2000]}

【前のH2の内容（文脈維持用・変更不要）】
{prev_html if prev_html else '（なし）'}

【再生成対象のH2】
見出し: {current['title']}
現在のHTML:
{current['html']}

【次のH2の内容（文脈維持用・変更不要）】
{next_html if next_html else '（なし）'}

【修正指示】
{instruction}

【出力ルール】
- このH2セクションのHTMLのみを出力する（前後のH2は出力しない）
- 修正指示を確実に反映する
- 前後のH2との文体・トーン・形式を合わせる
- HTML本文のみ出力。説明文・コードフェンス不要
"""
    client = _ant.Anthropic(api_key=claude_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def build_inputs_from_row(row: dict, defaults: dict | None = None) -> dict:
    clinics_raw = row.get("clinics_raw", "")
    clinics = []
    for item in clinics_raw.split(","):
        item = item.strip()
        if "::" in item:
            parts = item.split("::")
            clinics.append({
                "name":        parts[0].strip(),
                "domain":      parts[1].strip() if len(parts) > 1 else "",
                "recommended": parts[2].strip() if len(parts) > 2 else "",
                "appeal":      parts[3].strip() if len(parts) > 3 else "",
            })

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
    st.caption("ステータスが空欄の行を対象に一括生成します。")

    if not article_sheet_url:
        st.warning("サイドバーで「記事スプレッドシートURL」を設定してください。")

    _batch_col1, _batch_col2 = st.columns([2, 1])
    batch_tab_sel = _batch_col1.selectbox(
        "処理するタブ", ARTICLE_TABS, key="batch_tab_sel",
    )
    batch_db_type = _batch_col2.selectbox(
        "DBタイプ", [DB_TYPE_CLINIC, DB_TYPE_LIFESTYLE], key="batch_db_type",
    )

    # ノウハウ一括はサイト名・ジャンルを列に持たないのでUIで入力
    _batch_is_bulk = batch_tab_sel == "ノウハウ一括"
    if _batch_is_bulk:
        _bulk_col1, _bulk_col2 = st.columns(2)
        _bnk_site_opts = ["指定なし"] + site_config_manager.list_sites(_site_cfg_creds, _site_cfg_parent_folder)
        bulk_site_name = _bulk_col1.selectbox("サイト名（全行共通）", _bnk_site_opts, key="bulk_site_name")
        bulk_genre     = _bulk_col2.text_input("ジャンル（全行共通）", key="bulk_genre")

    dry_run = st.toggle("ドライラン（APIを使わず対象行の確認のみ）", key="batch_dry_run")

    # ── スプシタブ初期化 ────────────────────────────────────────────
    with st.expander("🔧 スプシのタブを初期化（ヘッダー作成・更新）", expanded=False):
        st.caption("スプシにタブがない場合や、ヘッダーを最新の形式に更新したい場合に使います。データ行には影響しません。")
        _init_tab_sel = st.selectbox("対象タブ", ARTICLE_TABS, key="batch_init_tab_sel")
        if st.button("📋 ヘッダーを作成/更新", key="batch_init_tab_btn"):
            _init_creds = _get_gcp_creds(sheets_creds_file)
            if not _init_creds:
                st.error("Google Sheets 認証情報が未設定です")
            elif not article_sheet_url:
                st.error("記事スプレッドシートURLが未設定です")
            else:
                try:
                    get_sheet(article_sheet_url, _init_creds, tab_name=_init_tab_sel)
                    st.success(f"✅ [{_init_tab_sel}] タブのヘッダーを更新しました")
                except Exception as _ie:
                    st.error(f"エラー: {_ie}")

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
            _batch_is_bulk = batch_tab_sel == "ノウハウ一括"
            _batch_is_kh   = batch_tab_sel == "ノウハウ" or _batch_is_bulk

            if _batch_is_bulk:
                _b_site = st.session_state.get("bulk_site_name", "指定なし")
                _b_site = "" if _b_site == "指定なし" else _b_site
                _b_genre = st.session_state.get("bulk_genre", "")
                rows = read_input_rows_knowhow_bulk(ws, site_name=_b_site, genre=_b_genre)
            elif batch_tab_sel == "ノウハウ":
                rows = read_input_rows_knowhow(ws)
            else:
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
                    if _batch_is_bulk:
                        _write_status = write_status_knowhow_bulk
                    elif batch_tab_sel == "ノウハウ":
                        _write_status = write_status_knowhow
                    else:
                        _write_status = write_status
                    _write_status(ws, row_num, "処理中")

                    try:
                        inputs = build_inputs_from_row(row, defaults)

                        # サイトパーツ読み込み（site_nameが登録済みサイトと一致する場合のみ）
                        _batch_site_parts = ""
                        _batch_site_name = inputs.get("site_name", "")
                        if _batch_site_name and _batch_site_name in site_config_manager.list_sites(_site_cfg_creds, _site_cfg_parent_folder):
                            _sc = site_config_manager.load_site_config(_batch_site_name, _site_cfg_creds, _site_cfg_parent_folder)
                            _batch_site_parts = site_config_manager.format_site_parts(_sc.get("components", []))

                        comp = analyze_competitors(inputs["competitor_urls"], claude_key, gemini_api_key=gemini_key, research_provider=research_provider)
                        # ノウハウ系は案件探索しない
                        if _batch_is_kh:
                            inputs["clinics"] = []
                        elif inputs["competitor_urls"]:
                            discovered = discover_clinics_from_competitors(
                                comp["raw_pages"], inputs["clinics"], claude_key, gemini_api_key=gemini_key, research_provider=research_provider
                            )
                            inputs["clinics"] = inputs["clinics"] + discovered
                        else:
                            discovered = auto_discover_clinics(
                                inputs["main_kw"], inputs["genre"], claude_key, inputs["clinics"], gemini_api_key=gemini_key, research_provider=research_provider
                            )
                            inputs["clinics"] = inputs["clinics"] + discovered
                        _batch_active_db_url = db_sheet_url if batch_db_type == DB_TYPE_CLINIC else lifestyle_sheet_url
                        _batch_db_cache = clinic_db_manager.build_db_cache([c["name"] for c in inputs["clinics"]], genre=inputs.get("genre", ""), creds_data=creds_data, sheet_url=_batch_active_db_url)
                        clinics   = collect_clinic_info(inputs["clinics"], inputs["genre"], claude_key, inputs.get("article_type", ""), db_cache=_batch_db_cache, db_type=batch_db_type, gemini_api_key=gemini_key, research_provider=research_provider)
                        structure = generate_structure(inputs, comp, clinics, claude_key, gemini_api_key=gemini_key, article_provider=article_provider)
                        output    = generate_body(inputs, structure, clinics, claude_key, comp,
                                                  site_parts=_batch_site_parts, gemini_api_key=gemini_key, article_provider=article_provider)

                        _out = {
                            "title":     structure["title"],
                            "meta":      structure["meta"],
                            "html":      output["html"],
                            "todo_list": output["todo_list"],
                        }
                        if _batch_is_bulk:
                            write_output_row_knowhow_bulk(ws, row_num, _out)
                            write_status_knowhow_bulk(ws, row_num, "完了")
                        elif batch_tab_sel == "ノウハウ":
                            write_output_row_knowhow(ws, row_num, _out)
                            write_status_knowhow(ws, row_num, "完了")
                        else:
                            write_output_row(ws, row_num, {**_out, "clinics": inputs["clinics"]})
                            write_status(ws, row_num, "完了")

                    except Exception as e:
                        _write_status(ws, row_num, f"エラー: {e}")
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
    _registered_sites = site_config_manager.list_sites(_site_cfg_creds, _site_cfg_parent_folder)
    _site_options = ["（なし）"] + _registered_sites
    selected_site_for_parts = st.selectbox(
        "サイトパーツを使用する",
        _site_options,
        key="t_site_parts_sel",
        help="登録済みサイトを選ぶと、そのサイトのHTMLパーツを記事生成に使用します。",
    )
    if selected_site_for_parts != "（なし）":
        _preview_cfg = site_config_manager.load_site_config(selected_site_for_parts, _site_cfg_creds, _site_cfg_parent_folder)
        _active_count = sum(1 for c in _preview_cfg.get("components", []) if c.get("active", True))
        st.caption(f"✅ {selected_site_for_parts}：有効パーツ {_active_count} 件")

    st.divider()

    # 復元ボタン経由でタイプが変更された場合、radio描画前に適用する
    if "_pending_article_type" in st.session_state:
        st.session_state["test_type"] = st.session_state.pop("_pending_article_type")

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

    # ── 登録情報履歴（スプシ最新5件・デプロイをまたいで永続）────────────────
    _hist_cache_key = f"t2_sheet_hist_{article_type}"
    if article_sheet_url:
        _hist_col1, _hist_col2 = st.columns([9, 1])
        _hist_col1.empty()
        if _hist_col2.button("🔄", key="t2_hist_refresh", help="スプシから再読み込み"):
            st.session_state.pop(_hist_cache_key, None)

        if _hist_cache_key not in st.session_state:
            _hist_creds = _get_gcp_creds(sheets_creds_file)
            if _hist_creds:
                try:
                    _hist_ws = get_worksheet_readonly(article_sheet_url, _hist_creds, article_type)
                    if _hist_ws:
                        _hist_reader = read_recent_input_rows_knowhow if article_type == "ノウハウ" else read_recent_input_rows
                        st.session_state[_hist_cache_key] = _hist_reader(_hist_ws)
                    else:
                        st.session_state[_hist_cache_key] = []
                except Exception:
                    st.session_state[_hist_cache_key] = []

        _sheet_hist = st.session_state.get(_hist_cache_key, [])
        if _sheet_hist:
            with st.expander(f"📋 {article_type}の登録情報履歴（最新{len(_sheet_hist)}件）", expanded=False):
                for _th in _sheet_hist:
                    _th_clinics = []
                    for _item in [x.strip() for x in _th.get("clinics_raw", "").split(",") if x.strip()]:
                        _p = _item.split("::")
                        _th_clinics.append({
                            "name":        _p[0].strip(),
                            "domain":      _p[1].strip() if len(_p) > 1 else "",
                            "recommended": _p[2].strip() if len(_p) > 2 else "",
                            "appeal":      _p[3].strip() if len(_p) > 3 else "",
                        })
                    _th_comps = [u.strip() for u in _th.get("competitor_urls_raw", "").split(",") if u.strip()]
                    _th_row = _th.get("row_index", "")
                    with st.expander(f"**{_th.get('main_kw', '(不明)')}**  （行{_th_row}）", expanded=False):
                        if _th_clinics:
                            st.caption("掲載案件")
                            for _thc in _th_clinics:
                                if _thc.get("name"):
                                    st.write(f"・{_thc['name']} / {_thc.get('domain', '')}")
                        if _th_comps:
                            st.caption(f"競合URL: {len(_th_comps)}件")
                        if st.button("📥 この入力条件を復元", key=f"th_restore_r{_th_row}"):
                            _r_atype = _th.get("article_type") or article_type
                            if _r_atype in ["地域", "比較", "商標", "ノウハウ"]:
                                st.session_state["_pending_article_type"] = _r_atype
                            st.session_state["t_site"]       = _th.get("site_name", "")
                            st.session_state["t_genre"]      = _th.get("genre", "")
                            st.session_state["t_main_kw"]    = _th.get("main_kw", "")
                            st.session_state["t_sub_kw"]     = _th.get("sub_kw", "")
                            st.session_state["t_related_kw"] = _th.get("related_kw", "")
                            # 強みをcustom_blockからパースして個別フィールドに戻す
                            _cb_raw = _th.get("custom_block", "")
                            _r_strengths = [{"point": "", "basis": ""} for _ in range(3)]
                            _cb_clean = _cb_raw
                            if "【比較優位性】" in _cb_raw:
                                _cb_split = _cb_raw.split("【比較優位性】", 1)
                                _cb_clean = _cb_split[0].rstrip()
                                for _sln in _cb_split[1].strip().split("\n"):
                                    _sm = re.match(r"強み(\d+): (.+?)(?:（根拠: (.+?)）)?$", _sln.strip())
                                    if _sm:
                                        _si = int(_sm.group(1)) - 1
                                        if 0 <= _si < 3:
                                            _r_strengths[_si] = {"point": _sm.group(2).strip(), "basis": _sm.group(3).strip() if _sm.group(3) else ""}
                            st.session_state["t_custom"] = _cb_clean
                            st.session_state["t_trademark_strengths"] = _r_strengths
                            for _stri in range(3):
                                st.session_state[f"tm_str_pt_{_stri}"] = _r_strengths[_stri]["point"]
                                st.session_state[f"tm_str_bs_{_stri}"] = _r_strengths[_stri]["basis"]
                            _r_set_clinics = _th_clinics or [{"name": "", "domain": "", "recommended": "", "appeal": ""}]
                            st.session_state["test_clinics"] = _r_set_clinics
                            # 商標フォームの個別widgetキーを更新
                            if _r_set_clinics:
                                st.session_state["tm_clinic_name"]   = _r_set_clinics[0]["name"]
                                st.session_state["tm_clinic_domain"] = _r_set_clinics[0]["domain"]
                                # clinics_rawに最訴求プランがない行（一括生成）はI列をフォールバック
                                st.session_state["tm_clinic_rec"]    = _r_set_clinics[0]["recommended"] or _th.get("recommended", "")
                            # 地域/比較フォームの個別widgetキーを更新
                            for _rci2, _rc2 in enumerate(_r_set_clinics):
                                st.session_state[f"tcn_{_rci2}"] = _rc2["name"]
                                st.session_state[f"tcd_{_rci2}"] = _rc2["domain"]
                                st.session_state[f"tcr_{_rci2}"] = _rc2["recommended"]
                                st.session_state[f"tca_{_rci2}"] = _rc2["appeal"]
                            for _rci in range(5):
                                st.session_state[f"t_comp_{_rci}"] = _th_comps[_rci] if _rci < len(_th_comps) else ""
                            st.rerun()

    # ── 行番号指定して復元 ────────────────────────────────────────
    if article_sheet_url:
        with st.expander("🔢 スプシの特定行から復元", expanded=False):
            _sr_col1, _sr_col2 = st.columns([2, 1])
            _sr_tab = _sr_col1.selectbox("タブ", ARTICLE_TABS, key="t2_sr_tab")
            _sr_row = _sr_col2.number_input("行番号", min_value=2, value=2, step=1, key="t2_sr_row")
            if st.button("📥 この行を復元", key="t2_sr_btn"):
                _sr_creds = _get_gcp_creds(sheets_creds_file)
                if not _sr_creds:
                    st.error("Google Sheets 認証情報が未設定です")
                else:
                    try:
                        _sr_ws = get_worksheet_readonly(article_sheet_url, _sr_creds, _sr_tab)
                        if not _sr_ws:
                            st.warning(f"タブ「{_sr_tab}」が見つかりません")
                        else:
                            _sr_all = read_input_rows_knowhow(_sr_ws) if _sr_tab == "ノウハウ" else read_input_rows(_sr_ws, default_article_type=_sr_tab)
                            _sr_data = next((r for r in _sr_all if r["row_index"] == int(_sr_row)), None)
                            if not _sr_data:
                                st.warning(f"行 {int(_sr_row)} にデータが見つかりません（main_kwが空の行は除外されます）")
                            else:
                                _sr_atype = _sr_data.get("article_type") or _sr_tab
                                if _sr_atype in ["地域", "比較", "商標", "ノウハウ"]:
                                    st.session_state["_pending_article_type"] = _sr_atype
                                st.session_state["t_site"]       = _sr_data.get("site_name", "")
                                st.session_state["t_genre"]      = _sr_data.get("genre", "")
                                st.session_state["t_main_kw"]    = _sr_data.get("main_kw", "")
                                st.session_state["t_sub_kw"]     = _sr_data.get("sub_kw", "")
                                st.session_state["t_related_kw"] = _sr_data.get("related_kw", "")
                                # 強みをcustom_blockからパースして個別フィールドに戻す
                                _sr_cb_raw = _sr_data.get("custom_block", "")
                                _sr_strengths = [{"point": "", "basis": ""} for _ in range(3)]
                                _sr_cb_clean = _sr_cb_raw
                                if "【比較優位性】" in _sr_cb_raw:
                                    _sr_cb_split = _sr_cb_raw.split("【比較優位性】", 1)
                                    _sr_cb_clean = _sr_cb_split[0].rstrip()
                                    for _sln in _sr_cb_split[1].strip().split("\n"):
                                        _sm = re.match(r"強み(\d+): (.+?)(?:（根拠: (.+?)）)?$", _sln.strip())
                                        if _sm:
                                            _si = int(_sm.group(1)) - 1
                                            if 0 <= _si < 3:
                                                _sr_strengths[_si] = {"point": _sm.group(2).strip(), "basis": _sm.group(3).strip() if _sm.group(3) else ""}
                                st.session_state["t_custom"] = _sr_cb_clean
                                st.session_state["t_trademark_strengths"] = _sr_strengths
                                for _stri in range(3):
                                    st.session_state[f"tm_str_pt_{_stri}"] = _sr_strengths[_stri]["point"]
                                    st.session_state[f"tm_str_bs_{_stri}"] = _sr_strengths[_stri]["basis"]
                                _sr_clinics = []
                                for _src in [x.strip() for x in _sr_data.get("clinics_raw", "").split(",") if x.strip()]:
                                    _sp = _src.split("::")
                                    _sr_clinics.append({
                                        "name":        _sp[0].strip(),
                                        "domain":      _sp[1].strip() if len(_sp) > 1 else "",
                                        "recommended": _sp[2].strip() if len(_sp) > 2 else "",
                                        "appeal":      _sp[3].strip() if len(_sp) > 3 else "",
                                    })
                                _sr_set_clinics = _sr_clinics or [{"name": "", "domain": "", "recommended": "", "appeal": ""}]
                                st.session_state["test_clinics"] = _sr_set_clinics
                                # 商標フォームの個別widgetキーを更新
                                if _sr_set_clinics:
                                    st.session_state["tm_clinic_name"]   = _sr_set_clinics[0]["name"]
                                    st.session_state["tm_clinic_domain"] = _sr_set_clinics[0]["domain"]
                                    # clinics_rawに最訴求プランがない行（一括生成）はI列をフォールバック
                                    st.session_state["tm_clinic_rec"]    = _sr_set_clinics[0]["recommended"] or _sr_data.get("recommended", "")
                                # 地域/比較フォームの個別widgetキーを更新
                                for _sci2, _sc2 in enumerate(_sr_set_clinics):
                                    st.session_state[f"tcn_{_sci2}"] = _sc2["name"]
                                    st.session_state[f"tcd_{_sci2}"] = _sc2["domain"]
                                    st.session_state[f"tcr_{_sci2}"] = _sc2["recommended"]
                                    st.session_state[f"tca_{_sci2}"] = _sc2["appeal"]
                                _sr_comps = [u.strip() for u in _sr_data.get("competitor_urls_raw", "").split(",") if u.strip()]
                                for _sci in range(5):
                                    st.session_state[f"t_comp_{_sci}"] = _sr_comps[_sci] if _sci < len(_sr_comps) else ""
                                st.rerun()
                    except Exception as _sre:
                        st.error(f"読み込みエラー: {_sre}")

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("基本情報")
        _cst_site_opts = ["指定なし"] + _registered_sites
        if st.session_state.get("t_site", "") not in _cst_site_opts:
            st.session_state["t_site"] = "指定なし"
        _cst_site_sel = st.selectbox("サイト名", _cst_site_opts, key="t_site")
        site_name = "" if _cst_site_sel == "指定なし" else _cst_site_sel
        genre     = st.text_input("ジャンル *", key="t_genre", placeholder="クマ取り / AGA治療 / 医療ダイエット")
        main_kw   = st.text_input("メインKW *", key="t_main_kw")
        sub_kw    = st.text_input("サブKW（カンマ区切り）", key="t_sub_kw")
        related_kw = st.text_area(
            "関連KW（任意・改行区切り）",
            key="t_related_kw",
            placeholder="シミ取り 大阪 口コミ\nシミ取り 皮膚科 大阪 保険適用\nシミ取り 大阪 一回で",
            help="構成の網羅性チェックに使用。検索面からそのままコピペできます。",
            height=120,
        )

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

        if "custom_blocks" not in st.session_state:
            st.session_state.custom_blocks = [{"text": "", "intent": ""}]

        cb_to_remove = []
        for i, cb in enumerate(st.session_state.custom_blocks):
            cb_cols = st.columns([11, 1])
            t = cb_cols[0].text_area(
                f"追加指示 {i + 1}（任意）",
                value=cb["text"], height=80, key=f"cb_text_{i}",
                placeholder="例：GLP-1の仕組みを解説するセクションを追加してほしい",
            )
            if len(st.session_state.custom_blocks) > 1 and cb_cols[1].button("✕", key=f"cb_rm_{i}"):
                cb_to_remove.append(i)
            intent = st.text_area(
                "追加指示の意図（任意）",
                value=cb["intent"], height=60, key=f"cb_intent_{i}",
                placeholder="例：既存薬に慣れたユーザーが新鮮に受け取れるようにしたい",
            )
            st.session_state.custom_blocks[i] = {"text": t, "intent": intent}
        for idx in reversed(cb_to_remove):
            st.session_state.custom_blocks.pop(idx)
        if st.button("＋ 追加指示を追加", key="cb_add"):
            st.session_state.custom_blocks.append({"text": "", "intent": ""})
            st.rerun()

    if article_type != "ノウハウ":
        st.divider()
        st.subheader("含めるセクション")
        selected_topics = _render_topic_checkboxes(article_type, key_prefix="t")
    else:
        selected_topics = None

    st.divider()

    # ── 掲載案件・競合URL（記事タイプごとに分岐）────────────────
    if "test_clinics" not in st.session_state:
        st.session_state.test_clinics = [{"name": "", "domain": "", "recommended": "", "appeal": ""}]
    for _c in st.session_state.test_clinics:
        _c.setdefault("recommended", "")
        _c.setdefault("appeal", "")

    if article_type == "商標":
        # ── 商標記事：1院固定 ─────────────────────────────────
        clinic_count = 1
        st.session_state.test_clinics = st.session_state.test_clinics[:1]
        st.subheader("対象案件（1件固定）")
        _tm_c = st.session_state.test_clinics[0]
        _tm_c0, _tm_c1, _tm_c2 = st.columns([3, 3, 1])
        _tm_n = _tm_c0.text_input("案件名 *", value=_tm_c["name"], key="tm_clinic_name", placeholder="東京美肌堂")
        _tm_d = _tm_c1.text_input("ドメイン *", value=_tm_c["domain"], key="tm_clinic_domain", placeholder="tokyo-bihado.com")
        _tm_c2.markdown("<div style='padding-top:1.6rem'></div>", unsafe_allow_html=True)
        if _tm_c2.button("📂 DB読込", key="tm_load_db", use_container_width=True):
            if _tm_n.strip():
                _tm_db_creds = _get_gcp_creds(sheets_creds_file)
                _tm_db_url = db_sheet_url if custom_db_type == DB_TYPE_CLINIC else lifestyle_sheet_url
                _tm_db_result = clinic_db_manager.build_db_cache(
                    [_tm_n.strip()], genre=genre,
                    creds_data=_tm_db_creds, sheet_url=_tm_db_url,
                )
                st.session_state["t_trademark_db_loaded"] = _tm_db_result.get(_tm_n.strip(), "")
            else:
                st.warning("案件名を入力してください")

        _tm_db_loaded = st.session_state.get("t_trademark_db_loaded")
        if _tm_db_loaded is not None:
            if _tm_db_loaded:
                st.caption("📂 DB情報（記事生成に反映されます）")
                st.text_area(
                    "", value=_tm_db_loaded, height=120, disabled=True,
                    key="tm_db_preview", label_visibility="collapsed",
                )
            else:
                st.info(f"「{_tm_n}」はDBに未登録です（スクレイピングで取得）")

        _tm_r = st.text_input("最訴求プラン *", value=_tm_c["recommended"], key="tm_clinic_rec", placeholder="例：美容内服パックコース 月額3,980円〜")
        st.session_state.test_clinics[0] = {"name": _tm_n, "domain": _tm_d, "recommended": _tm_r, "appeal": ""}

        st.markdown("**比較優位性（強み）**")
        st.caption("ここに書いた強みと根拠が記事の訴求軸になります。AIが自然な文脈に組み込みます。")
        if "t_trademark_strengths" not in st.session_state:
            st.session_state["t_trademark_strengths"] = [{"point": "", "basis": ""} for _ in range(3)]
        _str_labels = ["強み①", "強み②", "強み③"]
        _str_placeholders = [
            ("通院不要でオンライン処方のみ", "公式サイトに「完全オンライン診療」と明記"),
            ("処方量が他院より高用量から対応", "0.5mg〜スタートで他院より1段階上"),
            ("返金保証あり", "30日以内なら全額返金と明記"),
        ]
        for _si in range(3):
            _s = st.session_state["t_trademark_strengths"][_si]
            _sc1, _sc2 = st.columns([2, 3])
            _sp = _sc1.text_input(_str_labels[_si], value=_s["point"], key=f"tm_str_pt_{_si}", placeholder=_str_placeholders[_si][0])
            _sb = _sc2.text_input("根拠・補足", value=_s["basis"], key=f"tm_str_bs_{_si}", placeholder=_str_placeholders[_si][1])
            st.session_state["t_trademark_strengths"][_si] = {"point": _sp, "basis": _sb}

        with st.expander("📋 補完情報・参考URL（任意）", expanded=False):
            st.caption("DBやスクレイプで取れない情報（料金・取扱い薬・件数など）と重点スクレイプしたいURLを入力すると[要確認]が減ります。")
            st.text_area(
                "補完情報（自由記述）",
                key="t_trademark_supplement",
                height=160,
                placeholder="例：\n取り扱い薬：セマグルチド 0.25mg〜2.4mg\n料金：月額9,800円〜\n院数：全国50院\n返金保証：30日以内全額返金",
            )
            st.text_input(
                "参考URL（任意）",
                key="t_trademark_ref_url",
                placeholder="https://tokyo-bihado.com/plan/ ← 料金ページなど重点スクレイプしたいURL",
            )

        st.subheader("競合URL")
        st.caption("上位記事の構成参照用。案件の追加には使用しません。")
        competitor_urls = []
        for i in range(5):
            u = st.text_input(
                f"競合URL {i+1}" if i == 0 else "",
                key=f"t_comp_{i}",
                label_visibility="visible" if i == 0 else "collapsed",
            )
            if u.strip():
                competitor_urls.append(u.strip())

    elif article_type in ("地域", "比較"):
        # ── 地域・比較記事：複数院 ───────────────────────────────
        _ca_col1, _ca_col2 = st.columns([3, 1])
        _ca_col1.subheader("掲載案件")
        _ca_col2.markdown("<div style='padding-top:0.5rem'></div>", unsafe_allow_html=True)
        clinic_count = int(_ca_col2.number_input(
            "件数（任意）", min_value=0, value=0, step=1, key="t_clinic_count",
            help="空白(0)にすると競合の掲載件数に自動で合わせます。",
        ))
        st.caption("※ここに入力した案件は必ず記事に掲載されます。空欄のままでも自動探索で補完されます。")
        to_remove = []
        for i, c in enumerate(st.session_state.test_clinics):
            is_first = (i == 0)
            st.caption("案件 1（最上位）" if is_first else f"案件 {i + 1}")
            tc0, tc1, tc2 = st.columns([3, 3, 1])
            n = tc0.text_input("案件名 *", value=c["name"],   key=f"tcn_{i}", placeholder="TCB東京中央美容外科")
            d = tc1.text_input("ドメイン *", value=c["domain"], key=f"tcd_{i}", placeholder="tcb.net または https://lp.example.com/...")
            if tc2.button("✕", key=f"trm_{i}") and len(st.session_state.test_clinics) > 1:
                to_remove.append(i)
            rec_label = "最訴求プラン *" if is_first else "最訴求プラン（任意）"
            r = st.text_input(rec_label, value=c["recommended"], key=f"tcr_{i}", placeholder="例：セマグルチド0.5mgプラン")
            a = st.text_area("強み・比較優位性（任意）", value=c["appeal"], height=60, key=f"tca_{i}", placeholder="例：他社より処方量が1段階上から始められる")
            st.session_state.test_clinics[i] = {"name": n, "domain": d, "recommended": r, "appeal": a}
        for idx in reversed(to_remove):
            st.session_state.test_clinics.pop(idx)
        if st.button("＋ 案件を追加", key="t_add"):
            st.session_state.test_clinics.append({"name": "", "domain": "", "recommended": "", "appeal": ""})
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

    else:
        # ── ノウハウ記事：掲載案件なし ──────────────────────────
        clinic_count = 0
        st.session_state.test_clinics = []
        st.info("ノウハウ記事は掲載案件なし（教育コンテンツのみ生成）")

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
    with st.expander("👤 ユーザー認識インプット（任意）"):
        st.caption("このKWで検索するユーザーの前提知識・思い込み。AIが説明の深さ・切り口を調整します。")
        st.text_area(
            "ユーザーの前提・認識",
            height=100, key="t_user_awareness",
            placeholder="例：いびきで検索するユーザーはレーザー治療があまり浮かんでいない\n例：AGA治療 札幌で検索するユーザーはすでにオンライン診療を想定していそう",
        )

    st.divider()
    output_tab_sel = st.selectbox(
        "スプシ書き込み先タブ（任意）", ["（書き込まない）"] + ARTICLE_TABS, key="t_out_tab",
        help="選択すると生成完了後にスプシへ自動書き込みします。メインKWが一致する行を優先し、なければ次の空き行に書き込みます。",
    )

    st.divider()
    if article_type != "ノウハウ":
        gen_mode = st.radio(
            "生成モード",
            ["一括生成", "見出し確認あり"],
            horizontal=True,
            key="t_gen_mode",
            help="「見出し確認あり」を選ぶと、見出し確認・修正後に本文を生成できます。",
        )
    else:
        gen_mode = "一括生成"
    _run_label = "🔍 見出しを生成" if gen_mode == "見出し確認あり" else "🚀 実行"
    if st.button(_run_label, type="primary", use_container_width=True, key="run_test"):
        valid_clinics = [c for c in st.session_state.get("test_clinics", []) if c["name"] and c["domain"]]
        errs = []
        if not claude_key:  errs.append("Claude API Key 未設定")
        if not main_kw:     errs.append("メインKW を入力してください")
        if not genre:       errs.append("ジャンル を入力してください")
        if article_type == "商標" and not valid_clinics:
            errs.append("商標記事：案件名とドメインを入力してください")
        for e in errs:
            st.error(e)

        if not errs:
            # サイトパーツ構築
            _single_site_parts = ""
            _single_site_config = {}
            if selected_site_for_parts != "（なし）":
                _sc = site_config_manager.load_site_config(selected_site_for_parts, _site_cfg_creds, _site_cfg_parent_folder)
                _single_site_parts = site_config_manager.format_site_parts(_sc.get("components", []))
                _single_site_config = _sc

            _cb_texts   = [cb["text"].strip()  for cb in st.session_state.get("custom_blocks", []) if cb["text"].strip()]
            _cb_intents = [cb["intent"].strip() for cb in st.session_state.get("custom_blocks", []) if cb["intent"].strip()]
            combined_block  = "\n".join(filter(None, [default_block_val] + _cb_texts))
            combined_intent = "\n".join(_cb_intents)
            _first_valid = next((c for c in st.session_state.test_clinics if c["name"] and c["domain"]), None)
            # 商標記事：強み①〜③をappeal_pointsに変換
            _appeal_points = []
            if article_type == "商標":
                for _ssi, _ss in enumerate(st.session_state.get("t_trademark_strengths", [])):
                    if _ss.get("point", "").strip():
                        _pt = _ss["point"].strip()
                        _bs = _ss.get("basis", "").strip()
                        _appeal_points.append(f"強み{_ssi+1}: {_pt}" + (f"（根拠: {_bs}）" if _bs else ""))
                # 補完情報をcustom_blockに追加
                _tm_supp = st.session_state.get("t_trademark_supplement", "").strip()
                if _tm_supp:
                    combined_block = "\n\n".join(filter(None, [combined_block, f"【案件補完情報（記事生成に使用・そのまま引用しない）】\n{_tm_supp}"]))
                # 参考URLをcompetitor_urlsに追加（最優先スクレイプ）
                _tm_ref_url = st.session_state.get("t_trademark_ref_url", "").strip()
                if _tm_ref_url:
                    competitor_urls = [_tm_ref_url] + [u for u in competitor_urls if u != _tm_ref_url]
            inputs = {
                "article_type":    article_type,
                "site_name":       site_name,
                "main_kw":         main_kw,
                "sub_kw":          [k.strip() for k in sub_kw.split(",") if k.strip()],
                "genre":           genre,
                "recommended":     _first_valid["recommended"].strip() if _first_valid else "",
                "custom_block":    combined_block,
                "custom_intent":   combined_intent,
                "related_kw":      related_kw,
                "clinics":         valid_clinics,
                "competitor_urls": competitor_urls,
                "selected_topics": selected_topics,
                "user_awareness":  st.session_state.get("t_user_awareness", "").strip(),
                "clinic_count":    clinic_count,
                "appeal_points":   _appeal_points,
            }
            _t2_write_needed = False
            st.session_state.pop("t2_draft", None)  # モード切替時に古いdraftをクリア
            with st.status("生成中...", expanded=True) as s:
                try:
                    st.write("🔍 競合分析中...")
                    comp = analyze_competitors(competitor_urls, claude_key, gemini_api_key=gemini_key, research_provider=research_provider)
                    if article_type == "商標":
                        # 商標記事は自動探索を行わず、ユーザー指定の1院のみ使用
                        all_clinics = valid_clinics[:1]
                        st.write(f"　→ 商標記事のため自動探索スキップ。対象案件: {all_clinics[0]['name'] if all_clinics else '（未指定）'}")
                        inputs["clinic_count"] = 1
                    elif article_type == "ノウハウ":
                        # ノウハウ記事は掲載案件なし・自動探索しない
                        all_clinics = []
                        st.write("　→ ノウハウ記事のため案件探索スキップ")
                    else:
                        st.write("🤖 案件自動探索中...")
                        if competitor_urls:
                            discovered = discover_clinics_from_competitors(
                                comp["raw_pages"], valid_clinics, claude_key, gemini_api_key=gemini_key, research_provider=research_provider
                            )
                        else:
                            discovered = auto_discover_clinics(
                                main_kw, genre, claude_key, valid_clinics, gemini_api_key=gemini_key, research_provider=research_provider
                            )
                        # 院数制限：valid_clinics（必須）は常に保持、discoveredをトリム
                        if clinic_count > 0:
                            n_discover = max(0, clinic_count - len(valid_clinics))
                            discovered = discovered[:n_discover]
                        all_clinics = valid_clinics + discovered
                        if discovered:
                            st.write(f"　→ {len(discovered)} 件を自動追加: {', '.join(c['name'] for c in discovered)}")
                        # 院数不足の場合：auto_discoverで補完 → それでも足りなければ指定数を実際の数に下げる
                        if clinic_count > 0 and len(all_clinics) < clinic_count:
                            _shortfall = clinic_count - len(all_clinics)
                            st.write(f"　→ {_shortfall} 件不足のため追加探索中...")
                            _extra = auto_discover_clinics(
                                main_kw, genre, claude_key, all_clinics,
                                gemini_api_key=gemini_key, research_provider=research_provider
                            )
                            all_clinics = all_clinics + _extra[:_shortfall]
                            if _extra:
                                st.write(f"　→ 補完: {', '.join(c['name'] for c in _extra[:_shortfall])}")
                        # それでも足りない場合は実際の院数に合わせる（空白院を生成させない）
                        if clinic_count > 0 and len(all_clinics) < clinic_count:
                            st.write(f"　→ 件数を {len(all_clinics)} 件に調整（探索結果が{clinic_count}件に届かなかったため）")
                            inputs["clinic_count"] = len(all_clinics)
                    inputs["clinics"] = all_clinics
                    st.write("🔍 案件情報収集中...")
                    _t2_db_creds = _get_gcp_creds(sheets_creds_file)
                    _t2_active_db_url = db_sheet_url if custom_db_type == DB_TYPE_CLINIC else lifestyle_sheet_url
                    _t2_db_cache = clinic_db_manager.build_db_cache([c["name"] for c in all_clinics], genre=genre, creds_data=_t2_db_creds, sheet_url=_t2_active_db_url)
                    if _t2_db_cache:
                        st.write(f"　→ DB参照: {len(_t2_db_cache)} 案件（スクレイピングスキップ）")
                    else:
                        st.write(f"　→ DBヒット: 0件（スクレイピングで取得）")
                    clinics = collect_clinic_info(all_clinics, genre, claude_key, article_type, db_cache=_t2_db_cache, db_type=custom_db_type, gemini_api_key=gemini_key, research_provider=research_provider)
                    st.write("📐 構成生成中...")
                    structure = generate_structure(inputs, comp, clinics, claude_key, gemini_api_key=gemini_key, article_provider=article_provider)
                    if gen_mode == "見出し確認あり":
                        st.session_state["t2_draft"] = {
                            "structure":   structure,
                            "comp":        comp,
                            "clinics":     clinics,
                            "all_clinics": all_clinics,
                            "inputs":      inputs,
                            "site_parts":  _single_site_parts,
                            "site_config": _single_site_config,
                        }
                        s.update(label="✅ 見出し生成完了 — 下で確認してください", state="complete")
                    else:
                        _provider_label = "Gemini Flash" if article_provider == "gemini" else "Claude"
                        st.write(f"✍️ 本文生成中（{_provider_label}）...")
                        output = generate_body(inputs, structure, clinics, claude_key, comp,
                                              site_parts=_single_site_parts, gemini_api_key=gemini_key, article_provider=article_provider)
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
                            "_inputs": {
                                "article_type":   article_type,
                                "site_name":      site_name,
                                "genre":          genre,
                                "main_kw":        main_kw,
                                "sub_kw":         sub_kw,
                                "related_kw":     related_kw,
                                "recommended":    inputs["recommended"],
                                "custom_block":   combined_block,
                                "clinics":        valid_clinics,
                                "competitor_urls": competitor_urls,
                                "tm_strengths":   st.session_state.get("t_trademark_strengths", []) if article_type == "商標" else [],
                            },
                        }
                        s.update(label="✅ 完了", state="complete")
                        _save_output_cache(main_kw, st.session_state["t2_last"])
                        _t2_write_needed = True

                except Exception as e:
                    s.update(label="❌ エラー", state="error")
                    st.error(str(e))

            # ── スプシ書き込み（st.statusの外で実行し、結果を確実に表示）──
            if _t2_write_needed and st.session_state.get("t2_last"):
                _t2_for_write = st.session_state["t2_last"]
                if output_tab_sel != "（書き込まない）" and article_sheet_url:
                    creds_out = _get_gcp_creds(sheets_creds_file)
                    if not creds_out:
                        st.warning("⚠️ スプシ書き込みスキップ：GCP認証が設定されていません（SecretsまたはJSONファイルを確認）")
                    else:
                        with st.spinner(f"📊 [{output_tab_sel}] タブに書き込み中..."):
                            try:
                                _kh_write = output_tab_sel == "ノウハウ"
                                _n_pad = 13 if _kh_write else 16
                                _title_col = COL_TITLE_KNOWHOW if _kh_write else COL_TITLE
                                ws_out = get_sheet(article_sheet_url, creds_out, tab_name=output_tab_sel)
                                _all_vals = ws_out.get_all_values()
                                _target_row = None
                                for _ri, _rd in enumerate(_all_vals[1:], start=2):
                                    _pd = _rd + [""] * (_n_pad - len(_rd))
                                    if _pd[3] == main_kw and not _pd[_title_col]:
                                        _target_row = _ri
                                        break
                                if _target_row is None:
                                    for _ri, _rd in enumerate(_all_vals[1:], start=2):
                                        _pd = _rd + [""] * (_n_pad - len(_rd))
                                        if not _pd[_title_col]:
                                            _target_row = _ri
                                            break
                                if _target_row is None:
                                    _target_row = len(_all_vals) + 1
                                _fw = write_full_row_knowhow if _kh_write else write_full_row
                                _fw(ws_out, _target_row, _t2_for_write.get("_inputs", {}), _t2_for_write)
                            except Exception as we:
                                import traceback as _tb
                                st.error(f"スプシ書き込みエラー: {we}")
                                st.code(_tb.format_exc())
                            else:
                                st.success(f"✅ [{output_tab_sel}] 行{_target_row}に書き込みました")
                                st.session_state.pop(f"t2_sheet_hist_{article_type}", None)

    # ── 見出し確認フェーズ ──────────────────────────────────────
    _t2_draft = st.session_state.get("t2_draft")
    if _t2_draft:
        st.divider()
        st.subheader("📐 見出し確認・修正")
        st.caption("内容を確認して、修正指示があれば入力してください。問題なければそのまま本文を生成できます。")

        with st.expander("タイトル案・見出し一覧", expanded=True):
            st.code(_t2_draft["structure"]["structure_text"], language=None)

        _rev_note = st.text_area(
            "修正指示（任意）",
            key="t2_revision_input",
            placeholder="例：費用のH2は後ろに移動して\n例：クリニック紹介を5件にして\n例：「副作用リスク」のH2を追加して",
            height=100,
        )

        _rbtn1, _rbtn2 = st.columns(2)
        if _rbtn1.button("✏️ 修正を反映", key="t2_revise_btn", disabled=not (_rev_note or "").strip()):
            with st.spinner("構成を修正中..."):
                try:
                    _rv_inputs = {**_t2_draft["inputs"], "_revision_note": _rev_note.strip()}
                    _new_struct = generate_structure(
                        _rv_inputs, _t2_draft["comp"], _t2_draft["clinics"],
                        claude_key, gemini_api_key=gemini_key, article_provider=article_provider,
                    )
                    st.session_state["t2_draft"] = {**_t2_draft, "structure": _new_struct}
                    st.session_state["t2_revision_input"] = ""
                    # Drive に構成修正ログを保存（失敗してもメインフローは止めない）
                    _struct_log_creds = _get_gcp_creds(sheets_creds_file)
                    if _struct_log_creds:
                        try:
                            _sl_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                            _sl_kw = _t2_draft["inputs"].get("main_kw", "")[:20].replace(" ", "_")
                            _sl_atype = _t2_draft["inputs"].get("article_type", "不明")
                            drive_uploader.upload_json(
                                {
                                    "date": datetime.date.today().isoformat(),
                                    "article_type": _sl_atype,
                                    "main_kw": _t2_draft["inputs"].get("main_kw", ""),
                                    "instruction": _rev_note.strip(),
                                    "before_structure": _t2_draft["structure"]["structure_text"],
                                    "after_structure": _new_struct["structure_text"],
                                },
                                f"struct_{_sl_ts}_{_sl_kw}.json",
                                ["修正ログ", "構成", _sl_atype],
                                _struct_log_creds,
                                _edit_logs_folder_id,
                            )
                        except Exception:
                            pass
                    st.rerun()
                except Exception as _re:
                    st.error(f"修正エラー: {_re}")

        if _rbtn2.button("✍️ この見出しで本文を生成", key="t2_gen_body_btn", type="primary"):
            _prov_label = "Gemini Flash" if article_provider == "gemini" else "Claude"
            with st.status(f"本文生成中（{_prov_label}）...", expanded=True) as _bs:
                try:
                    _di = _t2_draft["inputs"]
                    _ds = _t2_draft["structure"]
                    output = generate_body(
                        _di, _ds, _t2_draft["clinics"],
                        claude_key, _t2_draft["comp"],
                        site_parts=_t2_draft["site_parts"],
                        gemini_api_key=gemini_key,
                        article_provider=article_provider,
                    )
                    st.session_state["t2_last"] = {
                        "html":           output["html"],
                        "title":          _ds["title"],
                        "meta":           _ds["meta"],
                        "todo_list":      output["todo_list"],
                        "structure_text": _ds["structure_text"],
                        "site_config":    _t2_draft["site_config"],
                        "site_name":      _di.get("site_name", ""),
                        "main_kw":        _di.get("main_kw", ""),
                        "debug":          output.get("debug"),
                        "clinics":        _t2_draft["all_clinics"],
                        "_inputs": {
                            "article_type":    _di.get("article_type", ""),
                            "site_name":       _di.get("site_name", ""),
                            "genre":           _di.get("genre", ""),
                            "main_kw":         _di.get("main_kw", ""),
                            "sub_kw":          ", ".join(_di["sub_kw"]) if isinstance(_di.get("sub_kw"), list) else _di.get("sub_kw", ""),
                            "related_kw":      _di.get("related_kw", ""),
                            "recommended":     _di.get("recommended", ""),
                            "custom_block":    _di.get("custom_block", ""),
                            "clinics":         _t2_draft["all_clinics"],
                            "competitor_urls": _di.get("competitor_urls", []),
                            "tm_strengths":    st.session_state.get("t_trademark_strengths", []) if _di.get("article_type") == "商標" else [],
                        },
                    }
                    _bs.update(label="✅ 完了", state="complete")
                    _save_output_cache(_di.get("main_kw", ""), st.session_state["t2_last"])
                    st.session_state.pop("t2_draft", None)
                    st.session_state.pop(f"t2_sheet_hist_{_di.get('article_type', '')}", None)
                except Exception as _be:
                    _bs.update(label="❌ エラー", state="error")
                    st.error(str(_be))
            # スプシ書き込み（本文生成成功時のみ）
            if st.session_state.get("t2_last") and output_tab_sel != "（書き込まない）" and article_sheet_url:
                _creds_draft = _get_gcp_creds(sheets_creds_file)
                if _creds_draft:
                    with st.spinner(f"📊 [{output_tab_sel}] タブに書き込み中..."):
                        try:
                            _kh_d = output_tab_sel == "ノウハウ"
                            _np_d = 13 if _kh_d else 16
                            _tc_d = COL_TITLE_KNOWHOW if _kh_d else COL_TITLE
                            _ws_d = get_sheet(article_sheet_url, _creds_draft, tab_name=output_tab_sel)
                            _av_d = _ws_d.get_all_values()
                            _di_kw = st.session_state["t2_last"].get("main_kw", "")
                            _tr_d = None
                            for _ri_d, _rd_d in enumerate(_av_d[1:], start=2):
                                _pd_d = _rd_d + [""] * (_np_d - len(_rd_d))
                                if _pd_d[3] == _di_kw and not _pd_d[_tc_d]:
                                    _tr_d = _ri_d
                                    break
                            if _tr_d is None:
                                for _ri_d, _rd_d in enumerate(_av_d[1:], start=2):
                                    _pd_d = _rd_d + [""] * (_np_d - len(_rd_d))
                                    if not _pd_d[_tc_d]:
                                        _tr_d = _ri_d
                                        break
                            if _tr_d is None:
                                _tr_d = len(_av_d) + 1
                            _t2lfw = st.session_state["t2_last"]
                            _fw_d = write_full_row_knowhow if _kh_d else write_full_row
                            _fw_d(_ws_d, _tr_d, _t2lfw.get("_inputs", {}), _t2lfw)
                            st.success(f"✅ [{output_tab_sel}] 行{_tr_d}に書き込みました")
                            st.session_state.pop(f"t2_sheet_hist_{article_type}", None)
                        except Exception as _we_d:
                            st.error(f"スプシ書き込みエラー: {_we_d}")


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
            _tl_raw = _t2_last["todo_list"]
            if "---参考文献候補---" in _tl_raw:
                _tl_main, _tl_refs = _tl_raw.split("---参考文献候補---", 1)
                if _tl_main.strip():
                    st.warning("**[要確認]リスト**\n" + _tl_main.strip())
                st.info("**📚 参考文献候補**\n" + _tl_refs.strip())
            else:
                st.warning("**[要確認]リスト**\n" + _tl_raw)
        # ── H2ブロック編集UI ─────────────────────────────────────
        _html_hash = hashlib.md5(_t2_last["html"].encode()).hexdigest()
        if st.session_state.get("t2_h2_blocks_hash") != _html_hash:
            st.session_state["t2_h2_blocks"] = _split_html_by_h2(_t2_last["html"])
            st.session_state["t2_h2_blocks_hash"] = _html_hash
        _h2_blocks = st.session_state["t2_h2_blocks"]

        _h2_split_method = "マーカー" if any("H2_BLOCK_START" in b["html"] for b in _h2_blocks) else ("<h2>タグ" if len(_h2_blocks) > 1 else "分割不可")
        st.subheader(f"H2ブロック編集（{len(_h2_blocks)}ブロック / 分割方式: {_h2_split_method}）")
        _h2_regen_inputs = {**_t2_last.get("_inputs", {}), "main_kw": _t2_last["main_kw"]}

        for _bi, _block in enumerate(_h2_blocks):
            _is_confirmed = _block["confirmed"]
            _is_modified = _block.get("modified", False)
            _status_icon = "✅" if _is_confirmed else ("✏️" if _is_modified else "⬜")
            with st.expander(f"{_status_icon} {_block['title']}", expanded=not _is_confirmed):
                st.code(_block["html"], language="html")

                _dl_col, _ = st.columns([2, 5])
                with _dl_col:
                    st.download_button(
                        "📥 このH2をDL",
                        _block["html"].encode("utf-8"),
                        file_name=f"h2_{_bi+1}_{_block['title'][:20].replace(' ','_')}.html",
                        mime="text/html",
                        key=f"t2_h2_dl_{_bi}",
                    )

                st.text_area(
                    "修正指示",
                    value=_block.get("instruction", ""),
                    key=f"t2_h2_instr_{_bi}",
                    placeholder="例：この段落の具体例をもっと増やしてほしい",
                    height=80,
                )

                _rcol1, _rcol2 = st.columns(2)
                if _rcol1.button("🔄 このH2を再生成", key=f"t2_h2_regen_{_bi}"):
                    _current_instr = st.session_state.get(f"t2_h2_instr_{_bi}", "").strip()
                    if not _current_instr:
                        st.warning("修正指示を入力してください")
                    else:
                        with st.spinner("再生成中..."):
                            try:
                                _new_html = _regenerate_h2_block(
                                    _bi, _h2_blocks, _current_instr,
                                    _h2_regen_inputs, _t2_last["structure_text"], claude_key,
                                )
                                # Drive に修正ログを保存（失敗してもメインフローは止めない）
                                _edit_creds = _get_gcp_creds(sheets_creds_file)
                                if _edit_creds:
                                    try:
                                        _log_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                                        _log_kw = _t2_last["main_kw"][:20].replace(" ", "_")
                                        _log_atype = _t2_last.get("_inputs", {}).get("article_type", "不明")
                                        drive_uploader.upload_json(
                                            {
                                                "date": datetime.date.today().isoformat(),
                                                "article_type": _log_atype,
                                                "main_kw": _t2_last["main_kw"],
                                                "h2_title": _block["title"],
                                                "instruction": _current_instr,
                                                "before_html": _block["original_html"],
                                                "after_html": _new_html,
                                            },
                                            f"edit_{_log_ts}_{_log_kw}.json",
                                            ["修正ログ", "本文", _log_atype],
                                            _edit_creds,
                                            _edit_logs_folder_id,
                                        )
                                    except Exception:
                                        pass
                                _h2_blocks[_bi]["html"] = _new_html
                                _h2_blocks[_bi]["modified"] = True
                                _h2_blocks[_bi]["confirmed"] = False
                                _h2_blocks[_bi]["instruction"] = _current_instr
                                st.session_state["t2_h2_blocks"] = _h2_blocks
                                st.rerun()
                            except Exception as _re:
                                st.error(f"再生成エラー: {_re}")

                _confirm_label = "✅ 確定済み（クリックで解除）" if _is_confirmed else "☑️ この内容で確定"
                if _rcol2.button(_confirm_label, key=f"t2_h2_confirm_{_bi}"):
                    _h2_blocks[_bi]["confirmed"] = not _is_confirmed
                    st.session_state["t2_h2_blocks"] = _h2_blocks
                    st.rerun()

        # ── 全体ダウンロード + スプシ書き出し ───────────────────────
        st.divider()
        _full_html = "\n\n".join(b["html"] for b in _h2_blocks)
        _confirmed_count = sum(1 for b in _h2_blocks if b["confirmed"])

        _btm_col1, _btm_col2 = st.columns(2)
        with _btm_col1:
            st.download_button(
                "📥 記事全体をダウンロード（全H2まとめ）",
                _full_html.encode("utf-8"),
                file_name=f"{_t2_last['main_kw'].replace(' ','_')}_full.html",
                mime="text/html",
                key="t2_dl_full",
            )
        with _btm_col2:
            if output_tab_sel != "（書き込まない）" and article_sheet_url:
                if st.button(
                    f"📊 スプシに書き出す（{_confirmed_count}/{len(_h2_blocks)} 確定）",
                    key="t2_h2_sheet_write",
                ):
                    _h2_write_creds = _get_gcp_creds(sheets_creds_file)
                    if not _h2_write_creds:
                        st.error("Google Sheets 認証情報が未設定です")
                    else:
                        with st.spinner(f"[{output_tab_sel}] タブに書き込み中..."):
                            try:
                                _kh_h2 = output_tab_sel == "ノウハウ"
                                _np_h2 = 13 if _kh_h2 else 16
                                _tc_h2 = COL_TITLE_KNOWHOW if _kh_h2 else COL_TITLE
                                ws_h2 = get_sheet(article_sheet_url, _h2_write_creds, tab_name=output_tab_sel)
                                _h2_all_vals = ws_h2.get_all_values()
                                _h2_target_row = None
                                for _ri, _rd in enumerate(_h2_all_vals[1:], start=2):
                                    _pd = _rd + [""] * (_np_h2 - len(_rd))
                                    if _pd[3] == _t2_last["main_kw"] and not _pd[_tc_h2]:
                                        _h2_target_row = _ri
                                        break
                                if _h2_target_row is None:
                                    for _ri, _rd in enumerate(_h2_all_vals[1:], start=2):
                                        _pd = _rd + [""] * (_np_h2 - len(_rd))
                                        if not _pd[_tc_h2]:
                                            _h2_target_row = _ri
                                            break
                                if _h2_target_row is None:
                                    _h2_target_row = len(_h2_all_vals) + 1
                                _h2_write_inp = dict(_t2_last.get("_inputs", {}))
                                _fw_h2 = write_full_row_knowhow if _kh_h2 else write_full_row
                                _fw_h2(ws_h2, _h2_target_row, _h2_write_inp, {**_t2_last, "html": _full_html})
                                st.success(f"✅ [{output_tab_sel}] 行{_h2_target_row}に書き込みました")
                            except Exception as _we3:
                                import traceback as _tb3
                                st.error(f"スプシ書き込みエラー: {_we3}")
                                st.code(_tb3.format_exc())
            else:
                st.caption("スプシ書き込み先が設定されていません（サイドバーで設定）")

    # 掲載案件一覧（案件ブロックタブ用）
    if _t2_last and _t2_last.get("clinics"):
        st.divider()
        st.subheader("掲載案件一覧（案件ブロック用コピペ）")
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

    # ── 画像生成セクション（サイト設定に画像テンプレートが登録されている場合のみ表示）──
    if _t2_last and _t2_last.get("site_config", {}).get("image_templates"):
        st.divider()
        st.subheader("🖼️ 画像生成")
        st.caption(f"対象記事: {_t2_last['main_kw']}")
        _img_site_config = _t2_last["site_config"]

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
            if image_provider == "dalle" and not openai_key:
                errs_img.append("DALL-E を使うには OpenAI API Key が必要です（サイドバーから入力してください）")
            elif image_provider == "gemini" and not gemini_key:
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
                                _img_site_config,
                                claude_key,
                                _img_slug.strip(),
                            )
                            st.write(f"　→ {len(prompts)} 枚分のプロンプトを生成しました")

                            _img_results = []
                            for i, p in enumerate(prompts):
                                st.write(f"🎨 画像生成中 ({i+1}/{len(prompts)}): {p['filename']}...")
                                if p.get("_unresolved_vars"):
                                    st.warning(
                                        f"⚠️ 変数置換漏れ: {', '.join(p['_unresolved_vars'])} が未置換のままです。"
                                        "テンプレートの変数名を見直すか、再生成してください。"
                                    )
                                img_bytes = image_generator.generate_image_bytes(
                                    p["prompt"],
                                    gemini_api_key=gemini_key,
                                    openai_api_key=openai_key,
                                    provider=image_provider,
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
    check_mode    = st.radio(
        "チェックモード",
        ["standard", "reader_rejection"],
        format_func=lambda x: "標準チェック（フォーマット・ルール）" if x == "standard" else "読者視点チェック（1位を選ばない理由）",
        horizontal=True,
        key="chk_mode",
    )
    check_main_kw = st.text_input("メインKW", key="chk_kw")
    check_sub_kw  = st.text_input("サブKW（カンマ区切り）", key="chk_sub")
    _chk_col1, _chk_col2 = st.columns(2)
    check_title = _chk_col1.text_input("タイトル（任意）", key="chk_title")
    check_meta  = _chk_col2.text_input("メタディスクリプション（任意）", key="chk_meta")
    html_input  = st.text_area("HTMLを貼り付け", height=300, key="chk_html")

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
                    gemini_api_key=gemini_key,
                    article_provider=article_provider,
                    check_mode=check_mode,
                    title=check_title,
                    meta=check_meta,
                )
                st.markdown(result)


# ════════════════════════════════════════════════════════
#  Tab4: 執筆チャット
# ════════════════════════════════════════════════════════
_WRITING_KNOWLEDGE_DIR = pathlib.Path(__file__).parent / "knowledge"

def _load_writing_knowledge(files: list) -> str:
    texts = []
    for fname in files:
        p = _WRITING_KNOWLEDGE_DIR / fname
        if p.exists():
            texts.append(f"## {fname}\n\n{p.read_text(encoding='utf-8')}")
    return "\n\n---\n\n".join(texts)

_WRITING_KNOWLEDGE_FILES = ["writing-rules.md", "content-guidelines.md", "logic-structure.md"]

_WRITING_SYSTEM = """あなたはSEOコンテンツの執筆・添削の専門家です。
医療・美容・ダイエット系SEOメディアの執筆ルールを熟知しています。
以下の社内ナレッジと執筆ルールに基づいて、文章の添削・修正・書き直しを行います。

【社内ナレッジ】
{knowledge}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
執筆ルール（自分の出力にも必ず適用する）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【絶対に使わない語・表現】

▼ 指示語・参照語
「この」「その」「ここ」「そこ」「これ」「次」「以上」「以下」「本記事」「この記事」「このブロック」

▼ 場所参照
「先ほど」「この後」「上記」「下記」「ページ上部」「記事冒頭」「前述の」「次のセクション」「〜で紹介します」「〜をご覧ください」

▼ 橋渡し文・接続詞
「つまり」「それでは」「以下で詳しく解説します」「次の項目をご確認ください」

▼ 推量・疑問形（タイトル・見出し・本文すべて禁止）
「〜でしょう」「〜かもしれません」「〜ではないでしょうか」→ 言い切りに変換

▼ 最上級・誇大表現
「絶対」「完璧」「100%」「必ず」「唯一無二」「世界一」「No.1」「最高」「最強」「完治保証」

▼ AI頻出の抽象ワード（具体に置き換える）
「納得のいく対価」「適切な方法」「傾向」「判断につながります」「〜に役立ちます」

【文体ルール】
- です・ます調で統一
- 1文1メッセージ（1文で複数のことを言わない）
- 同じ語尾を3回以上連続させない（体言止めを交える）
- 主語のない文・主張のない文は削除する

【PREP構造（各段落の基本）】
結論（1文）→ 理由（〜だから）→ 具体例・事実・数値 → 再結論＋So What（だからあなたはこうする）

【医療広告NG表現】
「厚生労働省が推奨」「絶対〜できる」「100%効果がある」「完治保証」「無制限」「し放題」「体験談・Before/After」は使用禁止

【「AIっぽい」「自然にして」と言われたら】
以下を確認して書き直す：
- 推量・疑問形 → 言い切りに変換
- 抽象的なメリット → 具体的な数値・事実・行動に置換
- 橋渡しだけの文・一般論 → 削除して次の文に統合
- 「〜でしょう」「〜ではないでしょうか」 → 「〜です」「〜してください」

【HTMLタグの扱い】
- 入力にHTMLタグが含まれている場合、修正後もHTMLタグを保持したまま返す
- タグ構造は変えない。テキスト部分のみ修正する
- コードブロック（```html）で囲んで返す

【添削フィードバックの形式】
- 問題箇所を引用してから指摘する
- 「なぜNGか」の理由を1行で添える
- 修正案を具体的に出す
- 問題がなければ「問題なし」と明記してから良い点をコメントする
"""

with _safe_tab(tab_writing_chat):
    st.title("✍️ 執筆チャット")
    st.caption("社内ルールが前提として入った状態で壁打ちできます。直したい意図と文章をセットで投げてください。")

    if st.sidebar.button("🗑️ 執筆チャットをリセット", key="wc_reset"):
        st.session_state["wc_messages"] = []
        st.rerun()

    if "wc_messages" not in st.session_state:
        st.session_state["wc_messages"] = []

    if not st.session_state["wc_messages"]:
        st.info(
            "**使い方**　直したい意図と文章をセットで投げてください。文章だけ貼るのはNGです。\n\n"
            "例）「AIっぽい表現を直してほしい ＋ 〔文章〕」\n"
            "例）「PREP構造になってるか確認して ＋ 〔文章〕」\n"
            "例）「この表現、医療広告的にOK？ ＋ 〔文章〕」"
        )

    for _wc_msg in st.session_state["wc_messages"]:
        with st.chat_message(_wc_msg["role"]):
            st.markdown(_wc_msg["content"])

    if _wc_prompt := st.chat_input("直したい意図＋文章を一緒に書いてください。例）「AIっぽいので自然にして」＋文章", key="wc_input"):
        if not claude_key:
            st.error("Claude API Key が未設定です。左のサイドバーで設定してください。")
        else:
            st.session_state["wc_messages"].append({"role": "user", "content": _wc_prompt})
            with st.chat_message("user"):
                st.markdown(_wc_prompt)

            _wc_knowledge = _load_writing_knowledge(_WRITING_KNOWLEDGE_FILES)
            _wc_system = _WRITING_SYSTEM.format(knowledge=_wc_knowledge)

            import anthropic as _anthropic
            _wc_client = _anthropic.Anthropic(api_key=claude_key)

            with st.chat_message("assistant"):
                _wc_response = ""
                _wc_placeholder = st.empty()
                with _wc_client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    system=_wc_system,
                    messages=[
                        {"role": m["role"], "content": m["content"]}
                        for m in st.session_state["wc_messages"]
                    ],
                ) as _wc_stream:
                    for _wc_text in _wc_stream.text_stream:
                        _wc_response += _wc_text
                        _wc_placeholder.markdown(_wc_response + "▌")
                    _wc_placeholder.markdown(_wc_response)

            st.session_state["wc_messages"].append({"role": "assistant", "content": _wc_response})


# ════════════════════════════════════════════════════════
#  Tab5: サイト設定
# ════════════════════════════════════════════════════════
with _safe_tab(tab_settings):
    st.title("⚙️ サイト設定")
    st.caption("サイト別の画像テンプレート・HTMLパーツを登録します。")

    sites_list = site_config_manager.list_sites(_site_cfg_creds, _site_cfg_parent_folder)
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
            _config4 = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
            st.markdown("---")
            if st.button("🗑️ このサイトを削除", key="cfg_del"):
                site_config_manager.delete_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                st.success(f"「{_current_site4}」を削除しました")
                st.rerun()

    with col_right4:
        if not _current_site4:
            st.info("左側でサイトを選択するか、新規サイト名を入力してください。")
        else:
            st.subheader(f"「{_current_site4}」の設定")

            # ── 1. 画像テンプレート ──────────────────────────────────
            st.markdown("### 🖼️ 1. 画像テンプレート")
            _existing_tmpls = _config4.get("image_templates", [])

            # 登録済みテンプレート一覧
            if _existing_tmpls:
                st.caption(f"登録済みテンプレート: {len(_existing_tmpls)} 件")
                for _ei, _et in enumerate(_existing_tmpls):
                    _et_label = _et.get("name") or _et.get("layout_type") or f"テンプレート{_ei+1}"
                    with st.expander(f"📋 {_et_label}"):
                        _et_prompt = image_generator._resolve_template_prompt(_et)
                        st.code(_et_prompt[:500] + ("..." if len(_et_prompt) > 500 else ""), language="")
                        if st.button(f"🗑️ 削除", key=f"del_tmpl_{_current_site4}_{_ei}"):
                            _cfg_now = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                            _cfg_now["image_templates"] = [t for j, t in enumerate(_cfg_now.get("image_templates", [])) if j != _ei]
                            site_config_manager.save_site_config(_current_site4, _cfg_now, _site_cfg_creds, _site_cfg_parent_folder)
                            st.rerun()
            else:
                st.caption("テンプレート未登録")

            st.markdown("**テンプレートを追加**")
            _img_mode = st.radio(
                "追加方法",
                ["md", "upload"],
                format_func=lambda x: {"md": "mdファイルをアップ", "upload": "見本画像からカスタム生成（手動）"}[x],
                horizontal=True,
                key=f"img_mode_{_current_site4}",
            )

            if _img_mode == "md":
                st.caption("プロンプトを記述した .md ファイルをアップすると、ファイル名をテンプレート名として自動登録します。複数ファイル同時アップ可。")
                _md_uploads = st.file_uploader(
                    "mdファイルをアップ（複数選択可）",
                    type=["md", "txt"],
                    accept_multiple_files=True,
                    key=f"t4_md_upload_{_current_site4}",
                )
                if _md_uploads:
                    _md_add_mode = st.radio("保存方式", ["追加（既存を保持）", "上書き（既存を置き換え）"], horizontal=True, key=f"md_add_mode_{_current_site4}")
                    if st.button(f"💾 {len(_md_uploads)}件を登録", key=f"btn_save_md_{_current_site4}", type="primary"):
                        _cfg_now = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                        _new_md_tmpls = []
                        for _mf in _md_uploads:
                            _mf.seek(0)
                            _md_body = _mf.read().decode("utf-8", errors="replace").strip()
                            _md_name = pathlib.Path(_mf.name).stem
                            if _md_body:
                                _new_md_tmpls.append({"base_prompt": _md_body, "name": _md_name})
                        if _new_md_tmpls:
                            if _md_add_mode.startswith("追加"):
                                _cfg_now["image_templates"] = _cfg_now.get("image_templates", []) + _new_md_tmpls
                            else:
                                _cfg_now["image_templates"] = _new_md_tmpls
                            if site_config_manager.save_site_config(_current_site4, _cfg_now, _site_cfg_creds, _site_cfg_parent_folder):
                                st.success(f"✅ {len(_new_md_tmpls)}件を保存しました。")
                                st.rerun()
                            else:
                                st.error("保存に失敗しました。")

            elif _img_mode == "upload":
                st.caption("見本画像をアップすると、Claude Visionで構造を解析してプロンプトテンプレートを自動生成します。複数枚同時アップ可。")
                _t4_img_uploads = st.file_uploader(
                    "画像をアップ（jpg / png / webp・複数選択可）",
                    type=["jpg", "jpeg", "png", "webp"],
                    accept_multiple_files=True,
                    key=f"t4_img_upload_{_current_site4}",
                )
                if _t4_img_uploads:
                    for _uf in _t4_img_uploads:
                        st.image(_uf, width=300, caption=_uf.name)
                    _upload_add_mode = st.radio("保存方式", ["追加（既存を保持）", "上書き（既存を置き換え）"], horizontal=True, key=f"upload_add_mode_{_current_site4}")
                    if st.button(f"✨ {len(_t4_img_uploads)}枚からテンプレートを生成して保存", key=f"btn_gen_tmpl_{_current_site4}", type="primary"):
                        if not claude_key:
                            st.error("Claude API Key が未設定です（サイドバーから入力してください）")
                        else:
                            _cfg_now = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                            _generated_tmpls = []
                            with st.spinner(f"画像を解析中（{len(_t4_img_uploads)}枚）..."):
                                for _uf in _t4_img_uploads:
                                    try:
                                        _uf.seek(0)
                                        _t4_mime = _uf.type or "image/png"
                                        _t4_img_bytes = _uf.read()
                                        _t4_generated = image_generator.generate_template_from_image(
                                            _t4_img_bytes, _t4_mime, _cfg_now, claude_key
                                        )
                                        _generated_tmpls.append({"base_prompt": _t4_generated, "name": pathlib.Path(_uf.name).stem})
                                        st.write(f"✅ {_uf.name} 完了")
                                    except Exception as _t4_e:
                                        st.error(f"{_uf.name} 生成エラー: {_t4_e}")
                            if _generated_tmpls:
                                if _upload_add_mode.startswith("追加"):
                                    _cfg_now["image_templates"] = _cfg_now.get("image_templates", []) + _generated_tmpls
                                else:
                                    _cfg_now["image_templates"] = _generated_tmpls
                                if site_config_manager.save_site_config(_current_site4, _cfg_now, _site_cfg_creds, _site_cfg_parent_folder):
                                    st.success(f"✅ {len(_generated_tmpls)}件のテンプレートを保存しました。")
                                    st.rerun()
                                else:
                                    st.error("保存に失敗しました。")

            st.markdown("---")

            # ── 2. HTMLパーツ ────────────────────────────────────────
            st.markdown("### 🧩 2. HTMLパーツ")
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
                    _cfg_now = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                    if "追記" in _import_mode:
                        _existing_names = {c["name"] for c in _cfg_now.get("components", [])}
                        _merged = _cfg_now.get("components", []) + [c for c in _parsed_components if c["name"] not in _existing_names]
                        _cfg_now["components"] = _merged
                    else:
                        _cfg_now["components"] = _parsed_components
                    if site_config_manager.save_site_config(_current_site4, _cfg_now, _site_cfg_creds, _site_cfg_parent_folder):
                        st.success(f"{len(_parsed_components)} 件をインポートしました。ページをリロードして確認してください。")
                        st.rerun()
                    else:
                        st.error("保存に失敗しました。")
            st.markdown("---")
            with st.form(f"site_form_{_current_site4}"):
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
                _cfg_now = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                _cfg_now["components"] = _updated_comps
                if site_config_manager.save_site_config(_current_site4, _cfg_now, _site_cfg_creds, _site_cfg_parent_folder):
                    st.success(f"「{_current_site4}」の設定を保存しました。")
                    st.rerun()
                else:
                    st.error("保存に失敗しました。")

            # ── 案件ブロックテンプレート管理 ─────────────────────
            st.markdown("---")
            st.markdown("### 📋 3. 案件ブロックテンプレート")
            st.caption("おすすめ案件紹介ブロックの構成・形式をテンプレートとして登録します。")

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

                        st.caption("上位3件のリンク設置箇所")
                        _cbt_existing_links = _cbt.get("top3_link_placements", [])
                        _cbt_links = []
                        for _lk, _ll in [("heading", "見出し案件名"), ("spec_image", "スペック画像"), ("cta_button", "CTAボタン")]:
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

                st.markdown("**＋ 新規案件ブロックテンプレート**")
                _new_cbt_name = st.text_input("テンプレート名", key=f"new_cbt_name_{_current_site4}", placeholder="地域記事案件ブロック（例）")
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

                _cb_submitted = st.form_submit_button("💾 案件ブロックテンプレートを保存", type="primary")

            if _cb_submitted:
                _cb_save_config = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                _cb_save_config["clinic_block_templates"] = _updated_cb_tmpls
                if site_config_manager.save_site_config(_current_site4, _cb_save_config, _site_cfg_creds, _site_cfg_parent_folder):
                    st.success("案件ブロックテンプレートを保存しました。")
                    st.rerun()
                else:
                    st.error("保存に失敗しました。")


# ════════════════════════════════════════════════════════
#  Tab5: ランキングブロック
# ════════════════════════════════════════════════════════
with _safe_tab(tab_rank):
    st.title("🏥 ランキングブロック")
    st.caption("おすすめ紹介ブロックのHTMLを案件ごとに生成します。「カスタム記事作成」タブの「掲載案件一覧」をコピペして使ってください。")

    _cb_sites = site_config_manager.list_sites(_site_cfg_creds, _site_cfg_parent_folder)
    _cb_site_opts = ["（なし）"] + _cb_sites
    _cb_sel_site = st.selectbox("サイトを選択（テンプレート読込）", _cb_site_opts, key="cb_site_sel")

    _cb_site_cfg = {}
    _cb_templates = []
    _cb_template_names = []
    if _cb_sel_site != "（なし）":
        _cb_site_cfg = site_config_manager.load_site_config(_cb_sel_site, _site_cfg_creds, _site_cfg_parent_folder)
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
        st.info("サイトに案件ブロックテンプレートが登録されていません。先にサイト設定タブで登録してください。")

    st.divider()

    _cb_kw_col1, _cb_kw_col2 = st.columns(2)
    _cb_main_kw = _cb_kw_col1.text_input("メインKW", key="cb_main_kw")
    _cb_sub_kw  = _cb_kw_col2.text_input("サブKW（カンマ区切り）", key="cb_sub_kw")
    _cb_opt_col1, _cb_opt_col2 = st.columns([3, 1])
    _cb_db_type = _cb_opt_col1.selectbox("DBタイプ", [DB_TYPE_CLINIC, DB_TYPE_LIFESTYLE], key="cb_db_type")
    _cb_clinic_count = int(_cb_opt_col2.number_input(
        "件数（任意）", min_value=0, value=0, step=1, key="cb_clinic_count",
        help="生成するブロック数を指定。0で全件生成。記事の掲載件数と揃えてください。",
    ))
    _cb_criteria = st.text_area(
        "記事内の「選び方」セクション（文章をそのまま貼り付け）",
        height=120, key="cb_criteria",
        placeholder="記事内の「○○の選び方」セクションの文章をそのまま貼り付けてください。",
    )

    st.divider()
    st.subheader("掲載案件一覧")
    st.caption("カスタム作成タブで記事生成後、スプシのP列（掲載案件一覧）の内容をコピーして貼り付けてください。")
    _cb_clinic_paste = st.text_area(
        "",
        height=150, key="cb_clinic_paste",
        label_visibility="collapsed",
        placeholder="1. TCB東京中央美容外科 大阪院::https://tcb.net/osaka\n2. 湘南美容クリニック 梅田院::https://s-b-c.net/\n3. 品川スキンクリニック 大阪院::[要確認]",
    )

    if st.button("📋 案件リストを読み込む", key="cb_parse_btn"):
        if _cb_clinic_paste.strip():
            st.session_state["cb_clinics"] = clinic_block_writer.parse_clinic_list(_cb_clinic_paste)
            st.rerun()
        else:
            st.warning("案件リストを貼り付けてください")

    _cb_clinics = st.session_state.get("cb_clinics", [])

    if _cb_clinics:
        st.caption(f"読み込み完了: {len(_cb_clinics)} 案件")
        st.divider()
        st.subheader("各案件の入力情報")

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
        _cb_gen_all = st.button("🚀 全案件のブロックを生成", type="primary", use_container_width=True, key="cb_gen_all")

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
                _cb_reference_html = ""  # 1院目のHTMLをフォーマット参照として後続院に渡す
                _cb_clinics_to_gen = _cb_clinics[:_cb_clinic_count] if _cb_clinic_count > 0 else _cb_clinics
                with st.status("案件ブロック生成中...", expanded=True) as _cb_status:
                    for _cbc in _cb_clinics_to_gen:
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
                                gemini_api_key=gemini_key, research_provider=research_provider,
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
                                reference_html=_cb_reference_html,
                            )
                            if not _cb_reference_html:
                                _cb_reference_html = _html
                            _cb_results.append({"rank": _r, "name": _cbc["name"], "html": _html})
                        except Exception as _e:
                            st.warning(f"{_r}位 ({_cbc['name']}) でエラー: {_e}")

                    _cb_status.update(label=f"✅ {len(_cb_results)} 件のブロックを生成しました", state="complete")

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
            "📥 全件まとめてダウンロード",
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
        _url_label = "案件DB" if _db_type_sel == DB_TYPE_CLINIC else "ライフスタイルDB"
        st.warning(f"サイドバーで「{_url_label} スプレッドシートURL」を入力してください。未設定の場合はローカルJSONに保存されます（Streamlit Cloud再起動で消えます）。")
    elif not _db_creds:
        st.warning("Google Sheets 認証が未設定です。ローカルJSONにフォールバックします。")
    else:
        st.caption(f"✅ Google Sheets DB に接続中　｜　URL: `{_active_db_url[:60]}...`")

    # ── 新規追加フォーム ──────────────────────────────────
    st.subheader("＋ 新規追加")

    _existing_genres = list(clinic_db_manager.load_db(creds_data=_db_creds, sheet_url=_active_db_url).keys())
    _genre_options = _existing_genres + ["＋ 新規ジャンル"]
    _db_genre_sel = st.selectbox("ジャンル", _genre_options, key="db_genre_sel")
    if _db_genre_sel == "＋ 新規ジャンル":
        _db_new_genre = st.text_input("新規ジャンル名", placeholder="例：AGA治療", key="db_new_genre_input")
    else:
        _db_new_genre = _db_genre_sel

    with st.form("db_add_form"):
        _db_fa, _db_fb = st.columns([2, 2])
        _db_new_name   = _db_fa.text_input("案件名（クリニック名・商品名等）", placeholder="TCB東京中央美容外科")
        _db_new_domain = _db_fb.text_input("メインURL（ドメイン or パス指定）", placeholder="tcb.net  または  tcb.net/osaka/umeda/")
        _db_new_extra_urls = st.text_area(
            "追加クロールURL（任意・1行1URL）",
            placeholder="https://tcb.net/price/\nhttps://tcb.net/payment/\n料金ページや支払いページのURLを直接指定すると取得精度が上がります",
            height=90,
            key="db_new_extra_urls",
        )
        _db_add_now = st.form_submit_button("追加してクロール", type="primary", use_container_width=True)

    if _db_add_now:
        _errs_db = []
        if not _db_new_name.strip():
            _errs_db.append("案件名を入力してください")
        if not _db_new_domain.strip():
            _errs_db.append("URL / ドメインを入力してください")
        if not _db_new_genre.strip():
            _errs_db.append("ジャンルを入力してください")
        if not claude_key:
            _errs_db.append("Claude API Key が未設定です")
        for _e in _errs_db:
            st.error(_e)

        if not _errs_db:
            _name_new = _db_new_name.strip()
            _domain_new = _db_new_domain.strip()
            _genre_new = _db_new_genre.strip()

            _extra_urls_list = [u.strip() for u in _db_new_extra_urls.splitlines() if u.strip() and u.strip().startswith("http")]
            with st.status(f"{_name_new} のサイトをクロール中...", expanded=True) as _add_status:
                try:
                    _start_url = _domain_new if _domain_new.startswith("http") else f"https://{_domain_new}"
                    st.write("🔍 メインURLをクロール中（最大20ページ）...")
                    _content_new = crawl_site(_start_url, _genre_new, max_pages=20)
                    for _eu in _extra_urls_list:
                        st.write(f"🔍 追加URL取得中: {_eu}")
                        _eu_content = fetch_page_text(_eu)
                        if not _eu_content.startswith("[取得失敗"):
                            _content_new += f"\n\n--- 追加URL: {_eu} ---\n{_eu_content}"
                    _provider_label_db = "Gemini Flash" if research_provider == "gemini" else "Claude Sonnet"
                    st.write(f"🤖 「{_genre_new}」向けに情報抽出中（{_provider_label_db}）...")
                    _info_new = extract_clinic_info_from_content(_content_new, _name_new, _genre_new, claude_key, db_type=_db_type_sel, gemini_api_key=gemini_key, research_provider=research_provider)
                    clinic_db_manager.upsert_clinic(_name_new, _domain_new, _genre_new, _info_new, creds_data=_db_creds, sheet_url=_active_db_url)
                    _add_status.update(label=f"✅ 「{_name_new}」を「{_genre_new}」に追加しました", state="complete")
                    st.rerun()
                except Exception as _e_new:
                    import traceback as _tb
                    _add_status.update(label="❌ エラー", state="error")
                    st.error(f"取得エラー: {type(_e_new).__name__}: {_e_new}")
                    st.code(_tb.format_exc())

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
                                            _ci = extract_clinic_info_from_content(_content_b, _dn, _cg, claude_key, db_type=_db_type_sel, gemini_api_key=gemini_key, research_provider=research_provider)
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
                            _d_info = _de.get("info", "")
                            _d_info_edited = st.text_area(
                                "取得済み情報（直接編集可）",
                                value=_d_info,
                                height=200,
                                key=f"db_info_{_g_name}_{_dn}",
                            )
                            if st.button("💾 この内容で保存", key=f"db_save_manual_{_g_name}_{_dn}"):
                                clinic_db_manager.upsert_clinic(
                                    _dn, _de.get("domain", ""), _g_name, _d_info_edited,
                                    creds_data=_db_creds, sheet_url=_active_db_url,
                                )
                                st.success("保存しました")
                                st.rerun()
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
                                                _ci2 = extract_clinic_info_from_content(_content2, _dn, _cg2, claude_key, db_type=_db_type_sel, gemini_api_key=gemini_key, research_provider=research_provider)
                                                clinic_db_manager.upsert_clinic(_dn, _dom2, _cg2, _ci2, creds_data=_db_creds, sheet_url=_active_db_url)
                                            st.success(f"再取得完了（{len(_clinic_genres2)} ジャンル更新）")
                                            st.rerun()
                                        except Exception as _rr_e:
                                            st.error(f"エラー: {_rr_e}")
                            if _rc2.button(f"🗑️ このジャンルから削除", key=f"db_del_{_g_name}_{_dn}"):
                                clinic_db_manager.delete_clinic(_dn, genre=_g_name, creds_data=_db_creds, sheet_url=_active_db_url)
                                st.success(f"「{_dn}」を「{_g_name}」から削除しました")
                                st.rerun()


# ════════════════════════════════════════════════════════
#  ヘルプタブ
# ════════════════════════════════════════════════════════
_HELP_SYSTEM = """あなたは「CV Article Writer」というSEO記事生成ツールのサポートアシスタントです。
ユーザーはこのツールを使って医療・美容・ライフスタイル等のジャンルでCV最適化された記事を生成しています。
疑問に対してフレンドリーかつ簡潔に答えてください。

## ツール概要
Streamlit上で動作し、Google スプレッドシートと連携してSEO記事（HTML）を自動生成するツールです。

## 画面構成
- 「コンテンツ作成」セクション
  - 一括作成：スプレッドシートの入力行を一括処理して記事を自動生成
  - カスタム作成：1記事ずつ手動で設定して単発生成
  - ランキングブロック：掲載案件のランキングHTMLブロックを単体生成
  - 品質チェック：生成済みHTMLをAIで自動チェック
- 「データ・設定」セクション
  - 商品データベース：案件（クリニック・サービス等）の情報を事前登録
  - サイト設定：サイト固有のHTMLパーツ（ヘッダー・フッター等）を登録

## 記事タイプ
- 地域記事：「〇〇（施術名） 地域名」などのローカルKW向け。複数案件を掲載
- 比較記事：複数案件を比較・おすすめ紹介する記事
- 商標記事：特定クリニック・ブランドの指名KW向け記事
- ノウハウ記事：施術や症状などの情報提供KW向け。案件掲載なし

## 主な入力フィールドの説明

### 基本情報
- サイト名：掲載するサイト名。サイト設定で登録済みの名前と一致するとHTMLパーツが自動適用される
- ジャンル：記事のジャンル（例：クマ取り、AGA治療）。案件DBの検索にも使用される
- メインKW：この記事で上位表示させたい主要キーワード（必須）
- サブKW：一緒に含めたいキーワード。カンマ区切りで複数指定可能
- 関連KW：構成の網羅性チェックに使うキーワード。改行区切り。サーチコンソールからそのままコピペ可

### 掲載案件（地域・比較・商標記事で使用）
- 案件名：掲載するクリニック・サービスの正式名称
- ドメイン：そのクリニック・サービスのドメイン（例：tcb.net）。スクレイピングで情報収集に使用
- おすすめ：最も推したい案件に「はい」等を入力。ランキング1位・最訴求として扱われる
- アピールポイント：その案件の特徴メモ。記事生成の参考情報として使われる

### スプレッドシートのF列フォーマット（一括作成用）
`案件名::ドメイン::おすすめフラグ::アピールポイント` の形式で入力します。
例：`TCB東京中央美容外科::tcb.net::最訴求::院数業界最多`
複数案件はカンマ区切りで並べます。
例：`TCB東京中央美容外科::tcb.net::最訴求::院数業界最多, 湘南美容外科::s-b-c.net:::`

### 競合URL（G列）
構成を参考にする競合サイトのURL。カンマ区切りで複数指定可能。未指定でもAIが自動探索します。

### 追加指示（H列）
記事生成時の特別な指示やこだわり。「設定タブ」で記事タイプ別にデフォルト追加指示も設定できます。

### 最訴求プラン（I列）
最もCVさせたいプラン・案件の詳細情報。商標・比較記事で特に有効。

## 商品データベースについて
案件のドメインや補足情報を事前登録しておくと、記事生成時のスクレイピングをスキップできます。
ジャンル別にタブが分かれており、一度登録した情報は全記事タイプ・全サイトで共有されます。
「DBタイプ」で「クリニック系」と「ライフスタイル系」を切り替えられます。

## よくある質問
Q: スプレッドシートに何も入力していないのに記事は作れますか？
A: カスタム作成タブならスプシなしで単発生成できます。一括作成はスプシが必要です。

Q: 競合URLを入れないとどうなりますか？
A: AIが自動でウェブ検索して競合構成を参照します。精度を上げたい場合は手動で入力推奨です。

Q: 個人情報や社外秘の情報を入力しても大丈夫ですか？
A: 入力内容はClaude API（Anthropic社）に送信されます。Anthropicのプライバシーポリシーに従って処理されます。機密性の高い情報（個人名・契約金額等）は入力しないことを推奨します。掲載案件名・ドメイン・キーワードは通常の業務情報として問題ありません。

Q: 生成した記事はどこに保存されますか？
A: スプレッドシートの指定タブ（L〜P列）に書き出されます。カスタム作成では画面上でHTML編集・ダウンロードも可能です。

Q: 記事タイプを間違えて生成してしまいました。
A: カスタム作成の場合、タブ上部の「登録情報履歴」から入力条件を復元して再生成できます。スプシに書き出した場合は、スプシの該当行を修正して一括作成で再処理するか、カスタム作成で上書きしてください。
"""

if tab_help:
    st.title("❓ ヘルプ")
    st.caption("使い方の疑問・入力内容の確認など、なんでも聞いてください。")

    if not claude_key:
        st.warning("Claude API Key が設定されていないとヘルプチャットは使用できません。サイドバーで設定してください。")
    else:
        import anthropic as _ant_help

        if "help_messages" not in st.session_state:
            st.session_state["help_messages"] = []

        for _hm in st.session_state["help_messages"]:
            with st.chat_message(_hm["role"]):
                st.markdown(_hm["content"])

        if _help_input := st.chat_input("使い方や入力内容について質問してください"):
            st.session_state["help_messages"].append({"role": "user", "content": _help_input})
            with st.chat_message("user"):
                st.markdown(_help_input)

            with st.chat_message("assistant"):
                with st.spinner("考え中..."):
                    _help_client = _ant_help.Anthropic(api_key=claude_key)
                    _help_resp = _help_client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=1024,
                        system=_HELP_SYSTEM,
                        messages=[
                            {"role": m["role"], "content": m["content"]}
                            for m in st.session_state["help_messages"]
                        ],
                    )
                    _help_reply = _help_resp.content[0].text
                st.markdown(_help_reply)
            st.session_state["help_messages"].append({"role": "assistant", "content": _help_reply})

        if st.session_state.get("help_messages"):
            if st.button("🗑️ 会話をリセット", key="help_reset"):
                st.session_state["help_messages"] = []
                st.rerun()
