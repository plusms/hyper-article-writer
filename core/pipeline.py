"""記事1本を作る工程。画面から切り離してある。

Streamlit の中でしか動かないと、20本流している間ブラウザを開き続けることになり、
接続が切れた時点で全部やり直しになる。ここには st を持ち込まない。ログは呼び出し側が
渡す関数に流す。ツールの画面もコマンドからの実行も、同じこの関数を呼ぶ。
"""

import re

from core import article_review, article_type_db, clinic_block_writer, clinic_db_manager
from core import facility_db, output_check
from core.planner import generate_structure
from core.researcher import analyze_competitors
from core.sheets import read_site_info
from core.writer import WRITING_RULES, extract_criteria_summary, generate_body

# writer が地域・比較記事で埋め込むプレースホルダ。空白の揺れを拾えるようにする
CLINIC_BLOCK_RE = re.compile(r"<!--\s*クリニック紹介ブロック入る\s*-->")

# モデルを呼ぶ検品をかける掲載順位の上限。ここまでがリンクとCTAと料金表を持つ。
REVIEW_BLOCK_RANK_MAX = 3
# ここから下はまとめて1回で書かせる。2〜3段落でリンクもCTAも無いので1院1コールは無駄。
BATCH_BLOCK_RANK_FROM = 4
# 1回のまとめ生成に入れる院数。7院を1回に詰めると入力が9千字を超えて、
# 渡してあるデータをモデルが読み切れず「DBに情報がない」と書く。仙台3本目で
# 要確認8件がこれだった。3院ずつなら4千字前後に収まる。
BATCH_BLOCK_CHUNK = 3


class Settings:
    """1回の実行で共通の設定。記事ごとに変わらないものだけ持つ。"""

    def __init__(self, claude_key="", gemini_key="", article_provider="claude",
                 research_provider="gemini", gcp_creds=None, db_sheet_url="",
                 site_info_sheet_url="", site_parts="", site_name="", auto_review=True):
        self.claude_key = claude_key
        self.gemini_key = gemini_key
        self.article_provider = article_provider
        self.research_provider = research_provider
        self.gcp_creds = gcp_creds
        self.db_sheet_url = db_sheet_url
        self.site_info_sheet_url = site_info_sheet_url
        self.site_parts = site_parts
        self.site_name = site_name
        self.auto_review = auto_review


def _noop(_msg: str) -> None:
    pass


def review_constraints(inputs: dict, clinic_info: dict, type_record: dict | None = None) -> str:
    """構成の検品に渡す制約。案件DBにある案件しか使えないことを明示する。"""
    lines = [
        f"記事タイプ: {inputs.get('article_type', '')}",
        f"メインキーワード: {inputs.get('main_kw', '')}",
        f"サブキーワード: {', '.join(inputs.get('sub_kw', []) or []) or '（なし）'}",
    ]
    names = list(clinic_info.keys())
    if names:
        lines.append(f"紹介できる案件: {', '.join(names)}（このリスト以外は紹介しない）")
    else:
        lines.append("案件の紹介ブロックは設けない")
    constraints = article_type_db.build_constraints(type_record or {})
    if constraints:
        lines.append(constraints)
    return "\n".join(lines)


def load_article_type(inputs: dict, settings: Settings) -> dict:
    if not (settings.gcp_creds and settings.db_sheet_url):
        return {}
    try:
        rows = article_type_db.load_types(settings.gcp_creds, settings.db_sheet_url)
    except Exception:
        return {}
    return article_type_db.pick_type(
        rows, inputs.get("article_type", ""), inputs.get("genre", "")
    )


def attach_facilities(clinic_info: dict, inputs: dict, settings: Settings, log=_noop) -> dict:
    """地域記事なら、その地域の院情報を案件情報に足して返す。

    その地域に院がない案件は落とす。来院できない案件を紹介しても意味がない。
    """
    if inputs.get("article_type") != "地域":
        return clinic_info
    if not (settings.gcp_creds and settings.db_sheet_url):
        return clinic_info
    rows = facility_db.load_facilities(settings.gcp_creds, settings.db_sheet_url)
    if not rows:
        return clinic_info
    region = inputs.get("region", "").strip()
    if not region:
        region = facility_db.pick_region(inputs.get("main_kw", ""), facility_db.list_regions(rows))
    if not region:
        log("　→ 院タブに一致する地域がありません。院情報なしで生成します")
        return clinic_info
    ok, blocked = facility_db.select_for_article(rows, region, list(clinic_info.keys()))
    if not ok:
        log(f"　→ 院タブに{region}の該当案件がありません。院情報なしで生成します")
        return clinic_info
    dropped: dict = {}
    for name in clinic_info:
        matched = ok.get(name, [])
        if not matched:
            dropped[name] = blocked.get(name, ["院タブに行がない"])
        elif not facility_db.has_real_facility(matched):
            dropped[name] = [f"{region}に院がない"]
    for name in dropped:
        ok.pop(name, None)
    kept = {n: t for n, t in clinic_info.items() if n not in dropped}
    log(f"　→ 院タブ（{region}）: {len(ok)} 案件ぶんの院情報を使います")
    for name, reasons in dropped.items():
        log(f"　→ 記事に載せません: {name}（{' / '.join(reasons)}）")
    return facility_db.attach_to_clinic_info(kept, ok)


def sync_clinic_list(inputs: dict, clinic_info: dict, log=_noop) -> None:
    """記事に載せる案件だけを inputs["clinics"] に残す。"""
    before = inputs.get("clinics", [])
    if not before:
        return
    after = [c for c in before if c.get("name") in clinic_info]
    if len(after) == len(before):
        return
    removed = [c.get("name", "") for c in before if c.get("name") not in clinic_info]
    inputs["clinics"] = after
    if inputs.get("clinic_count", 0) > len(after):
        inputs["clinic_count"] = len(after)
    log(f"　→ 掲載案件から外しました: {'、'.join(removed)}（残り {len(after)} 案件）")


def push_rank(record: dict) -> int:
    try:
        return int(str(record.get("推し順位", "")).strip())
    except (TypeError, ValueError):
        return 999


def check_block(block_html: str, source_text: str, rank: int, log=_noop) -> None:
    """モデルを使わない検品。入力データに無い金額と禁止ワードを拾って知らせる。"""
    try:
        found = output_check.run_checks(block_html, source_text)
    except Exception:
        return
    items = found.get("fix", []) + found.get("human", [])
    if items:
        log(f"　→ {rank}位の紹介ブロックに機械チェックの指摘 {len(items)}件")
        for item in items[:5]:
            log(f"　　- [{item.get('rule', '')}] {item.get('text', '')[:40]}")


def review_block(block_html: str, source_text: str, inputs: dict, settings: Settings,
                 rank: int = 1, log=_noop) -> str:
    """紹介ブロックを別モデルで検品して直す。モデルを呼ぶのは上位3院まで。"""
    check_block(block_html, source_text, rank, log=log)
    if rank > REVIEW_BLOCK_RANK_MAX:
        return block_html
    if not (settings.auto_review and settings.claude_key and settings.gemini_key):
        return block_html
    try:
        result = article_review.run_review_loop(
            block_html, WRITING_RULES,
            writer_provider=settings.article_provider,
            claude_api_key=settings.claude_key, gemini_api_key=settings.gemini_key,
            article_type=inputs.get("article_type", ""),
            main_kw=inputs.get("main_kw", ""),
            source_text=source_text,
            max_rounds=1,
            progress=log,
        )
    except Exception as e:
        log(f"　→ 紹介ブロックの検品に失敗しました（{type(e).__name__}）。そのまま使います")
        return block_html
    if result.get("human"):
        log(f"　→ 紹介ブロックで人が見る指摘 {len(result['human'])}件")
    return result.get("html") or block_html


def _link_rule(base_link: str, slug: str) -> str:
    if not (base_link and slug):
        return ""
    return (
        "【リンクのパラメータ】\n"
        f"- 見出し・本文中のテキストリンク: {base_link}?{slug}_rank_txt\n"
        f"- 画像のリンク: {base_link}?{slug}_rank_bn\n"
        f"- CTAボタン: {base_link}?{slug}_rank_bt\n"
    )


def fill_clinic_blocks(html: str, clinic_info: dict, records: dict, inputs: dict,
                       type_record: dict, settings: Settings, log=_noop) -> str:
    """本文のプレースホルダを紹介ブロックで置き換える。

    上位3院は1院1コール。4位以降はまとめて1コール。4位以降は2〜3段落で
    リンクもCTAも無いので、1院ずつ呼ぶと待ち時間が伸びるだけになる。
    """
    if not CLINIC_BLOCK_RE.search(html) or not clinic_info:
        return html
    criteria = extract_criteria_summary(html, settings.claude_key)
    reference = article_type_db.get_reference_html(type_record or {}, "紹介ブロックの並び")
    trim = article_type_db.get_reference_html(type_record or {}, "紹介ブロックで削るもの")
    slug = inputs.get("slug", "").strip()
    ordered = sorted(clinic_info.items(), key=lambda kv: push_rank(records.get(kv[0], {})))

    blocks = []
    lower = []
    for rank, (name, info) in enumerate(ordered, start=1):
        if rank >= BATCH_BLOCK_RANK_FROM:
            lower.append((rank, name, info))
            continue
        record = records.get(name, {})
        base_link = str(record.get("送客リンク", "")).strip()
        instruction = "\n".join(filter(None, [
            ("【見本から削るもの・残すもの】\n" + trim) if trim else "",
            _link_rule(base_link, slug),
        ]))
        log(f"　🏥 {rank}位 {name} の紹介ブロックを生成中...")
        try:
            block = clinic_block_writer.generate_clinic_block(
                name=name, rank=rank, scraped_info=info,
                price_data=str(record.get("紹介ブロックに出すプラン", "")).strip(),
                extra_notes="", link_url=base_link,
                lp_plan=str(record.get("比較表に出すプラン", "")).strip(),
                main_kw=inputs.get("main_kw", ""), sub_kw=inputs.get("sub_kw", []),
                criteria_text=criteria, claude_api_key=settings.claude_key,
                site_parts=settings.site_parts, reference_html=reference,
                extra_instruction=instruction, article_type=inputs.get("article_type", ""),
                gemini_api_key=settings.gemini_key, article_provider=settings.article_provider,
            )
        except Exception as e:
            log(f"　→ {rank}位 {name} で失敗: {e}")
            continue
        block = review_block(block, info, inputs, settings, rank=rank, log=log)
        blocks.append(block)
        reference = block

    for start in range(0, len(lower), BATCH_BLOCK_CHUNK):
        chunk = lower[start:start + BATCH_BLOCK_CHUNK]
        log(f"　🏥 {chunk[0][0]}位から{chunk[-1][0]}位の {len(chunk)} 院をまとめて生成中...")
        entries = [
            {
                "rank": rank,
                "name": name,
                "info": info,
                "price_data": str(records.get(name, {}).get("紹介ブロックに出すプラン", "")).strip(),
            }
            for rank, name, info in chunk
        ]
        try:
            lower_html = clinic_block_writer.generate_lower_blocks(
                entries,
                main_kw=inputs.get("main_kw", ""), sub_kw=inputs.get("sub_kw", []),
                criteria_text=criteria, claude_api_key=settings.claude_key,
                site_parts=settings.site_parts, reference_html=reference,
                article_type=inputs.get("article_type", ""),
                gemini_api_key=settings.gemini_key, article_provider=settings.article_provider,
            )
        except Exception as e:
            log(f"　→ 4位以降のまとめ生成に失敗: {e}")
            lower_html = ""
        if lower_html:
            lower_html = fix_lower_blocks(lower_html, chunk, inputs, settings, log=log)
            blocks.append(lower_html)
            reference = lower_html

    if not blocks:
        return html
    filled = CLINIC_BLOCK_RE.sub(lambda _m: "\n".join(blocks), html, count=1)
    if CLINIC_BLOCK_RE.search(filled):
        log("　→ 紹介ブロックの差し込み口が2つ以上あります。1つ目だけ埋めました")
    return filled


def fix_lower_blocks(html: str, lower: list, inputs: dict, settings: Settings, log=_noop) -> str:
    """4位以降のまとめブロックを直す。

    1院ずつモデルの検品をかけると回数が増えるので、まとめた1つに対して
    機械の置き換え1回とモデルの修正1回だけかける。
    """
    fixed, replaced = output_check.apply_mechanical_fixes(html)
    if replaced:
        log("　→ 4位以降を機械で置き換え: "
            + "、".join(f"{b}→{a or '削除'}" for b, a in replaced))

    source_text = "\n\n".join(info for _rank, _name, info in lower)
    try:
        found = output_check.run_checks(fixed, source_text)
    except Exception:
        return fixed
    for item in found.get("human", []):
        log(f"　→ 4位以降で人が見る指摘: [{item.get('rule', '')}] {item.get('text', '')[:40]}")
    to_fix = found.get("fix", [])
    if not to_fix:
        return fixed
    log(f"　→ 4位以降の残り {len(to_fix)}件をまとめて直します")
    if not (settings.auto_review and settings.claude_key):
        return fixed
    try:
        repaired = article_review.apply_fixes(
            fixed, to_fix, settings.article_provider,
            claude_api_key=settings.claude_key, gemini_api_key=settings.gemini_key,
        )
    except Exception as e:
        log(f"　→ 4位以降の修正に失敗しました（{type(e).__name__}）。そのまま使います")
        return fixed
    if not repaired or len(repaired) < len(fixed) * 0.7:
        log("　→ 修正後が短くなりすぎたので元のまま使います")
        return fixed
    return repaired


def generate_article(inputs: dict, settings: Settings, log=_noop, on_body=None) -> dict:
    """記事1本を最後まで作る。

    on_body は本文ができた時点で呼ばれる。途中で落ちたときに本文を残すために使う。
    Returns: {"title", "meta", "html", "todo_list", "clinics"}
    """
    comp = analyze_competitors(
        inputs.get("competitor_urls", []), settings.claude_key,
        gemini_api_key=settings.gemini_key, research_provider=settings.research_provider,
    )

    inputs["clinics"] = clinic_db_manager.list_clinics_by_rank(
        inputs.get("genre", ""), creds_data=settings.gcp_creds, sheet_url=settings.db_sheet_url,
    )
    if not inputs["clinics"]:
        raise RuntimeError("案件DBから案件を取れませんでした。ジャンルの指定と案件DBのタブを確認してください")
    inputs["clinic_count"] = len(inputs["clinics"])
    log("　→ 案件DBの推し順位順に "
        f"{len(inputs['clinics'])} 案件: " + "、".join(c["name"] for c in inputs["clinics"]))

    dupes = clinic_db_manager.find_duplicate_names(
        settings.gcp_creds, settings.db_sheet_url, inputs.get("genre", ""),
    )
    if dupes:
        log("　→ 案件DBに二重登録: " + "、".join(dupes) + "（推し順位の小さいほうを使います）")

    clinics, _registered, blocked = clinic_db_manager.collect_via_db(
        inputs["clinics"], inputs.get("genre", ""),
        creds_data=settings.gcp_creds, sheet_url=settings.db_sheet_url, progress=log,
    )
    log(f"　→ 案件DBから取得: {len(clinics)} 案件")
    if blocked:
        log("　→ 照合を通らず記事に載せません: " + " / ".join(blocked))
    clinics = attach_facilities(clinics, inputs, settings, log=log)
    if not clinics and inputs["clinics"]:
        raise RuntimeError("案件DBに使える案件が1件もありません")
    sync_clinic_list(inputs, clinics, log=log)

    type_record = load_article_type(inputs, settings)
    if type_record:
        log(f"　→ 型を適用: {inputs.get('article_type', '')}／{inputs.get('genre', '')}")

    structure = generate_structure(
        inputs, comp, clinics, settings.claude_key, gemini_api_key=settings.gemini_key,
        article_provider=settings.article_provider,
        type_constraints=article_type_db.build_constraints(type_record),
    )
    if settings.auto_review and settings.claude_key and settings.gemini_key:
        try:
            reviewed = article_review.run_structure_review_loop(
                structure.get("structure_text", ""),
                review_constraints(inputs, clinics, type_record),
                writer_provider=settings.article_provider,
                claude_api_key=settings.claude_key, gemini_api_key=settings.gemini_key,
                article_type=inputs.get("article_type", ""),
                main_kw=inputs.get("main_kw", ""), sub_kw=inputs.get("sub_kw", []),
                max_rounds=1, progress=log,
            )
            structure = dict(structure)
            structure["structure_text"] = reviewed["structure_text"]
        except Exception as e:
            log(f"　→ 構成の検品に失敗しました（{type(e).__name__}）。構成はそのまま使います")

    site_info = {}
    if settings.site_name and settings.site_info_sheet_url and settings.gcp_creds:
        try:
            site_info = read_site_info(
                settings.site_info_sheet_url, settings.gcp_creds, settings.site_name
            )
        except Exception:
            site_info = {}

    reference_block = article_type_db.build_reference_block(
        type_record, columns=["比較表の列構成", "選び方H2の作り"], log=log,
    )
    parts = "\n\n".join(filter(None, [settings.site_parts, reference_block]))
    output = generate_body(
        inputs, structure, clinics, settings.claude_key, comp,
        site_parts=parts, gemini_api_key=settings.gemini_key,
        article_provider=settings.article_provider,
        notation_rules=site_info.get("notation_rules"),
        site_notes=site_info.get("notes", ""),
    )
    if settings.auto_review and settings.claude_key and settings.gemini_key:
        try:
            reviewed = article_review.run_review_loop(
                output.get("html", ""), WRITING_RULES,
                writer_provider=settings.article_provider,
                claude_api_key=settings.claude_key, gemini_api_key=settings.gemini_key,
                article_type=inputs.get("article_type", ""),
                main_kw=inputs.get("main_kw", ""),
                source_text="\n\n".join(clinics.values()) if clinics else "",
                # 2周まわすと、直した箇所が別の規則に触れて指摘が増えることがある。
                # 仙台2本目は35件が20件に減ったあと45件に増えた。1周で切って残りは人へ。
                max_rounds=1, progress=log,
            )
            output = dict(output)
            output["html"] = reviewed["html"]
            if reviewed.get("human"):
                human = output_check.format_findings(reviewed["human"])
                output["todo_list"] = (
                    output.get("todo_list", "") + "\n\n【人が確認する指摘】\n" + human
                ).strip()
        except Exception as e:
            log(f"　→ 本文の検品に失敗しました（{type(e).__name__}）。本文はそのまま使います")

    result = {
        "title": structure["title"],
        "meta": structure["meta"],
        "html": output["html"],
        "todo_list": output.get("todo_list", ""),
        "clinics": inputs.get("clinics", []),
    }
    if on_body:
        try:
            on_body(dict(result))
        except Exception as e:
            log(f"　→ 途中保存に失敗しました（{type(e).__name__}）。生成は続けます")

    records = clinic_db_manager.build_db_records(
        list(clinics.keys()), genre=inputs.get("genre", ""),
        creds_data=settings.gcp_creds, sheet_url=settings.db_sheet_url,
    ) if clinics else {}
    result["html"] = fill_clinic_blocks(
        output["html"], clinics, records, inputs, type_record, settings, log=log,
    )

    # 見本に無いクラス名をモデルが作ると装飾が効かないまま公開される。
    # 直すのは人なので、指摘として要確認欄に出す。
    reference_all = "\n".join(
        article_type_db.get_reference_html(type_record or {}, col)
        for col in article_type_db.REFERENCE_COLUMNS
    )
    # 記事全体に機械の直しをかける。検品を1周に減らしたぶん、置き換え先が決まって
    # いる違反はここで確実に消す。モデルに任せると直したそばから別の違反が出る。
    result["html"], replaced = output_check.apply_mechanical_fixes(result["html"])
    if replaced:
        log(f"　→ 記事全体を機械で置き換え: {len(replaced)}種類")

    # 1案件に張るリンクの本数を上限までに減らす。埼玉で1案件14本張られた。
    result["html"], cut_links = output_check.limit_affiliate_links(result["html"])
    if cut_links:
        log(f"　→ 多すぎる送客リンク {cut_links}本を外しました")

    # 表のセルに残った要確認の印を消す。指示だけでは守られないので機械で消す。
    result["html"], cell_marks = output_check.replace_todo_in_cells(result["html"])
    if cell_marks:
        log(f"　→ 表のセルの要確認 {cell_marks}件を「−」にしました")

    # 本文に残った要確認の印を外して要確認欄へ移す。印が本文に出たまま公開されると事故になる。
    result["html"], pulled = output_check.pull_todo_marks(result["html"])
    if pulled:
        log(f"　→ 本文の要確認の印 {len(pulled)}件を要確認欄へ移しました")
        result["todo_list"] = (
            result.get("todo_list", "")
            + "\n\n【本文から外した要確認】\n"
            + "\n".join(f"- {x}" for x in pulled)
        ).strip()

    # 段落の分割は機械でやる。モデルに任せると直したそばから別の違反が出た。
    result["html"], split_count = output_check.split_long_paragraphs(result["html"])
    if split_count:
        log(f"　→ 4文以上の段落を {split_count}件 機械で分けました")

    # 置換表に無い禁止ワードが残ることがある。黙って通さず要確認へ回す。
    remaining = output_check.run_checks(result["html"], "")
    if remaining.get("fix"):
        words = "、".join(sorted({f["text"][:20] for f in remaining["fix"]}))
        log(f"　→ 機械チェックの残り {len(remaining['fix'])}件: {words}")
        result["todo_list"] = (
            result.get("todo_list", "")
            + "\n\n【機械チェックで残った箇所】\n" + words
        ).strip()

    # 仕様として置かないと決めたものの指摘を落とす
    result["todo_list"], dropped = output_check.strip_useless_todo(result.get("todo_list", ""))
    if dropped:
        log(f"　→ 要確認から対応不要な指摘を {dropped}件 落としました")

    invented = output_check.find_invented_classes(result["html"], reference_all)
    if invented:
        names = "、".join(f["text"] for f in invented)
        log(f"　→ 見本に無いクラス名が {len(invented)}種類: {names}")
        result["todo_list"] = (
            result.get("todo_list", "")
            + "\n\n【見本に無いクラス名】\n" + names
        ).strip()
    return result
