"""複数のAIで記事を見て直す層。

守っている縛りは4つ。
1. 事実の指摘はAIに直させない。入力データとのズレなので、直させると辻褄合わせで
   嘘を作る。人のキューへ回す
2. 修正は差分限定。全文を再生成すると直っていない箇所が壊れて収束しない
3. 指摘は「該当箇所の原文」と「違反した規則名」の2つが揃うものだけ有効。場所が
   特定できない指摘は捨てる
4. 周回上限を設ける。上限のない自動修正は必ず止まらなくなる

見る側は書いた側と別のモデルにする。同じモデルで書いて同じモデルで見ると見落とす。
本文と規則だけを渡し、構成を作った過程は渡さない。自分が作ったものだと知らない
状態で見るので追認にならない。
"""

import json
import re

from core import output_check

MAX_ROUNDS = 2

# 事実に関する指摘はAIに直させない。この種別は人のキューへ回す。
HUMAN_KINDS = {"事実", "出典"}
FIX_KINDS = {"表現", "表記", "マーカー", "構成"}

_TAG_RE = re.compile(r"<[^>]+>")
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def pick_reviewer_provider(writer_provider: str) -> str:
    """書いた側と別のモデルを返す。"""
    return "gemini" if writer_provider == "claude" else "claude"


def _strip_tags(html: str) -> str:
    return _TAG_RE.sub("", html)


def _call_model(provider: str, prompt: str, claude_api_key: str = "", gemini_api_key: str = "") -> str:
    from core import config
    if provider == "openai" and config.openai_ready():
        return config.call_openai(prompt, max_tokens=8000)
    if provider == "gemini" and gemini_api_key:
        from google import genai as _genai
        client = _genai.Client(api_key=gemini_api_key)
        from core.config import GEMINI_TEXT_MODEL
        response = client.models.generate_content(model=GEMINI_TEXT_MODEL, contents=prompt)
        return response.text or ""
    import anthropic
    client = anthropic.Anthropic(api_key=claude_api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def parse_findings(raw: str) -> list[dict]:
    """モデルの出力から指摘のリストを取り出す。壊れていれば空を返す。"""
    text = raw.strip()
    block = _JSON_BLOCK_RE.search(text)
    if block:
        text = block.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    findings = []
    for item in data:
        if not isinstance(item, dict):
            continue
        findings.append({
            "kind": str(item.get("種別", item.get("kind", ""))).strip(),
            "rule": str(item.get("規則", item.get("rule", ""))).strip(),
            "text": str(item.get("原文", item.get("quote", ""))).strip(),
            "detail": str(item.get("理由", item.get("reason", ""))).strip(),
        })
    return findings


def keep_locatable(findings: list[dict], html: str) -> list[dict]:
    """原文が本文に実在する指摘だけ残す。場所が特定できない指摘は捨てる。"""
    plain = _strip_tags(html)
    kept = []
    seen = set()
    for f in findings:
        quote = f.get("text", "")
        rule = f.get("rule", "")
        if not quote or not rule:
            continue
        if quote not in html and quote not in plain:
            continue
        key = (rule, quote)
        if key in seen:
            continue
        seen.add(key)
        kept.append(f)
    return kept


def split_by_owner(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """AIに直させる指摘と、人のキューへ回す指摘に分ける。

    種別が空・想定外のものは人のキューへ回す。AIに投げて嘘を作るより安全側に倒す。
    """
    fix, human = [], []
    for f in findings:
        if f.get("kind") in FIX_KINDS:
            fix.append(f)
        else:
            human.append(f)
    return fix, human


def build_review_prompt(html: str, rules: str, article_type: str = "", main_kw: str = "") -> str:
    return f"""あなたは公開前の記事を検品する担当者です。この記事を書いたのはあなたではありません。

次の記事HTMLが、下の規則に違反していないかを見てください。

【記事タイプ】{article_type or "（指定なし）"}
【メインキーワード】{main_kw or "（指定なし）"}

【規則】
{rules}

【記事HTML】
{html[:60000]}

【出し方】
- 違反を見つけた箇所だけを挙げる。良い点・全体の講評は書かない
- 1件につき、記事から一字一句そのまま抜き出した原文を必ず添える。要約・言い換えをしない
- 原文が抜き出せない指摘は挙げない
- 種別は次から選ぶ。表現／表記／マーカー／構成／事実／出典
- 材料不足の印（[要確認]で始まる箇所）は検査の対象外。指摘に挙げない
- 出力はJSONの配列だけ。前後に説明文を書かない

[
  {{"種別": "表現", "規則": "禁止ワード", "原文": "記事からそのまま抜いた文字列", "理由": "何がどう違反か1文"}}
]
"""


def build_fix_prompt(html: str, findings: list[dict]) -> str:
    lines = "\n".join(
        f"- [{f['rule']}] {f['text']}\n　{f['detail']}" for f in findings
    )
    return f"""次の記事HTMLについて、以下の指摘箇所だけを書き換えてください。

指摘のない箇所は1文字も変更しないでください。全文を作り直すと直っていない箇所が
壊れて収束しないため、差分修正に限定します。タグの構造・クラス名・リンクのURLは
変えないでください。

【指摘】
{lines}

【記事HTML】
{html}

【出し方】
- 修正後のHTML全体をそのまま出力する
- コードブロック記号（```）をつけない
- 説明・前置き・変更点の要約を書かない
"""


def build_structure_review_prompt(structure_text: str, constraints: str,
                                  article_type: str = "", main_kw: str = "", sub_kw: list | None = None) -> str:
    return f"""あなたは公開前の記事構成を検品する担当者です。この構成を作ったのはあなたではありません。

次の見出し構成が、下の制約に反していないかを見てください。

【記事タイプ】{article_type or "（指定なし）"}
【メインキーワード】{main_kw or "（指定なし）"}
【サブキーワード】{', '.join(sub_kw or []) or "（指定なし）"}

【制約】
{constraints}

【見出し構成】
{structure_text[:20000]}

【見る観点】
- メインキーワードとサブキーワードに答えるH2があるか
- 同じことを書くH2・H3が重複していないか。隣り合う見出しで中身が衝突していないか
- 記事タイプに必要な固定H2が揃っているか
- 見出しに禁止ワード・断定表現が入っていないか
- 案件DBに無い情報を前提にした見出しがないか

【出し方】
- 違反を見つけた箇所だけを挙げる。良い点・全体の講評は書かない
- 1件につき、構成から一字一句そのまま抜き出した見出しを原文として添える
- 原文が抜き出せない指摘は挙げない
- 種別は次から選ぶ。構成／表現／事実
- 出力はJSONの配列だけ。前後に説明文を書かない

[
  {{"種別": "構成", "規則": "サブキーワードに答えるH2がない", "原文": "構成からそのまま抜いた見出し", "理由": "何がどう反しているか1文"}}
]
"""


def build_structure_fix_prompt(structure_text: str, findings: list[dict]) -> str:
    lines = "\n".join(f"- [{f['rule']}] {f['text']}\n　{f['detail']}" for f in findings)
    return f"""次の見出し構成について、以下の指摘箇所だけを直してください。

指摘のない見出しは1文字も変更しないでください。構成全体を作り直すと、通っていた
見出しまで変わって収束しません。見出しの階層と並び順は変えないでください。

【指摘】
{lines}

【見出し構成】
{structure_text}

【出し方】
- 修正後の見出し構成をそのまま出力する
- 説明・前置き・変更点の要約を書かない
"""


def review_structure(structure_text: str, constraints: str, provider: str,
                     claude_api_key: str = "", gemini_api_key: str = "",
                     article_type: str = "", main_kw: str = "", sub_kw: list | None = None) -> list[dict]:
    raw = _call_model(
        provider, build_structure_review_prompt(structure_text, constraints, article_type, main_kw, sub_kw),
        claude_api_key=claude_api_key, gemini_api_key=gemini_api_key,
    )
    return keep_locatable(parse_findings(raw), structure_text)


def apply_structure_fixes(structure_text: str, findings: list[dict], provider: str,
                          claude_api_key: str = "", gemini_api_key: str = "") -> str:
    if not findings:
        return structure_text
    fixed = _call_model(
        provider, build_structure_fix_prompt(structure_text, findings),
        claude_api_key=claude_api_key, gemini_api_key=gemini_api_key,
    ).strip()
    if not fixed or len(fixed) < len(structure_text) * 0.7:
        return structure_text
    return fixed


def run_structure_review_loop(
    structure_text: str,
    constraints: str,
    writer_provider: str = "claude",
    claude_api_key: str = "",
    gemini_api_key: str = "",
    article_type: str = "",
    main_kw: str = "",
    sub_kw: list | None = None,
    max_rounds: int = 1,
    progress=None,
) -> dict:
    """構成を別モデルに見せ、構成・表現の指摘だけ直す。

    事実の指摘は人のキューへ回す。Returns: {"structure_text", "rounds", "human", "remaining"}
    """
    def _log(msg: str) -> None:
        if progress:
            progress(msg)

    reviewer = pick_reviewer_provider(writer_provider)
    _log(f"　→ 構成の検品: {reviewer}")

    current = structure_text
    human: list[dict] = []
    remaining: list[dict] = []
    rounds = 0

    for round_no in range(1, max_rounds + 2):
        findings = review_structure(
            current, constraints, reviewer,
            claude_api_key=claude_api_key, gemini_api_key=gemini_api_key,
            article_type=article_type, main_kw=main_kw, sub_kw=sub_kw,
        )
        fix, human_now = split_by_owner(findings)
        for f in human_now:
            if f not in human:
                human.append(f)
        _log(f"　→ 構成 {round_no}回目の検品: 直す指摘 {len(fix)}件 ／ 人のキュー {len(human_now)}件")
        if not fix:
            remaining = []
            break
        if round_no > max_rounds:
            remaining = fix
            break
        current = apply_structure_fixes(
            current, fix, writer_provider,
            claude_api_key=claude_api_key, gemini_api_key=gemini_api_key,
        )
        rounds = round_no

    return {"structure_text": current, "rounds": rounds, "human": human, "remaining": remaining}


def review_once(html: str, rules: str, provider: str, claude_api_key: str = "",
                gemini_api_key: str = "", article_type: str = "", main_kw: str = "") -> list[dict]:
    """1周ぶんの検品。場所が特定できた指摘だけ返す。"""
    raw = _call_model(
        provider, build_review_prompt(html, rules, article_type, main_kw),
        claude_api_key=claude_api_key, gemini_api_key=gemini_api_key,
    )
    return keep_locatable(parse_findings(raw), html)


def apply_fixes(html: str, findings: list[dict], provider: str,
                claude_api_key: str = "", gemini_api_key: str = "") -> str:
    """指摘箇所だけを書き換えた記事HTMLを返す。直すのは本文を書いた側のモデル。"""
    if not findings:
        return html
    fixed = _call_model(
        provider, build_fix_prompt(html, findings),
        claude_api_key=claude_api_key, gemini_api_key=gemini_api_key,
    ).strip()
    fixed = re.sub(r"^```(?:html)?\s*", "", fixed)
    fixed = re.sub(r"\s*```$", "", fixed)
    # 大きく削られていたら差分修正になっていない。元のHTMLを残す。
    if not fixed or len(fixed) < len(html) * 0.7:
        return html
    return fixed


def run_review_loop(
    html: str,
    rules: str,
    writer_provider: str = "claude",
    claude_api_key: str = "",
    gemini_api_key: str = "",
    article_type: str = "",
    main_kw: str = "",
    source_text: str = "",
    max_rounds: int = MAX_ROUNDS,
    progress=None,
) -> dict:
    """機械検出とAI検品を合わせて回し、表現の指摘だけ直す。

    Returns: {"html", "rounds", "human", "remaining", "history"}
    """
    def _log(msg: str) -> None:
        if progress:
            progress(msg)

    reviewer = pick_reviewer_provider(writer_provider)
    _log(f"　→ 書き手: {writer_provider} ／ 見る側: {reviewer}")

    human: list[dict] = []
    history: list[dict] = []
    current = html
    remaining: list[dict] = []
    rounds = 0

    # 検品は修正の回数より1回多く回す。最後の修正が効いたかを見ないと、
    # 直っているのに人のキューへ回してしまう。
    for round_no in range(1, max_rounds + 2):
        machine = output_check.run_checks(current, source_text)
        ai_findings = review_once(
            current, rules, reviewer,
            claude_api_key=claude_api_key, gemini_api_key=gemini_api_key,
            article_type=article_type, main_kw=main_kw,
        )
        ai_fix, ai_human = split_by_owner(ai_findings)
        fix = machine["fix"] + ai_fix
        human_now = machine["human"] + ai_human
        for f in human_now:
            if f not in human:
                human.append(f)
        history.append({"round": round_no, "fix": len(fix), "human": len(human_now)})
        _log(f"　→ {round_no}回目の検品: 直す指摘 {len(fix)}件 ／ 人のキュー {len(human_now)}件")

        if not fix:
            remaining = []
            break
        if round_no > max_rounds:
            remaining = fix
            break

        current = apply_fixes(
            current, fix, writer_provider,
            claude_api_key=claude_api_key, gemini_api_key=gemini_api_key,
        )
        rounds = round_no

    if remaining:
        _log(f"　→ {max_rounds}周で指摘が消えませんでした。人のキューへ回します")

    return {
        "html": current,
        "rounds": rounds,
        "human": human,
        "remaining": remaining,
        "history": history,
    }
