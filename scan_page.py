"""Scan page: discover vendors by trade and area, deep-dive them, and route them into queues."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import db
import discovery
import pipeline
import routing

st.title("🛰️ Scan for vendors")
st.caption("Finds licensed businesses for the trades and areas you pick (Washington L&I contractor registry), "
           "crawls each one's website for contact details, and sorts them into the **Primary** queue (all critical "
           "fields found) or the **Secondary** queue (something missing). Results are saved as they come in.")

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
def estimate(trades: tuple, customs: tuple, presets: tuple, custom_areas: str) -> dict:
    ts = [discovery.TRADES[t] for t in trades] + [discovery.custom_trade(c) for c in customs]
    return discovery.count_lni(ts, discovery.build_area_filter(list(presets), custom_areas))


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
    c1, c2 = st.columns(2)
    trades = c1.multiselect("Trades", list(discovery.TRADES),
                            default=[t for t in prev.get("trades", ["Roofing", "Gutters"]) if t in discovery.TRADES])
    custom_trades = [t.strip() for t in c1.text_input(
        "Other trades (optional)", value=", ".join(prev.get("custom_trades", [])),
        placeholder="e.g. Fencing, Window cleaning",
        help="Matched against business names and license specialties.").split(",") if t.strip()]
    presets = c2.multiselect("Areas", list(discovery.AREA_PRESETS),
                             default=[a for a in prev.get("area_presets", []) if a in discovery.AREA_PRESETS],
                             format_func=lambda a: a.strip(),
                             help="Regions are ZIP-code based, so every town in them is included. "
                                  "Indented entries are submarkets.")
    custom_areas = c2.text_input("Other cities or ZIPs (optional)", value=prev.get("custom_areas", ""),
                                 placeholder="e.g. Spokane, Camas, 98604, 993",
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
                counts = estimate(tuple(trades), tuple(custom_trades), tuple(presets), custom_areas)
            total = sum(counts.values())
            st.info(" · ".join(f"**{k}**: {v}" for k, v in counts.items())
                    + f"  \nAbout **{total}** active licenses (before merging duplicates and records you already "
                      f"have). This scan would add up to **{min(total, max_new)}** new businesses; at ~10 s each "
                      f"with {workers} at a time, roughly **{max(1, round(min(total, max_new) * 10 / workers / 60))} "
                      f"min**.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't reach the L&I registry: {exc}")
    if b2.button("▶ Start scan", type="primary", disabled=not ready, width="stretch"):
        params = pipeline.ScanParams(trades=trades, custom_trades=custom_trades, area_presets=presets,
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
pick = h1.selectbox("Scan", runs["Id"].tolist(), format_func=lambda i: f"#{i}")
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
