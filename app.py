"""Streamlit front-end for the contact-info scraper.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import io
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime

import pandas as pd
import streamlit as st

from scraper import ScrapeResult, scrape_url

COLUMNS = ["URL", "Status", "Emails", "Phone Numbers", "Contact Names", "Insurance or Bond", "Pages Scraped"]

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


def run_scrape(urls: list[str], workers: int, progress, status_box, table) -> list[dict]:
    """Scrape concurrently, refreshing the UI from the main thread as results land."""
    rows: list[dict | None] = [None] * len(urls)
    in_flight: set[str] = set()
    lock = threading.Lock()

    def task(url: str) -> ScrapeResult:
        with lock:
            in_flight.add(url)
        try:
            return scrape_url(url)
        except Exception as exc:  # noqa: BLE001 - belt and braces; scrape_url shouldn't raise
            return ScrapeResult(url=url, status=f"Error: {type(exc).__name__}")
        finally:
            with lock:
                in_flight.discard(url)

    done_count = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(task, url): i for i, url in enumerate(urls)}
        while pending:
            # Short timeout so the "currently processing" label stays live
            finished, _ = wait(pending, timeout=0.3, return_when=FIRST_COMPLETED)
            for fut in finished:
                rows[pending.pop(fut)] = fut.result().as_row()
                done_count += 1

            with lock:
                current = sorted(in_flight)
            label = ", ".join(current[:3]) + (f" (+{len(current) - 3} more)" if len(current) > 3 else "")
            progress.progress(done_count / len(urls), text=f"{done_count} / {len(urls)} complete")
            status_box.info(f"⏳ Processing: {label}" if current else "Finishing up…")

            if finished:
                df = pd.DataFrame([r for r in rows if r is not None], columns=COLUMNS)
                table.dataframe(style_status(df), width="stretch", hide_index=True)

    return [r for r in rows if r is not None]


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

if "results" not in st.session_state:
    st.session_state.results = []

st.title("🔎 Contact Info Scraper")
st.caption(
    "Paste website URLs (one per line). Each homepage is scanned for emails, phone numbers, "
    "contact names and insurance/bond/licence details; if contact or insurance info is missing, "
    "a Contact, Licensing or About page on the same domain is checked."
)

with st.sidebar:
    st.header("Settings")
    workers = st.slider("Concurrent requests", min_value=1, max_value=20, value=8,
                        help="How many sites to scrape at the same time.")
    st.markdown("**Timeout:** 5 seconds per request")
    if st.button("Clear results"):
        st.session_state.results = []
        st.rerun()

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
    t0 = time.perf_counter()
    progress = progress_slot.progress(0.0, text=f"0 / {len(urls)} complete")
    st.session_state.results = run_scrape(urls, workers, progress, status_slot, table_slot)
    elapsed = time.perf_counter() - t0
    failed = sum(not r["Status"].startswith("OK") for r in st.session_state.results)
    status_slot.success(
        f"✅ Done — {len(urls)} URLs in {elapsed:.1f}s"
        + (f" ({failed} failed)" if failed else "")
    )

results = st.session_state.results
if results:
    df = pd.DataFrame(results, columns=COLUMNS)
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
