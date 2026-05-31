"""
OpenAI gpt-image-2 クライアント（seo-image-generatorから移植）
GeminiImageClient と同じインターフェースで動く。
"""

from __future__ import annotations

import base64
import io

from openai import OpenAI
from PIL import Image


OPENAI_IMAGE_MODEL = "gpt-image-2"

_ASPECT_RATIO_TO_SIZE: dict[str, str] = {
    "16:9": "1536x1024",
    "3:2":  "1536x1024",
    "4:3":  "1536x1024",
    "5:4":  "1536x1024",
    "21:9": "1536x1024",
    "1:1":  "1024x1024",
    "9:16": "1024x1536",
    "2:3":  "1024x1536",
    "3:4":  "1024x1536",
    "4:5":  "1024x1536",
}

_OPENAI_MAX_REF_DIMENSION = 1800


def _shrink_for_openai(img: Image.Image) -> Image.Image:
    w, h = img.size
    longest = max(w, h)
    if longest <= _OPENAI_MAX_REF_DIMENSION:
        return img
    scale = _OPENAI_MAX_REF_DIMENSION / float(longest)
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    resample = getattr(Image, "Resampling", Image).LANCZOS
    return img.resize(new_size, resample)


class OpenAIImageClient:
    """OpenAI gpt-image-2 クライアント。GeminiImageClient と同じインターフェースを提供。"""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("OPENAI_API_KEY が設定されていません")
        self.client = OpenAI(api_key=api_key)

    def generate_image(
        self,
        prompt: str,
        reference_images: list[Image.Image] | None = None,
        aspect_ratio: str = "16:9",
    ) -> tuple[Image.Image | None, str | None]:
        size = _ASPECT_RATIO_TO_SIZE.get(aspect_ratio, "1024x1024")

        if reference_images:
            image_files = []
            for i, ref_img in enumerate(reference_images):
                shrunk = _shrink_for_openai(ref_img)
                buf = io.BytesIO()
                shrunk.save(buf, format="PNG")
                buf.seek(0)
                buf.name = f"ref_{i}.png"
                image_files.append(buf)

            response = self.client.images.edit(
                model=OPENAI_IMAGE_MODEL,
                image=image_files if len(image_files) > 1 else image_files[0],
                prompt=prompt,
                size=size,
                n=1,
            )
        else:
            response = self.client.images.generate(
                model=OPENAI_IMAGE_MODEL,
                prompt=prompt,
                size=size,
                n=1,
            )

        if not response.data:
            return None, None

        item = response.data[0]
        b64 = getattr(item, "b64_json", None)
        if not b64:
            return None, getattr(item, "revised_prompt", None)

        generated_image = Image.open(io.BytesIO(base64.b64decode(b64)))
        return generated_image, getattr(item, "revised_prompt", None)

    def generate_image_bytes(
        self,
        prompt: str,
        reference_images: list[Image.Image] | None = None,
        aspect_ratio: str = "16:9",
        format: str = "PNG",
    ) -> tuple[bytes | None, str | None]:
        image, text = self.generate_image(
            prompt=prompt,
            reference_images=reference_images,
            aspect_ratio=aspect_ratio,
        )
        if image is None:
            return None, text
        buf = io.BytesIO()
        image.save(buf, format=format)
        return buf.getvalue(), text
