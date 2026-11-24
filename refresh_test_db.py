"""Replace the TEST database's records with a fresh copy of PRODUCTION's.

Usage:  .venv/bin/python refresh_test_db.py
Only writes to the test database; production is read-only here. Needs `az login`.
"""

import db

TEST_DATABASE = "ContactScraperTest"
PRODUCTION_DATABASE = "ContactScraper"


def main() -> None:
    cfg = db.load_config()
    if cfg is None:
        raise SystemExit("Azure SQL isn't configured (.streamlit/secrets.toml).")
    prod = db.DbConfig(server=cfg.server, database=PRODUCTION_DATABASE)
    test = db.DbConfig(server=cfg.server, database=TEST_DATABASE)
    src, dst = db.connect(prod, wait_for_resume=240), db.connect(test, wait_for_resume=240)
    try:
        db.ensure_schema(dst)
        s, d = src.cursor(), dst.cursor()
        d.execute("SELECT name FROM sys.columns WHERE object_id = OBJECT_ID('dbo.Vendors') "
                  "AND name <> 'RowVer' ORDER BY column_id")
        cols = [r[0] for r in d.fetchall()]
        s.execute(f"SELECT {', '.join(cols)} FROM dbo.Vendors ORDER BY Id")
        rows = [list(r) for r in s.fetchall()]
        d.execute("DELETE FROM dbo.Vendors")
        d.execute("SET IDENTITY_INSERT dbo.Vendors ON")
        if rows:
            d.executemany(f"INSERT INTO dbo.Vendors ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
        d.execute("SET IDENTITY_INSERT dbo.Vendors OFF")
        dst.commit()
        print(f"Copied {len(rows)} records from {PRODUCTION_DATABASE} to {TEST_DATABASE}.")
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    main()
