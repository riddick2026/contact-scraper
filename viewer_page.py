"""Database viewer page: browse, edit, add and delete saved records. Loaded by app.py."""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import streamlit as st

import checks
import db
import enrich
import rescan

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
    """Rescan the records, write what was found, and leave a summary message for the next run."""
    progress = st.progress(0.0, text="Loading records…")
    records = db.load_records(cfg, ids)
    search = enrich.WebSearch()
    results: list[tuple[dict, dict, str]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(rescan.rescan_record, r, search): r for r in records}
        for n, fut in enumerate(futures, start=1):
            rec = futures[fut]
            try:
                fields, note = fut.result()
            except Exception as exc:  # noqa: BLE001 - one bad record shouldn't stop the batch
                fields, note = {}, f"error: {type(exc).__name__}"
            results.append((rec, fields, note))
            progress.progress(n / len(records), text=f"Rescanned {n} / {len(records)}: {rec['VendorName']}")
    updated, skipped = db.update_scanned(cfg, [(r["Id"], r["RowVer"], f) for r, f, _ in results])
    lines = [f"- **{r['VendorName'] or r['Website']}**: {note}" for r, _, note in results]
    filled = sum(note.startswith("filled") for _, _, note in results)
    summary = (f"🔄 Rescanned {len(results)} record(s): {filled} with new information"
               + (f"; {skipped} skipped because they changed during the scan" if skipped else "") + ".\n\n"
               + "\n".join(lines[:50]) + (f"\n- … and {len(lines) - 50} more" if len(lines) > 50 else ""))
    st.session_state.viewer_message = ("success" if filled else "info", summary)


def run_email_check(ids: list[int]) -> None:
    rows = df_all[df_all["Id"].isin(ids)]
    with st.spinner(f"Checking emails for {len(rows)} record(s)…"):
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = dict(zip(rows["Id"].astype(int),
                                pool.map(lambda e: checks.check_emails(e) or "No email address", rows["Email"])))
        db.save_email_statuses(cfg, statuses)
    problems = {i: s for i, s in statuses.items() if not checks.email_status_is_ok(s) and s != "No email address"}
    names = dict(zip(rows["Id"].astype(int), rows["VendorName"]))
    detail = "\n".join(f"- **{names[i]}**: {s}" for i, s in list(problems.items())[:50])
    st.session_state.viewer_message = (
        "warning" if problems else "success",
        f"✉️ Checked {len(statuses)} record(s): {len(problems)} with email problems."
        + (f"\n\n{detail}" if detail else "")
        + "\n\nThese checks catch bad formats, typos, placeholder addresses and domains that can't receive "
          "mail. They can't confirm a specific mailbox exists.")


def to_excel(df: pd.DataFrame) -> bytes:
    out = df.copy()
    out = out.drop(columns=["Select"], errors="ignore")
    for col in ("FirstSeen", "LastScraped", "LastEdited", "EmailCheckedAt"):
        out[col] = pd.to_datetime(out[col]).dt.tz_localize(None)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        out.rename(columns={k: v.get("label", k) for k, v in COLUMN_CONFIG.items()}) \
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

m = st.columns(5)
m[0].metric("Records", len(df_all))
m[1].metric("With email", int((~_is_gap(df_all["Email"])).sum()))
m[2].metric("With valid phone", int(df_all["Phone"].map(checks.has_valid_phone).sum()))
m[3].metric("With insurance/bond", int((~_is_gap(df_all["InsuranceOrBond"])).sum()))
m[4].metric("Edited by hand", int(df_all["LastEdited"].notna().sum()))

SORTABLE = {DEFAULT_SORT: None, **{COLUMN_CONFIG[c]["label"]: c for c in db.VIEW_COLUMNS
                                    if c not in ("ExtraData", "PagesScraped")}}

f1, f2, f3 = st.columns([2, 1.3, 1.3])
search = f1.text_input("Search", placeholder="Vendor, email, phone, website, notes…")
show = f2.selectbox("Show", list(FILTERS))
picked = multiselect_filter(f3, "Added from", df_all["Source"])

g1, g2, g3 = st.columns(3)
picked_types = multiselect_filter(g1, "Vendor type", df_all["VendorType"], enrich.VENDOR_TYPES)
picked_onboarded = multiselect_filter(g2, "Onboarded", df_all["Onboarded"], db.ONBOARDED_VALUES)
picked_liability = multiselect_filter(g3, "Liability insurance", df_all["LiabilityInsurance"], db.LIABILITY_VALUES)

s1, s2, s3 = st.columns([2, 1, 1.6], vertical_alignment="bottom")
sort_label = s1.selectbox("Sort by", list(SORTABLE))
descending = s2.toggle("Descending", value=sort_label == DEFAULT_SORT)
select_all = s3.checkbox("Select all shown", help="Tick every record in the current view (for rescan or email check)")

view = FILTERS[show](df_all)
view = view[in_choices(view["Source"], picked) & in_choices(view["VendorType"], picked_types)
            & in_choices(view["Onboarded"], picked_onboarded) & in_choices(view["LiabilityInsurance"], picked_liability)]
if search.strip():
    text = view[["VendorName", "VendorType", "State", "Website", "ContactName", "Email", "Phone", "InsuranceOrBond",
                 "Notes", "ExtraData"]].fillna("").astype(str).agg(" ".join, axis=1)
    view = view[text.str.contains(search.strip(), case=False, regex=False)]
view = sort_view(view, sort_label, descending).reset_index(drop=True)
view.insert(0, "Select", select_all)

st.caption("Click a cell to edit it. Tick ✔ to select records for **Rescan** or **Check emails**. Add a record in "
           "the empty last row; select rows by their left edge and press Delete to remove them. Nothing is written "
           "until you click **Save changes**. Changing the search, filters or sort discards unsaved edits.")

view_key = "|".join(map(str, (show, search, picked, picked_types, picked_onboarded, picked_liability,
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
if not selected:
    tools.caption("Tick ✔ on records (or **Select all shown**) to rescan them or check their emails.")

if len(view) != len(df_all):
    st.caption(f"Showing {len(view)} of {len(df_all)} records.")
