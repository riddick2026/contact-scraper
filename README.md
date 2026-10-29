# Contact Scraper

A Streamlit app that scrapes websites for contact details: emails, phone numbers and contact names.

- Paste URLs (one per line) and click **Start Scraping**.
- Sites are scraped concurrently, with live progress and results.
- If a homepage has no email or phone, one Contact/About page on the same domain is checked.
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
