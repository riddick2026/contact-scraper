# Contact Scraper

A Streamlit app that scrapes websites for contact details: emails, phone numbers, contact names, and insurance or bond details.

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

- `app.py`: Streamlit UI (input, progress, results table, Excel export)
- `scraper.py`: scraping engine (fetching, link following, email/phone/name extraction)
