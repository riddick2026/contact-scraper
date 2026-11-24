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

## Saving to Azure SQL

Both modes have a **💾 Save to Azure SQL** button. Results go into one table, `dbo.Vendors`, with one row per vendor (spreadsheet mode) or per website (pasted URLs). Saving again **updates** the existing row instead of adding a duplicate:

- A newly scraped real value replaces the stored one; a gap marker (`Not found`, `Unknown`, `Site unreachable`…) never overwrites a real stored value.
- Once a record has been edited in the Database viewer, re-scrapes only fill its empty or gap fields; your hand edits are kept.
- A pasted URL updates the vendor row that already has that website, if exactly one does.
- A spreadsheet column named **State** fills the record's `State` (otherwise it's left blank to fill in the viewer). Other spreadsheet columns are kept as JSON in `ExtraData`. `FirstSeen` / `LastScraped` are tracked, and `Notes`, `LastEdited`, `LastEditedBy` are set by the viewer.

The app signs in with your Microsoft Entra ID account through the Azure CLI, so no database password is stored anywhere.

## Database viewer

Open **🗂️ Database viewer** in the app's sidebar to browse and update the saved records in a spreadsheet-style grid:

- **Search** across vendor, type, website, contact, email, phone, insurance and notes.
- **Show** quick filters: no email, valid phone number, no valid phone number, missing contact name or insurance, email problems, emails not checked yet, website needs checking, edited by hand.
- **Filter** by Added from (spreadsheet, URL list, manual), **Vendor type**, **Onboarded** and **Liability insurance**.
- **Sort by** any column, ascending or descending, on top of any filter. The default is last activity, newest first.
- **Edit** a cell by clicking it (press Enter to open it, Enter again to keep the change). Vendor, vendor type, state, onboarded, liability insurance, website, contact name, email, phone, insurance or bond, and notes are editable; dates and scrape details are read-only.
- **Select** records with the ✔ column (or **Select all shown**), then:
  - **🔄 Rescan selected** looks for missing contact name, email, phone, insurance and vendor type on each vendor's website, and searches for a website when there isn't one (using the same checks as spreadsheet mode). It only fills empty or "Not found" fields; it never changes existing values, including hand edits.
  - **✉️ Check emails** checks each address for a bad format, a typo in a common domain (gmial.com), placeholder or system addresses, throwaway inboxes, and domains that don't exist or can't receive mail. Results go in **Email check**. These free checks can't confirm that a specific mailbox exists at a working domain; only sending a message (or a paid verification service) can.
- **Add** a record in the empty last row. **Delete** by selecting rows by their left edge and pressing Delete; you confirm before anything is removed.
- Nothing is written until **Save changes**. Each change records who made it and when. If a record was re-scraped or edited elsewhere after you loaded it, your change to that record is skipped and reported instead of overwriting newer data.
- **Download this view** exports the filtered records to Excel.

### Vendor fields

| Field | Values | Notes |
|---|---|---|
| Vendor type | Roofers, Gutters, Plumbers | From a spreadsheet column named Trade, Type, Category or Vendor Type; otherwise from the vendor's name, then their website. |
| Onboarded | No, In process, Yes | New vendors start at No. |
| Liability insurance | Unknown, Yes, No, In process | New vendors start at Unknown. |

The database only accepts the listed values for Onboarded and Liability insurance. Clearing one in the viewer resets it to its default.

### Set up a computer

1. Install the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) and sign in (your tenant requires MFA, so name it):

   ```bash
   az login --tenant dc5bdcf9-e0ad-4ace-a592-43c245b439fe
   ```

2. Create `.streamlit/secrets.toml` (it's git-ignored, so it isn't synced):

   ```toml
   [azure_sql]
   server = "contact-scraper-4b54ea.database.windows.net"
   database = "ContactScraper"
   ```

3. Use **Test connection** in the sidebar.

### Allow a new network

Azure SQL only accepts connections from allowed IP addresses. On a new network (another office, home, a laptop elsewhere), the app says the firewall is blocking it. Allow your current IP with:

```bash
az sql server firewall-rule create -g rg-contact-scraper -s contact-scraper-4b54ea -n "client-$(curl -s https://api.ipify.org | tr . -)" --start-ip-address "$(curl -s https://api.ipify.org)" --end-ip-address "$(curl -s https://api.ipify.org)"
```

The app's error message shows the exact IP Azure saw. Use that one: with the **Azure VPN Client** connected, SQL traffic leaves through the VPN gateway's shared IPs (rules `azvpn-…` already allow the ones seen so far), not your network's public IP, so a lookup site like ipify would give the wrong answer.

### Cost

The database uses Azure SQL's free offer (100,000 vCore-seconds of compute and 32 GB of storage per month). It auto-pauses when idle and when the monthly free allowance is used up, so it doesn't bill. After a pause, the first save or connection takes up to a minute while it wakes.

## Hosted version

The whole app (scraper and Database viewer) also runs on Azure at **https://vendors.crosbyllc.com**, so you can use it from any device without installing anything. (The original address, https://contact-scraper-4b54ea.azurewebsites.net, still works.)

- **Custom domain:** `vendors.crosbyllc.com` is a CNAME to the web app, plus an `asuid.vendors` TXT record that proves ownership, both in crosbyllc.com's DNS. HTTPS uses a free App Service managed certificate that Azure renews automatically. Both addresses are allowed as sign-in redirect URLs in the "Contact Scraper" app registration.

- **Sign-in:** Microsoft sign-in is required. Anyone in the `Default Directory` tenant (members and guests) can open it; nobody gets in anonymously. Viewer edits are recorded under the signed-in person's name.
- **Database access:** the web app signs in to Azure SQL with its own managed identity (`contact-scraper-4b54ea`, read/write only), so no password is stored. Its outbound IPs are allowed in the SQL firewall (`webapp-…` rules).
- **Plan:** App Service **Basic B1** (Linux, West US 3), about $13/month, covered by the Visual Studio subscription's monthly Azure credit (which has a spending limit). Always On is enabled, so the site doesn't sleep. The free F1 tier was tried first but can't install the app's packages within its CPU limit.

- **Scraping from Azure:** some websites and search engines block data-centre IPs, so the hosted scraper may find fewer websites than running it on your own computer.

## Test environment

A separate copy of the app for trying changes before they reach production:

| | Production | Test |
|---|---|---|
| Site | https://vendors.crosbyllc.com | https://contact-scraper-4b54ea-test.azurewebsites.net |
| Web app | `contact-scraper-4b54ea` | `contact-scraper-4b54ea-test` (same B1 plan, no extra cost) |
| Database | `ContactScraper` | `ContactScraperTest` (free offer; started as a copy of production) |
| Git branch | `main` | `test` |
| Banner | none | orange **TEST ENVIRONMENT** banner |

Each web app's identity can only reach its own database, so nothing done in test can change production data. Sign-in and the people who can use it are the same for both. The app on a local computer uses the test database by default (see `.streamlit/secrets.toml`; it shows the TEST banner).

### Workflow

1. Work on the `test` branch: `git switch test`, make and commit changes, `git push`.
2. Deploy to test and try it: `./deploy.sh test`.
3. When it's right, merge into production: `git switch main && git merge test && git push`.
4. Deploy production: `./deploy.sh production` (it refuses unless you're on an up-to-date `main`).

If a change adds database columns, the first time it runs it adds them to that environment's database, so test gets them first.

### Refresh test data from production

To start test over with a fresh copy of production data (this replaces everything in the test database):

```bash
.venv/bin/python refresh_test_db.py
```

### Deploy an update

`./deploy.sh test` or `./deploy.sh production` (needs `az login`). It deploys exactly what is committed, not unsaved edits.

Azure installs `requirements.txt` during the deploy. App settings (`AZURE_SQL_SERVER`, `AZURE_SQL_DATABASE`, the sign-in secret) live in the web app's configuration, not in the repo.

### Give someone access

Anyone in the directory can already sign in. To restrict it to named people, in the Azure portal open **Microsoft Entra ID → Enterprise applications → Contact Scraper → Properties**, set **Assignment required** to Yes, then add people under **Users and groups**.

## Optional: better name detection

Install spaCy and its English model; the scraper uses it automatically when present.

```bash
.venv/bin/pip install spacy
.venv/bin/python -m spacy download en_core_web_sm
```

## Files

- `app.py`: entry point, page navigation and the TEST banner
- `deploy.sh`: deploys the committed code to the test or production web app
- `refresh_test_db.py`: replaces the test database's records with a copy of production's
- `scraper_page.py`: scraper UI (spreadsheet + URL modes, progress, results, exports, save to Azure SQL)
- `viewer_page.py`: Database viewer (browse, filter, sort, edit, add, delete, rescan, email check, export)
- `rescan.py`: rescans selected records and fills only their missing fields
- `checks.py`: phone number validity and email deliverability checks
- `enrich.py`: spreadsheet loading (CSV/Excel/Google Sheets), website search, row enrichment, colour-coded Excel export
- `db.py`: Azure SQL storage (connection, `dbo.Vendors` table, save/update logic, viewer reads and edits)
- `scraper.py`: scraping engine (fetching, link following, email/phone/name extraction)
