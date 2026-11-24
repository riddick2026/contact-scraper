"""Database viewer page: browse, edit, add and delete saved records. Loaded by app.py."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

import checks
import db
import discovery
import enrich
import records_ui as ui
import routing

FILTERS = {
    "All records": lambda df: df,
    "No email": lambda df: df[ui.is_gap(df["Email"])],
    "Valid phone number": lambda df: df[df["Phone"].map(checks.has_valid_phone)],
    "No valid phone number": lambda df: df[~df["Phone"].map(checks.has_valid_phone)],
    "Missing contact name": lambda df: df[ui.is_gap(df["ContactName"])],
    "Missing insurance/bond": lambda df: df[ui.is_gap(df["InsuranceOrBond"])],
    "Email problems": lambda df: df[df["EmailStatus"].notna() & ~df["EmailStatus"].map(checks.email_status_is_ok)
                                    & ~ui.is_gap(df["Email"])],
    "Emails not checked yet": lambda df: df[~ui.is_gap(df["Email"]) & df["EmailCheckedAt"].isna()],
    "Website needs checking": lambda df: df[df["WebsiteSource"].isin([enrich.SRC_LOW, enrich.SRC_NONE])
                                           | ui.is_gap(df["Website"])],
    "Edited by hand": lambda df: df[df["LastEdited"].notna()],
}
NONE_LABEL = "(not set)"
DEFAULT_SORT = "Last activity"


def load() -> None:
    with st.spinner("Loading records… (a paused database can take up to a minute to wake)"):
        version = db.data_version(cfg)
        df, versions = db.load_vendors(cfg)
    st.session_state.viewer = {"df": df, "versions": versions, "loaded": datetime.now(), "version": version}
    st.session_state.viewer_token = st.session_state.get("viewer_token", 0) + 1


def notify(kind: str, message: str) -> None:
    st.session_state.viewer_message = (kind, message)


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


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

st.header("🗂️ Database viewer")

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

elif st.session_state.get("page_entered", True):
    # Coming back from another page (e.g. after a scan): refresh if any record changed meanwhile.
    # Only on arrival, so edits in progress on this page are never discarded.
    try:
        if db.data_version(cfg) != st.session_state.viewer.get("version"):
            load()
    except db.DbError as exc:
        st.warning(f"Couldn't check for new data ({exc}); showing what was loaded earlier.")

data = st.session_state.viewer
df_all: pd.DataFrame = data["df"]

if msg := st.session_state.pop("viewer_message", None):
    getattr(st, msg[0])(msg[1])

top = st.columns([3, 1])
user = ui.signed_in_user()
top[0].caption(f"**{cfg.label}** · {len(df_all)} records · loaded {data['loaded']:%H:%M:%S}"
               + (f" · signed in as {user}" if user else ""))
if top[1].button("🔄 Reload", width="stretch", help="Fetch the latest data (discards unsaved edits)."):
    try:
        load()
        st.rerun()
    except db.DbError as exc:
        st.error(f"Couldn't load records: {exc}")

COLUMN_CONFIG = ui.column_config(df_all)

n_primary = int((df_all["Queue"] == routing.PRIMARY).sum())
m = st.columns(6)
m[0].metric("Records", len(df_all))
m[1].metric("Primary queue", n_primary)
m[2].metric("Secondary queue", len(df_all) - n_primary)
m[3].metric("With email", int((~ui.is_gap(df_all["Email"])).sum()))
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

g0, g1, g2, g3, g4 = st.columns(5)
picked_states = multiselect_filter(g0, "State", df_all["State"], list(discovery.STATES.values()))
picked_types = multiselect_filter(g1, "Vendor type", df_all["VendorType"],
                                  list(dict.fromkeys([*enrich.VENDOR_TYPES,
                                                      *(t.vendor_type for t in discovery.TRADES.values())])))
picked_regions = multiselect_filter(g2, "Region", df_all["Region"])
picked_onboarded = multiselect_filter(g3, "Onboarded", df_all["Onboarded"], db.ONBOARDED_VALUES)
picked_liability = multiselect_filter(g4, "Liability insurance", df_all["LiabilityInsurance"], db.LIABILITY_VALUES)

s1, s2, s3 = st.columns([2, 1, 1.6], vertical_alignment="bottom")
sort_label = s1.selectbox("Sort by", list(SORTABLE))
descending = s2.toggle("Descending", value=sort_label == DEFAULT_SORT)
select_all = s3.checkbox("Select all shown",
                         help="Tick every record in the current view (for rescan, email check or queue moves)")

view = FILTERS[show](df_all if queue_view is None else df_all[df_all["Queue"] == queue_view])
view = view[in_choices(view["Source"], picked) & in_choices(view["VendorType"], picked_types)
            & in_choices(view["Region"], picked_regions) & in_choices(view["State"], picked_states)
            & in_choices(view["Onboarded"], picked_onboarded) & in_choices(view["LiabilityInsurance"], picked_liability)]
if search.strip():
    text = view[["VendorName", "VendorType", "State", "Website", "ContactName", "Email", "Phone", "InsuranceOrBond",
                 "Notes", "ExtraData", "Address", "City", "Zip", "ServiceArea", "MissingFields",
                 "LicenseNumber"]].fillna("").astype(str).agg(" ".join, axis=1)
    view = view[text.str.contains(search.strip(), case=False, regex=False)]
view = sort_view(view, sort_label, descending).reset_index(drop=True)
view.insert(0, "Select", select_all)

st.caption("Click a cell to edit it. Tick ✔ to select records for **Rescan**, **Check emails** or queue moves. Add a "
           "record in the empty last row; select rows by their left edge and press Delete to remove them. Nothing is "
           "written until you click **Save changes**. Changing the search, filters or sort discards unsaved edits.")

view_key = "|".join(map(str, (queue_view, show, search, picked, picked_states, picked_types, picked_regions,
                                picked_onboarded, picked_liability, sort_label, descending, select_all)))
edited = st.data_editor(
    view,
    key=f"editor-{st.session_state.viewer_token}-{hash(view_key)}",
    column_config=COLUMN_CONFIG,
    column_order=["Select", *db.VIEW_COLUMNS],
    disabled=[c for c in db.VIEW_COLUMNS if c not in db.EDITABLE_COLUMNS],
    num_rows="dynamic",
    hide_index=True,
    height=min(600, 38 + 35 * (len(view) + 1)),
)

ui.record_tools(cfg, view, edited, data["versions"], user, notify, load, key="viewer")
st.download_button(
    "📥 Download this view (Excel)", data=ui.to_excel(view),
    file_name=f"vendors_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    disabled=view.empty,
)

with st.expander("📤 Export queues"):
    ui.export_buttons(df_all, key="viewer", note="Exports cover the whole queue, not just the filtered view "
                                                 "(use **Download this view** for that).")

if len(view) != len(df_all):
    st.caption(f"Showing {len(view)} of {len(df_all)} records.")
