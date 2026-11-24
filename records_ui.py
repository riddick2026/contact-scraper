"""Record-editing tools shared by the Database viewer and the Scan page's results panel.

Both pages show records in an editable grid with a ✔ selection column; these helpers turn the
grid's edits and selection into saves, rescans, email checks, queue moves and exports. Results
are reported through `notify(kind, message)`, and `reload()` refreshes the page's data.
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable

import pandas as pd
import streamlit as st

import checks
import db
import discovery
import enrich
import pipeline
import routing

GAPS = set(db.GAP_VALUES) | {""}

COLUMN_CONFIG = {
    "Select": st.column_config.CheckboxColumn("✔", width="small", help="Select records to rescan or check"),
    "Id": st.column_config.NumberColumn("ID", width="small", format="%d"),
    "Queue": st.column_config.TextColumn("Queue", width="small"),
    "MissingFields": st.column_config.TextColumn("Missing", width="medium",
                                                 help="Critical fields the deep dive couldn't find"),
    "VendorName": st.column_config.TextColumn("Vendor", width="medium"),
    "VendorType": st.column_config.SelectboxColumn("Vendor type", options=enrich.VENDOR_TYPES, width="small"),
    "State": st.column_config.TextColumn("State", width="small"),
    "Onboarded": st.column_config.SelectboxColumn("Onboarded", options=db.ONBOARDED_VALUES, width="small",
                                                  required=True),
    "LiabilityInsurance": st.column_config.SelectboxColumn("Liability insurance", options=db.LIABILITY_VALUES,
                                                           width="small", required=True),
    "Website": st.column_config.TextColumn("Website", width="medium"),
    "ContactName": st.column_config.TextColumn("Contact name", width="medium"),
    "Email": st.column_config.TextColumn("Email", width="medium"),
    "Phone": st.column_config.TextColumn("Phone", width="medium"),
    "InsuranceOrBond": st.column_config.TextColumn("Insurance or bond", width="large"),
    "Notes": st.column_config.TextColumn("Notes", width="large"),
    "EmailStatus": st.column_config.TextColumn("Email check", width="medium"),
    "EmailCheckedAt": st.column_config.DatetimeColumn("Email checked (UTC)", format="YYYY-MM-DD HH:mm"),
    "Address": st.column_config.TextColumn("Address", width="medium"),
    "City": st.column_config.TextColumn("City", width="small"),
    "Zip": st.column_config.TextColumn("ZIP", width="small"),
    "Region": st.column_config.TextColumn("Region", width="small"),
    "ServiceArea": st.column_config.TextColumn("Service area", width="medium"),
    "CompletionScore": st.column_config.ProgressColumn("Completeness", min_value=0, max_value=100, format="%d%%",
                                                       width="small"),
    "LicenseNumber": st.column_config.TextColumn("License #"),
    "UBI": st.column_config.TextColumn("UBI"),
    "EnrichmentStatus": st.column_config.TextColumn("Deep dive"),
    "EnrichedAt": st.column_config.DatetimeColumn("Deep-dived (UTC)", format="YYYY-MM-DD HH:mm"),
    "QueueLocked": st.column_config.CheckboxColumn("Moved by hand", width="small"),
    "ScanRunId": st.column_config.NumberColumn("Scan #", format="%d", width="small"),
    "WebsiteSource": st.column_config.TextColumn("Website source"),
    "ScrapeStatus": st.column_config.TextColumn("Scrape status"),
    "Source": st.column_config.TextColumn("Added from", width="small"),
    "SourceFile": st.column_config.TextColumn("File"),
    "ExtraData": st.column_config.TextColumn("Other sheet columns"),
    "FirstSeen": st.column_config.DatetimeColumn("First seen (UTC)", format="YYYY-MM-DD HH:mm"),
    "LastScraped": st.column_config.DatetimeColumn("Last scraped (UTC)", format="YYYY-MM-DD HH:mm"),
    "LastEdited": st.column_config.DatetimeColumn("Last edited (UTC)", format="YYYY-MM-DD HH:mm"),
    "LastEditedBy": st.column_config.TextColumn("Edited by"),
}

Notify = Callable[[str, str], None]


def is_gap(col: pd.Series) -> pd.Series:
    return col.fillna("").astype(str).str.strip().isin(GAPS)


def norm(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def signed_in_user() -> str | None:
    """The person signed in through App Service authentication, when hosted on Azure."""
    return st.context.headers.get("X-Ms-Client-Principal-Name")


def column_config(df: pd.DataFrame) -> dict:
    """COLUMN_CONFIG with the vendor-type dropdown offering every known type plus any in the data."""
    cfg = dict(COLUMN_CONFIG)
    cfg["VendorType"] = st.column_config.SelectboxColumn(
        "Vendor type", width="small",
        options=sorted(set(enrich.VENDOR_TYPES) | {t.vendor_type for t in discovery.TRADES.values()}
                       | set(df["VendorType"].dropna())))
    return cfg


def find_changes(before: pd.DataFrame, after: pd.DataFrame) -> tuple[dict[int, dict], list[dict], list[int]]:
    """Diff the editor's output against what it was given, by record Id."""
    original = before.set_index("Id")
    kept = after[after["Id"].notna()].copy()
    kept["Id"] = kept["Id"].astype(int)
    kept = kept.set_index("Id")

    updates: dict[int, dict] = {}
    for row_id, row in kept.iterrows():
        changes = {c: row[c] for c in db.EDITABLE_COLUMNS if norm(row[c]) != norm(original.at[row_id, c])}
        if changes:
            updates[int(row_id)] = changes
    inserts = [r for r in after[after["Id"].isna()].to_dict("records")
               if any(norm(r.get(c)) for c in db.EDITABLE_COLUMNS)]
    deletes = [int(i) for i in original.index if i not in kept.index]
    return updates, inserts, deletes


def selected_ids(edited: pd.DataFrame) -> list[int]:
    return edited.loc[(edited["Select"] == True) & edited["Id"].notna(), "Id"].astype(int).tolist()  # noqa: E712


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #

def rescan(cfg: db.DbConfig, ids: list[int], notify: Notify) -> None:
    """Deep-dive the records again, save what was found and re-route them."""
    progress = st.progress(0.0, text="Loading records…")
    records = db.load_records(cfg, ids)
    search = enrich.WebSearch()
    critical = db.critical_fields(cfg)
    results: list[tuple[dict, dict, str]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(pipeline.deep_dive, r, search, critical, 6): r for r in records}
        for n, fut in enumerate(futures, start=1):
            rec = futures[fut]
            try:
                fields, note = fut.result()
                db.save_enrichment(cfg, rec["Id"], fields)
            except Exception as exc:  # noqa: BLE001 - one bad record shouldn't stop the batch
                fields, note = {}, f"error: {type(exc).__name__}"
            results.append((rec, fields, note))
            progress.progress(n / len(records), text=f"Rescanned {n} / {len(records)}: {rec['VendorName']}")
    lines = [f"- **{r['VendorName'] or r['Website']}**: {note} → {f.get('Queue', r.get('Queue'))}"
             for r, f, note in results]
    filled = sum("filled" in note or "website found" in note for _, _, note in results)
    notify("success" if filled else "info",
           f"🔄 Rescanned {len(results)} record(s): {filled} with new information.\n\n"
           + "\n".join(lines[:50]) + (f"\n- … and {len(lines) - 50} more" if len(lines) > 50 else ""))


def check_emails(cfg: db.DbConfig, rows: pd.DataFrame, notify: Notify) -> None:
    with st.spinner(f"Checking emails for {len(rows)} record(s)…"):
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = dict(zip(rows["Id"].astype(int),
                                pool.map(lambda e: checks.check_emails(e) or "No email address", rows["Email"])))
        db.save_email_statuses(cfg, statuses)
        db.reroute(cfg, list(statuses))
    problems = {i: s for i, s in statuses.items() if not checks.email_status_is_ok(s) and s != "No email address"}
    names = dict(zip(rows["Id"].astype(int), rows["VendorName"]))
    detail = "\n".join(f"- **{names[i]}**: {s}" for i, s in list(problems.items())[:50])
    notify("warning" if problems else "success",
           f"✉️ Checked {len(statuses)} record(s): {len(problems)} with email problems."
           + (f"\n\n{detail}" if detail else "")
           + "\n\nThese checks catch bad formats, typos, placeholder addresses and domains that can't receive "
             "mail. They can't confirm a specific mailbox exists.")


def move(cfg: db.DbConfig, ids: list[int], queue: str | None, notify: Notify) -> None:
    """Promote/demote by hand, or (queue=None) hand the records back to automatic routing."""
    if queue is None:
        counts = db.reroute(cfg, ids, unlock=True)
        notify("success", f"↺ Handed {len(ids)} record(s) back to automatic routing: "
                          f"{counts['Primary']} Primary, {counts['Secondary']} Secondary.")
    else:
        n = db.set_queue(cfg, ids, queue)
        notify("success", f"Moved {n} record(s) to the {queue} queue.")


def save(cfg: db.DbConfig, view: pd.DataFrame, versions: dict, changes: tuple, user: str | None,
         notify: Notify) -> None:
    updates, inserts, deletes = changes
    with st.spinner("Saving…"):
        names = {int(r.Id): norm(r.VendorName) or norm(r.Website) for r in view.itertuples()}
        res = db.apply_changes(cfg, versions, updates, inserts, deletes, names, editor=user)
    done = ", ".join(f"{n} {label}" for n, label in ((res.updated, "updated"), (res.inserted, "added"),
                                                      (res.deleted, "deleted")) if n) or "nothing changed"
    if res.conflicts:
        notify("warning", f"Saved: {done}. Skipped because they changed since you loaded them (re-scraped or "
                          f"edited elsewhere): {', '.join(res.conflicts)}. Reloaded the latest data; make those "
                          "edits again if still needed.")
    else:
        notify("success", f"💾 Saved: {done}.")


def record_tools(cfg: db.DbConfig, view: pd.DataFrame, edited: pd.DataFrame, versions: dict, user: str | None,
                 notify: Notify, reload: Callable[[], None], key: str, allow_delete: bool = True) -> None:
    """Unsaved-changes summary and the Save / Rescan / Check emails / queue-move buttons for a grid."""
    updates, inserts, deletes = find_changes(view, edited)
    if not allow_delete:
        deletes = []
    pending = len(updates) + len(inserts) + len(deletes)
    selected = selected_ids(edited)

    def run(action: Callable[[], None], what: str) -> None:
        try:
            action()
            reload()
            st.rerun()
        except db.DbError as exc:
            st.error(f"Couldn't {what}: {exc}")

    actions = st.container(horizontal=True, vertical_alignment="center")
    if pending:
        parts = [f"{n} {label}" for n, label in ((len(updates), "edited"), (len(inserts), "new"),
                                                  (len(deletes), "to delete")) if n]
        actions.markdown(f"**Unsaved:** {', '.join(parts)}")
    confirm_delete = True
    if deletes:
        names = ", ".join(str(view.loc[view["Id"] == i, "VendorName"].iloc[0] or
                              view.loc[view["Id"] == i, "Website"].iloc[0]) for i in deletes[:5])
        confirm_delete = actions.checkbox(f"Permanently delete {names}{' …' if len(deletes) > 5 else ''}",
                                          key=f"{key}-confirm-delete")
    if actions.button("💾 Save changes", type="primary", disabled=not pending or not confirm_delete,
                      key=f"{key}-save"):
        run(lambda: save(cfg, view, versions, (updates, inserts, deletes), user, notify), "save")

    tools = st.container(horizontal=True, vertical_alignment="center")
    busy = "Save or discard your edits first." if pending else None
    off = not selected or bool(pending)
    if tools.button(f"🔄 Rescan selected ({len(selected)})", disabled=off, key=f"{key}-rescan", help=busy
                    or "Look for missing contact info, email, phone, insurance and website. Never overwrites "
                       "existing values. About 5-15 seconds per record."):
        run(lambda: rescan(cfg, selected, notify), "rescan")
    if tools.button(f"✉️ Check emails ({len(selected)})", disabled=off, key=f"{key}-emails", help=busy
                    or "Check each selected record's emails for bad formats, typos, placeholder addresses and "
                       "domains that can't receive mail."):
        run(lambda: check_emails(cfg, edited[edited["Id"].isin(selected)], notify), "check emails")
    if tools.button(f"⬆️ Promote to Primary ({len(selected)})", disabled=off, key=f"{key}-promote",
                    help=busy or "Move to the Primary queue by hand; it stays there until you change it."):
        run(lambda: move(cfg, selected, routing.PRIMARY, notify), "move records")
    if tools.button(f"⬇️ Demote to Secondary ({len(selected)})", disabled=off, key=f"{key}-demote",
                    help=busy or "Move to the Secondary queue by hand; it stays there until you change it."):
        run(lambda: move(cfg, selected, routing.SECONDARY, notify), "move records")
    if tools.button(f"↺ Auto-route ({len(selected)})", disabled=off, key=f"{key}-autoroute",
                    help=busy or "Undo hand moves: put the records back in whichever queue their data qualifies "
                                 "for."):
        run(lambda: move(cfg, selected, None, notify), "move records")
    if not selected:
        tools.caption("Tick ✔ on records (or **Select all shown**) to rescan, check emails or move them between "
                      "queues.")


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #

EXPORT_COLUMNS = {"VendorName": "Business_Name", "VendorType": "Trade", "ContactName": "Contact_Name",
                  "Email": "Email", "EmailStatus": "Email_Check", "Phone": "Phone", "Website": "Website",
                  "Address": "Address", "City": "City", "Zip": "ZIP", "State": "State", "Region": "Region",
                  "ServiceArea": "Service_Area", "InsuranceOrBond": "Insurance_or_Bond",
                  "LicenseNumber": "License_Number", "UBI": "UBI", "Onboarded": "Onboarded",
                  "LiabilityInsurance": "Liability_Insurance", "CompletionScore": "Completeness_Pct",
                  "EnrichedAt": "Enriched_At_UTC", "Notes": "Notes", "Id": "Record_ID"}


def export_frame(df: pd.DataFrame, with_queue: bool) -> pd.DataFrame:
    """Clean, CRM-friendly columns; the all-records export adds Queue_Status and Missing_Fields."""
    out = df[list(EXPORT_COLUMNS)].rename(columns=EXPORT_COLUMNS)
    if with_queue:
        out.insert(0, "Queue_Status", df["Queue"].values)
        out.insert(1, "Missing_Fields", df["MissingFields"].fillna("").values)
    out["Enriched_At_UTC"] = pd.to_datetime(out["Enriched_At_UTC"]).dt.tz_localize(None)
    return out


def to_excel(df: pd.DataFrame, labelled: bool = True) -> bytes:
    out = df.copy()
    out = out.drop(columns=["Select"], errors="ignore")
    for col in ("FirstSeen", "LastScraped", "LastEdited", "EmailCheckedAt", "EnrichedAt"):
        if col in out:
            out[col] = pd.to_datetime(out[col]).dt.tz_localize(None)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        (out.rename(columns={k: v.get("label", k) for k, v in COLUMN_CONFIG.items()}) if labelled else out) \
           .to_excel(writer, index=False, sheet_name="Vendors")
        sheet = writer.sheets["Vendors"]
        for idx, col in enumerate(out.columns, start=1):
            width = max([len(str(col)), *out[col].astype(str).str.len().tolist()] or [10])
            sheet.column_dimensions[sheet.cell(row=1, column=idx).column_letter].width = min(width + 2, 50)
        sheet.freeze_panes = "B2"
    return buffer.getvalue()


def export_buttons(df: pd.DataFrame, key: str, name: str = "vendors", note: str = "") -> None:
    """Primary-only, Secondary-only and All (with Queue_Status, Missing_Fields) downloads for these records."""
    fmt = st.radio("Format", ["Excel (.xlsx)", "CSV"], horizontal=True, label_visibility="collapsed",
                   key=f"{key}-format")
    excel = fmt.startswith("Excel")
    stamp = f"{datetime.now():%Y%m%d_%H%M}"
    ex = st.container(horizontal=True)
    for label, queue in (("Primary only", routing.PRIMARY), ("Secondary only", routing.SECONDARY),
                         ("All (with Queue_Status, Missing_Fields)", None)):
        rows = df if queue is None else df[df["Queue"] == queue]
        out = export_frame(rows, with_queue=queue is None)
        slug = (queue or "all").lower()
        ex.download_button(f"📥 {label} ({len(rows)})",
                           data=to_excel(out, labelled=False) if excel else out.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"{name}_{slug}_{stamp}.{'xlsx' if excel else 'csv'}",
                           mime=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                 if excel else "text/csv"),
                           disabled=rows.empty, key=f"{key}-export-{slug}")
    if note:
        st.caption(note)
