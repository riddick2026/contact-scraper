"""Azure SQL storage for scrape results.

One row per vendor (spreadsheet mode) or per website (URL mode). Saving again updates the
existing row; a re-scrape never replaces a real value with a gap marker like "Not found", and
rows edited in the viewer only have their empty/gap fields filled.

Connection settings come from environment variables or ``.streamlit/secrets.toml``:

    [azure_sql]
    server = "contact-scraper-xxxxxx.database.windows.net"
    database = "ContactScraper"
    # authentication = "ActiveDirectoryDefault"   # default: uses your `az login`
"""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

import enrich
from scraper import INSURANCE_UNKNOWN, normalize_url

SECRETS_FILE = Path(__file__).parent / ".streamlit" / "secrets.toml"

SOURCE_SHEET = "Spreadsheet"
SOURCE_URLS = "URL list"

# Gap markers the app writes; never allowed to overwrite a real stored value
GAP_VALUES = sorted(enrich.PLACEHOLDERS | {INSURANCE_UNKNOWN, enrich.SRC_NONE, enrich.SRC_NO_NAME})

# Columns written by a save, in staging/insert order
FIELDS = ["RecordKey", "VendorName", "VendorType", "State", "Website", "WebsiteDomain", "WebsiteSource", "ContactName", "Email",
          "Phone", "InsuranceOrBond", "ScrapeStatus", "PagesScraped", "Source", "SourceFile", "ExtraData"]
# Scraped values that follow the "real value beats gap marker" rule
DATA_COLUMNS = ["Website", "WebsiteSource", "ContactName", "Email", "Phone", "InsuranceOrBond", "PagesScraped"]

SCHEMA_SQL = """
IF OBJECT_ID(N'dbo.Vendors', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Vendors (
        Id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Vendors PRIMARY KEY,
        RecordKey       NVARCHAR(450)  NOT NULL CONSTRAINT UQ_Vendors_RecordKey UNIQUE,
        VendorName      NVARCHAR(400)  NULL,
        State           NVARCHAR(100)  NULL,
        Website         NVARCHAR(1000) NULL,
        WebsiteDomain   NVARCHAR(255)  NULL,
        WebsiteSource   NVARCHAR(100)  NULL,
        ContactName     NVARCHAR(1000) NULL,
        Email           NVARCHAR(1000) NULL,
        Phone           NVARCHAR(500)  NULL,
        InsuranceOrBond NVARCHAR(MAX)  NULL,
        ScrapeStatus    NVARCHAR(500)  NULL,
        PagesScraped    NVARCHAR(MAX)  NULL,
        Source          NVARCHAR(50)   NOT NULL,   -- 'Spreadsheet' or 'URL list'
        SourceFile      NVARCHAR(255)  NULL,
        ExtraData       NVARCHAR(MAX)  NULL,       -- other spreadsheet columns, as JSON
        Notes           NVARCHAR(MAX)  NULL,       -- free text, edited in the viewer
        FirstSeen       DATETIME2(0)   NOT NULL CONSTRAINT DF_Vendors_FirstSeen DEFAULT SYSUTCDATETIME(),
        LastScraped     DATETIME2(0)   NOT NULL CONSTRAINT DF_Vendors_LastScraped DEFAULT SYSUTCDATETIME(),
        LastEdited      DATETIME2(0)   NULL,       -- set by the viewer
        LastEditedBy    NVARCHAR(256)  NULL,
        RowVer          ROWVERSION     NOT NULL    -- lets the viewer detect conflicting edits
    );
    CREATE INDEX IX_Vendors_WebsiteDomain ON dbo.Vendors (WebsiteDomain);
END
-- Columns added after the table was first created
IF COL_LENGTH(N'dbo.Vendors', N'State') IS NULL
    ALTER TABLE dbo.Vendors ADD State NVARCHAR(100) NULL;
IF COL_LENGTH(N'dbo.Vendors', N'VendorType') IS NULL
    ALTER TABLE dbo.Vendors ADD VendorType NVARCHAR(50) NULL;
-- NOT NULL + DEFAULT fills existing rows with the default
IF COL_LENGTH(N'dbo.Vendors', N'Onboarded') IS NULL
    ALTER TABLE dbo.Vendors ADD Onboarded NVARCHAR(20) NOT NULL
        CONSTRAINT DF_Vendors_Onboarded DEFAULT N'No'
        CONSTRAINT CK_Vendors_Onboarded CHECK (Onboarded IN (N'Yes', N'No', N'In process'));
IF COL_LENGTH(N'dbo.Vendors', N'LiabilityInsurance') IS NULL
    ALTER TABLE dbo.Vendors ADD LiabilityInsurance NVARCHAR(20) NOT NULL
        CONSTRAINT DF_Vendors_LiabilityInsurance DEFAULT N'Unknown'
        CONSTRAINT CK_Vendors_LiabilityInsurance CHECK (LiabilityInsurance IN (N'Unknown', N'Yes', N'No', N'In process'));
IF COL_LENGTH(N'dbo.Vendors', N'EmailStatus') IS NULL
    ALTER TABLE dbo.Vendors ADD EmailStatus NVARCHAR(1000) NULL, EmailCheckedAt DATETIME2(0) NULL;
-- Discovery / deep-dive / queues
IF COL_LENGTH(N'dbo.Vendors', N'Queue') IS NULL
    ALTER TABLE dbo.Vendors ADD
        Queue NVARCHAR(20) NOT NULL CONSTRAINT DF_Vendors_Queue DEFAULT N'Secondary'
            CONSTRAINT CK_Vendors_Queue CHECK (Queue IN (N'Primary', N'Secondary')),
        QueueLocked BIT NOT NULL CONSTRAINT DF_Vendors_QueueLocked DEFAULT 0,  -- moved by hand
        MissingFields NVARCHAR(400) NULL,
        CompletionScore INT NULL,
        Address NVARCHAR(500) NULL,
        City NVARCHAR(100) NULL,
        Zip NVARCHAR(20) NULL,
        Region NVARCHAR(100) NULL,
        ServiceArea NVARCHAR(1000) NULL,
        UBI NVARCHAR(20) NULL,                 -- WA state business ID
        LicenseNumber NVARCHAR(50) NULL,       -- WA L&I contractor license
        EnrichmentStatus NVARCHAR(20) NULL,    -- Pending / Done / Failed
        EnrichedAt DATETIME2(0) NULL,
        ScanRunId INT NULL;
IF OBJECT_ID(N'dbo.ScanRuns', N'U') IS NULL
    CREATE TABLE dbo.ScanRuns (
        Id             INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ScanRuns PRIMARY KEY,
        StartedAt      DATETIME2(0)  NOT NULL CONSTRAINT DF_ScanRuns_StartedAt DEFAULT SYSUTCDATETIME(),
        FinishedAt     DATETIME2(0)  NULL,
        StartedBy      NVARCHAR(256) NULL,
        Trades         NVARCHAR(500) NULL,
        Areas          NVARCHAR(1000) NULL,
        CriticalFields NVARCHAR(500) NULL,
        Params         NVARCHAR(MAX) NULL,      -- full scan settings, JSON
        Status         NVARCHAR(20)  NOT NULL,  -- Running / Completed / Interrupted / Cancelled / Failed
        Discovered     INT NULL, NewRecords INT NULL, Enriched INT NULL,
        PrimaryCount   INT NULL, SecondaryCount INT NULL,
        Log            NVARCHAR(MAX) NULL
    );
IF OBJECT_ID(N'dbo.ScanRunRecords', N'U') IS NULL
    CREATE TABLE dbo.ScanRunRecords (          -- every business each scan found (records keep only the last scan)
        ScanRunId INT NOT NULL,
        VendorId  INT NOT NULL,
        Outcome   NVARCHAR(30) NOT NULL,       -- New / Already in database / Already in database (recent)
        CONSTRAINT PK_ScanRunRecords PRIMARY KEY (ScanRunId, VendorId)
    );
IF OBJECT_ID(N'dbo.AppSettings', N'U') IS NULL
    CREATE TABLE dbo.AppSettings (Name NVARCHAR(100) NOT NULL CONSTRAINT PK_AppSettings PRIMARY KEY,
                                  Value NVARCHAR(MAX) NULL);
"""

ONBOARDED_VALUES = ["No", "In process", "Yes"]
LIABILITY_VALUES = ["Unknown", "Yes", "No", "In process"]
# Columns that can't be blank: clearing one in the viewer resets it to its default
COLUMN_DEFAULTS = {"Onboarded": "No", "LiabilityInsurance": "Unknown"}


class DbError(Exception):
    """A user-facing database problem (not configured, firewall, sign-in...)."""


# --------------------------------------------------------------------------- #
# Configuration & connection
# --------------------------------------------------------------------------- #

@dataclass
class DbConfig:
    server: str
    database: str
    authentication: str = "ActiveDirectoryDefault"

    @property
    def label(self) -> str:
        return f"{self.server.removesuffix('.database.windows.net')} / {self.database}"


def load_config() -> DbConfig | None:
    """Read settings from AZURE_SQL_* env vars, else .streamlit/secrets.toml. None if unset."""
    section: dict = {}
    if SECRETS_FILE.exists():
        try:
            section = tomllib.loads(SECRETS_FILE.read_text()).get("azure_sql", {})
        except tomllib.TOMLDecodeError as exc:
            raise DbError(f"Can't read {SECRETS_FILE.name}: {exc}") from exc
    server = os.environ.get("AZURE_SQL_SERVER") or section.get("server", "")
    database = os.environ.get("AZURE_SQL_DATABASE") or section.get("database", "")
    auth = os.environ.get("AZURE_SQL_AUTH") or section.get("authentication", "ActiveDirectoryDefault")
    if not (server and database):
        return None
    if "." not in server:
        server += ".database.windows.net"
    return DbConfig(server=server, database=database, authentication=auth)


def app_environment() -> str:
    """'production' unless APP_ENVIRONMENT or secrets.toml's [app] environment says otherwise (e.g. 'test')."""
    env = os.environ.get("APP_ENVIRONMENT", "")
    if not env and SECRETS_FILE.exists():
        try:
            env = tomllib.loads(SECRETS_FILE.read_text()).get("app", {}).get("environment", "")
        except tomllib.TOMLDecodeError:
            env = ""
    return (env or "production").strip().lower()


def _friendly(exc: Exception) -> str:
    msg = str(exc)
    if "40615" in msg or re.search(r"Client with IP address '[\d.]+' is not allowed", msg):
        ip = re.search(r"IP address '([\d.]+)'", msg)
        return (f"Azure SQL's firewall is blocking this network{f' ({ip.group(1)})' if ip else ''}. "
                "Add your IP: see 'Allow a new network' in the README.")
    if "DefaultAzureCredential" in msg or "az login" in msg.lower() or "AADSTS" in msg:
        return "Couldn't sign in to Azure. Run `az login` in a terminal, then try again."
    if "Login failed" in msg or "18456" in msg:
        return "Signed in, but your account has no access to this database."
    return msg.splitlines()[0][:300] if msg else type(exc).__name__


def connect(cfg: DbConfig, wait_for_resume: float = 90):
    """Open a connection, waiting while a paused serverless database wakes up."""
    # The Rust provider has its own TLS; the default ODBC 18 provider needs OpenSSL in a
    # system location that Homebrew on Apple Silicon doesn't use
    os.environ.setdefault("MSSQL_PYTHON_NATIVE_PROVIDER", "mssql-odbc")
    import mssql_python  # imported lazily so the app still runs without the driver

    conn_str = (f"Server=tcp:{cfg.server},1433;Database={cfg.database};Encrypt=yes;"
                f"TrustServerCertificate=no;Authentication={cfg.authentication}")
    deadline = time.monotonic() + wait_for_resume
    while True:
        try:
            return mssql_python.connect(conn_str)
        except Exception as exc:  # noqa: BLE001 - driver raises several types
            # A database resuming from auto-pause refuses (40613) or times out until it's ready
            waking = any(s in str(exc) for s in ("40613", "not currently available", "Timeout"))
            if waking and time.monotonic() < deadline:
                time.sleep(5)
                continue
            raise DbError(_friendly(exc)) from exc


def ensure_schema(conn) -> None:
    cur = conn.cursor()
    cur.execute(SCHEMA_SQL)
    conn.commit()


def test_connection(cfg: DbConfig) -> str:
    """Connect, create the table if needed, and return a short status line."""
    conn = connect(cfg)
    try:
        ensure_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT SUSER_SNAME(), COUNT(*) FROM dbo.Vendors")
        user, count = cur.fetchone()
        return f"Connected as {user} — {count} saved record{'s' if count != 1 else ''}"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Turning results into records
# --------------------------------------------------------------------------- #

def _text(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s or None


def _domain(website: str | None) -> str | None:
    if not website or website in GAP_VALUES:
        return None
    url = normalize_url(website)
    host = (urlparse(url).hostname or "") if url else ""
    return host.lower().removeprefix("www.") or None


def _vendor_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _record(vendor, website, **fields) -> dict | None:
    vendor, website = _text(vendor), _text(website)
    domain = _domain(website)
    if vendor:
        key = "vendor:" + _vendor_key(vendor)
    elif domain:
        key = "site:" + domain
    elif website:
        key = "url:" + website.lower()
    else:
        return None
    rec = dict.fromkeys(FIELDS)
    rec.update(RecordKey=key[:450], VendorName=vendor, Website=website, WebsiteDomain=domain)
    rec.update({k: _text(v) for k, v in fields.items()})
    return rec


def _state_column(columns, taken: set) -> str | None:
    return next((c for c in columns if c not in taken
                 and re.sub(r"[^a-z]", "", str(c).lower()) in ("state", "st", "stateprovince", "province")), None)


def records_from_sheet(df: pd.DataFrame, mapping: dict[str, str], vendor_col: str, source_file: str) -> list[dict]:
    """Records from spreadsheet-mode results; unmapped sheet columns go into ExtraData."""
    used = {vendor_col, *mapping.values()}
    state_col = _state_column(df.columns, used)
    if state_col:
        used.add(state_col)
    extra_cols = [c for c in df.columns if c not in used]
    out = []
    for row in df.to_dict("records"):
        extra = {c: _text(row.get(c)) for c in extra_cols if _text(row.get(c))}
        rec = _record(
            row.get(vendor_col), row.get(mapping[enrich.OUT_WEBSITE]),
            WebsiteSource=row.get(mapping[enrich.OUT_SOURCE]),
            ContactName=row.get(mapping[enrich.OUT_NAME]),
            Email=row.get(mapping[enrich.OUT_EMAIL]),
            Phone=row.get(mapping[enrich.OUT_PHONE]),
            InsuranceOrBond=row.get(mapping[enrich.OUT_INSURANCE]),
            ScrapeStatus=row.get(mapping[enrich.OUT_STATUS]),
            State=row.get(state_col) if state_col else None,
            VendorType=enrich.vendor_type(row.get(mapping.get(enrich.OUT_TYPE, ""))) or enrich.vendor_type(
                row.get(vendor_col)),
            Source=SOURCE_SHEET, SourceFile=source_file,
            ExtraData=json.dumps(extra, ensure_ascii=False) if extra else None,
        )
        if rec:
            out.append(rec)
    return out


def records_from_urls(rows: list[dict]) -> list[dict]:
    """Records from URL-mode results (rows from ScrapeResult.as_row())."""
    out = []
    for row in rows:
        rec = _record(
            None, row.get("URL"),
            ContactName=row.get("Contact Names"), Email=row.get("Emails"), Phone=row.get("Phone Numbers"),
            InsuranceOrBond=row.get("Insurance or Bond"), ScrapeStatus=row.get("Status"),
            PagesScraped=row.get("Pages Scraped"), Source=SOURCE_URLS,
            VendorType=enrich.vendor_type(row.get("Vendor Type")),
        )
        if rec:
            out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #

def _sql_list(values: list[str]) -> str:
    return ", ".join("N'" + v.replace("'", "''") + "'" for v in values)


def _merge_sql() -> str:
    gaps = _sql_list(GAP_VALUES)

    def keep_real(col: str) -> str:
        # New real value wins, unless the row was edited in the viewer; a gap marker (or any value,
        # for viewer-edited rows) only fills an empty/gap cell
        return (f"t.{col} = CASE WHEN t.LastEdited IS NULL AND s.{col} IS NOT NULL "
                f"AND s.{col} NOT IN ({gaps}) THEN s.{col} "
                f"WHEN t.{col} IS NULL OR t.{col} IN ({gaps}) THEN COALESCE(s.{col}, t.{col}) "
                f"ELSE t.{col} END")

    updates = ",\n        ".join(
        [keep_real(c) for c in DATA_COLUMNS]
        + ["t.WebsiteDomain = CASE WHEN t.LastEdited IS NULL THEN COALESCE(s.WebsiteDomain, t.WebsiteDomain) "
           "ELSE COALESCE(t.WebsiteDomain, s.WebsiteDomain) END",
           "t.VendorName = CASE WHEN t.LastEdited IS NULL THEN COALESCE(s.VendorName, t.VendorName) "
           "ELSE COALESCE(t.VendorName, s.VendorName) END",
           "t.State = CASE WHEN t.LastEdited IS NULL THEN COALESCE(s.State, t.State) "
           "ELSE COALESCE(t.State, s.State) END",
           "t.VendorType = CASE WHEN t.LastEdited IS NULL THEN COALESCE(s.VendorType, t.VendorType) "
           "ELSE COALESCE(t.VendorType, s.VendorType) END",
           "t.ScrapeStatus = s.ScrapeStatus",
           "t.SourceFile = COALESCE(s.SourceFile, t.SourceFile)",
           "t.ExtraData = COALESCE(s.ExtraData, t.ExtraData)",
           "t.LastScraped = SYSUTCDATETIME()"]
    )
    cols = ", ".join(FIELDS)
    return f"""
MERGE dbo.Vendors WITH (HOLDLOCK) AS t
USING #stage AS s ON t.RecordKey = s.RecordKey
WHEN MATCHED THEN UPDATE SET
        {updates}
WHEN NOT MATCHED THEN
    INSERT ({cols}) VALUES ({", ".join("s." + c for c in FIELDS)})
OUTPUT $action;
"""


STAGE_SQL = """
CREATE TABLE #stage (
    RecordKey NVARCHAR(450) NOT NULL PRIMARY KEY, VendorName NVARCHAR(400), VendorType NVARCHAR(50),
    State NVARCHAR(100),
    Website NVARCHAR(1000),
    WebsiteDomain NVARCHAR(255), WebsiteSource NVARCHAR(100), ContactName NVARCHAR(1000),
    Email NVARCHAR(1000), Phone NVARCHAR(500), InsuranceOrBond NVARCHAR(MAX), ScrapeStatus NVARCHAR(500),
    PagesScraped NVARCHAR(MAX), Source NVARCHAR(50) NOT NULL, SourceFile NVARCHAR(255), ExtraData NVARCHAR(MAX)
);
"""

# A pasted URL updates the vendor record that already has that website, if exactly one does
LINK_URLS_TO_VENDORS_SQL = """
UPDATE s SET s.RecordKey = v.RecordKey
FROM #stage s
JOIN (SELECT WebsiteDomain, MIN(RecordKey) AS RecordKey FROM dbo.Vendors
      WHERE WebsiteDomain IS NOT NULL GROUP BY WebsiteDomain HAVING COUNT(*) = 1) v
  ON v.WebsiteDomain = s.WebsiteDomain
WHERE s.VendorName IS NULL
  AND NOT EXISTS (SELECT 1 FROM #stage s2 WHERE s2.RecordKey = v.RecordKey);
"""


def save_records(cfg: DbConfig, records: list[dict]) -> dict[str, int]:
    """Upsert records; returns {"inserted": n, "updated": n}."""
    by_key = {r["RecordKey"]: r for r in records}  # later duplicates win
    if not by_key:
        return {"inserted": 0, "updated": 0}
    conn = connect(cfg)
    try:
        ensure_schema(conn)
        cur = conn.cursor()
        cur.execute(STAGE_SQL)
        cur.executemany(
            f"INSERT INTO #stage ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
            [[r[f] for f in FIELDS] for r in by_key.values()],
        )
        cur.execute(LINK_URLS_TO_VENDORS_SQL)
        cur.execute(_merge_sql())
        actions = [row[0] for row in cur.fetchall()]
        conn.commit()
    except DbError:
        raise
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise DbError(_friendly(exc)) from exc
    finally:
        conn.close()
    return {"inserted": actions.count("INSERT"), "updated": actions.count("UPDATE")}


# --------------------------------------------------------------------------- #
# Viewer: load, edit, add, delete
# --------------------------------------------------------------------------- #

SOURCE_MANUAL = "Manual"

# Columns the viewer lets people change
EDITABLE_COLUMNS = ["VendorName", "VendorType", "State", "Onboarded", "LiabilityInsurance", "Website",
                    "ContactName", "Email", "Phone", "Address", "City", "Zip", "ServiceArea", "InsuranceOrBond",
                    "Notes"]
VIEW_COLUMNS = ["Id", "Queue", "MissingFields", *EDITABLE_COLUMNS, "CompletionScore", "Region", "EmailStatus",
                "EmailCheckedAt", "WebsiteSource", "ScrapeStatus", "LicenseNumber", "UBI", "EnrichmentStatus",
                "EnrichedAt", "Source", "SourceFile", "ExtraData", "FirstSeen", "LastScraped", "LastEdited",
                "LastEditedBy", "QueueLocked", "ScanRunId"]


@dataclass
class ChangeResult:
    updated: int = 0
    inserted: int = 0
    deleted: int = 0
    conflicts: list[str] | None = None  # rows changed by someone else since loading


def load_vendors(cfg: DbConfig, scan_run_id: int | None = None) -> tuple[pd.DataFrame, dict[int, bytes]]:
    """Saved records (all, or those from one scan), newest activity first, plus each row's version stamp by Id."""
    conn = connect(cfg)
    try:
        ensure_schema(conn)
        cur = conn.cursor()
        where = ("WHERE Id IN (SELECT VendorId FROM dbo.ScanRunRecords WHERE ScanRunId = ?) "
                 if scan_run_id is not None else "")
        cur.execute(f"SELECT {', '.join(VIEW_COLUMNS)}, RowVer FROM dbo.Vendors {where}"
                    "ORDER BY COALESCE(LastEdited, LastScraped) DESC, Id DESC",
                    [int(scan_run_id)] if scan_run_id is not None else [])
        rows = cur.fetchall()
    except DbError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DbError(_friendly(exc)) from exc
    finally:
        conn.close()
    df = pd.DataFrame([list(r)[:-1] for r in rows], columns=VIEW_COLUMNS)
    versions = {int(r[0]): bytes(r[-1]) for r in rows}
    return df, versions


def _label(row: dict) -> str:
    return str(row.get("VendorName") or row.get("Website") or f"record {row.get('Id')}")


def apply_changes(cfg: DbConfig, versions: dict[int, bytes], updates: dict[int, dict],
                  inserts: list[dict], deletes: list[int], names: dict[int, str] | None = None,
                  editor: str | None = None) -> ChangeResult:
    """Write viewer edits in one transaction.

    Updates and deletes only apply if the row hasn't changed since it was loaded (its
    RowVer still matches); otherwise they're skipped and reported as conflicts. `editor` is
    recorded as LastEditedBy; without it, the database login name is used.
    """
    result = ChangeResult(conflicts=[])
    names = names or {}
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        for row_id, changes in updates.items():
            sets = {c: _text(v) for c, v in changes.items() if c in EDITABLE_COLUMNS}
            sets.update({c: sets[c] or d for c, d in COLUMN_DEFAULTS.items() if c in sets})
            if not sets:
                continue
            if "Website" in sets:
                sets["WebsiteDomain"] = _domain(sets["Website"])
            assignments = ", ".join(f"{c} = ?" for c in sets)
            cur.execute(
                f"UPDATE dbo.Vendors SET {assignments}, LastEdited = SYSUTCDATETIME(), "
                "LastEditedBy = COALESCE(?, SUSER_SNAME()) WHERE Id = ? AND RowVer = ?",
                [*sets.values(), editor, row_id, versions[row_id]],
            )
            if cur.rowcount == 1:
                result.updated += 1
            else:
                result.conflicts.append(names.get(row_id) or f"record {row_id}")

        for row_id in deletes:
            cur.execute("DELETE FROM dbo.Vendors WHERE Id = ? AND RowVer = ?", [row_id, versions[row_id]])
            if cur.rowcount == 1:
                result.deleted += 1
            else:
                result.conflicts.append(f"{names.get(row_id) or f'record {row_id}'} (not deleted)")

        for row in inserts:
            fields = {c: row.get(c) for c in EDITABLE_COLUMNS
                      if c not in ("VendorName", "Website", "Notes", *COLUMN_DEFAULTS)}
            rec = _record(row.get("VendorName"), row.get("Website"), Source=SOURCE_MANUAL, **fields)
            if rec is None:
                continue  # blank row
            cur.execute("SELECT 1 FROM dbo.Vendors WHERE RecordKey = ?", [rec["RecordKey"]])
            if cur.fetchone():
                result.conflicts.append(f"{_label(rec)} (already exists, not added)")
                continue
            cols = [*FIELDS, "Notes", "LastEdited", "LastEditedBy"]
            cur.execute(
                f"INSERT INTO dbo.Vendors ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * (len(FIELDS) + 1))}, SYSUTCDATETIME(), COALESCE(?, SUSER_SNAME()))",
                [*(rec[f] for f in FIELDS), _text(row.get("Notes")), editor],
            )
            statuses = {c: _text(row.get(c)) for c in COLUMN_DEFAULTS if _text(row.get(c))}
            if statuses:
                cur.execute(f"UPDATE dbo.Vendors SET {', '.join(f'{c} = ?' for c in statuses)} WHERE RecordKey = ?",
                            [*statuses.values(), rec["RecordKey"]])
            result.inserted += 1
        conn.commit()
    except DbError:
        raise
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise DbError(_friendly(exc)) from exc
    finally:
        conn.close()
    # Edits can complete (or break) a record: re-route edited and new rows (hand-moved ones stay put)
    touched = list(updates)
    if inserts:
        conn = connect(cfg)
        try:
            cur = conn.cursor()
            cur.execute("SELECT Id FROM dbo.Vendors WHERE CompletionScore IS NULL")
            touched += [int(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()
    if touched:
        reroute(cfg, touched)
    return result


# --------------------------------------------------------------------------- #
# Rescans and email checks on selected records
# --------------------------------------------------------------------------- #

RESCAN_COLUMNS = ["Id", "VendorName", "VendorType", "State", "Website", "WebsiteDomain", "WebsiteSource",
                  "ContactName", "Email", "Phone", "InsuranceOrBond", "ScrapeStatus", "PagesScraped", "ExtraData",
                  "Address", "City", "Zip", "ServiceArea", "EmailStatus", "QueueLocked", "Queue"]


def load_records(cfg: DbConfig, ids: list[int]) -> list[dict]:
    """Full records for the given Ids, each with its RowVer, for rescanning."""
    if not ids:
        return []
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(RESCAN_COLUMNS)}, RowVer FROM dbo.Vendors "
                    f"WHERE Id IN ({', '.join('?' * len(ids))})", [int(i) for i in ids])
        return [{**dict(zip(RESCAN_COLUMNS, r[:-1])), "RowVer": bytes(r[-1])} for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        raise DbError(_friendly(exc)) from exc
    finally:
        conn.close()


def update_scanned(cfg: DbConfig, updates: list[tuple[int, bytes, dict]]) -> tuple[int, int]:
    """Write rescan results [(Id, RowVer, {column: value})]; returns (updated, skipped-as-changed)."""
    done = skipped = 0
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        for row_id, rowver, fields in updates:
            if not fields:
                continue
            cur.execute(f"UPDATE dbo.Vendors SET {', '.join(f'{c} = ?' for c in fields)}, "
                        "LastScraped = SYSUTCDATETIME() WHERE Id = ? AND RowVer = ?",
                        [*fields.values(), row_id, rowver])
            done, skipped = (done + 1, skipped) if cur.rowcount == 1 else (done, skipped + 1)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise DbError(_friendly(exc)) from exc
    finally:
        conn.close()
    return done, skipped


def save_email_statuses(cfg: DbConfig, statuses: dict[int, str | None]) -> int:
    """Store email-check results; they don't touch scraped data, so no version check is needed."""
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.executemany("UPDATE dbo.Vendors SET EmailStatus = ?, EmailCheckedAt = SYSUTCDATETIME() WHERE Id = ?",
                        [[s, int(i)] for i, s in statuses.items()])
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise DbError(_friendly(exc)) from exc
    finally:
        conn.close()
    return len(statuses)


# --------------------------------------------------------------------------- #
# App settings
# --------------------------------------------------------------------------- #

CRITICAL_SETTING = "critical_fields"


def get_setting(cfg: DbConfig, name: str, default=None):
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute("SELECT Value FROM dbo.AppSettings WHERE Name = ?", [name])
        row = cur.fetchone()
        return json.loads(row[0]) if row and row[0] else default
    finally:
        conn.close()


def set_setting(cfg: DbConfig, name: str, value) -> None:
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute("""MERGE dbo.AppSettings AS t USING (SELECT ? AS Name, ? AS Value) AS s ON t.Name = s.Name
                       WHEN MATCHED THEN UPDATE SET Value = s.Value
                       WHEN NOT MATCHED THEN INSERT (Name, Value) VALUES (s.Name, s.Value);""",
                    [name, json.dumps(value)])
        conn.commit()
    finally:
        conn.close()


def critical_fields(cfg: DbConfig) -> list[str]:
    import routing
    saved = get_setting(cfg, CRITICAL_SETTING)
    return [f for f in (saved or routing.DEFAULT_CRITICAL) if f in routing.CRITICAL_FIELDS]


# --------------------------------------------------------------------------- #
# Scan runs
# --------------------------------------------------------------------------- #

RUN_COLUMNS = ["Id", "StartedAt", "FinishedAt", "StartedBy", "Trades", "Areas", "CriticalFields", "Status",
               "Discovered", "NewRecords", "Enriched", "PrimaryCount", "SecondaryCount", "Params"]


def create_run(cfg: DbConfig, **fields) -> int:
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO dbo.ScanRuns ({', '.join(fields)}) OUTPUT INSERTED.Id "
                    f"VALUES ({', '.join('?' * len(fields))})", list(fields.values()))
        run_id = int(cur.fetchone()[0])
        conn.commit()
        return run_id
    finally:
        conn.close()


def update_run(cfg: DbConfig, run_id: int, log: str | None = None, **fields) -> None:
    """Set run columns and/or append text to its log."""
    sets = [f"{c} = ?" for c in fields] + (["Log = CONCAT(COALESCE(Log, N''), ?)"] if log else [])
    if not sets:
        return
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE dbo.ScanRuns SET {', '.join(sets)} WHERE Id = ?",
                    [*fields.values(), *([log] if log else []), run_id])
        conn.commit()
    finally:
        conn.close()


def list_runs(cfg: DbConfig, limit: int = 20) -> pd.DataFrame:
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT TOP {int(limit)} {', '.join(RUN_COLUMNS)} FROM dbo.ScanRuns ORDER BY Id DESC")
        return pd.DataFrame([list(r) for r in cur.fetchall()], columns=RUN_COLUMNS)
    finally:
        conn.close()


def get_run_log(cfg: DbConfig, run_id: int) -> str:
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute("SELECT Log FROM dbo.ScanRuns WHERE Id = ?", [run_id])
        row = cur.fetchone()
        return (row[0] or "") if row else ""
    finally:
        conn.close()


def mark_interrupted(cfg: DbConfig, keep_ids: set[int]) -> int:
    """Runs marked Running that no thread in this server is working on (e.g. after a restart)."""
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        ids = ", ".join(str(int(i)) for i in keep_ids) or "-1"
        cur.execute(f"UPDATE dbo.ScanRuns SET Status = N'Interrupted', "
                    f"Log = CONCAT(COALESCE(Log, N''), N'Interrupted (app restarted); resume to finish.' + CHAR(10)) "
                    f"WHERE Status = N'Running' AND Id NOT IN ({ids})")
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Discovery merge and deep-dive storage
# --------------------------------------------------------------------------- #

_SUFFIXES = re.compile(r"\b(llc|l l c|inc|incorporated|corp|corporation|co|company|ltd|pllc|lp|llp|dba)\b")


def _name_key(name: str | None) -> str:
    return re.sub(r"\s+", " ", _SUFFIXES.sub(" ", _vendor_key(name or ""))).strip()


def _phone_keys(value) -> set[str]:
    return {d[-10:] for d in (re.sub(r"\D", "", p) for p in re.split(r"[,;/]", str(value or ""))) if len(d) >= 10}


def discovery_state_name(abbr: str | None) -> str:
    import discovery
    return discovery.STATES.get(abbr or "WA", abbr or "Washington")


REDIVE_AFTER_DAYS = 30  # existing records deep-dived more recently than this aren't crawled again


def merge_discovered(cfg: DbConfig, run_id: int, items: list, limit: int | None = None) -> tuple[int, int, int]:
    """Add discovered businesses, merging with existing records (same UBI, name or phone).

    Existing records only get empty fields filled. New records, and existing ones not deep-dived in
    the last REDIVE_AFTER_DAYS days, are queued for the deep dive (EnrichmentStatus = 'Pending');
    all are tagged with the run. Returns (new, matched existing, matched but recently deep-dived).
    `limit` caps how many *new* records are added.
    """
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute("SELECT Id, VendorName, Phone, UBI, VendorType, ContactName, Address, City, Zip, Region, "
                    "State, WebsiteDomain, InsuranceOrBond, LicenseNumber, "
                    f"CASE WHEN EnrichedAt > DATEADD(day, -{REDIVE_AFTER_DAYS}, SYSUTCDATETIME()) "
                    "THEN 1 ELSE 0 END FROM dbo.Vendors")
        cols = ["Id", "VendorName", "Phone", "UBI", "VendorType", "ContactName", "Address", "City", "Zip",
                "Region", "State", "WebsiteDomain", "InsuranceOrBond", "LicenseNumber", "Fresh"]
        existing = [dict(zip(cols, r)) for r in cur.fetchall()]
        by_ubi = {e["UBI"]: e for e in existing if e["UBI"]}
        by_name = {_name_key(e["VendorName"]): e for e in existing if e["VendorName"]}
        by_phone = {p: e for e in existing for p in _phone_keys(e["Phone"])}
        by_domain = {e["WebsiteDomain"]: e for e in existing if e["WebsiteDomain"]}

        inserts, updates, seen = [], [], set()
        for it in items:
            state_name = discovery_state_name(it.state)
            domain = _domain(it.website) if it.website else None
            match = (by_ubi.get(it.ubi) if it.ubi else None) or (by_domain.get(domain) if domain else None) \
                or by_name.get(_name_key(it.name)) \
                or next((by_phone[p] for p in _phone_keys(it.phone) if p in by_phone), None)
            if match:
                if match["Id"] in seen:
                    continue
                seen.add(match["Id"])
                fill = {"UBI": it.ubi, "Region": it.region}
                for col, val in (("VendorType", it.vendor_type), ("ContactName", it.owner),
                                 ("Address", it.address), ("City", it.city), ("Zip", it.zip),
                                 ("State", state_name), ("LicenseNumber", it.license_number),
                                 ("InsuranceOrBond", it.insurance)):
                    if val and (match.get(col) is None or str(match.get(col)).strip() in GAP_VALUES + [""]):
                        fill[col] = val
                if it.phone and not _phone_keys(match["Phone"]):
                    fill["Phone"] = it.phone
                updates.append((match["Id"], {k: v for k, v in fill.items() if v}))
            else:
                if limit is not None and len(inserts) >= limit:
                    continue
                key = "vendor:" + _vendor_key(it.name)
                inserts.append({
                    "RecordKey": key[:450], "VendorName": it.name, "VendorType": it.vendor_type, "State": state_name,
                    "Phone": it.phone, "Address": it.address, "City": it.city, "Zip": it.zip, "Region": it.region,
                    "ContactName": it.owner, "UBI": it.ubi, "LicenseNumber": it.license_number, "Source": it.source,
                    "Website": it.website, "WebsiteDomain": domain,
                    "WebsiteSource": "Web search discovery" if it.website else None,
                    "InsuranceOrBond": it.insurance, "ScanRunId": run_id, "EnrichmentStatus": "Pending",
                })
                by_name[_name_key(it.name)] = {"Id": None}  # avoid duplicate inserts within this batch

        fresh_ids = {e["Id"] for e in existing if e["Fresh"]}
        members = {rec_id: ("Already in database (recent)" if rec_id in fresh_ids else "Already in database")
                   for rec_id, _ in updates}
        for rec_id, fill in updates:
            fill["ScanRunId"] = run_id
            if rec_id not in fresh_ids:
                fill["EnrichmentStatus"] = "Pending"
            cur.execute(f"UPDATE dbo.Vendors SET {', '.join(f'{c} = ?' for c in fill)} WHERE Id = ?",
                        [*fill.values(), rec_id])
        if inserts:
            cols = list(inserts[0])
            cur.executemany(
                f"INSERT INTO dbo.Vendors ({', '.join(cols)}) SELECT {', '.join('?' * len(cols))} "
                f"WHERE NOT EXISTS (SELECT 1 FROM dbo.Vendors WHERE RecordKey = ?)",
                [[r[c] for c in cols] + [r["RecordKey"]] for r in inserts])
            keys = [r["RecordKey"] for r in inserts]
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                cur.execute(f"SELECT Id FROM dbo.Vendors WHERE RecordKey IN ({', '.join('?' * len(chunk))})", chunk)
                members.update({int(r[0]): "New" for r in cur.fetchall()})
        if members:
            cur.executemany("INSERT INTO dbo.ScanRunRecords (ScanRunId, VendorId, Outcome) SELECT ?, ?, ? WHERE NOT "
                            "EXISTS (SELECT 1 FROM dbo.ScanRunRecords WHERE ScanRunId = ? AND VendorId = ?)",
                            [[run_id, vid, outcome, run_id, vid] for vid, outcome in members.items()])
        conn.commit()
        return len(inserts), len(updates), sum(1 for rec_id, _ in updates if rec_id in fresh_ids)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise DbError(_friendly(exc)) from exc
    finally:
        conn.close()


def pending_records(cfg: DbConfig, run_id: int) -> list[dict]:
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(RESCAN_COLUMNS)} FROM dbo.Vendors "
                    "WHERE ScanRunId = ? AND EnrichmentStatus = N'Pending' ORDER BY Id", [run_id])
        return [dict(zip(RESCAN_COLUMNS, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def save_enrichment(cfg: DbConfig, rec_id: int, fields: dict) -> None:
    """Store one record's deep-dive results (gap fills, routing, status) as soon as it's done."""
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE dbo.Vendors SET {', '.join(f'{c} = ?' for c in fields)}, "
                    "EnrichedAt = SYSUTCDATETIME(), LastScraped = SYSUTCDATETIME() WHERE Id = ?",
                    [*fields.values(), rec_id])
        conn.commit()
    finally:
        conn.close()


def run_counts(cfg: DbConfig, run_id: int) -> dict[str, int]:
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute("""SELECT COUNT(*),
                              SUM(CASE WHEN EnrichmentStatus IN (N'Done', N'Failed') THEN 1 ELSE 0 END),
                              SUM(CASE WHEN Queue = N'Primary' THEN 1 ELSE 0 END),
                              SUM(CASE WHEN Queue = N'Secondary' THEN 1 ELSE 0 END)
                       FROM dbo.Vendors WHERE ScanRunId = ?""", [run_id])
        total, done, prim, sec = (int(x or 0) for x in cur.fetchone())
        return {"total": total, "done": done, "primary": prim, "secondary": sec}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Queues
# --------------------------------------------------------------------------- #

def set_queue(cfg: DbConfig, ids: list[int], queue: str) -> int:
    """Move records by hand (promote/demote); they stay put until auto-routing is turned back on."""
    if not ids:
        return 0
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE dbo.Vendors SET Queue = ?, QueueLocked = 1 WHERE Id IN ({', '.join('?' * len(ids))})",
                    [queue, *[int(i) for i in ids]])
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def reroute(cfg: DbConfig, ids: list[int] | None = None, critical: list[str] | None = None,
            unlock: bool = False) -> dict[str, int]:
    """Recompute Queue/MissingFields/CompletionScore for the given records (all if None).

    unlock=True also clears hand moves first, handing the records back to automatic routing.
    """
    import routing
    critical = critical or critical_fields(cfg)
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        where = f" WHERE Id IN ({', '.join(str(int(i)) for i in ids)})" if ids else ""
        if ids is not None and not ids:
            return {"Primary": 0, "Secondary": 0}
        if unlock:
            cur.execute(f"UPDATE dbo.Vendors SET QueueLocked = 0{where}")
        cur.execute(f"SELECT {', '.join(RESCAN_COLUMNS)} FROM dbo.Vendors{where}")
        rows = [dict(zip(RESCAN_COLUMNS, r)) for r in cur.fetchall()]
        counts = {"Primary": 0, "Secondary": 0}
        params = []
        for r in rows:
            f = routing.route(r, critical)
            queue = f.get("Queue", r["Queue"])
            counts[queue] += 1
            params.append([queue, f["MissingFields"], f["CompletionScore"], r["Id"]])
        cur.executemany("UPDATE dbo.Vendors SET Queue = ?, MissingFields = ?, CompletionScore = ? WHERE Id = ?",
                        params)
        conn.commit()
        return counts
    finally:
        conn.close()


def scan_outcomes(cfg: DbConfig, run_id: int) -> dict[int, str]:
    """{record Id: 'New' / 'Already in database' / ...} for the businesses a scan found."""
    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute("SELECT VendorId, Outcome FROM dbo.ScanRunRecords WHERE ScanRunId = ?", [run_id])
        return {int(r[0]): r[1] for r in cur.fetchall()}
    finally:
        conn.close()
