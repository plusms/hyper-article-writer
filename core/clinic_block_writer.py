import re
import anthropic

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
    template: dict,
    main_kw: str,
    sub_kw: list,
    criteria_text: str,
    claude_api_key: str,
    site_parts: str = "",
    reference_html: str = "",
    extra_instruction: str = "",
    article_type: str = "",
) -> str:
    is_top3 = rank <= 3
    heading_type = template.get("heading_type", 1)
    component_order = template.get("component_order", ["intro_text", "basic_info"])
    basic_info_fields = template.get("basic_info_fields", [])
    basic_info_html_sample = template.get("basic_info_html_sample", "")
    price_table_templates = template.get("price_table_templates", [])
    top3_link_placements = template.get("top3_link_placements", [])

    active_components = [c for c in component_order if c != "cta_button" or is_top3]

    heading_map = {
        1: f'<h3 id="clinic-xxx">{name}</h3>',
        2: f'<h3 id="clinic-xxx">{name}は[記事内容に合わせた1文コメント]</h3>',
        3: f'小見出しパーツを使用: {name}',
        4: f'専用パーツ（コメント先行型）: [コメント]{name}',
    }
    heading_instruction = heading_map.get(heading_type, heading_map[1])

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
【基本情報テーブルのHTMLサンプル（行名・形式を必ず踏襲）】
以下は基本情報テーブルの見本HTMLです。行名（項目名）・テーブル構造・HTMLタグをそのまま使用してください。
各行の内容はこのクリニック固有の情報で埋め直すこと。取得できない項目は[要確認]と記載。
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
    else:
        top3_section = f"【4位以下ルール（{rank}位）】\n- クリニック紹介文は2〜3段落\n- リンク・CTAボタンなし\n"

    components_str = " → ".join(COMPONENT_LABELS.get(c, c) for c in active_components)

    reference_section = (
        "【フォーマット参照（1院目のHTML）】\n"
        "以下は同じ記事内の別の院で生成済みのHTMLです。\n"
        "テーブルの列数・列名・小見出しの位置・コンポーネントの順序・書き方を完全に統一してください。\n"
        f"{reference_html[:3000]}\n"
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

【コンポーネント構成と出力順序】
{components_str}

【見出しの形式】
{heading_instruction}

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
- 紹介文（クリニック紹介テキスト部分）は1〜3段落を目安にコンパクトにまとめる。冗長にしない
- ブロック末尾に免責注記・掲載情報の更新案内・「公式サイトでご確認ください」などの注釈を追加しない（指示がない限り一切不要）

HTML本文のみを出力してください。説明文・コードフェンスは不要。
"""

    client = anthropic.Anthropic(api_key=claude_api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def edit_clinic_block(html: str, instruction: str, claude_api_key: str) -> str:
    """生成済みHTMLブロックに対して指示を適用して修正する。"""
    prompt = f"""以下のクリニック紹介ブロックHTMLに対して、指示に従って修正してください。

【修正指示】
{instruction}

【修正前HTML】
{html}

HTML本文のみを出力してください。説明文・コードフェンスは不要。"""
    client = anthropic.Anthropic(api_key=claude_api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()
