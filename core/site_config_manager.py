import json
import os
from typing import Any, Dict, List

from bs4 import BeautifulSoup

SITES_CONFIG_DIR = "config/sites"


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
            {"name": "大見出し", "pattern": "<h2>{{title}}</h2>", "active": True},
            {"name": "小見出し (h3)", "pattern": "<h3>{{title}}</h3>", "active": True},
            {"name": "テーブル", "pattern": '<table class="table">\n    {{content}}\n</table>', "active": True},
            {"name": "シンプルボックス", "pattern": '<div class="simple-box">\n    {{content}}\n</div>', "active": True},
            {"name": "番号付きリスト", "pattern": "<ol>\n    {{content}}\n</ol>", "active": True},
            {"name": "番号なしリスト", "pattern": "<ul>\n    {{content}}\n</ul>", "active": True},
            {"name": "CTA", "pattern": '<div class="c-btn">\n    {{link}}\n</div>', "active": True},
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
    """パーツ置き場HTMLから <h2 id="..."> ＋ <textarea> のペアを抽出してcomponentsリストを返す。"""
    soup = BeautifulSoup(html_content, "html.parser")
    components = []

    for h2 in soup.find_all("h2", id=True):
        h2_name = h2.get_text(strip=True)

        # h2 以降・次の h2 の前までの兄弟要素を収集
        block = []
        for sib in h2.next_siblings:
            if getattr(sib, "name", None) == "h2":
                break
            block.append(sib)

        # h3 と textarea の位置を記録
        h3_positions = [(i, s) for i, s in enumerate(block) if getattr(s, "name", None) == "h3"]
        ta_positions  = [(i, s) for i, s in enumerate(block) if getattr(s, "name", None) == "textarea"]

        if not ta_positions:
            continue

        if h3_positions:
            # h3 ごとに「h3 より後・次 h3 より前」の最初の textarea を対応付ける
            for hi, (h3_idx, h3_tag) in enumerate(h3_positions):
                next_h3_idx = h3_positions[hi + 1][0] if hi + 1 < len(h3_positions) else len(block)
                ta_in_range = [ta for ta_i, ta in ta_positions if h3_idx < ta_i < next_h3_idx]
                if not ta_in_range:
                    continue
                pattern = ta_in_range[0].get_text().strip()
                if not pattern:
                    continue
                h3_name = h3_tag.get_text(strip=True)
                components.append({
                    "name": f"{h2_name}（{h3_name}）",
                    "pattern": pattern,
                    "active": True,
                })
        else:
            # h3 なし — 説明文のみの textarea（「ソースを簡素化…」など）はスキップ
            for _, ta in ta_positions:
                pattern = ta.get_text().strip()
                if pattern and not pattern.startswith("ソースを簡素化"):
                    components.append({"name": h2_name, "pattern": pattern, "active": True})
                    break

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
