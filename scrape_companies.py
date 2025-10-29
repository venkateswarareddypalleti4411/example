import json
import re
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT_SECONDS = 20


@dataclass
class CompanyRecord:
    company_name: str = ""
    description: str = ""
    founded: str = ""
    company_sector_type: str = ""
    business_type: str = ""
    company_size: str = ""
    website_link: str = ""
    operating_type: str = ""
    company_type: str = ""
    founders: str = ""
    industry: str = ""
    about: str = ""
    headquarters: str = ""


def normalize_website_link(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    # Strip default ports
    host = host.replace(":80", "").replace(":443", "")
    return host.lower()


def http_get(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 200 and resp.text:
            return resp.text
        return None
    except requests.RequestException:
        return None


def first_text(*values: Optional[str]) -> str:
    for v in values:
        if v and v.strip():
            return v.strip()
    return ""


def extract_meta(soup: BeautifulSoup, name: str = None, prop: str = None) -> Optional[str]:
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"]
    if prop:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return tag["content"]
    return None


def _domain_brand_from_host(host: str) -> str:
    host = host.lower()
    # pick the longest non-www label as brand seed
    labels = [lbl for lbl in host.split(".") if lbl and lbl != "www"]
    if not labels:
        return host
    seed = max(labels, key=len)
    # Title-case but keep numbers as-is (e.g., Dash0)
    if seed.isalpha():
        return seed.title()
    # Mixed alnum: capitalize first letter only
    return seed[0].upper() + seed[1:]


def _choose_brand_from_title(title: str) -> str:
    # Split with common separators and pick the most brand-like token
    tokens = re.split(r"\s*[\-|\u2013\u2014\|\u00b7]\s*", title)
    def score(token: str) -> int:
        token = token.strip()
        if not token:
            return 0
        # Prefer short tokens with capitalization
        words = token.split()
        cap_words = sum(1 for w in words if re.match(r"[A-Z][A-Za-z0-9\-']+", w))
        camel = 1 if re.search(r"[a-z][A-Z]", token) else 0
        digits = 1 if re.search(r"\d", token) else 0
        length_penalty = max(0, len(words) - 3)
        return cap_words * 3 + camel * 2 + digits - length_penalty
    if len(tokens) == 1:
        return tokens[0].strip()
    best = max(tokens, key=score)
    return best.strip()


def extract_company_name(url: str, soup: BeautifulSoup) -> str:
    og_site = extract_meta(soup, prop="og:site_name")
    if og_site:
        return og_site.strip()
    og_title = extract_meta(soup, prop="og:title")
    if og_title:
        candidate = _choose_brand_from_title(og_title)
        if candidate:
            return candidate
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    if title:
        candidate = _choose_brand_from_title(title)
        if candidate:
            return candidate
    return _domain_brand_from_host(normalize_website_link(url))


def extract_description(soup: BeautifulSoup) -> str:
    return first_text(
        extract_meta(soup, name="description"),
        extract_meta(soup, prop="og:description"),
    )


def absolute_link(base_url: str, href: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith("#"):
        return None
    return urljoin(base_url, href)


def find_candidate_links(base_url: str, soup: BeautifulSoup, keywords: List[str]) -> List[str]:
    links: List[str] = []
    for a in soup.find_all("a"):
        text = (a.get_text() or "").strip().lower()
        href = a.get("href")
        if not href:
            continue
        url = absolute_link(base_url, href)
        if not url:
            continue
        for kw in keywords:
            if kw in text or kw in href.lower():
                links.append(url)
                break
    # Also try common slugs directly
    for slug in ["/about", "/about-us", "/company", "/who-we-are", "/our-story", "/team", "/leadership", "/contact", "/contact-us"]:
        links.append(urljoin(base_url, slug))
    # De-duplicate while preserving order
    seen = set()
    unique_links = []
    for l in links:
        if l not in seen:
            unique_links.append(l)
            seen.add(l)
    return unique_links


def extract_about_text(soup: BeautifulSoup) -> str:
    # Prefer main content paragraphs
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text = " ".join([p for p in paragraphs if len(p.split()) >= 5])
    # Trim excessively long text
    if len(text) > 1500:
        text = text[:1500].rsplit(" ", 1)[0] + ""
    return text.strip()


def search_for_headquarters(text: str) -> str:
    # Look for explicit Headquarters label
    hq_match = re.search(r"Headquarters?\s*[:\-]?\s*(.+)", text, flags=re.IGNORECASE)
    if hq_match:
        candidate = hq_match.group(1).strip()
        candidate = re.split(r"\n|\.|\u2022|\||,?\s*USA|,?\s*US$", candidate)[0].strip()
        # Require a comma (e.g., City, ST or City, Country) to reduce false positives
        if "," in candidate and len(candidate) >= 3:
            return candidate
    # Address-like lines: city, state pattern
    for line in text.splitlines():
        line_clean = line.strip()
        if re.search(r"\b[A-Za-z][A-Za-z\s]+,\s*[A-Z]{2}(?:\s*\d{5})?\b", line_clean):
            return line_clean
    return ""


def search_for_founded(text: str) -> str:
    m = re.search(r"Founded\s*(?:in)?\s*(\d{4})", text, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.search(r"Since\s*(\d{4})", text, flags=re.IGNORECASE)
    if m2:
        return m2.group(1)
    return ""


def search_for_founders(text: str) -> str:
    # High-precision patterns only; avoid false positives
    patterns = [
        r"Founded by\s+([^\n\.;:]+)",
        r"Founders?\s*[:\-]\s*([^\n\.;:]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue
        segment = m.group(1)
        # Split by and/&, or commas
        raw_parts = re.split(r"\s+(?:and|&)\s+|,", segment)
        cleaned: List[str] = []
        for part in raw_parts:
            name = part.strip()
            # Keep tokens that look like names (2-4 capitalized words, optional hyphens)
            if re.fullmatch(r"(?:[A-Z][A-Za-z\-']+\s+){1,3}[A-Z][A-Za-z\-']+", name):
                cleaned.append(name)
        if cleaned:
            # Deduplicate preserving order
            seen = set()
            unique: List[str] = []
            for c in cleaned:
                if c not in seen:
                    unique.append(c)
                    seen.add(c)
            return ",".join(unique)
    return ""


def scrape_company(url: str) -> CompanyRecord:
    html = http_get(url)
    if not html:
        return CompanyRecord(website_link=normalize_website_link(url))

    soup = BeautifulSoup(html, "lxml")

    record = CompanyRecord()
    record.website_link = normalize_website_link(url)
    record.company_name = extract_company_name(url, soup)
    record.description = extract_description(soup)

    # Explore candidate pages for more details
    candidates = find_candidate_links(
        base_url=url,
        soup=soup,
        keywords=["about", "company", "story", "who we are", "team", "leadership", "contact", "offices", "locations"],
    )

    visited = 0
    aggregated_text = []

    for link in candidates:
        if visited >= 5:
            break
        page = http_get(link)
        if not page:
            continue
        visited += 1
        s = BeautifulSoup(page, "lxml")
        text = s.get_text("\n", strip=True)
        if text:
            aggregated_text.append(text)
        # Fill about (first good-looking about page)
        if not record.about:
            # Prefer pages with "about", "company" in URL
            if any(k in link.lower() for k in ["about", "company", "our-story", "who-we-are"]):
                record.about = extract_about_text(s)
        # Fallback: if still empty and this page has enough paragraphs, use it
        if not record.about:
            paragraphs = [p.get_text(" ", strip=True) for p in s.find_all("p")]
            if len(" ".join(paragraphs)) > 200:
                record.about = extract_about_text(s)

    combined = "\n".join(aggregated_text)

    # Attempt to extract structured fields conservatively
    if not record.founded:
        record.founded = search_for_founded(combined)
    if not record.founders:
        record.founders = search_for_founders(combined)
    if not record.headquarters:
        record.headquarters = search_for_headquarters(combined)

    # Ensure strings (no None)
    for k, v in asdict(record).items():
        if v is None:
            setattr(record, k, "")

    return record


def main() -> None:
    urls = [
        "https://alphataraxia.com",
        "https://tacto.com",
        "https://www.litify.com",
        "https://quidel.com",
        "https://dash0.com",
        "https://www.csgroup.com",
        "https://www.accuweather.com",
        "https://www.dimensional.com",
        "https://www.policyme.com",
        "https://flipster.io",
    ]

    results: List[Dict[str, str]] = []
    for i, url in enumerate(urls, start=1):
        print(f"Scraping {i}/{len(urls)}: {url}")
        rec = scrape_company(url)
        # Reorder and ensure all required keys exist exactly
        ordered: Dict[str, str] = {
            "company_name": rec.company_name,
            "description": rec.description,
            "founded": rec.founded,
            "company_sector_type": rec.company_sector_type,
            "business_type": rec.business_type,
            "company_size": rec.company_size,
            "website_link": rec.website_link,
            "operating_type": rec.operating_type,
            "company_type": rec.company_type,
            "founders": rec.founders,
            "industry": rec.industry,
            "about": rec.about,
            "headquarters": rec.headquarters,
        }
        results.append(ordered)
        # Be a polite scraper
        time.sleep(1.0)

    out_path = "example_10.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(results)} records to {out_path}")


if __name__ == "__main__":
    main()
