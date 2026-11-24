"""Bring a database up to date with this version of the app. Safe to run any number of times.

Usage:  .venv/bin/python migrate.py ContactScraperTest      (or ContactScraper for production)

Runs as the signed-in Azure user (`az login`), because the web apps' own identities may only read
and write data, not change the table structure. deploy.sh runs it before every deploy.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import checks
import db


def main(database: str) -> None:
    base = db.load_config()
    if base is None:
        raise SystemExit("Azure SQL isn't configured (.streamlit/secrets.toml).")
    cfg = db.DbConfig(server=base.server, database=database)
    conn = db.connect(cfg, wait_for_resume=240)
    try:
        db.ensure_schema(conn)  # adds any missing columns/tables
        cur = conn.cursor()
        # Emails never checked: check them, so the "Valid email" critical field can be judged
        cur.execute("SELECT Id, Email FROM dbo.Vendors WHERE EmailCheckedAt IS NULL AND Email LIKE N'%@%'")
        rows = cur.fetchall()
        if rows:
            with ThreadPoolExecutor(8) as pool:
                statuses = list(pool.map(lambda r: checks.check_emails(r[1]), rows))
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            cur.executemany("UPDATE dbo.Vendors SET EmailStatus = ?, EmailCheckedAt = ? WHERE Id = ?",
                            [[s, now, r[0]] for r, s in zip(rows, statuses)])
            conn.commit()
        # Scans from before ScanRunRecords existed: remember at least each record's latest scan
        cur.execute("INSERT INTO dbo.ScanRunRecords (ScanRunId, VendorId, Outcome) SELECT v.ScanRunId, v.Id, "
                    "CASE WHEN v.FirstSeen >= r.StartedAt THEN N'New' ELSE N'Already in database' END "
                    "FROM dbo.Vendors v JOIN dbo.ScanRuns r ON r.Id = v.ScanRunId WHERE NOT EXISTS "
                    "(SELECT 1 FROM dbo.ScanRunRecords x WHERE x.ScanRunId = v.ScanRunId AND x.VendorId = v.Id)")
        conn.commit()
        cur.execute("SELECT Id FROM dbo.Vendors WHERE CompletionScore IS NULL")
        unrouted = [int(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()
    routed = db.reroute(cfg, unrouted) if unrouted else {}
    print(f"{database}: schema up to date; {len(rows)} emails checked; "
          f"{len(unrouted)} records routed {routed or ''}".strip())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: migrate.py <database name>")
    main(sys.argv[1])
