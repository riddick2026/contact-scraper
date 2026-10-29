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

REQUEST_TIMEOUT = 10  # seconds, per request
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
                      "wixpress.com", "sentry-next.wixpress.com", "yourdomain.com",
                      # website-builder placeholders and third-party services embedded in sites
                      "mysite.com", "yoursite.com", "website.com", "godaddy.com", "wix.com", "squarespace.com",
                      "weebly.com", "wisetack.com", "mytangledweb.com", "hibu.com", "scorpion.co")
# Local parts that are never a business's contact address (legal pages, bounces, placeholders)
EMAIL_JUNK_LOCAL = re.compile(r"^(filler|terms|privacy|legal|abuse|noreply|no-reply|donotreply|do-not-reply|"
                              r"postmaster|mailer-daemon|example|test|user|username|name|email|your-?email|"
                              r"johndoe|john\.doe|janedoe|webmaster|webmasta)$", re.I)

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
    # "founded by Jane Smith", "owned and operated by Jane Smith"
    re.compile(rf"\b(?i:founded|started|established|owned(?:\s+and\s+operated)?|run|led)\s+(?i:by)\s+({FULL_NAME})"),
    # "Hi, I'm Jane Smith", "My name is Jane Smith", "Meet Jane Smith"
    re.compile(rf"(?i:\bmy\s+name\s+is|\bhi,?\s+i['’]?m|\bi\s+am|\bmeet)\s+({FULL_NAME})"),
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
    # form labels and placeholders: "First Last", "Full Name", "Your Name"
    "first", "last", "full", "name", "names", "your", "my",
    # page headings and phrases that look like names
    "general", "questions", "question", "calculator", "mortgage", "community", "involvement", "plan",
    "membership", "member", "members", "elementary", "school", "human", "resource", "resources",
    "international", "business", "design", "designs", "luxurious", "preferred", "premium", "quality",
    "control", "contractor", "contractors", "every", "job", "see", "it", "first", "gold", "elite",
    "emerald", "platinum", "diamond", "certified", "award", "awards", "winner", "financing", "warranty",
    "gallery", "testimonials", "projects", "portfolio", "faq", "faqs", "schedule", "appointment",
    # place-name words: "Citrus Heights", "Montgomery Village"
    "heights", "village", "grange", "city", "county", "park", "lake", "beach", "springs", "valley",
    "hills", "creek", "ridge", "harbor", "island", "mount", "mountain", "bay", "falls", "grove",
    "junction", "sound", "puget", "seattle", "tacoma", "spokane", "vancouver", "portland", "olympia",
}

CONTACT_LINK_RE = re.compile(r"contact|get[\s_-]?in[\s_-]?touch|reach[\s_-]?us|kontakt|contacto", re.I)
ABOUT_LINK_RE = re.compile(r"about|team|who[\s_-]?we[\s_-]?are|our[\s_-]?story|impressum", re.I)
INSURANCE_LINK_RE = re.compile(r"licen[cs]|insur|bond|credential|accredit|certification", re.I)
INSURANCE_FALLBACK_LINK_RE = re.compile(r"about|why[\s_-]?(?:choose|us)|who[\s_-]?we[\s_-]?are|our[\s_-]?company", re.I)

# --------------------------------------------------------------------------- #
# Insurance / bond / licence heuristics
# --------------------------------------------------------------------------- #

INSURANCE_UNKNOWN = "Unknown"

_LBI = r"(licensed|bonded|insured)"
_JOIN = r"\s*(?:,|&|\+|/|\band\b)?\s*(?:fully\s+)?"
# "Licensed, Bonded & Insured", "fully insured", "Bonded and Insured"
INSURANCE_STATUS_RE = re.compile(
    rf"\b((?:fully|properly)\s+)?{_LBI}(?:{_JOIN}{_LBI})?(?:{_JOIN}{_LBI})?\b", re.I
)
# A lone "licensed"/"insured" only counts when it is clearly about the business
_STATUS_BEFORE = re.compile(r"(?:\bwe\s+are|\bwe'?re|\bwe’re|\bare|\bis|\bcompany|\bbusiness)\s*$", re.I)
_STATUS_AFTER = re.compile(
    r"^\s+(?:and\s+\w+\s+)?(?:\w+\s+)?(?:contractors?|company|business|professionals?|technicians?|"
    r"plumbers?|electricians?|roofers?|movers?|team|crews?|staff|installers?|pros?|experts?|"
    r"specialists?|tradesmen|in\s+(?:the\s+)?(?:state|texas|[A-Z][a-z]+))\b",
    re.I,
)

# Licence / registration / bond numbers: "TX License #TACLA12345C", "Lic. No: 123456",
# "Bond #SB-99812", "Registration Number: 4471", "USDOT 1234567"
_ID = r"([A-Z0-9][A-Z0-9-]{2,19})"
LICENSE_LABELED_RE = re.compile(
    r"(?:\b(?:[A-Z]{2,5})\s+)?"                                        # TX, TDLR, CSLB
    r"(?i:(?:state\s+|contractor'?s?\s+|master\s+|electrical\s+|plumbing\s+|hvac\s+|business\s+)?"
    r"(?:licen[cs]e|lic\.?|registration|reg\.?|bond|insurance\s+policy)"
    r"\s*(?:no\.?|number|num\.?|#|:)\s*[:.#]?\s*)"
    + _ID
)
LICENSE_PREFIX_RE = re.compile(
    r"\b(?:TACLA|TECL|RMP|TDLR|CSLB|ROC)\s*[#:-]?\s*\d{3,8}[A-Z]?\b"
    r"|\b(?:US\s?DOT|USDOT)\s*(?:No\.?|#|Number)?\s*:?\s*\d{4,8}\b"
    r"|\bMC\s*(?:No\.?|#|Number)\s*:?\s*\d{5,8}\b"
)

COVERAGE_PATTERNS = [
    ("General liability insurance", re.compile(r"\bgeneral\s+liability\b", re.I)),
    ("Liability insurance", re.compile(
        r"\b(?:business|public|commercial)\s+liability\b|\bliability\s+(?:insurance|coverage)\b", re.I)),
    ("Professional liability (E&O)", re.compile(
        r"\bprofessional\s+liability\b|\berrors\s+(?:and|&)\s+omissions\b", re.I)),
    ("Workers' compensation", re.compile(r"\bworkers?['’]?s?['’]?\s+comp(?:ensation)?\b", re.I)),
    ("Surety bond", re.compile(
        r"\bsurety\s+bond(?:ed)?\b|\b(?:license|contractor['’]?s?|performance|payment)\s+bond\b", re.I)),
    ("Certificate of insurance", re.compile(r"\bcertificates?\s+of\s+insurance\b", re.I)),
]
_MONEY = r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|mil|M|K)\b)?"
COVERAGE_AMOUNT_RES = [
    re.compile(rf"({_MONEY})\s+(?:in\s+|of\s+)?(?:general\s+)?(?:liability|insurance|coverage|bond)", re.I),
    re.compile(rf"\b(?:insured|coverage|liability|bonded)\s+(?:up\s+to|for|of|to)\s+({_MONEY})", re.I),
]
# Mentions of *customers'* insurance (claims, adjusters) or legal services, not the provider's
COVERAGE_EXCLUDE_RE = re.compile(
    r"claims?\b|adjuster|deductible|accept\w*\s+(?:all\s+)?insurance|insurance\s+(?:compan|carrier|provider|work|job)|"
    r"lawyer|attorney|law\s+firm|injur|lawsuit",
    re.I,
)

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
    insurance_terms: set[str] = field(default_factory=set)        # licensed / bonded / insured
    insurance_details: dict[str, None] = field(default_factory=dict)  # ordered set of findings
    pages_scraped: list[str] = field(default_factory=list)
    text: str = ""  # visible text of scraped pages, used to sanity-check search matches

    def has_contact_info(self) -> bool:
        return bool(self.emails or self.phones)

    def has_insurance_info(self) -> bool:
        return bool(self.insurance_terms or self.insurance_details)

    def insurance_summary(self) -> str:
        order = ["Licensed", "Bonded", "Insured"]
        terms = [t for t in order if t.lower() in self.insurance_terms]
        parts = []
        if terms:
            parts.append(terms[0] if len(terms) == 1 else ", ".join(terms[:-1]) + " & " + terms[-1])
        parts.extend(self.insurance_details)
        return "; ".join(parts[:10]) or INSURANCE_UNKNOWN

    def as_row(self) -> dict:
        return {
            "URL": self.url,
            "Status": self.status,
            "Emails": ", ".join(sorted(self.emails)),
            "Phone Numbers": ", ".join(sorted(self.phones)),
            "Contact Names": ", ".join(sorted(self.names)),
            "Insurance or Bond": self.insurance_summary(),
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
    if EMAIL_JUNK_LOCAL.match(local):
        return None
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
    ext = EXTENSION_RE.search(raw)
    ext_txt = f" x{re.sub(r'[^0-9]', '', ext.group(0))}" if ext else ""
    stripped = raw.strip()
    nanp = None
    if len(digits) == 11 and digits[0] == "1":
        nanp = digits[1:]
    elif len(digits) == 10 and not stripped.startswith("+"):
        nanp = digits
    elif stripped.startswith("+1"):
        return None  # "+1..." without exactly 10 more digits is a typo (e.g. tel:+136-5747248)
    if nanp is not None:
        # North American numbers: area code and exchange can't start with 0 or 1
        if nanp[0] in "01" or nanp[3] in "01" or len(set(nanp)) <= 2:
            return None
        return f"({nanp[:3]}) {nanp[3:6]}-{nanp[6:]}{ext_txt}"
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


NAME_TRADE_WORDS = {
    "roofing", "roofer", "roofers", "roof", "plumbing", "plumber", "plumbers", "gutter", "gutters",
    "construction", "heating", "cooling", "exteriors", "exterior", "solar", "operated", "licensed",
    "insured", "bonded", "wine", "estimate", "estimates", "free", "repair", "residential", "commercial",
    "drain", "sewer", "water", "home", "homes", "llc", "inc", "corp", "systems", "solutions", "quote",
    "reviews", "review", "google", "yelp", "certified", "installation", "emergency", "today", "now",
}


def _valid_name(name: str) -> str | None:
    name = re.sub(r"\s+", " ", name).strip(" ,.-–|")
    if re.search(r"[,\d@/()&]", name):
        return None
    parts_ = name.split()
    if len(parts_) >= 2 and (len(parts_[0].strip(".")) < 2 or len(parts_[-1].strip(".")) < 2):
        return None  # "Justine S", "P Greg": initials-only first/last names are review handles
    if any(w.lower().strip(".") in NAME_TRADE_WORDS for w in parts_):
        return None
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
            if "Review" in types or "Comment" in types:
                return  # review/comment authors are customers, not contacts
            if "Person" in types and isinstance(node.get("name"), str):
                names.add(node["name"])
            for k, v in node.items():
                if k in ("review", "reviews", "comment", "aggregateRating"):
                    continue
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


def _normalize_money(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw.strip())
    return re.sub(r"\s?(mil|M)\b$", " million", re.sub(r"\s?K\b$", "K", raw))


def extract_insurance(text: str) -> tuple[set[str], list[str]]:
    """Return ({licensed/bonded/insured}, [licence numbers, coverage types, amounts])."""
    flat = re.sub(r"\s+", " ", text)
    terms: set[str] = set()
    details: list[str] = []

    def add(item: str) -> None:
        if item not in details:
            details.append(item)

    for m in INSURANCE_STATUS_RE.finditer(flat):
        words = {w.lower() for w in m.groups()[1:] if w}
        before, after = flat[max(0, m.start() - 25):m.start()], flat[m.end():m.end() + 40]
        if len(words) >= 2 or m.group(1) or _STATUS_BEFORE.search(before) or _STATUS_AFTER.search(after):
            terms |= words

    # Licence / registration / bond numbers, de-duplicated by the ID itself
    seen_ids: set[str] = set()
    for m in [*LICENSE_LABELED_RE.finditer(flat), *LICENSE_PREFIX_RE.finditer(flat)]:
        snippet = re.sub(r"\s+", " ", m.group(0)).strip(" :.")
        ident = m.group(1) if m.re is LICENSE_LABELED_RE else re.sub(r"\D", "", snippet)
        if not re.search(r"\d", ident):
            continue
        key = re.sub(r"[^0-9A-Z]", "", ident.upper())
        if any(key in s or s in key for s in seen_ids):
            continue
        seen_ids.add(key)
        add(snippet)

    for label, pattern in COVERAGE_PATTERNS:
        for m in pattern.finditer(flat):
            window = flat[max(0, m.start() - 80):m.end() + 80]
            if not COVERAGE_EXCLUDE_RE.search(window):
                add(label)
                break

    for pattern in COVERAGE_AMOUNT_RES:
        for m in pattern.finditer(flat):
            window = flat[max(0, m.start() - 80):m.end() + 80]
            if not COVERAGE_EXCLUDE_RE.search(window):
                add(f"Coverage: {_normalize_money(m.group(1))}")

    return terms, details


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


def find_about_link(soup: BeautifulSoup, base_url: str, skip: list[str]) -> str | None:
    """Return an About/Team page on the same domain that hasn't been scraped yet."""
    skip_norm = {s.rstrip("/") for s in skip} | {base_url.rstrip("/")}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#", 1)[0]
        if not _same_domain(absolute, base_url) or absolute.rstrip("/") in skip_norm:
            continue
        if ABOUT_LINK_RE.search(f"{a.get_text(' ', strip=True)} {href}"):
            return absolute
    return None


def find_insurance_link(soup: BeautifulSoup, base_url: str, skip: list[str]) -> str | None:
    """Return a licensing/insurance page on the same domain, falling back to 'About'."""
    skip_norm = {s.rstrip("/") for s in skip}
    best, fallback = None, None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#", 1)[0]
        if not _same_domain(absolute, base_url) or absolute.rstrip("/") in skip_norm \
                or absolute.rstrip("/") == base_url.rstrip("/"):
            continue
        label = f"{a.get_text(' ', strip=True)} {href}"
        if INSURANCE_LINK_RE.search(label):
            best = absolute
            break
        if fallback is None and INSURANCE_FALLBACK_LINK_RE.search(label):
            fallback = absolute
    return best or fallback


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


def _url_variants(url: str) -> list[str]:
    """The URL, then www-toggled and plain-HTTP versions (small sites are often misconfigured)."""
    p = urlparse(url)
    host = p.netloc
    alt_host = host[4:] if host.lower().startswith("www.") else f"www.{host}"
    variants = [url, p._replace(netloc=alt_host).geturl()]
    if p.scheme == "https":
        variants += [v.replace("https://", "http://", 1) for v in list(variants)]
    return variants


def fetch_with_fallbacks(session: requests.Session, url: str) -> tuple[str, str]:
    last_exc: Exception | None = None
    for candidate in _url_variants(url):
        try:
            return fetch(session, candidate)
        except requests.exceptions.HTTPError:
            raise  # the server answered; a different host spelling won't help
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if isinstance(exc, requests.exceptions.Timeout):
                break  # a slow site won't get faster; don't spend 4x the timeout
    raise last_exc  # type: ignore[misc]


def _scrape_page(result: ScrapeResult, url: str, html: str) -> BeautifulSoup:
    link_soup = BeautifulSoup(html, PARSER)
    text = _visible_text(BeautifulSoup(html, PARSER))
    # mailto/tel/ld+json live in markup, so extract from the untouched copy
    result.emails |= extract_emails(link_soup, text)
    result.phones |= extract_phones(link_soup, text)
    result.names |= extract_names(link_soup, text)
    terms, details = extract_insurance(text)
    result.insurance_terms |= terms
    for item in details:
        result.insurance_details.setdefault(item, None)
    result.pages_scraped.append(url)
    result.text += "\n" + text[:150_000]
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
            final_url, html = fetch_with_fallbacks(session, url)
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

            if not (result.emails and result.phones):
                follow = find_contact_link(soup, final_url)
                if follow and follow.rstrip("/") in {u.rstrip("/") for u in result.pages_scraped}:
                    follow = None
                if follow:
                    try:
                        sub_url, sub_html = fetch(session, follow)
                        _scrape_page(result, sub_url, sub_html)
                    except requests.exceptions.RequestException:
                        result.status = "OK (contact page failed to load)"

            # Insurance/licence claims often live on an About or Licensing page
            # Owner/team names usually live on an About or Team page
            if not result.has_insurance_info() or not result.names:
                if result.has_insurance_info():
                    follow = find_about_link(soup, final_url, result.pages_scraped)
                else:
                    follow = find_insurance_link(soup, final_url, result.pages_scraped)
                if follow:
                    try:
                        sub_url, sub_html = fetch(session, follow)
                        _scrape_page(result, sub_url, sub_html)
                    except requests.exceptions.RequestException:
                        pass  # optional page; homepage results still stand
        except Exception as exc:  # noqa: BLE001 - parsing problems shouldn't crash the app
            result.status = f"Parse Error: {type(exc).__name__}"
            return result

    if result.status == "OK" and not (result.emails or result.phones or result.names):
        result.status = "OK (no contact info found)"
    return result

