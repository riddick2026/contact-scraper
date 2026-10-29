# Contact Scraper

A Streamlit app that scrapes websites for contact details: emails, phone numbers, contact names, and insurance or bond details.

## Vendor spreadsheet mode

Upload a **CSV or Excel** file, or paste a **Google Sheet** link (shared as "Anyone with the link can view"). For each row the app:

1. Uses the vendor's website from the sheet, or — if it's blank — searches the web for the vendor name (plus any City/State columns) and picks the vendor's own site, skipping directories like Yelp, BBB, Facebook and Angi. The found URL is written into the Website column.
2. Scrapes that site for **Contact Name, Email, Phone and Insurance or Bond**.
3. Fills every gap with a clear label, and colour-codes it in the Excel download (a Legend tab explains each value):

| Value | Meaning |
|---|---|
| `Not found` | Site loaded, but that detail isn't on it |
| `Unknown` | Site doesn't mention insurance, bonding or a licence |
| `No website` | No site in the sheet and none found by search |
| `Site unreachable` | Site didn't load (down, blocked, timed out) |
| `Search failed` | Web search errored (rate limit) — re-run those rows |
| `Found by search (verify)` | Site found, but its domain doesn't clearly match the name — check it |

Values already in the sheet (e.g. an existing Email) are kept; only blanks are filled. Columns are auto-detected and can be changed before running. Results download as a colour-coded `.xlsx` or a `.csv` (import into Google Sheets with File → Import).

Website search tries several free engines in turn (DuckDuckGo, then Bing) and records the reason in **Scrape Status** if all of them fail. Use **Test website search** in the sidebar to check search works on your network. For long lists, add a free **Brave Search API key** in the sidebar for faster, more reliable lookups.

You can re-upload a results file: cells holding `Search failed`, `Not found`, etc. are treated as blank and re-processed.

## Paste URLs mode

- Paste URLs (one per line) and click **Start Scraping**.
- Sites are scraped concurrently, with live progress and results.
- If a homepage has no email or phone, one Contact/About page on the same domain is checked.
- **Insurance or Bond** lists what the site states about the provider's coverage and registration: "Licensed, Bonded & Insured" style claims, licence/registration/bond numbers (e.g. `License #TACLA12345C`, `USDOT 1234567`), coverage types (general liability, workers' compensation, surety bond) and coverage amounts. If none is found on the pages already scraped, one Licensing/Insurance/About page is checked; otherwise the field shows `Unknown`. Mentions of *customers'* insurance (claims, adjusters) are ignored.
- Each request times out after 5 seconds; failed sites show `Connection Error` instead of crashing the app.
- Export the results with **Download to Excel** (`.xlsx`).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Windows, use `.venv\Scripts\pip` and `.venv\Scripts\streamlit` instead.

## Run

On Windows, just double-click `run.bat`. Otherwise:

```bash
.venv/bin/streamlit run app.py
```

Then open http://localhost:8501.

## Optional: better name detection

Install spaCy and its English model; the scraper uses it automatically when present.

```bash
.venv/bin/pip install spacy
.venv/bin/python -m spacy download en_core_web_sm
```

## Files

- `app.py`: Streamlit UI (spreadsheet + URL modes, progress, results, exports)
- `enrich.py`: spreadsheet loading (CSV/Excel/Google Sheets), website search, row enrichment, colour-coded Excel export
- `scraper.py`: scraping engine (fetching, link following, email/phone/name extraction)
