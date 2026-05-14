import anthropic


def _claude_call(api_key: str, prompt: str) -> str:
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as e:
        return f"[生成失敗: {e}]"


def _gemini_call(api_key: str, prompt: str) -> str:
    try:
        from google import genai as _genai
        client = _genai.Client(api_key=api_key)
        response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return response.text
    except Exception as e:
        return f"[生成失敗: {e}]"


def _llm_call(claude_api_key: str, prompt: str, gemini_api_key: str = "", provider: str = "claude") -> str:
    if provider == "gemini" and gemini_api_key:
        return _gemini_call(gemini_api_key, prompt)
    return _claude_call(claude_api_key, prompt)


def generate_structure(inputs: dict, competitor_analysis: dict, clinic_info: dict, claude_api_key: str, gemini_api_key: str = "", article_provider: str = "claude") -> dict:
    article_type = inputs["article_type"]
    clinics_list_parts = []
    for c in inputs["clinics"]:
        entry = f"- {c['name']} ({c['domain']})"
        if c.get("recommended"):
            entry += f"\n  最訴求プラン: {c['recommended']}"
        if c.get("appeal"):
            entry += f"\n  強み・比較優位性: {c['appeal']}"
        clinics_list_parts.append(entry)
    clinics_list = "\n".join(clinics_list_parts)
    clinic_info_text = "\n\n".join(
        f"【{name}】\n{info}" for name, info in clinic_info.items()
    )

    if article_type == "地域":
        type_note = (
            "地域名はメインKWから自動判断してください。\n"
            f"ジャンル: {inputs['genre']}\n"
            "固定H2：冒頭比較表、費用相場、おすすめクリニック紹介、まとめ"
        )
    elif article_type == "比較":
        type_note = (
            f"ジャンル: {inputs['genre']}\n"
            "固定H2：冒頭比較表、おすすめクリニック紹介、まとめ"
        )
    elif article_type == "ノウハウ":
        type_note = (
            f"ジャンル: {inputs['genre']}\n"
            "クリニック紹介ブロック・具体的な料金表は設けない。情報提供・解説に特化した構成にする。\n"
            "固定H2：冒頭文、まとめ（まとめの直前にCV記事誘導セクションを置く）\n"
            "CV記事誘導セクションは「関連CV記事へ誘導する1ブロック」。\n"
            "読者が次に取るべき行動（クリニック選びや費用確認）につながる橋渡し記事として設計する。\n"
            "冒頭文はAFDE構成：A（読者の悩みを具体的に提示）→ F（誰向けか絞り込む）→ D（記事で分かることを端的に示す）→ E（読んだあとに得られる状態）。"
        )
    else:  # 商標
        _clinics = inputs.get("clinics", [])
        if len(_clinics) == 1:
            type_note = (
                f"掲載クリニック：{_clinics[0]['name']}（このクリニック専用の記事）\n"
                f"ジャンル: {inputs['genre']}\n"
                "1院専用記事。他院との比較・複数院紹介は行わない。\n"
                "「おすすめクリニック紹介」「クリニックの選び方」等の複数院前提の構成要素は設けない。\n"
                "固定H2：冒頭（最訴求プラン・営業時間・諸費用）、まとめ"
            )
        else:
            # 商標記事は必ず1院専用。clinicsが複数渡されても先頭1院のみ使用
            _trademark_name = _clinics[0]["name"] if _clinics else "（メインKWから判断）"
            type_note = (
                f"掲載クリニック：{_trademark_name}（このクリニック専用の記事）\n"
                f"ジャンル: {inputs['genre']}\n"
                "1院専用記事。他院との比較・複数院紹介は行わない。\n"
                "「おすすめクリニック紹介」「クリニックの選び方」等の複数院前提の構成要素は設けない。\n"
                "固定H2：冒頭（最訴求プラン・営業時間・諸費用）、まとめ"
            )

    custom_note = f"\n【追加指示】\n{inputs['custom_block']}" if inputs.get("custom_block") else ""

    appeal_note = ""
    _appeals = [a for a in inputs.get("appeal_points", []) if a and a.strip()]
    if _appeals:
        _article_type_pl = inputs.get("article_type", "")
        if _article_type_pl == "商標":
            appeal_note = "\n【比較優位性・強み（構成の軸として使う）】\n"
            for i, ap in enumerate(_appeals, 1):
                appeal_note += f"強み{i}: {ap}\n"
            appeal_note += "※これらの強みが自然に伝わるH2/H3構成にする。強みを直接見出しにしない。\n"
        else:
            appeal_note = "\n【訴求インプット（優先度順）】\n"
            for i, ap in enumerate(_appeals, 1):
                appeal_note += f"第{i}訴求: {ap}\n"
            appeal_note += "※第1訴求を最も目立つ位置・強調度で記事に反映する。専用H2は不要、記事全体の訴求軸として使う。\n"

    user_awareness_note = ""
    if inputs.get("user_awareness", "").strip():
        user_awareness_note = f"\n【ユーザーの前提・認識レベル】\n{inputs['user_awareness']}\n※この認識状態を踏まえて、説明の深さ・切り口を調整する。\n"

    custom_intent_note = ""
    if inputs.get("custom_intent", "").strip():
        custom_intent_note = f"\n【追加指示の意図・切り口】\n{inputs['custom_intent']}\n※追加指示をこの意図・切り口で記事に組み込む。\n"

    related_kw_note = (
        f"\n【関連KW】\n{inputs['related_kw']}\n"
        "※重要なトピックが抜けていれば独立H2/H3を追加してよい。"
        "検索ボリュームが低そうな派生KWのために独立セクションは作らない。"
        "深掘りの優先順位はメインKW・サブKWを最優先とする。"
    ) if inputs.get("related_kw") else ""
    recommended_note = (
        f"\n【最訴求プラン】{inputs['recommended']}\n"
        "【配置ルール（厳守）】\n"
        "- 配置OK：おすすめクリニック紹介H2（最上位に配置）\n"
        "- 配置OK：費用相場・まとめ（文脈上自然であれば院名・プラン名を出してよい）\n"
        "- 配置NG：仕組み解説・選び方・症状説明・知識教育系セクション\n"
        "  理由：このセクションのユーザーはまだ「始めるかどうか」を検討中。\n"
        "  院名・プランを出すと広告感になり信頼を損なう。\n"
        "  先に治療を始める動機を固め、選択肢の提示はクリニック紹介ブロックで行う。\n"
    ) if inputs.get("recommended") else ""

    _clinic_count = inputs.get("clinic_count", 0)
    if _clinic_count > 0:
        _count_rule = f"掲載院数：{_clinic_count}院（厳守。おすすめクリニック紹介H2に{_clinic_count}院分のH3を設けること）"
    else:
        _count_rule = "掲載院数：競合の掲載院数に合わせること（競合分析を参照して適切な院数を判断）"
    clinics_note = (
        f"\n【クリニック紹介の制約】\n"
        f"{_count_rule}\n"
        "紹介するクリニックは以下のリストに限定する。\n"
        "リスト外のクリニックの[要確認]セクションは出力しない。\n"
        "クリニックが0件の場合、クリニック紹介H2自体を設けない。"
    ) if inputs["clinics"] else "\nクリニック指定なし：クリニック紹介H2は設けない。"

    revision_note = (
        f"\n\n【構成の修正指示（最優先で反映すること）】\n{inputs['_revision_note']}\n"
        if inputs.get("_revision_note") else ""
    )

    selected = inputs.get("selected_topics")
    from core.config import TOPICS, TOPIC_LABELS
    all_topics = TOPICS.get(article_type, [])
    optional_topics = [t for t in all_topics if not t["fixed"]]
    if selected is not None and optional_topics:
        included = [TOPIC_LABELS.get(k, k) for k in selected if k not in ("intro", "summary")]
        excluded = [TOPIC_LABELS.get(t["key"], t["key"]) for t in optional_topics if t["key"] not in selected]
        topics_note = "\n【含めるオプションセクション】\n"
        if included:
            topics_note += "含める: " + "、".join(included) + "\n"
        if excluded:
            topics_note += "含めない（H2を設けない）: " + "、".join(excluded) + "\n"
    else:
        topics_note = ""

    prompt = f"""あなたはSEO記事の構成設計の専門家です。
{topics_note}
以下の競合分析・クリニック情報を踏まえて、最適な記事構成（H1/H2/H3）を設計してください。

【競合分析結果】
{competitor_analysis.get('analysis', '（競合なし）')}

【クリニック情報】
{clinic_info_text if clinic_info_text else '（情報なし）'}

---

【記事タイプ】{article_type}
{type_note}
【メインKW】{inputs['main_kw']}
【サブKW】{', '.join(inputs['sub_kw'])}
{custom_note}{custom_intent_note}{appeal_note}{user_awareness_note}
{related_kw_note}
{recommended_note}
{clinics_note}

【掲載クリニック一覧（このリストのクリニックのみ紹介する）】
{clinics_list if clinics_list else "（指定なし）"}

【構成ルール】

■ 記事構成の設計思想（最重要）
この記事の入口は「メインKW・サブKWで検索してきたユーザーの検索意図」、出口は「CV（申込・予約・問い合わせ）」。
H2の役割は、入口の検索意図を満たしながら、ユーザーをCVへと論理的に誘導することである。

H2候補の選定基準：
- 競合が共通して入れている必須トピック
- 関連KWから想起されるユーザーが同時に知りたい情報
- ユーザーがCVを決断するために必要な情報
→ これらの候補を「ユーザーがCVする可能性が高い、納得しやすい順番」に並べる
→ 「このH2を読んだあとユーザーはCVに近づくか」を各H2で自問する。答えがNoなら不要

■ KWと見出しの原則
- H1: メインKWを自然に含む・32文字以内・問いかけ型禁止
- メインKWは記事前半のH2（1〜3番目）に必ず含める。メインKWはユーザーの知りたい気持ちが最も強いKWなので、前半で登場するのが自然
- サブKWは前半〜中盤のH2に散らして含める。各サブKWが最低1つのH2に入ること
- KWを含めた上で、そのジャンル・地域・商標でしか通じない固有の表現にする
- KWを含められない場合は見出し表現を変える。「費用について」「選び方のポイント」「注意事項」等の汎用表現はNG
- 見出しに使用できる記号は「！」「？」のみ。「（）」「｜」「、」「【】」はすべて禁止
- 「！」を使う場合、「！」の直前が内容のある語句になっていること。「ANS.！」「BEST！」のように「！」の前だけ切り取ると意味のない一語になる形は禁止
- H2・H3に「徹底」という語は使わない
- まとめのH2には「まとめ」という名称を使わない（記事の内容を表す見出しにする）
- **H3見出しには、メインKW・サブKW、または当該ジャンルに固有の語（共起語・処方・副作用・クリニック選び等）を自然に含める。どのKWも入らない場合でもジャンル固有の語を最低1語入れる**
- 【地域記事限定】地名を付け足しただけで本文が全国共通の内容になる見出しを作らない。見出しに地域固有情報（価格帯・エリア特性・アクセス）が反映されているかを確認してから採用する

■ H2構成
- サブKWごとの検索意図に対し、アンサーするH2を一つずつ設ける。複数のサブKWが近い意図なら一つのH2にまとめてよい
- 追加指示がある場合は適切な位置に挿入する
- **各H2は「そのH2だけで完結するトピック」を扱う。他のH2と内容が重なるH2は設けない**
- 費用・料金・コスト系は必ず1つのH2に集約する。後半で再度触れるのは禁止
- 対面/オンライン比較・診察方法・受診方法系は1つのH2に集約する
- 選び方・クリニックの探し方系は1つのH2に集約する
- 同じトピックを「前半で概要→後半で詳細」と分割するのも禁止。1つのH2で深く扱う

■ クリニック・商品紹介の配置
- 基本は記事前半（冒頭〜中盤）にクリニック・商品紹介を配置する
- ただし「ユーザーがこの知識なしにクリニック・商品を見ても判断できない」と判断できる場合のみ、必要最低限の教育コンテンツ（選び方・仕組み・費用軸の説明等）を先に入れてよい

■ H3構成
- 各H2に2〜4本（おすすめクリニック紹介H2の直下にはH3を置かない）

■ 最訴求プラン
- 指定されている場合、冒頭と該当セクションで最上位に配置する

【セクション別ルール】

■ 費用相場H2
- そのジャンルでユーザーが意思決定するために必要な費用項目をすべて網羅する（プラン料金だけでなく、診察料・薬代・送料・麻酔代・追加費用の有無等）
- 項目別で出すか総額で出すかは納得感を基準に判断する（比較しやすい軸なら項目別、総コストが重要なら総額）
- 【地域記事限定】その地域特有の価格帯コメントを1文入れる（「〇〇エリアでは〇万円台で選ぶと主要クリニックの価格帯に合いやすい」等。掲載院の価格帯と著しく乖離する相場感を断定しない）

■ エリア別おすすめH2（選択時）
- H3をエリア（駅名・地区名）ごとに設ける
- おすすめクリニック紹介ブロックとは別物。エリアの特徴・アクセス・そのエリアでクリニックを選ぶ観点を中心に書く

■ 治療法・プラン・症状別おすすめH2（選択時）
- H3を治療法・プラン・症状ごとに設ける
- 治療法/プラン/症状のどれでH3を切るかは、関連KW・競合構成・そのジャンルのユーザーの検索傾向で判断する

■ 診察/処方/施術/カウンセリングの流れH2（選択時）
- 「予約→診察/カウンセリング→施術/処方→アフターケア」の全ステップをカバーする
- オンライン/対面の別、所要時間、当日の注意点を含める
- H3はステップ単位またはフェーズ単位で切る（ジャンル特性に合わせて判断）

■ よくある質問H2（選択時）
- 記事内で直接扱えなかったネガティブ系・詳細系クエリ（怪しい・副作用・失敗・料金の疑問等）をQ&A形式で受け皿にする
- 各Q&Aは1H3ではなく、QA形式の箇条書きブロックとして1H2にまとめる構成を基本とする

■ 向いている人・向いていない人H2（選択時）
- 「どんな人に向いているか」「向いていない人の条件」を明示する
- 向いていない人へは代替案（他の治療法・他クリニック）を示してネガティブで終わらない

{revision_note}
以下の形式で出力してください：

タイトル案①: （メインKW含有・32文字以内）
タイトル案②:
タイトル案③:

メタディスクリプション: （80〜100文字・メインKW・サブKWを含む）

---
H1:

H2:（最初のH2）
　H3:
　H3:

H2:
　H3:
　H3:
（以下同様、すべてのH2/H3を列挙）
---

[要確認]リスト:
- （情報が取得できなかった項目があれば記載。クリニックリスト外の要確認は記載しない）

【出力前の自己チェック（必ず実行・修正してから出力）】
① メインKW「{inputs['main_kw']}」が前半1〜3番目のH2のいずれかに含まれているか。含まれていなければ見出し文言を修正する
② サブKW（{', '.join(inputs['sub_kw'])}）それぞれが最低1つのH2に含まれているか。含まれていないサブKWがあれば対応H2の文言を修正する
③ H2・H3に「（）」「｜」「、」「【】」が使われていないか
④ H2同士でトピックが重複していないか。費用系・対面/オンライン系・選び方系などが複数H2に分散していれば1つに統合する
⑤ H2・H3に「徹底」が使われていないか。あれば別の表現に置き換える
⑥ 「！」を使っている見出しで「！」の直前が意味のある語句になっているか（「ANS.！」等の形はNG）
⑦ 各H3見出しにメインKW・サブKW、またはジャンル固有語（共起語）が入っているか。汎用表現だけのH3があれば修正する
"""

    raw = _llm_call(claude_api_key, prompt, gemini_api_key=gemini_api_key, provider=article_provider)

    title, meta, todo_list = "", "", ""
    for line in raw.split("\n"):
        if line.startswith("タイトル案①:"):
            title = line.replace("タイトル案①:", "").strip()
        if line.startswith("メタディスクリプション:"):
            meta = line.replace("メタディスクリプション:", "").strip()

    if "[要確認]リスト:" in raw:
        todo_list = raw.split("[要確認]リスト:", 1)[1].strip()

    return {
        "title": title,
        "meta": meta,
        "structure_text": raw,
        "todo_list": todo_list,
    }
