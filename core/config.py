# ── 使うモデル ─────────────────────────────────────────────
# 全部ここで固定する。人が選べるようにすると出力の精度が担当者ごとにぶれる。
# 変えるときはこのファイルだけを直す。
#
# 書く工程（構成・本文・紹介ブロック・検品・品質チェック）は上位モデル。
# 調べる工程（競合分析・案件探索・選び方の要約）は軽いモデル。
CLAUDE_WRITER_MODEL = "claude-sonnet-4-6"
CLAUDE_RESEARCH_MODEL = "claude-haiku-4-5-20251001"
GEMINI_TEXT_MODEL = "gemini-2.5-flash"
GEMINI_RESEARCH_MODEL = "gemini-2.5-flash"
# 日付入りのIDを指定する。日付なしのIDは指す先がいつの間にか変わるので、
# 同じ入力でも出力が変わって精度がぶれる。
OPENAI_TEXT_MODEL = "gpt-5.5-2026-04-23"
OPENAI_RESEARCH_MODEL = "gpt-5.4-mini-2026-03-17"

OPENAI_API_KEY = ""


def set_openai(api_key: str = "") -> None:
    """app.py が起動時にキーを入れる。モデルは上の定数で固定する。"""
    global OPENAI_API_KEY
    OPENAI_API_KEY = api_key or ""


def openai_ready() -> bool:
    return bool(OPENAI_API_KEY and OPENAI_TEXT_MODEL)


def list_openai_text_models(api_key: str) -> list:
    """そのキーで使えるモデルのうち、文章生成に使うものを返す。

    モデルを固定するときの候補確認に使う。画面の選択肢には出さない。
    """
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    skip = ("image", "audio", "realtime", "transcribe", "tts", "embedding", "moderation")
    ids = []
    for model in client.models.list().data:
        model_id = getattr(model, "id", "")
        if not model_id.startswith(("gpt", "o1", "o3", "o4")):
            continue
        if any(word in model_id for word in skip):
            continue
        ids.append(model_id)
    return sorted(set(ids))


def call_openai_messages(messages: list, max_tokens: int = 8192, model: str = "") -> str:
    """OpenAIで文章を生成する。Responsesが通らないSDK・モデルでも動くよう2段構え。"""
    from openai import OpenAI
    model = model or OPENAI_TEXT_MODEL
    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        response = client.responses.create(
            model=model,
            input=messages,
            max_output_tokens=max_tokens,
        )
        text = getattr(response, "output_text", "") or ""
        if text.strip():
            return text
    except Exception:
        pass
    completion = client.chat.completions.create(
        model=model,
        messages=messages,
        max_completion_tokens=max_tokens,
    )
    return completion.choices[0].message.content or ""


def call_openai(prompt: str, max_tokens: int = 8192, model: str = "") -> str:
    return call_openai_messages(
        [{"role": "user", "content": prompt}], max_tokens=max_tokens, model=model,
    )

TOPICS = {
    "地域": [
        {"key": "intro",            "label": "★ 冒頭（冒頭文 + 比較表）",           "fixed": True,  "default": True},
        {"key": "how_to_choose",    "label": "クリニックの選び方",                   "fixed": False, "default": True},
        {"key": "price_range",      "label": "費用相場",                             "fixed": False, "default": True},
        {"key": "recommended",      "label": "おすすめ紹介ブロック",                 "fixed": False, "default": True},
        {"key": "area_clinics",     "label": "エリア別おすすめ",                     "fixed": False, "default": False},
        {"key": "treatment_type",   "label": "治療法・プラン・症状別おすすめ",       "fixed": False, "default": False},
        {"key": "fit_unfit",        "label": "向いている人・向いていない人",         "fixed": False, "default": False},
        {"key": "faq",              "label": "よくある質問",                         "fixed": False, "default": False},
        {"key": "summary",          "label": "★ まとめ",                             "fixed": True,  "default": True},
    ],
    "比較": [
        {"key": "intro",            "label": "★ 冒頭（冒頭文 + 比較表）",           "fixed": True,  "default": True},
        {"key": "how_to_choose",    "label": "クリニックの選び方",                   "fixed": False, "default": True},
        {"key": "price_range",      "label": "費用相場",                             "fixed": False, "default": True},
        {"key": "recommended",      "label": "おすすめ紹介ブロック",                 "fixed": False, "default": True},
        {"key": "treatment_flow",   "label": "診察/処方/施術/カウンセリングの流れ", "fixed": False, "default": False},
        {"key": "fit_unfit",        "label": "向いている人・向いていない人",         "fixed": False, "default": True},
        {"key": "faq",              "label": "よくある質問",                         "fixed": False, "default": False},
        {"key": "summary",          "label": "★ まとめ",                             "fixed": True,  "default": True},
    ],
    "商標": [
        {"key": "intro",            "label": "★ 冒頭（冒頭文 + おすすめプラン + 営業時間 + 諸費用）", "fixed": True,  "default": True},
        {"key": "pricing",          "label": "料金プラン",                           "fixed": False, "default": True},
        {"key": "treatment_flow",   "label": "診察/処方/施術/カウンセリングの流れ", "fixed": False, "default": True},
        {"key": "reviews",          "label": "口コミ・評判",                         "fixed": False, "default": True},
        {"key": "fit_unfit",        "label": "向いている人・向いていない人",         "fixed": False, "default": False},
        {"key": "coupons",          "label": "クーポン・割引情報",                   "fixed": False, "default": False},
        {"key": "faq",              "label": "よくある質問",                         "fixed": False, "default": False},
        {"key": "summary",          "label": "★ まとめ",                             "fixed": True,  "default": True},
    ],
    "ノウハウ": [
        {"key": "intro",            "label": "★ 冒頭文",                             "fixed": True,  "default": True},
        {"key": "what_is",          "label": "〇〇とは（基本解説）",                 "fixed": False, "default": True},
        {"key": "types",            "label": "種類・分類",                           "fixed": False, "default": False},
        {"key": "how_to_choose",    "label": "選び方・比較軸",                       "fixed": False, "default": True},
        {"key": "cost",             "label": "費用・料金相場",                       "fixed": False, "default": False},
        {"key": "flow",             "label": "手順・流れ",                           "fixed": False, "default": False},
        {"key": "merit_demerit",    "label": "メリット・デメリット",                 "fixed": False, "default": False},
        {"key": "faq",              "label": "よくある質問",                         "fixed": False, "default": False},
        {"key": "cv_link",          "label": "★ CV記事への誘導",                     "fixed": True,  "default": True},
        {"key": "summary",          "label": "★ まとめ",                             "fixed": True,  "default": True},
    ],
}

TOPIC_LABELS = {
    "intro":           "冒頭（冒頭文 + 比較表）",
    "how_to_choose":   "クリニックの選び方",
    "price_range":     "費用相場",
    "recommended":     "おすすめクリニック紹介ブロック",
    "area_clinics":    "エリア別おすすめクリニック",
    "treatment_type":  "治療法・プラン・症状別おすすめ",
    "treatment_flow":  "診察/処方/施術/カウンセリングの流れ",
    "fit_unfit":       "向いている人・向いていない人",
    "faq":             "よくある質問",
    "summary":         "まとめ",
    "pricing":         "料金プラン",
    "reviews":         "口コミ・評判",
    "coupons":         "クーポン・割引情報",
    # ノウハウ
    "what_is":         "〇〇とは（基本解説）",
    "types":           "種類・分類",
    "cost":            "費用・料金相場",
    "flow":            "手順・流れ",
    "merit_demerit":   "メリット・デメリット",
    "cv_link":         "CV記事への誘導",
}
