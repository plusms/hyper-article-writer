import base64
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


def _claude_call(api_key: str, prompt: str, max_tokens: int = 4096, model: str = "claude-haiku-4-5-20251001") -> str:
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as e:
        return f"[情報取得失敗: {e}]"


def _gemini_call(api_key: str, prompt: str) -> str:
    try:
        from google import genai as _genai
        client = _genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"[情報取得失敗: {e}]"


def _research_call(
    prompt: str,
    claude_api_key: str = "",
    gemini_api_key: str = "",
    provider: str = "claude",
    max_tokens: int = 4096,
) -> str:
    if provider == "gemini" and gemini_api_key:
        return _gemini_call(gemini_api_key, prompt)
    return _claude_call(claude_api_key, prompt, max_tokens)


def analyze_competitors(competitor_urls: list, claude_api_key: str, gemini_api_key: str = "", research_provider: str = "claude") -> dict:
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
    analysis = _research_call(prompt, claude_api_key, gemini_api_key, research_provider)
    return {"raw_pages": pages, "analysis": analysis}


def discover_clinics_from_competitors(
    raw_pages: dict, specified_clinics: list, claude_api_key: str, gemini_api_key: str = "", research_provider: str = "claude"
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

    text = _research_call(prompt, claude_api_key, gemini_api_key, research_provider)

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
    main_kw: str, genre: str, claude_api_key: str, specified_clinics: list, gemini_api_key: str = "", research_provider: str = "claude"
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

    text = _research_call(prompt, claude_api_key, gemini_api_key, research_provider)

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


def crawl_site(start_url: str, genre: str, max_pages: int = 20) -> str:
    """指定URLから同ドメイン内をBFSクロールし、収集ページのテキストを結合して返す。"""
    from collections import deque

    if not start_url.startswith("http"):
        start_url = f"https://{start_url}"

    parsed = urlparse(start_url)
    base_domain = parsed.netloc
    path_prefix = parsed.path.rstrip("/")

    priority_kw = [
        "料金", "price", "費用", "プラン", "plan", "menu", "メニュー",
        "access", "アクセス", "flow", "流れ", "faq", "よくある",
        "実績", "症例", "診療", "初診", "about", "概要", "特徴",
    ]
    if genre:
        priority_kw.append(genre[:5])

    headers_ua = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    visited: set = set()
    priority_q: deque = deque([start_url])
    regular_q: deque = deque()
    collected: list = []

    while (priority_q or regular_q) and len(collected) < max_pages:
        url = priority_q.popleft() if priority_q else regular_q.popleft()
        clean_url = url.split("#")[0]
        if clean_url in visited:
            continue
        visited.add(clean_url)

        try:
            r = requests.get(clean_url, headers=headers_ua, timeout=15)
            r.encoding = r.apparent_encoding
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            headings = [
                f"{h.name.upper()}: {h.get_text(strip=True)}"
                for h in soup.find_all(["h1", "h2", "h3"])
                if h.get_text(strip=True)
            ]
            body = soup.get_text(separator="\n", strip=True)[:10000]
            collected.append(
                f"=== {clean_url} ===\n"
                "【見出し構成】\n" + "\n".join(headings) + "\n\n【本文抜粋】\n" + body
            )

            for a in soup.find_all("a", href=True):
                href = urljoin(clean_url, a["href"]).split("#")[0]
                lp = urlparse(href)
                if lp.netloc != base_domain:
                    continue
                if href in visited:
                    continue
                if path_prefix and not lp.path.startswith(path_prefix):
                    continue
                link_text = (a.get_text(strip=True) + " " + href).lower()
                if any(kw.lower() in link_text for kw in priority_kw if kw):
                    priority_q.append(href)
                else:
                    regular_q.append(href)
        except Exception:
            continue

    return "\n\n".join(collected)


DB_TYPE_CLINIC     = "クリニック"
DB_TYPE_LIFESTYLE  = "ライフスタイル"

_CLINIC_FIELDS = """\
院名：
住所：
診療時間：
休診日：
予約方法：
料金詳細（プランごと・全プラン・税込/税抜を記載）：
諸費用（診察料・カウンセリング料・麻酔代・薬代・アフターケア代等）：
保証（再手術保証・返金保証等）：
支払方法（カード・ローン・現金）：
割引情報（学割・モニター・クーポン等）：
実績（症例数・在籍医師・認定資格等）：
配送情報（薬・サプリ等の配送有無・方法）：
診療の流れ（予約〜アフターケアまでのステップ）：
診察方法（対面・オンライン・電話）：
途中解約（解約条件・違約金等）：
参照URL：
全国院数："""

_LIFESTYLE_FIELDS = """\
ブランド名：
商品名（代表的なもの5つ）：
料金：
会社名：
送料：
配送情報："""

# 後方互換
_EXTRACTION_FIELDS = _CLINIC_FIELDS


def _get_fields(db_type: str) -> str:
    return _LIFESTYLE_FIELDS if db_type == DB_TYPE_LIFESTYLE else _CLINIC_FIELDS


_LP_IMAGE_PROMPT = """\
LPに記載されている以下の情報をすべて抜き出してください。補完・推測は一切しないでください。

- キャッチコピー・メイン訴求（LPの冒頭・目立つ見出し）
- 訴求軸・強み（なぜ選ばれるか・他院との違い）
- メインプラン・おすすめプランの料金と内容
- LP限定クーポン・割引情報・キャンペーン
- CTA（今すぐ予約・無料カウンセリング等のボタン文言）
- その他、クロールでは取れないLP固有の情報

見つからない項目は省略してください。形式は自由でよいので、読み取れた情報をすべて出力してください。"""


def _lp_images_gemini(image_bytes_list: list[bytes], name: str, gemini_api_key: str) -> str:
    from google import genai as _genai
    from google.genai import types as _gtypes
    client = _genai.Client(api_key=gemini_api_key)
    parts = []
    for img_bytes in image_bytes_list:
        mime = "image/jpeg" if img_bytes[:2] == b"\xff\xd8" else "image/png"
        parts.append(_gtypes.Part.from_bytes(data=img_bytes, mime_type=mime))
    parts.append(f"上記は「{name}」のランディングページ（LP）のスクリーンショットです。\n{_LP_IMAGE_PROMPT}")
    response = client.models.generate_content(model="gemini-2.0-flash", contents=parts)
    return response.text


def _lp_images_claude(image_bytes_list: list[bytes], name: str, claude_api_key: str) -> str:
    image_contents = []
    for img_bytes in image_bytes_list:
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        media_type = "image/jpeg" if img_bytes[:2] == b"\xff\xd8" else "image/png"
        image_contents.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    image_contents.append({
        "type": "text",
        "text": f"上記は「{name}」のランディングページ（LP）のスクリーンショットです。\n{_LP_IMAGE_PROMPT}",
    })
    client = anthropic.Anthropic(api_key=claude_api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": image_contents}],
    )
    return msg.content[0].text


def extract_text_from_lp_images(
    image_bytes_list: list[bytes],
    name: str,
    claude_api_key: str,
    gemini_api_key: str = "",
    research_provider: str = "gemini",
) -> str:
    """LPスクリーンショット画像群からテキスト情報を抽出する。Gemini優先、フォールバックClaude。"""
    if not image_bytes_list:
        return ""
    try:
        if research_provider == "gemini" and gemini_api_key:
            return _lp_images_gemini(image_bytes_list, name, gemini_api_key)
        if claude_api_key:
            return _lp_images_claude(image_bytes_list, name, claude_api_key)
        return "[LP画像解析失敗: APIキー未設定]"
    except Exception as e:
        return f"[LP画像解析失敗: {e}]"


def build_content_with_lp(crawl_content: str, lp_text: str) -> str:
    """クロール結果とLP解析テキストを結合する。LP情報を優先ラベル付きで追記。"""
    parts = []
    if crawl_content:
        parts.append(crawl_content)
    if lp_text:
        parts.append(f"【LP情報（ランディングページ）— クーポン・料金・訴求軸はこちらを優先】\n{lp_text}")
    return "\n\n".join(parts)


def extract_clinic_info_from_content(content: str, name: str, genre: str, claude_api_key: str, db_type: str = DB_TYPE_CLINIC, gemini_api_key: str = "", research_provider: str = "claude") -> str:
    """クロール済みコンテンツから指定ジャンルの情報を抽出する。案件DB保存用。"""
    fields = _get_fields(db_type)
    genre_note = (
        f"「{genre}」に関する情報のみ抽出してください。料金詳細は「{genre}」のプランのみ記載してください。\n"
        if genre and db_type == DB_TYPE_CLINIC else ""
    )
    prompt = f"""以下の{name}のWebサイト内容から情報を抽出してください。
{genre_note}取得できない項目は「[要確認]」と記載してください。補完・推測は一切しないでください。

{content[:40000]}

出力は「【{name}】」の見出しで始め、以下の形式で記載してください：

【{name}】
{fields}"""
    return _research_call(prompt, claude_api_key, gemini_api_key, research_provider, max_tokens=8192)


def collect_clinic_info(clinics: list, genre: str, claude_api_key: str, article_type: str = "", db_cache: dict | None = None, full_crawl: bool = False, db_type: str = DB_TYPE_CLINIC, gemini_api_key: str = "", research_provider: str = "claude") -> dict:
    if not clinics:
        return {}

    db_cache = db_cache or {}
    db_results = {c["name"]: db_cache[c["name"]] for c in clinics if c["name"] in db_cache}
    clinics_to_scrape = [c for c in clinics if c["name"] not in db_cache]

    if not clinics_to_scrape:
        return db_results

    scraped_results = {}

    if full_crawl:
        # DB事前収集用：1院ずつクロール → 個別抽出
        for clinic in clinics_to_scrape:
            name = clinic["name"]
            domain_or_url = clinic["domain"]
            start_url = domain_or_url if domain_or_url.startswith("http") else f"https://{domain_or_url}"
            content = crawl_site(start_url, genre, max_pages=20)
            fields = _get_fields(db_type)
            prompt = f"""以下の{name}のWebサイト内容から情報を抽出してください。
取得できない項目は「[要確認]」と記載してください。補完・推測は一切しないでください。

{content[:30000]}

出力は「【{name}】」の見出しで始め、以下の形式で記載してください：

【{name}】
{fields}"""
            scraped_results[name] = _research_call(prompt, claude_api_key, gemini_api_key, research_provider, max_tokens=8192)
    else:
        # 記事生成時：従来通り main + 料金ページのみ、一括抽出
        fetched = {}
        for clinic in clinics_to_scrape:
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

            if main_url:
                extra_pages = _find_price_pages(main_url, genre)
                for extra_url in extra_pages:
                    extra = fetch_page_text(extra_url)
                    if not extra.startswith("[取得失敗"):
                        content += f"\n\n--- 追加ページ: {extra_url} ---\n{extra}"

            fetched[name] = content

        fields = _get_fields(db_type)
        clinic_blocks = "\n\n".join(
            f"【{name}】\nWebサイト内容：\n{content[:6000]}\n\n{fields}"
            for name, content in fetched.items()
        )
        prompt = f"""以下の各クリニックについて、提供されたWebサイト内容から情報を抽出してください。
取得できない項目は「[要確認]」と記載してください。補完・推測は一切しないでください。

{clinic_blocks}

各クリニックの出力は「【クリニック名】」の見出しで始めてください。"""
        result_text = _research_call(prompt, claude_api_key, gemini_api_key, research_provider)

        for clinic in clinics_to_scrape:
            name = clinic["name"]
            marker = f"【{name}】"
            if marker not in result_text:
                scraped_results[name] = "[要確認]"
                continue
            start = result_text.index(marker)
            next_pos = len(result_text)
            for other in clinics_to_scrape:
                if other["name"] == name:
                    continue
                other_marker = f"【{other['name']}】"
                if other_marker in result_text:
                    pos = result_text.index(other_marker)
                    if pos > start:
                        next_pos = min(next_pos, pos)
            scraped_results[name] = result_text[start:next_pos].strip()

    return {**db_results, **scraped_results}
