"""
hyper-article-writer 画像生成モジュール（3層アーキテクチャ版）

Layer1: デザインシステム（サイト設定 design_system から生成）
Layer2: 画像案提案（記事本文 → Gemini が JSON で提案）
Layer3: 画像生成（Layer1 + Layer2 → Gemini/OpenAI に投げる）

参照画像はGoogle Driveに保存し、サイト選択時に1回DLしてst.session_stateにキャッシュする。
"""

from __future__ import annotations

import json
from typing import Optional

from PIL import Image

from core.img_prompt_templates import (
    render_design_system,
    render_proposal_prompt,
    render_generation_prompt,
)
from core.img_gemini_client import GeminiImageClient
from core.img_openai_client import OpenAIImageClient


# ── Layer1: デザインシステム ──────────────────────────────────

def build_design_system(site_config: dict) -> str:
    """サイト設定からLayer1プロンプトを生成"""
    return render_design_system(site_config)


# ── Layer2: 画像案提案 ────────────────────────────────────────

def generate_image_proposals(
    article_text: str,
    site_config: dict,
    gemini_api_key: str,
) -> list[dict]:
    """
    記事本文を分析して画像案JSONを生成する。
    Returns: proposal dict のリスト（3〜5件）
    """
    if not gemini_api_key:
        return []

    prompt = render_proposal_prompt(article_text, site_config)
    client = GeminiImageClient(api_key=gemini_api_key)
    raw = client.analyze_text(prompt)

    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    try:
        proposals = json.loads(raw)
        return proposals if isinstance(proposals, list) else []
    except Exception:
        return []


# ── Layer3: 生成プロンプト組み立て + 画像生成 ──────────────────

def build_generation_prompt(
    design_system: str,
    proposal: dict,
    aspect_ratio: str = "16:9",
    has_reference_images: bool = False,
) -> str:
    """Layer1 + Layer2 から最終生成プロンプトを組み立てる"""
    return render_generation_prompt(
        design_system=design_system,
        proposal=proposal,
        aspect_ratio=aspect_ratio,
        has_reference_images=has_reference_images,
    )


def generate_image_bytes(
    prompt: str,
    reference_images: list[Image.Image] | None = None,
    provider: str = "gemini",
    gemini_api_key: str = "",
    openai_api_key: str = "",
    aspect_ratio: str = "16:9",
) -> Optional[bytes]:
    """
    指定プロバイダで画像を生成してbytesを返す。
    reference_images: st.session_stateから渡すPIL Imageのリスト
    """
    if provider == "dalle" and openai_api_key:
        client = OpenAIImageClient(api_key=openai_api_key)
    elif gemini_api_key:
        client = GeminiImageClient(api_key=gemini_api_key)
    else:
        return None

    img_bytes, _ = client.generate_image_bytes(
        prompt=prompt,
        reference_images=reference_images or None,
        aspect_ratio=aspect_ratio,
    )
    return img_bytes


# ── 参照画像：Driveとのやり取り ──────────────────────────────

def load_reference_images_from_drive(
    site_id: str,
    credentials_dict: dict,
    parent_folder_id: str,
) -> list[Image.Image]:
    """
    Google DriveからサイトIDの参照画像をDLしてPIL Imageのリストで返す。
    サイト選択時に1回だけ呼び出し、st.session_stateにキャッシュする。
    """
    from core.drive_uploader import download_reference_images
    return download_reference_images(site_id, credentials_dict, parent_folder_id)


def upload_reference_image_to_drive(
    image_bytes: bytes,
    filename: str,
    site_id: str,
    credentials_dict: dict,
    parent_folder_id: str,
) -> str:
    """参照画像をGoogle Driveにアップロード"""
    from core.drive_uploader import upload_reference_image
    return upload_reference_image(image_bytes, filename, site_id, credentials_dict, parent_folder_id)


def list_reference_images_in_drive(
    site_id: str,
    credentials_dict: dict,
    parent_folder_id: str,
) -> list[dict]:
    """DriveのサイトID参照画像一覧を返す（id・name）"""
    from core.drive_uploader import list_reference_images
    return list_reference_images(site_id, credentials_dict, parent_folder_id)


def delete_reference_image_from_drive(
    file_id: str,
    credentials_dict: dict,
) -> bool:
    """DriveのサイトID参照画像を削除"""
    from core.drive_uploader import delete_reference_image
    return delete_reference_image(file_id, credentials_dict)


def analyze_reference_images(
    images: list[Image.Image],
    site_config: dict,
    gemini_api_key: str,
) -> dict:
    """
    参照画像をGeminiで分析してデザインシステム全フィールドをdictで返す。
    呼び出し側でそのまま design_system にマージして保存する。

    Returns: {
        "illustration_style": str,
        "line_weight": str,
        "character_style": str,
        "fill_style": str,
        "card_style": str,
        "spacing": str,
        "prohibited_elements": str,
        "additional_notes": str,
        "ref_image_analysis": str,
    }
    """
    if not images or not gemini_api_key:
        return {}

    client = GeminiImageClient(api_key=gemini_api_key)
    ds = site_config.get("design_system", {})
    color_hint = ""
    if ds.get("primary_color"):
        color_hint = f"\nサイトのメインカラーは {ds['primary_color']} です。色の記述ではこれを基準にしてください。"

    prompt = (
        "添付された参照画像を分析して、以下のJSON形式で出力してください。"
        "AI画像生成プロンプトとして直接使える日本語・具体的な記述にすること。"
        "JSONのみ出力（コードブロック・説明文不要）。" + color_hint + """

{
  "illustration_style": "イラストの様式（例: フラットデザイン・手描き風・アイコン系など）",
  "line_weight": "線の太さ・質感（例: 均一な細線2px・太めのアウトライン・線なしなど）",
  "character_style": "人物の描き方（例: 4頭身・記号的表現・写実的・なしなど）",
  "fill_style": "塗りスタイル（例: フラット塗り・グラデーションあり・影ありなど）",
  "card_style": "カード・ボックスの形状（例: 白背景+角丸8px・枠線あり・影ありなど）",
  "spacing": "余白感（例: 広めに均等・コンパクトなど）",
  "prohibited_elements": "このスタイルに合わない要素（例: 3D・手書き・写真・過剰な装飾など）",
  "additional_notes": "その他の特徴（例: 全体的にクリーン・カラフルな配色・医療系の清潔感など）",
  "ref_image_analysis": "上記をまとめた総合スタイル説明（200字以内・画像生成プロンプトとして使う）"
}"""
    )

    raw = client.analyze_with_images(prompt, images)

    # JSONを抽出
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    try:
        result = json.loads(raw)
        return {k: str(v) for k, v in result.items()}
    except Exception:
        # パース失敗時はref_image_analysisだけ返す
        return {"ref_image_analysis": raw[:500]}


# ── 一括生成ヘルパー（記事作成フローから呼ぶ） ───────────────

def generate_images_for_article(
    article_text: str,
    site_config: dict,
    reference_images: list[Image.Image],
    provider: str,
    gemini_api_key: str,
    openai_api_key: str,
    aspect_ratio: str = "16:9",
) -> list[dict]:
    """
    記事本文からすべての画像を生成して返す。
    Returns: [{"proposal": dict, "bytes": bytes | None}, ...]
    """
    proposals = generate_image_proposals(article_text, site_config, gemini_api_key)
    if not proposals:
        return []

    design_system = build_design_system(site_config)
    has_ref = bool(reference_images)
    results = []

    for proposal in proposals:
        use_aspect = proposal.get("recommended_aspect_ratio", aspect_ratio)
        prompt = build_generation_prompt(design_system, proposal, use_aspect, has_ref)
        img_bytes = generate_image_bytes(
            prompt=prompt,
            reference_images=reference_images if has_ref else None,
            provider=provider,
            gemini_api_key=gemini_api_key,
            openai_api_key=openai_api_key,
            aspect_ratio=use_aspect,
        )
        results.append({"proposal": proposal, "bytes": img_bytes})

    return results
