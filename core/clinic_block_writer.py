import re
import anthropic

NL = chr(10)


def _call_model(prompt: str, claude_api_key: str, gemini_api_key: str = "",
                provider: str = "claude", max_tokens: int = 8192) -> str:
    """紹介ブロックの生成に使うモデル。構成・本文と同じ選択に従う。"""
    from core import config
    if provider == "openai" and config.openai_ready():
        return config.call_openai(prompt, max_tokens=max_tokens).strip()
    if provider == "gemini" and gemini_api_key:
        from google import genai as _genai
        from core.config import GEMINI_TEXT_MODEL
        client = _genai.Client(api_key=gemini_api_key)
        response = client.models.generate_content(model=GEMINI_TEXT_MODEL, contents=prompt)
        return (response.text or "").strip()
    client = anthropic.Anthropic(api_key=claude_api_key)
    msg = client.messages.create(
        model=config.CLAUDE_WRITER_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


COMPONENT_LABELS = {
    "spec_image": "スペック画像",
    "intro_text": "クリニック紹介文",
    "appeal_points": "おすすめポイント（箇条書き）",
    "price_table": "料金テーブル",
    "reviews": "口コミ",
    "map_image": "マップ画像",
    "basic_info": "基本情報テーブル",
    "cta_button": "CTAボタン（上位3院のみ）",
}

ALL_COMPONENTS = list(COMPONENT_LABELS.keys())

BASIC_INFO_FIELD_LABELS = {
    "address": "住所",
    "access": "最寄り駅",
    "hours": "診療時間",
    "holidays": "休診日",
    "payment": "支払方法",
    "shipping": "配送情報",
    "discount": "割引情報",
    "cancel": "途中解約",
    "dosage": "取扱い用量",
    "plan": "取扱いプラン",
    "clinics_count": "院数",
    "reservation": "予約方法",
    "phone": "電話番号",
    "website": "公式サイト",
    "areas": "主な展開エリア",
    "consultation": "診察方法",
}

ALL_BASIC_INFO_FIELDS = list(BASIC_INFO_FIELD_LABELS.keys())

HEADING_TYPE_OPTIONS = {
    1: "①H3（クリニック名＋○○院のみ）",
    2: "②H3（クリニック名＋○○院＋コメント）",
    3: "③小見出し（クリニック名＋○○院のみ）",
    4: "④専用パーツ（コメント先行＋クリニック名＋○○院）",
}


def parse_clinic_list(text: str) -> list[dict]:
    """掲載院一覧テキストをパースして [{rank, name, url}] リストに変換する。"""
    clinics = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("==="):
            continue
        m = re.match(r'^(\d+)[.\)、]\s*(.+?)::(https?://\S+|\[要確認\]|unknown)$', line)
        if m:
            rank = int(m.group(1))
            name = m.group(2).strip()
            url = m.group(3).strip()
            clinics.append({"rank": rank, "name": name, "url": "" if url in ("[要確認]", "unknown") else url})
        else:
            m2 = re.match(r'^(\d+)[.\)、]\s*(.+)$', line)
            if m2:
                clinics.append({"rank": int(m2.group(1)), "name": m2.group(2).strip(), "url": ""})
    return clinics


def generate_clinic_block(
    name: str,
    rank: int,
    scraped_info: str,
    price_data: str,
    extra_notes: str,
    link_url: str,
    lp_plan: str,
    template: dict | None = None,
    main_kw: str = "",
    sub_kw: list | None = None,
    criteria_text: str = "",
    claude_api_key: str = "",
    site_parts: str = "",
    reference_html: str = "",
    extra_instruction: str = "",
    article_type: str = "",
    gemini_api_key: str = "",
    article_provider: str = "claude",
) -> str:
    # テンプレート設定は任意。型タブの見本HTMLがあれば、そちらが構成の正になる。
    template = template or {}
    sub_kw = sub_kw or []
    is_top3 = rank <= 3
    heading_type = template.get("heading_type")
    component_order = template.get("component_order") or []
    basic_info_fields = template.get("basic_info_fields", [])
    basic_info_html_sample = template.get("basic_info_html_sample", "")
    show_rank_in_heading = template.get("show_rank_in_heading", True)
    price_table_templates = template.get("price_table_templates", [])
    top3_link_placements = template.get("top3_link_placements", [])

    active_components = [c for c in component_order if c != "cta_button" or is_top3]

    heading_map = {
        1: f'<h3 id="clinic-xxx">{name}</h3>',
        2: f'<h3 id="clinic-xxx">{name}は[記事内容に合わせた1文コメント]</h3>',
        3: f'小見出しパーツを使用: {name}',
        4: f'専用パーツ（コメント先行型）: [コメント]{name}',
    }
    heading_instruction = heading_map.get(heading_type, "") if heading_type else ""
    if heading_instruction and not show_rank_in_heading:
        heading_instruction += "\n※見出し・小見出しに「1位」「2位」などの順位番号を含めない"

    price_section = ""
    if "price_table" in active_components:
        if price_table_templates:
            pt_text = "\n\n".join(
                f"テンプレート「{pt['name']}」:\n{pt['html']}"
                for pt in price_table_templates
            )
            price_section = f"""
【料金テーブル（厳守）】
以下のHTMLテンプレートの{{{{変数}}}}を入力された料金データで埋めること。取得できない数値は[要確認]。
- テーブルの直前に必ず小見出しパーツ（または小見出し相当のHTML）を置く
- テーブル内に院名・クリニック名を含む行・セルを設けない
- テンプレートの列数・構造を変えない
{pt_text}

入力された料金データ:
{price_data or '（未入力）'}
"""
        elif price_data:
            price_section = f"""
【料金テーブル（厳守）】
以下の料金データをもとに料金テーブルHTMLを作成してください。
- テーブルの直前に必ず小見出しパーツ（または小見出し相当のHTML）を置く
- テーブル内に院名・クリニック名を含む行・セルを設けない
- 全院で列数・列名を統一する（項目を追加・削除しない）
{price_data}
"""

    basic_info_section = ""
    if "basic_info" in active_components and basic_info_fields:
        field_names = [BASIC_INFO_FIELD_LABELS.get(f, f) for f in basic_info_fields]
        basic_info_section = f"""
【基本情報テーブル（厳守）】
- 出力する項目（順番も固定）: {', '.join(field_names)}
- 取得できない項目は[要確認]と記載し、項目自体は省略しない（全{len(field_names)}行を必ず出力）
- テーブルは2列（項目名 | 内容）固定。列を増減しない
- テーブルの直前に必ず小見出しパーツ（または小見出し相当のHTML）を置く
- テーブル内に院名・クリニック名を含める行・セルを設けない
- 書き方・形式は他院と完全統一（診療時間の区切り文字・改行方法・単位の表記など）
"""

    article_type_section = ""
    if article_type == "地域":
        article_type_section = """
【記事タイプ：地域記事】
紹介文を書く際の方針（強制ルールではなく、質を上げるための指針）：
- 紹介文の約半分は「このジャンル×この地域」で成立するトピックを軸にする。その地域の読者が気にしそうな文脈・需要を起点にして自然に地域性が出るように書く（アクセス・通いやすさは刺さるジャンルならそれを使うが縛りではない）
- 全体の約3分の1はその院固有の情報（強み・実績・特徴）。ジャンル×地域の文脈との重複はOK
- 院ごとに切り口を変え、同じ記事内で紹介文の型が揃わないようにする
"""
    elif article_type == "比較":
        article_type_section = """
【記事タイプ：比較記事】
紹介文を書く際の方針（強制ルールではなく、質を上げるための指針）：
- 選び方コンテンツの比較軸を念頭に置きながら、各院の強みを自然な流れで紹介する
- 比較軸への答え合わせのような単調な構成にしない。読者が「この院を選ぶ理由」を感じ取れる文脈で書く
- 院ごとに切り口・角度を変え、同じ記事内で紹介文がテンプレ化しないようにする
"""

    basic_info_sample_section = ""
    if basic_info_html_sample:
        basic_info_sample_section = f"""
【見本ブロックHTML（構成・形式を完全に踏襲）】
以下は同じ記事で使用する見本の1院分HTMLです。下記の要素をすべて踏襲してください。
- 基本情報テーブル：行名（項目名）・列数・HTMLタグをそのまま使用する
- 料金テーブル：タブ切り替えの有無・施術説明の有無・行の粒度をそのまま使用する
- 口コミ・コメント総括：テキストブロックの有無・構成をそのまま使用する
- その他コンポーネント（バッジ・アイコン・ボックス等）：配置・形式をそのまま使用する
各セクションの内容はこのクリニック固有の情報で埋め直すこと。取得できない項目は[要確認]と記載。
{basic_info_html_sample}
"""

    appeal_points_section = ""
    if "appeal_points" in active_components:
        appeal_points_section = """
【おすすめポイント（箇条書き）（厳守）】
- クリニックの強み・差別化ポイントを箇条書きで3〜5項目出力する
- 各項目は簡潔に1〜2行。読者が「選ぶ理由」として納得できる具体的な内容にする
- サイトパーツの箇条書きHTMLがあればそれを使用する。なければ<ul><li>形式で出力する
- 直前に小見出しパーツ（または相当のHTML）を置く
"""

    if is_top3:
        top3_section = f"【上位3院ルール（{rank}位）】\n- クリニック紹介文は4段落\n"
        if rank == 1:
            top3_section += "- 1位のため選び方コンテンツの全項目にマッチしていることを自然に示す\n"
        else:
            top3_section += "- 選び方コンテンツの約3分の2の項目にマッチしている内容にする（自然な文章が大前提）\n"
        if "heading" in top3_link_placements and link_url:
            top3_section += f'- 見出しのクリニック名にリンク: href="{link_url}"\n'
        if "spec_image" in top3_link_placements and link_url:
            top3_section += f'- スペック画像にリンク: href="{link_url}"\n'
        if "cta_button" in top3_link_placements:
            if link_url:
                top3_section += f'- CTAボタン設置: href="{link_url}"（サイトパーツのCTAボタンHTMLを使用すること。汎用<a>タグで代替しない）\n'
            if lp_plan:
                top3_section += f'- LP掲載プランを記載: {lp_plan}\n'
        if not top3_link_placements and link_url:
            # テンプレート設定なしで見本だけを頼りにする場合。見本のURLをそのまま
            # 使われるとよそのクリニック・よその記事のリンクが出るので明示する。
            top3_section += (
                f'- リンク先は {link_url} を使う。パラメータの付け方は追加指示に従う\n'
                "- 見本HTMLに書かれているURLをそのまま使わない\n"
            )
            if lp_plan:
                top3_section += f'- LP掲載プランを記載: {lp_plan}\n'
    else:
        top3_section = f"【4位以下ルール（{rank}位）】\n- クリニック紹介文は2〜3段落\n- リンク・CTAボタンなし\n"

    components_str = " → ".join(COMPONENT_LABELS.get(c, c) for c in active_components)

    # 見本は切らずに全文渡す。テンプレート設定を置かない運用では、この見本だけが
    # ブロックの作りを決める。切ると後半のテーブル・CTAが落ちて再現できない。
    reference_section = (
        "【見本HTML（この作りをそのまま踏襲する）】\n"
        "同じ記事で使う1院分の見本です。テーブルの列数と列名・小見出しの位置・"
        "コンポーネントの並び・タグ構造・クラス名をそのまま真似してください。\n"
        "中身はこのクリニックの情報で埋め直します。取れない項目は[要確認]と書きます。\n"
        f"{reference_html}\n"
    ) if reference_html else ""

    prompt = f"""あなたはSEO記事のおすすめクリニック紹介ブロック専門ライターです。
以下の条件に従って、1院分のHTMLブロックを生成してください。

【記事メインKW】{main_kw}
【サブKW】{', '.join(sub_kw) if sub_kw else '（なし）'}

【このクリニック】
クリニック名（本文中でも必ず○○院まで記載）: {name}
掲載順位: {rank}位
{top3_section}
【選び方コンテンツ（記事内の評価軸）】
{criteria_text or '（未入力）'}

【公式サイトから取得した情報】
{scraped_info or '（取得できませんでした）'}

【追加メモ・補足情報】
{extra_notes or '（なし）'}

{f"【コンポーネント構成と出力順序】{chr(10)}{components_str}{chr(10)}" if components_str else ""}
{f"【見出しの形式】{chr(10)}{heading_instruction}{chr(10)}" if heading_instruction else ""}

{article_type_section}
{price_section}
{basic_info_section}
{basic_info_sample_section}
{appeal_points_section}
{f"【サイト別HTMLパーツ】{chr(10)}{site_parts}" if site_parts else ""}
{reference_section}
{f"【追加指示】{chr(10)}{extra_instruction}{chr(10)}" if extra_instruction.strip() else ""}
【要確認ルール（厳守）】
- 公式サイトから取得できなかった情報・確認が取れない情報・推測になる情報は `[要確認：○○]` と記載する
- 補完・捏造はしない。不確かな情報をそれらしく書かない
- [要確認]は項目を省略するためではなく「ここを人間が確認してください」というマーカー

【共通ルール】
- メインKW・サブKWで検索するユーザーに刺さる切り口で紹介文を書く
- 選び方コンテンツの項目に自然に触れた内容にする（評価軸を露骨に列挙しない）
- クリニック名は本文中でも○○院まで必ず記載
- テーブル（料金・基本情報）の直前には必ず小見出しを置く（小見出しなしでいきなりテーブルを始めない）
- テーブル内に院名・クリニック名を含む行・セルを設けない
- **サイトパーツが提供されている場合（上記「サイト別HTMLパーツ」参照）、CTAボタン・見出し・小見出し等は必ずそのパーツのHTMLをそのまま使用する。汎用タグ（<a href="...">公式サイトはこちら</a>等）で代替しない**
- 文中に「」（隅付き括弧）を使わない。強調は<strong>タグ
- 「以下のとおり」「次のとおり」等の記事内参照表現を使わない
- 紹介文（クリニック紹介テキスト部分）は3〜4段落。1段落1主張・1トピックを厳守する
- ブロック末尾に免責注記・掲載情報の更新案内・「公式サイトでご確認ください」などの注釈を追加しない（指示がない限り一切不要）

HTML本文のみを出力してください。説明文・コードフェンスは不要。
"""

    return _call_model(prompt, claude_api_key, gemini_api_key, article_provider)


def edit_clinic_block(html: str, instruction: str, claude_api_key: str,
                      gemini_api_key: str = "", article_provider: str = "claude") -> str:
    """生成済みHTMLブロックに対して指示を適用して修正する。"""
    prompt = f"""以下のクリニック紹介ブロックHTMLに対して、指示に従って修正してください。

【修正指示】
{instruction}

【修正前HTML】
{html}

HTML本文のみを出力してください。説明文・コードフェンスは不要。"""
    return _call_model(prompt, claude_api_key, gemini_api_key, article_provider)


def generate_lower_blocks(entries: list, main_kw: str = '', sub_kw: list | None = None,
                          criteria_text: str = '', claude_api_key: str = '',
                          site_parts: str = '', reference_html: str = '',
                          article_type: str = '', gemini_api_key: str = '',
                          article_provider: str = 'claude') -> str:
    """4位以降の紹介ブロックをまとめて1回で作る。

    4位以降は2〜3段落でリンクもCTAも料金テーブルも出さない。1院ずつモデルを
    呼ぶと記事1本の待ち時間がその分だけ伸びるので、まとめて書かせる。
    """
    sub_kw = sub_kw or []
    if not entries:
        return ''
    blocks = []
    for entry in entries:
        name = entry.get('name', '')
        info = entry.get('info', '') or '（情報なし）'
        price = entry.get('price_data', '') or '（記載なし）'
        blocks.append(
            f"【{entry['rank']}位 {name}】" + NL
            + '使ってよい事実:' + NL + info + NL
            + '料金:' + NL + price
        )
    ranks = '、'.join(f"{e['rank']}位 {e.get('name', '')}" for e in entries)
    reference_section = ''
    if reference_html:
        reference_section = (
            '【見本HTML（見出しと段落の作りを合わせる）】' + NL
            + '上位院の出力です。見出しのタグとクラス名を合わせてください。' + NL
            + '料金テーブル・基本情報テーブル・CTAボタン・画像は真似しないでください。' + NL
            + reference_html[:6000] + NL
        )
    parts_section = ''
    if site_parts:
        parts_section = '【サイト別HTMLパーツ】' + NL + site_parts

    prompt = (
        'あなたはSEO記事のおすすめクリニック紹介ブロック専門ライターです。' + NL
        + '4位以降の院を、下の順番どおりに続けて出力してください。' + NL + NL
        + f'【記事メインKW】{main_kw}' + NL
        + '【サブKW】' + ('、'.join(sub_kw) if sub_kw else '（なし）') + NL
        + '【記事タイプ】' + (article_type or '（指定なし）') + NL + NL
        + '【出力する院と順番】' + NL + ranks + NL + NL
        + '【選び方コンテンツ（記事内の評価軸）】' + NL
        + (criteria_text or '（未入力）') + NL + NL
        + '【各院の情報】' + NL + (NL + NL).join(blocks) + NL + NL
        + reference_section + NL
        + parts_section + NL + NL
        + '【4位以降のルール（厳守）】' + NL
        + '- 1院につき見出し1つと紹介文2〜3段落だけ。1段落1主張' + NL
        + '- リンク・CTAボタン・画像・料金テーブル・基本情報テーブルを出さない' + NL
        + '- 料金に触れる場合は本文の文章の中で書く。表にしない' + NL
        + '- 院ごとに切り口を変える。同じ言い回しを繰り返さない' + NL
        + '- クリニック名は本文中でも省略せずに書く' + NL + NL
        + '【使ってよい事実の範囲（最優先）】' + NL
        + '- 各院の情報に書かれていることだけを事実として書く。書かれていないことは書かない' + NL
        + '- 効果が出るまでの回数と期間、痛みの程度、機器の仕組み、地域の事情も、' + NL
        + '  上に無ければ書かない。必要なら [要確認：〜] の形で残す' + NL
        + '- 数字を自分で決めて書かない' + NL + NL
        + '【共通ルール】' + NL
        + '- 文中に隅付き括弧を使わない。強調は<strong>タグ' + NL
        + '- 以下のとおり・次のとおり等の記事内参照表現を使わない' + NL
        + '- ブロック末尾に免責注記・掲載情報の更新案内を追加しない' + NL + NL
        + 'HTML本文のみを出力してください。説明文・コードフェンスは不要。' + NL
    )
    return _call_model(prompt, claude_api_key, gemini_api_key, article_provider, max_tokens=8192)
