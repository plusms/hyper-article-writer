import anthropic

WRITING_RULES = """
【ライティングルール（すべて厳守）】

■ 文体
- 必ず「です・ます」調で統一する
- 同じ語尾（〜ます・〜です）が3回以上連続しないよう体言止めを交える
- 推量（〜でしょう・〜かもしれません）は使わない。言い切りのみ

■ 文章構成
- PREP法：結論→理由・根拠→具体例→SoWhat（読者の次の判断・選択・行動）
- 各H3の末尾は必ずSoWhat。「〜重要です」「〜大切です」「〜しましょう」「〜確認してください」で終わらない→「〜することで△△できます」「〜になります」まで行動後の状態変化を書く
- SoWhatの文末バリエーション（同じ表現を2H3以上連続させない）：選択→「〜を選べます／絞り込めます」、判断→「〜かどうかを判断できます」、防止→「〜を防げます／リスクを抑えられます」、継続→「〜が続けやすくなります」、行動→「〜を試しやすくなります」、状態変化→「〜に変わります／〜できる環境が整います」
- H3の段落数は3〜5段落（<p>1つ・<table>1つ・<ul>/<ol>1つをそれぞれ1段落としてカウント）
- H3内にテキスト<p>を最低1つ入れる。テーブル・箇条書きのみはNG
- 冒頭文は4ステップ：①読者の悩みを具体的に言語化（顕在悩み＋潜在リスク）→②誰向けか絞り込む→③記事で分かることを端的に示す→④読後の未来（「〜できます」「〜に変わります」まで書く。「〜を知りたい方へ」で終わらない）
- H2直下に導入文を入れる。3〜4文・80〜120文字を目安。最後は行動後の状態（「〜できるようになります」等）で締める。「〜を解説します」だけで終わるのはNG。H3・テーブル・リストで直接始めない
- 1つのH2内で、少なくとも1つのH3にテーブルまたは箇条書きを使用する

■ テーブルルール
- テーブルには必ず小見出し（題名）を直前の<p>または<div>で入れる
- テーブル一番左の列は<td>ではなく<th>を使う
- スクロールテーブル（4列以上）：<div class="scrolltable"><table>...</table></div>

■ 箇条書きルール
- 箇条書き（<ul>）には必ず小見出し（題名）を直前の<p>または<strong>で入れる
- 番号なし箇条書き（<ul><li>）はOK。番号付き「①②③」「(1)(2)(3)」は禁止

■ 禁止：記事内参照表現（一切使わない）
- 「以下の」「以上の」「下記の」「次の」「上記の」「前述の」「先ほど」
- 「次のセクション」「ページ上部」「記事冒頭」「後述」
- 「本記事」「この記事」「このブロック」
- 「〜で紹介します」「〜をご覧ください」「〜で解説します」「〜を確認してください」

■ 禁止：問いかけ表現
- 「〜に悩んでいませんか」「〜でお悩みではないですか」「〜ではないでしょうか」「〜に困っていませんか」

■ 禁止：テンプレ導入文
- 「この記事では」「今回は」「こちらでは」「ここでは」「本記事では」「ここからは」

■ 禁止：PREP接続語
- H3内のPREP展開で「つまり」「要するに」「例えば」を接続語として使わない
- 文章の流れ自体でPREP構造を表現する

■ H4タグ禁止
- <h4>タグは使用禁止。H3の下に詳細を入れたい場合は<p>で表現する

■ 絶対NGワード
- 抽象語：傾向・設計・前提・把握・整理・方向性・実態
- 結論をぼかす：現実的な選択肢になります・後悔しにくい・失敗しにくい・納得感のある
- テンプレ語：もちろん・大切です・なお・順番に・動線・生活動線・糸抜き（→抜糸）
- 不自然な強調：救世主・味方・第一歩・鍵・近道・スムーズ・最適・最もふさわしい
- 誰目線か不明：「実態」「現状」「現場では」

■ こそあど・指示語を使わない
- 「これ」「それ」「この」「その」「ここ」「そこ」→具体的な名詞に置き換える

■ 見出しルール
- H2・H3に「｜」「、」「【】」を使わない
- 使える記号：「！」「？」「（）」のみ
- まとめのH2見出しに「まとめ」という名称を使わない
- H2・H3はメインKWをなるべく含める。難しい場合は最低でもそのKW・ジャンル・地域でしか通じない固有の表現にする
  NG：「費用について」「選び方のポイント」「注意点をご確認ください」
  OK：「ピコレーザー大阪の1回あたり費用と照射範囲の目安」
- H3見出しは「他の地域・他のジャンルに置き換えても成立する汎用表現」を禁止する

■ 文章品質
- 主語を省略しない。「〜があります」「〜できます」は主語が不明なのでNG
- 「〜な場合があります」「〜も大切です」等の主張のない文を入れない
- 人間が使わない造語・不自然な表現を避ける（「総合的に最善な選択肢」「包括的なアプローチ」等）

■ 医療・美容系ルール
- 「絶対」「100%」「最強」「日本一」「最安値」は断定で使わない
- 自由診療の料金（税込）・回数・リスク・副作用を一箇所にまとめて記載する

■ クリニック紹介文
- ファクトチェック済みのデータのみ使用。補完・推測は一切しない
- [要確認]の項目はそのままの形でHTMLに出力し、本文に組み込まない
- 段落構成：1段落目（立地・アクセス）、2段落目（治療方法・施術の特徴）、3段落目（治療方針・通いやすさ）
- おすすめクリニック紹介のH2直下にはH3を置かない

■ 料金相場セクション
- 説明文中に具体的なクリニック名を出さない
- 相場感・価格帯・料金を左右する要素の説明に集中する

■ まとめセクション
- <h2>タグで配置。見出しに「まとめ」という名称を使わない
- まとめの中に箇条書きを入れない。自然文で書く
"""

_QUALITY_BASE = """
【品質チェック基準（共通・全記事必須）】

■ NGワード・NGパターン（即修正）
Q1. こそあど言葉が使われていないか（これ・それ・この・その・ここ・そこ）
Q2. 記事内参照表現が使われていないか（以下の・以上の・下記の・次の・上記の・本記事・この記事・このブロック・先ほど・上記・前述の・次のセクション・ページ上部・〜で紹介します・〜をご覧ください）
Q3. 問いかけ表現が使われていないか（〜に悩んでいませんか・〜でお悩みではないですか・〜ではないでしょうか）
Q4. テンプレ導入文が使われていないか（この記事では・今回は・こちらでは・ここでは・本記事では・ここからは）
Q5. その他NGワードが使われていないか（傾向・設計・前提・把握・整理・方向性・もちろん・大切です・なお・順番に・動線・糸抜き・現実的な選択肢・後悔しにくい・失敗しにくい・スムーズ・活用・言えます・言えるでしょう・「判断につながります」「判断しやすくなります」等の間接的判断表現）
Q6. 番号付き列挙（①②③等）が本文・見出しで使われていないか
Q7. H4タグが使われていないか（使用禁止）
Q8. 「つまり」「例えば」等の接続語をH3内のPREP接続に使っていないか（PREPは文章の流れで表現する）

■ 見出しルール
Q9. 見出し（H2・H3）に「？」「｜」「、」「【】」が入っていないか
Q10. メインKWが3〜5箇所の見出しに自然に入っているか
Q11. サブKWが最低1本のH2に入っているか
Q12. H3の見出しがそのKW・ジャンルでしか通じない固有の見出しか（「費用について」「選び方のポイント」「注意点をご確認ください」等の汎用表現はNG）

■ 構成・ブロックルール
Q13. H2直下（最初のH3の前）に導入文（3〜4文・80〜120文字）があるか。いきなりH3・テーブル・リストで始まっていないか。最後が「〜を解説します」だけで終わっていないか
Q14. 各H2に1つ以上テーブルまたは箇条書きが含まれているか
Q15. テーブル・箇条書きの直前に指示文（「次の表を確認してください」「以下で確認してください」等）が使われていないか
Q16. まとめがH2単位で設置されており「まとめ」という名称を見出しに使っていないか
Q17. 1つのH2が1つの疑問解決に集中しているか（途中で別の疑問・悩みを提示していないか）

■ H3本文ルール
Q18. 各H3が3段落以上5段落以内か（<p>タグ・<table>・<ul>/<ol>のそれぞれを1段落としてカウント）
Q19. H3内がテーブル・箇条書きのみになっていないか（テキスト段落が最低1つ必要）
Q20. 各H3の末尾にSoWhat（行動後の状態変化まで含む締め文）が入っているか。NG：「〜重要です」「〜大切です」（説明止まり）「〜しましょう」「〜確認してください」（行動喚起止まり）→OK：「〜することで△△できます」「〜が続けやすくなります」等
Q21. 各H3本文に「そのKWとジャンルでしか通じない文」が最低1文あるか（他ジャンル・他KWでも言える一般論のみで終わっていないか）

■ 冒頭文
Q22. 冒頭文がAFDE構成になっているか（A:読者の悩みを具体的に提示 / F:誰向けか絞り込む / D:記事で分かることを端的に示す / E:信頼性の根拠）
Q23. 冒頭文が潜在意図を言語化しているか（顕在意図の繰り返しではないか。「〜を知りたい方へ」で終わっていないか）

■ 文章品質
Q24. 主語が省略されていないか（「読者」「病院」等の主語もNG）
Q25. 削除しても意味の通じる文・主張のない短文が含まれていないか（「〜があります」「〜も大切です」「〜な場合があります」「詳しく解説します」等）
Q26. 人間が使わないような不自然な言葉・造語が使われていないか

■ HTML構造
Q27. まとめがdivボックスではなくh2タグで配置されているか
Q28. [要確認]箇所が本文に組み込まれず、そのまま記載されているか
"""

_QUALITY_LOCAL = """
【地域記事 追加チェック基準】

Q29. 各H3本文に「地域名を別地域に置換しても成立しない文」が1文以上あるか（地域固有情報の有無）
Q30. 主要サブエリア（駅名・エリア名）の粒度で書かれているか（「〇〇全体は〜」で止まっていないか）
Q31. 地域ユーザーの動機・不安・移動前提（交通手段・アクセス等）が反映されているか
Q32. 地域断言（「〇〇地域は〜がある」等）に根拠が示されているか（「傾向があります」で根拠なしはNG）
"""

_QUALITY_COMPARISON = """
【比較記事 追加チェック基準】

Q29. 比較軸が冒頭で明示されているか（「〇〇・〇〇・〇〇の3軸で比較します」等）
Q30. 比較情報がテーブルで整理されているか（文中に並べていないか）
Q31. 「向いている人・向いていない人」の条件が示されているか
Q32. 最終結論（「迷ったらこれ」相当の選択指針）が出ているか。結論なしで終わっていないか
Q33. 比較対象の選定根拠が示されているか（なぜその対象を選んだか）
"""

_QUALITY_BRAND = """
【商標記事 追加チェック基準】

Q29. ブランド名がタイトル・H1・冒頭文の前半に含まれているか
Q30. 費用総額（初診料・薬代・送料・追加費用の有無）が記載されているか
Q31. 診察の流れ（オンライン/対面の別）が記載されているか
Q32. 診療時間が記載されているか
Q33. 「怪しい」「副作用」「失敗」「効果なし」等ネガティブ系クエリへの対応が含まれているか
Q34. 冒頭に簡易料金表またはCTA（申込・予約・問い合わせ導線）が置かれているか
"""

_QUALITY_HOWTO = """
【ノウハウ記事 追加チェック基準】

Q29. クリニック紹介ブロック・具体的な料金表が設けられていないか（情報提供記事として）
Q30. 各H3が「どう判断・行動すればよいか」まで踏み込んでいるか（仕組み説明のみで終わっていないか）
Q31. CV記事誘導セクション（[要確認：関連CV記事URL]）がまとめの直前に置かれているか
Q32. 医療・専門用語をユーザーが行動判断できる粒度に噛み砕いているか
"""

_QUALITY_TYPE = {
    "地域": _QUALITY_LOCAL,
    "比較": _QUALITY_COMPARISON,
    "商標": _QUALITY_BRAND,
    "ノウハウ": _QUALITY_HOWTO,
}

_TYPE_INSTRUCTIONS = {
    "ノウハウ": """
【ノウハウ記事 執筆要件（厳守）】

■ 基本方針
- クリニック紹介ブロック・具体的な料金表は設けない
- 情報提供・解説に徹する。「〇〇できます」「〇〇で解決します」等の断定型で書く
- 「なぜそうなるか」の仕組みより「どう判断・行動すればよいか」を優先する
- 医学的・専門的な説明はユーザーが行動判断できる粒度まで噛み砕く
- 各H3でそのジャンル・KW特有の具体例・数値・条件を最低1つ入れる

■ CV記事誘導セクション（まとめの直前に配置・固定）
- <p>[要確認：関連CV記事のURLをここに挿入してください]</p> の形式でプレースホルダーを出力
- 誘導文は「〇〇で実際にクリニックを選ぶなら」「〇〇の費用を比較するなら」等のアクション起点で書く
- リンクは架空のURLを書かない。プレースホルダーのみ

■ ノウハウ禁止事項（CV記事との区別）
- 特定クリニックの紹介・評価はしない
- 「〇〇クリニックがおすすめ」「〇〇の料金は△△円」等の具体情報はNG
""",
    "地域": """
【地域記事 執筆要件（厳守）】

■ 地域固有情報
- 各H3本文に「地域名を別地域に置き換えると成立しない文」を最低1段落（3文以上）入れる
- 入れられないH3は削除するか別の地域固有トピックに差し替える（無理やり地名を挿入しない）
- 「どの地域でも同じことが言える一般論」に無理やり地名を入れて地域記事に見せかけない
- 主要サブエリア（駅名・エリア名）の粒度で記述する（「大阪全体では〜」で止めない）
- 地域ユーザーの移動前提・通院事情（最寄り駅からのアクセス・通いやすさ等）を反映する
- 地域断言には根拠を示す。「傾向があります」だけで根拠なしはNG

■ ノウハウの扱い（カニバリ防止）
- 以下はノウハウとして省く：「〇〇とは」「なぜ〇〇になるか」の仕組み解説・施術メカニズム・医学的説明・症状の種類や原因の説明
- 以下は地域記事として書く：クリニック選び方（地域・費用・アクセス軸）・料金費用相場・施術の流れ（予約〜アフターケア）・エリア・アクセス情報

■ 費用相場セクション
- そのジャンルで意思決定に必要な費用項目をすべて出す（プラン料金だけでなく、診察料・薬代・麻酔代・追加費用の有無等）
- 項目別 or 総額の選択はユーザーの納得感を基準にする

■ CTAの配置
- クリニック紹介ブロックの上位1〜3位の各クリニック紹介直下にCTAを置く
- 冒頭比較表にもCTAを組み込む（比較表で意思決定熱量が上がるタイミングを逃さない）
- CTAは「1行目：ベネフィット訴求」「2行目：アクション動詞」の2行フォーマット
""",
    "比較": """
【比較記事 執筆要件（厳守）】
- 冒頭（H1直後の冒頭比較表H2）に掲載院3〜5院のCV比較表を必ず設置する（料金・特徴・対象・アクセス等の軸で比較）
- 比較軸を冒頭で明示する（何と何を何で比べるかを最初に示す）
- 比較情報はテーブルで整理する（文中に並べない）
- 「向いている人・向いていない人」の条件を示す
- 最終結論（「迷ったらこれ」相当の選択指針）を出す。結論なしで終わらない
- 比較対象の選定根拠を示す

■ 費用相場セクション
- そのジャンルで意思決定に必要な費用項目をすべて出す（プラン料金だけでなく、診察料・薬代・麻酔代・追加費用の有無等）
- 項目別 or 総額の選択はユーザーの納得感を基準にする

■ CTAの配置
- クリニック紹介ブロックの上位1〜3位の各クリニック紹介直下にCTAを置く
- 冒頭比較表にもCTAを組み込む（比較表で意思決定熱量が上がるタイミングを逃さない）
- CTAは「1行目：ベネフィット訴求」「2行目：アクション動詞」の2行フォーマット
""",
    "商標": """
【商標記事 執筆要件（厳守）】
- ブランド名をタイトル・H1・冒頭文の前半に含める
- 費用総額（初診料・薬代・送料・追加費用の有無）を記載する
- 診察の流れ（オンライン/対面の別）を記載する
- 診療時間を記載する
- ネガティブ系クエリ（怪しい・副作用・失敗・効果なし）への対応を含める
- 冒頭に簡易料金表またはCTA（申込・予約・問い合わせ導線）を置く

■ 口コミH2の構成（3H3が基本）
- H3①「好評口コミ」：ポジティブな口コミを箇条書きで提示 → 評価される理由・背景を本文で展開 → SoWhatは「〜と感じる方に向いています」等のベネフィット
- H3②「辛口口コミ」：ネガティブな口コミを箇条書きで提示 → 発生しやすい原因を示す → 対処法・解決策まで書いてSoWhatで締める（ネガティブで終わらない）
- H3③「口コミ総評」：好評・辛口を踏まえた総合評価 → 「どんな人に向いているか」で締める

■ CTAの配置
- ユーザーが「欲しい・試したい」と思うタイミング（料金確認直後・口コミ好評直後・まとめ直前等）に都度設置する
- CTAは「1行目：ベネフィット訴求」「2行目：アクション動詞」の2行フォーマット
- 同じ文言を繰り返さない。H2ごとに訴求軸をずらす（料金訴求→口コミ訴求→安心訴求等）
""",
}


def _get_quality_criteria(article_type: str) -> str:
    return _QUALITY_BASE + _QUALITY_TYPE.get(article_type, "")


def _normalize_heading(line: str) -> str:
    """Normalize full-width colon and markdown bold in heading lines."""
    return line.replace("：", ":").replace("**", "").strip()


def _parse_h2_sections(structure_text: str) -> list[str]:
    """Extract H2 blocks (each H2 + its H3s) from structure_text."""
    sections = []
    current = []
    in_body = False
    for line in structure_text.split("\n"):
        norm = _normalize_heading(line)
        if norm.startswith("H1:"):
            in_body = True
            continue
        if not in_body:
            continue
        if norm == "---":
            break
        if norm.startswith("H2:"):
            if current:
                sections.append("\n".join(current))
            current = [norm]
        elif norm.startswith("H3:") and current:
            current.append("  " + norm)
    if current:
        sections.append("\n".join(current))
    return sections


def _build_body_prompt(
    inputs: dict,
    structure: dict,
    clinic_info: dict,
    competitor_analysis: dict | None,
    h2_scope: str,
    include_h1: bool,
    use_clinic_placeholder: bool = False,
    site_parts: str = "",
) -> str:
    article_type = inputs["article_type"]
    clinic_names = list(clinic_info.keys()) if clinic_info else []
    clinic_info_text = "\n\n".join(
        f"【{name}】\n{info}" for name, info in clinic_info.items()
    )

    if article_type == "地域":
        type_context = f"ジャンル: {inputs['genre']}（地域名はメインKWから自動判断）"
    elif article_type == "比較":
        type_context = f"ジャンル: {inputs['genre']}"
    else:
        type_context = f"ジャンル: {inputs['genre']}（クリニック名・商標名はメインKWから自動判断）"

    recommended_note = (
        f"【最訴求プラン】{inputs['recommended']}\n"
        "※このプランを冒頭・おすすめセクションで最上位に配置してください。\n"
    ) if inputs.get("recommended") else ""

    if article_type == "商標" and len(clinic_names) == 1:
        clinic_restriction = (
            f"【掲載クリニック（1院専用）】\n"
            f"掲載クリニック：{clinic_names[0]}\n"
            "記事全体を通じてこの1院に絞った情報のみ記載する。\n"
            "他院との比較・他院名の言及・複数院前提の表現（「各クリニック」「おすすめの院」等）は一切使わない。\n"
        )
    elif clinic_names:
        clinic_restriction = (
            f"【クリニック紹介の制約】\n"
            f"紹介できるクリニック：{', '.join(clinic_names)}\n"
            "このリスト以外のクリニックは紹介しない。[要確認]としても出力しない。\n"
        )
    else:
        clinic_restriction = "クリニック指定なし：クリニック紹介セクションは設けない。\n"

    competitor_note = (
        f"【競合分析サマリー（差別化・網羅性チェックに使用）】\n"
        f"{competitor_analysis.get('analysis', '')[:2000]}\n"
    ) if competitor_analysis else ""

    type_instruction = _TYPE_INSTRUCTIONS.get(article_type, "")

    scope_note = (
        "H1と冒頭文から始め、【今回生成するH2】に列挙したH2セクションのみを出力してください。"
        if include_h1 else
        "【今回生成するH2】に列挙したH2セクションのみを出力してください。H1・冒頭文は出力しない。"
    )

    todo_note = (
        "最後に「---[要確認]リスト---」として取得できなかった項目を箇条書きでまとめる。"
        if not include_h1 else ""
    )

    clinic_placeholder_note = (
        "\n- おすすめクリニック紹介のH2セクション（「おすすめ」「紹介」「ランキング」等を含むH2）は、"
        "本文を一切生成しない。<h2>タグのみ出力し、直後に `<!-- クリニック紹介ブロック入る -->` とだけ記述して次のH2に進む。\n"
    ) if use_clinic_placeholder else ""

    site_parts_block = (
        f"\n{site_parts}\n"
        "※上記のサイト別パーツを使用すること。{{変数名}}は実際の内容に置き換えること。パーツリスト外のクラス名・タグは使用しない。\n"
    ) if site_parts else ""

    return f"""あなたはSEO記事の執筆専門家です。
記事全体の構成を把握したうえで、指定されたH2セクションのみHTMLで出力してください。

{scope_note}

【記事タイプ】{article_type}
【{type_context}】
【メインKW】{inputs['main_kw']}
【サブKW】{', '.join(inputs['sub_kw'])}
{recommended_note}
{clinic_restriction}
{competitor_note}
【記事全体の構成（把握用）】
{structure['structure_text'][:3000]}

【今回生成するH2】
{h2_scope}

【クリニック情報（このデータのみ使用・補完・推測禁止）】
{clinic_info_text[:3000] if clinic_info_text else "（情報なし）"}

{WRITING_RULES}
{type_instruction}
{site_parts_block}
【HTML出力ルール】
- 見出し: <h1>、<h2>、<h3> タグを使用（<h4>は禁止）
- 段落: <p> タグ（裸のテキスト禁止）
- マーカー: <span class="marker">テキスト</span>（各H3に1か所のみ。一文全体には引かない）
- 太文字: <span class="b">テキスト</span>
- 比較表: <table class="comparison-table"><thead><tr><th>...</th></tr></thead><tbody>...</tbody></table>
- [要確認]箇所: テキストをそのまま出力する（例: <p>[要確認：GoogleマップID]</p>）。補完しない
- HTMLの外側にコードブロック記号（```）をつけない。HTMLをそのまま出力する{clinic_placeholder_note}
{todo_note}
【出力前の自己チェック（必ず実行・チェック結果は出力しない）】
以下を確認し、違反があれば修正してからHTMLのみを出力してください。
① こそあど言葉（これ・それ・この・その・ここ・そこ）→ 具体的な名詞に置き換える
② 各H3の末尾がSoWhat（行動後の状態変化まで書いた締め文）になっているか
   NG：「〜重要です」「〜大切です」「〜しましょう」「〜確認してください」
   OK：「〜することで△△できます」「〜が続けやすくなります」等
③ H2直下に導入文（3〜4文・80〜120文字）があるか。いきなりH3・テーブル・リストで始まっていないか
④ 記事内参照表現（以下の・上記の・本記事・先ほど・前述・次のセクション等）が使われていないか
⑤ SoWhatの文末パターンが連続2H3以上重複していないか"""


def generate_body(
    inputs: dict,
    structure: dict,
    clinic_info: dict,
    claude_api_key: str,
    competitor_analysis: dict | None = None,
    site_parts: str = "",
) -> dict:
    client = anthropic.Anthropic(api_key=claude_api_key)

    use_clinic_placeholder = inputs.get("article_type") in ("地域", "比較")

    def _call(messages: list) -> str:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=messages,
        )
        return msg.content[0].text

    def _finish(raw: str, debug: str = "") -> dict:
        html_part = raw
        todo_part = ""
        if "---[要確認]リスト---" in raw:
            parts = raw.split("---[要確認]リスト---", 1)
            html_part = parts[0].strip()
            todo_part = parts[1].strip()
        result = {"html": html_part, "todo_list": todo_part}
        if debug:
            result["debug"] = debug
        return result

    h2_sections = _parse_h2_sections(structure["structure_text"])

    if not h2_sections:
        # H2パース失敗: 構成テキスト全体をスコープとして単発コール
        fallback_prompt = _build_body_prompt(
            inputs, structure, clinic_info, competitor_analysis,
            h2_scope=structure["structure_text"],
            include_h1=True,
            use_clinic_placeholder=use_clinic_placeholder,
            site_parts=site_parts,
        )
        return _finish(_call([{"role": "user", "content": fallback_prompt}]),
                       debug="H2パース失敗: フォールバック使用")

    mid = max(1, len(h2_sections) // 2)
    first_half = h2_sections[:mid]
    second_half = h2_sections[mid:]

    # ターン1: H1 + 冒頭文 + 前半H2（自己チェック内包）
    prompt1 = _build_body_prompt(
        inputs, structure, clinic_info, competitor_analysis,
        h2_scope="\n".join(first_half),
        include_h1=True,
        use_clinic_placeholder=use_clinic_placeholder,
        site_parts=site_parts,
    )
    messages = [{"role": "user", "content": prompt1}]
    part1 = _call(messages)

    if not second_half:
        return _finish(part1)

    # ターン2: 後半H2（前半の会話コンテキストを保持した状態で生成・自己チェック内包）
    messages.append({"role": "assistant", "content": part1})
    prompt2 = _build_body_prompt(
        inputs, structure, clinic_info, competitor_analysis,
        h2_scope="\n".join(second_half),
        include_h1=False,
        use_clinic_placeholder=use_clinic_placeholder,
        site_parts=site_parts,
    )
    messages.append({"role": "user", "content": prompt2})
    part2 = _call(messages)

    raw = part1.rstrip() + "\n" + part2.lstrip()
    return _finish(raw)


def quality_check(html: str, article_type: str, main_kw: str, sub_kw: list, claude_api_key: str) -> str:
    client = anthropic.Anthropic(api_key=claude_api_key)
    criteria = _get_quality_criteria(article_type)

    prompt = f"""以下の記事HTML（{article_type}記事）の品質チェックを実施してください。

【メインKW】{main_kw}
【サブKW】{', '.join(sub_kw)}

{criteria}

【記事HTML】
{html[:6000]}

チェック結果を以下の形式で出力してください。
全項目に対してひとつずつ判定を行い、スキップしないこと。

## 品質チェック結果（{article_type}記事）

### ❌ 要修正
| 項目番号 | 問題箇所（該当テキスト or タグ） | 修正指示（具体的に） |
|---------|-------------------------------|-------------------|
| Q〇 | （該当箇所を引用） | （どう直すか） |

### ⚠️ 要確認
| 項目番号 | 該当箇所 | 確認内容 |
|---------|---------|---------|
| Q〇 | （該当箇所） | （何を確認するか） |

### ✅ 問題なし
Q〇, Q〇, Q〇 …（問題なしの項目番号を列挙）
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text
