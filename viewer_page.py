"""Database viewer page: browse, edit, add and delete saved records. Loaded by app.py."""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import streamlit as st

import db
import enrich

GAPS = set(db.GAP_VALUES) | {""}

FILTERS = {
    "All records": lambda df: df,
    "Missing email": lambda df: df[_is_gap(df["Email"])],
    "Missing phone": lambda df: df[_is_gap(df["Phone"])],
    "Missing contact name": lambda df: df[_is_gap(df["ContactName"])],
    "Missing insurance/bond": lambda df: df[_is_gap(df["InsuranceOrBond"])],
    "Website needs checking": lambda df: df[df["WebsiteSource"].isin([enrich.SRC_LOW, enrich.SRC_NONE])
                                           | _is_gap(df["Website"])],
    "Edited by hand": lambda df: df[df["LastEdited"].notna()],
}

COLUMN_CONFIG = {
    "Id": st.column_config.NumberColumn("ID", width="small", format="%d"),
    "VendorName": st.column_config.TextColumn("Vendor", width="medium"),
    "Website": st.column_config.TextColumn("Website", width="medium"),
    "ContactName": st.column_config.TextColumn("Contact name", width="medium"),
    "Email": st.column_config.TextColumn("Email", width="medium"),
    "Phone": st.column_config.TextColumn("Phone", width="medium"),
    "InsuranceOrBond": st.column_config.TextColumn("Insurance or bond", width="large"),
    "Notes": st.column_config.TextColumn("Notes", width="large"),
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


def to_excel(df: pd.DataFrame) -> bytes:
    out = df.copy()
    for col in ("FirstSeen", "LastScraped", "LastEdited"):
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
m[2].metric("With phone", int((~_is_gap(df_all["Phone"])).sum()))
m[3].metric("With insurance/bond", int((~_is_gap(df_all["InsuranceOrBond"])).sum()))
m[4].metric("Edited by hand", int(df_all["LastEdited"].notna().sum()))

f1, f2, f3 = st.columns([2, 1.3, 1.3])
search = f1.text_input("Search", placeholder="Vendor, email, phone, website, notes…")
show = f2.selectbox("Show", list(FILTERS))
sources = sorted(df_all["Source"].dropna().unique())
picked = f3.multiselect("Added from", sources, default=sources)

view = FILTERS[show](df_all)
view = view[view["Source"].isin(picked)]
if search.strip():
    text = view[["VendorName", "Website", "ContactName", "Email", "Phone", "InsuranceOrBond", "Notes",
                 "ExtraData"]].fillna("").astype(str).agg(" ".join, axis=1)
    view = view[text.str.contains(search.strip(), case=False, regex=False)]
view = view.reset_index(drop=True)

st.caption("Click a cell to edit it. Add a record in the empty last row; select rows by their left edge and "
           "press Delete to remove them. Nothing is written until you click **Save changes**. "
           "Changing the search or filters discards unsaved edits.")

editor_key = f"editor-{st.session_state.viewer_token}-{show}-{search}-{'|'.join(picked)}"
edited = st.data_editor(
    view,
    key=editor_key,
    column_config=COLUMN_CONFIG,
    column_order=db.VIEW_COLUMNS,
    disabled=[c for c in db.VIEW_COLUMNS if c not in db.EDITABLE_COLUMNS],
    num_rows="dynamic",
    hide_index=True,
    height=min(600, 38 + 35 * (len(view) + 1)),
)

updates, inserts, deletes = find_changes(view, edited)
pending = len(updates) + len(inserts) + len(deletes)

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

if len(view) != len(df_all):
    st.caption(f"Showing {len(view)} of {len(df_all)} records.")
