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
FIELDS = ["RecordKey", "VendorName", "Website", "WebsiteDomain", "WebsiteSource", "ContactName", "Email",
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
"""


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


def records_from_sheet(df: pd.DataFrame, mapping: dict[str, str], vendor_col: str, source_file: str) -> list[dict]:
    """Records from spreadsheet-mode results; unmapped sheet columns go into ExtraData."""
    used = {vendor_col, *mapping.values()}
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
    RecordKey NVARCHAR(450) NOT NULL PRIMARY KEY, VendorName NVARCHAR(400), Website NVARCHAR(1000),
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
EDITABLE_COLUMNS = ["VendorName", "Website", "ContactName", "Email", "Phone", "InsuranceOrBond", "Notes"]
VIEW_COLUMNS = ["Id", *EDITABLE_COLUMNS, "WebsiteSource", "ScrapeStatus", "Source", "SourceFile", "ExtraData",
                "FirstSeen", "LastScraped", "LastEdited", "LastEditedBy"]


@dataclass
class ChangeResult:
    updated: int = 0
    inserted: int = 0
    deleted: int = 0
    conflicts: list[str] | None = None  # rows changed by someone else since loading


def load_vendors(cfg: DbConfig) -> tuple[pd.DataFrame, dict[int, bytes]]:
    """All saved records, newest activity first, plus each row's version stamp by Id."""
    conn = connect(cfg)
    try:
        ensure_schema(conn)
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(VIEW_COLUMNS)}, RowVer FROM dbo.Vendors "
                    "ORDER BY COALESCE(LastEdited, LastScraped) DESC, Id DESC")
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
            fields = {c: row.get(c) for c in EDITABLE_COLUMNS if c not in ("VendorName", "Website", "Notes")}
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
            result.inserted += 1
        conn.commit()
    except DbError:
        raise
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise DbError(_friendly(exc)) from exc
    finally:
        conn.close()
    return result
