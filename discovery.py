"""Stage 1 of a scan: discover businesses by trade and geographic area.

Source: the Washington State L&I contractor registry (data.wa.gov, updated daily). Every
contractor working in Washington must register, so it's a near-complete, legal source of
licensed businesses with name, phone, address, owner and trade. More sources can be added by
writing another `discover_*` function that returns `Discovered` records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests

LNI_URL = "https://data.wa.gov/resource/m8qx-ubtq.json"
SOURCE_LNI = "WA L&I registry"

# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Trade:
    label: str            # shown in the scan form
    vendor_type: str      # stored in VendorType
    specialties: tuple = ()      # L&I specialty codes
    license_types: tuple = ()    # L&I contractor license type codes (PC = plumbing, EC = electrical)
    name_words: tuple = ()       # also match businesses whose name contains these words


TRADES = {
    t.label: t for t in [
        Trade("Roofing", "Roofers", specialties=("CD",), name_words=("ROOF",)),
        Trade("Gutters", "Gutters", specialties=("CV",), name_words=("GUTTER",)),
        Trade("Plumbing", "Plumbers", license_types=("PC",), name_words=("PLUMB", "ROOTER")),
        Trade("HVAC", "HVAC", specialties=("SM", "6A", "6B"), name_words=("HVAC", "HEATING", "AIR CONDITION")),
        Trade("Electrical", "Electricians", license_types=("EC",), name_words=("ELECTRIC",)),
        Trade("Siding", "Siding", specialties=("SW",), name_words=("SIDING",)),
        Trade("Painting", "Painters", specialties=("CB",), name_words=("PAINT",)),
        Trade("Landscaping", "Landscapers", specialties=("BW",), name_words=("LANDSCAP",)),
    ]
}


def custom_trade(text: str) -> Trade:
    """A free-text trade: matches the word in business names and specialty descriptions."""
    word = re.sub(r"[^A-Za-z ]", "", text).strip()
    return Trade(word.title(), word.title(), name_words=(word.upper(),))


# --------------------------------------------------------------------------- #
# Geography: presets are ZIP-prefix based so whole regions (incl. small towns) are covered
# --------------------------------------------------------------------------- #

LEWIS_COUNTY_CITIES = {"CENTRALIA", "CHEHALIS", "WINLOCK", "NAPAVINE"}  # 985 ZIPs, grouped with the South region

AREA_PRESETS: dict[str, dict] = {
    "Western WA / Puget Sound": {"zip_prefixes": ["980", "981", "982", "983", "984", "985"]},
    "  Seattle / King County": {"zip_prefixes": ["980", "981"]},
    "  North Sound (Everett, Bellingham, Skagit)": {"zip_prefixes": ["982"]},
    "  South Sound & Kitsap (Tacoma, Olympia, Bremerton)": {"zip_prefixes": ["983", "984", "985"]},
    "Southern WA / Vancouver": {"zip_prefixes": ["986"], "cities": sorted(LEWIS_COUNTY_CITIES)},
    "Eastern WA / Spokane": {"zip_prefixes": ["990", "991", "992"]},
    "Central WA (Yakima / Wenatchee / Moses Lake)": {"zip_prefixes": ["988", "989"]},
    "Tri-Cities (Kennewick / Richland / Pasco)": {"zip_prefixes": ["993"]},
}

# Region label stored on each record, from its ZIP
REGION_BY_ZIP3 = {
    **dict.fromkeys(["980", "981"], "Seattle / King County"),
    "982": "North Sound",
    **dict.fromkeys(["983", "984", "985"], "South Sound & Kitsap"),
    "986": "Southern WA / Vancouver",
    **dict.fromkeys(["988", "989"], "Central WA"),
    **dict.fromkeys(["990", "991", "992"], "Eastern WA / Spokane"),
    "993": "Tri-Cities",
}


@dataclass
class AreaFilter:
    zip_prefixes: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    def soql(self) -> str:
        parts = [f"zip like '{z}%'" for z in self.zip_prefixes]
        if self.cities:
            parts.append("upper(city) in (" + ", ".join("'" + c.replace("'", "''") + "'" for c in self.cities) + ")")
        return "(" + " OR ".join(parts) + ")" if parts else "1=1"


def build_area_filter(presets: list[str], custom: str = "") -> AreaFilter:
    """Combine preset regions with custom text: ZIPs/ZIP prefixes ("98604, 993") and city names."""
    f = AreaFilter(labels=list(presets))
    for p in presets:
        f.zip_prefixes += AREA_PRESETS[p].get("zip_prefixes", [])
        f.cities += AREA_PRESETS[p].get("cities", [])
    for token in re.split(r"[,;\n]+", custom or ""):
        token = token.strip()
        if re.fullmatch(r"\d{3,5}", token):
            f.zip_prefixes.append(token)
        elif token:
            f.cities.append(re.sub(r",?\s*(WA|Washington)$", "", token, flags=re.I).strip().upper())
    if custom.strip():
        f.labels.append(custom.strip())
    f.zip_prefixes = sorted(set(f.zip_prefixes))
    f.cities = sorted(set(f.cities))
    return f


# --------------------------------------------------------------------------- #
# Discovered records
# --------------------------------------------------------------------------- #

@dataclass
class Discovered:
    name: str
    vendor_type: str
    phone: str | None
    address: str | None
    city: str | None
    zip: str | None
    region: str | None
    owner: str | None
    ubi: str | None
    license_number: str | None
    source: str = SOURCE_LNI


_KEEP_UPPER = {"LLC", "LLP", "PLLC", "PC", "HVAC", "NW", "PNW", "USA", "II", "III", "IV", "DBA", "WA"}
_DIRECTIONS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def tidy_name(raw: str) -> str:
    """'3D ROOFING LLC' -> '3D Roofing LLC'; keeps acronyms and mixed case that was already there."""
    if not raw or raw != raw.upper():
        return (raw or "").strip()
    words = []
    for w in raw.split():
        core = re.sub(r"[^A-Za-z]", "", w)
        if core in _KEEP_UPPER or (len(core) <= 3 and not re.search(r"[AEIOUY]", core)):
            words.append(w)
        else:
            words.append(re.sub(r"[A-Za-z][A-Za-z']*", lambda m: m.group(0).capitalize(), w.lower()))
    return " ".join(words)


def tidy_address(raw: str) -> str:
    """'1503 SW 4TH AVE' -> '1503 SW 4th Ave'; 'SE MCGILLIVRAY BLVD' -> 'SE McGillivray Blvd'."""
    out = []
    for w in raw.split():
        bare = w.strip(".,").upper()
        if bare in _DIRECTIONS or bare == "PO" or re.fullmatch(r"[A-Z]?\d+[A-Z]?|#\w+", bare) and not re.fullmatch(r"\d+(ST|ND|RD|TH)", bare):
            out.append(w.upper())
        elif re.fullmatch(r"\d+(ST|ND|RD|TH)", bare):
            out.append(w.lower())
        else:
            t = w.lower().capitalize()
            out.append(re.sub(r"^Mc([a-z])", lambda m: "Mc" + m.group(1).upper(), t))
    return " ".join(out)


def tidy_person(raw: str | None) -> str | None:
    """'DEARNALEY, CRAIG ALAN' -> 'Craig Alan Dearnaley'."""
    if not raw:
        return None
    if "," in raw:
        last, first = [x.strip() for x in raw.split(",", 1)]
        raw = f"{first} {last}"
    def cap(word: str) -> str:
        return "-".join(x.capitalize() for x in word.split("-")) if word.isupper() or word.islower() else word
    return " ".join(cap(p) for p in raw.split()) or None


def tidy_phone(raw: str | None) -> str | None:
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else None


def _trade_clause(trade: Trade) -> str:
    parts = []
    if trade.specialties:
        codes = ", ".join(f"'{c}'" for c in trade.specialties)
        parts.append(f"specialtycode1 in ({codes}) OR specialtycode2 in ({codes})")
    if trade.license_types:
        parts.append("contractorlicensetypecode in (" + ", ".join(f"'{c}'" for c in trade.license_types) + ")")
    for w in trade.name_words:
        w = w.replace("'", "''")
        parts.append(f"upper(businessname) like '%{w}%'")
        if not trade.specialties and not trade.license_types:  # custom trade: also specialty text
            parts.append(f"upper(specialtycode1desc) like '%{w}%' OR upper(specialtycode2desc) like '%{w}%'")
    return "(" + " OR ".join(parts) + ")"


def _query(where: str, select: str | None = None, limit: int = 50000, timeout: int = 60) -> list[dict]:
    params = {"$where": where, "$limit": limit}
    if select:
        params["$select"] = select
    resp = requests.get(LNI_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _where(trade: Trade, area: AreaFilter) -> str:
    return f"contractorlicensestatus='ACTIVE' AND {_trade_clause(trade)} AND {area.soql()}"


def count_lni(trades: list[Trade], area: AreaFilter) -> dict[str, int]:
    """Quick per-trade counts for the scan form's estimate (before de-duplication)."""
    return {t.label: int(_query(_where(t, area), select="count(*) as n")[0]["n"]) for t in trades}


def discover_lni(trades: list[Trade], area: AreaFilter, log=print) -> list[Discovered]:
    """Active licensed contractors for the trades in the area, de-duplicated by UBI (state business ID).

    A business licensed for several selected trades gets the first selected trade's vendor type.
    """
    by_ubi: dict[str, Discovered] = {}
    for trade in trades:
        rows = _query(_where(trade, area))
        new = 0
        for r in rows:
            key = r.get("ubi") or r.get("contractorlicensenumber") or r.get("businessname", "")
            if key in by_ubi:
                continue
            zip5 = (r.get("zip") or "")[:5] or None
            city = (r.get("city") or "").title() or None
            street = " ".join(x for x in (r.get("address1"), r.get("address2")) if x).strip()
            address = ", ".join(x for x in (tidy_address(street) if street.isupper() else street, city,
                                            " ".join(x for x in (r.get("state"), zip5) if x)) if x) or None
            by_ubi[key] = Discovered(
                name=tidy_name(r.get("businessname", "")), vendor_type=trade.vendor_type,
                phone=tidy_phone(r.get("phonenumber")), address=address, city=city, zip=zip5,
                region=("Southern WA / Vancouver" if (r.get("city") or "").upper() in LEWIS_COUNTY_CITIES
                        else REGION_BY_ZIP3.get((zip5 or "")[:3])),
                owner=tidy_person(r.get("primaryprincipalname")),
                ubi=r.get("ubi"), license_number=r.get("contractorlicensenumber"),
            )
            new += 1
        log(f"Stage 1: {trade.label}: {len(rows)} active licenses in area, {new} new businesses")
    return list(by_ubi.values())
