"""Queue routing: Primary (all critical fields present) vs Secondary (something critical missing).

The critical fields are chosen in the Scan page and saved as an app setting. A record that was
promoted or demoted by hand (QueueLocked) is never moved by automatic routing.
"""

from __future__ import annotations

import checks

PRIMARY, SECONDARY = "Primary", "Secondary"
QUEUES = [PRIMARY, SECONDARY]

GAPS = {"", "not found", "unknown", "no website", "site unreachable", "search failed", "no match found",
        "none", "nan"}


def _filled(value) -> bool:
    return value is not None and str(value).strip().lower() not in GAPS


def _website(r) -> bool:
    return _filled(r.get("Website"))


def _phone(r) -> bool:
    return checks.has_valid_phone(r.get("Phone"))


def _email(r) -> bool:
    return _filled(r.get("Email")) and "@" in str(r.get("Email"))


def _valid_email(r) -> bool:
    return _email(r) and checks.email_status_is_ok(r.get("EmailStatus"))


def _valid_email_label(r) -> str:
    if not _email(r):
        return "[MISSING: EMAIL]"
    if not r.get("EmailStatus"):
        return "[EMAIL NOT CHECKED]"
    return "[INVALID EMAIL]"


# label -> (test, missing-field indicator)
CRITICAL_FIELDS = {
    "Business name": (lambda r: _filled(r.get("VendorName")), "[MISSING: NAME]"),
    "Website": (_website, "[NO DOMAIN FOUND]"),
    "Phone": (_phone, "[MISSING: PHONE]"),
    "Email": (_email, "[MISSING: EMAIL]"),
    "Valid email": (_valid_email, _valid_email_label),
    "Contact name": (lambda r: _filled(r.get("ContactName")), "[MISSING: CONTACT NAME]"),
    "Address": (lambda r: _filled(r.get("Address")), "[MISSING: ADDRESS]"),
    "Insurance/bond info": (lambda r: _filled(r.get("InsuranceOrBond")), "[MISSING: INSURANCE INFO]"),
}
DEFAULT_CRITICAL = ["Business name", "Website", "Phone", "Valid email"]
# Fields counted in the completion score (how much we know about a vendor, 0-100)
SCORED = ["Business name", "Website", "Phone", "Valid email", "Contact name", "Address", "Insurance/bond info"]


def evaluate(record: dict, critical: list[str]) -> tuple[str, str, int]:
    """(queue, missing-field indicators, completion score) for a record dict with DB column names."""
    missing = []
    for name in critical:
        test, label = CRITICAL_FIELDS[name]
        if not test(record):
            missing.append(label(record) if callable(label) else label)
    missing = list(dict.fromkeys(missing))  # "Email" and "Valid email" both report a missing email
    score = round(100 * sum(CRITICAL_FIELDS[n][0](record) for n in SCORED) / len(SCORED))
    return (PRIMARY if not missing else SECONDARY), " ".join(missing), score


def route(record: dict, critical: list[str]) -> dict:
    """Column updates for a record: Queue (unless locked), MissingFields and CompletionScore."""
    queue, missing, score = evaluate(record, critical)
    fields = {"MissingFields": missing or None, "CompletionScore": score}
    if not record.get("QueueLocked"):
        fields["Queue"] = queue
    return fields
