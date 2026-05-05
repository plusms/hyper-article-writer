import json
import anthropic
from typing import Optional

try:
    from google import genai as _google_genai
    from google.genai import types as _google_genai_types
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

_IMAGE_MODEL = "gemini-3.1-flash-image-preview"
_VISION_MODEL = "gemini-2.0-flash"


def generate_image_prompts(
    structure_text: str,
    site_config: dict,
    claude_api_key: str,
    slug: str,
) -> list[dict]:
    """Claude で各H2/H3向けの画像プロンプトJSON を生成する。"""
    templates = site_config.get("image_templates", [])
    if not templates:
        return []

    templates_text = ""
    for t in templates:
        templates_text += (
            f"\n### テンプレート名: {t['name']}\n"
            f"使用シーン: {t.get('usage_scene', '')}\n"
            f"ベースプロンプト:\n```\n{t['base_prompt']}\n```\n"
        )

    prompt = f"""あなたは画像プロンプト生成の専門家です。
以下の記事構成と画像テンプレートを元に、記事に挿入すべき画像のプロンプトをJSON形式で出力してください。

## 利用可能な画像テンプレート
{templates_text}

## テンプレート選択ルール
- テンプレートの「使用シーン」をもとに、各H2/H3に最適なテンプレートを選ぶ
- 同じテンプレートを2回連続して使わない

## 実行手順
1. 記事構成（H2/H3）を読み込み、画像を挿入すべき箇所を3〜5箇所選定する
2. 各箇所に最適なテンプレートを選択する
3. テンプレートの{{{{変数名}}}}を記事の内容（H2/H3のテキスト・説明）に合わせて差し替える
   - 構造・カラーコード・レイアウトは変更しない
   - テキスト内容のみを記事内容に合わせて差し替える
4. filenameは「{slug}-英単語.webp」形式（記事内重複なし・英小文字・ハイフン区切り可）

## 記事構成
{structure_text}

## 出力形式（JSON配列のみ・説明文・コードフェンス不要）
[
  {{
    "position": "挿入位置の見出しテキスト（H2またはH3のテキストをそのまま）",
    "filename": "{slug}-word.webp",
    "alt": "画像の内容説明（日本語20〜40字）",
    "template": "テンプレート名",
    "prompt": "（変数差し替え済みのプロンプト全文）"
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
    return json.loads(text)


def generate_template_from_image(
    image_bytes: bytes,
    mime_type: str,
    site_config: dict,
    gemini_api_key: str,
) -> str:
    """参照画像を解析して再利用可能なプロンプトテンプレートを生成する。"""
    if not _GENAI_AVAILABLE:
        raise ImportError("google-genai がインストールされていません")

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

    client = _google_genai.Client(api_key=gemini_api_key)
    response = client.models.generate_content(
        model=_VISION_MODEL,
        contents=[
            _google_genai_types.Part(
                inline_data=_google_genai_types.Blob(mime_type=mime_type, data=image_bytes)
            ),
            meta_prompt,
        ],
    )
    return response.text.strip()


def generate_image_preview(prompt: str, gemini_api_key: str) -> Optional[bytes]:
    """プロンプトから画像プレビューを生成して bytes を返す。失敗時は None。"""
    if not _GENAI_AVAILABLE:
        raise ImportError("google-genai がインストールされていません")
    client = _google_genai.Client(api_key=gemini_api_key)
    response = client.models.generate_content(
        model=_IMAGE_MODEL,
        contents=prompt,
        config=_google_genai_types.GenerateContentConfig(
            response_modalities=["IMAGE"],
        ),
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data
    return None


def generate_image_bytes(
    prompt: str, gemini_api_key: str, model_override: Optional[str] = None
) -> Optional[bytes]:
    """Gemini/Imagen で画像を生成し bytes を返す。失敗時は None。"""
    if not _GENAI_AVAILABLE:
        raise ImportError("google-genai がインストールされていません")
    model = model_override or _IMAGE_MODEL
    client = _google_genai.Client(api_key=gemini_api_key)

    if "imagen" in model.lower():
        # Imagen系: generate_images API
        response = client.models.generate_images(
            model=model,
            prompt=prompt,
            config=_google_genai_types.GenerateImagesConfig(number_of_images=1),
        )
        if response.generated_images:
            return response.generated_images[0].image.image_bytes
        return None
    else:
        # Gemini系: generate_content + response_modalities
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=_google_genai_types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                return part.inline_data.data
        return None
