"""Stage 1 of a scan: discover businesses by state, trade and geographic area.

Each state uses the best free, official source it publishes, and falls back to web-search
discovery for trades the state doesn't license (or states with no usable data):

  Washington  L&I contractor registry (data.wa.gov) .................. all trades
  Oregon      CCB active licenses (data.oregon.gov) ................... all trades (by business name)
              + Building Codes Division licenses ...................... plumbing, electrical, HVAC
  Texas       TDLR licenses (data.texas.gov) .......................... HVAC, electrical
              TSBPE Responsible Master Plumber list (tsbpe.texas.gov) . plumbing
              web search ............................................. everything else (e.g. roofing)
  Idaho       web search (DOPL's list is behind an interactive form)
  Oklahoma    web search (CIB offers lookups only, no bulk list)

Every source returns `Discovered` records; `discover()` runs the right ones and merges duplicates.
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

STATES = {"WA": "Washington", "OR": "Oregon", "ID": "Idaho", "TX": "Texas", "OK": "Oklahoma"}
# Area codes per state: prefer a business's local number, and spot out-of-state lookalikes
STATE_AREA_CODES = {
    "WA": {"206", "253", "360", "425", "509", "564"},
    "OR": {"503", "541", "458", "971"},
    "ID": {"208", "986"},
    "TX": {"210", "214", "254", "281", "325", "346", "361", "409", "430", "432", "469", "512", "682", "713", "726",
           "737", "806", "817", "830", "832", "903", "915", "936", "940", "945", "956", "972", "979"},
    "OK": {"405", "539", "572", "580", "918"},
}
TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888"}
STATE_ABBR = {v: k for k, v in STATES.items()}

SOURCE_WA = "WA L&I registry"
SOURCE_OR_CCB = "Oregon CCB registry"
SOURCE_OR_BCD = "Oregon Building Codes Division"
SOURCE_TX_TDLR = "Texas TDLR licenses"
SOURCE_TX_TSBPE = "Texas TSBPE plumbers"
SOURCE_WEB = "Web search"

WA_URL = "https://data.wa.gov/resource/m8qx-ubtq.json"
OR_CCB_URL = "https://data.oregon.gov/resource/g77e-6bhs.json"
OR_BCD_URL = "https://data.oregon.gov/resource/vhbr-cuaq.json"
TX_TDLR_URL = "https://data.texas.gov/resource/7358-krk7.json"
TX_TSBPE_URL = "https://tsbpe.texas.gov/download-csv/RMP/"
# Registry downloads identify the app honestly (some sites refuse the default Python client)
DATA_HEADERS = {"User-Agent": "ContactScraper/1.0 (+https://vendors.crosbyllc.com)"}

# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Trade:
    label: str            # shown in the scan form
    vendor_type: str      # stored in VendorType
    search: str           # words used for web-search discovery, e.g. "roofing contractor"
    specialties: tuple = ()      # WA L&I specialty codes
    license_types: tuple = ()    # WA L&I contractor license type codes (PC = plumbing, EC = electrical)
    name_words: tuple = ()       # match businesses whose name contains these words
    or_bcd: tuple = ()           # Oregon BCD license type prefixes, e.g. "PB" = plumbing contractor
    tx_tdlr: tuple = ()          # Texas TDLR license types
    tx_tsbpe: bool = False       # Texas plumbing board list
    stem: str = ""               # word a matching website must mention (web discovery check)


TRADES = {
    t.label: t for t in [
        Trade("Roofing", "Roofers", "roofing contractor", specialties=("CD",), name_words=("ROOF",), stem="roof"),
        Trade("Gutters", "Gutters", "gutter installation company", specialties=("CV",), name_words=("GUTTER",),
              stem="gutter"),
        Trade("Plumbing", "Plumbers", "plumbing company", license_types=("PC",), name_words=("PLUMB", "ROOTER"),
              or_bcd=("PB",), tx_tsbpe=True, stem="plumb"),
        Trade("HVAC", "HVAC", "HVAC heating and air conditioning company", specialties=("SM", "6A", "6B"),
              name_words=("HVAC", "HEATING", "AIR CONDITION"), or_bcd=("LHR",), tx_tdlr=("A/C Contractor",),
              stem="hvac|heating|air condition|furnace"),
        Trade("Electrical", "Electricians", "electrical contractor", license_types=("EC",), name_words=("ELECTRIC",),
              or_bcd=("C-",), tx_tdlr=("Electrical Contractor",), stem="electric"),
        Trade("Siding", "Siding", "siding contractor", specialties=("SW",), name_words=("SIDING",), stem="siding"),
        Trade("Painting", "Painters", "house painting contractor", specialties=("CB",), name_words=("PAINT",),
              stem="paint"),
        Trade("Landscaping", "Landscapers", "landscaping company", specialties=("BW",), name_words=("LANDSCAP",),
              stem="landscap"),
    ]
}


def custom_trade(text: str) -> Trade:
    """A free-text trade: matches the word in business names (and WA specialty descriptions)."""
    word = re.sub(r"[^A-Za-z ]", "", text).strip()
    return Trade(word.title(), word.title(), f"{word.lower()} company", name_words=(word.upper(),),
                 stem=word.lower().rstrip("s")[:8])


def sources_for(state: str, trade: Trade) -> list[str]:
    """Which Stage 1 sources a scan uses for this trade in this state (shown on the Scan page)."""
    if state == "WA":
        return [SOURCE_WA]
    if state == "OR":
        return [SOURCE_OR_CCB] + ([SOURCE_OR_BCD] if trade.or_bcd else [])
    if state == "TX":
        srcs = ([SOURCE_TX_TDLR] if trade.tx_tdlr else []) + ([SOURCE_TX_TSBPE] if trade.tx_tsbpe else [])
        return srcs or [SOURCE_WEB]
    return [SOURCE_WEB]


# --------------------------------------------------------------------------- #
# Geography, per state. Registries filter by ZIP prefix, city or county; web search uses "cities".
# Indented labels are submarkets of the region above them.
# --------------------------------------------------------------------------- #

LEWIS_COUNTY_CITIES = {"CENTRALIA", "CHEHALIS", "WINLOCK", "NAPAVINE"}  # 985 ZIPs, grouped with the South region

AREA_PRESETS: dict[str, dict[str, dict]] = {
    "WA": {
        "Western WA / Puget Sound": {"zip_prefixes": ["980", "981", "982", "983", "984", "985"],
                                     "search": ["Seattle", "Tacoma", "Everett", "Bellevue", "Kent", "Olympia",
                                                "Bellingham"]},
        "  Seattle / King County": {"zip_prefixes": ["980", "981"],
                                    "search": ["Seattle", "Bellevue", "Kent", "Renton", "Redmond"]},
        "  North Sound (Everett, Bellingham, Skagit)": {"zip_prefixes": ["982"],
                                                        "search": ["Everett", "Bellingham", "Mount Vernon",
                                                                   "Marysville"]},
        "  South Sound & Kitsap (Tacoma, Olympia, Bremerton)": {"zip_prefixes": ["983", "984", "985"],
                                                                "search": ["Tacoma", "Olympia", "Bremerton",
                                                                           "Puyallup"]},
        "Southern WA / Vancouver": {"zip_prefixes": ["986"], "cities": sorted(LEWIS_COUNTY_CITIES),
                                    "search": ["Vancouver", "Longview", "Camas", "Battle Ground", "Centralia"]},
        "Eastern WA / Spokane": {"zip_prefixes": ["990", "991", "992"], "search": ["Spokane", "Spokane Valley"]},
        "Central WA (Yakima / Wenatchee / Moses Lake)": {"zip_prefixes": ["988", "989"],
                                                         "search": ["Yakima", "Wenatchee", "Ellensburg",
                                                                    "Moses Lake"]},
        "Tri-Cities (Kennewick / Richland / Pasco)": {"zip_prefixes": ["993"],
                                                      "search": ["Kennewick", "Richland", "Pasco"]},
    },
    "OR": {
        "Portland Metro": {"zip_prefixes": ["970", "971", "972"],
                           "search": ["Portland", "Beaverton", "Gresham", "Hillsboro", "Tigard", "Lake Oswego"]},
        "Willamette Valley (Salem / Eugene / Corvallis)": {"zip_prefixes": ["973", "974"],
                                                           "search": ["Salem", "Eugene", "Corvallis", "Albany",
                                                                      "Springfield"]},
        "Southern Oregon (Medford / Grants Pass / Klamath Falls)": {"zip_prefixes": ["975", "976"],
                                                                    "search": ["Medford", "Grants Pass",
                                                                               "Ashland", "Klamath Falls"]},
        "Central Oregon (Bend / Redmond)": {"zip_prefixes": ["977"], "search": ["Bend", "Redmond", "Prineville"]},
        "Eastern Oregon (Pendleton / Hermiston / Ontario)": {"zip_prefixes": ["978", "979"],
                                                             "search": ["Pendleton", "Hermiston", "La Grande",
                                                                        "Ontario"]},
        "Oregon Coast": {"cities": ["ASTORIA", "SEASIDE", "TILLAMOOK", "LINCOLN CITY", "NEWPORT", "FLORENCE",
                                    "COOS BAY", "NORTH BEND", "BROOKINGS"],
                         "search": ["Astoria", "Lincoln City", "Newport", "Coos Bay", "Brookings"]},
    },
    "ID": {
        "Boise / Treasure Valley": {"zip_prefixes": ["836", "837"],
                                    "search": ["Boise", "Meridian", "Nampa", "Caldwell", "Eagle"]},
        "North Idaho (Coeur d'Alene / Sandpoint)": {"zip_prefixes": ["838"],
                                                    "search": ["Coeur d'Alene", "Post Falls", "Sandpoint",
                                                               "Hayden"]},
        "North Central Idaho (Lewiston / Moscow)": {"zip_prefixes": ["835"], "search": ["Lewiston", "Moscow"]},
        "Magic Valley (Twin Falls)": {"zip_prefixes": ["833"], "search": ["Twin Falls", "Jerome", "Burley"]},
        "Eastern Idaho (Idaho Falls / Pocatello)": {"zip_prefixes": ["832", "834"],
                                                    "search": ["Idaho Falls", "Pocatello", "Rexburg",
                                                               "Blackfoot"]},
    },
    "TX": {
        "Dallas-Fort Worth": {"counties": ["DALLAS", "TARRANT", "COLLIN", "DENTON", "ROCKWALL", "KAUFMAN", "ELLIS",
                                           "JOHNSON", "PARKER"],
                              "search": ["Dallas", "Fort Worth", "Plano", "Arlington", "Frisco", "Irving",
                                         "McKinney", "Denton"]},
        "Houston": {"counties": ["HARRIS", "FORT BEND", "MONTGOMERY", "BRAZORIA", "GALVESTON"],
                    "search": ["Houston", "Sugar Land", "The Woodlands", "Katy", "Pearland", "League City"]},
        "Austin": {"counties": ["TRAVIS", "WILLIAMSON", "HAYS"],
                   "search": ["Austin", "Round Rock", "Georgetown", "Cedar Park", "San Marcos"]},
        "San Antonio": {"counties": ["BEXAR", "COMAL", "GUADALUPE"],
                        "search": ["San Antonio", "New Braunfels", "Schertz"]},
        "El Paso": {"counties": ["EL PASO"], "search": ["El Paso"]},
        "Rio Grande Valley": {"counties": ["HIDALGO", "CAMERON"],
                              "search": ["McAllen", "Brownsville", "Harlingen", "Edinburg"]},
        "Corpus Christi": {"counties": ["NUECES", "SAN PATRICIO"], "search": ["Corpus Christi"]},
        "West Texas (Lubbock / Amarillo / Midland-Odessa)": {"counties": ["LUBBOCK", "POTTER", "RANDALL", "MIDLAND",
                                                                          "ECTOR"],
                                                             "search": ["Lubbock", "Amarillo", "Midland",
                                                                        "Odessa"]},
        "East Texas (Tyler / Longview)": {"counties": ["SMITH", "GREGG"], "search": ["Tyler", "Longview"]},
        "Central Texas (Waco / Killeen / Temple)": {"counties": ["MCLENNAN", "BELL"],
                                                    "search": ["Waco", "Killeen", "Temple"]},
    },
    "OK": {
        "Oklahoma City Metro": {"zip_prefixes": ["730", "731"],
                                "search": ["Oklahoma City", "Edmond", "Norman", "Moore", "Yukon", "Midwest City"]},
        "Tulsa Metro": {"zip_prefixes": ["740", "741"],
                        "search": ["Tulsa", "Broken Arrow", "Owasso", "Jenks", "Sand Springs"]},
        "Southwest Oklahoma (Lawton)": {"zip_prefixes": ["735"], "search": ["Lawton", "Duncan"]},
        "North Central Oklahoma (Enid / Stillwater / Ponca City)": {"zip_prefixes": ["737"],
                                                                    "search": ["Enid", "Stillwater",
                                                                               "Ponca City"]},
        "Eastern Oklahoma (Muskogee / McAlester / Bartlesville)": {"search": ["Muskogee", "McAlester",
                                                                             "Bartlesville", "Tahlequah"]},
        "Southern Oklahoma (Ardmore / Durant)": {"search": ["Ardmore", "Durant", "Ada"]},
    },
}


@dataclass
class AreaFilter:
    state: str
    zip_prefixes: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)       # upper-case, for registry filters
    counties: list[str] = field(default_factory=list)     # upper-case, for county-only registries (TX)
    search_places: list[str] = field(default_factory=list)  # for web-search discovery
    labels: list[str] = field(default_factory=list)

    def soql(self, zip_field: str = "zip", city_field: str = "city", county_field: str | None = None) -> str:
        parts = [f"{zip_field} like '{z}%'" for z in self.zip_prefixes]
        if self.cities:
            parts.append(f"upper({city_field}) in (" + ", ".join("'" + c.replace("'", "''") + "'"
                                                                 for c in self.cities) + ")")
        if county_field and self.counties:
            parts.append(f"upper({county_field}) in (" + ", ".join("'" + c.replace("'", "''") + "'"
                                                                   for c in self.counties) + ")")
        return "(" + " OR ".join(parts) + ")" if parts else "1=1"


def build_area_filter(presets: list[str], custom: str = "", state: str = "WA") -> AreaFilter:
    """Combine the state's preset regions with custom text: ZIPs/ZIP prefixes ("98604, 993"), city names."""
    f = AreaFilter(state=state, labels=[p.strip() for p in presets])
    for p in presets:
        spec = AREA_PRESETS[state][p]
        f.zip_prefixes += spec.get("zip_prefixes", [])
        f.cities += spec.get("cities", [])
        f.counties += spec.get("counties", [])
        f.search_places += spec.get("search", [])
    for token in re.split(r"[,;\n]+", custom or ""):
        token = token.strip()
        if re.fullmatch(r"\d{3,5}", token):
            f.zip_prefixes.append(token)
            if len(token) == 5:
                f.search_places.append(token)
        elif token:
            city = re.sub(rf",?\s*({state}|{STATES[state]})$", "", token, flags=re.I).strip()
            f.cities.append(city.upper())
            f.search_places.append(city.title())
    if custom.strip():
        f.labels.append(custom.strip())
    f.zip_prefixes, f.cities, f.counties = sorted(set(f.zip_prefixes)), sorted(set(f.cities)), sorted(set(f.counties))
    f.search_places = list(dict.fromkeys(f.search_places))
    return f


def region_for(state: str, zip5: str | None, city: str | None, county: str | None) -> str | None:
    """The top-level preset region a business falls in (stored as Region)."""
    city_u, county_u = (city or "").upper(), (county or "").upper()
    for label, spec in AREA_PRESETS.get(state, {}).items():
        if label.startswith(" "):
            continue
        if (zip5 and any(zip5.startswith(z) for z in spec.get("zip_prefixes", []))) \
                or city_u in spec.get("cities", []) or county_u in spec.get("counties", []):
            return label
    return None


# --------------------------------------------------------------------------- #
# Discovered records and tidying
# --------------------------------------------------------------------------- #

@dataclass
class Discovered:
    name: str
    vendor_type: str
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    zip: str | None = None
    region: str | None = None
    owner: str | None = None
    ubi: str | None = None              # state business ID (WA UBI) when the source has one
    license_number: str | None = None
    state: str = "WA"
    website: str | None = None          # web-search discovery finds the site first
    insurance: str | None = None        # insurance/bond details from registries that publish them
    source: str = SOURCE_WA


_KEEP_UPPER = {"LLC", "LLP", "PLLC", "PC", "HVAC", "NW", "PNW", "USA", "II", "III", "IV", "DBA", "WA", "AC",
               "A/C", "TX", "OK", "OR"}
_DIRECTIONS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def tidy_name(raw: str) -> str:
    """'3D ROOFING LLC' -> '3D Roofing LLC'; keeps acronyms and mixed case that was already there."""
    raw = re.sub(r"\s+", " ", raw or "").strip()
    if not raw or raw != raw.upper():
        return raw
    words = []
    for w in raw.split():
        core = re.sub(r"[^A-Za-z/]", "", w)
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


def tidy_city(raw: str | None, state: str) -> str | None:
    """'BEND OR' -> 'Bend' (some registries append the state to the city)."""
    city = re.sub(rf"[,\s]+({state}|{STATES.get(state, state)})$", "", (raw or "").strip(), flags=re.I)
    return city.title() or None


def search_places_for(state: str, region: str | None) -> list[str]:
    """The main cities of a record's region, for website searches when the record has no city."""
    for label, spec in AREA_PRESETS.get(state, {}).items():
        if label.strip() == (region or "").strip():
            return spec.get("search", [])
    return []


def _address(street: str | None, city: str | None, state: str, zip5: str | None) -> str | None:
    street = (street or "").strip()
    street = tidy_address(street) if street.isupper() else street
    return ", ".join(x for x in (street, city, " ".join(x for x in (state, zip5) if x)) if x) or None


def _insurance(company: str | None, amount: str | None = None, expires: str | None = None,
               label: str = "Liability insurance") -> str | None:
    if not company:
        return None
    amt = f" ${int(float(amount)):,}" if amount and re.fullmatch(r"[\d.]+", str(amount)) else ""
    exp = f" (expires {expires[:10]})" if expires else ""
    return f"{label}: {tidy_name(company)}{amt}{exp}"


def _get_json(url: str, where: str, select: str | None = None, limit: int = 50000) -> list[dict]:
    params = {"$where": where, "$limit": limit}
    if select:
        params["$select"] = select
    resp = requests.get(url, params=params, headers=DATA_HEADERS, timeout=90)
    resp.raise_for_status()
    return resp.json()


def _name_clause(trade: Trade, field_name: str) -> list[str]:
    return [f"upper({field_name}) like '%{w.replace(chr(39), chr(39) * 2)}%'" for w in trade.name_words]


# --------------------------------------------------------------------------- #
# Washington: L&I contractor registry
# --------------------------------------------------------------------------- #

def _wa_trade_clause(trade: Trade) -> str:
    parts = []
    if trade.specialties:
        codes = ", ".join(f"'{c}'" for c in trade.specialties)
        parts.append(f"specialtycode1 in ({codes}) OR specialtycode2 in ({codes})")
    if trade.license_types:
        parts.append("contractorlicensetypecode in (" + ", ".join(f"'{c}'" for c in trade.license_types) + ")")
    parts += _name_clause(trade, "businessname")
    if not trade.specialties and not trade.license_types:  # custom trade: also the specialty text
        for w in trade.name_words:
            parts.append(f"upper(specialtycode1desc) like '%{w}%' OR upper(specialtycode2desc) like '%{w}%'")
    return "(" + " OR ".join(parts) + ")"


def _wa_where(trade: Trade, area: AreaFilter) -> str:
    return f"contractorlicensestatus='ACTIVE' AND {_wa_trade_clause(trade)} AND {area.soql()}"


def discover_wa(trade: Trade, area: AreaFilter) -> list[Discovered]:
    out = []
    for r in _get_json(WA_URL, _wa_where(trade, area)):
        zip5 = (r.get("zip") or "")[:5] or None
        city = (r.get("city") or "").title() or None
        out.append(Discovered(
            name=tidy_name(r.get("businessname", "")), vendor_type=trade.vendor_type, state="WA",
            phone=tidy_phone(r.get("phonenumber")), city=city, zip=zip5,
            address=_address(" ".join(x for x in (r.get("address1"), r.get("address2")) if x), city,
                             r.get("state") or "WA", zip5),
            region=region_for("WA", zip5, r.get("city"), None), owner=tidy_person(r.get("primaryprincipalname")),
            ubi=r.get("ubi"), license_number=r.get("contractorlicensenumber"), source=SOURCE_WA))
    return out


# --------------------------------------------------------------------------- #
# Oregon: CCB (all contractors, with insurance and bond) + Building Codes Division (trade licenses)
# --------------------------------------------------------------------------- #

def discover_or_ccb(trade: Trade, area: AreaFilter) -> list[Discovered]:
    where = "(" + " OR ".join(_name_clause(trade, "full_name")) + ") AND " + area.soql("zip_code", "city")
    out = []
    for r in _get_json(OR_CCB_URL, where):
        zip5 = (r.get("zip_code") or "")[:5] or None
        city = tidy_city(r.get("city"), "OR")
        ins = _insurance(r.get("ins_company"), r.get("ins_amount"), r.get("ins_exp_date"))
        bond = _insurance(r.get("bond_company"), r.get("bond_amount"), r.get("bond_exp_date"), "Bond")
        out.append(Discovered(
            name=tidy_name(r.get("full_name", "")), vendor_type=trade.vendor_type, state="OR",
            phone=tidy_phone(r.get("phone_number")), city=city, zip=zip5,
            address=_address(r.get("address"), city, r.get("state") or "OR", zip5),
            region=region_for("OR", zip5, r.get("city"), r.get("county_name")), owner=tidy_person(r.get("rmi_name")),
            license_number=f"CCB {r.get('license_number')}" if r.get("license_number") else None,
            insurance="; ".join(x for x in (ins, bond) if x) or None, source=SOURCE_OR_CCB))
    return out


def discover_or_bcd(trade: Trade, area: AreaFilter) -> list[Discovered]:
    if not trade.or_bcd:
        return []
    types = " OR ".join(f"lictype like '{t}%'" for t in trade.or_bcd)
    where = f"({types}) AND {area.soql('zipcode', 'city')}"
    out = []
    for r in _get_json(OR_BCD_URL, where):
        zip5 = (r.get("zipcode") or "")[:5] or None
        city = tidy_city(r.get("city"), "OR")
        out.append(Discovered(
            name=tidy_name(r.get("dba") or r.get("full_name", "")), vendor_type=trade.vendor_type, state="OR",
            city=city, zip=zip5, address=_address(r.get("addr1"), city, r.get("state") or "OR", zip5),
            region=region_for("OR", zip5, r.get("city"), r.get("county")),
            license_number=f"OR BCD {r.get('licnbr')}" if r.get("licnbr") else None, source=SOURCE_OR_BCD))
    return out


# --------------------------------------------------------------------------- #
# Texas: TDLR (HVAC, electrical) and the plumbing board's Responsible Master Plumber list
# --------------------------------------------------------------------------- #

def _tx_csz(text: str | None) -> tuple[str | None, str | None]:
    """'AUSTIN TX 78701-1234' -> ('Austin', '78701')."""
    m = re.match(r"\s*(.*?)\s+TX\s+(\d{5})", text or "")
    return (m.group(1).title(), m.group(2)) if m else (None, None)


def discover_tx_tdlr(trade: Trade, area: AreaFilter) -> list[Discovered]:
    if not trade.tx_tdlr:
        return []
    types = ", ".join(f"'{t}'" for t in trade.tx_tdlr)
    city_clauses = [f"upper(business_city_state_zip) like '{c.replace(chr(39), chr(39) * 2)} %'" for c in area.cities]
    zip_clauses = [f"business_city_state_zip like '% {z}%'" for z in area.zip_prefixes]
    county = ("upper(business_county) in (" + ", ".join(f"'{c}'" for c in area.counties) + ")") if area.counties else ""
    place = " OR ".join([x for x in [county] if x] + city_clauses + zip_clauses) or "1=1"
    out = []
    for r in _get_json(TX_TDLR_URL, f"license_type in ({types}) AND ({place})"):
        city, zip5 = _tx_csz(r.get("business_city_state_zip"))
        county = (r.get("business_county") or "").title() or None
        out.append(Discovered(
            name=tidy_name(r.get("business_name") or tidy_person(r.get("owner_name")) or ""),
            vendor_type=trade.vendor_type, state="TX", phone=tidy_phone(r.get("business_telephone")),
            city=city, zip=zip5,
            address=(_address(r.get("business_address_line1"), city, "TX", zip5) if city
                     else f"{county} County, TX" if county else None),
            region=region_for("TX", zip5, city, r.get("business_county")), owner=tidy_person(r.get("owner_name")),
            license_number=f"TDLR {r.get('license_number')}" if r.get("license_number") else None,
            source=SOURCE_TX_TDLR))
    return out


_TSBPE_CACHE: dict = {}
_TSBPE_LOCK = threading.Lock()


def _tsbpe_rows() -> list[dict]:
    """The Responsible Master Plumber CSV (updated daily by the board), cached for 12 hours."""
    with _TSBPE_LOCK:
        if _TSBPE_CACHE.get("at", 0) > time.time() - 12 * 3600:
            return _TSBPE_CACHE["rows"]
        resp = requests.get(TX_TSBPE_URL, headers=DATA_HEADERS, timeout=120)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig", errors="replace"))))
        _TSBPE_CACHE.update(at=time.time(), rows=rows)
        return rows


def discover_tx_tsbpe(trade: Trade, area: AreaFilter) -> list[Discovered]:
    if not trade.tx_tsbpe:
        return []
    out = []
    for r in _tsbpe_rows():
        if r.get("LIC_STATUS", "").lower() not in ("current", "active"):
            continue
        zip5 = (r.get("ZIP") or "")[:5]
        if not ((r.get("COUNTY") or "").upper() in area.counties or (r.get("CITY") or "").upper() in area.cities
                or any(zip5.startswith(z) for z in area.zip_prefixes)):
            continue
        person = " ".join(x for x in (r.get("FIRST_NAME"), r.get("MIDDLE_NAME"), r.get("LAST_NAME")) if x)
        city = (r.get("CITY") or "").title() or None
        out.append(Discovered(
            name=tidy_name(r.get("PLUMB_COMPANY") or person), vendor_type=trade.vendor_type, state="TX",
            phone=tidy_phone(r.get("PHONE")), city=city, zip=zip5 or None,
            address=_address(" ".join(x for x in (r.get("ADDR1"), r.get("ADDR2")) if x), city, "TX", zip5),
            region=region_for("TX", zip5, r.get("CITY"), r.get("COUNTY")), owner=tidy_person(person),
            license_number=f"TSBPE {r.get('LICENSE_NBR')}" if r.get("LICENSE_NBR") else None,
            insurance=_insurance(r.get("INSURANCE_COMPANY"), expires=r.get("INS_EXPIRY_DTE")),
            source=SOURCE_TX_TSBPE))
    return out


# --------------------------------------------------------------------------- #
# Web-search discovery (states/trades without a usable registry)
# --------------------------------------------------------------------------- #

WEB_TIMEOUT = 8
# Excluding the big directories surfaces businesses' own sites (both search engines support "-word")
NO_DIRECTORIES = "-yelp -angi -bbb -yellowpages -homeadvisor -thumbtack -houzz -expertise -nextdoor -facebook"
_TITLE_SPLIT = re.compile(r"\s*\|\s*|\s+[\-–—:•·]\s+")


_COPYRIGHT = re.compile(r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}\s*(?:[-–]\s*\d{2,4})?\s*)?,?\s*"
                        r"([A-Z][A-Za-z0-9&'’.,\- ]{2,60}?)\s*(?:\.|,|\||all rights|$)", re.I)
_SLOGANY = re.compile(r"(?i)\b(best|top|near me|services?|contractors?|company|companies|repair|installation)\b.*"
                      r"\b(in|near|for)\b|,\s*[A-Z]{2}\b")


def _site_name(soup: BeautifulSoup, url: str) -> str:
    """The business name, preferring one that matches the site's domain (see _name_candidates)."""
    root = re.sub(r"[^a-z]", "", (urlparse(url).hostname or "").lower().removeprefix("www.").split(".")[0])
    # A capitalised phrase in the page that spells out the domain ("Copper Cloud Rain Gutters") is the name
    text = soup.get_text(" ", strip=True)
    for m in re.finditer(r"\b(?:[A-Z][A-Za-z'’&]*\s+){1,5}[A-Z][A-Za-z'’&]*", text):
        phrase = m.group(0)
        if len(root) >= 6 and re.sub(r"[^a-z]", "", phrase.lower()) in (root, root.removesuffix("llc")):
            return phrase
    cands = [c for c in _name_candidates(soup, url) if c]
    for c in cands:
        letters = re.sub(r"[^a-z]", "", c.lower())
        if root and len(letters) >= 5 and (letters in root or root in letters or letters[:8] in root):
            return c
    # e.g. "Bob's Plumbing" for bobsplumbinginc.com: most of the domain, from its start
    for m in re.finditer(r"\b(?:[A-Z][A-Za-z'’&]*\s+){1,4}[A-Z][A-Za-z'’&]*", text):
        letters = re.sub(r"[^a-z]", "", m.group(0).lower())
        if len(root) >= 8 and root.startswith(letters) and len(letters) >= 0.7 * len(root):
            return m.group(0)
    return cands[0] if cands else (urlparse(url).hostname or url).removeprefix("www.")


def _name_candidates(soup: BeautifulSoup, url: str):
    """Business-name candidates in order: schema.org data, og:site_name, the © line, title segments."""
    import json
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
            if isinstance(node, dict) and isinstance(node.get("name"), str) and any(
                    t in str(node.get("@type", "")) for t in ("LocalBusiness", "Organization", "Contractor",
                                                              "RoofingContractor", "Plumber", "HVACBusiness",
                                                              "Electrician", "HomeAndConstructionBusiness")):
                yield node["name"].strip()
    meta = soup.find("meta", attrs={"property": "og:site_name"})
    if meta and meta.get("content", "").strip() and not _SLOGANY.search(meta["content"]):
        yield meta["content"].strip()
    m = _COPYRIGHT.search(soup.get_text(" ", strip=True)[-3000:])
    if m and len(m.group(1).split()) <= 7:
        yield m.group(1).strip(" .,-")
    title = (soup.title.string if soup.title and soup.title.string else "").strip()
    parts = [p.strip() for p in _TITLE_SPLIT.split(title) if p.strip()]
    yield from sorted(parts, key=lambda p: (bool(_SLOGANY.search(p)), len(p)))


def _check_candidate(url: str, trade: Trade, area: AreaFilter, place: str) -> Discovered | None:
    """Open a search result's homepage; keep it only if it's a business in this trade and area."""
    from enrich import _homepage, _is_directory  # local import: enrich imports scraper, not discovery
    from scraper import HEADERS, extract_phones

    home = _homepage(url)
    try:
        resp = requests.get(home, headers=HEADERS, timeout=WEB_TIMEOUT, allow_redirects=True)
        if resp.status_code >= 400 or "html" not in resp.headers.get("Content-Type", "html"):
            return None
    except requests.RequestException:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    if _is_directory(resp.url, soup.title.string if soup.title and soup.title.string else ""):
        return None
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    low = text.lower()
    if not re.search(trade.stem or trade.search.split()[0], low):
        return None  # not about this trade
    state_name = STATES[area.state]
    places = [p.lower() for p in area.search_places if not p.isdigit()] + [state_name.lower()]
    if not (any(re.search(rf"\b{re.escape(p)}\b", low) for p in places)
            or re.search(rf"\b{area.state}\.?,?\s+\d{{5}}\b", text)):
        return None  # doesn't name a city in the area, the state, or "ST 12345"
    phones = sorted(extract_phones(BeautifulSoup(resp.text, "lxml"), text))
    codes = {re.sub(r"\D", "", p)[-10:][:3] for p in phones}
    if codes and not codes & STATE_AREA_CODES.get(area.state, set()) and not codes <= TOLL_FREE:
        return None  # only out-of-state numbers: a national lead-generation site or a namesake elsewhere
    local = [p for p in phones if re.sub(r"\D", "", p)[-10:][:3] in STATE_AREA_CODES.get(area.state, set())]
    phones = local or phones
    return Discovered(name=tidy_name(_site_name(BeautifulSoup(resp.text, "lxml"), resp.url))[:120],
                      vendor_type=trade.vendor_type, state=area.state, website=_homepage(resp.url),
                      phone=tidy_phone(phones[0]) if phones else None, city=place,
                      region=next((label.strip() for label in area.labels
                                   if label.strip() in AREA_PRESETS[area.state]), None),
                      source=SOURCE_WEB)


def discover_web(trade: Trade, area: AreaFilter, search, limit: int, log=print) -> list[Discovered]:
    """Search '[trade] [city] [state]' for each place in the area and keep verified business sites."""
    from enrich import SearchError, _is_directory, _registered_domain

    seen_domains: set[str] = set()
    seen_phones: set[str] = set()
    found: list[Discovered] = []
    places = area.search_places or [STATES[area.state]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for place in places:
            if len(found) >= limit:
                break
            results = []
            state_name = STATES[area.state]  # full name: engines misread "OK", "OR", "ID"
            for query in (f"{trade.search} {place}, {state_name}",
                          f"{trade.vendor_type.lower()} {place} {state_name} {NO_DIRECTORIES}",
                          f"local {trade.search} near {place} {state_name} {NO_DIRECTORIES}"):
                try:
                    results += search.search(query, max_results=25)
                except SearchError as exc:
                    log(f"Stage 1: web search failed for '{query}' ({exc})")
            urls = []
            for r in results:
                url = r.get("url", "")
                dom = _registered_domain(urlparse(url).hostname or "")
                if not url.startswith("http") or not dom or dom in seen_domains or _is_directory(url, r.get("title", "")):
                    continue
                seen_domains.add(dom)
                urls.append(url)
            kept = []
            for d in pool.map(lambda u: _check_candidate(u, trade, area, place), urls):
                key = re.sub(r"\D", "", d.phone or "")[-10:] if d else ""
                if d and not (key and key in seen_phones):  # one site per phone: lead-gen networks reuse numbers
                    seen_phones.add(key)
                    kept.append(d)
            found += kept
            log(f"Stage 1: {trade.label} in {place}: {len(results)} search results, {len(urls)} candidate sites, "
                f"{len(kept)} verified businesses")
    return found[:limit]


# --------------------------------------------------------------------------- #
# Running the right sources
# --------------------------------------------------------------------------- #

def count_registry(state: str, trades: list[Trade], area: AreaFilter) -> dict[str, str]:
    """Per-trade estimate for the Scan page: a license count, or a note that web search is used."""
    out = {}
    for t in trades:
        srcs = sources_for(state, t)
        if srcs == [SOURCE_WEB]:
            out[t.label] = f"web search across {len(area.search_places)} cities"
            continue
        n = 0
        if SOURCE_WA in srcs:
            n += int(_get_json(WA_URL, _wa_where(t, area), "count(*) as n")[0]["n"])
        if SOURCE_OR_CCB in srcs:
            n += int(_get_json(OR_CCB_URL, "(" + " OR ".join(_name_clause(t, "full_name")) + ") AND "
                               + area.soql("zip_code", "city"), "count(*) as n")[0]["n"])
        if SOURCE_OR_BCD in srcs:
            n += len(discover_or_bcd(t, area))
        if SOURCE_TX_TDLR in srcs:
            n += len(discover_tx_tdlr(t, area))
        if SOURCE_TX_TSBPE in srcs:
            n += len(discover_tx_tsbpe(t, area))
        out[t.label] = f"{n} licenses ({', '.join(srcs)})"
    return out


def _dedupe_key(d: Discovered) -> str:
    if d.ubi:
        return "ubi:" + d.ubi
    if d.website:
        return "web:" + (urlparse(d.website).hostname or "").lower().removeprefix("www.")
    # Sources disagree on phones (some have none), so the cleaned name is the key
    name = re.sub(r"\b(llc|inc|corp|co|company|ltd|pllc|dba)\b", " ", re.sub(r"[^a-z0-9 ]", " ", d.name.lower()))
    return "name:" + re.sub(r"\s+", " ", name).strip()


def discover(state: str, trades: list[Trade], area: AreaFilter, search=None, web_limit: int = 100,
             log=print) -> list[Discovered]:
    """Businesses for the trades in the area, from the state's sources, with duplicates merged.

    A business found for several trades (or by several sources) keeps the first one's record,
    with empty fields filled from the others.
    """
    merged: dict[str, Discovered] = {}
    for trade in trades:
        for src in sources_for(state, trade):
            try:
                if src == SOURCE_WA:
                    items = discover_wa(trade, area)
                elif src == SOURCE_OR_CCB:
                    items = discover_or_ccb(trade, area)
                elif src == SOURCE_OR_BCD:
                    items = discover_or_bcd(trade, area)
                elif src == SOURCE_TX_TDLR:
                    items = discover_tx_tdlr(trade, area)
                elif src == SOURCE_TX_TSBPE:
                    items = discover_tx_tsbpe(trade, area)
                else:
                    items = discover_web(trade, area, search, web_limit, log)
            except requests.RequestException as exc:
                log(f"Stage 1: {src} unavailable for {trade.label} ({exc}); skipped")
                continue
            new = 0
            for d in items:
                key = _dedupe_key(d)
                if key in merged:
                    have = merged[key]
                    for f in ("phone", "address", "city", "zip", "region", "owner", "license_number", "website",
                              "insurance"):
                        if not getattr(have, f) and getattr(d, f):
                            setattr(have, f, getattr(d, f))
                else:
                    merged[key] = d
                    new += 1
            if src != SOURCE_WEB:
                log(f"Stage 1: {trade.label} from {src}: {len(items)} active licenses in area, {new} new businesses")
    return list(merged.values())
