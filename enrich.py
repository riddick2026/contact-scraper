"""Spreadsheet enrichment: load a vendor list, find each vendor's website, scrape it.

Flow per row:
  1. Use the row's website if it has one; otherwise search the web for the vendor name
     (plus any location columns) and pick the vendor's own site, skipping directories.
  2. Scrape that site for contact names, emails, phones and insurance/bond details.
  3. Fill every field, using clear placeholders for anything that could not be found.
"""

from __future__ import annotations

import io
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests

from scraper import HEADERS, INSURANCE_UNKNOWN, normalize_url, scrape_url

# --------------------------------------------------------------------------- #
# Placeholders: chosen so gaps are easy to filter/spot in the output sheet
# --------------------------------------------------------------------------- #

NOT_FOUND = "Not found"              # site loaded, but this field wasn't on it
NO_WEBSITE = "No website"            # no site provided and none found by search
SITE_UNREACHABLE = "Site unreachable"  # site found/provided but could not be loaded
SEARCH_FAILED = "Search failed"      # the web search itself errored (rate limit etc.)

SRC_PROVIDED = "Provided"
SRC_FOUND = "Found by search"
SRC_LOW = "Found by search (verify)"
SRC_NONE = "No match found"
SRC_FAILED = "Search failed"
SRC_NO_NAME = "No vendor name"

OUT_WEBSITE = "Website"
OUT_SOURCE = "Website Source"
OUT_NAME = "Contact Name"
OUT_EMAIL = "Email"
OUT_PHONE = "Phone"
OUT_INSURANCE = "Insurance or Bond"
OUT_STATUS = "Scrape Status"
DATA_FIELDS = [OUT_NAME, OUT_EMAIL, OUT_PHONE, OUT_INSURANCE]

# Values this app writes for gaps: treated as blank when a sheet is re-run
PLACEHOLDERS = {NOT_FOUND, NO_WEBSITE, SITE_UNREACHABLE, SEARCH_FAILED, INSURANCE_UNKNOWN}

# Cells with these values get highlighted in the Excel export
WARN_VALUES = {NOT_FOUND, INSURANCE_UNKNOWN, SRC_LOW}
BAD_VALUES = {NO_WEBSITE, SITE_UNREACHABLE, SEARCH_FAILED, SRC_NONE, SRC_FAILED, SRC_NO_NAME}

# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

GSHEET_RE = re.compile(r"docs\.google\.com/spreadsheets/d/(e/)?([A-Za-z0-9_-]+)")


class LoadError(Exception):
    pass


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.fillna("").astype(str)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.loc[:, [not c.lower().startswith("unnamed:") or df[c].str.strip().ne("").any() for c in df.columns]]
    return df[df.apply(lambda r: r.str.strip().ne("").any(), axis=1)].reset_index(drop=True)


def load_file(name: str, data: bytes) -> dict[str, pd.DataFrame]:
    """Return {sheet name: DataFrame} for a CSV or Excel upload (all values as text)."""
    lower = name.lower()
    try:
        if lower.endswith((".csv", ".txt")):
            for enc in ("utf-8-sig", "cp1252", "latin-1"):
                try:
                    df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False,
                                     encoding=enc, sep=None, engine="python")
                    return {"Sheet1": _clean(df)}
                except UnicodeDecodeError:
                    continue
            raise LoadError("Couldn't read the CSV's text encoding.")
        if lower.endswith((".xlsx", ".xlsm", ".xls")):
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str)
            return {k: _clean(v) for k, v in sheets.items()}
    except LoadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LoadError(f"Couldn't read {name}: {exc}") from exc
    raise LoadError("Upload a .csv, .xlsx or .xls file.")


def gsheet_csv_url(url: str) -> str:
    m = GSHEET_RE.search(url)
    if not m:
        raise LoadError("That doesn't look like a Google Sheets link.")
    published, sheet_id = m.group(1), m.group(2)
    parsed = urlparse(url)
    gid = parse_qs(parsed.query).get("gid", [None])[0] or (
        re.search(r"gid=(\d+)", parsed.fragment).group(1) if "gid=" in parsed.fragment else None)
    base = f"https://docs.google.com/spreadsheets/d/{'e/' if published else ''}{sheet_id}"
    if published:
        return f"{base}/pub?output=csv" + (f"&gid={gid}" if gid else "")
    return f"{base}/export?format=csv" + (f"&gid={gid}" if gid else "")


def load_gsheet(url: str) -> dict[str, pd.DataFrame]:
    export = gsheet_csv_url(url)
    try:
        resp = requests.get(export, headers=HEADERS, timeout=20, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        raise LoadError(f"Couldn't reach Google Sheets: {type(exc).__name__}") from exc
    ctype = resp.headers.get("Content-Type", "")
    if resp.status_code != 200 or "html" in ctype or "accounts.google.com" in resp.url:
        raise LoadError(
            "Google wouldn't share this sheet. In Google Sheets choose Share → General access → "
            "\"Anyone with the link\" (Viewer), or download it as .xlsx/.csv and upload that instead."
        )
    return load_file("sheet.csv", resp.content)


# --------------------------------------------------------------------------- #
# Column detection
# --------------------------------------------------------------------------- #

def _find_col(columns: list[str], hints: list[str]) -> str | None:
    norm = {c: re.sub(r"[^a-z]", " ", c.lower()).strip() for c in columns}
    for hint in hints:                       # exact match first, in priority order
        for c, n in norm.items():
            if n == hint:
                return c
    for hint in hints:                       # then "contains" match
        for c, n in norm.items():
            if re.search(rf"\b{hint}\b", n):
                return c
    return None


def detect_columns(df: pd.DataFrame) -> dict:
    cols = list(df.columns)
    vendor = _find_col(cols, ["vendor name", "vendor", "company name", "business name", "company",
                              "business", "provider name", "provider", "supplier", "contractor",
                              "organization", "name"])
    website = _find_col([c for c in cols if c != vendor],
                        ["website", "web site", "website url", "url", "homepage", "web", "site", "domain"])
    location = [c for c in cols if c not in (vendor, website) and re.search(
        r"\b(city|state|town|location|county|zip|postal)\b", c.lower())]
    return {"vendor": vendor, "website": website, "location": location}


# --------------------------------------------------------------------------- #
# Website discovery
# --------------------------------------------------------------------------- #

DIRECTORY_DOMAINS = {
    "yelp.com", "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "pinterest.com", "bbb.org", "angi.com", "angieslist.com", "homeadvisor.com",
    "thumbtack.com", "houzz.com", "porch.com", "nextdoor.com", "yellowpages.com", "yp.com",
    "superpages.com", "manta.com", "mapquest.com", "google.com", "goo.gl", "bing.com", "apple.com",
    "chamberofcommerce.com", "buildzoom.com", "bizapedia.com", "opencorporates.com", "dnb.com",
    "zoominfo.com", "indeed.com", "glassdoor.com", "wikipedia.org", "crunchbase.com", "bloomberg.com",
    "loc8nearme.com", "citysearch.com", "merchantcircle.com", "birdeye.com", "nicelocal.com",
    "expertise.com", "threebestrated.com", "networx.com", "fixr.com", "homeguide.com", "bark.com",
    "cylex.us.com", "brownbook.net", "hotfrog.com", "alignable.com", "trustpilot.com", "reddit.com",
    "quora.com", "amazon.com", "ebay.com", "craigslist.org", "zillow.com", "realtor.com",
    "texas.gov", "sos.state.tx.us", "tdlr.texas.gov", "usa.gov", "opengovus.com", "corporationwiki.com",
    "buzzfile.com", "signalhire.com", "rocketreach.co", "apollo.io", "allbiz.com", "showmelocal.com",
    "dexknows.com", "local.com", "porch.com", "angi.com", "procore.com", "contractorsdirectory.com",
}
NAME_STOPWORDS = {"llc", "inc", "co", "corp", "corporation", "company", "the", "and", "of", "ltd",
                  "lp", "llp", "pllc", "pc", "group", "services", "service", "enterprises", "dba",
                  "incorporated", "limited", "a", "an", "&"}


def _registered_domain(host: str) -> str:
    host = host.lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "us"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_directory(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    reg = _registered_domain(host)
    return reg in DIRECTORY_DOMAINS or host.endswith((".gov", ".edu")) or "google." in host


def _name_tokens(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    return [w for w in words if w not in NAME_STOPWORDS and len(w) > 1] or words


def score_candidate(vendor: str, url: str, title: str) -> tuple[float, float]:
    """(domain score, title score) in [0, 1]: how well a search result matches the vendor."""
    tokens = _name_tokens(vendor)
    if not tokens:
        return 0.0, 0.0
    root = _registered_domain(urlparse(url).hostname or "").split(".")[0]
    domain_hits = sum(t in root for t in tokens)
    initials = "".join(t[0] for t in tokens)
    domain = domain_hits / len(tokens)
    if len(tokens) >= 2 and root.startswith(initials):
        domain = max(domain, 0.6)
    # Whole name squashed together, e.g. "abcplumbing" for "A B C Plumbing"
    if "".join(tokens) in root:
        domain = 1.0
    title_words = set(re.findall(r"[a-z0-9]+", title.lower()))
    title_score = sum(t in title_words for t in tokens) / len(tokens)
    return domain, title_score


def _homepage(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}/"


class SearchError(Exception):
    pass


search_errors: dict[str, str] = {}  # vendor -> last search error, for the status column


def _ddg_html(query: str) -> list[dict]:
    """DuckDuckGo's no-JavaScript results page, fetched directly."""
    resp = requests.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "us-en"},
                         headers=HEADERS, timeout=10)
    resp.raise_for_status()
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if "uddg=" in href:
            href = parse_qs(urlparse(href).query).get("uddg", [""])[0]
        if href.startswith("http") and "duckduckgo.com/y.js" not in href:
            out.append({"url": href, "title": a.get_text(" ", strip=True)})
    if not out and ("anomaly" in resp.text.lower() or "challenge" in resp.text.lower()):
        raise SearchError("DuckDuckGo asked for a bot check")
    return out


def _bing_html(query: str) -> list[dict]:
    """Bing's results page, fetched directly."""
    import base64
    from bs4 import BeautifulSoup
    resp = requests.get("https://www.bing.com/search",
                        params={"q": query, "setlang": "en-US", "cc": "US", "count": 15},
                        headers=HEADERS, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for a in soup.select("li.b_algo h2 a"):
        href = a.get("href", "")
        if "bing.com/ck/a" in href:  # tracking redirect: real URL is base64 in the "u" parameter
            u = parse_qs(urlparse(href).query).get("u", [""])[0]
            if u.startswith("a1"):
                try:
                    href = base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
                except Exception:  # noqa: BLE001
                    continue
        if href.startswith("http"):
            out.append({"url": href, "title": a.get_text(" ", strip=True)})
    return out


def _ddgs_lib(query: str) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore  # older package name
        except ImportError as exc:
            raise SearchError("ddgs package not installed") from exc
    results = DDGS().text(query, max_results=10, region="us-en")
    return [{"url": r.get("href", ""), "title": r.get("title", "")} for r in results or []]


class WebSearch:
    """Rate-limited web search with fallbacks.

    Brave Search API (if a key is given) → ddgs library → DuckDuckGo HTML → Bing HTML.
    An engine that errors is skipped for the rest of the run after repeated failures.
    """

    def __init__(self, brave_api_key: str = "", min_interval: float = 1.0):
        self.brave_key = brave_api_key.strip()
        self.min_interval = 0.4 if self.brave_key else min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self.failures: dict[str, int] = {}
        self.last_engine = ""
        self.engines = ([("Brave", self._brave)] if self.brave_key else []) + [
            ("ddgs", _ddgs_lib), ("DuckDuckGo", _ddg_html), ("Bing", _bing_html)]

    def _wait_turn(self) -> None:
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()

    def _brave(self, query: str) -> list[dict]:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 10, "country": "us"},
            headers={"Accept": "application/json", "X-Subscription-Token": self.brave_key},
            timeout=10,
        )
        if resp.status_code in (401, 403, 422):
            raise SearchError("Brave API key rejected")
        if resp.status_code == 429:
            raise SearchError("Brave rate limit")
        resp.raise_for_status()
        return [{"url": r.get("url", ""), "title": r.get("title", "")}
                for r in resp.json().get("web", {}).get("results", [])]

    # kept for tests/back-compat: the first non-Brave engine
    def _ddg(self, query: str) -> list[dict]:
        return _ddgs_lib(query)

    def search(self, query: str) -> list[dict]:
        errors = []
        for attempt in range(2):
            for name, engine in self.engines:
                if self.failures.get(name, 0) >= 3:
                    continue  # this engine keeps failing on this network; stop trying it
                self._wait_turn()
                try:
                    results = (self._ddg(query) if name == "ddgs" else engine(query))
                except Exception as exc:  # noqa: BLE001 - try the next engine
                    with self._lock:
                        self.failures[name] = self.failures.get(name, 0) + 1
                    msg = str(exc).strip().splitlines()[0][:80] if str(exc).strip() else ""
                    errors.append(f"{name}: {type(exc).__name__}{' ' + msg if msg else ''}")
                    continue
                with self._lock:
                    self.failures[name] = 0
                    self.last_engine = name
                if results:
                    return results
            if not errors:
                return []  # every engine answered with no results
            time.sleep(2 * (attempt + 1))
        raise SearchError("; ".join(dict.fromkeys(errors))[:300] or "all search engines failed")


def find_website(search: WebSearch, vendor: str, location: str = "") -> tuple[str, str]:
    """Return (homepage url or '', source label). On failure the reason is in search.errors."""
    query = f"{vendor} {location}".strip()
    try:
        results = search.search(query)
    except SearchError as exc:
        search_errors[vendor] = str(exc)
        return "", SRC_FAILED

    best, best_key = None, (-1.0, -1.0)
    for rank, r in enumerate(results):
        url, title = r["url"], r["title"]
        if not url.startswith("http") or _is_directory(url):
            continue
        domain, title_score = score_candidate(vendor, url, title)
        key = (domain + 0.5 * title_score - 0.02 * rank, domain)
        if key > best_key:
            best, best_key = (url, domain, title_score), key
    if not best:
        return "", SRC_NONE
    url, domain, title_score = best
    if domain >= 0.5:
        return _homepage(url), SRC_FOUND
    if title_score >= 0.6:
        return _homepage(url), SRC_LOW
    return "", SRC_NONE


# --------------------------------------------------------------------------- #
# Row enrichment
# --------------------------------------------------------------------------- #

@dataclass
class EnrichConfig:
    vendor_col: str
    website_col: str | None
    location_cols: list[str]


def enrich_row(row: dict, cfg: EnrichConfig, search: WebSearch) -> dict:
    """Return the output fields for one input row."""
    vendor = str(row.get(cfg.vendor_col, "")).strip()
    given = str(row.get(cfg.website_col, "")).strip() if cfg.website_col else ""
    if given in PLACEHOLDERS:
        given = ""
    location = " ".join(str(row.get(c, "")).strip() for c in cfg.location_cols).strip()
    out: dict[str, str] = {}

    url = normalize_url(given) if given else None
    if url:
        source = SRC_PROVIDED
    elif vendor:
        url, source = find_website(search, vendor, location)
    else:
        url, source = "", SRC_NO_NAME
    out[OUT_SOURCE] = source

    if not url:
        filler = SEARCH_FAILED if source == SRC_FAILED else NO_WEBSITE
        out[OUT_WEBSITE] = given or filler
        out.update({f: filler for f in DATA_FIELDS})
        out[OUT_STATUS] = {SRC_FAILED: "Search failed - " + search_errors.get(vendor, "try again later"),
                           SRC_NO_NAME: "Skipped - no vendor name or website"}.get(source, "No website found")
        return out

    out[OUT_WEBSITE] = given if source == SRC_PROVIDED else url
    result = scrape_url(url)
    if not result.status.startswith("OK"):
        out.update({f: SITE_UNREACHABLE for f in DATA_FIELDS})
        out[OUT_STATUS] = result.status
        return out

    out[OUT_NAME] = ", ".join(sorted(result.names)) or NOT_FOUND
    out[OUT_EMAIL] = ", ".join(sorted(result.emails)) or NOT_FOUND
    out[OUT_PHONE] = ", ".join(sorted(result.phones)) or NOT_FOUND
    out[OUT_INSURANCE] = result.insurance_summary()  # "Unknown" when nothing found
    missing = [f for f in DATA_FIELDS if out[f] in (NOT_FOUND, INSURANCE_UNKNOWN)]
    out[OUT_STATUS] = "Complete" if not missing else "Partial - missing " + ", ".join(missing)
    return out


def _match_output_col(field: str, norm: str) -> bool:
    if field == OUT_EMAIL:
        return "email" in norm or norm == "mail"
    if field == OUT_PHONE:
        return any(k in norm for k in ("phone", "telephone", "mobile", "cell")) or norm in ("tel", "number")
    if field == OUT_NAME:
        return norm in ("contact", "contactname", "contactperson", "name") or (
            "contact" in norm and "name" in norm) or norm in ("owner", "ownername", "primarycontact")
    if field == OUT_INSURANCE:
        return "insurance" in norm or "bond" in norm
    if field == OUT_WEBSITE:
        return norm in ("website", "websiteurl", "url", "site", "web", "homepage", "domain")
    if field == OUT_SOURCE:
        return norm == "websitesource"
    if field == OUT_STATUS:
        return norm == "scrapestatus"
    return False


def output_columns(df: pd.DataFrame, cfg: EnrichConfig) -> dict[str, str]:
    """Map each output field to the sheet column it goes in (existing column when one fits)."""
    taken = {cfg.vendor_col, *cfg.location_cols}
    mapping: dict[str, str] = {}
    for field in [OUT_WEBSITE, OUT_SOURCE, OUT_STATUS, OUT_EMAIL, OUT_PHONE, OUT_INSURANCE, OUT_NAME]:
        if field == OUT_WEBSITE and cfg.website_col:
            mapping[field] = cfg.website_col
            taken.add(cfg.website_col)
            continue
        match = next((c for c in df.columns if c not in taken
                      and _match_output_col(field, re.sub(r"[^a-z]", "", c.lower()))), None)
        mapping[field] = match or field
        if match:
            taken.add(match)
    return mapping


def merge_row(row: dict, out: dict, mapping: dict[str, str]) -> dict:
    """Write results into the row, keeping any values the sheet already had."""
    merged = dict(row)
    for field, value in out.items():
        col = mapping[field]
        current = str(merged.get(col, "")).strip()
        if field in (OUT_SOURCE, OUT_STATUS) or not current or current in PLACEHOLDERS:
            merged[col] = value
    return merged


def ordered_columns(df: pd.DataFrame, mapping: dict[str, str]) -> list[str]:
    cols = list(df.columns)
    order = [OUT_WEBSITE, OUT_SOURCE, OUT_NAME, OUT_EMAIL, OUT_PHONE, OUT_INSURANCE, OUT_STATUS]
    return cols + [mapping[f] for f in order if mapping[f] not in cols]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def to_highlighted_excel(df: pd.DataFrame, sheet_name: str = "Vendors") -> bytes:
    from openpyxl.styles import Font, PatternFill

    warn = PatternFill("solid", fgColor="FFF2CC")   # light yellow
    bad = PatternFill("solid", fgColor="F8CBAD")    # light red
    found = PatternFill("solid", fgColor="E2EFDA")  # light green: filled in by the app
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31] or "Vendors")
        ws = writer.sheets[sheet_name[:31] or "Vendors"]
        for idx, col in enumerate(df.columns, start=1):
            letter = ws.cell(row=1, column=idx).column_letter
            width = max([len(str(col)), *df[col].astype(str).str.len().tolist()] or [10])
            ws.column_dimensions[letter].width = min(width + 2, 60)
            ws.cell(row=1, column=idx).font = Font(bold=True)
        for r, row in enumerate(df.itertuples(index=False), start=2):
            for c, value in enumerate(row, start=1):
                v = str(value)
                if v in BAD_VALUES:
                    ws.cell(row=r, column=c).fill = bad
                elif v in WARN_VALUES or v.startswith("Partial"):
                    ws.cell(row=r, column=c).fill = warn
                elif v == SRC_FOUND:
                    ws.cell(row=r, column=c).fill = found
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions

        legend = writer.book.create_sheet("Legend")
        rows = [
            ("Value", "Meaning"),
            (NOT_FOUND, "The website loaded, but this detail isn't on it."),
            (INSURANCE_UNKNOWN, "The website doesn't mention insurance, bonding or a license."),
            (NO_WEBSITE, "No website was in the sheet and none could be found by search."),
            (SITE_UNREACHABLE, "A website was found/provided but didn't load (down, blocked or timed out)."),
            (SEARCH_FAILED, "The web search errored (usually rate limiting). Re-run these rows."),
            (SRC_FOUND, "Website found by searching the vendor name; the domain matches the name."),
            (SRC_LOW, "Website found by search but the domain doesn't clearly match. Check it."),
            (SRC_PROVIDED, "Website came from your sheet."),
        ]
        for i, (a, b) in enumerate(rows, start=1):
            legend.cell(row=i, column=1, value=a)
            legend.cell(row=i, column=2, value=b)
            if a in BAD_VALUES:
                legend.cell(row=i, column=1).fill = bad
            elif a in WARN_VALUES:
                legend.cell(row=i, column=1).fill = warn
            elif a == SRC_FOUND:
                legend.cell(row=i, column=1).fill = found
        legend.cell(row=1, column=1).font = legend.cell(row=1, column=2).font = Font(bold=True)
        legend.column_dimensions["A"].width = 26
        legend.column_dimensions["B"].width = 80
    return buffer.getvalue()
