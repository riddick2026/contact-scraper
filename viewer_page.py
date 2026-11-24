"""Database viewer page: browse, edit, add and delete saved records. Loaded by app.py."""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import streamlit as st

import checks
import db
import discovery
import enrich
import pipeline
import routing

GAPS = set(db.GAP_VALUES) | {""}

FILTERS = {
    "All records": lambda df: df,
    "No email": lambda df: df[_is_gap(df["Email"])],
    "Valid phone number": lambda df: df[df["Phone"].map(checks.has_valid_phone)],
    "No valid phone number": lambda df: df[~df["Phone"].map(checks.has_valid_phone)],
    "Missing contact name": lambda df: df[_is_gap(df["ContactName"])],
    "Missing insurance/bond": lambda df: df[_is_gap(df["InsuranceOrBond"])],
    "Email problems": lambda df: df[df["EmailStatus"].notna() & ~df["EmailStatus"].map(checks.email_status_is_ok)
                                    & ~_is_gap(df["Email"])],
    "Emails not checked yet": lambda df: df[~_is_gap(df["Email"]) & df["EmailCheckedAt"].isna()],
    "Website needs checking": lambda df: df[df["WebsiteSource"].isin([enrich.SRC_LOW, enrich.SRC_NONE])
                                           | _is_gap(df["Website"])],
    "Edited by hand": lambda df: df[df["LastEdited"].notna()],
}
NONE_LABEL = "(not set)"
DEFAULT_SORT = "Last activity"

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
    "LicenseNumber": st.column_config.TextColumn("WA license #"),
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


def _is_gap(col: pd.Series) -> pd.Series:
    return col.fillna("").astype(str).str.strip().isin(GAPS)


def _norm(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def signed_in_user() -> str | None:
    """The person signed in through App Service authentication, when hosted on Azure."""
    return st.context.headers.get("X-Ms-Client-Principal-Name")


def load() -> None:
    with st.spinner("Loading records… (a paused database can take up to a minute to wake)"):
        df, versions = db.load_vendors(cfg)
    st.session_state.viewer = {"df": df, "versions": versions, "loaded": datetime.now()}
    st.session_state.viewer_token = st.session_state.get("viewer_token", 0) + 1


def find_changes(before: pd.DataFrame, after: pd.DataFrame) -> tuple[dict[int, dict], list[dict], list[int]]:
    """Diff the editor's output against what it was given, by record Id."""
    original = before.set_index("Id")
    kept = after[after["Id"].notna()].copy()
    kept["Id"] = kept["Id"].astype(int)
    kept = kept.set_index("Id")

    updates: dict[int, dict] = {}
    for row_id, row in kept.iterrows():
        changes = {c: row[c] for c in db.EDITABLE_COLUMNS if _norm(row[c]) != _norm(original.at[row_id, c])}
        if changes:
            updates[int(row_id)] = changes
    inserts = [r for r in after[after["Id"].isna()].to_dict("records")
               if any(_norm(r.get(c)) for c in db.EDITABLE_COLUMNS)]
    deletes = [int(i) for i in original.index if i not in kept.index]
    return updates, inserts, deletes


def multiselect_filter(container, label: str, series: pd.Series, known: list[str] | None = None) -> list[str]:
    """A filter that starts empty; empty means "all". Offers every known value plus any others in the data."""
    present = {NONE_LABEL if pd.isna(v) or v == "" else str(v) for v in series}
    options = list(known or []) + sorted(present - set(known or []))
    return container.multiselect(label, options, default=[], placeholder="All")


def in_choices(series: pd.Series, chosen: list[str]) -> pd.Series:
    if not chosen:  # nothing picked: don't filter on this field
        return pd.Series(True, index=series.index)
    return series.fillna(NONE_LABEL).replace("", NONE_LABEL).astype(str).isin(chosen)


def sort_view(df: pd.DataFrame, label: str, descending: bool) -> pd.DataFrame:
    if label == DEFAULT_SORT:
        key = df["LastEdited"].fillna(df["LastScraped"])
        return df.loc[key.sort_values(ascending=not descending, na_position="last").index]
    col = SORTABLE[label]
    if col in ("Id", "FirstSeen", "LastScraped", "LastEdited", "EmailCheckedAt"):
        return df.sort_values(col, ascending=not descending, na_position="last")
    return df.sort_values(col, ascending=not descending, na_position="last",
                          key=lambda s: s.fillna("").astype(str).str.lower().replace("", None))


def run_rescan(ids: list[int]) -> None:
    """Deep-dive the records again, save what was found and re-route them; leave a summary message."""
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
    summary = (f"🔄 Rescanned {len(results)} record(s): {filled} with new information.\n\n"
               + "\n".join(lines[:50]) + (f"\n- … and {len(lines) - 50} more" if len(lines) > 50 else ""))
    st.session_state.viewer_message = ("success" if filled else "info", summary)


def run_email_check(ids: list[int]) -> None:
    rows = df_all[df_all["Id"].isin(ids)]
    with st.spinner(f"Checking emails for {len(rows)} record(s)…"):
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = dict(zip(rows["Id"].astype(int),
                                pool.map(lambda e: checks.check_emails(e) or "No email address", rows["Email"])))
        db.save_email_statuses(cfg, statuses)
        db.reroute(cfg, list(statuses))
    problems = {i: s for i, s in statuses.items() if not checks.email_status_is_ok(s) and s != "No email address"}
    names = dict(zip(rows["Id"].astype(int), rows["VendorName"]))
    detail = "\n".join(f"- **{names[i]}**: {s}" for i, s in list(problems.items())[:50])
    st.session_state.viewer_message = (
        "warning" if problems else "success",
        f"✉️ Checked {len(statuses)} record(s): {len(problems)} with email problems."
        + (f"\n\n{detail}" if detail else "")
        + "\n\nThese checks catch bad formats, typos, placeholder addresses and domains that can't receive "
          "mail. They can't confirm a specific mailbox exists.")


EXPORT_COLUMNS = {"VendorName": "Business_Name", "VendorType": "Trade", "ContactName": "Contact_Name",
                  "Email": "Email", "EmailStatus": "Email_Check", "Phone": "Phone", "Website": "Website",
                  "Address": "Address", "City": "City", "Zip": "ZIP", "State": "State", "Region": "Region",
                  "ServiceArea": "Service_Area", "InsuranceOrBond": "Insurance_or_Bond",
                  "LicenseNumber": "WA_License", "UBI": "UBI", "Onboarded": "Onboarded",
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


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

st.title("🗂️ Database viewer")

try:
    cfg = db.load_config()
except db.DbError as exc:
    st.error(str(exc))
    st.stop()
if cfg is None:
    st.info("Azure SQL isn't set up on this computer yet. Add an `[azure_sql]` section to "
            "`.streamlit/secrets.toml` (see the README), then reload this page.")
    st.stop()

if "viewer" not in st.session_state:
    try:
        load()
    except db.DbError as exc:
        st.error(f"Couldn't load records: {exc}")
        if st.button("Try again"):
            st.rerun()
        st.stop()

data = st.session_state.viewer
df_all: pd.DataFrame = data["df"]

if msg := st.session_state.pop("viewer_message", None):
    getattr(st, msg[0])(msg[1])

top = st.columns([3, 1])
user = signed_in_user()
top[0].caption(f"**{cfg.label}** · {len(df_all)} records · loaded {data['loaded']:%H:%M:%S}"
               + (f" · signed in as {user}" if user else ""))
if top[1].button("🔄 Reload", width="stretch", help="Fetch the latest data (discards unsaved edits)."):
    try:
        load()
        st.rerun()
    except db.DbError as exc:
        st.error(f"Couldn't load records: {exc}")

COLUMN_CONFIG["VendorType"] = st.column_config.SelectboxColumn(
    "Vendor type", width="small",
    options=sorted(set(enrich.VENDOR_TYPES) | {t.vendor_type for t in discovery.TRADES.values()}
                   | set(df_all["VendorType"].dropna())))

n_primary = int((df_all["Queue"] == routing.PRIMARY).sum())
m = st.columns(6)
m[0].metric("Records", len(df_all))
m[1].metric("Primary queue", n_primary)
m[2].metric("Secondary queue", len(df_all) - n_primary)
m[3].metric("With email", int((~_is_gap(df_all["Email"])).sum()))
m[4].metric("With valid phone", int(df_all["Phone"].map(checks.has_valid_phone).sum()))
m[5].metric("Edited by hand", int(df_all["LastEdited"].notna().sum()))

QUEUE_VIEWS = {"All records": None, f"Primary ({n_primary})": routing.PRIMARY,
               f"Secondary ({len(df_all) - n_primary})": routing.SECONDARY}
queue_label = st.segmented_control("Queue", list(QUEUE_VIEWS), default="All records", label_visibility="collapsed")
queue_view = QUEUE_VIEWS.get(queue_label or "All records")
st.caption(f"Critical fields for the Primary queue: **{', '.join(db.critical_fields(cfg))}** (change them on the "
           "Scan page). Records you promote or demote by hand stay where you put them.")

SORTABLE = {DEFAULT_SORT: None, **{COLUMN_CONFIG[c]["label"]: c for c in db.VIEW_COLUMNS
                                    if c not in ("ExtraData", "PagesScraped")}}

f1, f2, f3 = st.columns([2, 1.3, 1.3])
search = f1.text_input("Search", placeholder="Vendor, email, phone, website, notes…")
show = f2.selectbox("Show", list(FILTERS))
picked = multiselect_filter(f3, "Added from", df_all["Source"])

g1, g2, g3, g4 = st.columns(4)
picked_types = multiselect_filter(g1, "Vendor type", df_all["VendorType"],
                                  list(dict.fromkeys([*enrich.VENDOR_TYPES,
                                                      *(t.vendor_type for t in discovery.TRADES.values())])))
picked_regions = multiselect_filter(g2, "Region", df_all["Region"])
picked_onboarded = multiselect_filter(g3, "Onboarded", df_all["Onboarded"], db.ONBOARDED_VALUES)
picked_liability = multiselect_filter(g4, "Liability insurance", df_all["LiabilityInsurance"], db.LIABILITY_VALUES)

s1, s2, s3 = st.columns([2, 1, 1.6], vertical_alignment="bottom")
sort_label = s1.selectbox("Sort by", list(SORTABLE))
descending = s2.toggle("Descending", value=sort_label == DEFAULT_SORT)
select_all = s3.checkbox("Select all shown", help="Tick every record in the current view (for rescan, email check or queue moves)")

view = FILTERS[show](df_all if queue_view is None else df_all[df_all["Queue"] == queue_view])
view = view[in_choices(view["Source"], picked) & in_choices(view["VendorType"], picked_types)
            & in_choices(view["Region"], picked_regions)
            & in_choices(view["Onboarded"], picked_onboarded) & in_choices(view["LiabilityInsurance"], picked_liability)]
if search.strip():
    text = view[["VendorName", "VendorType", "State", "Website", "ContactName", "Email", "Phone", "InsuranceOrBond",
                 "Notes", "ExtraData", "Address", "City", "Zip", "ServiceArea", "MissingFields", "LicenseNumber"]].fillna("").astype(str).agg(" ".join, axis=1)
    view = view[text.str.contains(search.strip(), case=False, regex=False)]
view = sort_view(view, sort_label, descending).reset_index(drop=True)
view.insert(0, "Select", select_all)

st.caption("Click a cell to edit it. Tick ✔ to select records for **Rescan**, **Check emails** or queue moves. Add a record in "
           "the empty last row; select rows by their left edge and press Delete to remove them. Nothing is written "
           "until you click **Save changes**. Changing the search, filters or sort discards unsaved edits.")

view_key = "|".join(map(str, (queue_view, show, search, picked, picked_types, picked_regions, picked_onboarded,
                                picked_liability,
                                sort_label, descending, select_all)))
editor_key = f"editor-{st.session_state.viewer_token}-{hash(view_key)}"
edited = st.data_editor(
    view,
    key=editor_key,
    column_config=COLUMN_CONFIG,
    column_order=["Select", *db.VIEW_COLUMNS],
    disabled=[c for c in db.VIEW_COLUMNS if c not in db.EDITABLE_COLUMNS],
    num_rows="dynamic",
    hide_index=True,
    height=min(600, 38 + 35 * (len(view) + 1)),
)

updates, inserts, deletes = find_changes(view, edited)
pending = len(updates) + len(inserts) + len(deletes)
selected = edited.loc[(edited["Select"] == True) & edited["Id"].notna(), "Id"].astype(int).tolist()  # noqa: E712

actions = st.container(horizontal=True, vertical_alignment="center")
if pending:
    parts = [f"{n} {label}" for n, label in ((len(updates), "edited"), (len(inserts), "new"),
                                              (len(deletes), "to delete")) if n]
    actions.markdown(f"**Unsaved:** {', '.join(parts)}")
confirm_delete = True
if deletes:
    names = ", ".join(str(view.loc[view["Id"] == i, "VendorName"].iloc[0] or
                          view.loc[view["Id"] == i, "Website"].iloc[0]) for i in deletes[:5])
    confirm_delete = actions.checkbox(f"Permanently delete {names}{' …' if len(deletes) > 5 else ''}")

if actions.button("💾 Save changes", type="primary", disabled=not pending or not confirm_delete):
    try:
        with st.spinner("Saving…"):
            names = {int(r.Id): _norm(r.VendorName) or _norm(r.Website) for r in view.itertuples()}
            res = db.apply_changes(cfg, data["versions"], updates, inserts,
                                   deletes if confirm_delete else [], names, editor=user)
        done = ", ".join(f"{n} {label}" for n, label in ((res.updated, "updated"), (res.inserted, "added"),
                                                          (res.deleted, "deleted")) if n) or "nothing changed"
        if res.conflicts:
            st.session_state.viewer_message = (
                "warning", f"Saved: {done}. Skipped because they changed since you loaded them "
                           f"(re-scraped or edited elsewhere): {', '.join(res.conflicts)}. Reloaded the latest "
                           "data; make those edits again if still needed.")
        else:
            st.session_state.viewer_message = ("success", f"💾 Saved: {done}.")
        load()
        st.rerun()
    except db.DbError as exc:
        st.error(f"Couldn't save: {exc}")

actions.download_button(
    "📥 Download this view (Excel)", data=to_excel(view),
    file_name=f"vendors_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    disabled=view.empty,
)

tools = st.container(horizontal=True, vertical_alignment="center")
busy_help = "Save or discard your edits first." if pending else None
if tools.button(f"🔄 Rescan selected ({len(selected)})", disabled=not selected or bool(pending), help=busy_help
                or "Look for missing contact info, email, phone, insurance and website. Never overwrites "
                   "existing values. About 5-15 seconds per record."):
    try:
        run_rescan(selected)
        load()
        st.rerun()
    except db.DbError as exc:
        st.error(f"Couldn't rescan: {exc}")
if tools.button(f"✉️ Check emails ({len(selected)})", disabled=not selected or bool(pending), help=busy_help
                or "Check each selected record's emails for bad formats, typos, placeholder addresses and "
                   "domains that can't receive mail."):
    try:
        run_email_check(selected)
        load()
        st.rerun()
    except db.DbError as exc:
        st.error(f"Couldn't check emails: {exc}")
def move(queue: str | None) -> None:
    try:
        if queue is None:
            counts = db.reroute(cfg, selected, unlock=True)
            st.session_state.viewer_message = (
                "success", f"↺ Handed {len(selected)} record(s) back to automatic routing: "
                           f"{counts['Primary']} Primary, {counts['Secondary']} Secondary.")
        else:
            n = db.set_queue(cfg, selected, queue)
            st.session_state.viewer_message = ("success", f"Moved {n} record(s) to the {queue} queue.")
        load()
        st.rerun()
    except db.DbError as exc:
        st.error(f"Couldn't move records: {exc}")


if tools.button(f"⬆️ Promote to Primary ({len(selected)})", disabled=not selected or bool(pending),
                help=busy_help or "Move to the Primary queue by hand; it stays there until you change it."):
    move(routing.PRIMARY)
if tools.button(f"⬇️ Demote to Secondary ({len(selected)})", disabled=not selected or bool(pending),
                help=busy_help or "Move to the Secondary queue by hand; it stays there until you change it."):
    move(routing.SECONDARY)
if tools.button(f"↺ Auto-route ({len(selected)})", disabled=not selected or bool(pending),
                help=busy_help or "Undo hand moves: put the records back in whichever queue their data qualifies for."):
    move(None)
if not selected:
    tools.caption("Tick ✔ on records (or **Select all shown**) to rescan, check emails or move them between queues.")

with st.expander("📤 Export queues"):
    fmt = st.radio("Format", ["Excel (.xlsx)", "CSV"], horizontal=True, label_visibility="collapsed")
    stamp = f"{datetime.now():%Y%m%d_%H%M}"
    ex = st.container(horizontal=True)
    for label, queue in (("Primary queue only", routing.PRIMARY), ("Secondary queue only", routing.SECONDARY),
                         ("All records (with Queue_Status, Missing_Fields)", None)):
        rows = df_all if queue is None else df_all[df_all["Queue"] == queue]
        out = export_frame(rows, with_queue=queue is None)
        slug = (queue or "all").lower()
        ex.download_button(f"📥 {label} ({len(rows)})",
                           data=to_excel(out, labelled=False) if fmt.startswith("Excel") else
                           out.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"vendors_{slug}_{stamp}.{'xlsx' if fmt.startswith('Excel') else 'csv'}",
                           mime=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                 if fmt.startswith("Excel") else "text/csv"),
                           disabled=rows.empty, key=f"export-{slug}")
    st.caption("Exports cover the whole queue, not just the filtered view (use **Download this view** for that).")

if len(view) != len(df_all):
    st.caption(f"Showing {len(view)} of {len(df_all)} records.")
