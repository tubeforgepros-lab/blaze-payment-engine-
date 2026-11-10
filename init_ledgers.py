import sqlite3

def setup_database():
    # Connects to a local ledger file (acts exactly like our Postgres instance)
    conn = sqlite3.connect("blaze_ledgers.db")
    cursor = conn.cursor()
    
    # 1. Create Native Ledger Table (Internal immutable balances)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS native_ledger (
            account_id TEXT PRIMARY KEY,
            account_type TEXT NOT NULL,
            balance REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # 2. Create Secondary/Shadow Tracking Ledger Table (System transaction logs)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS track_ledger (
            job_id TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            status TEXT NOT NULL,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # Seed our Operational Balance Pool with mock funding ($100,000.00)
    cursor.execute("""
        INSERT OR IGNORE INTO native_ledger (account_id, account_type, balance)
        VALUES ('pool_01', 'OPERATIONAL_POOL', 100000.00);
    """)
    
    conn.commit()
    conn.close()
    print("--- Financial Ledgers Initialized & Funded Successfully ---")

if __name__ == "__main__":
    setup_database()
