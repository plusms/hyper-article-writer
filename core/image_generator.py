import base64
import json
import re
import requests
import anthropic
from typing import Optional

try:
    from google import genai as _google_genai
    from google.genai import types as _google_genai_types
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

try:
    import openai as _openai_lib
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

_IMAGE_MODEL = "gemini-2.0-flash-preview-image-generation"   # backward compat alias
_IMAGE_MODEL_GEMINI = "gemini-2.0-flash-preview-image-generation"
_IMAGE_MODEL_GEMINI_FALLBACKS = [
    "gemini-2.0-flash-exp",
    "imagen-3.0-generate-001",
    "imagen-3.0-fast-generate-001",
]
_IMAGE_MODEL_DALLE  = "dall-e-3"


# ── 組み込みレイアウトテンプレート ───────────────────────────────
BUILTIN_TEMPLATES: dict[str, dict] = {
    "side_by_side_3": {
        "name": "横並び3項目リスト",
        "base_prompt": """\
フラットデザインのクリーンなレイアウトを用いた、3項目リストインフォグラフィック。フォントは全体を通じてNoto Sans JPを使用すること。

**全体構成**
上部にタイトルエリアを配置し、その下に3つの同一サイズのカードを横に均等に並べた3列レイアウトを構成する。背景色は#F1F4FF、全体的に#49589Bを基調とした紺色。デザインはシンプルで白を活かした、清潔感のあるスタイルとする。

**上部タイトルエリア**
白（#FFFFFF）の角丸ボックス（角丸10px）を中央揃えで配置。ボックス内に#49589BのNoto Sans JP Boldテキストで「{{main_title}}」を中央揃えで表示する。ボックスには紫系のドロップシャドウ（rgba(73,88,155,0.4)）を付与する。

**カード構造（全体）**
3つのカードを横並びで均等配置。各カードは白（#FFFFFF）の角丸ボックス（角丸6px）で、紫系のドロップシャドウ（rgba(73,88,155,0.4)）を付与する。カード内部は上部にテキストエリア、下部にイラスト画像エリア（白背景）の2分割構造。

**個別カードの詳細**
- カード1（左）: テキスト「{{card_text_1}}」（Noto Sans JP Bold 30px、#49589B、中央揃え）、画像「{{card_image_content_1}}」
- カード2（中央）: テキスト「{{card_text_2}}」（同上）、画像「{{card_image_content_2}}」
- カード3（右）: テキスト「{{card_text_3}}」（同上）、画像「{{card_image_content_3}}」

**イラスト指定（全画像エリア共通）**
黒の細いアウトラインを使用したフラットデザインのイラスト。白背景・装飾・影なし。登場人物は日本人男性（20〜50代の現役世代）、上半身、背後・斜め角に向かうこと。髪色は#49589B（ダークカラー）、肌色は#FFFFFF、衣装は#49589B系の色のシンプルな長袖シャツ。顔はシンプルに描き、目は小さな点または細いライン・口はシンプルな曲線のみ、鼻筋・顔ライン・陰影などの細かい描写は入れないこと。シワ・白髪など年齢を特定できる要素はNG。彫りの深さ・ひげが濃いなど外国人っぽい描写はNG。使用色は#FFFFFFを除く3色まで（#49589Bを基調とすること）。画像内に文字・記号・数字は一切入れないこと。

日本語の文字崩れがないよう正確にレンダリングすること。""",
    },
    "comparison_table": {
        "name": "項目付き比較表（2対象×3項目）",
        "base_prompt": """\
フラットデザインのクリーンなレイアウトを用いた、2対象×3項目の比較表インフォグラフィック。フォントは全体を通じてNoto Sans JPを使用すること。

**全体構成**
上部にタイトルエリアを配置し、その下に左側の比較項目列と右側の2対象列（計3列）を並べた比較表レイアウトを構成する。背景色は#F1F4FF、全体的に#49589Bを基調とした紺色。

**上部タイトルエリア**
白（#FFFFFF）の角丸ボックス（角丸10px）を中央揃えで配置。ボックス内に#49589BのNoto Sans JP Boldテキストで「{{main_title}}」を中央揃えで表示する。ボックスには紫系のドロップシャドウ（rgba(73,88,155,0.4)）を付与する。

**比較表構造**
3列構成。各列の詳細：
- 左列（比較項目）: 3行のセル。各セルの背景は#FFE9E3（薄ピンク）、角丸4px、Noto Sans JP Bold 32px、#49589B、中央揃え。セル間に背景色（#F1F4FF）の細い区切りを設ける。
- 中央列（比較対象1）: 上部ヘッダーは背景#49589B・角丸6px 6px 0px 0px・Noto Sans JP Bold 30px・白（#FFFFFF）・中央揃え。3行の内容セルは背景白（#FFFFFF）・Noto Sans JP Bold 32px・#333333・中央揃え。セル間に#FFE9E3の細いボーダーで区切る。
- 右列（比較対象2）: 中央列と同構造。

**個別セルの詳細**
- 比較項目: 「{{item_label_1}}」「{{item_label_2}}」「{{item_label_3}}」
- 比較対象1: ヘッダー「{{compare_name_1}}」、内容「{{compare_1_item_1}}」「{{compare_1_item_2}}」「{{compare_1_item_3}}」
- 比較対象2: ヘッダー「{{compare_name_2}}」、内容「{{compare_2_item_1}}」「{{compare_2_item_2}}」「{{compare_2_item_3}}」

日本語の文字崩れがないよう正確にレンダリングすること。""",
    },
    "vertical_list_3": {
        "name": "縦積み見出しつきリスト（3項目）",
        "base_prompt": """\
フラットデザインのクリーンなレイアウトを用いた、見出しつき縦積み3項目リストインフォグラフィック。フォントは全体を通じてNoto Sans JPを使用すること。

**全体構成**
上部にタイトルエリアを配置し、その下に3つのリストアイテムを縦に積み重ねたレイアウトを構成する。背景色は#F1F4FF、全体的に#49589Bを基調とした紺色。

**上部タイトルエリア**
白（#FFFFFF）の角丸ボックス（角丸10px）を中央揃えで配置。ボックス内に#49589BのNoto Sans JP Boldテキストで「{{main_title}}」を中央揃えで表示する。ボックスには紫系のドロップシャドウ（rgba(73,88,155,0.4)）を付与する。

**リストアイテム構造（全体）**
3つのアイテムを縦に均等配置。各アイテムは左右2分割の横長カード（角丸6px、ドロップシャドウ）。
- 左エリア（見出し）: 背景#FFE9E3（薄ピンク）、角丸6px 0px 0px 6px、Noto Sans JP Bold 32px、#49589B、左揃え
- 右エリア（内容）: 背景白（#FFFFFF）、角丸0px 6px 6px 0px、左に100×100pxのイラスト画像（白背景）、右に説明テキスト

**個別アイテムの詳細**
- アイテム1: 左見出し「{{item_header_1}}」、右テキスト「{{item_body_1}}」（Noto Sans JP Medium 30px、#333333、左揃え）、右画像「{{item_image_content_1}}」
- アイテム2: 左見出し「{{item_header_2}}」、右テキスト「{{item_body_2}}」（同上）、右画像「{{item_image_content_2}}」
- アイテム3: 左見出し「{{item_header_3}}」、右テキスト「{{item_body_3}}」（同上）、右画像「{{item_image_content_3}}」

**イラスト指定（全画像エリア共通）**
黒の細いアウトラインを使用したフラットデザインのイラスト。白背景・装飾・影なし。登場人物は日本人男性（20〜50代の現役世代）、上半身、背後・斜め角に向かうこと。髪色は#49589B（ダークカラー）、肌色は#FFFFFF、衣装は#49589B系の色のシンプルな長袖シャツ。顔はシンプルに描き、目は小さな点または細いライン・口はシンプルな曲線のみ、鼻筋・顔ライン・陰影などの細かい描写は入れないこと。シワ・白髪など年齢を特定できる要素はNG。彫りの深さ・ひげが濃いなど外国人っぽい描写はNG。使用色は#FFFFFFを除く3色まで（#49589Bを基調とすること）。画像内に文字・記号・数字は一切入れないこと。イラストエリアが小さいため、アイコンに近いシンプルな表現にし、人物単体または専門家単体で構成すること。

日本語の文字崩れがないよう正確にレンダリングすること。""",
    },
}


# ── プロンプト生成（シングルテンプレート）────────────────────────
def _resolve_template_prompt(tmpl: dict) -> str:
    """テンプレート辞書からベースプロンプト文字列を解決する。"""
    layout_type = tmpl.get("layout_type", "")
    if layout_type and layout_type in BUILTIN_TEMPLATES:
        return BUILTIN_TEMPLATES[layout_type]["base_prompt"]
    return tmpl.get("base_prompt", "").strip()


def generate_image_prompts(
    structure_text: str,
    site_config: dict,
    claude_api_key: str,
    slug: str,
) -> list[dict]:
    """Claude でH2/H3向けの画像プロンプトJSONを生成する。複数テンプレートに対応。"""
    templates = site_config.get("image_templates", [])
    if not templates:
        return []

    valid_templates = [
        {"id": i + 1, "name": t.get("name") or t.get("layout_type") or f"テンプレート{i+1}", "prompt": _resolve_template_prompt(t)}
        for i, t in enumerate(templates)
        if _resolve_template_prompt(t)
    ]
    if not valid_templates:
        return []

    if len(valid_templates) == 1:
        templates_section = f"""## 画像テンプレート（ベースプロンプト）
```
{valid_templates[0]["prompt"]}
```

## 使用ルール
- テンプレートの {{{{変数名}}}} を記事の各H2/H3の内容に合わせて差し替える
- 構造・カラーコード・レイアウトは変更しない。テキスト内容のみ差し替える
- filenameは「{slug}-英単語.webp」形式。英単語は画像の内容を1語で表す説明的な単語にすること（例：effect / cost / flow / merit / risk / compare / method / point）。番号（1・2・3）や"word"は使用禁止
- 出力JSONの各要素に "template_id": 1 を含める"""
    else:
        tmpl_blocks = "\n\n".join(
            f"### テンプレート{t['id']}（{t['name']}）\n```\n{t['prompt']}\n```"
            for t in valid_templates
        )
        tmpl_ids = ", ".join(str(t["id"]) for t in valid_templates)
        templates_section = f"""## 画像テンプレート（複数）
以下のテンプレートから、各H2/H3の内容・レイアウト適性に最も合うものを選んで使用してください。

{tmpl_blocks}

## 使用ルール
- 各画像ごとに最適なテンプレートを選ぶ（すべて同じテンプレートである必要はない）
- テンプレートの {{{{変数名}}}} を記事の各H2/H3の内容に合わせて差し替える
- 構造・カラーコード・レイアウトは変更しない。テキスト内容のみ差し替える
- filenameは「{slug}-英単語.webp」形式。英単語は画像の内容を1語で表す説明的な単語にすること（例：effect / cost / flow / merit / risk / compare / method / point）。番号（1・2・3）や"word"は使用禁止
- 出力JSONの各要素に "template_id": （使用したテンプレート番号: {tmpl_ids}のいずれか）を含める"""

    prompt = f"""あなたは画像プロンプト生成の専門家です。
以下の記事構成を読み、記事に挿入すべき画像を3〜5箇所選定し、下記テンプレートをベースにプロンプトをJSON形式で出力してください。

{templates_section}

## 記事構成
{structure_text}

## 変数置換ルール（最重要・厳守）
- テンプレート中の {{{{変数名}}}} はすべて実際の内容に置き換えること
- 出力する "prompt" フィールドに {{{{ }}}} 形式の変数を1つも残してはならない
- 変数名（例：card_label_sub, main_title, item_header_1 など）は置換後のプロンプトに含めない
- 変数の意味が不明な場合は、記事の内容・見出し・テーマから最も自然な語句を推測して埋める
- 「変数名のまま出力」は絶対禁止。必ずテキスト内容で置き換えること

## 出力形式（JSON配列のみ・説明文・コードフェンス不要）
[
  {{
    "position": "挿入位置の見出しテキスト",
    "filename": "{slug}-effect.webp",
    "alt": "画像の内容説明（日本語20〜40字）",
    "template_id": 1,
    "prompt": "（変数差し替え済みのプロンプト全文。{{{{}}}}形式の文字列が一切含まれないこと）"
  }}
]
"""
    client = anthropic.Anthropic(api_key=claude_api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    items = json.loads(text)

    # 置換漏れ検知 → 残っていたら2nd passで自動補完
    needs_retry = [(i, item) for i, item in enumerate(items) if re.search(r'\{\{[^}]+\}\}', item.get("prompt", ""))]
    if needs_retry:
        for i, item in needs_retry:
            unresolved = re.findall(r'\{\{[^}]+\}\}', item["prompt"])
            retry_prompt = f"""以下のプロンプト文中に {{{{変数名}}}} 形式の未置換変数が残っています。
記事構成の内容をもとに、すべての変数を実際のテキストに置き換えてください。

## 対象プロンプト
{item["prompt"]}

## 未置換変数
{', '.join(set(unresolved))}

## 記事構成（参考）
{structure_text[:2000]}

## 出力ルール
- 変数をすべて置き換えたプロンプト全文のみ出力する
- {{{{}}}} 形式の文字列を1つも残さない
- 説明文・コードフェンス不要"""
            retry_msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                messages=[{"role": "user", "content": retry_prompt}],
            )
            items[i]["prompt"] = retry_msg.content[0].text.strip()

    return items


# ── 見本画像 → テンプレート＋トンマナ自動生成（Claude Vision）──
def generate_template_from_image(
    image_bytes: bytes,
    mime_type: str,
    site_config: dict,
    claude_api_key: str,
) -> str:
    """見本画像をClaude Visionで解析して再利用可能なプロンプトテンプレートを生成する。"""
    color_instruction = ""
    if site_config:
        colors = site_config.get("design_rules", {}).get("colors", {})
        if colors:
            label_map = {
                "main": "メインカラー", "accent_red": "アクセント（赤）",
                "accent_yellow": "アクセント（黄）", "accent_orange": "アクセント（オレンジ）",
                "bg_white": "背景（白）", "bg_gray": "背景（グレー）", "text": "テキスト",
            }
            color_instruction = (
                "\n## サイト指定カラーパレット（最優先）\n"
                "以下のカラーコードはサイトの正式カラーです。"
                "画像から読み取った色ではなく、**このパレットの値を優先して使用すること**。\n"
            )
            for key, val in colors.items():
                color_instruction += f"- {label_map.get(key, key)}: {val}\n"
            color_instruction += "画像から読み取った色が上記パレットに近い場合は、必ずパレットの値に置き換えて出力すること。\n"

    meta_prompt = (
        "あなたは画像生成プロンプトの設計専門家です。\n"
        "添付された参照画像を詳細に解析し、同じ構造・デザインの画像を異なるテーマ・内容で再現・応用できる汎用プロンプトテンプレートを生成してください。\n"
        + color_instruction +
        "\n## 出力言語\n**すべて日本語で出力すること。英語・ローマ字の混在禁止。**\n"
        "\n## 出力形式（厳守）\n以下の6セクションを**この順番・この構造で必ず出力すること**。セクションの追加・削除・順序変更は禁止。\n"
        "\n### [1] スタイル宣言（冒頭1文）\nデザイン様式・ジャンル・レイアウト種別（列数・グリッド形式など）・フォント指定を1文で記述する。\n"
        "\n### [2] 全体構成\n以下の項目をすべて含めて記述する。\n"
        "- レイアウト骨格（タイトルエリアの位置・カードの配置形式）\n"
        "- 背景色（カラーコードで記載）\n- 基調カラー2色（カラーコードで記載）\n"
        "- デザインテイスト（余白感・スタイル）\n"
        "\n### [3] 上部タイトルエリア\n画像のタイトルエリアのデザインを記述する。\n"
        "- 配置方法（左右分割 / 中央揃え / その他）\n"
        "- テキスト要素の数・色・フォント・装飾・形状を画像から正確に読み取って記述\n"
        "- テキスト内容はすべて {{変数名}} 形式で変数化する（元画像のテキストをそのまま出力しない）\n"
        "- 変数の個数・命名は画像の構造に従って自由に決める\n"
        "\n### [4] カード共通仕様\n画像のカード構造を記述する。\n"
        "- カードの外形（角丸/角型・枠線の有無と色・背景色）\n"
        "- カード内部のエリア分割方法（上下 / 左右 / その他）\n"
        "- 各エリアに含まれる要素を列挙し、それぞれの色・フォント・形状・配置を記述\n"
        "- テキスト内容・イラスト指示（何を描くか）はすべて {{変数名}} 形式で変数化する\n"
        "  （元画像のテキスト・イラスト内容をそのまま出力しない。必ず変数に置き換える）\n"
        "- **リスト・繰り返し要素（箇条書き・行・アイテムなど）は連番変数で定義する**\n"
        "  - 例：4行リストなら {{list_label_1}}〜{{list_label_4}}、{{list_detail_1}}〜{{list_detail_4}} と定義\n"
        "  - `_n` や `_N` のような汎用記号での定義は禁止。必ず実際の個数分だけ番号を振る\n"
        "- 変数の種類・個数・命名は画像の構造に従って自由に決める\n"
        "\n### [5] 個別カードの詳細\n画像に含まれるカードを1枚ずつ記述する。\n"
        "- カードの位置を明示する（例：左・中央・右 / 上・下）\n"
        "- **[4] で定義した変数を1つも省略せずすべて列挙すること**\n"
        "  - 連番変数（例：{{list_label_1}}〜{{list_label_4}}）は番号ごとに必ず全て記述する\n"
        "  - 「〜以下同様」「省略」などの省略表記は禁止\n"
        "- カードの枚数・変数の個数は画像の実態に合わせる\n"
        "\n### [6] 品質指示（末尾1文）\n「日本語の文字崩れがないよう正確にレンダリングすること。」を固定で出力する。\n"
        "\n## 変数化のルール\n"
        "- テキスト内容・イラスト指示（記事ごとに差し替えが必要な内容）はすべて {{変数名}} に変数化する\n"
        "- **元画像のテキスト・イラスト内容をそのまま出力することを厳禁**。必ず変数に置き換えること\n"
        "- 変数形式：{{変数名}}（スネークケース・英小文字）\n"
        "- デザイン要素（カラーコード・形状・レイアウト・フォント）は**変数化せず固定値で記述する**\n"
        "\n## カラーコードのルール（厳守）\n"
        "- サイト指定カラーパレットがある場合は**パレットの値を最優先**。画像から読み取った色より上位\n"
        "- パレットにない色のみ、画像から読み取った値をそのまま使用すること\n"
        "- カラーコードの解釈・近似・変換・省略は禁止\n"
        "- カラーコードは必ず6桁の16進数（例：#47c1d3）で記述する\n"
        "\n## 禁止事項\n"
        "- 画像に存在しない要素を追加しない\n"
        "- セクション番号・見出し（[1]〜[6]）は出力に含めない（プロンプト本文のみ出力）\n"
        "- 英語・ローマ字での出力\n"
        "- 元画像のテキスト・イラスト内容をそのまま変数化せずに出力すること"
    )

    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
    client = anthropic.Anthropic(api_key=claude_api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_data}},
            {"type": "text", "text": meta_prompt},
        ]}],
    )
    return msg.content[0].text.strip()


def generate_tone_from_image(
    image_bytes: bytes,
    mime_type: str,
    claude_api_key: str,
) -> str:
    """見本画像からサイトのトンマナを一言で生成する。"""
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
    client = anthropic.Anthropic(api_key=claude_api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_data}},
            {"type": "text", "text": "この画像のデザインスタイル・雰囲気・トンマナを一言で表現してください（例: 医療的でクリーン、ビジネスライク、ポップで明るい）。30文字以内で出力してください。説明文不要。"},
        ]}],
    )
    return msg.content[0].text.strip()


# ── 画像生成（Gemini）────────────────────────────────────────
def _generate_image_bytes_gemini(
    prompt: str,
    gemini_api_key: str,
    model_override: Optional[str] = None,
) -> Optional[bytes]:
    if not _GENAI_AVAILABLE:
        raise ImportError("google-genai がインストールされていません")
    primary = model_override or _IMAGE_MODEL_GEMINI
    models_to_try = [primary]
    if not model_override:
        for fb in _IMAGE_MODEL_GEMINI_FALLBACKS:
            if fb != primary:
                models_to_try.append(fb)

    client = _google_genai.Client(api_key=gemini_api_key)
    last_err = None
    for try_model in models_to_try:
        try:
            if "imagen" in try_model.lower():
                response = client.models.generate_images(
                    model=try_model,
                    prompt=prompt,
                    config=_google_genai_types.GenerateImagesConfig(number_of_images=1),
                )
                if response.generated_images:
                    return response.generated_images[0].image.image_bytes
                return None
            else:
                response = client.models.generate_content(
                    model=try_model,
                    contents=prompt,
                    config=_google_genai_types.GenerateContentConfig(response_modalities=["IMAGE"]),
                )
                for part in response.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        return part.inline_data.data
            return None
        except Exception as e:
            err_str = str(e).lower()
            if "404" in err_str or "not found" in err_str or "not_found" in err_str:
                last_err = e
                continue
            raise
    raise last_err or RuntimeError("全モデルで画像生成に失敗しました")


# ── 画像生成（DALL-E）────────────────────────────────────────
def _generate_image_bytes_dalle(
    prompt: str,
    openai_api_key: str,
) -> Optional[bytes]:
    if not _OPENAI_AVAILABLE:
        raise ImportError("openai がインストールされていません（pip install openai）")
    client = _openai_lib.OpenAI(api_key=openai_api_key)
    response = client.images.generate(
        model=_IMAGE_MODEL_DALLE,
        prompt=prompt,
        size="1024x1024",
        quality="standard",
        n=1,
        response_format="url",
    )
    url = response.data[0].url
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.content


# ── 統合インターフェース ────────────────────────────────────
def generate_image_bytes(
    prompt: str,
    gemini_api_key: str = "",
    openai_api_key: str = "",
    provider: str = "gemini",
    model_override: Optional[str] = None,
) -> Optional[bytes]:
    """指定プロバイダーで画像を生成して bytes を返す。"""
    if provider == "dalle":
        return _generate_image_bytes_dalle(prompt, openai_api_key)
    return _generate_image_bytes_gemini(prompt, gemini_api_key, model_override)


def generate_image_preview(
    prompt: str,
    gemini_api_key: str = "",
    openai_api_key: str = "",
    provider: str = "gemini",
) -> Optional[bytes]:
    """プロンプトから画像プレビューを生成して bytes を返す。"""
    if provider == "dalle":
        return _generate_image_bytes_dalle(prompt, openai_api_key)
    if not _GENAI_AVAILABLE:
        raise ImportError("google-genai がインストールされていません")
    client = _google_genai.Client(api_key=gemini_api_key)
    response = client.models.generate_content(
        model=_IMAGE_MODEL_GEMINI,
        contents=prompt,
        config=_google_genai_types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data
    return None
