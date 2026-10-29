"""Streamlit front-end for the contact-info scraper.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import io
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Callable

import pandas as pd
import streamlit as st

import enrich
from scraper import INSURANCE_UNKNOWN, ScrapeResult, scrape_url

URL_COLUMNS = ["URL", "Status", "Emails", "Phone Numbers", "Contact Names", "Insurance or Bond", "Pages Scraped"]

st.set_page_config(page_title="Contact Scraper", page_icon="🔎", layout="wide")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def parse_urls(raw: str) -> list[str]:
    """One URL per line; drop blanks, comments and duplicates while keeping order."""
    seen, urls = set(), []
    for line in raw.splitlines():
        url = line.strip()
        if url and not url.startswith("#") and url.lower() not in seen:
            seen.add(url.lower())
            urls.append(url)
    return urls


def to_excel(df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Contacts")
        sheet = writer.sheets["Contacts"]
        for idx, col in enumerate(df.columns, start=1):
            width = max([len(col), *df[col].astype(str).str.len().tolist()] or [len(col)])
            sheet.column_dimensions[sheet.cell(row=1, column=idx).column_letter].width = min(width + 2, 60)
        sheet.freeze_panes = "A2"
    return buffer.getvalue()


def style_status(df: pd.DataFrame):
    def color(val: str) -> str:
        if val.startswith("OK (no"):
            return "color: #b7791f"
        if val.startswith("OK"):
            return "color: #2f855a"
        return "color: #c53030; font-weight: 600"

    return df.style.map(color, subset=["Status"])


def style_enriched(df: pd.DataFrame):
    def color(val) -> str:
        v = str(val)
        if v in enrich.BAD_VALUES:
            return "background-color: #f8cbad; color: #000"
        if v in enrich.WARN_VALUES or v.startswith("Partial"):
            return "background-color: #fff2cc; color: #000"
        if v in (enrich.SRC_FOUND, "Complete"):
            return "background-color: #e2efda; color: #000"
        return ""

    return df.style.map(color)


def run_concurrent(labels: list[str], task: Callable[[int], dict], workers: int,
                   progress, status_box, render: Callable[[list[dict]], None]) -> list[dict]:
    """Run task(i) for every item concurrently, refreshing the UI from the main thread."""
    rows: list[dict | None] = [None] * len(labels)
    in_flight: set[str] = set()
    lock = threading.Lock()

    def wrapped(i: int) -> dict:
        with lock:
            in_flight.add(labels[i])
        try:
            return task(i)
        finally:
            with lock:
                in_flight.discard(labels[i])

    done_count = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(wrapped, i): i for i in range(len(labels))}
        while pending:
            finished, _ = wait(pending, timeout=0.3, return_when=FIRST_COMPLETED)
            for fut in finished:
                rows[pending.pop(fut)] = fut.result()
                done_count += 1

            with lock:
                current = sorted(in_flight)
            label = ", ".join(current[:3]) + (f" (+{len(current) - 3} more)" if len(current) > 3 else "")
            progress.progress(done_count / len(labels), text=f"{done_count} / {len(labels)} complete")
            status_box.info(f"⏳ Processing: {label}" if current else "Finishing up…")
            if finished:
                render([r for r in rows if r is not None])

    return [r for r in rows if r is not None]


@st.cache_data(show_spinner=False)
def cached_load_file(name: str, data: bytes) -> dict[str, pd.DataFrame]:
    return enrich.load_file(name, data)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

for key, default in {"results": [], "enriched": None, "gsheet": None, "gsheet_url": ""}.items():
    st.session_state.setdefault(key, default)

st.title("🔎 Contact Info Scraper")

with st.sidebar:
    st.header("Settings")
    workers = st.slider("Concurrent requests", min_value=1, max_value=20, value=8,
                        help="How many sites to scrape at the same time.")
    st.markdown("**Timeout:** 5 seconds per request")
    brave_key = st.text_input(
        "Brave Search API key (optional)", type="password",
        help="Used to look up vendor websites. Without a key the app uses DuckDuckGo, which is free "
             "but may slow down or block very long lists. Free keys: brave.com/search/api",
    )
    with st.expander("Test website search"):
        test_q = st.text_input("Search for", value="Gold Shield Roofing Vancouver WA")
        if st.button("Run test search"):
            tester = enrich.WebSearch(brave_key)
            try:
                with st.spinner("Searching…"):
                    hits = tester.search(test_q)
                st.success(f"Search works ({tester.last_engine}) — {len(hits)} results")
                for h in hits[:5]:
                    st.caption(h["url"])
            except enrich.SearchError as exc:
                st.error(f"Search failed: {exc}")
    if st.button("Clear results"):
        st.session_state.results = []
        st.session_state.enriched = None
        st.rerun()

tab_sheet, tab_urls = st.tabs(["📄 Vendor spreadsheet", "🔗 Paste URLs"])

# --------------------------- Spreadsheet mode ------------------------------ #
with tab_sheet:
    st.caption(
        "Upload a vendor list (CSV or Excel) or paste a Google Sheet link. For each vendor the app uses "
        "the website in the sheet, or searches for one if it's blank, then scrapes the site for contact "
        "name, email, phone and insurance/bond details. Anything it can't find is clearly labelled."
    )
    source = st.radio("Source", ["Upload a file", "Google Sheet link"], horizontal=True,
                      label_visibility="collapsed")

    sheets: dict[str, pd.DataFrame] | None = None
    base_name = "vendors"
    if source == "Upload a file":
        upload = st.file_uploader("CSV or Excel file", type=["csv", "xlsx", "xlsm", "xls"])
        if upload is not None:
            base_name = upload.name.rsplit(".", 1)[0]
            try:
                sheets = cached_load_file(upload.name, upload.getvalue())
            except enrich.LoadError as exc:
                st.error(str(exc))
    else:
        c1, c2 = st.columns([4, 1])
        gs_url = c1.text_input("Google Sheet link", placeholder="https://docs.google.com/spreadsheets/d/…",
                               label_visibility="collapsed")
        if c2.button("Load sheet", width="stretch", disabled=not gs_url.strip()):
            try:
                with st.spinner("Loading sheet…"):
                    st.session_state.gsheet = enrich.load_gsheet(gs_url.strip())
                    st.session_state.gsheet_url = gs_url.strip()
            except enrich.LoadError as exc:
                st.session_state.gsheet = None
                st.error(str(exc))
        if st.session_state.gsheet is not None and st.session_state.gsheet_url == gs_url.strip():
            sheets = st.session_state.gsheet
            base_name = "google_sheet"
        st.caption("The sheet must be shared as \"Anyone with the link can view\". Results come back as a "
                   "download you can open in Excel or import into Google Sheets (File → Import).")

    if sheets:
        sheet_name = next(iter(sheets))
        if len(sheets) > 1:
            sheet_name = st.selectbox("Worksheet", list(sheets))
        df_in = sheets[sheet_name]
        if df_in.empty:
            st.warning("That sheet has no rows.")
        else:
            detected = enrich.detect_columns(df_in)
            cols = list(df_in.columns)
            none = "(none)"
            c1, c2, c3 = st.columns(3)
            vendor_col = c1.selectbox("Vendor name column", cols,
                                      index=cols.index(detected["vendor"]) if detected["vendor"] else 0)
            web_opts = [none] + [c for c in cols if c != vendor_col]
            website_sel = c2.selectbox(
                "Website column", web_opts,
                index=web_opts.index(detected["website"]) if detected["website"] in web_opts else 0,
                help="Blank cells get filled with the website the app finds. Pick (none) to add a new column.")
            loc_opts = [c for c in cols if c not in (vendor_col, website_sel)]
            location_cols = c3.multiselect(
                "Location columns (improve search)", loc_opts,
                default=[c for c in detected["location"] if c in loc_opts],
                help="City/state help pick the right business when names are common.")

            with st.expander(f"Preview — {len(df_in)} rows", expanded=False):
                st.dataframe(df_in.head(20), width="stretch", hide_index=True)

            extra_terms = st.text_input(
                "Extra search words (optional)", placeholder="e.g. WA",
                help="Added to every website search, e.g. the state, to avoid same-named businesses elsewhere.")
            cfg = enrich.EnrichConfig(vendor_col=vendor_col,
                                      website_col=None if website_sel == none else website_sel,
                                      location_cols=location_cols, extra_search_terms=extra_terms.strip())
            _map = enrich.output_columns(df_in, cfg)
            if _map[enrich.OUT_PHONE] in df_in.columns:
                cfg.phone_col = _map[enrich.OUT_PHONE]  # sheet phone confirms search matches
            to_search = sum(1 for _, r in df_in.iterrows()
                            if not (cfg.website_col and str(r[cfg.website_col]).strip()
                                    and str(r[cfg.website_col]).strip() not in enrich.PLACEHOLDERS))
            st.markdown(f"**{len(df_in)}** vendors · **{to_search}** need a website lookup")

            run_key = (base_name, sheet_name, tuple(cols), len(df_in), vendor_col, website_sel, extra_terms)
            go = st.button("Find websites & scrape", type="primary", key="go_sheet")
            sheet_progress, sheet_status, sheet_table = st.empty(), st.empty(), st.empty()

            if go:
                mapping = enrich.output_columns(df_in, cfg)
                columns = enrich.ordered_columns(df_in, mapping)
                search = enrich.WebSearch(brave_key)
                records = df_in.to_dict("records")

                def task(i: int) -> dict:
                    try:
                        out = enrich.enrich_row(records[i], cfg, search)
                    except Exception as exc:  # noqa: BLE001 - one bad row shouldn't stop the run
                        out = {f: enrich.SITE_UNREACHABLE for f in enrich.DATA_FIELDS}
                        out.update({enrich.OUT_WEBSITE: str(records[i].get(cfg.website_col or "", "")),
                                    enrich.OUT_SOURCE: "", enrich.OUT_STATUS: f"Error: {type(exc).__name__}"})
                    return enrich.merge_row(records[i], out, mapping)

                def render(rows: list[dict]) -> None:
                    sheet_table.dataframe(style_enriched(pd.DataFrame(rows, columns=columns)),
                                          width="stretch", hide_index=True)

                t0 = time.perf_counter()
                labels = [str(r.get(vendor_col, "")) or f"row {i + 2}" for i, r in enumerate(records)]
                progress = sheet_progress.progress(0.0, text=f"0 / {len(records)} complete")
                rows = run_concurrent(labels, task, workers, progress, sheet_status, render)
                enrich.flag_shared_sites(rows, mapping, vendor_col)
                st.session_state.enriched = {
                    "df": pd.DataFrame(rows, columns=columns), "mapping": mapping, "key": run_key,
                    "name": base_name, "sheet": sheet_name,
                }
                sheet_status.success(f"✅ Done — {len(rows)} vendors in {time.perf_counter() - t0:.1f}s")

            enriched = st.session_state.enriched
            if enriched is not None and enriched.get("key") != run_key:
                enriched = None  # results belong to a different sheet/selection
            if enriched is not None:
                df_out, mapping = enriched["df"], enriched["mapping"]
                sheet_table.dataframe(style_enriched(df_out), width="stretch", hide_index=True)

                def count_real(field: str) -> int:
                    col = df_out[mapping[field]].astype(str)
                    blanks = enrich.BAD_VALUES | {enrich.NOT_FOUND, INSURANCE_UNKNOWN, ""}
                    return int((~col.isin(blanks)).sum())

                m = st.columns(6)
                m[0].metric("Vendors", len(df_out))
                m[1].metric("Websites found", int(df_out[mapping[enrich.OUT_SOURCE]].isin(
                    [enrich.SRC_FOUND, enrich.SRC_LOW]).sum()))
                m[2].metric("With contact name", count_real(enrich.OUT_NAME))
                m[3].metric("With email", count_real(enrich.OUT_EMAIL))
                m[4].metric("With phone", count_real(enrich.OUT_PHONE))
                m[5].metric("With insurance/bond", count_real(enrich.OUT_INSURANCE))

                stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
                d1, d2, _ = st.columns([1.5, 1, 2.5])
                d1.download_button(
                    "📥 Download Excel (color-coded)",
                    data=enrich.to_highlighted_excel(df_out, enriched["sheet"]),
                    file_name=f"{enriched['name']}_enriched_{stamp}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary", width="stretch",
                )
                d2.download_button(
                    "📥 Download CSV", data=df_out.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"{enriched['name']}_enriched_{stamp}.csv", mime="text/csv", width="stretch",
                )
                st.caption("🟥 No website / unreachable / search failed · 🟨 Not found, Unknown or verify · "
                           "🟩 Website found by search. The Excel file includes a Legend tab.")
            elif not go:
                sheet_table.info("Results will appear here as each vendor finishes.")

# ------------------------------ URL mode ----------------------------------- #
with tab_urls:
    st.caption(
        "Paste website URLs (one per line). Each homepage is scanned for emails, phone numbers, "
        "contact names and insurance/bond/licence details; if contact or insurance info is missing, "
        "a Contact, Licensing or About page on the same domain is checked."
    )
    raw_urls = st.text_area(
        "URLs",
        height=240,
        placeholder="https://example.com\nanother-site.org\nhttps://www.company.co.uk/",
    )
    urls = parse_urls(raw_urls)

    col_btn, col_info = st.columns([1, 3])
    start = col_btn.button("Start Scraping", type="primary", disabled=not urls, width="stretch")
    col_info.markdown(f"**{len(urls)}** unique URL{'s' if len(urls) != 1 else ''} ready")

    progress_slot = st.empty()
    status_slot = st.empty()
    st.subheader("Results")
    table_slot = st.empty()

    if start:
        def url_task(i: int) -> dict:
            try:
                return scrape_url(urls[i]).as_row()
            except Exception as exc:  # noqa: BLE001 - scrape_url shouldn't raise
                return ScrapeResult(url=urls[i], status=f"Error: {type(exc).__name__}").as_row()

        def url_render(rows: list[dict]) -> None:
            table_slot.dataframe(style_status(pd.DataFrame(rows, columns=URL_COLUMNS)),
                                 width="stretch", hide_index=True)

        t0 = time.perf_counter()
        progress = progress_slot.progress(0.0, text=f"0 / {len(urls)} complete")
        st.session_state.results = run_concurrent(urls, url_task, workers, progress, status_slot, url_render)
        elapsed = time.perf_counter() - t0
        failed = sum(not r["Status"].startswith("OK") for r in st.session_state.results)
        status_slot.success(
            f"✅ Done — {len(urls)} URLs in {elapsed:.1f}s"
            + (f" ({failed} failed)" if failed else "")
        )

    results = st.session_state.results
    if results:
        df = pd.DataFrame(results, columns=URL_COLUMNS)
        table_slot.dataframe(style_status(df), width="stretch", hide_index=True)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Sites", len(df))
        m2.metric("With emails", int((df["Emails"] != "").sum()))
        m3.metric("With phones", int((df["Phone Numbers"] != "").sum()))
        m4.metric("With insurance/bond", int((df["Insurance or Bond"] != "Unknown").sum()))
        m5.metric("Errors", int((~df["Status"].str.startswith("OK")).sum()))

        st.download_button(
            "📥 Download to Excel",
            data=to_excel(df),
            file_name=f"contacts_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
    elif not start:
        table_slot.info("Results will appear here as each site finishes.")
