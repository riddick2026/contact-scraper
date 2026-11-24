"""Scan page: discover vendors by trade and area, deep-dive them, and route them into queues."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import db
import discovery
import pipeline
import records_ui as ui
import routing

st.title("🛰️ Scan for vendors")
st.caption("Finds businesses for the trades and areas you pick (from each state's contractor license data where it "
           "exists, otherwise by web search), crawls each one's website for contact details, and sorts them into the "
           "**Primary** queue (all critical fields found) or the **Secondary** queue (something missing). Results are "
           "saved as they come in.")

try:
    cfg = db.load_config()
except db.DbError as exc:
    st.error(str(exc))
    st.stop()
if cfg is None:
    st.info("Azure SQL isn't set up on this computer yet (see the README).")
    st.stop()

if "scan_reconciled" not in st.session_state:
    try:
        pipeline.reconcile(cfg)
    except db.DbError as exc:
        st.error(f"Couldn't reach the database: {exc}")
        st.stop()
    st.session_state.scan_reconciled = True

user = st.context.headers.get("X-Ms-Client-Principal-Name")


@st.cache_data(ttl=600, show_spinner=False)
def estimate(state: str, trades: tuple, customs: tuple, presets: tuple, custom_areas: str) -> dict:
    ts = [discovery.TRADES[t] for t in trades] + [discovery.custom_trade(c) for c in customs]
    return discovery.count_registry(state, ts, discovery.build_area_filter(list(presets), custom_areas, state))


def last_params() -> dict:
    try:
        runs = db.list_runs(cfg, 1)
        return json.loads(runs.iloc[0]["Params"]) if len(runs) and runs.iloc[0]["Params"] else {}
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# Live progress of the running scan
# --------------------------------------------------------------------------- #

@st.fragment(run_every=2)
def live_panel(run_id: int) -> None:
    state = pipeline.get_state(run_id)
    if state is None or state.finished:
        st.session_state.scan_finished = run_id
        st.rerun(scope="app")
        return
    st.subheader(f"Scan #{run_id} running · {state.stage}")
    if state.total:
        st.progress(state.done / state.total, text=f"Deep dive: {state.done} of {state.total} records")
    m = st.columns(4)
    m[0].metric("Deep-dived", f"{state.done} / {state.total or '…'}")
    m[1].metric("→ Primary", state.primary)
    m[2].metric("→ Secondary", state.secondary)
    m[3].metric("Remaining", max(0, state.total - state.done) if state.total else "…")
    st.code("\n".join(state.lines[-150:]) or "Starting…", language=None, height=360)
    if st.button("⏹ Cancel scan", help="Stops after the records in progress. Finished records are kept; "
                                       "the rest can be resumed later."):
        pipeline.cancel(run_id)
        st.toast("Cancelling… records in progress will finish first.")


if (finished := st.session_state.pop("scan_finished", None)) is not None:
    st.success(f"✅ Scan #{finished} finished. Its results are at the bottom of this page.")

active = pipeline.active_run()
if active:
    st.info("A scan is running. You can leave this page or close the tab; it keeps going and saves as it goes.")
    live_panel(active.run_id)
    st.stop()

# --------------------------------------------------------------------------- #
# New scan
# --------------------------------------------------------------------------- #

prev = last_params()
saved_critical = db.critical_fields(cfg)

with st.container(border=True):
    st.subheader("New scan")
    states = list(discovery.STATES)
    scan_state = st.selectbox("State", states, index=states.index(prev.get("state", "WA")) if prev.get("state", "WA")
                              in states else 0, format_func=lambda s: discovery.STATES[s],
                              help="Areas and data sources change with the state.")
    c1, c2 = st.columns(2)
    trades = c1.multiselect("Trades", list(discovery.TRADES),
                            default=[t for t in prev.get("trades", ["Roofing", "Gutters"]) if t in discovery.TRADES])
    custom_trades = [t.strip() for t in c1.text_input(
        "Other trades (optional)", value=", ".join(prev.get("custom_trades", [])),
        placeholder="e.g. Fencing, Window cleaning",
        help="Matched against business names and license specialties.").split(",") if t.strip()]
    chosen = [discovery.TRADES[t] for t in trades] + [discovery.custom_trade(t) for t in custom_trades]
    if chosen:
        c1.caption("**Sources:** " + " · ".join(
            f"{t.label}: {' + '.join(discovery.sources_for(scan_state, t))}" for t in chosen)
            + ("  \n*Web search* finds businesses with a website; it's used where the state has no usable "
               "license data for that trade." if any(discovery.sources_for(scan_state, t) == [discovery.SOURCE_WEB]
                                                   for t in chosen) else ""))
    state_presets = discovery.AREA_PRESETS[scan_state]
    presets = c2.multiselect(f"Areas in {discovery.STATES[scan_state]}", list(state_presets), key=f"areas-{scan_state}",
                             default=[a for a in prev.get("area_presets", []) if a in state_presets],
                             format_func=lambda a: a.strip(),
                             help="Regions cover every town in them (by ZIP code or county). Indented entries are "
                                  "submarkets. Web-search scans search the region's main cities.")
    custom_areas = c2.text_input("Other cities or ZIPs (optional)", key=f"custom-areas-{scan_state}",
                                 value=prev.get("custom_areas", "") if prev.get("state", "WA") == scan_state else "",
                                 placeholder="e.g. Spokane, Camas, 98604, 993" if scan_state == "WA" else
                                 "City names or ZIPs, comma-separated",
                                 help="City names, 5-digit ZIPs, or 3-digit ZIP prefixes, separated by commas.")
    critical = st.multiselect(
        "Critical fields (a record needs all of these to be in the Primary queue)",
        list(routing.CRITICAL_FIELDS), default=prev.get("critical", saved_critical),
        help="'Valid email' means an email that passed the email check (not just present).")
    with st.expander("Advanced"):
        a1, a2, a3 = st.columns(3)
        max_new = a1.number_input("Max new businesses per scan", 1, 3000, int(prev.get("max_new", 100)),
                                  help="Caps how many new records one scan adds. Each takes ~5-15 s to deep-dive.")
        workers = a2.number_input("Sites crawled at once", 1, 12, int(prev.get("workers", 6)))
        max_pages = a3.number_input("Pages per site", 1, 12, int(prev.get("max_pages", 6)))
        brave_key = st.text_input("Brave Search API key (optional)", type="password",
                                  help="Makes website lookups faster and more reliable for big scans. "
                                       "Free keys: brave.com/search/api")

    ready = bool((trades or custom_trades) and (presets or custom_areas.strip()) and critical)
    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("🔍 Estimate", disabled=not ready, width="stretch"):
        try:
            with st.spinner("Counting licensed businesses…"):
                counts = estimate(scan_state, tuple(trades), tuple(custom_trades), tuple(presets), custom_areas)
            total = sum(int(v.split()[0]) for v in counts.values() if v.split()[0].isdigit())
            web = any(not v.split()[0].isdigit() for v in counts.values())
            expect = max_new if web else min(total, max_new)
            st.info(" · ".join(f"**{k}**: {v}" for k, v in counts.items())
                    + (f"  \nAbout **{total}** active licenses (before merging duplicates and records you already "
                       f"have)." if total else "")
                    + (" Web search finds an unknown number in advance." if web else "")
                    + f"  \nThis scan adds up to **{expect}** new businesses; at ~10 s each with {workers} at a time, "
                      f"roughly **{max(1, round(expect * 10 / workers / 60))} min**"
                    + (" (plus a few minutes of web searching)." if web else "."))
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't reach the L&I registry: {exc}")
    if b2.button("▶ Start scan", type="primary", disabled=not ready, width="stretch"):
        params = pipeline.ScanParams(trades=trades, state=scan_state, custom_trades=custom_trades, area_presets=presets,
                                     custom_areas=custom_areas.strip(), critical=critical, max_new=int(max_new),
                                     workers=int(workers), max_pages=int(max_pages), brave_key=brave_key)
        try:
            pipeline.start(cfg, params, user)
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't start the scan: {exc}")
    if not ready:
        st.caption("Pick at least one trade, one area and one critical field.")

# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #

st.subheader("Recent scans")
try:
    runs = db.list_runs(cfg, 25)
except db.DbError as exc:
    st.error(str(exc))
    st.stop()
if runs.empty:
    st.caption("No scans yet.")
    st.stop()

show = runs.drop(columns=["Params"]).rename(columns={
    "Id": "Scan", "StartedAt": "Started (UTC)", "FinishedAt": "Finished (UTC)", "StartedBy": "By",
    "CriticalFields": "Critical fields", "NewRecords": "New", "Enriched": "Deep-dived",
    "PrimaryCount": "Primary", "SecondaryCount": "Secondary"})
st.dataframe(show, hide_index=True, width="stretch")

h1, h2 = st.columns([1, 3])
pick = h1.selectbox("Scan", runs["Id"].tolist(), format_func=lambda i: f"#{i}",
                    help="Pick a scan to resume it, read its log, or work with its results below.")
row = runs[runs["Id"] == pick].iloc[0]
with h2:
    if row["Status"] in ("Interrupted", "Cancelled", "Failed"):
        params = pipeline.ScanParams(**{k: v for k, v in json.loads(row["Params"] or "{}").items()
                                        if k in pipeline.ScanParams.__dataclass_fields__})
        st.write("")
        if st.button(f"↻ Resume scan #{pick}", help="Deep-dives the records this scan hadn't finished."):
            try:
                pipeline.resume(cfg, int(pick), params)
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
with st.expander(f"Log for scan #{pick}"):
    st.code(db.get_run_log(cfg, int(pick)) or "(empty)", language=None, height=360)


# --------------------------------------------------------------------------- #
# Results of the selected scan
# --------------------------------------------------------------------------- #

PANEL_COLUMNS = ["Queue", "MissingFields", "ScanOutcome", "VendorName", "VendorType", "Phone", "Email", "Website",
                 "ContactName", "City", "Address", "CompletionScore", "InsuranceOrBond", "ServiceArea", "Region",
                 "Onboarded", "LiabilityInsurance", "Notes", "EmailStatus", "LicenseNumber", "Id"]


def load_results(run_id: int) -> None:
    with st.spinner("Loading this scan's results…"):
        df, versions = db.load_vendors(cfg, scan_run_id=run_id)
        outcomes = db.scan_outcomes(cfg, run_id)
    df.insert(0, "ScanOutcome", df["Id"].map(outcomes))
    st.session_state.scan_results = {"run": run_id, "df": df, "versions": versions,
                                     "token": st.session_state.get("scan_results", {}).get("token", 0) + 1}


def notify(kind: str, message: str) -> None:
    st.session_state.scan_message = (kind, message)


st.divider()
st.subheader(f"Results of scan #{pick}")
results = st.session_state.get("scan_results")
if not results or results["run"] != pick:
    try:
        load_results(int(pick))
    except db.DbError as exc:
        st.error(f"Couldn't load results: {exc}")
        st.stop()
    results = st.session_state.scan_results
df_scan: pd.DataFrame = results["df"]

if msg := st.session_state.pop("scan_message", None):
    getattr(st, msg[0])(msg[1])

if df_scan.empty:
    st.caption("No records are linked to this scan. (Scans from before scan tracking was added only keep the "
               "records they were the last to touch; later scans of the same area take over the rest.)")
    st.page_link("viewer_page.py", label="Open the Database viewer for all records", icon="🗂️")
    st.stop()

n_new = int((df_scan["ScanOutcome"] == "New").sum())
n_primary = int((df_scan["Queue"] == routing.PRIMARY).sum())
m = st.columns(5)
m[0].metric("Businesses", len(df_scan))
m[1].metric("New", n_new)
m[2].metric("Already in database", len(df_scan) - n_new)
m[3].metric("Primary", n_primary)
m[4].metric("Secondary", len(df_scan) - n_primary)

r1, r2, r3 = st.columns([3, 1.4, 1], vertical_alignment="bottom")
views = {"All": None, f"Primary ({n_primary})": routing.PRIMARY,
         f"Secondary ({len(df_scan) - n_primary})": routing.SECONDARY}
queue_label = r1.segmented_control("Queue", list(views), default="All", key="scan-queue",
                                   label_visibility="collapsed")
select_all = r2.checkbox("Select all shown", key="scan-select-all")
if r3.button("🔄 Reload", key="scan-reload", width="stretch", help="Fetch the latest data (discards unsaved edits)."):
    load_results(int(pick))
    st.rerun()

queue_view = views.get(queue_label or "All")
view = (df_scan if queue_view is None else df_scan[df_scan["Queue"] == queue_view]).reset_index(drop=True)
view.insert(0, "Select", select_all)
config = ui.column_config(df_scan) | {"ScanOutcome": st.column_config.TextColumn(
    "This scan", width="small", help="New to the database, or already there (gaps filled)")}
edited = st.data_editor(
    view,
    key=f"scan-editor-{pick}-{results['token']}-{queue_view}-{select_all}",
    column_config=config,
    column_order=["Select", *PANEL_COLUMNS],
    disabled=[c for c in view.columns if c not in db.EDITABLE_COLUMNS and c != "Select"],
    hide_index=True,
    height=min(520, 38 + 35 * (len(view) + 1)),
)
st.caption("Click a cell to edit it; tick ✔ to rescan, check emails or move records between queues. Nothing is "
           "written until you click **Save changes**. Records found by a later scan move to that scan's results.")
ui.record_tools(cfg, view, edited, results["versions"], user, notify, lambda: load_results(int(pick)),
                key="scan", allow_delete=False)
with st.expander(f"📤 Export scan #{pick}'s results"):
    ui.export_buttons(df_scan, key="scan", name=f"scan{pick}")
st.page_link("viewer_page.py", label="Open the Database viewer for all records, filters and search", icon="🗂️")
