"""Data-quality checks: phone number validity and email deliverability.

The email check can't prove a mailbox exists (only sending a message does that), but it
catches what most often bounces: malformed addresses, typos in common domains, placeholder
and throwaway addresses, and domains that don't exist or don't accept mail.
"""

from __future__ import annotations

import re
import threading

import dns.exception
import dns.resolver

from scraper import EMAIL_RE, _clean_email

# --------------------------------------------------------------------------- #
# Phones
# --------------------------------------------------------------------------- #

def phone_is_valid(phone: str) -> bool:
    """A dialable North American number: 10 digits (optional leading 1), real area code and exchange."""
    digits = re.sub(r"\D", "", re.split(r"(?i)\s*(?:ext\.?|x)\s*\d", phone)[0])
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return False
    area, exchange, line = digits[:3], digits[3:6], digits[6:]
    if area[0] in "01" or exchange[0] in "01" or area[1:] == "11" or exchange[1:] == "11":
        return False
    if exchange == "555" and line.startswith("01"):
        return False  # 555-0100..0199 are reserved for fiction
    return len(set(digits)) > 2  # 333-333-3333 and similar fillers


def has_valid_phone(value) -> bool:
    return any(phone_is_valid(p) for p in re.split(r"[,;/]", str(value or "")) if p.strip())


# --------------------------------------------------------------------------- #
# Emails
# --------------------------------------------------------------------------- #

EMAIL_OK = "OK"

# Common misspellings of big mail providers
DOMAIN_TYPOS = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gamil.com": "gmail.com", "gmail.co": "gmail.com",
    "gmail.con": "gmail.com", "gnail.com": "gmail.com", "gmaill.com": "gmail.com", "gmal.com": "gmail.com",
    "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com", "yahoo.con": "yahoo.com", "yhoo.com": "yahoo.com",
    "hotmial.com": "hotmail.com", "hotmal.com": "hotmail.com", "hotmail.con": "hotmail.com",
    "outlok.com": "outlook.com", "outllook.com": "outlook.com", "outlook.con": "outlook.com",
    "comcast.com": "comcast.net", "icloud.co": "icloud.com", "aol.co": "aol.com",
}
# Throwaway-inbox services: addresses there stop working quickly
DISPOSABLE_DOMAINS = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "guerrillamail.net", "tempmail.com",
    "temp-mail.org", "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com",
    "maildrop.cc", "throwawaymail.com", "fakeinbox.com", "mintemail.com",
}

_resolver = dns.resolver.Resolver()
_resolver.lifetime = 6.0
_domain_cache: dict[str, str] = {}
_cache_lock = threading.Lock()


def _domain_status(domain: str) -> str:
    """EMAIL_OK, or why the domain can't receive mail. Cached per domain."""
    with _cache_lock:
        if domain in _domain_cache:
            return _domain_cache[domain]
    try:
        answers = _resolver.resolve(domain, "MX")
        hosts = [str(r.exchange).rstrip(".") for r in answers]
        status = "Domain doesn't accept email" if hosts in ([], [""]) else EMAIL_OK  # null MX (RFC 7505)
    except dns.resolver.NXDOMAIN:
        status = "Domain doesn't exist"
    except dns.resolver.NoAnswer:
        # No MX record: mail falls back to the domain's own address (RFC 5321), if it has one
        try:
            _resolver.resolve(domain, "A")
            status = EMAIL_OK
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            status = "Domain has no mail server"
        except dns.exception.DNSException:
            status = "Couldn't check domain (DNS lookup failed)"
    except dns.resolver.NoNameservers:
        status = "Domain is misconfigured (no working DNS)"
    except dns.exception.DNSException:
        status = "Couldn't check domain (DNS lookup failed)"
    if not status.startswith("Couldn't"):  # don't cache transient failures
        with _cache_lock:
            _domain_cache[domain] = status
    return status


def check_email(address: str) -> str:
    """EMAIL_OK, or a short reason the address will likely bounce."""
    address = address.strip().strip(".").lower()
    if not EMAIL_RE.fullmatch(address):
        return "Invalid format"
    if _clean_email(address) is None:
        return "Placeholder or system address"
    domain = address.rsplit("@", 1)[1]
    if domain in DOMAIN_TYPOS:
        return f"Likely typo (did you mean {DOMAIN_TYPOS[domain]}?)"
    if domain in DISPOSABLE_DOMAINS:
        return "Disposable address"
    return _domain_status(domain)


def check_emails(value) -> str | None:
    """Status line for a record's email cell ("a@x.com, b@y.com"); None if it holds no addresses."""
    addresses = [a.strip() for a in re.split(r"[,;\s]+", str(value or "")) if "@" in a]
    if not addresses:
        return None
    results = {a: check_email(a) for a in addresses}
    bad = {a: r for a, r in results.items() if r != EMAIL_OK}
    if not bad:
        return EMAIL_OK if len(results) == 1 else f"{EMAIL_OK} (all {len(results)})"
    detail = "; ".join(f"{a}: {r}" for a, r in bad.items())
    return (f"Problem: {detail}" if len(bad) == len(results)
            else f"Problem: {len(bad)} of {len(results)} - {detail}")


def email_status_is_ok(status) -> bool:
    return str(status or "").startswith(EMAIL_OK)
