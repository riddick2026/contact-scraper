"""Rescan saved records to fill in missing information.

Only empty or gap-marker fields ("Not found", "No website", ...) are filled; values already in
the database, including hand edits, are never changed. A record without a website gets a new
web search, and a found site is only used if it passes the same vendor checks as spreadsheet
mode (name, trade, area).
"""

from __future__ import annotations

import json

import db
import enrich
from scraper import INSURANCE_UNKNOWN, scrape_url

GAPS = set(db.GAP_VALUES) | {""}
FILL_FIELDS = {"ContactName": "Contact Name", "Email": "Email", "Phone": "Phone",
               "InsuranceOrBond": "Insurance or Bond"}


def _gap(value) -> bool:
    return value is None or str(value).strip() in GAPS


def rescan_record(rec: dict, search: enrich.WebSearch) -> tuple[dict, str]:
    """({column: new value} for gaps that could be filled, a one-line summary)."""
    vendor = rec["VendorName"] or ""
    extra = json.loads(rec["ExtraData"]) if rec.get("ExtraData") else {}
    location = " ".join(x for x in (extra.get("City Located") or extra.get("City") or "", rec.get("State") or "")
                        if x).strip()
    fields: dict = {}
    site = None if _gap(rec["Website"]) else rec["Website"]

    if site:
        result = scrape_url(site)
    elif vendor:
        url, source = enrich.find_website(search, vendor, location)
        if not url:
            return {}, "search failed, try again later" if source == enrich.SRC_FAILED else "no website found"
        result = scrape_url(url)
        if not result.status.startswith("OK"):
            return {}, f"found {url} but it didn't load"
        verdict, why = enrich.site_matches_vendor(vendor, url, result.text, location)
        if verdict == "reject":
            return {}, f"no matching website found ({url} rejected: {why})"
        fields.update(Website=url, WebsiteDomain=db._domain(url),
                      WebsiteSource=enrich.SRC_LOW if verdict == "verify" else source)
    else:
        return {}, "no vendor name or website to search"

    if not result.status.startswith("OK"):
        return {}, f"website unreachable ({result.status})"

    found = {
        "ContactName": ", ".join(enrich.clean_names(sorted(result.names), vendor)),
        "Email": ", ".join(sorted(result.emails)),
        "Phone": ", ".join(sorted(result.phones)),
        "InsuranceOrBond": "" if result.insurance_summary() == INSURANCE_UNKNOWN else result.insurance_summary(),
    }
    for col, value in found.items():
        if _gap(rec[col]) and value:
            fields[col] = value
    if not rec.get("VendorType"):
        vtype = enrich.vendor_type(vendor) or enrich.guess_vendor_type(result.text)
        if vtype:
            fields["VendorType"] = vtype
    if _gap(rec.get("PagesScraped")) or "Website" in fields:
        fields["PagesScraped"] = " | ".join(result.pages_scraped)

    final = {c: fields.get(c, rec[c]) for c in FILL_FIELDS}
    missing = [label for c, label in FILL_FIELDS.items() if _gap(final[c])]
    fields["ScrapeStatus"] = "Complete" if not missing else "Partial - missing " + ", ".join(missing)
    filled = [FILL_FIELDS.get(c, c) for c in fields if c in FILL_FIELDS or c in ("Website", "VendorType")]
    return fields, ("filled " + ", ".join(filled)) if filled else "nothing new found"
