import time
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai


def fetch_page_text(url: str) -> str:
    """Fetch a URL and return headings + body text (static HTML only)."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        headings = [
            f"{h.name.upper()}: {h.get_text(strip=True)}"
            for h in soup.find_all(["h1", "h2", "h3"])
            if h.get_text(strip=True)
        ]
        body = soup.get_text(separator="\n", strip=True)[:15000]
        return "【見出し構成】\n" + "\n".join(headings) + "\n\n【本文抜粋】\n" + body
    except Exception as e:
        return f"[取得失敗: {e}]"


def _gemini_call(model, prompt: str, max_retries: int = 3) -> str:
    """Universal Gemini call with exponential backoff on 429."""
    waits = [15, 30, 60]
    for attempt in range(max_retries):
        try:
            return model.generate_content(prompt).text
        except Exception as e:
            err = str(e)
            is_rate = any(k in err for k in ("429", "Resource exhausted", "RESOURCE_EXHAUSTED", "quota"))
            if is_rate and attempt < max_retries - 1:
                time.sleep(waits[attempt])
                continue
            return f"[情報取得失敗: {err}]"
    return "[情報取得失敗: レートリミット上限]"


def analyze_competitors(competitor_urls: list, gemini_api_key: str) -> dict:
    """Fetch competitor pages and analyze structure with Gemini."""
    pages = {url: fetch_page_text(url) for url in competitor_urls if url.strip()}

    if not pages:
        return {"raw_pages": {}, "analysis": "（競合なし）"}

    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    pages_text = "\n\n".join(
        f"=== 競合記事: {url} ===\n{content}"
        for url, content in pages.items()
    )

    prompt = f"""以下の競合記事（{len(pages)}件）の見出し構成を分析してください。

{pages_text}

以下を日本語で出力してください：

【必須トピック（複数記事に共通するH2項目）】
-

【差別化候補（一部のみのユニークH2）】
-

【競合の見出し構成一覧】
競合①: H2→ ...
競合②: H2→ ...

【クリニック紹介ブロックの共通フィールド】
-

【ジャンル特有のフィールド】
-
"""
    analysis = _gemini_call(model, prompt)
    return {"raw_pages": pages, "analysis": analysis}


def discover_clinics_from_competitors(
    raw_pages: dict, specified_clinics: list, gemini_api_key: str
) -> list:
    """Extract clinic names/domains from already-fetched competitor pages."""
    if not raw_pages:
        return []

    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    specified_names = {c["name"] for c in specified_clinics}
    pages_text = "\n\n".join(
        f"=== {url} ===\n{content[:2000]}"
        for url, content in raw_pages.items()
    )
    exclude_note = (
        f"除外（指定済み）: {', '.join(specified_names)}\n" if specified_names else ""
    )

    prompt = f"""以下の競合記事で「おすすめクリニック」として紹介されているクリニック名と公式URLを抽出してください。
{exclude_note}
{pages_text}

出力形式（1行1クリニック）：
クリニック名::URL_またはドメイン

URLが不明な場合は「クリニック名::unknown」。見つからない場合は「なし」とだけ出力。説明文は不要。"""

    text = _gemini_call(model, prompt)

    if text.startswith("[情報") or text.strip().lower() in ("なし", ""):
        return []

    discovered = []
    for line in text.splitlines():
        line = line.strip("- ").strip()
        if "::" not in line:
            continue
        name, domain = line.split("::", 1)
        name, domain = name.strip(), domain.strip()
        if not name or name in specified_names:
            continue
        if domain.lower() in ("unknown", "不明", ""):
            continue
        discovered.append({"name": name, "domain": domain})

    return discovered


def auto_discover_clinics(
    main_kw: str, genre: str, gemini_api_key: str, specified_clinics: list
) -> list:
    """Discover clinics using Gemini's knowledge (no search grounding)."""
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    specified_names = {c["name"] for c in specified_clinics}
    exclude_note = (
        f"除外（指定済み）: {', '.join(specified_names)}\n" if specified_names else ""
    )

    prompt = f"""「{main_kw}」というキーワードで集客している{genre}クリニックを最大5件リストアップしてください。
{exclude_note}
出力形式（1行1クリニック）：
クリニック名::公式サイトのドメイン

例：
TCB東京中央美容外科::tcb.net
湘南美容クリニック::s-b-c.net

説明文は不要。"""

    text = _gemini_call(model, prompt)

    discovered = []
    for line in text.strip().splitlines():
        line = line.strip("- ").strip()
        if "::" not in line:
            continue
        name, domain = line.split("::", 1)
        name, domain = name.strip(), domain.strip()
        if not name or name in specified_names or not domain:
            continue
        discovered.append({"name": name, "domain": domain})

    return discovered[:5]


def collect_clinic_info(clinics: list, genre: str, gemini_api_key: str) -> dict:
    """Collect clinic info: static fetch → Gemini extraction. All clinics in one batched call."""
    if not clinics:
        return {}

    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    # Fetch page content for each clinic
    fetched = {}
    for clinic in clinics:
        name = clinic["name"]
        domain_or_url = clinic["domain"]

        if domain_or_url.startswith("http"):
            content = fetch_page_text(domain_or_url)
        else:
            content = "[取得失敗]"
            for prefix in ["https://", "https://www."]:
                result = fetch_page_text(f"{prefix}{domain_or_url}")
                if not result.startswith("[取得失敗"):
                    content = result
                    break
        fetched[name] = content

    # Build one batched extraction prompt
    clinic_blocks = "\n\n".join(
        f"【{name}】\nWebサイト内容：\n{content[:2000]}\n\n"
        f"院名：\n住所：\nアクセス（最寄り駅・徒歩分数）：\n診療時間：\n休診日：\n"
        f"{genre}の料金（税込/税抜）：\n支払い方法（カード・ローン・現金）：\n"
        f"麻酔の有無と料金：\n学割・割引情報：\n予約方法："
        for name, content in fetched.items()
    )

    prompt = f"""以下の各クリニックについて、提供されたWebサイト内容から情報を抽出してください。
取得できない項目は「[要確認]」と記載してください。補完・推測は一切しないでください。

{clinic_blocks}

各クリニックの出力は「【クリニック名】」の見出しで始めてください。"""

    result_text = _gemini_call(model, prompt)

    # Parse per-clinic sections
    results = {}
    for clinic in clinics:
        name = clinic["name"]
        marker = f"【{name}】"
        if marker not in result_text:
            results[name] = "[要確認]"
            continue

        start = result_text.index(marker)
        next_pos = len(result_text)
        for other in clinics:
            if other["name"] == name:
                continue
            other_marker = f"【{other['name']}】"
            if other_marker in result_text:
                pos = result_text.index(other_marker)
                if pos > start:
                    next_pos = min(next_pos, pos)

        results[name] = result_text[start:next_pos].strip()

    return results
