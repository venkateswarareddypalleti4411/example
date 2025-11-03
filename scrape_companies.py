import argparse
import json
import re
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
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
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"


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
    host = host.replace(":80", "").replace(":443", "")
    return host.lower().rstrip("/")


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
    labels = [lbl for lbl in host.split(".") if lbl and lbl != "www"]
    if not labels:
        return host
    seed = max(labels, key=len)
    if seed.isalpha():
        return seed.title()
    return seed[0].upper() + seed[1:]


def _choose_brand_from_title(title: str, domain_hint: str = "") -> str:
    tokens = re.split(r"\s*[\-|\u2013\u2014\|\u00b7]\s*", title)
    dh = re.sub(r"[^a-z0-9]", "", domain_hint.lower()) if domain_hint else ""

    def score(token: str) -> int:
        token = token.strip()
        if not token:
            return 0
        words = token.split()
        cap_words = sum(1 for w in words if re.match(r"[A-Z][A-Za-z0-9\-']+", w))
        camel = 1 if re.search(r"[a-z][A-Z]", token) else 0
        digits = 1 if re.search(r"\d", token) else 0
        length_penalty = max(0, len(words) - 2)  # prefer <= 2 words
        base = cap_words * 3 + camel * 2 + digits - length_penalty
        if dh:
            tnorm = re.sub(r"[^a-z0-9]", "", token.lower())
            if dh in tnorm:
                base += 10
        return base

    if len(tokens) == 1:
        return tokens[0].strip()
    best = max(tokens, key=score)
    return best.strip()


def extract_company_name(url: str, soup: BeautifulSoup) -> str:
    host = normalize_website_link(url)
    domain_hint = _domain_brand_from_host(host)
    og_site = extract_meta(soup, prop="og:site_name")
    if og_site:
        return og_site.strip()
    og_title = extract_meta(soup, prop="og:title")
    if og_title:
        candidate = _choose_brand_from_title(og_title, domain_hint)
        if candidate:
            return candidate
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    if title:
        candidate = _choose_brand_from_title(title, domain_hint)
        if candidate:
            return candidate
    return domain_hint


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
    for slug in ["/about", "/about-us", "/company", "/who-we-are", "/our-story", "/team", "/leadership", "/contact", "/contact-us", "/legal", "/imprint", "/privacy-policy", "/locations"]:
        links.append(urljoin(base_url, slug))
    seen = set()
    unique_links = []
    for l in links:
        if l not in seen:
            unique_links.append(l)
            seen.add(l)
    return unique_links


def extract_about_text(soup: BeautifulSoup) -> str:
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text = " ".join([p for p in paragraphs if len(p.split()) >= 5])
    if len(text) > 1600:
        text = text[:1600].rsplit(" ", 1)[0]
    return text.strip()


def search_for_headquarters(text: str) -> str:
    hq_match = re.search(r"Headquarters?\s*[:\-]?\s*(.+)", text, flags=re.IGNORECASE)
    if hq_match:
        candidate = hq_match.group(1).strip()
        candidate = re.split(r"\n|\.|\u2022|\||,?\s*USA|,?\s*US$", candidate)[0].strip()
        if "," in candidate and len(candidate) >= 3:
            return candidate
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
    patterns = [
        r"Founded by\s+([^\n\.;:]+)",
        r"Founders?\s*[:\-]\s*([^\n\.;:]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue
        segment = m.group(1)
        raw_parts = re.split(r"\s+(?:and|&)\s+|,", segment)
        cleaned: List[str] = []
        for part in raw_parts:
            name = part.strip()
            if re.fullmatch(r"(?:[A-Z][A-Za-z\-']+\s+){1,3}[A-Z][A-Za-z\-']+", name):
                cleaned.append(name)
        if cleaned:
            seen = set()
            unique: List[str] = []
            for c in cleaned:
                if c not in seen:
                    unique.append(c)
                    seen.add(c)
            return ",".join(unique)
    return ""


def parse_jsonld(soup: BeautifulSoup) -> Tuple[Dict[str, Any], List[str]]:
    data: Dict[str, Any] = {}
    same_as: List[str] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = script.string or script.text
            if not raw:
                continue
            obj = json.loads(raw)
        except Exception:
            continue
        def handle(item: Any) -> None:
            nonlocal data, same_as
            if not isinstance(item, dict):
                return
            t = item.get("@type")
            if isinstance(t, list):
                types = [str(x).lower() for x in t]
            else:
                types = [str(t).lower()] if t else []
            name = item.get("name")
            if name and not data.get("name"):
                data["name"] = str(name)
            description = item.get("description")
            if description and not data.get("description"):
                data["description"] = str(description)
            founding = item.get("foundingDate") or item.get("foundingYear")
            if founding and not data.get("foundingDate"):
                data["foundingDate"] = str(founding)
            num_emp = item.get("numberOfEmployees") or item.get("employee")
            if num_emp and not data.get("numberOfEmployees"):
                try:
                    data["numberOfEmployees"] = int(num_emp)
                except Exception:
                    pass
            founder = item.get("founder") or item.get("founders")
            if founder and not data.get("founders"):
                if isinstance(founder, list):
                    names: List[str] = []
                    for f in founder:
                        if isinstance(f, dict) and f.get("name"):
                            names.append(str(f["name"]))
                        elif isinstance(f, str):
                            names.append(f)
                    if names:
                        data["founders"] = names
                elif isinstance(founder, dict) and founder.get("name"):
                    data["founders"] = [str(founder["name"])]
                elif isinstance(founder, str):
                    data["founders"] = [founder]
            addr = item.get("address")
            if isinstance(addr, dict):
                locality = addr.get("addressLocality")
                region = addr.get("addressRegion")
                country = addr.get("addressCountry")
                if any([locality, region, country]) and not data.get("address"):
                    data["address"] = ", ".join([x for x in [locality, region, country] if x])
            same = item.get("sameAs")
            if same:
                if isinstance(same, list):
                    same_as.extend([str(x) for x in same])
                elif isinstance(same, str):
                    same_as.append(same)
        if isinstance(obj, list):
            for it in obj:
                handle(it)
        else:
            handle(obj)
    seen = set()
    same_as_unique: List[str] = []
    for s in same_as:
        if s not in seen:
            same_as_unique.append(s)
            seen.add(s)
    return data, same_as_unique


def bucket_employees(n: int) -> str:
    buckets = [
        (1, 10), (11, 50), (51, 200), (201, 500), (501, 1000),
        (1001, 5000), (5001, 10000)
    ]
    for lo, hi in buckets:
        if lo <= n <= hi:
            return f"{lo}-{hi}"
    if n >= 10001:
        return "10001+"
    return ""


def enrich_from_jsonld(record: CompanyRecord, jsonld: Dict[str, Any]) -> None:
    if not record.company_name and jsonld.get("name"):
        record.company_name = str(jsonld["name"]).strip()
    if not record.description and jsonld.get("description"):
        record.description = str(jsonld["description"]).strip()
    if not record.founded and jsonld.get("foundingDate"):
        m = re.search(r"(\d{4})", str(jsonld["foundingDate"]))
        if m:
            record.founded = m.group(1)
    if not record.company_size and isinstance(jsonld.get("numberOfEmployees"), int):
        record.company_size = bucket_employees(int(jsonld["numberOfEmployees"]))
    if not record.founders and isinstance(jsonld.get("founders"), list):
        record.founders = ",".join([str(x) for x in jsonld["founders"]])
    if not record.headquarters and jsonld.get("address"):
        record.headquarters = str(jsonld["address"]).strip()


def _wikidata_get_entities(ids: List[str]) -> Dict[str, Any]:
    if not ids:
        return {}
    try:
        r = requests.get(
            WIKIDATA_API,
            params={
                "action": "wbgetentities",
                "ids": "|".join(ids),
                "props": "labels|claims|sitelinks",
                "languages": "en",
                "format": "json",
            },
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if r.status_code == 200:
            return r.json().get("entities", {})
    except Exception:
        return {}
    return {}


def _wikidata_search_candidate(name: str) -> List[str]:
    try:
        r = requests.get(
            WIKIDATA_API,
            params={
                "action": "wbsearchentities",
                "search": name,
                "language": "en",
                "type": "item",
                "limit": 5,
                "format": "json",
            },
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if r.status_code == 200:
            data = r.json()
            return [res["id"] for res in data.get("search", []) if res.get("id")]
    except Exception:
        return []
    return []


def _pick_wikidata_qid_by_official_site(candidates: List[str], host: str) -> Optional[str]:
    entities = _wikidata_get_entities(candidates)
    for qid, ent in entities.items():
        claims = ent.get("claims", {})
        if "P856" in claims:
            for cl in claims["P856"]:
                try:
                    url = cl["mainsnak"]["datavalue"]["value"]
                except Exception:
                    continue
                if host in (url or ""):
                    return qid
    return None


def _qid_from_same_as(same_as: List[str]) -> Optional[str]:
    qid: Optional[str] = None
    wiki_url: Optional[str] = None
    for s in same_as:
        if "wikidata.org/entity/" in s:
            m = re.search(r"/(Q\d+)$", s)
            if m:
                return m.group(1)
        if "wikipedia.org/wiki/" in s and not wiki_url:
            wiki_url = s
    if wiki_url:
        try:
            title = wiki_url.split("/wiki/")[-1]
            r = requests.get(
                WIKIPEDIA_API,
                params={
                    "action": "query",
                    "prop": "pageprops",
                    "titles": title,
                    "format": "json",
                },
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if r.status_code == 200:
                pages = r.json().get("query", {}).get("pages", {})
                for _, p in pages.items():
                    qid = p.get("pageprops", {}).get("wikibase_item")
                    if qid:
                        return qid
        except Exception:
            return None
    return None


def _extract_time_year(claims: Dict[str, Any], prop: str) -> str:
    if prop not in claims:
        return ""
    for cl in claims[prop]:
        try:
            time_val = cl["mainsnak"]["datavalue"]["value"]["time"]
            m = re.search(r"(\d{4})", time_val)
            if m:
                return m.group(1)
        except Exception:
            continue
    return ""


def _extract_item_labels(entities: Dict[str, Any], ids: List[str]) -> List[str]:
    labels: List[str] = []
    if not ids:
        return labels
    missing = [i for i in ids if i not in entities]
    if missing:
        more = _wikidata_get_entities(missing)
        entities.update(more)
    for iid in ids:
        ent = entities.get(iid)
        if not ent:
            continue
        lab = ent.get("labels", {}).get("en", {}).get("value")
        if lab:
            labels.append(lab)
    return labels


def _extract_item_ids_from_claim(claims: Dict[str, Any], prop: str) -> List[str]:
    ids: List[str] = []
    if prop not in claims:
        return ids
    for cl in claims[prop]:
        try:
            iid = cl["mainsnak"]["datavalue"]["value"]["id"]
            ids.append(iid)
        except Exception:
            continue
    seen = set()
    out: List[str] = []
    for x in ids:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def enrich_from_wikidata(record: CompanyRecord, same_as: List[str]) -> None:
    host = record.website_link
    # Step 1: try sameAs
    qid = _qid_from_same_as(same_as)
    if qid:
        picked = _pick_wikidata_qid_by_official_site([qid], host) or qid
    else:
        # Step 2: search by company_name and domain brand
        brand_candidates: List[str] = []
        if record.company_name:
            brand_candidates.extend(_wikidata_search_candidate(record.company_name))
        domain_brand = _domain_brand_from_host(host)
        if domain_brand and domain_brand.lower() not in (record.company_name or "").lower():
            brand_candidates.extend(_wikidata_search_candidate(domain_brand))
        # remove duplicates
        unique: List[str] = []
        seen: set = set()
        for c in brand_candidates:
            if c not in seen:
                unique.append(c)
                seen.add(c)
        picked = _pick_wikidata_qid_by_official_site(unique, host)
    if not picked:
        return

    entities = _wikidata_get_entities([picked])
    ent = entities.get(picked, {})
    claims = ent.get("claims", {})

    if not record.founded:
        year = _extract_time_year(claims, "P571")
        if year:
            record.founded = year

    if not record.founders:
        founder_ids = _extract_item_ids_from_claim(claims, "P112")
        founders = _extract_item_labels(entities, founder_ids)
        if founders:
            record.founders = ",".join(founders)

    if not record.headquarters:
        hq_ids = _extract_item_ids_from_claim(claims, "P159")
        hq_labels = _extract_item_labels(entities, hq_ids)
        if hq_labels:
            record.headquarters = ", ".join(hq_labels)

    if not record.industry:
        ind_ids = _extract_item_ids_from_claim(claims, "P452")
        ind_labels = _extract_item_labels(entities, ind_ids)
        if ind_labels:
            record.industry = ",".join(ind_labels)

    if not record.company_size:
        if "P1128" in claims:
            try:
                cl = claims["P1128"][0]
                n = int(cl["mainsnak"]["datavalue"]["value"]["amount"].lstrip("+"))
                record.company_size = bucket_employees(n)
            except Exception:
                pass

    if not record.operating_type:
        dissolved = bool(claims.get("P576"))
        record.operating_type = "Inactive" if dissolved else "Active"

    if not record.company_type or not record.company_sector_type or not record.business_type:
        legal_ids = _extract_item_ids_from_claim(claims, "P1454")
        legal_labels = _extract_item_labels(entities, legal_ids)
        if legal_labels and not record.business_type:
            record.business_type = ",".join(legal_labels)
        if not record.company_sector_type:
            if "P414" in claims or "P249" in claims:
                record.company_sector_type = "Public"
            else:
                record.company_sector_type = "Private"
        if not record.company_type:
            inst_ids = _extract_item_ids_from_claim(claims, "P31")
            inst_labels = [s.lower() for s in _extract_item_labels(entities, inst_ids)]
            record.company_type = "Nonprofit" if any("non-profit" in s or "nonprofit" in s for s in inst_labels) else "For Profit"


def scrape_company(url: str, do_wikidata: bool = True) -> CompanyRecord:
    html = http_get(url)
    if not html:
        return CompanyRecord(website_link=normalize_website_link(url))

    soup = BeautifulSoup(html, "lxml")

    record = CompanyRecord()
    record.website_link = normalize_website_link(url)
    record.company_name = extract_company_name(url, soup)
    record.description = extract_description(soup)

    jsonld_data, same_as = parse_jsonld(soup)
    enrich_from_jsonld(record, jsonld_data)

    candidates = find_candidate_links(
        base_url=url,
        soup=soup,
        keywords=["about", "company", "story", "who we are", "team", "leadership", "contact", "offices", "locations", "legal", "privacy"],
    )

    visited = 0
    aggregated_text = []

    for link in candidates:
        if visited >= 6:
            break
        page = http_get(link)
        if not page:
            continue
        visited += 1
        s = BeautifulSoup(page, "lxml")
        jd, same2 = parse_jsonld(s)
        if jd:
            enrich_from_jsonld(record, jd)
        if same2:
            same_as.extend(same2)
        text = s.get_text("\n", strip=True)
        if text:
            aggregated_text.append(text)
        if not record.about and any(k in link.lower() for k in ["about", "company", "our-story", "who-we-are"]):
            record.about = extract_about_text(s)
        if not record.about:
            paragraphs = [p.get_text(" ", strip=True) for p in s.find_all("p")]
            if len(" ".join(paragraphs)) > 200:
                record.about = extract_about_text(s)

    combined = "\n".join(aggregated_text)

    if not record.founded:
        record.founded = search_for_founded(combined)
    if not record.founders:
        record.founders = search_for_founders(combined)
    if not record.headquarters:
        record.headquarters = search_for_headquarters(combined)

    if do_wikidata:
        enrich_from_wikidata(record, same_as)

    for k, v in asdict(record).items():
        if v is None:
            setattr(record, k, "")

    return record


def load_existing(path: str) -> List[Dict[str, str]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        return []
    return []


def upsert_record(records: List[Dict[str, str]], new_rec: Dict[str, str]) -> List[Dict[str, str]]:
    host = new_rec.get("website_link", "").lower().rstrip("/")
    updated = False
    for i, r in enumerate(records):
        if r.get("website_link", "").lower().rstrip("/") == host:
            records[i] = new_rec
            updated = True
            break
    if not updated:
        records.append(new_rec)
    return records


def to_ordered_dict(rec: CompanyRecord) -> Dict[str, str]:
    return {
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="scrape single URL", default="")
    parser.add_argument("--output", help="output JSON file", default="example_10.json")
    parser.add_argument("--no-wikidata", action="store_true", help="disable Wikidata enrichment")
    args = parser.parse_args()

    default_urls = [
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

    urls: List[str] = [args.url] if args.url else default_urls

    existing = load_existing(args.output)
    for i, url in enumerate(urls, start=1):
        print(f"Scraping {i}/{len(urls)}: {url}")
        rec = scrape_company(url, do_wikidata=not args.no_wikidata)
        ordered = to_ordered_dict(rec)
        if args.url:
            existing = upsert_record(existing, ordered)
        else:
            existing.append(ordered)
        time.sleep(1.0)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(existing)} records to {args.output}")


if __name__ == "__main__":
    main()
