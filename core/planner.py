import time
import google.generativeai as genai


def _gemini_call(model, prompt: str, max_retries: int = 3) -> str:
    """Gemini call with exponential backoff on 429."""
    waits = [15, 30, 60]
    for attempt in range(max_retries):
        try:
            return model.generate_content(prompt).text
        except Exception as e:
            err = str(e)
            is_rate = any(k in err for k in ("429", "Resource exhausted", "RESOURCE_EXHAUSTED", "quota"))
            if is_rate and attempt < max_retries - 1:
                time.sleep(waits[attempt])
                continue
            return f"[生成失敗: {err}]"
    return "[生成失敗: レートリミット上限]"


def generate_structure(inputs: dict, competitor_analysis: dict, clinic_info: dict, gemini_api_key: str) -> dict:
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    article_type = inputs["article_type"]
    clinics_list = "\n".join(f"- {c['name']} ({c['domain']})" for c in inputs["clinics"])
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
    else:
        type_note = (
            "クリニック名（商標名）はメインKWから自動判断してください。\n"
            f"ジャンル: {inputs['genre']}\n"
            "固定H2：冒頭（最訴求プラン・営業時間・諸費用）、まとめ"
        )

    custom_note = f"\n【追加指示】\n{inputs['custom_block']}" if inputs.get("custom_block") else ""
    related_kw_note = (
        f"\n【関連KW】\n{inputs['related_kw']}\n"
        "※重要なトピックが抜けていれば独立H2/H3を追加してよい。"
        "検索ボリュームが低そうな派生KWのために独立セクションは作らない。"
        "深掘りの優先順位はメインKW・サブKWを最優先とする。"
    ) if inputs.get("related_kw") else ""
    recommended_note = (
        f"\n【最訴求プラン】{inputs['recommended']}\n"
        "※記事構成の中でこのプランを最上位に配置してください。"
    ) if inputs.get("recommended") else ""

    clinics_note = (
        "\n【クリニック紹介の制約】\n"
        "紹介するクリニックは以下のリストに限定する。\n"
        "リスト外のクリニックの[要確認]セクションは出力しない。\n"
        "クリニックが0件の場合、クリニック紹介H2自体を設けない。"
    ) if inputs["clinics"] else "\nクリニック指定なし：クリニック紹介H2は設けない。"

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

    # Single prompt: load all context then request structure (avoids 2-call chat overhead)
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
{custom_note}
{related_kw_note}
{recommended_note}
{clinics_note}

【掲載クリニック一覧（このリストのクリニックのみ紹介する）】
{clinics_list if clinics_list else "（指定なし）"}

【構成ルール】
- H1: メインKWを自然に含む・32文字以内・問いかけ型禁止
- H2: 競合の必須トピックを網羅しつつ、競合にない差別化トピックも入れる。追加指示があれば適切な位置に挿入する
- H3: 各H2に2〜4本（おすすめクリニック紹介H2の直下にはH3を置かない）
- 見出しに「？」「｜」「、」「【】」は使わない。使える記号は「！」のみ
- サブKWを最低1本のH2に自然に含める
- 最訴求プランが指定されている場合、冒頭と該当セクションで最上位に配置する
- まとめのH2には「まとめ」という名称を使わない（内容を表す見出しにする）
- H3の見出しはそのKW・ジャンルでしか通じない固有の表現にする（「費用について」「選び方のポイント」等の汎用表現はNG）

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
"""

    raw = _gemini_call(model, prompt)

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
