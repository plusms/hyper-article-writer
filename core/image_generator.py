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
