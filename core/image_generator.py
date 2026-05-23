import base64
import io as _io_module
import json
import os as _os_module
import re
import tempfile as _tempfile_module
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

try:
    from PIL import Image as _PIL_Image, ImageDraw as _PIL_Draw, ImageFont as _PIL_Font
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

_IMAGE_MODEL = "gemini-2.0-flash-preview-image-generation"   # backward compat alias
_IMAGE_MODEL_GEMINI = "gemini-2.0-flash-preview-image-generation"
_IMAGE_MODEL_GEMINI_PRESETS = [
    "gemini-2.0-flash-preview-image-generation",
    "gemini-2.0-flash-exp-image-generation",
    "gemini-2.0-flash-exp",
    "gemini-2.0-flash",
]

# API から自動検出した結果をキャッシュ（再起動まで保持）
_detected_image_models: list = []
_IMAGE_MODEL_DALLE  = "dall-e-3"

# ── PIL ヘルパー ────────────────────────────────────────────────
_FONT_CACHE: dict = {}


def _find_or_download_font(bold: bool = True) -> Optional[str]:
    """日本語対応フォントパスを返す。なければダウンロードしてキャッシュする。"""
    weight = "Bold" if bold else "Regular"
    candidates = [
        f"/usr/share/fonts/opentype/noto/NotoSansCJKjp-{weight}.otf",
        f"/usr/share/fonts/opentype/noto/NotoSansCJK-{weight}.ttc",
        f"/usr/share/fonts/truetype/noto/NotoSansCJKjp-{weight}.otf",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
        "C:\\Windows\\Fonts\\meiryo.ttc",
        "C:\\Windows\\Fonts\\msgothic.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    ]
    for p in candidates:
        if _os_module.path.exists(p):
            return p
    cache_dir = _os_module.path.join(_tempfile_module.gettempdir(), "cv_article_fonts")
    _os_module.makedirs(cache_dir, exist_ok=True)
    local = _os_module.path.join(cache_dir, f"NotoSansJP-{weight}.otf")
    if not _os_module.path.exists(local):
        url = f"https://github.com/notofonts/noto-cjk/raw/main/Sans/SubsetOTF/JP/NotoSansJP-{weight}.otf"
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            with open(local, "wb") as fh:
                fh.write(resp.content)
        except Exception:
            return None
    return local if _os_module.path.exists(local) else None


def _get_pil_font(size: int, bold: bool = True):
    """PIL ImageFont を返す（キャッシュ付き）。フォント取得失敗時は load_default。"""
    if not _PIL_AVAILABLE:
        return None
    key = (size, bold)
    if key not in _FONT_CACHE:
        path = _find_or_download_font(bold)
        try:
            _FONT_CACHE[key] = _PIL_Font.truetype(path, size) if path else _PIL_Font.load_default()
        except Exception:
            _FONT_CACHE[key] = _PIL_Font.load_default()
    return _FONT_CACHE[key]


def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _draw_rounded_rect(draw, xy: tuple, radius: int, fill: tuple) -> None:
    x0, y0, x1, y1 = xy
    r = min(radius, (x1 - x0) // 2, (y1 - y0) // 2)
    draw.rectangle([x0 + r, y0, x1 - r, y1], fill=fill)
    draw.rectangle([x0, y0 + r, x1, y1 - r], fill=fill)
    draw.ellipse([x0, y0, x0 + 2 * r, y0 + 2 * r], fill=fill)
    draw.ellipse([x1 - 2 * r, y0, x1, y0 + 2 * r], fill=fill)
    draw.ellipse([x0, y1 - 2 * r, x0 + 2 * r, y1], fill=fill)
    draw.ellipse([x1 - 2 * r, y1 - 2 * r, x1, y1], fill=fill)


def _draw_text_block(draw, text: str, font, x_center: int, y: int, color: tuple, max_width: int, line_spacing: float = 1.4) -> int:
    """テキストを折り返して中央揃えで描画。描画後の y 座標を返す。"""
    if not text or not font:
        return y
    lines: list[str] = []
    current = ""
    for ch in str(text):
        test = current + ch
        try:
            w = font.getlength(test)
        except Exception:
            bbox = font.getbbox(test)
            w = bbox[2] - bbox[0]
        if w > max_width and current:
            lines.append(current)
            current = ch
        else:
            current = test
    if current:
        lines.append(current)
    try:
        _, _, _, line_h = font.getbbox("あ")
    except Exception:
        line_h = 20
    for line in lines:
        try:
            lw = font.getlength(line)
        except Exception:
            bbox = font.getbbox(line)
            lw = bbox[2] - bbox[0]
        draw.text((x_center - int(lw) // 2, y), line, font=font, fill=color)
        y += int(line_h * line_spacing)
    return y


def _parse_template_to_layout(prompt: str, claude_api_key: str) -> dict:
    """テンプレートプロンプトをPILレンダリング用JSONに変換する。"""
    parse_prompt = f"""以下の画像テンプレートプロンプトを解析し、PILでレンダリングするためのJSONのみを出力してください。説明文・コードフェンス不要。

## テンプレートプロンプト
{prompt}

## 出力JSON形式
{{
  "width": 800,
  "bg_color": "#F1F4FF",
  "layout": "3col_cards",
  "title": {{
    "text": "タイトルテキスト（実際の内容）",
    "color": "#49589B",
    "font_size": 32,
    "bg_color": "#FFFFFF"
  }},
  "items": [
    {{
      "header": "見出し（ある場合のみ）",
      "body": "カード本文テキスト（実際の内容）",
      "illustration_prompt": "flat illustration of ... (English, 10 words max, no text in image)",
      "card_bg_color": "#FFFFFF",
      "header_bg_color": "#FFE9E3",
      "header_color": "#49589B",
      "body_color": "#49589B"
    }}
  ]
}}

layoutは以下のいずれか：
- "3col_cards"（横並び3カード）
- "vertical_list_3"（縦積み3項目）
- "comparison_table"（比較表）
- "generic"（その他）

itemsはプロンプト内のカード・行・項目ごとに1エントリ。テキストは実際の内容（変数名ではなく）を入れること。"""

    client = anthropic.Anthropic(api_key=claude_api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": parse_prompt}],
    )
    text = msg.content[0].text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    return json.loads(text)


def _gen_illust_small(illust_prompt: str, gemini_api_key: str) -> Optional[bytes]:
    """Gemini でイラスト小画像を生成して返す。失敗時は None。"""
    if not gemini_api_key or not _GENAI_AVAILABLE:
        return None
    clean = (
        "Simple flat design illustration, white background, absolutely no text no letters no numbers no symbols. "
        f"{illust_prompt} "
        "Square format, clean minimal icon style, dark blue #49589B as main color."
    )
    try:
        return _generate_image_bytes_gemini(clean, gemini_api_key)
    except Exception:
        return None


_3COL_CARD_IPAD = 14      # カード内上下左右の余白
_3COL_TEXT_H = 100        # テキストエリアの高さ予算（24px × 1.4spacing × 3行）
_3COL_ILLUST_SIZE = 90    # イラスト正方形サイズ
_3COL_TEXT_ILLUST_GAP = 12  # テキスト下端〜イラスト上端の隙間


def _render_3col_cards(img, draw, items: list, y0: int, W: int, H: int, PAD: int, R: int, gemini_api_key: str) -> None:
    n = max(len(items), 1)
    GAP = 10
    card_w = (W - PAD * 2 - GAP * (n - 1)) // n
    card_h = H - y0 - PAD

    # テキスト・イラストの y 座標は固定（card_h に依存しない）
    text_y = y0 + _3COL_CARD_IPAD
    illust_y = y0 + _3COL_CARD_IPAD + _3COL_TEXT_H + _3COL_TEXT_ILLUST_GAP

    for i, item in enumerate(items[:n]):
        x0 = PAD + i * (card_w + GAP)
        x1 = x0 + card_w
        card_bg = _hex_to_rgb(item.get("card_bg_color", "#FFFFFF"))
        body_col = _hex_to_rgb(item.get("body_color", "#49589B"))

        _draw_rounded_rect(draw, (x0, y0, x1, y0 + card_h), R, card_bg)

        body = str(item.get("body", ""))
        b_font = _get_pil_font(24, bold=True)
        if body and b_font:
            _draw_text_block(draw, body, b_font, (x0 + x1) // 2, text_y, body_col, card_w - _3COL_CARD_IPAD * 2)

        if gemini_api_key and item.get("illustration_prompt"):
            illust_bytes = _gen_illust_small(item["illustration_prompt"], gemini_api_key)
            if illust_bytes:
                try:
                    illust_img = _PIL_Image.open(_io_module.BytesIO(illust_bytes)).convert("RGB")
                    illust_img = illust_img.resize((_3COL_ILLUST_SIZE, _3COL_ILLUST_SIZE), _PIL_Image.LANCZOS)
                    paste_x = (x0 + x1) // 2 - _3COL_ILLUST_SIZE // 2
                    img.paste(illust_img, (paste_x, illust_y))
                except Exception:
                    pass


def _render_vertical_list(img, draw, items: list, y0: int, W: int, H: int, PAD: int, R: int, gemini_api_key: str) -> None:
    n = max(len(items), 1)
    GAP = 10
    item_h = (H - y0 - PAD - GAP * (n - 1)) // n
    illust_w = min(70, item_h - 16)
    header_w = (W - PAD * 2) // 3

    for i, item in enumerate(items[:n]):
        iy = y0 + i * (item_h + GAP)
        ix0, ix1 = PAD, W - PAD
        card_bg = _hex_to_rgb(item.get("card_bg_color", "#FFFFFF"))
        hdr_bg = _hex_to_rgb(item.get("header_bg_color", "#FFE9E3"))
        hdr_col = _hex_to_rgb(item.get("header_color", "#49589B"))
        body_col = _hex_to_rgb(item.get("body_color", "#333333"))

        _draw_rounded_rect(draw, (ix0, iy, ix1, iy + item_h), R, card_bg)
        _draw_rounded_rect(draw, (ix0, iy, ix0 + header_w, iy + item_h), R, hdr_bg)

        header = str(item.get("header") or item.get("body", ""))
        h_font = _get_pil_font(22, bold=True)
        if header and h_font:
            _draw_text_block(draw, header, h_font, ix0 + header_w // 2, iy + 10, hdr_col, header_w - 12)

        body = str(item.get("body", ""))
        b_font = _get_pil_font(20, bold=False)
        if body and b_font:
            body_x_start = ix0 + header_w + illust_w + 12
            _draw_text_block(draw, body, b_font, (body_x_start + ix1) // 2, iy + 10, body_col, ix1 - body_x_start - 8)

        if gemini_api_key and item.get("illustration_prompt"):
            illust_bytes = _gen_illust_small(item["illustration_prompt"], gemini_api_key)
            if illust_bytes:
                try:
                    illust_img = _PIL_Image.open(_io_module.BytesIO(illust_bytes)).convert("RGB")
                    illust_img = illust_img.resize((illust_w, illust_w), _PIL_Image.LANCZOS)
                    img.paste(illust_img, (ix0 + header_w + 6, iy + (item_h - illust_w) // 2))
                except Exception:
                    pass


def _render_generic_cards(img, draw, items: list, y0: int, W: int, H: int, PAD: int, R: int, gemini_api_key: str) -> None:
    n = max(len(items), 1)
    GAP = 10
    item_h = min(90, (H - y0 - PAD - GAP * (n - 1)) // n)

    for i, item in enumerate(items[:n]):
        iy = y0 + i * (item_h + GAP)
        card_bg = _hex_to_rgb(item.get("card_bg_color", "#FFFFFF"))
        body_col = _hex_to_rgb(item.get("body_color", "#49589B"))
        _draw_rounded_rect(draw, (PAD, iy, W - PAD, iy + item_h), R, card_bg)
        body = str(item.get("body", ""))
        font = _get_pil_font(24, bold=True)
        if body and font:
            _draw_text_block(draw, body, font, W // 2, iy + 10, body_col, W - PAD * 4)


def generate_image_pil(prompt: str, claude_api_key: str, gemini_api_key: str = "") -> Optional[bytes]:
    """PIL でテキストをレンダリングし、Gemini でイラスト部分のみ生成するハイブリッド方式。"""
    if not _PIL_AVAILABLE:
        return None

    layout = _parse_template_to_layout(prompt, claude_api_key)

    W = int(layout.get("width", 800))
    bg_rgb = _hex_to_rgb(layout.get("bg_color", "#F1F4FF"))
    layout_type = layout.get("layout", "generic")
    items = layout.get("items", [])
    n = len(items)

    PAD, R = 16, 8
    TITLE_H = 54
    content_y = PAD + TITLE_H + 14  # 84px

    if layout_type == "3col_cards":
        _card_h = _3COL_CARD_IPAD + _3COL_TEXT_H + _3COL_TEXT_ILLUST_GAP + _3COL_ILLUST_SIZE + _3COL_CARD_IPAD
        H = content_y + _card_h + PAD  # 84 + 230 + 16 = 330
    elif layout_type == "vertical_list_3":
        H = max(460, content_y + n * 150 + PAD)
    else:
        H = max(400, content_y + n * 100 + PAD)

    img = _PIL_Image.new("RGB", (W, H), bg_rgb)
    draw = _PIL_Draw.Draw(img)

    # タイトルバー
    t = layout.get("title", {})
    t_text = str(t.get("text", ""))
    t_color = _hex_to_rgb(t.get("color", "#49589B"))
    t_bg = _hex_to_rgb(t.get("bg_color", "#FFFFFF"))
    t_font = _get_pil_font(min(int(t.get("font_size", 30)), 36), bold=True)
    _draw_rounded_rect(draw, (PAD, PAD, W - PAD, PAD + TITLE_H), 10, t_bg)
    if t_text and t_font:
        _draw_text_block(draw, t_text, t_font, W // 2, PAD + 10, t_color, W - PAD * 4)

    if layout_type == "3col_cards":
        _render_3col_cards(img, draw, items, content_y, W, H, PAD, R, gemini_api_key)
    elif layout_type == "vertical_list_3":
        _render_vertical_list(img, draw, items, content_y, W, H, PAD, R, gemini_api_key)
    else:
        _render_generic_cards(img, draw, items, content_y, W, H, PAD, R, gemini_api_key)

    buf = _io_module.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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


# ── 利用可能な画像生成モデルを API から自動検出 ──────────────
def _auto_detect_image_models(client) -> list:
    """models.list() で画像生成対応モデルを検出して返す。検出失敗時は空リスト。"""
    try:
        found = []
        for m in client.models.list():
            name = str(getattr(m, "name", "")).replace("models/", "")
            methods = [str(x) for x in getattr(m, "supported_generation_methods", [])]
            is_image = (
                "image" in name.lower()
                or "generateImages" in methods
                or any("image" in x.lower() for x in methods)
            )
            if is_image:
                found.append(name)
        # 画像生成専用モデルを先頭に
        found.sort(key=lambda n: (0 if "image" in n.lower() else 1, n))
        return found if found else _IMAGE_MODEL_GEMINI_PRESETS
    except Exception:
        return _IMAGE_MODEL_GEMINI_PRESETS


# ── 画像生成（Gemini）────────────────────────────────────────
def _generate_image_bytes_gemini(
    prompt: str,
    gemini_api_key: str,
    model_override: Optional[str] = None,
) -> Optional[bytes]:
    if not _GENAI_AVAILABLE:
        raise ImportError("google-genai がインストールされていません")
    global _detected_image_models
    client = _google_genai.Client(api_key=gemini_api_key)

    if model_override:
        models_to_try = [model_override]
    else:
        # キャッシュがなければ API から利用可能モデルを自動検出
        if not _detected_image_models:
            _detected_image_models = _auto_detect_image_models(client)
        models_to_try = _detected_image_models or _IMAGE_MODEL_GEMINI_PRESETS

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
                last_err = ValueError(f"{try_model}: 画像が生成されませんでした")
                continue
            else:
                response = client.models.generate_content(
                    model=try_model,
                    contents=prompt,
                    config=_google_genai_types.GenerateContentConfig(
                        response_modalities=["TEXT", "IMAGE"]
                    ),
                )
                for part in response.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        return part.inline_data.data
                last_err = ValueError(f"{try_model}: レスポンスに画像が含まれていませんでした")
                continue
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
    claude_api_key: str = "",
) -> Optional[bytes]:
    """指定プロバイダーで画像を生成して bytes を返す。
    claude_api_key が渡された場合は PIL ハイブリッド方式を優先する（日本語文字化け対策）。
    """
    if claude_api_key and _PIL_AVAILABLE:
        try:
            result = generate_image_pil(prompt, claude_api_key, gemini_api_key)
            if result:
                return result
        except Exception:
            pass  # PIL 失敗時は Gemini/DALL-E にフォールバック
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
