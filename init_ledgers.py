import os
import sqlite3

DB_PATH = os.getenv(
    "BLAZE_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "blaze_ledgers.db"),
)


def _column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table});").fetchall()}


def setup_database(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()

    # 1. Native ledger (internal operational balances)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS native_ledger (
            account_id TEXT PRIMARY KEY,
            account_type TEXT NOT NULL,
            balance REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # 2. Shadow tracking ledger (system transaction log)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS track_ledger (
            job_id TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            status TEXT NOT NULL,
            bank_tx_id TEXT,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # 3. Webhook idempotency store. Stripe delivers webhooks at-least-once,
    #    so every event id must be processed exactly once.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payout_id TEXT,
            amount REAL,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # --- Migrations for databases created by older revisions ---
    track_cols = _column_names(conn, "track_ledger")
    if "bank_tx_id" not in track_cols:
        cursor.execute("ALTER TABLE track_ledger ADD COLUMN bank_tx_id TEXT;")
        print("[MIGRATION] added track_ledger.bank_tx_id")

    # Backfill bank_tx_id from the legacy "job_id (bank_tx_id)" convention.
    cursor.execute(
        "SELECT job_id FROM track_ledger WHERE bank_tx_id IS NULL AND job_id LIKE '% (%)';"
    )
    for (legacy_job_id,) in cursor.fetchall():
        bank_tx_id = legacy_job_id[legacy_job_id.rfind("(") + 1 : -1]
        cursor.execute(
            "UPDATE track_ledger SET bank_tx_id = ? WHERE job_id = ?;",
            (bank_tx_id, legacy_job_id),
        )
        print(f"[MIGRATION] backfilled bank_tx_id={bank_tx_id}")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_track_bank_tx ON track_ledger(bank_tx_id);")

    # Seed the operational pool. Override with BLAZE_SEED_BALANCE in non-prod.
    seed = float(os.getenv("BLAZE_SEED_BALANCE", "100000.00"))
    cursor.execute(
        """
        INSERT OR IGNORE INTO native_ledger (account_id, account_type, balance)
        VALUES ('pool_01', 'OPERATIONAL_POOL', ?);
        """,
        (seed,),
    )

    conn.commit()
    conn.close()
    print(f"--- Financial Ledgers Initialized & Funded Successfully ({db_path}) ---")


if __name__ == "__main__":
    setup_database()
