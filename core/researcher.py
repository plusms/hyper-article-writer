import requests
import anthropic
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse


def fetch_page_text(url: str) -> str:
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


def _claude_call(api_key: str, prompt: str) -> str:
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as e:
        return f"[情報取得失敗: {e}]"


def analyze_competitors(competitor_urls: list, claude_api_key: str) -> dict:
    pages = {url: fetch_page_text(url) for url in competitor_urls if url.strip()}

    if not pages:
        return {"raw_pages": {}, "analysis": "（競合なし）"}

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
    analysis = _claude_call(claude_api_key, prompt)
    return {"raw_pages": pages, "analysis": analysis}


def discover_clinics_from_competitors(
    raw_pages: dict, specified_clinics: list, claude_api_key: str
) -> list:
    if not raw_pages:
        return []

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

    text = _claude_call(claude_api_key, prompt)

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
    main_kw: str, genre: str, claude_api_key: str, specified_clinics: list
) -> list:
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

    text = _claude_call(claude_api_key, prompt)

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


def _find_price_pages(base_url: str, genre: str, max_pages: int = 2) -> list[str]:
    """トップページのリンクから料金・メニュー系ページのURLを抽出する。"""
    price_keywords = ["price", "料金", "費用", "プラン", "plan", "menu", "メニュー", "cost", "ryokin"]
    if genre:
        price_keywords.append(genre[:4])
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(base_url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        base_domain = urlparse(base_url).netloc
        seen, links = set(), []
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            if urlparse(href).netloc != base_domain:
                continue
            if href in seen or href == base_url:
                continue
            link_text = (a.get_text(strip=True) + " " + href).lower()
            if any(kw.lower() in link_text for kw in price_keywords if kw):
                seen.add(href)
                links.append(href)
                if len(links) >= max_pages:
                    break
        return links
    except Exception:
        return []


def collect_clinic_info(clinics: list, genre: str, claude_api_key: str, article_type: str = "") -> dict:
    if not clinics:
        return {}

    fetched = {}
    for clinic in clinics:
        name = clinic["name"]
        domain_or_url = clinic["domain"]

        if domain_or_url.startswith("http"):
            main_url = domain_or_url
            content = fetch_page_text(main_url)
        else:
            main_url = None
            content = "[取得失敗]"
            for prefix in ["https://", "https://www."]:
                result = fetch_page_text(f"{prefix}{domain_or_url}")
                if not result.startswith("[取得失敗"):
                    main_url = f"{prefix}{domain_or_url}"
                    content = result
                    break

        # 商標記事は料金・メニューページも追加取得
        if article_type == "商標" and main_url:
            extra_pages = _find_price_pages(main_url, genre)
            for extra_url in extra_pages:
                extra = fetch_page_text(extra_url)
                if not extra.startswith("[取得失敗"):
                    content += f"\n\n--- 追加ページ: {extra_url} ---\n{extra}"

        fetched[name] = content

    clinic_blocks = "\n\n".join(
        f"【{name}】\nWebサイト内容：\n{content[:6000]}\n\n"
        f"院名：\n住所：\nアクセス（最寄り駅・徒歩分数）：\n診療時間：\n休診日：\n"
        f"{genre}の料金（税込/税抜）：\n支払い方法（カード・ローン・現金）：\n"
        f"麻酔の有無と料金：\n学割・割引情報：\n予約方法："
        for name, content in fetched.items()
    )

    prompt = f"""以下の各クリニックについて、提供されたWebサイト内容から情報を抽出してください。
取得できない項目は「[要確認]」と記載してください。補完・推測は一切しないでください。

{clinic_blocks}

各クリニックの出力は「【クリニック名】」の見出しで始めてください。"""

    result_text = _claude_call(claude_api_key, prompt)

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
