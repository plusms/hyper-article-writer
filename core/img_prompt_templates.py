"""
3層プロンプトテンプレート（seo-image-generatorから移植）
Layer1: デザインシステム（サイト固有のスタイル定義）
Layer2: 画像案提案（記事分析 → 3〜5案のJSON出力）
Layer3: 画像生成（Layer1 + 構成説明を結合）
"""

from __future__ import annotations

DESIGN_SYSTEM_TEMPLATE = """あなたはプロのUIデザイナーです。
以下のデザインシステムを厳密に適用して画像を生成してください。

== ブランド ==
言語: {language}
※サイト名・ブランド名（{brand_name}）を画像内に表示してはならない。画像内テキストは読者にとって有益な情報のみにすること。

== 配色パレット ==
- 背景色: {background_color}
- メイン色: {primary_color}
- サブ色: {secondary_color}
- アクセント色（強調）: {accent_color}
- テキスト色: {text_color}
- 警告・重要色: {danger_color}
※上記以外の色の使用は禁止

== イラストレーション・タッチ ==
- スタイル: {illustration_style}
- 線の太さ・質感: {line_weight}
- 人物造形: {character_style}
- 塗り: {fill_style}
- 背景描写: 人物の背景（部屋、家具、床の線）は一切描かない

== UI・レイアウト構造 ==
- カード: {card_style}
- フォント: {font_family}相当の、癖のないモダンゴシック体。細字・丸文字は禁止
- 余白: {spacing}
- ブロック構造: 「小見出し帯 → イラスト → 説明文」の縦積みを基本とする

== 禁止事項 ==
{prohibited_elements}

== 追加スタイルノート ==
{additional_notes}

== 参照画像から抽出したデザイン特徴（最重要・厳守） ==
{ref_image_analysis}
"""

IMAGE_PROPOSAL_TEMPLATE = """あなたはSEO記事の画像設計ディレクターです。
あなたの仕事は「記事の構造整理」ではなく「読者の体験設計」です。

各H2セクションに来た読者が「今、何を不安に思っているか」「何がわかれば安心するか」を考え、
その読者の気持ちに寄り添う画像案を3〜5個設計してください。

== 記事本文 ==
{article_text}

== 最重要原則：読者ファーストの画像設計 ==
1. **読者の気持ちを想像する**: このH2に来た読者は今何を知りたい？何が不安？
2. **何を見せたら解決するか考える**: 比較表？具体的なモノの画像？ステップ？数字？
3. **記事の主題を視覚的に表現する**: 読者が「この記事は自分に関係ある」と一瞬で感じるビジュアルにする
4. **最後に構図を決める**: 内容が決まってから、それを最も伝えやすい構図を選ぶ

== 画像案数の決定ルール ==
- 入口H2（導入・全体像）：必ず1つ
- 実務ブロックH2（具体手順・比較・選び方）：必ず1つ
- ケース系H2（例外・応用・パターン）：必要なら1つ
- 合計3〜5案（5案を超えない）

== 構図の選び方 ==
- 分類型（横3 or 横4 or 2×2）: 項目を並列で見せたい時
- 比較型（横並び2〜3列）: A vs B、ビフォーアフター
- フロー型（横ステップ）: 手順・流れ・プロセスを見せる時のみ
- ピラミッド型: 重要度の階層がある時のみ

== 1画像あたりの情報量（厳守・文字化け防止） ==
- 見出し: 最大8文字以内
- 説明文: 最大20文字×2行まで
- 画像全体で合計100文字以内を目安とする

== トンマナ ==
- ブランドトーン: {brand_tone}
- 画像サイズ: {image_width}×{image_height}px の画角で潰れない情報量を維持

== 出力形式（JSON配列で必ず出力） ==
```json
[
  {{
    "placement": "H2: [見出しテキスト]",
    "reader_mindset": "このH2に来た読者が今思っていること・知りたいこと",
    "purpose": "この画像で読者の何を解決するか",
    "conclusion": "画像を見た読者が得る結論（1文）",
    "layout_type": "分類型|比較型|フロー型|ピラミッド型",
    "layout_reason": "読者にとってこの構図がベストな理由",
    "blocks": [
      {{"heading": "見出し", "description": "説明文", "illustration": "描くべき具体的なイラスト内容"}}
    ],
    "recommended_aspect_ratio": "16:9|4:3|3:4|9:16",
    "composition_description": "空間配置の説明のみ（色・スタイル・雰囲気は書かない）"
  }}
]
```
"""

IMAGE_GENERATION_TEMPLATE = """{design_system_prompt}

== 画像生成リクエスト ==
以下の内容で{layout_type}のインフォグラフィック画像を作成してください。

【読者の状況】{reader_mindset}
【この画像の役割】{purpose}
【読者が得る結論】{conclusion}

== コンテンツブロック ==
{blocks_text}

== 構成イメージ ==
{composition_description}

== イラスト指示 ==
- 各ブロックには記事の主題に関連する具体的なイラストを必ず描くこと
- 抽象的なアイコンではなく、読者が「あ、これのことか」と直感でわかる具体的なモノ・人・場面を描く

== テキスト描画ルール（厳守・文字化け防止） ==
- 画像内テキストは最小限にすること
- 各見出しは8文字以内、説明文は20文字×2行以内
- 画像全体で合計100文字以内
- 文字サイズは十分に大きく、判読可能なサイズで配置
- 画像内のテキストはすべてJapanese（日本語）で記述
- サイト名・ブランド名は画像内に表示しない

== 技術要件 ==
- アスペクト比: {aspect_ratio}
- デザインシステムの配色を厳守
- 視覚的階層: タイトル > メインコンテンツ > 補足情報
"""

IMAGE_GENERATION_WITH_REF_TEMPLATE = """添付の参照画像と同じビジュアルスタイルで、{layout_type}のインフォグラフィック画像を作成してください。
スタイル（色・線・塗り・人物タッチ・カード形状・余白）はすべて参照画像を模倣すること。

【この画像の目的】{purpose}
【読者が得る結論】{conclusion}

{blocks_text}

【構成】{composition_description}

- 各ブロックのイラストは大きく、具体的に描く（アイコンではなく人物・モノの場面描写）
- テキストは日本語で記述。画像内テキストは合計100文字以内
- サイト名・ブランド名は画像内に入れない
"""


def render_design_system(config: dict) -> str:
    """サイト設定からデザインシステムプロンプトを生成"""
    ds = config.get("design_system", {})
    # design_rulesからの後方互換
    colors = config.get("design_rules", {}).get("colors", {})
    return DESIGN_SYSTEM_TEMPLATE.format(
        brand_name=ds.get("brand_name", ""),
        language=ds.get("language", "Japanese"),
        background_color=ds.get("background_color") or colors.get("bg_white", "#FFFFFF"),
        primary_color=ds.get("primary_color") or colors.get("main", "#3B82F6"),
        secondary_color=ds.get("secondary_color", "#10B981"),
        accent_color=ds.get("accent_color") or colors.get("accent_red", "#F59E0B"),
        text_color=ds.get("text_color") or colors.get("text", "#1F2937"),
        danger_color=ds.get("danger_color", "#E74A3B"),
        illustration_style=ds.get("illustration_style", "flat minimal"),
        line_weight=ds.get("line_weight", "均一な細線"),
        character_style=ds.get("character_style", "シンプルな人物"),
        fill_style=ds.get("fill_style", "フラット塗り"),
        card_style=ds.get("card_style", "白背景 + 角丸"),
        font_family=ds.get("font_family", "Noto Sans JP"),
        spacing=ds.get("spacing", "広めに均等"),
        prohibited_elements=ds.get("prohibited_elements", ""),
        additional_notes=ds.get("additional_notes", ""),
        ref_image_analysis=ds.get("ref_image_analysis", "（参照画像なし）"),
    )


def render_proposal_prompt(article_text: str, config: dict) -> str:
    """記事本文とサイト設定から画像案提案プロンプトを生成"""
    ds = config.get("design_system", {})
    return IMAGE_PROPOSAL_TEMPLATE.format(
        article_text=article_text[:4000],
        brand_tone=ds.get("brand_tone", "professional and approachable"),
        image_width=ds.get("image_width", 886),
        image_height=ds.get("image_height", 600),
    )


def _build_blocks_text(proposal: dict) -> str:
    """proposalのblocksをテキスト化する"""
    blocks = proposal.get("blocks", [])
    if blocks and isinstance(blocks[0], dict):
        lines = []
        for b in blocks:
            line = f"- 【{b.get('heading', '')}】{b.get('description', '')}"
            illust = b.get("illustration", "")
            if illust:
                line += f"　→ イラスト: {illust}"
            lines.append(line)
        return "\n".join(lines)
    return "\n".join(f"- {b}" for b in blocks)


def render_generation_prompt(
    design_system: str,
    proposal: dict,
    aspect_ratio: str,
    has_reference_images: bool = False,
) -> str:
    """デザインシステム + 画像案から最終生成プロンプトを組み立てる"""
    blocks_text = _build_blocks_text(proposal)

    if has_reference_images:
        return IMAGE_GENERATION_WITH_REF_TEMPLATE.format(
            layout_type=proposal.get("layout_type", ""),
            purpose=proposal.get("purpose", ""),
            conclusion=proposal.get("conclusion", ""),
            blocks_text=blocks_text,
            composition_description=proposal.get("composition_description", ""),
        )

    return IMAGE_GENERATION_TEMPLATE.format(
        design_system_prompt=design_system,
        layout_type=proposal.get("layout_type", ""),
        reader_mindset=proposal.get("reader_mindset", ""),
        purpose=proposal.get("purpose", ""),
        conclusion=proposal.get("conclusion", ""),
        blocks_text=blocks_text,
        composition_description=proposal.get("composition_description", ""),
        aspect_ratio=aspect_ratio,
    )
