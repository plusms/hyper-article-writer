import json
import os
from typing import Any, Dict, List

from bs4 import BeautifulSoup

SITES_CONFIG_DIR = "config/sites"

# ── 固定23スロット ──────────────────────────────────────────────
FIXED_COMPONENT_SCHEMA: List[str] = [
    "H2",
    "H3",
    "小見出し",
    "箇条書き（リスト）",
    "箇条書き（チェックリスト）",
    "箇条書き（数字）",
    "ボックス①（枠のみ、背景色なし）",
    "ボックス②（背景色あり）",
    "補足ボックス",
    "まとめボックス",
    "右寄せリンク",
    "ナンバリングパーツ",
    "ステップパーツ・フローパーツ",
    "画像",
    "メリット・デメリット",
    "口コミ",
    "マーカー",
    "太文字",
    "小文字",
    "テーブル",
    "スクロールテーブル",
    "タブ切り替えテーブル",
    "CTAボタン",
]

# スロット → (name内の必須KW, name内の除外KW, textarea内から抜き出すタグ/クラス名)
_SLOT_RULES: Dict[str, tuple] = {
    "H2":                           (["大見出し"], [],             "h2"),
    "H3":                           (["大見出し"], [],             "h3"),
    "小見出し":                      (["大見出し"], [],             "subhead"),
    "箇条書き（リスト）":             (["番号なし"], [],             None),
    "箇条書き（チェックリスト）":      (["チェック"], [],             None),
    "箇条書き（数字）":               (["番号付き"], [],             None),
    "ボックス①（枠のみ、背景色なし）": (["ボーダー"], [],             None),
    "ボックス②（背景色あり）":        (["背景色あり"], [],           None),
    "補足ボックス":                   (["補足"],    [],             None),
    "まとめボックス":                 (["まとめ"],  ["調査"],        None),
    "右寄せリンク":                   (["右寄せ"],  [],             None),
    "ナンバリングパーツ":             (["ナンバリング"], [],          None),
    "ステップパーツ・フローパーツ":    (["フロー"],  [],             None),
    "画像":                           (["コンテンツ幅"], [],         None),
    "メリット・デメリット":           (["メリット"], [],             None),
    "口コミ":                         (["口コミ"],  [],             None),
    "マーカー":                       (["文字装飾"], [],            "marker"),
    "太文字":                         (["文字装飾"], [],            "bold"),
    "小文字":                         (["文字装飾"], [],            "text-small"),
    "テーブル":                       (["テーブル"], ["スクロール", "タブ"], None),
    "スクロールテーブル":             (["スクロール", "通常"], [],   None),
    "タブ切り替えテーブル":           (["タブ切り替え"], [],         None),
    "CTAボタン":                     (["CTA"],     [],             None),
}


def _extract_element(pattern: str, selector: str) -> str:
    """textareaの内容から特定タグ or CSSクラスの要素を抽出して返す。"""
    s = BeautifulSoup(pattern, "html.parser")
    if selector in ("h2", "h3", "h4"):
        el = s.find(selector)
    else:
        el = s.find(class_=selector)
    return str(el).strip() if el else pattern


def get_default_site_config() -> Dict[str, Any]:
    return {
        "design_rules": {
            "tone": "",
            "colors": {
                "main": "#47c1d3",
                "accent_red": "#fe766b",
                "accent_yellow": "#ffd711",
                "accent_orange": "#fd9b23",
                "bg_white": "#FFFFFF",
                "bg_gray": "#eeeeee",
                "text": "#333333",
            },
        },
        "components": [
            {"name": slot, "pattern": "", "active": True}
            for slot in FIXED_COMPONENT_SCHEMA
        ],
        "clinic_block_templates": [],
    }


def list_sites() -> List[str]:
    if not os.path.exists(SITES_CONFIG_DIR):
        return []
    return sorted([f[:-5] for f in os.listdir(SITES_CONFIG_DIR) if f.endswith(".json")])


def load_site_config(site_name: str) -> Dict[str, Any]:
    path = os.path.join(SITES_CONFIG_DIR, f"{site_name}.json")
    if not os.path.exists(path):
        return get_default_site_config()
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
        default = get_default_site_config()
        for key, val in default.items():
            if key not in config:
                config[key] = val
            elif isinstance(val, dict):
                for sub_key, sub_val in val.items():
                    if sub_key not in config[key]:
                        config[key][sub_key] = sub_val
        return config
    except Exception as e:
        print(f"Error loading site config for {site_name}: {e}")
        return get_default_site_config()


def save_site_config(site_name: str, config: Dict[str, Any]) -> bool:
    try:
        os.makedirs(SITES_CONFIG_DIR, exist_ok=True)
        path = os.path.join(SITES_CONFIG_DIR, f"{site_name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"Error saving site config for {site_name}: {e}")
        return False


def delete_site_config(site_name: str) -> bool:
    path = os.path.join(SITES_CONFIG_DIR, f"{site_name}.json")
    if os.path.exists(path):
        os.remove(path)
    return True


def parse_parts_page(html_content: str) -> List[Dict[str, Any]]:
    """パーツ置き場HTMLから固定23スロットにマッピングしてcomponentsリストを返す。
    マッチするパターンが見つからないスロットは pattern="" で登録する。
    """
    soup = BeautifulSoup(html_content, "html.parser")

    # Step1: HTML全体から (表示名, textareaテキスト) ペアを収集
    raw: List[tuple] = []
    for h2 in soup.find_all("h2", id=True):
        h2_name = h2.get_text(strip=True)
        block = []
        for sib in h2.next_siblings:
            if getattr(sib, "name", None) == "h2":
                break
            block.append(sib)

        h3_positions = [(i, s) for i, s in enumerate(block) if getattr(s, "name", None) == "h3"]
        ta_positions  = [(i, s) for i, s in enumerate(block) if getattr(s, "name", None) == "textarea"]

        if not ta_positions:
            continue

        if h3_positions:
            for hi, (h3_idx, h3_tag) in enumerate(h3_positions):
                next_h3_idx = h3_positions[hi + 1][0] if hi + 1 < len(h3_positions) else len(block)
                ta_in_range = [ta for ta_i, ta in ta_positions if h3_idx < ta_i < next_h3_idx]
                if ta_in_range:
                    pat = ta_in_range[0].get_text().strip()
                    if pat:
                        raw.append((f"{h2_name}（{h3_tag.get_text(strip=True)}）", pat))
        else:
            for _, ta in ta_positions:
                pat = ta.get_text().strip()
                if pat and not pat.startswith("ソースを簡素化"):
                    raw.append((h2_name, pat))
                    break

    # Step2: 固定スロットごとにベストマッチを探す
    components = []
    for slot in FIXED_COMPONENT_SCHEMA:
        pattern = ""
        if slot in _SLOT_RULES:
            required, excluded, extract_sel = _SLOT_RULES[slot]
            for name, pat in raw:
                if not all(kw in name for kw in required):
                    continue
                if any(kw in name for kw in excluded):
                    continue
                pattern = _extract_element(pat, extract_sel) if extract_sel else pat
                break
        components.append({"name": slot, "pattern": pattern, "active": True})

    return components


def format_site_parts(components: List[Dict[str, Any]]) -> str:
    active = [c for c in components if c.get("active", True)]
    if not active:
        return ""
    lines = [
        "【サイト別HTMLパーツ一覧】",
        "記事本文ではこれらのパーツを使用してください。各パーツの {{変数名}} は実際の内容に置き換えてください。\n",
    ]
    for c in active:
        lines.append(f"■ {c.get('name', '')}\n{c['pattern']}")
    return "\n\n".join(lines)
