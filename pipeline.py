"""Scan pipeline: Stage 1 discovery -> Stage 2 deep dive -> queue routing.

A scan runs in a background thread of the app server, so it keeps going if the browser tab is
closed. Progress lives in the database: Stage 1 saves every discovered business as a 'Pending'
record, and Stage 2 saves each record as soon as its deep dive finishes. A scan interrupted by a
restart (or cancelled) can be resumed: it simply picks up the records still 'Pending'.
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import checks
import db
import discovery
import enrich
import routing
from scraper import INSURANCE_UNKNOWN, crawl_site

LOCAL_AREA_CODES = {"206", "253", "360", "425", "509", "564", "503", "971"}


@dataclass
class ScanParams:
    trades: list[str]
    custom_trades: list[str] = field(default_factory=list)
    area_presets: list[str] = field(default_factory=list)
    custom_areas: str = ""
    critical: list[str] = field(default_factory=lambda: list(routing.DEFAULT_CRITICAL))
    max_new: int = 100          # cap on new businesses added per scan
    workers: int = 6            # sites crawled at once
    max_pages: int = 6          # pages crawled per site
    brave_key: str = ""         # optional; not saved with the run

    def describe(self) -> dict:
        d = asdict(self)
        d.pop("brave_key")
        return d


@dataclass
class RunState:
    run_id: int
    stage: str = "Starting"
    total: int = 0
    done: int = 0
    primary: int = 0
    secondary: int = 0
    lines: list[str] = field(default_factory=list)
    unflushed: list[str] = field(default_factory=list)
    last_flush: float = field(default_factory=time.monotonic)
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: bool = False


_RUNS: dict[int, RunState] = {}
_LOCK = threading.Lock()


def active_run() -> RunState | None:
    with _LOCK:
        return next((s for s in _RUNS.values() if not s.finished), None)


def get_state(run_id: int) -> RunState | None:
    with _LOCK:
        return _RUNS.get(run_id)


def reconcile(cfg: db.DbConfig) -> None:
    """Mark runs left 'Running' by a previous server process as Interrupted (resumable)."""
    with _LOCK:
        alive = {i for i, s in _RUNS.items() if not s.finished}
    db.mark_interrupted(cfg, alive)


def start(cfg: db.DbConfig, params: ScanParams, user: str | None) -> int:
    if active_run():
        raise RuntimeError("A scan is already running; wait for it to finish or cancel it.")
    trades = params.trades + params.custom_trades
    run_id = db.create_run(cfg, StartedBy=user, Trades=", ".join(trades),
                           Areas=", ".join(params.area_presets + ([params.custom_areas] if params.custom_areas else [])),
                           CriticalFields=", ".join(params.critical), Params=json.dumps(params.describe()),
                           Status="Running")
    _launch(cfg, run_id, params, discover=True)
    return run_id


def resume(cfg: db.DbConfig, run_id: int, params: ScanParams) -> None:
    if active_run():
        raise RuntimeError("A scan is already running.")
    db.update_run(cfg, run_id, Status="Running", FinishedAt=None, log=_stamp("Resumed"))
    _launch(cfg, run_id, params, discover=False)


def cancel(run_id: int) -> None:
    state = get_state(run_id)
    if state:
        state.cancel.set()


def _launch(cfg, run_id, params, discover):
    state = RunState(run_id)
    with _LOCK:
        _RUNS[run_id] = state
    threading.Thread(target=_run, args=(cfg, run_id, params, state, discover), daemon=True,
                     name=f"scan-{run_id}").start()


def _stamp(msg: str) -> str:
    return f"{datetime.now(timezone.utc):%H:%M:%S} {msg}\n"


def _log(cfg, state: RunState, msg: str, force: bool = False) -> None:
    line = _stamp(msg)
    with _LOCK:
        state.lines.append(line.rstrip())
        state.unflushed.append(line)
        due = force or len(state.unflushed) >= 25 or time.monotonic() - state.last_flush > 5
        text = "".join(state.unflushed) if due else ""
        if due:
            state.unflushed.clear()
            state.last_flush = time.monotonic()
    if text:
        try:
            db.update_run(cfg, state.run_id, log=text)
        except Exception:  # noqa: BLE001 - logging must never stop the scan
            pass


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #

def _run(cfg, run_id: int, params: ScanParams, state: RunState, discover: bool) -> None:
    try:
        db.set_setting(cfg, db.CRITICAL_SETTING, params.critical)
        if discover:
            state.stage = "Stage 1: discovery"
            trades = [discovery.TRADES[t] for t in params.trades] + [discovery.custom_trade(t)
                                                                      for t in params.custom_trades]
            area = discovery.build_area_filter(params.area_presets, params.custom_areas)
            _log(cfg, state, f"Stage 1: searching the WA L&I contractor registry for {', '.join(t.label for t in trades)}"
                             f" in {', '.join(area.labels) or 'all of WA'}", force=True)
            items = discovery.discover_lni(trades, area, log=lambda m: _log(cfg, state, m))
            _log(cfg, state, f"Stage 1: discovered {len(items)} unique businesses (duplicates across trades/areas merged)")
            new, matched, fresh = db.merge_discovered(cfg, run_id, items, limit=params.max_new)
            skipped = max(0, len(items) - matched - new)
            _log(cfg, state, f"Stage 1: {new} new records added, {matched} already in the database (gaps filled"
                             + (f"; {fresh} deep-dived in the last {db.REDIVE_AFTER_DAYS} days, not crawled again"
                                if fresh else "") + ")"
                             + (f", {skipped} not added (limit of {params.max_new} new per scan)" if skipped else ""),
                 force=True)
            db.update_run(cfg, run_id, Discovered=len(items), NewRecords=new)

        records = db.pending_records(cfg, run_id)
        state.stage, state.total = "Stage 2: deep dive", len(records)
        _log(cfg, state, f"Stage 2: deep diving {len(records)} records ({params.workers} at a time, "
                         f"up to {params.max_pages} pages per site)", force=True)
        search = enrich.WebSearch(params.brave_key)
        with ThreadPoolExecutor(max_workers=params.workers) as pool:
            futures = {pool.submit(deep_dive, r, search, params.critical, params.max_pages, state.cancel): r
                       for r in records}
            for fut in as_completed(futures):
                rec = futures[fut]
                if fut.cancelled():
                    continue  # never started: stays Pending for a later resume
                try:
                    fields, note = fut.result()
                except Exception as exc:  # noqa: BLE001 - one bad record never stops the batch
                    fields = {"EnrichmentStatus": "Failed", "ScrapeStatus": f"Deep dive error: {type(exc).__name__}",
                              **routing.route(rec, params.critical)}
                    note = f"error ({type(exc).__name__})"
                if fields.get("EnrichmentStatus") == "Cancelled":
                    continue  # left Pending for a later resume
                try:
                    db.save_enrichment(cfg, rec["Id"], fields)
                except Exception as exc:  # noqa: BLE001
                    note = f"couldn't save ({exc})"
                state.done += 1
                queue = fields.get("Queue", rec.get("Queue"))
                if queue == routing.PRIMARY:
                    state.primary += 1
                else:
                    state.secondary += 1
                _log(cfg, state, f"[{state.done}/{state.total}] {rec['VendorName']}: {note} -> {queue}"
                                 + (f" {fields['MissingFields']}" if fields.get("MissingFields") else ""))
                if state.cancel.is_set():
                    for f in futures:
                        f.cancel()

        counts = db.run_counts(cfg, run_id)
        status = "Cancelled" if state.cancel.is_set() else "Completed"
        left = counts["total"] - counts["done"]
        _log(cfg, state, f"{status}: {state.done} records deep-dived this run"
                         + (f", {left} still pending (resume to finish)" if left else "")
                         + f". This scan's {counts['total']} businesses: {counts['primary']} in Primary, "
                           f"{counts['secondary']} in Secondary", force=True)
        db.update_run(cfg, run_id, Status=status, FinishedAt=datetime.now(timezone.utc).replace(tzinfo=None),
                      Enriched=counts["done"], PrimaryCount=counts["primary"], SecondaryCount=counts["secondary"])
    except Exception as exc:  # noqa: BLE001
        _log(cfg, state, f"Scan failed: {exc}\n{traceback.format_exc()[-1500:]}", force=True)
        try:
            db.update_run(cfg, run_id, Status="Failed", FinishedAt=datetime.now(timezone.utc).replace(tzinfo=None))
        except Exception:  # noqa: BLE001
            pass
    finally:
        state.stage = "Finished"
        state.finished = True


# --------------------------------------------------------------------------- #
# Stage 2 for one record
# --------------------------------------------------------------------------- #

def _gap(value) -> bool:
    return not routing._filled(value)


def _digits(values) -> set[str]:
    return {re.sub(r"\D", "", p)[-10:] for p in values if len(re.sub(r"\D", "", p)) >= 10}


def _best_phone(phones: list[str]) -> str | None:
    valid = [p for p in phones if checks.phone_is_valid(p)]
    local = [p for p in valid if re.sub(r"\D", "", p)[-10:][:3] in LOCAL_AREA_CODES]
    return (local or valid or [None])[0]


def deep_dive(rec: dict, search: enrich.WebSearch, critical: list[str], max_pages: int,
              cancel: threading.Event | None = None) -> tuple[dict, str]:
    """({column: value} to save, short note). Fills only empty fields; never overwrites data."""
    if cancel is not None and cancel.is_set():
        return {"EnrichmentStatus": "Cancelled"}, "cancelled"
    vendor = rec["VendorName"] or ""
    location = f"{rec.get('City') or ''} WA".strip()
    fields: dict = {}
    notes: list[str] = []
    result = None
    site = None if _gap(rec.get("Website")) else rec["Website"]
    rec_phones = _digits(str(rec.get("Phone") or "").split(","))

    if site:
        result = crawl_site(site, max_pages)
    elif vendor:
        url, source = enrich.find_website(search, vendor, location)
        if not url:
            notes.append("search failed (try again later)" if source == enrich.SRC_FAILED else "no website found")
            fields["WebsiteSource"] = source
        else:
            candidate = crawl_site(url, max_pages)
            if candidate.pages_scraped:  # judge (and store) the site we actually landed on after redirects
                landed = candidate.pages_scraped[0]
                if db._domain(landed) != db._domain(url):
                    url = enrich._homepage(landed)
            if not candidate.status.startswith("OK"):
                notes.append(f"found {url} but it didn't load")
            else:
                site_digits = _digits(candidate.phones) | {d for d in rec_phones if d in re.sub(r"\D", "", candidate.text)}
                if rec_phones & site_digits:
                    verdict, why = "ok", ""            # the registry's phone number is on the site
                else:
                    verdict, why = enrich.site_matches_vendor(vendor, url, candidate.text, location)
                if verdict == "reject":
                    notes.append(f"no matching website ({url} rejected: {why})")
                    fields["WebsiteSource"] = enrich.SRC_NONE
                else:
                    result = candidate
                    fields.update(Website=url, WebsiteDomain=db._domain(url),
                                  WebsiteSource=enrich.SRC_LOW if verdict == "verify" else enrich.SRC_FOUND)
                    notes.append("website found" + (" (verify)" if verdict == "verify" else ""))

    if result is not None and result.status.startswith("OK"):
        found = {
            "ContactName": ", ".join(enrich.clean_names(sorted(result.names), vendor)[:4]),
            "Email": ", ".join(sorted(result.emails)),
            "InsuranceOrBond": "" if result.insurance_summary() == INSURANCE_UNKNOWN else result.insurance_summary(),
            "Address": next(iter(result.addresses), ""),
            "ServiceArea": next(iter(result.service_areas), ""),
            "Phone": _best_phone(sorted(result.phones)) or "",
        }
        filled = [c for c, v in found.items() if v and _gap(rec.get(c))]
        fields.update({c: found[c] for c in filled})
        if not rec.get("VendorType"):
            vtype = enrich.vendor_type(vendor) or enrich.guess_vendor_type(result.text)
            if vtype:
                fields["VendorType"] = vtype
        fields["PagesScraped"] = " | ".join(result.pages_scraped)
        if filled:
            notes.append("filled " + ", ".join(c.replace("ContactName", "contact").replace("InsuranceOrBond", "insurance")
                                               .lower() for c in filled))
        elif site:
            notes.append("nothing new on site")
        fields["ScrapeStatus"] = f"Deep dive: {len(result.pages_scraped)} pages" + (
            "" if result.status == "OK" else f" ({result.status})")
    elif result is not None:
        notes.append(f"website unreachable ({result.status})")
        fields["ScrapeStatus"] = result.status
    else:
        fields["ScrapeStatus"] = "No website found"

    # Check the email(s) we now have, then route
    email = fields.get("Email") or rec.get("Email")
    if not _gap(email):
        fields["EmailStatus"] = checks.check_emails(email)
        fields["EmailCheckedAt"] = datetime.now(timezone.utc).replace(tzinfo=None)
    fields.update(routing.route({**rec, **fields}, critical))
    fields["EnrichmentStatus"] = "Done"
    return fields, "; ".join(notes) or "done"
