# Contact Scraper

An end-to-end vendor discovery tool: pick trades and areas, and it finds licensed businesses, crawls their websites for contact details, checks their emails, and sorts them into an actionable **Primary** queue and a **Secondary** queue for records missing something critical. No spreadsheets to upload or export in between.

## Scan (🛰️ the home page)

1. **State:** Washington, Oregon, Idaho, Texas or Oklahoma. The areas and data sources change with it.
2. **Trades:** Roofing, Gutters, Plumbing, HVAC, Electrical, Siding, Painting, Landscaping, plus any other trade typed in. Under the trades, the page shows which source each one will use in the chosen state.
3. **Areas:** that state's region presets (with submarkets) and/or custom cities, ZIPs or 3-digit ZIP prefixes. Registry regions are ZIP- or county-based, so every town in them is included; web-search regions search the region's main cities.
4. **Critical fields:** which fields a record needs to be in the Primary queue (default: Business name, Website, Phone, Valid email). The choice is saved and used everywhere routing happens.
5. **Estimate** shows how many active licenses match (or that web search is used) and roughly how long a scan takes; **Start scan** runs it.

### Where businesses come from (Stage 1)

Each state uses its official, free contractor-license data where it exists, and web-search discovery where it doesn't:

| State | Source | Trades | Comes with |
|---|---|---|---|
| Washington | [L&I contractor registry](https://data.wa.gov/Labor/L-I-Contractor-License-Data-General/m8qx-ubtq) (daily) | all (license specialty/type, or the trade in the business name) | phone, address, owner, license #, UBI |
| Oregon | [CCB active licenses](https://data.oregon.gov/Business/CCB-Active-Licenses/g77e-6bhs) (daily) + [Building Codes Division licenses](https://data.oregon.gov/Business/Building-Codes-Division-Active-Contractor-Individual-L/vhbr-cuaq) | all (trade in the business name); BCD adds plumbing, electrical and HVAC contractors | phone, address, owner, license #, **liability insurance and bond** |
| Texas | [TDLR licenses](https://data.texas.gov/dataset/TDLR-All-Licenses/7358-krk7) | HVAC (A/C contractors), Electrical | owner, county; electrical also phone and address |
| Texas | [TSBPE Responsible Master Plumbers](https://tsbpe.texas.gov/free-licensee-list/) (daily CSV) | Plumbing | phone, address, owner, **liability insurance** |
| Texas | web search | everything else (Texas doesn't license roofers or gutter installers) | website, phone |
| Idaho | web search | all (DOPL's list is behind an interactive form, not a data feed) | website, phone |
| Oklahoma | web search | all (CIB offers lookups only, no bulk list) | website, phone |

**Web-search discovery** searches several phrasings of "[trade] [city], [state]" (e.g. roofing contractor, roofing company, roof repair, roofer, roof replacement) for every sizable town in the chosen regions (34 for Dallas-Fort Worth) (excluding Yelp, Angi, BBB and similar directories), then opens each result's homepage and keeps it only if it's about the trade, names a city in the area (or the state with a ZIP), and its phone numbers are local. Lead-generation networks (several sites sharing one phone number) are collapsed to one. It finds businesses that have a website, which tend to land in Primary, but fewer than a registry (roughly 100-130 roofers for Dallas-Fort Worth when the search engines aren't rate-limiting). Free search engines slow down after many searches; the scan then pauses and resumes rather than skipping cities, so big regions can take 10+ minutes. A free **Brave Search API key** (Advanced) avoids this. Results vary a little between runs: re-running a web-search scan finds more without creating duplicates.

The scan then runs on its own, with a live log and progress counters:

- **Stage 1: discovery.** Pulls businesses from the state's sources (above). Duplicates across trades and areas are merged by state business ID (UBI), and businesses already in the database (same UBI, name or phone) are merged into the existing record (only empty fields are filled). New records are saved immediately as *Pending*. **Max new businesses per scan** (Advanced) caps how many new ones one scan adds.
- **Stage 2: deep dive.** For every record from Stage 1: if there's no website, a web search finds one, and it's only accepted if the site carries the registry's phone number or mentions the vendor's name, trade and area (directories, lookalikes and redirecting domains are rejected). Then up to 6 pages per site are crawled (home, contact, about, team, service areas, licensing, services, including footer links) for emails (third-party, tracking and placeholder addresses filtered out), phone numbers, contact names, street address, service area and insurance/licence claims. Found emails are checked, and the record is routed. Only empty fields are ever filled.
- **Routing:** records with every critical field go to **Primary**; the rest go to **Secondary** with the missing fields listed (`[NO DOMAIN FOUND]`, `[MISSING: EMAIL]`, `[INVALID EMAIL]`, ...). Secondary records are never deleted automatically.

When a scan finishes, **its results appear at the bottom of the Scan page**: every business that scan found (marked *New* or *Already in database*), with Primary/Secondary counts and a queue switcher, editable in place, with the same Save, Rescan, Check emails, Promote/Demote/Auto-route and export tools as the Database viewer. Pick an earlier scan under Recent scans to work with its results instead; a business found again by a later scan appears in both scans' results.

Each record is saved as soon as its deep dive finishes, so timeouts, blocked sites and broken pages never lose progress. Scans run in the background: closing the browser tab doesn't stop them. **Cancel** stops after the records in progress; a scan interrupted by an app restart or cancelled can be **resumed** from Recent scans, which also keeps each scan's settings, counts and full log.

Website search uses free engines (DuckDuckGo, then Bing). For big scans, add a free **Brave Search API key** under Advanced for faster, more reliable lookups. Some sites block automated visits; those are logged as unreachable rather than worked around.

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

## Storage (Azure SQL)

Everything lives in one Azure SQL database: `dbo.Vendors` (one row per business), `dbo.ScanRuns` (each scan's settings, counts, status and log) and `dbo.AppSettings` (the critical-field choice).

- Scans and rescans only fill empty or gap fields (`Not found`, `No website`, ...); they never overwrite a value that's already there, including hand edits in the viewer.
- Businesses are matched across scans by state business ID (UBI), name or phone, so re-scanning an area updates records instead of duplicating them.
- Records from the original spreadsheet imports keep their spreadsheet columns as JSON in `ExtraData`. `FirstSeen`, `LastScraped`, `EnrichedAt` are tracked, and `Notes`, `LastEdited`, `LastEditedBy` are set by the viewer.

The app signs in with your Microsoft Entra ID account through the Azure CLI, so no database password is stored anywhere.

## Database viewer

Open **🗂️ Database viewer** in the app's sidebar to browse and update the saved records in a spreadsheet-style grid:

- **Queue switcher:** All records, Primary, or Secondary. The **Missing** column shows which critical fields a record lacks; **Completeness** scores how much is known about it.
- **⬆️ Promote / ⬇️ Demote** move selected records between queues by hand; they stay there (marked *Moved by hand*) until **↺ Auto-route** hands them back to automatic routing. Editing a record (say, typing in an email you found) re-routes it automatically.
- **📤 Export queues:** Primary only, Secondary only, or All records with `Queue_Status` and `Missing_Fields` columns, as Excel or CSV. Exports cover the whole queue; **Download this view** exports just the filtered grid.

- **Search** across vendor, type, website, contact, email, phone, address, insurance and notes.
- **Show** quick filters: no email, valid phone number, no valid phone number, missing contact name or insurance, email problems, emails not checked yet, website needs checking, edited by hand.
- **Filter** by Added from (registry, web search, spreadsheet, manual), **State**, **Vendor type**, **Region**, **Onboarded** and **Liability insurance**. An empty filter means all.
- **Sort by** any column, ascending or descending, on top of any filter. The default is last activity, newest first.
- **Edit** a cell by clicking it (press Enter to open it, Enter again to keep the change). Vendor, vendor type, state, onboarded, liability insurance, website, contact name, email, phone, insurance or bond, and notes are editable; dates and scrape details are read-only.
- **Select** records with the ✔ column (or **Select all shown**), then:
  - **🔄 Rescan selected** runs the Stage 2 deep dive again on those records (finding a website if missing, crawling it, checking emails, re-routing). It only fills empty or "Not found" fields; it never changes existing values, including hand edits.
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
- `scan_page.py`: Scan page (scan settings, estimate, live progress, history, resume)
- `pipeline.py`: the background scan pipeline (Stage 1, Stage 2 deep dive, routing, resume)
- `discovery.py`: Stage 1 sources per state (WA, OR, TX registries; web-search discovery), trades and area presets
- `routing.py`: critical fields and Primary/Secondary routing
- `migrate.py`: brings a database's structure up to date (run by deploy.sh)
- `deploy.sh`: deploys the committed code to the test or production web app
- `refresh_test_db.py`: replaces the test database's records with a copy of production's
- `viewer_page.py`: Database viewer (queues, browse, filter, sort, edit, add, delete, rescan, email check, promote/demote, exports)
- `records_ui.py`: the editing, rescan, email-check, queue and export tools shared by the viewer and the Scan page's results panel
- `checks.py`: phone number validity and email deliverability checks
- `enrich.py`: website search and verification, vendor types, name cleaning
- `db.py`: Azure SQL storage (connection, `dbo.Vendors` table, save/update logic, viewer reads and edits)
- `scraper.py`: scraping engine (fetching, multi-page crawl, email/phone/name/address/service-area/insurance extraction)
