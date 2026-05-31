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
) -> str:
    """
    参照画像をGeminiで分析してデザイン特徴テキストを返す。
    結果は site_config["design_system"]["ref_image_analysis"] に保存する。
    """
    if not images or not gemini_api_key:
        return ""

    client = GeminiImageClient(api_key=gemini_api_key)
    ds = site_config.get("design_system", {})
    color_hints = ""
    if ds.get("primary_color"):
        color_hints = (
            f"\nサイトのメインカラーは {ds['primary_color']} です。"
            "参照画像のスタイルを分析する際にこの配色を基準として言及してください。"
        )

    prompt = (
        "添付された参照画像のビジュアルデザイン特徴を日本語で簡潔に記述してください。"
        "以下の観点を含めること：イラストのタッチ・線の太さ・塗りスタイル・"
        "人物の描き方・カードの形状・余白感・全体的な雰囲気。"
        "AI画像生成への指示として使えるよう、具体的かつ簡潔に200字以内でまとめること。"
        + color_hints
    )
    return client.analyze_with_images(prompt, images)


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
