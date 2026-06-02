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
    extract_text_from_lp_images, build_content_with_lp,
    extract_clinic_names_from_article,
    DB_TYPE_CLINIC, DB_TYPE_LIFESTYLE,
)
from core.planner import generate_structure
from core.writer import generate_body, quality_check, heading_structure_check, extract_criteria_summary, inject_images_into_html
from core.sheets import (
    read_input_rows, write_output_row, write_full_row, write_status, get_sheet,
    get_settings_sheet, read_defaults, ARTICLE_TABS,
    get_worksheet_readonly, read_recent_input_rows,
    read_input_rows_knowhow, read_recent_input_rows_knowhow,
    write_status_knowhow, write_output_row_knowhow, write_full_row_knowhow,
    read_input_rows_knowhow_bulk, write_status_knowhow_bulk, write_output_row_knowhow_bulk,
    COL_TITLE, COL_TITLE_KNOWHOW, read_notation_rules,
    write_input_only_row, read_row_by_index,
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
_drive_folder_id          = _secret("DRIVE_PARENT_FOLDER_ID", "0ANR02wEPgx88Uk9PVA")
_site_cfg_parent_folder   = _drive_folder_id
_site_cfg_direct_id       = _secret("SITE_CONFIG_FOLDER_ID", "")
if _site_cfg_direct_id:
    site_config_manager.SITE_CONFIG_FOLDER_ID_OVERRIDE = _site_cfg_direct_id
_article_sheet_url_default         = _secret("ARTICLE_SHEET_URL")
_db_sheet_url_default              = _secret("CLINIC_DB_SHEET_URL")
_lifestyle_sheet_url_default       = _secret("LIFESTYLE_DB_SHEET_URL")
_notation_rules_sheet_url_default  = _secret("NOTATION_RULES_SHEET_URL", "1h6BBETAdRTfGsOFBCxSlS9M6gzQ30KlBx53wRUAjB84")

# ── サイドバー：設定 ──────────────────────────────────────
with st.sidebar:
    st.header("カテゴリ")
    st.radio(
        "セクション",
        ["📝 コンテンツ作成", "🗄️ データ・設定", "🖼️ 画像生成", "❓ ヘルプ"],
        key="main_nav",
        label_visibility="collapsed",
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
        ["gemini", "claude"],
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
    tab_cases = tab_settings = tab_help = tab_image_gen = None
elif _main_nav == "🗄️ データ・設定":
    tab_cases, tab_settings = st.tabs(["🗄️ 商品データベース", "⚙️ サイト設定"])
    tab_batch = tab_custom = tab_rank = tab_qual = tab_writing_chat = tab_help = tab_image_gen = None
elif _main_nav == "🖼️ 画像生成":
    tab_image_gen = True  # タブなしで直接描画
    tab_batch = tab_custom = tab_rank = tab_qual = tab_writing_chat = tab_cases = tab_settings = tab_help = None
else:  # ❓ ヘルプ
    tab_help = True  # タブなしで直接描画
    tab_batch = tab_custom = tab_rank = tab_qual = tab_writing_chat = tab_cases = tab_settings = tab_image_gen = None


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


def _parse_sanko_urls(info_text: str) -> list:
    """info_text の「参照URL：」行からURLリストを返す。"""
    for line in info_text.splitlines():
        if line.startswith("参照URL") and "：" in line:
            raw = line.split("：", 1)[1].strip()
            return [u.strip() for u in raw.replace("、", ",").split(",") if u.strip().startswith("http")]
    return []


def _merge_sanko_urls_in_info(new_info: str, old_urls: list) -> str:
    """new_info の参照URLフィールドに old_urls を重複なしで追記する。"""
    if not old_urls:
        return new_info
    lines = new_info.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("参照URL") and "：" in line:
            raw = line.split("：", 1)[1].strip()
            existing = [u.strip() for u in raw.replace("、", ",").split(",") if u.strip().startswith("http")]
            merged = existing[:]
            for u in old_urls:
                if u not in merged:
                    merged.append(u)
            lines[i] = f"参照URL：{', '.join(merged)}" if merged else line
            return "\n".join(lines)
    return new_info


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
    _cr_sep = "\n" if "\n" in clinics_raw else ","
    _cr_items = [x.strip() for x in clinics_raw.split(_cr_sep) if x.strip() and "::" in x]
    for item in _cr_items:
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


def _parse_batch_row_filter(filter_str: str) -> set | None:
    s = filter_str.strip()
    if not s:
        return None
    indices = set()
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                indices.update(range(int(a.strip()), int(b.strip()) + 1))
            except ValueError:
                pass
        else:
            try:
                indices.add(int(part))
            except ValueError:
                pass
    return indices or None


def _run_batch_core(rows, ws, is_bulk, is_kh, tab_name, defaults, creds_data):
    progress   = st.progress(0)
    status_msg = st.empty()

    for i, row in enumerate(rows):
        row_num = row["row_index"]
        kw = row["main_kw"]
        status_msg.info(f"処理中 ({i+1}/{len(rows)}): {kw}")
        if is_bulk:
            _write_status = write_status_knowhow_bulk
        elif tab_name == "ノウハウ":
            _write_status = write_status_knowhow
        else:
            _write_status = write_status
        _write_status(ws, row_num, "処理中")

        try:
            inputs = build_inputs_from_row(row, defaults)

            _batch_site_parts = ""
            _batch_site_name = inputs.get("site_name", "")
            if _batch_site_name and _batch_site_name in site_config_manager.list_sites(_site_cfg_creds, _site_cfg_parent_folder):
                _sc = site_config_manager.load_site_config(_batch_site_name, _site_cfg_creds, _site_cfg_parent_folder)
                _batch_site_parts = site_config_manager.format_site_parts(_sc.get("components", []))

            comp = analyze_competitors(inputs["competitor_urls"], claude_key, gemini_api_key=gemini_key, research_provider=research_provider)
            if is_kh:
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
            _batch_db_cache = clinic_db_manager.build_db_cache(
                [c["name"] for c in inputs["clinics"]],
                genre=inputs.get("genre", ""), creds_data=creds_data, sheet_url=db_sheet_url,
            )
            clinics   = collect_clinic_info(inputs["clinics"], inputs["genre"], claude_key, inputs.get("article_type", ""), db_cache=_batch_db_cache, db_type=DB_TYPE_CLINIC, gemini_api_key=gemini_key, research_provider=research_provider)
            structure = generate_structure(inputs, comp, clinics, claude_key, gemini_api_key=gemini_key, article_provider=article_provider)
            output    = generate_body(inputs, structure, clinics, claude_key, comp,
                                      site_parts=_batch_site_parts, gemini_api_key=gemini_key, article_provider=article_provider)

            _out = {
                "title":     structure["title"],
                "meta":      structure["meta"],
                "html":      output["html"],
                "todo_list": output["todo_list"],
            }
            if is_bulk:
                write_output_row_knowhow_bulk(ws, row_num, _out)
                write_status_knowhow_bulk(ws, row_num, "完了")
                # ── 画像生成（design_system が登録されているサイトのみ）──
                _bulk_slug = row.get("slug", "").strip()
                if not _bulk_slug:
                    st.caption(f"　⏭️ 画像スキップ（スラッグ未設定）: {kw}")
                elif not _batch_site_name:
                    st.caption(f"　⏭️ 画像スキップ（サイト名未設定）: {kw}")
                else:
                    _bulk_sc = site_config_manager.load_site_config(_batch_site_name, _site_cfg_creds, _site_cfg_parent_folder)
                    if not _bulk_sc.get("design_system"):
                        st.caption(f"　⏭️ 画像スキップ（{_batch_site_name} にデザインシステム未登録）: {kw}")
                    else:
                        try:
                            st.write(f"　🖼️ 画像生成中: {kw} ...")
                            _bulk_img_creds = _get_gcp_creds(sheets_creds_file)
                            _bulk_drive_folder = _drive_folder_id
                            # 参照画像をセッションキャッシュから取得（なければDL）
                            _bulk_ref_key = f"ref_images_{_batch_site_name}"
                            if _bulk_ref_key not in st.session_state:
                                st.session_state[_bulk_ref_key] = image_generator.load_reference_images_from_drive(
                                    _batch_site_name, _bulk_img_creds, _bulk_drive_folder,
                                ) if _bulk_img_creds else []
                            _bulk_ref_imgs = st.session_state[_bulk_ref_key]
                            _bulk_results = image_generator.generate_images_for_article(
                                article_text=output["html"],
                                site_config=_bulk_sc,
                                reference_images=_bulk_ref_imgs,
                                provider=image_provider,
                                gemini_api_key=gemini_key,
                                openai_api_key=openai_key,
                            )
                            _bulk_uploaded = 0
                            for _bi, _br in enumerate(_bulk_results):
                                if _br["bytes"] and _bulk_img_creds:
                                    _bulk_fname = f"{_bulk_slug}-img{_bi+1}.png"
                                    drive_uploader.upload_image(
                                        _br["bytes"], _bulk_fname,
                                        _batch_site_name, _bulk_slug,
                                        _bulk_img_creds, _bulk_drive_folder,
                                    )
                                    _bulk_uploaded += 1
                            st.write(f"　　→ {_bulk_uploaded} 枚アップロード完了")
                            # 画像タグを記事HTMLに注入してシート書き込み用HTMLを更新
                            _bulk_img_settings = _bulk_sc.get("image_settings", {})
                            if _bulk_img_settings.get("base_url") and _bulk_slug:
                                _out["html"] = inject_images_into_html(
                                    _out["html"], _bulk_results, _bulk_img_settings, _bulk_slug
                                )
                        except Exception as _img_e:
                            st.warning(f"　　→ 画像生成エラー ({kw}): {_img_e}")
            elif tab_name == "ノウハウ":
                write_output_row_knowhow(ws, row_num, _out)
                write_status_knowhow(ws, row_num, "完了")
            else:
                write_output_row(ws, row_num, {**_out, "clinics": inputs["clinics"]})
                write_status(ws, row_num, "完了")

        except Exception as e:
            _write_status(ws, row_num, f"エラー: {e}")
            st.warning(f"行{row_num} ({kw}) でエラー: {e}")

        progress.progress((i + 1) / len(rows))
        time.sleep(1)

    status_msg.success(f"✅ {len(rows)} 記事の処理が完了しました")


# ════════════════════════════════════════════════════════
#  Tab1: 一括作成
# ════════════════════════════════════════════════════════
with _safe_tab(tab_batch):
    st.title("📋 一括作成")
    st.caption("ステータスが空欄の行を対象に一括生成します。")

    if not article_sheet_url:
        st.warning("サイドバーで「記事スプレッドシートURL」を設定してください。")

    batch_tab_sel = "ノウハウ一括"

    # ノウハウ一括はサイト名・ジャンルを列に持たないのでUIで入力
    _batch_is_bulk = batch_tab_sel == "ノウハウ一括"
    if _batch_is_bulk:
        _bulk_col1, _bulk_col2 = st.columns(2)
        _bnk_site_opts = ["指定なし"] + site_config_manager.list_sites(_site_cfg_creds, _site_cfg_parent_folder)
        bulk_site_name = _bulk_col1.selectbox("サイト名（全行共通）", _bnk_site_opts, key="bulk_site_name")
        bulk_genre     = _bulk_col2.text_input("ジャンル（全行共通）", key="bulk_genre")

    batch_row_filter = st.text_input(
        "行を絞り込む（空白=全未処理行、例: 3,5,8 または 3-10）",
        key="batch_row_filter", placeholder="例: 3,5,8 または 3-10",
    )
    dry_run = st.toggle("ドライラン（対象行を確認・選択してから実行）", key="batch_dry_run")

    # ── ドライラン結果チェックリスト ──────────────────────────────────
    _dry_pending = st.session_state.get("batch_dry_rows", [])
    if _dry_pending:
        st.markdown("**実行する行を選択**（チェックを外した行はスキップ）")
        for _dr in _dry_pending:
            st.checkbox(
                f"行{_dr['row_index']}: [{_dr['article_type']}] {_dr['main_kw']}",
                value=True, key=f"brc_{_dr['row_index']}",
            )
        _run_sel_col, _clr_col = st.columns([2, 1])
        if _run_sel_col.button("✅ 選択した行を実行", type="primary", key="run_batch_selected"):
            _sel = [r for r in _dry_pending if st.session_state.get(f"brc_{r['row_index']}", True)]
            st.session_state["batch_dry_rows"] = []
            if _sel:
                _rc = _get_gcp_creds(sheets_creds_file)
                _rws = get_sheet(article_sheet_url, _rc, tab_name=batch_tab_sel)
                _r_is_bulk = batch_tab_sel == "ノウハウ一括"
                _r_is_kh   = batch_tab_sel in ("ノウハウ", "ノウハウ一括")
                try:
                    _r_defaults = read_defaults(get_settings_sheet(article_sheet_url, _rc))
                except Exception:
                    _r_defaults = {}
                _run_batch_core(_sel, _rws, _r_is_bulk, _r_is_kh, batch_tab_sel, _r_defaults, _rc)
        if _clr_col.button("クリア", key="batch_dry_clear"):
            st.session_state["batch_dry_rows"] = []
            st.rerun()

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
            pending = [r for r in rows if not r.get("status") or r.get("status") == "処理中"]
            _row_filter = _parse_batch_row_filter(st.session_state.get("batch_row_filter", ""))
            if _row_filter is not None:
                pending = [r for r in pending if r["row_index"] in _row_filter]

            try:
                settings_ws = get_settings_sheet(article_sheet_url, creds_data)
                defaults = read_defaults(settings_ws)
            except Exception:
                defaults = {}

            st.info(f"処理対象: **{len(pending)} 行** / 全 {len(rows)} 行")

            if dry_run:
                st.session_state["batch_dry_rows"] = pending
                st.rerun()
            else:
                _run_batch_core(pending, ws, _batch_is_bulk, _batch_is_kh, batch_tab_sel, defaults, creds_data)


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

    # ── スプシ行番号復元データを受け取ってフォームに展開 ─────────────────
    if "_t2_restore_data" in st.session_state:
        _rrd = st.session_state.pop("_t2_restore_data")
        if _rrd.get("article_type"):
            st.session_state["_pending_article_type"] = _rrd["article_type"]
        if _rrd.get("main_kw"):
            st.session_state["t_main_kw"] = _rrd["main_kw"]
        if _rrd.get("sub_kw"):
            st.session_state["t_sub_kw"] = _rrd["sub_kw"]
        if _rrd.get("related_kw"):
            st.session_state["t_related_kw"] = _rrd["related_kw"]
        if _rrd.get("custom_block"):
            st.session_state["custom_blocks"] = [{"text": _rrd["custom_block"], "intent": ""}]
        # 掲載院リストをパース
        _rrd_clinics = []
        _rrd_raw = _rrd.get("clinics_raw", "")
        _rrd_sep = "\n" if "\n" in _rrd_raw else ","
        _rrd_items = [x.strip() for x in _rrd_raw.split(_rrd_sep) if x.strip() and "::" in x]
        for _ci in _rrd_items:
            _cp = _ci.split("::")
            _rrd_clinics.append({
                "name": _cp[0].strip(),
                "domain": _cp[1].strip() if len(_cp) > 1 else "",
                "recommended": _cp[2].strip() if len(_cp) > 2 else "",
                "appeal": _cp[3].strip() if len(_cp) > 3 else "",
                "metarif_name": "",
            })
        if _rrd_clinics:
            st.session_state["test_clinics"] = _rrd_clinics
        # 競合URL
        _rrd_comps = [u.strip() for u in _rrd.get("competitor_urls_raw", "").split(",") if u.strip()]
        for _rci in range(5):
            st.session_state[f"t_comp_{_rci}"] = _rrd_comps[_rci] if _rci < len(_rrd_comps) else ""
        st.success(f"✅ 行{_rrd.get('row_index', '')}のデータを復元しました")

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
        with st.expander(f"📋 {article_type}の登録情報履歴（最新{len(_sheet_hist)}件）", expanded=False):
            # 行番号を直接入力して復元
            if article_type != "ノウハウ":
                st.caption("行番号を直接指定して復元")
                _row_restore_col1, _row_restore_col2 = st.columns([3, 1])
                _row_num_input = _row_restore_col1.number_input(
                    "行番号", min_value=2, value=2, step=1, key="t2_restore_row_num", label_visibility="collapsed",
                )
                if _row_restore_col2.button("この行を復元", key="t2_restore_row_btn"):
                    _rr_creds = _get_gcp_creds(sheets_creds_file)
                    if _rr_creds:
                        try:
                            _rr_ws = get_worksheet_readonly(article_sheet_url, _rr_creds, output_tab_sel if output_tab_sel != "（書き込まない）" else article_type)
                            if _rr_ws:
                                _rr_data = read_row_by_index(_rr_ws, int(_row_num_input))
                                if _rr_data:
                                    st.session_state["_t2_restore_data"] = _rr_data
                                    st.rerun()
                                else:
                                    st.error(f"行{_row_num_input}にデータがありません")
                        except Exception as _rr_e:
                            st.error(f"復元エラー: {_rr_e}")
                if _sheet_hist:
                    st.divider()
            if _sheet_hist:
                for _th in _sheet_hist:
                    _th_clinics = []
                    _th_raw = _th.get("clinics_raw", "")
                    _th_sep = "\n" if "\n" in _th_raw else ","
                    _th_cr_items = [x.strip() for x in _th_raw.split(_th_sep) if x.strip() and "::" in x]
                    for _item in _th_cr_items:
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
                                st.session_state[f"tcm_{_rci2}"] = _rc2.get("metarif_name", "")
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
                                _sr_cr_raw = _sr_data.get("clinics_raw", "")
                                _sr_cr_sep = "\n" if "\n" in _sr_cr_raw else ","
                                _sr_cr_items = [x.strip() for x in _sr_cr_raw.split(_sr_cr_sep) if x.strip() and "::" in x]
                                for _src in _sr_cr_items:
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
                                    st.session_state[f"tcm_{_sci2}"] = _sc2.get("metarif_name", "")
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
        sub_kw    = st.text_input("サブKW * （カンマ区切り）", key="t_sub_kw")
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
            for _pk, _wk in [
                (f"tcd_pending_{i}", f"tcd_{i}"),
                (f"tcr_pending_{i}", f"tcr_{i}"),
                (f"tca_pending_{i}", f"tca_{i}"),
            ]:
                if _pk in st.session_state:
                    st.session_state[_wk] = st.session_state.pop(_pk)
            is_first = (i == 0)
            st.caption("案件 1（最上位）" if is_first else f"案件 {i + 1}")
            tc0, tc1, tc2, tc3 = st.columns([3, 3, 1.2, 0.8])
            n = tc0.text_input("案件名 *", value=c["name"],   key=f"tcn_{i}", placeholder="TCB東京中央美容外科")
            d = tc1.text_input("ドメイン", value=c["domain"], key=f"tcd_{i}", placeholder="tcb.net または https://lp.example.com/...")
            tc2.markdown("<div style='padding-top:1.6rem'></div>", unsafe_allow_html=True)
            if tc2.button("📂 DB", key=f"tcdb_{i}", use_container_width=True, help="案件名でDB検索してドメイン・情報を読込"):
                if n.strip() and genre.strip():
                    _ca_db_creds = _get_gcp_creds(sheets_creds_file)
                    _ca_db_url = db_sheet_url if custom_db_type == DB_TYPE_CLINIC else lifestyle_sheet_url
                    _ca_db_data = clinic_db_manager.load_db(creds_data=_ca_db_creds, sheet_url=_ca_db_url, genre=genre.strip())
                    if n.strip() in _ca_db_data:
                        _ca_entry = _ca_db_data[n.strip()]
                        if _ca_entry.get("domain"):
                            st.session_state[f"tcd_pending_{i}"] = _ca_entry["domain"]
                        st.session_state[f"t_ca_db_info_{i}"] = _ca_entry.get("info", "")
                        st.session_state[f"t_ca_db_info_name_{i}"] = n.strip()
                        _lp_raw = _ca_entry.get("lp_info", "")
                        if _lp_raw:
                            # ★マーク付きの行のみを最訴求プランとして使用
                            _star_lines = [
                                line.lstrip("★").strip()
                                for line in _lp_raw.split("\n")
                                if line.strip().startswith("★")
                            ]
                            if _star_lines:
                                st.session_state[f"tcr_pending_{i}"] = _star_lines[0]
                        st.rerun()
                    else:
                        st.session_state[f"t_ca_db_info_{i}"] = ""
                        st.session_state[f"t_ca_db_info_name_{i}"] = n.strip()
                        st.info(f"「{n}」はDBに未登録です（スクレイピングで取得）")
                else:
                    st.warning("案件名とジャンルを入力してください")
            tc3.markdown("<div style='padding-top:1.6rem'></div>", unsafe_allow_html=True)
            if tc3.button("✕", key=f"trm_{i}", use_container_width=True) and len(st.session_state.test_clinics) > 1:
                to_remove.append(i)
            _ca_db_info_val = st.session_state.get(f"t_ca_db_info_{i}")
            _ca_db_info_name = st.session_state.get(f"t_ca_db_info_name_{i}", "")
            if _ca_db_info_val and _ca_db_info_name == n.strip():
                st.caption("📂 DB情報（記事生成に反映されます）")
                st.text_area("", value=_ca_db_info_val, height=100, disabled=True,
                             key=f"t_ca_db_preview_{i}", label_visibility="collapsed")
            rec_label = "最訴求プラン *" if is_first else "最訴求プラン（任意）"
            r = st.text_input(rec_label, value=c["recommended"], key=f"tcr_{i}", placeholder="例：セマグルチド0.5mgプラン")
            a = st.text_area("強み・比較優位性（任意）", value=c["appeal"], height=60, key=f"tca_{i}", placeholder="例：他社より処方量が1段階上から始められる")
            if i < 3:
                m = st.text_input("メタリフ名（任意）", value=c.get("metarif_name", ""), key=f"tcm_{i}", placeholder="例: diet-tcb.html")
            else:
                m = ""
            st.session_state.test_clinics[i] = {"name": n, "domain": d, "recommended": r, "appeal": a, "metarif_name": m}
        for idx in reversed(to_remove):
            st.session_state.test_clinics.pop(idx)
        if st.button("＋ 案件を追加", key="t_add"):
            st.session_state.test_clinics.append({"name": "", "domain": "", "recommended": "", "appeal": "", "metarif_name": ""})
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
    # ── 入力の一時保存ボタン ─────────────────────────────────────
    if st.button("📥 入力を一時保存（スプシに書き込む）", key="t2_save_input", use_container_width=True):
        _save_creds = _get_gcp_creds(sheets_creds_file)
        if not article_sheet_url:
            st.error("サイドバーでスプシURLを設定してください")
        elif output_tab_sel == "（書き込まない）":
            st.error("スプシ書き込み先タブを選択してください（「書き込まない」以外）")
        elif article_type == "ノウハウ":
            st.warning("ノウハウ記事の一時保存は非対応です")
        elif not _save_creds:
            st.error("GCP認証が設定されていません")
        elif not main_kw:
            st.error("メインKWを入力してください")
        else:
                try:
                    _save_ws = get_sheet(article_sheet_url, _save_creds, tab_name=output_tab_sel)
                    _save_vals = _save_ws.get_all_values()
                    # メインKWが一致する空き行を探す → なければ末尾に追加
                    _save_row = None
                    for _ri, _rd in enumerate(_save_vals[1:], start=2):
                        _pd = _rd + [""] * (11 - len(_rd))
                        if _pd[3] == main_kw:
                            _save_row = _ri
                            break
                    if _save_row is None:
                        _save_row = len(_save_vals) + 1
                    _save_clinics = [c for c in st.session_state.get("test_clinics", []) if c["name"]]
                    _save_inputs = {
                        "site_name":      st.session_state.get("t_site_sel", ""),
                        "genre":          genre,
                        "article_type":   article_type,
                        "main_kw":        main_kw,
                        "sub_kw":         sub_kw,
                        "clinics":        _save_clinics,
                        "competitor_urls":[u.strip() for u in [st.session_state.get(f"t_comp_{i}", "") for i in range(5)] if u.strip()],
                        "custom_block":   "\n".join(filter(None, [cb["text"].strip() for cb in st.session_state.get("custom_blocks", [])])),
                        "recommended":    _save_clinics[0]["recommended"] if _save_clinics else "",
                        "related_kw":     related_kw,
                    }
                    write_input_only_row(_save_ws, _save_row, _save_inputs)
                    st.success(f"✅ [{output_tab_sel}] 行{_save_row}に保存しました（復元時はこの行番号を使用）")
                    st.session_state.pop(_hist_cache_key, None)
                except Exception as _se:
                    st.error(f"保存エラー: {_se}")

    _run_label = "🔍 見出しを生成" if gen_mode == "見出し確認あり" else "🚀 実行"
    if st.button(_run_label, type="primary", use_container_width=True, key="run_test"):
        valid_clinics = [c for c in st.session_state.get("test_clinics", []) if c["name"] and c["domain"]]
        errs = []
        if not claude_key:      errs.append("Claude API Key 未設定")
        if not main_kw:         errs.append("メインKW を入力してください")
        if not sub_kw.strip():  errs.append("サブKW を入力してください")
        if not genre:           errs.append("ジャンル を入力してください")
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
                        if clinic_count > 0:
                            all_clinics = all_clinics[:clinic_count]
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
                        _t2_notation_rules = []
                        if _batch_site_name and _notation_rules_sheet_url_default:
                            try:
                                _nr_creds = _get_gcp_creds(sheets_creds_file)
                                if _nr_creds:
                                    _t2_notation_rules = read_notation_rules(
                                        _notation_rules_sheet_url_default, _nr_creds, _batch_site_name
                                    )
                            except Exception:
                                pass
                        output = generate_body(inputs, structure, clinics, claude_key, comp,
                                              site_parts=_single_site_parts, gemini_api_key=gemini_key,
                                              article_provider=article_provider, notation_rules=_t2_notation_rules)
                        st.write("📝 選び方コンテンツ抽出中...")
                        _criteria_summary = extract_criteria_summary(output["html"], claude_key)
                        st.session_state["t2_last"] = {
                            "html":             output["html"],
                            "title":            structure["title"],
                            "meta":             structure["meta"],
                            "todo_list":        output["todo_list"],
                            "structure_text":   structure["structure_text"],
                            "criteria_summary": _criteria_summary,
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

        if st.session_state.pop("_t2_revision_clear", False):
            st.session_state["t2_revision_input"] = ""
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
                    st.session_state["_t2_revision_clear"] = True
                    # Drive に構成修正ログを保存（失敗してもメインフローは止めない）
                    _struct_log_creds = _get_gcp_creds(sheets_creds_file)
                    if _struct_log_creds:
                        try:
                            _sl_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                            _sl_kw = _t2_draft["inputs"].get("main_kw", "")[:20].replace(" ", "_")
                            _sl_atype = _t2_draft["inputs"].get("article_type", "不明")
                            _sl_yyyymm = datetime.datetime.now().strftime("%Y%m")
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
                                ["修正ログ", _sl_atype, _sl_yyyymm],
                                _struct_log_creds,
                                _drive_folder_id,
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
                st.text_area(
                    "HTML（直接編集可）",
                    value=_block["html"],
                    key=f"t2_h2_edit_{_bi}",
                    height=220,
                    label_visibility="collapsed",
                )

                _dl_col, _save_col, _ = st.columns([2, 2, 3])
                with _dl_col:
                    st.download_button(
                        "📥 このH2をDL",
                        _block["html"].encode("utf-8"),
                        file_name=f"h2_{_bi+1}_{_block['title'][:20].replace(' ','_')}.html",
                        mime="text/html",
                        key=f"t2_h2_dl_{_bi}",
                    )
                with _save_col:
                    if st.button("💾 手動編集を保存", key=f"t2_h2_save_{_bi}"):
                        _edited_html = st.session_state.get(f"t2_h2_edit_{_bi}", _block["html"])
                        if _edited_html != _block["html"]:
                            _h2_blocks[_bi]["html"] = _edited_html
                            _h2_blocks[_bi]["modified"] = True
                            _h2_blocks[_bi]["confirmed"] = False
                            st.session_state["t2_h2_blocks"] = _h2_blocks
                            st.rerun()

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
                                        _log_yyyymm = datetime.datetime.now().strftime("%Y%m")
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
                                            ["修正ログ", _log_atype, _log_yyyymm],
                                            _edit_creds,
                                            _drive_folder_id,
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
        if _t2_last.get("clinics"):
            _rb_clinic_lines = "\n".join(
                f"{i+1}. {c['name']}::{(c['domain'] if c.get('domain','').startswith('http') else ('https://' + c['domain'].lstrip('/')) if c.get('domain') else '[要確認]')}"
                for i, c in enumerate(_t2_last["clinics"])
            )
            _rb_detail_lines = []
            for _di, _dc in enumerate(_t2_last.get("_inputs", {}).get("clinics", [])[:3]):
                _rb_detail_lines.append(f"---{_di+1}位: {_dc['name']}---")
                _rb_detail_lines.append(f"メタリフ名: {_dc.get('metarif_name', '')}")
                _rb_detail_lines.append(f"LPプラン: {_dc.get('recommended', '')}")
            _rb_export_text = (
                f"【メインKW】\n{_t2_last.get('main_kw', '')}\n\n"
                f"【サブKW】\n{_t2_last.get('_inputs', {}).get('sub_kw', '')}\n\n"
                f"【記事構成】\n{_t2_last.get('structure_text', '')}\n\n"
                f"【選び方コンテンツ】\n{_t2_last.get('criteria_summary', '')}\n\n"
                f"【件数】\n{len(_t2_last.get('clinics', []))}\n\n"
                f"【掲載院一覧】\n{_rb_clinic_lines}"
                + (f"\n\n【案件詳細】\n" + "\n".join(_rb_detail_lines) if _rb_detail_lines else "")
            )
            st.download_button(
                "📋 ランキングブロック用データをダウンロード",
                _rb_export_text.encode("utf-8"),
                file_name=f"{_t2_last['main_kw'].replace(' ','_')}_ranking.txt",
                mime="text/plain",
                key="t2_dl_ranking",
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

    # ── 画像生成セクション ──
    if _t2_last:
        if not _t2_last.get("site_config", {}).get("design_system"):
            st.divider()
            st.subheader("🖼️ 画像生成")
            st.info(f"「{_t2_last.get('site_name', 'このサイト')}」にデザインシステムが未登録です。サイト設定タブで参照画像をアップしてください。")
    if _t2_last and _t2_last.get("site_config", {}).get("design_system"):
        st.divider()
        st.subheader("🖼️ 画像生成")
        st.caption(f"対象記事: {_t2_last['main_kw']}")
        _img_site_config = _t2_last["site_config"]
        _img_site_name = _t2_last.get("site_name", "default") or "default"

        _img_slug = st.text_input(
            "スラッグ（Drive保存先フォルダ名・英数字ハイフンのみ）",
            key="t2_img_slug",
            placeholder="例: aga-treatment-tokyo",
        )

        if st.button("🖼️ 画像を生成してDriveにアップロード", key="t2_img_gen", type="primary"):
            errs_img = []
            if image_provider == "dalle" and not openai_key:
                errs_img.append("DALL-E を使うには OpenAI API Key が必要です")
            elif image_provider == "gemini" and not gemini_key:
                errs_img.append("Gemini API Key が未設定です")
            if not _img_slug.strip():
                errs_img.append("スラッグを入力してください")
            for e in errs_img:
                st.error(e)

            if not errs_img:
                _creds_img = _get_gcp_creds(sheets_creds_file)
                if not _creds_img:
                    st.error("Google Sheets 認証情報が未設定です")
                else:
                    with st.status("画像生成中...", expanded=True) as img_status:
                        try:
                            # 参照画像をキャッシュから取得
                            _ref_key = f"ref_images_{_img_site_name}"
                            if _ref_key not in st.session_state:
                                st.write("☁️ 参照画像をDriveから読み込み中...")
                                st.session_state[_ref_key] = image_generator.load_reference_images_from_drive(
                                    _img_site_name, _creds_img, _drive_folder_id,
                                )
                            _ref_imgs = st.session_state[_ref_key]
                            st.write(f"　→ 参照画像: {len(_ref_imgs)} 枚")

                            st.write("💡 画像案を生成中（Gemini）...")
                            _img_results_t2 = image_generator.generate_images_for_article(
                                article_text=_t2_last["html"],
                                site_config=_img_site_config,
                                reference_images=_ref_imgs,
                                provider=image_provider,
                                gemini_api_key=gemini_key,
                                openai_api_key=openai_key,
                            )
                            st.write(f"　→ {len(_img_results_t2)} 案を生成")

                            _uploaded_t2 = []
                            for i, _ir in enumerate(_img_results_t2):
                                if _ir["bytes"]:
                                    _ir_fname = f"{_img_slug.strip()}-img{i+1}.png"
                                    st.write(f"🎨 アップロード中 ({i+1}/{len(_img_results_t2)}): {_ir_fname}...")
                                    drive_url = drive_uploader.upload_image(
                                        _ir["bytes"], _ir_fname,
                                        _img_site_name, _img_slug.strip(),
                                        _creds_img, _drive_folder_id,
                                    )
                                    _uploaded_t2.append({"fname": _ir_fname, "drive_url": drive_url, "proposal": _ir["proposal"]})
                                else:
                                    st.warning(f"案{i+1} の画像生成に失敗しました")

                            img_status.update(label=f"✅ {len(_uploaded_t2)} 枚アップロード完了", state="complete")

                            # 画像タグを記事HTMLに注入
                            _img_settings_t2 = _img_site_config.get("image_settings", {})
                            if _img_settings_t2.get("base_url") and _img_slug.strip():
                                _updated_html = inject_images_into_html(
                                    _t2_last["html"],
                                    _img_results_t2,
                                    _img_settings_t2,
                                    _img_slug.strip(),
                                )
                                _t2_last["html"] = _updated_html
                                st.session_state["t2_last"] = _t2_last
                                st.session_state.pop("t2_h2_blocks_hash", None)
                                st.caption("✅ 画像タグを記事に挿入しました")

                            for r in _uploaded_t2:
                                st.markdown(
                                    f"**{r['proposal'].get('placement', '')}**  \n"
                                    f"ファイル名: `{r['fname']}`  \n"
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

    st.divider()
    st.subheader("見出し構成チェック")
    st.caption("H2・H3の見出し一覧をテキストで貼り付けてください（メインKW・サブKW・記事タイプは上の入力欄を共用）")
    heading_outline = st.text_area("見出し構成を貼り付け", height=200, key="chk_outline", placeholder="## 東京でダイエットクリニックを選ぶ3つの基準\n### 保険適用の有無で費用が大きく変わる\n### 処方される薬の種類と副作用リスク\n...")
    if st.button("見出し構成チェック実行", key="run_heading_check"):
        if not claude_key:
            st.error("Claude API Key が未設定です")
        elif not heading_outline.strip():
            st.error("見出し構成を貼り付けてください")
        else:
            with st.spinner("チェック中..."):
                heading_result = heading_structure_check(
                    heading_outline, check_type, check_main_kw,
                    [k.strip() for k in check_sub_kw.split(",") if k.strip()],
                    claude_key,
                    gemini_api_key=gemini_key,
                    article_provider=article_provider,
                )
                st.markdown(heading_result)


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

            # ── 1. デザインシステム ──────────────────────────────────
            st.markdown("### 🎨 1. デザインシステム")
            _ds = _config4.get("design_system", {})
            _dr_colors = _config4.get("design_rules", {}).get("colors", {})

            # 登録状況バッジ
            _ds_filled = [k for k in ("illustration_style", "ref_image_analysis", "primary_color") if _ds.get(k)]
            if len(_ds_filled) >= 2:
                st.success("✅ デザインシステム登録済み（参照画像からの自動入力を含む）")
            elif _ds:
                st.warning("⚠️ デザインシステム未完全（参照画像をアップしてスタイルを自動入力することを推奨）")
            else:
                st.info("ℹ️ デザインシステム未登録 — 下の参照画像をアップすると自動で入力されます")

            with st.form(key=f"ds_form_{_current_site4}"):
                st.caption("カラーパレット")
                _ds_col1, _ds_col2 = st.columns(2)
                with _ds_col1:
                    _ds_primary   = st.text_input("メインカラー",   value=_ds.get("primary_color") or _dr_colors.get("main", ""), key=f"ds_primary_{_current_site4}")
                    _ds_accent    = st.text_input("アクセントカラー", value=_ds.get("accent_color") or _dr_colors.get("accent_red", ""), key=f"ds_accent_{_current_site4}")
                    _ds_bg        = st.text_input("背景色",         value=_ds.get("background_color") or _dr_colors.get("bg_white", "#FFFFFF"), key=f"ds_bg_{_current_site4}")
                with _ds_col2:
                    _ds_text      = st.text_input("テキスト色",     value=_ds.get("text_color") or _dr_colors.get("text", "#333333"), key=f"ds_text_{_current_site4}")
                    _ds_secondary = st.text_input("サブカラー",     value=_ds.get("secondary_color", ""), key=f"ds_secondary_{_current_site4}")
                    _ds_danger    = st.text_input("警告色",         value=_ds.get("danger_color", ""), key=f"ds_danger_{_current_site4}")
                st.caption("イラストスタイル")
                _ds_style   = st.text_input("スタイル",   value=_ds.get("illustration_style", "flat minimal"), key=f"ds_style_{_current_site4}")
                _ds_prohibit = st.text_area("禁止事項",  value=_ds.get("prohibited_elements", ""), height=80, key=f"ds_prohibit_{_current_site4}")
                _ds_notes   = st.text_area("追加ノート", value=_ds.get("additional_notes", ""), height=60, key=f"ds_notes_{_current_site4}")
                if st.form_submit_button("💾 デザインシステムを保存", type="primary"):
                    _cfg_now = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                    _cfg_now.setdefault("design_system", {}).update({
                        "primary_color": _ds_primary, "accent_color": _ds_accent,
                        "background_color": _ds_bg, "text_color": _ds_text,
                        "secondary_color": _ds_secondary, "danger_color": _ds_danger,
                        "illustration_style": _ds_style,
                        "prohibited_elements": _ds_prohibit, "additional_notes": _ds_notes,
                    })
                    if site_config_manager.save_site_config(_current_site4, _cfg_now, _site_cfg_creds, _site_cfg_parent_folder):
                        st.success("✅ 保存しました。")
                        st.rerun()
                    else:
                        st.error("保存に失敗しました。")

            st.markdown("---")

            # ── 1b. 参照画像 ────────────────────────────────────────
            st.markdown("### 🖼️ 参照画像（最大5枚）")
            st.caption("サイトのデザインに近い既存画像をアップすると、そのスタイルを模倣して画像生成します。")

            # 登録済み一覧
            if _site_cfg_creds and _site_cfg_parent_folder:
                try:
                    _ref_files = image_generator.list_reference_images_in_drive(
                        _current_site4, _site_cfg_creds, _site_cfg_parent_folder
                    )
                    if _ref_files:
                        st.caption(f"登録済み: {len(_ref_files)} 枚")
                        for _rf in _ref_files:
                            _rf_col1, _rf_col2 = st.columns([4, 1])
                            with _rf_col1:
                                st.text(_rf["name"])
                            with _rf_col2:
                                if st.button("🗑️", key=f"del_ref_{_rf['id']}"):
                                    _del_ok, _del_err = image_generator.delete_reference_image_from_drive(_rf["id"], _site_cfg_creds)
                                    if _del_ok:
                                        st.session_state.pop(f"ref_images_{_current_site4}", None)
                                        st.rerun()
                                    else:
                                        st.error(f"削除失敗: {_del_err}")
                    else:
                        st.caption("参照画像未登録")
                except Exception as _ref_list_e:
                    st.caption(f"一覧取得エラー: {_ref_list_e}")

            # アップロード
            _ref_uploads = st.file_uploader(
                "参照画像をアップ（jpg / png / webp・複数選択可）",
                type=["jpg", "jpeg", "png", "webp"],
                accept_multiple_files=True,
                key=f"ref_img_upload_{_current_site4}",
            )
            if _ref_uploads:
                for _ru in _ref_uploads:
                    st.image(_ru, width=200, caption=_ru.name)
                if st.button(f"☁️ {len(_ref_uploads)}枚をDriveに保存してスタイル分析", key=f"btn_upload_ref_{_current_site4}", type="primary"):
                    if not _site_cfg_creds:
                        st.error("Google認証が未設定です")
                    elif not gemini_key:
                        st.error("Gemini API Key が未設定です")
                    else:
                        _cfg_now = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                        _pil_images = []
                        with st.spinner("Driveにアップロード中..."):
                            for _ru in _ref_uploads:
                                try:
                                    _ru.seek(0)
                                    _ru_bytes = _ru.read()
                                    image_generator.upload_reference_image_to_drive(
                                        _ru_bytes, _ru.name, _current_site4,
                                        _site_cfg_creds, _site_cfg_parent_folder,
                                    )
                                    from PIL import Image as _PILImg
                                    import io as _io
                                    _pil_images.append(_PILImg.open(_io.BytesIO(_ru_bytes)).convert("RGB"))
                                    st.write(f"✅ {_ru.name} アップ完了")
                                except Exception as _ru_e:
                                    st.error(f"{_ru.name} エラー: {_ru_e}")
                        if _pil_images:
                            with st.spinner("Geminiがスタイルを分析中..."):
                                _ds_from_ref = image_generator.analyze_reference_images(
                                    _pil_images, _cfg_now, gemini_key
                                )
                            # 分析結果をsession_stateに保存して確認画面へ
                            st.session_state[f"ds_analysis_{_current_site4}"] = _ds_from_ref

            # ── 分析結果の確認・保存 ──────────────────────────────
            _ds_analysis_key = f"ds_analysis_{_current_site4}"
            if st.session_state.get(_ds_analysis_key):
                _analysis = st.session_state[_ds_analysis_key]
                st.success("✅ Geminiがデザインシステムを分析しました。内容を確認して保存してください。")
                _label_map = {
                    "primary_color":      "メインカラー",
                    "accent_color":       "アクセントカラー",
                    "background_color":   "背景色",
                    "text_color":         "テキスト色",
                    "illustration_style": "イラストスタイル",
                    "line_weight":        "線の太さ・質感",
                    "character_style":    "人物の描き方",
                    "fill_style":         "塗りスタイル",
                    "card_style":         "カード形状",
                    "spacing":            "余白感",
                    "prohibited_elements":"禁止事項",
                    "additional_notes":   "追加ノート",
                    "ref_image_analysis": "総合スタイル説明",
                }
                for _fk, _flabel in _label_map.items():
                    if _analysis.get(_fk):
                        st.markdown(f"**{_flabel}**")
                        st.caption(_analysis[_fk])
                _rc1, _rc2 = st.columns(2)
                if _rc1.button("✅ この内容でデザインシステムに適用", key=f"apply_ds_{_current_site4}", type="primary"):
                    _cfg_apply = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                    _existing_ds = _cfg_apply.get("design_system", {})
                    for _k, _v in _analysis.items():
                        if _v:
                            _existing_ds[_k] = _v
                    _cfg_apply["design_system"] = _existing_ds
                    site_config_manager.save_site_config(_current_site4, _cfg_apply, _site_cfg_creds, _site_cfg_parent_folder)
                    st.session_state.pop(_ds_analysis_key, None)
                    st.session_state.pop(f"ref_images_{_current_site4}", None)
                    # フォームwidgetのセッションステートをクリアして新値で再描画させる
                    for _fk in [f"ds_primary_{_current_site4}", f"ds_accent_{_current_site4}",
                                 f"ds_bg_{_current_site4}", f"ds_text_{_current_site4}",
                                 f"ds_secondary_{_current_site4}", f"ds_danger_{_current_site4}",
                                 f"ds_style_{_current_site4}", f"ds_prohibit_{_current_site4}",
                                 f"ds_notes_{_current_site4}"]:
                        st.session_state.pop(_fk, None)
                    st.success("✅ 保存しました。")
                    st.rerun()
                if _rc2.button("✕ 破棄", key=f"discard_ds_{_current_site4}"):
                    st.session_state.pop(_ds_analysis_key, None)
                    st.rerun()

            st.markdown("---")

            # ── 1c. 画像テンプレート設定 ─────────────────────────────
            st.markdown("### 🖼️ 画像テンプレート設定")
            st.caption("記事に挿入される画像タグのHTMLテンプレートとベースURLを登録します。{src} と {alt} がプレースホルダーです。")
            _imgs = _config4.get("image_settings", {})
            with st.form(key=f"img_settings_form_{_current_site4}"):
                _imgs_base_url = st.text_input(
                    "画像ベースURL（末尾 / まで）",
                    value=_imgs.get("base_url", ""),
                    key=f"imgs_base_url_{_current_site4}",
                    placeholder="https://example.com/wp-content/uploads/",
                )
                _imgs_ext = st.selectbox(
                    "拡張子",
                    ["webp", "png", "jpg"],
                    index=["webp", "png", "jpg"].index(_imgs.get("ext", "webp")) if _imgs.get("ext", "webp") in ["webp", "png", "jpg"] else 0,
                    key=f"imgs_ext_{_current_site4}",
                )
                _imgs_template = st.text_area(
                    "HTMLテンプレート",
                    value=_imgs.get("template", '<div class="full_img">\n  <img decoding="async" src="{src}" alt="{alt}">\n</div>'),
                    height=100,
                    key=f"imgs_template_{_current_site4}",
                )
                if st.form_submit_button("💾 画像テンプレートを保存"):
                    _cfg_now = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                    _cfg_now["image_settings"] = {
                        "base_url": _imgs_base_url.strip(),
                        "ext": _imgs_ext,
                        "template": _imgs_template,
                    }
                    if site_config_manager.save_site_config(_current_site4, _cfg_now, _site_cfg_creds, _site_cfg_parent_folder):
                        st.success("✅ 保存しました。")
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
                        _cbt_bi_sample = st.text_area(
                            "基本情報テーブル HTMLサンプル（行名・形式の参考。内容はAIが埋める）",
                            value=_cbt.get("basic_info_html_sample", ""),
                            height=150,
                            key=f"cbt_bi_sample_{_current_site4}_{_cbi}",
                            placeholder="<table>...</table> の形式で貼り付け。行名と構造のみ参照されます。",
                        )

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
                                "basic_info_html_sample": _cbt_bi_sample.strip(),
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

            # ── 4. リンク設定 ────────────────────────────────────
            st.markdown("---")
            st.markdown("### 🔗 4. リンク設定")
            st.caption("アフィリリンク・外部リンクのルールを登録します。登録するとランキングブロック生成のプロンプトに反映されます。")
            _current_ls = _config4.get("link_settings", {})
            with st.form(f"link_settings_form_{_current_site4}"):
                _ls_base_url = st.text_input(
                    "アフィリリンク ベースURL",
                    value=_current_ls.get("affili_base_url", ""),
                    placeholder="https://koizumi-seikei.jp/obesity/web/",
                    key=f"ls_base_url_{_current_site4}",
                )
                _ls_col1, _ls_col2 = st.columns(2)
                _ls_positions = _ls_col1.text_input(
                    "記事の場所コード（コンマ区切り）",
                    value=_current_ls.get("affili_param_positions", "top,rank,matome,ryokin,kuchikomi"),
                    help="top=冒頭 / rank=ランキング / matome=まとめ / ryokin=料金 / kuchikomi=口コミ",
                    key=f"ls_pos_{_current_site4}",
                )
                _ls_formats = _ls_col2.text_input(
                    "形式コード（コンマ区切り）",
                    value=_current_ls.get("affili_param_formats", "bt,bn,txt"),
                    help="bt=ボタン / bn=バナー / txt=テキスト / txt2=テキスト2箇所目など",
                    key=f"ls_fmt_{_current_site4}",
                )
                st.caption("パラメータ形式（固定）: ?{記事スラッグ}_{記事の場所}_{形式}　例: ?diet-dmmrybelsus_rank_bn")
                st.caption("アフィリリンク属性（固定）: target=\"_blank\" rel=\"nofollow noopener noreferrer\"")
                st.caption("外部リンク属性（固定）: target=\"_blank\" rel=\"noopener noreferrer\"")
                _ls_submitted = st.form_submit_button("💾 リンク設定を保存", type="primary")

            if _ls_submitted:
                _ls_save_config = site_config_manager.load_site_config(_current_site4, _site_cfg_creds, _site_cfg_parent_folder)
                _ls_save_config["link_settings"] = {
                    "affili_base_url": _ls_base_url.strip(),
                    "affili_param_positions": _ls_positions.strip(),
                    "affili_param_formats": _ls_formats.strip(),
                }
                if site_config_manager.save_site_config(_current_site4, _ls_save_config, _site_cfg_creds, _site_cfg_parent_folder):
                    st.success("リンク設定を保存しました。")
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
    _cb_link_settings = {}
    _cb_affili_base = ""
    if _cb_sel_site != "（なし）":
        _cb_site_cfg = site_config_manager.load_site_config(_cb_sel_site, _site_cfg_creds, _site_cfg_parent_folder)
        _cb_templates = _cb_site_cfg.get("clinic_block_templates", [])
        _cb_template_names = [t.get("name", f"テンプレート{i+1}") for i, t in enumerate(_cb_templates)]
        _cb_link_settings = _cb_site_cfg.get("link_settings", {})
        _cb_affili_base = _cb_link_settings.get("affili_base_url", "").strip().rstrip("/")

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

    for _pk, _wk in [("cb_main_kw_pending", "cb_main_kw"), ("cb_sub_kw_pending", "cb_sub_kw")]:
        if _pk in st.session_state:
            st.session_state[_wk] = st.session_state.pop(_pk)

    _cb_kw_col1, _cb_kw_col2 = st.columns(2)
    _cb_main_kw = _cb_kw_col1.text_input("メインKW", key="cb_main_kw")
    _cb_sub_kw  = _cb_kw_col2.text_input("サブKW（カンマ区切り）", key="cb_sub_kw")
    _cb_db_type = st.selectbox("DBタイプ", [DB_TYPE_CLINIC, DB_TYPE_LIFESTYLE], key="cb_db_type")
    _rb_uploaded = st.file_uploader(
        "📤 本文作成データをアップロード（任意）",
        type=["txt"],
        key="rb_data_upload",
        help="本文作成タブの「ランキングブロック用データをダウンロード」で出力したファイルを読み込みます",
    )
    if _rb_uploaded is not None:
        _rb_fingerprint = f"{_rb_uploaded.name}_{_rb_uploaded.size}"
        if st.session_state.get("_rb_last_processed") == _rb_fingerprint:
            _rb_raw = None  # 処理済みのためスキップ
        else:
            st.session_state["_rb_last_processed"] = _rb_fingerprint
            _rb_raw = _rb_uploaded.read().decode("utf-8")
    else:
        _rb_raw = None
    if _rb_raw is not None:
        if "【掲載院一覧】" in _rb_raw:
            _rb_split = _rb_raw.split("【掲載院一覧】", 1)
            _rb_header = _rb_split[0]
            _rb_rest   = _rb_split[1]

            def _rb_extract(text, tag):
                import re as _re
                m = _re.search(rf"【{tag}】\n(.*?)(?=\n【|\Z)", text, _re.DOTALL)
                return m.group(1).strip() if m else ""

            _rb_main_kw  = _rb_extract(_rb_header, "メインKW")
            _rb_sub_kw   = _rb_extract(_rb_header, "サブKW")
            _rb_criteria = _rb_extract(_rb_header, "選び方コンテンツ")

            if _rb_main_kw:
                st.session_state["cb_main_kw_pending"] = _rb_main_kw
            if _rb_sub_kw:
                st.session_state["cb_sub_kw_pending"] = _rb_sub_kw
            if _rb_criteria:
                st.session_state["cb_criteria"] = _rb_criteria
            else:
                st.session_state["cb_criteria"] = _rb_header.replace("【構成・選び方】", "").strip()

            if "【案件詳細】" in _rb_rest:
                _rb_clinic_part, _rb_detail_part = _rb_rest.split("【案件詳細】", 1)
                st.session_state["cb_clinic_paste"] = _rb_clinic_part.strip()
                st.session_state["cb_clinics"] = clinic_block_writer.parse_clinic_list(_rb_clinic_part.strip())
                _rb_cur_rank = None
                for _rb_line in _rb_detail_part.strip().split("\n"):
                    _rb_line = _rb_line.strip()
                    _rb_rm = re.match(r"---(\d+)位: .+---", _rb_line)
                    if _rb_rm:
                        _rb_cur_rank = int(_rb_rm.group(1))
                    elif _rb_cur_rank and _rb_line.startswith("メタリフ名: "):
                        st.session_state[f"cb_metarif_{_rb_cur_rank}"] = _rb_line[len("メタリフ名: "):].strip()
                    elif _rb_cur_rank and _rb_line.startswith("LPプラン: "):
                        st.session_state[f"cb_lp_{_rb_cur_rank}"] = _rb_line[len("LPプラン: "):].strip()
            else:
                st.session_state["cb_clinic_paste"] = _rb_rest.strip()
                st.session_state["cb_clinics"] = clinic_block_writer.parse_clinic_list(_rb_rest.strip())
        else:
            st.session_state["cb_criteria"] = _rb_raw.strip()
        st.success("データを読み込みました")
        st.rerun()

    _cb_criteria = st.text_area(
        "記事内の「選び方」セクション（文章をそのまま貼り付け）",
        height=120, key="cb_criteria",
        placeholder="記事内の「○○の選び方」セクションの文章をそのまま貼り付けてください。",
    )

    st.divider()
    st.subheader("掲載案件一覧")
    st.caption("本文作成タブからダウンロードしたデータをアップロードするか、直接貼り付けてください。")
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
                    _cb_metarif_key = f"cb_metarif_{_r}"
                    if _cb_affili_base:
                        _cbc_metarif = st.text_input(
                            "メタリフ名",
                            key=_cb_metarif_key,
                            placeholder="diet-dmm-ryb.html",
                        )
                        _auto_link = f"{_cb_affili_base}/{_cbc_metarif}" if _cbc_metarif else ""
                    else:
                        _cbc_metarif = ""
                        _auto_link = ""

                    _default_link_val = st.session_state.get(f"cb_link_{_r}") or _auto_link or _cbc_url
                    _cbc_link = st.text_input(
                        "リンクURL（LP等）",
                        value=_default_link_val,
                        key=f"cb_link_{_r}",
                        placeholder="CTAボタン・見出しリンクのリンク先URL（パラメータは ?スラッグ_場所_形式 で追記）",
                    )
                    if _auto_link:
                        st.caption(f"↑ アフィリURL候補: `{_auto_link}`")
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
        if "cb_extra_blocks" not in st.session_state:
            st.session_state.cb_extra_blocks = [{"text": "", "intent": ""}]
        _cb_extra_to_remove = []
        for _cbi, _cbb in enumerate(st.session_state.cb_extra_blocks):
            _cbb_cols = st.columns([11, 1])
            _cbb_t = _cbb_cols[0].text_area(
                f"追加指示 {_cbi + 1}（任意・全院共通）",
                value=_cbb["text"], height=80, key=f"cbb_text_{_cbi}",
                placeholder="例：紹介文のトーンをもっとやわらかく／CTAボタンのテキストを「無料カウンセリングはこちら」に統一",
            )
            if len(st.session_state.cb_extra_blocks) > 1 and _cbb_cols[1].button("✕", key=f"cbb_rm_{_cbi}"):
                _cb_extra_to_remove.append(_cbi)
            _cbb_i = st.text_area(
                "追加指示の意図（任意）",
                value=_cbb["intent"], height=60, key=f"cbb_intent_{_cbi}",
                placeholder="例：競合との差別化を読者が直感的に感じ取れるようにしたい",
            )
            st.session_state.cb_extra_blocks[_cbi] = {"text": _cbb_t, "intent": _cbb_i}
        for _cbi_rm in reversed(_cb_extra_to_remove):
            st.session_state.cb_extra_blocks.pop(_cbi_rm)
        if st.button("＋ 追加指示を追加", key="cbb_add"):
            st.session_state.cb_extra_blocks.append({"text": "", "intent": ""})
            st.rerun()

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
                _cb_clinics_to_gen = _cb_clinics
                # link_settings をサイトパーツに追記
                _cb_link_rule_str = site_config_manager.format_link_settings(_cb_link_settings)
                if _cb_link_rule_str:
                    _cb_site_parts = (_cb_site_parts + "\n\n" + _cb_link_rule_str).strip() if _cb_site_parts else _cb_link_rule_str
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
                            # lp_plan が未入力ならDBのlp_infoをフォールバックとして使う
                            if not _lp_plan:
                                _db_lp_plan, _ = clinic_db_manager.get_clinic_lp_info(_cbc["name"], _t5_db_creds, _t5_active_db_url)
                                if _db_lp_plan:
                                    _lp_plan = _db_lp_plan
                                    st.write(f"　→ lp_info を最訴求プランに使用")
                            _scraped = collect_clinic_info(
                                [{"name": _cbc["name"], "domain": _clinic_url or _cbc["name"]}],
                                "", claude_key, db_cache=_t5_db_cache, db_type=_cb_db_type,
                                gemini_api_key=gemini_key, research_provider=research_provider,
                            )
                            _scraped_text = _scraped.get(_cbc["name"], "（取得失敗）")
                        except Exception:
                            _scraped_text = "（取得失敗）"

                        st.write(f"✍️ {_r}位: {_cbc['name']} のブロックを生成中...")
                        _cbb_texts   = [b["text"].strip()   for b in st.session_state.get("cb_extra_blocks", []) if b["text"].strip()]
                        _cbb_intents = [b["intent"].strip() for b in st.session_state.get("cb_extra_blocks", []) if b["intent"].strip()]
                        _cb_instr_val = "\n".join(filter(None, _cbb_texts + (["【意図】" + "\n".join(_cbb_intents)] if _cbb_intents else [])))
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
                                extra_instruction=_cb_instr_val,
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
            _edit_key = f"cb_res_edit_{_res['rank']}"
            if _edit_key not in st.session_state:
                st.session_state[_edit_key] = _res["html"]
            _edited_html = st.text_area(
                "HTML（直接編集可）",
                value=st.session_state[_edit_key],
                height=300,
                key=_edit_key,
                label_visibility="collapsed",
            )
            _col_save, _col_dl = st.columns([1, 3])
            with _col_save:
                if st.button("💾 保存", key=f"cb_save_{_res['rank']}"):
                    for _i, _r2 in enumerate(_cb_results):
                        if _r2["rank"] == _res["rank"]:
                            st.session_state["cb_results"][_i]["html"] = st.session_state[_edit_key]
                            break
                    st.success("保存しました")
            with _col_dl:
                st.download_button(
                    f"📥 {_res['rank']}位HTMLをダウンロード",
                    st.session_state[_edit_key],
                    file_name=f"clinic_block_{_res['rank']}_{_res['name'].replace(' ', '_')}.html",
                    mime="text/html",
                    key=f"cb_dl_{_res['rank']}",
                )
            st.divider()

        _all_html = "\n\n".join(
            "<!-- {}位: {} -->\n{}".format(
                r["rank"], r["name"],
                st.session_state.get(f"cb_res_edit_{r['rank']}", r["html"])
            )
            for r in _cb_results
        )
        st.download_button(
            "📥 全件まとめてダウンロード",
            _all_html,
            file_name="clinic_blocks_all.html",
            mime="text/html",
            key="cb_dl_all",
        )

        st.divider()
        st.subheader("全院一括編集")
        _bulk_instr = st.text_area(
            "修正指示（全院に適用）",
            height=100,
            placeholder="例：CTAボタンのテキストを「無料カウンセリングはこちら」に統一／紹介文の語尾を「です・ます」調に統一",
            key="cb_bulk_edit_instruction",
        )
        if st.button("🔄 全院を一括修正", key="cb_bulk_edit_btn"):
            if not _bulk_instr.strip():
                st.warning("修正指示を入力してください")
            elif not claude_key:
                st.error("Claude API Key が未設定です")
            else:
                _updated_results = []
                with st.status("一括修正中...", expanded=True) as _bulk_status:
                    for _res in st.session_state["cb_results"]:
                        _cur_html = st.session_state.get(f"cb_res_edit_{_res['rank']}", _res["html"])
                        st.write(f"✍️ {_res['rank']}位: {_res['name']} を修正中...")
                        try:
                            _new_html = clinic_block_writer.edit_clinic_block(_cur_html, _bulk_instr, claude_key)
                            _updated_results.append({"rank": _res["rank"], "name": _res["name"], "html": _new_html})
                            st.session_state[f"cb_res_edit_{_res['rank']}"] = _new_html
                        except Exception as _be:
                            st.warning(f"{_res['rank']}位 ({_res['name']}) でエラー: {_be}")
                            _updated_results.append(_res)
                    st.session_state["cb_results"] = _updated_results
                    _bulk_status.update(label="✅ 一括修正完了", state="complete")
                st.rerun()


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

    # ── DBデータ取得（TTLキャッシュ：30秒以内は再取得しない） ────────
    _ck = f"_db_nested_cache_{_db_type_sel}"
    _ts_key = f"_db_cache_ts_{_db_type_sel}"
    _cache_age = time.time() - st.session_state.get(_ts_key, 0)
    if _ck not in st.session_state or _cache_age > 30:
        _fresh = clinic_db_manager.load_db(creds_data=_db_creds, sheet_url=_active_db_url)
        if _fresh:
            st.session_state[_ck] = _fresh
            st.session_state[_ts_key] = time.time()
        elif clinic_db_manager.last_load_error and _ck not in st.session_state:
            st.warning(f"⚠️ Sheets読込エラー: {clinic_db_manager.last_load_error}")

    # ── 新規追加フォーム ──────────────────────────────────
    st.subheader("＋ 新規追加")

    _existing_genres = list(st.session_state.get(_ck, {}).keys())
    _genre_options = _existing_genres + ["＋ 新規ジャンル"]
    _db_genre_sel = st.selectbox("ジャンル", _genre_options, key="db_genre_sel")
    if _db_genre_sel == "＋ 新規ジャンル":
        _db_new_genre = st.text_input("新規ジャンル名", placeholder="例：AGA治療", key="db_new_genre_input")
    else:
        _db_new_genre = _db_genre_sel

    with st.form("db_add_form"):
        _db_fa, _db_fb = st.columns([2, 2])
        _db_new_name   = _db_fa.text_input("案件名（クリニック名・商品名等）", placeholder="TCB東京中央美容外科")
        _db_new_domain = _db_fb.text_input("メインURL（任意・ドメイン or パス指定）", placeholder="tcb.net  または  tcb.net/osaka/umeda/")
        _db_new_extra_urls = st.text_area(
            "追加クロールURL（任意・1行1URL）",
            placeholder="https://tcb.net/clinic/\nhttps://tcb.net/price/\n院一覧・料金ページなど。指定URLを起点に最大5ページたどります",
            height=90,
            key="db_new_extra_urls",
        )
        _db_new_extra_instruction = st.text_input(
            "追加クロールの取得指示（任意）",
            placeholder="例：各院の住所と診療時間を全都市分取ってきてほしい",
            key="db_new_extra_instruction",
        )
        _db_new_lp_images = st.file_uploader(
            "LPスクリーンショット（任意・複数可）",
            type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
            key="db_new_lp_images",
            help="LPのスクリーンショットを貼ると、クーポン・料金・訴求軸などLP固有の情報も抽出します。URLなしでLP画像のみでも登録できます。",
        )
        _db_add_now = st.form_submit_button("追加", type="primary", use_container_width=True)

    if _db_add_now:
        _errs_db = []
        if not _db_new_name.strip():
            _errs_db.append("案件名を入力してください")
        if not _db_new_domain.strip() and not _db_new_lp_images:
            _errs_db.append("URL / ドメイン または LPスクリーンショットのいずれかを入力してください")
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
            _has_url = bool(_domain_new)
            _has_lp  = bool(_db_new_lp_images)
            _mode_label = "クロール＋LP" if (_has_url and _has_lp) else ("LP画像のみ" if _has_lp else "クロールのみ")

            _extra_urls_list = [u.strip() for u in _db_new_extra_urls.splitlines() if u.strip() and u.strip().startswith("http")]
            with st.status(f"{_name_new} を取得中（{_mode_label}）...", expanded=True) as _add_status:
                try:
                    _crawl_content = ""
                    _lp_text = ""
                    _extra_content = ""

                    if _has_url:
                        _start_url = _domain_new if _domain_new.startswith("http") else f"https://{_domain_new}"
                        st.write("🔍 トップページ・サイトをクロール中（最大20ページ）...")
                        _crawl_content = crawl_site(_start_url, _genre_new, max_pages=20)
                        for _eu in _extra_urls_list:
                            st.write(f"🔍 追加URL起点クロール中（最大5ページ）: {_eu}")
                            _eu_content = crawl_site(_eu, _genre_new, max_pages=5, restrict_path=False)
                            if _eu_content:
                                _extra_content += f"\n\n--- 追加URL起点: {_eu} ---\n{_eu_content}"

                    if _has_lp:
                        st.write(f"🖼️ LP画像を解析中（{len(_db_new_lp_images)}枚）...")
                        _lp_bytes_list = [f.read() for f in _db_new_lp_images]
                        _lp_text = extract_text_from_lp_images(_lp_bytes_list, _name_new, claude_key, gemini_api_key=gemini_key, research_provider=research_provider)

                    _content_new = build_content_with_lp(_crawl_content, _lp_text, extra_content=_extra_content)
                    _provider_label_db = "Gemini Flash" if research_provider == "gemini" else "Claude Sonnet"
                    st.write(f"🤖 「{_genre_new}」向けに情報抽出中（{_provider_label_db}）...")
                    _info_new = extract_clinic_info_from_content(_content_new, _name_new, _genre_new, claude_key, db_type=_db_type_sel, gemini_api_key=gemini_key, research_provider=research_provider, extra_instruction=_db_new_extra_instruction.strip())
                    clinic_db_manager.upsert_clinic(_name_new, _domain_new, _genre_new, _info_new, lp_info=_lp_text, creds_data=_db_creds, sheet_url=_active_db_url)
                    _add_status.update(label=f"✅ 「{_name_new}」を「{_genre_new}」に追加しました（{_mode_label}）", state="complete")
                    _ck = f"_db_nested_cache_{_db_type_sel}"
                    st.session_state.setdefault(_ck, {}).setdefault(_genre_new, {})[_name_new] = {
                        "domain": _domain_new, "info": _info_new, "updated_at": str(datetime.date.today()),
                    }
                    st.rerun()
                except Exception as _e_new:
                    import traceback as _tb
                    _add_status.update(label="❌ エラー", state="error")
                    st.error(f"取得エラー: {type(_e_new).__name__}: {_e_new}")
                    st.code(_tb.format_exc())

    # ── 記事から一括登録・更新 ───────────────────────────────
    st.divider()
    with st.expander("📄 記事から一括登録・更新", expanded=False):
        st.caption("修正済み記事を貼り付け → クリニック名を抽出 → DBに登録・更新します")
        _art_text = st.text_area(
            "記事テキスト（HTMLまたはプレーンテキスト）",
            height=180,
            placeholder="記事のHTMLまたはテキストをここに貼り付けてください",
            key=f"db_art_text_{_db_type_sel}",
        )
        _art_genre_opts = list(st.session_state.get(_ck, {}).keys()) + ["＋ 新規ジャンル"]
        _art_genre_sel = st.selectbox("ジャンル（登録先）", _art_genre_opts, key="db_art_genre_sel")
        if _art_genre_sel == "＋ 新規ジャンル":
            _art_genre = st.text_input("新規ジャンル名", key="db_art_new_genre")
        else:
            _art_genre = _art_genre_sel

        _art_extract_btn = st.button(
            "🔍 クリニック名を自動抽出",
            key="db_art_extract",
            disabled=not _art_text.strip(),
        )
        if _art_extract_btn:
            if not claude_key:
                st.error("Claude API Key が未設定です")
            else:
                with st.spinner("クリニック名を抽出中..."):
                    _extracted_names = extract_clinic_names_from_article(
                        _art_text, claude_key, gemini_api_key=gemini_key, research_provider=research_provider
                    )
                    st.session_state[f"db_art_clinics_edit_{_db_type_sel}"] = "\n".join(_extracted_names)

        _art_clinics_edited = st.text_area(
            "登録・更新するクリニック名（1行1院名・編集可）",
            height=130,
            placeholder="「クリニック名を自動抽出」で自動入力されます。直接入力も可能です。",
            key=f"db_art_clinics_edit_{_db_type_sel}",
        )
        st.caption("不要な院は削除、追加したい院は追記してください")

        if st.button(
            "📥 登録・更新する",
            key="db_art_register",
            disabled=not (_art_clinics_edited.strip() and _art_text.strip()),
            type="primary",
            use_container_width=True,
        ):
            _target_names = [n.strip() for n in _art_clinics_edited.splitlines() if n.strip()]
            if not _art_genre.strip():
                st.error("ジャンルを選択してください")
            elif not claude_key:
                st.error("Claude API Key が未設定です")
            else:
                _art_article_content = build_content_with_lp(
                    "", "", extra_content=f"--- 記事テキスト ---\n{_art_text.strip()}"
                )
                _full_db_art = st.session_state.get(_ck, {})
                with st.status(f"{len(_target_names)} 件を登録・更新中...", expanded=True) as _art_status:
                    try:
                        for _art_name in _target_names:
                            st.write(f"🤖 「{_art_name}」の情報を抽出中...")
                            _art_domain = ""
                            for _gv in _full_db_art.values():
                                if isinstance(_gv, dict) and _art_name in _gv:
                                    _art_domain = _gv[_art_name].get("domain", "")
                                    break
                            _art_ci = extract_clinic_info_from_content(
                                _art_article_content, _art_name, _art_genre, claude_key,
                                db_type=_db_type_sel, gemini_api_key=gemini_key, research_provider=research_provider,
                            )
                            clinic_db_manager.upsert_clinic(
                                _art_name, _art_domain, _art_genre, _art_ci,
                                creds_data=_db_creds, sheet_url=_active_db_url,
                            )
                            st.session_state.setdefault(_ck, {}).setdefault(_art_genre, {})[_art_name] = {
                                "domain": _art_domain, "info": _art_ci, "updated_at": str(datetime.date.today()),
                            }
                            st.write("　✅ 完了")
                        _art_status.update(label=f"✅ {len(_target_names)} 件の登録・更新が完了しました", state="complete")
                        st.session_state.pop(f"db_art_clinics_edit_{_db_type_sel}", None)
                        st.rerun()
                    except Exception as _art_e:
                        _art_status.update(label="❌ エラー", state="error")
                        st.error(f"エラー: {_art_e}")

    # ── ジャンル別タブ表示 ──────────────────────────────────
    st.divider()
    _db_nested = st.session_state.get(_ck, {})
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
                                _full_db_now = st.session_state.get(_ck, {})
                                for _dn in sorted(_g_entries):
                                    _de = _g_entries[_dn]
                                    st.write(f"🔍 {_dn} をクロール中...")
                                    try:
                                        _clinic_genres_all = [g for g, ge in _full_db_now.items() if _dn in ge]
                                        _dom = _de.get("domain", _dn)
                                        _start = _dom if _dom.startswith("http") else f"https://{_dom}"
                                        _old_sanko_b = _parse_sanko_urls(_de.get("info", ""))
                                        _content_b = crawl_site(_start, _clinic_genres_all[0] if _clinic_genres_all else "", max_pages=20)
                                        for _su_b in _old_sanko_b:
                                            st.write(f"　🔍 参照URL起点クロール中（最大5ページ）: {_su_b}")
                                            _su_b_content = crawl_site(_su_b, _clinic_genres_all[0] if _clinic_genres_all else "", max_pages=5, restrict_path=False)
                                            if _su_b_content:
                                                _content_b += f"\n\n--- 参照URL起点: {_su_b} ---\n{_su_b_content}"
                                        for _cg in _clinic_genres_all:
                                            _ci = extract_clinic_info_from_content(_content_b, _dn, _cg, claude_key, db_type=_db_type_sel, gemini_api_key=gemini_key, research_provider=research_provider)
                                            _ci = _merge_sanko_urls_in_info(_ci, _old_sanko_b)
                                            clinic_db_manager.upsert_clinic(_dn, _dom, _cg, _ci, affili_filename=_de.get("affili_filename", ""), creds_data=_db_creds, sheet_url=_active_db_url)
                                        st.write("　→ ✅ 完了")
                                    except Exception as _be:
                                        st.write(f"　→ ❌ エラー: {_be}")
                                _batch_st.update(label="✅ 一括取得完了", state="complete")
                            st.session_state.pop(f"_db_nested_cache_{_db_type_sel}", None)  # 一括後は全量再取得を優先
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
                            _d_affili = _de.get("affili_filename", "")
                            _aff_col, _url_col = st.columns([2, 3])
                            _d_affili_edited = _aff_col.text_input(
                                "アフィリファイル名",
                                value=_d_affili,
                                key=f"db_affili_{_g_name}_{_dn}",
                                placeholder="diet-dmm-ryb.html",
                            )
                            _url_col.caption(f"URL: {_de.get('domain', '')}")
                            if _aff_col.button("💾 ファイル名を保存", key=f"db_save_affili_{_g_name}_{_dn}"):
                                clinic_db_manager.upsert_clinic(
                                    _dn, _de.get("domain", ""), _g_name, _de.get("info", ""),
                                    affili_filename=_d_affili_edited,
                                    creds_data=_db_creds, sheet_url=_active_db_url,
                                )
                                st.success("アフィリファイル名を保存しました")
                                _ck = f"_db_nested_cache_{_db_type_sel}"
                                st.session_state.setdefault(_ck, {}).setdefault(_g_name, {}).setdefault(_dn, {})["affili_filename"] = _d_affili_edited

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
                                    affili_filename=_de.get("affili_filename", ""),
                                    creds_data=_db_creds, sheet_url=_active_db_url,
                                )
                                st.success("保存しました")
                                _ck = f"_db_nested_cache_{_db_type_sel}"
                                st.session_state.setdefault(_ck, {}).setdefault(_g_name, {}).setdefault(_dn, {})["info"] = _d_info_edited
                                st.session_state[_ck][_g_name][_dn]["updated_at"] = str(datetime.date.today())
                                st.rerun()

                            st.divider()
                            st.caption("更新モード")
                            # [要確認]フィールドを自動検出
                            _missing_fields = [
                                ln.split("：")[0].strip()
                                for ln in _d_info.splitlines()
                                if "[要確認]" in ln
                                and not ln.strip().startswith(("■", "【", "#"))
                                and ln.split("：")[0].strip()
                            ]
                            _fill_gaps_instr = (
                                f"以下のフィールドが未取得です。サイト内を重点的に探して埋めてください：{', '.join(_missing_fields)}"
                                if _missing_fields else ""
                            )
                            _upd_extra_urls = st.text_area(
                                "追加クロールURL（任意・1行1URL）",
                                placeholder="https://tcb.net/clinic/\nhttps://tcb.net/price/\n院一覧・料金ページなど。指定URLを起点に最大5ページたどります",
                                height=80,
                                key=f"db_extra_urls_{_g_name}_{_dn}",
                            )
                            _upd_extra_instruction = st.text_input(
                                "追加クロールの取得指示（任意）",
                                placeholder="例：各院の住所と診療時間を全都市分取ってきてほしい",
                                key=f"db_extra_instr_{_g_name}_{_dn}",
                            )
                            _upd_paste_info = st.text_area(
                                "手動貼り付け情報（任意）",
                                placeholder="料金表・住所一覧・診療時間など、直接貼り付けたい情報。\nクロール結果と組み合わせて使えます。URLが未登録でもこれだけで抽出可能です。",
                                height=120,
                                key=f"db_paste_{_g_name}_{_dn}",
                            )
                            _upd_lp_imgs = st.file_uploader(
                                "LPスクリーンショット（LP更新時に添付）",
                                type=["png", "jpg", "jpeg", "webp"],
                                accept_multiple_files=True,
                                key=f"db_lp_imgs_{_g_name}_{_dn}",
                            )
                            _upd_c1, _upd_c2, _upd_c3, _upd_c4 = st.columns([3, 3, 3, 2])
                            _do_crawl_only  = _upd_c1.button("🔄 再クロールのみ", key=f"db_crawl_{_g_name}_{_dn}")
                            _do_lp_only     = _upd_c2.button("🖼️ LP更新のみ",    key=f"db_lp_{_g_name}_{_dn}")
                            _do_both        = _upd_c3.button("🔄🖼️ 両方",         key=f"db_both_{_g_name}_{_dn}")
                            _do_delete      = _upd_c4.button("🗑️ 削除",           key=f"db_del_{_g_name}_{_dn}")
                            _do_fill_gaps   = st.button(
                                f"✨ 空欄を補完（未取得 {len(_missing_fields)} 項目）" if _missing_fields else "✨ 空欄を補完（未取得なし）",
                                key=f"db_fill_{_g_name}_{_dn}",
                                disabled=not _missing_fields,
                                use_container_width=True,
                            )
                            _do_paste_only  = st.button(
                                "📋 貼り付けで更新",
                                key=f"db_paste_btn_{_g_name}_{_dn}",
                                disabled=not _upd_paste_info.strip(),
                                use_container_width=True,
                            )

                            if _do_delete:
                                clinic_db_manager.delete_clinic(_dn, genre=_g_name, creds_data=_db_creds, sheet_url=_active_db_url)
                                st.success(f"「{_dn}」を「{_g_name}」から削除しました")
                                _ck = f"_db_nested_cache_{_db_type_sel}"
                                if _ck in st.session_state:
                                    st.session_state[_ck].get(_g_name, {}).pop(_dn, None)
                                st.rerun()

                            _need_update = _do_crawl_only or _do_lp_only or _do_both or _do_fill_gaps or _do_paste_only
                            if _need_update:
                                if not claude_key:
                                    st.error("Claude API Key が未設定です")
                                elif (_do_lp_only or _do_both) and not _upd_lp_imgs:
                                    st.error("LP更新にはスクリーンショットを添付してください")
                                else:
                                    _use_crawl = _do_crawl_only or _do_both or _do_fill_gaps
                                    _use_lp    = _do_lp_only or _do_both
                                    _mode_str  = (
                                        f"空欄補完（{len(_missing_fields)}項目）" if _do_fill_gaps else
                                        "貼り付けで更新" if _do_paste_only else
                                        "再クロール＋LP" if (_use_crawl and _use_lp) else
                                        "LP更新のみ" if _use_lp else "再クロールのみ"
                                    )
                                    _upd_extra_list = [
                                        u.strip() for u in _upd_extra_urls.splitlines()
                                        if u.strip().startswith("http")
                                    ]
                                    _old_sanko_urls = _parse_sanko_urls(_d_info)
                                    if _use_crawl:
                                        for _su in _old_sanko_urls:
                                            if _su not in _upd_extra_list:
                                                _upd_extra_list.append(_su)
                                    with st.status(f"{_dn} を更新中（{_mode_str}）...", expanded=True) as _upd_st:
                                        try:
                                            _full_db2 = st.session_state.get(_ck, {})
                                            _clinic_genres2 = [g for g, ge in _full_db2.items() if _dn in ge]
                                            if not _clinic_genres2:
                                                _clinic_genres2 = [_g_name]
                                            _dom2 = _de.get("domain", "")

                                            _crawl_content2 = ""
                                            _lp_text2 = ""
                                            _extra_content2 = ""
                                            if _upd_paste_info.strip():
                                                _extra_content2 += f"\n\n--- 手動貼り付け情報 ---\n{_upd_paste_info.strip()}"

                                            if _use_crawl:
                                                if not _dom2:
                                                    st.warning("URLが未登録のためクロールをスキップしました")
                                                else:
                                                    _start2 = _dom2 if _dom2.startswith("http") else f"https://{_dom2}"
                                                    st.write("🔍 クロール中（最大20ページ）...")
                                                    _crawl_content2 = crawl_site(_start2, _clinic_genres2[0] if _clinic_genres2 else "", max_pages=20)
                                                    for _eu2 in _upd_extra_list:
                                                        st.write(f"🔍 追加URL起点クロール中（最大5ページ）: {_eu2}")
                                                        _eu2_content = crawl_site(_eu2, _clinic_genres2[0] if _clinic_genres2 else "", max_pages=5, restrict_path=False)
                                                        if _eu2_content:
                                                            _extra_content2 += f"\n\n--- 追加URL起点: {_eu2} ---\n{_eu2_content}"

                                            if _use_lp:
                                                st.write(f"🖼️ LP画像を解析中（{len(_upd_lp_imgs)}枚）...")
                                                _lp_bytes2 = [f.read() for f in _upd_lp_imgs]
                                                _lp_text2 = extract_text_from_lp_images(_lp_bytes2, _dn, claude_key, gemini_api_key=gemini_key, research_provider=research_provider)

                                            _combined2 = build_content_with_lp(_crawl_content2, _lp_text2, extra_content=_extra_content2)
                                            _upd_instr = (
                                                _fill_gaps_instr if _do_fill_gaps
                                                else st.session_state.get(f"db_extra_instr_{_g_name}_{_dn}", "")
                                            )
                                            _ck = f"_db_nested_cache_{_db_type_sel}"
                                            for _cg2 in _clinic_genres2:
                                                st.write(f"🤖 「{_cg2}」向けに情報抽出中...")
                                                _ci2 = extract_clinic_info_from_content(_combined2, _dn, _cg2, claude_key, db_type=_db_type_sel, gemini_api_key=gemini_key, research_provider=research_provider, extra_instruction=_upd_instr.strip())
                                                _ci2 = _merge_sanko_urls_in_info(_ci2, _old_sanko_urls)
                                                clinic_db_manager.upsert_clinic(_dn, _dom2, _cg2, _ci2, affili_filename=_de.get("affili_filename", ""), creds_data=_db_creds, sheet_url=_active_db_url)
                                                st.session_state.setdefault(_ck, {}).setdefault(_cg2, {})[_dn] = {
                                                    "domain": _dom2, "info": _ci2, "updated_at": str(datetime.date.today()),
                                                    "affili_filename": _de.get("affili_filename", ""),
                                                }
                                            _upd_st.update(label=f"✅ 更新完了（{_mode_str}・{len(_clinic_genres2)} ジャンル）", state="complete")
                                            st.session_state.pop(f"db_info_{_g_name}_{_dn}", None)
                                            st.rerun()
                                        except Exception as _rr_e:
                                            _upd_st.update(label="❌ エラー", state="error")
                                            st.error(f"エラー: {_rr_e}")


# ════════════════════════════════════════════════════════
#  画像生成セクション
# ════════════════════════════════════════════════════════
if tab_image_gen:
    st.title("🖼️ 画像生成")
    st.caption("記事HTMLを貼り付けてH2ごとに画像を生成し、Driveにアップロードします。")

    _ig_sites = site_config_manager.list_sites(_site_cfg_creds, _site_cfg_parent_folder)
    _ig_col1, _ig_col2 = st.columns([2, 2])
    _ig_site = _ig_col1.selectbox("サイト *", ["-- 選択 --"] + _ig_sites, key="ig_site")
    _ig_slug = _ig_col2.text_input("スラッグ *", key="ig_slug", placeholder="aga-treatment-tokyo")
    _ig_html = st.text_area("記事HTML *", height=200, key="ig_html",
                             placeholder="<h1>...</h1>\n<h2>AGAとは</h2>\n<p>...</p>")

    if st.button("🖼️ 画像を生成", type="primary", key="ig_gen_all"):
        _ig_errs = []
        if _ig_site == "-- 選択 --":
            _ig_errs.append("サイトを選択してください")
        if not _ig_slug.strip():
            _ig_errs.append("スラッグを入力してください")
        if not _ig_html.strip():
            _ig_errs.append("記事HTMLを入力してください")
        if not gemini_key:
            _ig_errs.append("Gemini API Key が未設定です（画像案の生成に使用）")
        for _ig_e in _ig_errs:
            st.error(_ig_e)

        if not _ig_errs:
            _ig_sc = site_config_manager.load_site_config(_ig_site, _site_cfg_creds, _site_cfg_parent_folder)
            if not _ig_sc.get("design_system"):
                st.error(f"「{_ig_site}」にデザインシステムが未登録です。サイト設定で登録してください。")
            else:
                _ig_creds = _get_gcp_creds(sheets_creds_file)
                with st.status("画像を生成中...", expanded=True) as _ig_status:
                    try:
                        # 参照画像をキャッシュから取得
                        _ig_ref_key = f"ref_images_{_ig_site}"
                        if _ig_ref_key not in st.session_state:
                            st.write("☁️ 参照画像をDriveから読み込み中...")
                            st.session_state[_ig_ref_key] = image_generator.load_reference_images_from_drive(
                                _ig_site, _ig_creds, _drive_folder_id,
                            ) if _ig_creds else []
                        _ig_ref_imgs = st.session_state[_ig_ref_key]
                        st.write(f"　→ 参照画像: {len(_ig_ref_imgs)} 枚")

                        st.write("💡 画像案を生成中（Gemini）...")
                        _ig_results = image_generator.generate_images_for_article(
                            article_text=_ig_html,
                            site_config=_ig_sc,
                            reference_images=_ig_ref_imgs,
                            provider=image_provider,
                            gemini_api_key=gemini_key,
                            openai_api_key=openai_key,
                        )
                        st.write(f"　→ {len(_ig_results)} 案を生成")

                        st.session_state["ig_results"] = _ig_results
                        _ig_status.update(label="✅ 生成完了", state="complete")
                    except Exception as _ig_ge:
                        _ig_status.update(label="❌ エラー", state="error")
                        st.error(str(_ig_ge))

    # ── 生成結果表示 ──────────────────────────────────────────
    if st.session_state.get("ig_results"):
        st.divider()
        st.subheader("生成結果")
        _ig_disp = st.session_state["ig_results"]

        for _ig_di, _ig_dr in enumerate(_ig_disp):
            _ig_proposal = _ig_dr["proposal"]
            _ig_img_bytes = _ig_dr["bytes"]
            with st.expander(
                f"📷 {_ig_di + 1}. {_ig_proposal.get('placement', '不明')}",
                expanded=True,
            ):
                _ig_rc1, _ig_rc2 = st.columns([1, 1])
                with _ig_rc1:
                    if _ig_img_bytes:
                        st.image(_ig_img_bytes)
                    else:
                        st.info("画像の生成に失敗しました")
                with _ig_rc2:
                    st.caption(f"**目的**: {_ig_proposal.get('purpose', '')}")
                    st.caption(f"**構図**: {_ig_proposal.get('layout_type', '')}")
                    _ig_instr = st.text_input(
                        "修正指示（任意）", key=f"ig_instr_{_ig_di}",
                        placeholder="もっと明るいトーンで / 比較表レイアウトで",
                    )
                    _ig_bc1, _ig_bc2 = st.columns(2)

                    if _ig_bc1.button("🔄 再生成", key=f"ig_regen_{_ig_di}", use_container_width=True):
                        _ig_sc_regen = site_config_manager.load_site_config(_ig_site, _site_cfg_creds, _site_cfg_parent_folder)
                        _ig_ds_regen = image_generator.build_design_system(_ig_sc_regen)
                        _ig_ref_regen = st.session_state.get(f"ref_images_{_ig_site}", [])
                        _regen_proposal = dict(_ig_proposal)
                        if _ig_instr.strip():
                            _regen_proposal["additional_instruction"] = _ig_instr.strip()
                        _regen_prompt = image_generator.build_generation_prompt(
                            _ig_ds_regen, _regen_proposal, "16:9", bool(_ig_ref_regen)
                        )
                        if _ig_instr.strip():
                            _regen_prompt += f"\n\n修正指示: {_ig_instr.strip()}"
                        with st.spinner("再生成中..."):
                            try:
                                _regen_bytes = image_generator.generate_image_bytes(
                                    _regen_prompt,
                                    reference_images=_ig_ref_regen or None,
                                    provider=image_provider,
                                    gemini_api_key=gemini_key,
                                    openai_api_key=openai_key,
                                )
                                st.session_state["ig_results"][_ig_di]["bytes"] = _regen_bytes
                                st.rerun()
                            except Exception as _regen_e:
                                st.error(f"再生成エラー: {_regen_e}")

                    if _ig_img_bytes:
                        if _ig_bc2.button("⬆️ アップロード", key=f"ig_upload_{_ig_di}", use_container_width=True):
                            try:
                                _ig_uc = _get_gcp_creds(sheets_creds_file)
                                _ig_fname = f"{_ig_slug.strip()}-img{_ig_di+1}.png"
                                drive_uploader.upload_image(
                                    _ig_img_bytes, _ig_fname,
                                    _ig_site, _ig_slug.strip(),
                                    _ig_uc, _drive_folder_id,
                                )
                                st.success(f"✅ {_ig_fname} をアップロードしました")
                            except Exception as _ig_ue:
                                st.error(f"アップロードエラー: {_ig_ue}")

        st.divider()
        if st.button("⬆️ 全件アップロード", key="ig_upload_all", type="primary", use_container_width=True):
            _ig_all_creds = _get_gcp_creds(sheets_creds_file)
            _ig_all_ok = 0
            for _ig_ai, _ig_ar in enumerate(st.session_state.get("ig_results", [])):
                if _ig_ar["bytes"]:
                    try:
                        _ig_all_fname = f"{_ig_slug.strip()}-img{_ig_ai+1}.png"
                        drive_uploader.upload_image(
                            _ig_ar["bytes"], _ig_all_fname,
                            _ig_site, _ig_slug.strip(),
                            _ig_all_creds, _drive_folder_id,
                        )
                        _ig_all_ok += 1
                    except Exception as _ig_ae:
                        st.warning(f"エラー (img{_ig_ai+1}): {_ig_ae}")
            st.success(f"✅ {_ig_all_ok} 件アップロード完了")


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
