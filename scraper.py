"""Contact-info scraping engine: emails, phone numbers and contact names."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    import lxml  # noqa: F401

    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

REQUEST_TIMEOUT = 5  # seconds, per request
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# --------------------------------------------------------------------------- #
# Regex patterns
# --------------------------------------------------------------------------- #

EMAIL_RE = re.compile(
    r"(?<![\w.+-])"                       # not preceded by an email char
    r"[A-Za-z0-9._%+-]{1,64}"             # local part
    r"@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"  # subdomains
    r"[A-Za-z]{2,24}"                     # TLD
    r"(?![\w-])"
)

# Common human obfuscations: "name [at] domain [dot] com", "name (at) domain.com"
OBFUSCATED_AT = re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*|\s+at\s+(?=[\w-]+\s*(?:[\[\(\{]\s*dot|\.))", re.I)
OBFUSCATED_DOT = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I)

# File-like "emails" such as logo@2x.png that are really asset names
EMAIL_JUNK_SUFFIX = re.compile(r"\.(png|jpe?g|gif|svg|webp|bmp|ico|css|js|woff2?|ttf|mp4|pdf)$", re.I)
EMAIL_JUNK_DOMAINS = ("example.com", "example.org", "domain.com", "email.com", "sentry.io",
                      "wixpress.com", "sentry-next.wixpress.com", "yourdomain.com")

PHONE_RE = re.compile(
    r"""
    (?<![\w/.-])                               # not glued to other tokens / URLs
    (?:
        # International: +44 20 7946 0958, +1-202-555-0143, +49 (0)30 123456
        \+\d{1,3}[\s.-]?(?:\(0\)[\s.-]?)?(?:\(?\d{1,4}\)?[\s.-]?){1,4}\d{2,4}
      |
        # North American: (202) 555-0143, 202-555-0143, 202.555.0143, 1-800-555-0199
        (?:1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{4}
      |
        # Generic national with leading 0: 020 7946 0958, 030-123456
        0\d{1,4}[\s.-]\d{2,4}(?:[\s.-]\d{2,4}){1,2}
    )
    (?:\s*(?:ext\.?|x|extension)\s*\d{1,5})?   # optional extension
    (?![\w/-])
    """,
    re.X | re.I,
)

# --------------------------------------------------------------------------- #
# Contact-name heuristics
# --------------------------------------------------------------------------- #

NAME_TOKEN = r"[A-Z][a-z'’-]+|[A-Z]\.|[A-Z][a-z]+-[A-Z][a-z]+|Mc[A-Z][a-z]+|O'[A-Z][a-z]+"
# Names never span lines, so only spaces/tabs may separate the parts
FULL_NAME = rf"(?:(?:Dr|Mr|Mrs|Ms|Prof)\.?[ \t]+)?(?:{NAME_TOKEN})(?:[ \t]+(?:{NAME_TOKEN})){{1,2}}"

TITLES = (
    r"CEO|CTO|CFO|COO|CMO|Founder|Co-Founder|Co-founder|Owner|President|Vice President|VP|"
    r"Director|Managing Director|Manager|Partner|Principal|Head of [A-Z][a-z]+|"
    r"Chief [A-Z][a-z]+ Officer|Chairman|Editor|Coordinator|Representative|Agent|Broker"
)

NAME_PATTERNS = [
    # "Contact: Jane Smith" / "Contact Person - Jane Smith" / "Name: Jane Smith"
    re.compile(rf"(?i:contact(?:\s+person)?|attn|attention|name|ask\s+for|speak\s+(?:to|with))\s*[:\-–]\s*({FULL_NAME})"),
    # "Jane Smith, CEO" / "Jane Smith - Founder" / "Jane Smith | Owner"
    re.compile(rf"({FULL_NAME})\s*[,\-–|]\s*(?:the\s+)?(?:{TITLES})\b"),
    # "CEO: Jane Smith" / "Founder Jane Smith" / "Owner - Jane Smith"
    re.compile(rf"\b(?:{TITLES})\s*[:\-–,]?\s+({FULL_NAME})"),
    # "Contact Jane Smith at", "email Jane Smith"
    re.compile(rf"\b(?i:contact|email|call)\s+({FULL_NAME})\s+(?:at|on|via)\b"),
]

# Words that look Capitalised but are never part of a person's name
NAME_STOPWORDS = {
    "contact", "us", "about", "home", "our", "the", "team", "company", "services", "privacy",
    "policy", "terms", "learn", "more", "read", "click", "here", "email", "phone", "call",
    "office", "support", "sales", "info", "customer", "service", "get", "in", "touch", "send",
    "message", "submit", "form", "inc", "llc", "ltd", "group", "street", "road", "avenue",
    "suite", "floor", "new", "york", "san", "francisco", "los", "angeles", "united", "states",
    "kingdom", "copyright", "all", "rights", "reserved", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december", "chief",
    "officer", "director", "manager", "founder", "president", "executive", "head", "of", "and",
    "for", "with", "board", "meet", "leadership", "management", "view", "profile", "linkedin",
    "twitter", "facebook", "instagram", "follow", "subscribe", "newsletter", "blog", "news",
    "careers", "jobs", "press", "media", "partner", "partners", "owner", "vp", "ceo", "cto",
}

CONTACT_LINK_RE = re.compile(r"contact|get[\s_-]?in[\s_-]?touch|reach[\s_-]?us|kontakt|contacto", re.I)
ABOUT_LINK_RE = re.compile(r"about|team|who[\s_-]?we[\s_-]?are|our[\s_-]?story|impressum", re.I)

# Optional NLP: use spaCy NER if the user has it installed, otherwise patterns only.
try:  # pragma: no cover - optional dependency
    import spacy

    _NLP = spacy.load("en_core_web_sm", disable=["parser", "lemmatizer"])
except Exception:  # noqa: BLE001 - any import/model failure just disables NER
    _NLP = None


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #

@dataclass
class ScrapeResult:
    url: str
    status: str = "OK"
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    pages_scraped: list[str] = field(default_factory=list)

    def has_contact_info(self) -> bool:
        return bool(self.emails or self.phones)

    def as_row(self) -> dict:
        return {
            "URL": self.url,
            "Status": self.status,
            "Emails": ", ".join(sorted(self.emails)),
            "Phone Numbers": ", ".join(sorted(self.phones)),
            "Contact Names": ", ".join(sorted(self.names)),
            "Pages Scraped": " | ".join(self.pages_scraped),
        }


# --------------------------------------------------------------------------- #
# Extraction helpers
# --------------------------------------------------------------------------- #

def normalize_url(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = parsed.hostname or ""
    if " " in parsed.netloc or not ("." in host or host == "localhost"):
        return None
    return raw


def _clean_email(email: str) -> str | None:
    email = email.strip().strip(".").lower()
    if EMAIL_JUNK_SUFFIX.search(email):
        return None
    domain = email.rsplit("@", 1)[-1]
    if any(domain == d or domain.endswith("." + d) for d in EMAIL_JUNK_DOMAINS):
        return None
    # Hashes from tracking scripts e.g. 3f2a9c...@sentry.io
    local = email.split("@", 1)[0]
    if len(local) >= 24 and re.fullmatch(r"[0-9a-f]+", local):
        return None
    return email


def extract_emails(soup: BeautifulSoup, text: str) -> set[str]:
    found: set[str] = set()

    for a in soup.select('a[href^="mailto:" i]'):
        addr = a.get("href", "")[7:].split("?", 1)[0]
        for m in EMAIL_RE.findall(requests.utils.unquote(addr)):
            found.add(m)

    found.update(EMAIL_RE.findall(text))

    deobfuscated = OBFUSCATED_DOT.sub(".", OBFUSCATED_AT.sub("@", text))
    if deobfuscated != text:
        found.update(EMAIL_RE.findall(deobfuscated))

    return {e for e in (_clean_email(x) for x in found) if e}


EXTENSION_RE = re.compile(r"(?i)\s*(?:ext\.?|x|extension)\s*\d+$")


def _phone_digits(raw: str) -> str:
    return re.sub(r"\D", "", EXTENSION_RE.sub("", raw))


def _clean_phone(raw: str) -> str | None:
    digits = _phone_digits(raw)
    if not 7 <= len(digits) <= 15:
        return None
    if len(set(digits)) <= 2:  # 0000000, 1111111111 etc.
        return None
    # Reject things that look like dates/years ranges, e.g. 2019-2024
    if re.fullmatch(r"(19|20)\d{2}[\s.-]+(19|20)\d{2}", raw.strip()):
        return None
    return re.sub(r"\s+", " ", raw.strip())


def extract_phones(soup: BeautifulSoup, text: str) -> set[str]:
    found: set[str] = set()
    for a in soup.select('a[href^="tel:" i]'):
        tel = requests.utils.unquote(a.get("href", "")[4:]).strip()
        if tel:
            found.add(tel)
    found.update(m.group(0) for m in PHONE_RE.finditer(text))

    # De-duplicate by digit signature, keeping the nicest-looking formatting
    by_digits: dict[str, str] = {}
    for p in found:
        cleaned = _clean_phone(p)
        if not cleaned:
            continue
        key = _phone_digits(cleaned)[-10:]
        if key not in by_digits or len(cleaned) > len(by_digits[key]):
            by_digits[key] = cleaned
    return set(by_digits.values())


def _valid_name(name: str) -> str | None:
    name = re.sub(r"\s+", " ", name).strip(" ,.-–|")
    parts = name.replace(".", ". ").split()
    words = [w.strip(".").lower() for w in parts if w.strip(".")]
    if len(words) < 2 or len(words) > 4:
        return None
    if any(w in NAME_STOPWORDS for w in words if w not in {"dr", "mr", "mrs", "ms", "prof"}):
        return None
    if len(name) > 40:
        return None
    return name


def _names_from_structured_data(soup: BeautifulSoup) -> set[str]:
    names: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Person" in types and isinstance(node.get("name"), str):
                names.add(node["name"])
            for key in ("founder", "employee", "author", "contactPoint", "member"):
                if key in node:
                    walk(node[key])
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue

    # Microdata: itemprop="name" inside itemtype=Person
    for person in soup.select('[itemtype*="schema.org/Person"]'):
        el = person.select_one('[itemprop="name"]')
        if el:
            names.add(el.get_text(" ", strip=True))

    # <meta name="author">
    meta = soup.find("meta", attrs={"name": re.compile("^author$", re.I)})
    if meta and meta.get("content"):
        names.add(meta["content"])

    # vCard / hCard microformat
    for el in soup.select(".vcard .fn, .h-card .p-name"):
        names.add(el.get_text(" ", strip=True))

    return names


def extract_names(soup: BeautifulSoup, text: str) -> set[str]:
    candidates = set(_names_from_structured_data(soup))

    for pattern in NAME_PATTERNS:
        candidates.update(m.group(1) for m in pattern.finditer(text))

    # Common team-card markup: <h3>Jane Smith</h3><p>CEO</p>
    for el in soup.select('[class*="team" i] h2, [class*="team" i] h3, [class*="team" i] h4, '
                          '[class*="staff" i] h3, [class*="member" i] h3, [class*="person" i] h3, '
                          '[class*="author" i] [class*="name" i], [class*="contact" i] [class*="name" i]'):
        t = el.get_text(" ", strip=True)
        if re.fullmatch(FULL_NAME, t):
            candidates.add(t)

    # Optional spaCy NER on contact-ish sentences only (keeps noise and cost down)
    if _NLP is not None:
        snippets = [s for s in re.split(r"(?<=[.!?\n])\s+", text)
                    if re.search(r"contact|email|call|phone|founder|ceo|owner|director|manager", s, re.I)]
        doc = _NLP(" ".join(snippets)[:20000])
        candidates.update(ent.text for ent in doc.ents if ent.label_ == "PERSON")

    return {n for n in (_valid_name(c) for c in candidates) if n}


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _same_domain(a: str, b: str) -> bool:
    strip = lambda h: h.lower().removeprefix("www.")  # noqa: E731
    return strip(urlparse(a).netloc) == strip(urlparse(b).netloc)


def find_contact_link(soup: BeautifulSoup, base_url: str) -> str | None:
    """Return the best 'Contact' link on the same domain, falling back to 'About'."""
    contact, about = None, None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#", 1)[0]
        if not _same_domain(absolute, base_url) or absolute.rstrip("/") == base_url.rstrip("/"):
            continue
        label = f"{a.get_text(' ', strip=True)} {href}"
        if contact is None and CONTACT_LINK_RE.search(label):
            contact = absolute
        elif about is None and ABOUT_LINK_RE.search(label):
            about = absolute
        if contact:
            break
    return contact or about


# --------------------------------------------------------------------------- #
# Fetch + scrape
# --------------------------------------------------------------------------- #

def fetch(session: requests.Session, url: str) -> tuple[str, str]:
    """Fetch a page, returning (final_url, html). Raises requests exceptions on failure."""
    resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype and "xml" not in ctype and ctype:
        raise requests.exceptions.ContentDecodingError(f"Non-HTML content: {ctype}")
    resp.encoding = resp.encoding or resp.apparent_encoding
    return resp.url, resp.text


def _scrape_page(result: ScrapeResult, url: str, html: str) -> BeautifulSoup:
    link_soup = BeautifulSoup(html, PARSER)
    text = _visible_text(BeautifulSoup(html, PARSER))
    # mailto/tel/ld+json live in markup, so extract from the untouched copy
    result.emails |= extract_emails(link_soup, text)
    result.phones |= extract_phones(link_soup, text)
    result.names |= extract_names(link_soup, text)
    result.pages_scraped.append(url)
    return link_soup


def scrape_url(raw_url: str) -> ScrapeResult:
    """Scrape one site. Never raises: failures are reported in ``status``."""
    url = normalize_url(raw_url)
    result = ScrapeResult(url=raw_url.strip())
    if url is None:
        result.status = "Invalid URL"
        return result

    with requests.Session() as session:
        try:
            try:
                final_url, html = fetch(session, url)
            except requests.exceptions.SSLError:
                # Some small-business sites only serve plain HTTP
                final_url, html = fetch(session, url.replace("https://", "http://", 1))
        except requests.exceptions.Timeout:
            result.status = "Connection Error (timeout)"
            return result
        except requests.exceptions.HTTPError as exc:
            result.status = f"Connection Error (HTTP {exc.response.status_code})"
            return result
        except requests.exceptions.RequestException:
            result.status = "Connection Error"
            return result
        except Exception as exc:  # noqa: BLE001 - never crash the app
            result.status = f"Error: {type(exc).__name__}"
            return result

        try:
            soup = _scrape_page(result, final_url, html)

            if not result.has_contact_info():
                follow = find_contact_link(soup, final_url)
                if follow:
                    try:
                        sub_url, sub_html = fetch(session, follow)
                        _scrape_page(result, sub_url, sub_html)
                    except requests.exceptions.RequestException:
                        result.status = "OK (contact page failed to load)"
        except Exception as exc:  # noqa: BLE001 - parsing problems shouldn't crash the app
            result.status = f"Parse Error: {type(exc).__name__}"
            return result

    if result.status == "OK" and not (result.emails or result.phones or result.names):
        result.status = "OK (no contact info found)"
    return result

