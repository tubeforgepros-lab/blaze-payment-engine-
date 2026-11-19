import os, sqlite3, psycopg

DATABASE_URL = os.getenv("DATABASE_URL")
SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blaze_ledgers.db")

def get_conn():
    return psycopg.connect(DATABASE_URL) if DATABASE_URL else sqlite3.connect(SQLITE_PATH)

def q(sql: str) -> str:
    return sql.replace("?", "%s") if DATABASE_URL else sql

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS native_ledger (account_id TEXT PRIMARY KEY, account_type TEXT NOT NULL, balance DOUBLE PRECISION NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
    c.execute("CREATE TABLE IF NOT EXISTS track_ledger (job_id TEXT PRIMARY KEY, amount DOUBLE PRECISION NOT NULL, status TEXT NOT NULL, logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
    if DATABASE_URL:
        c.execute("INSERT INTO native_ledger (account_id, account_type, balance) VALUES ('pool_01', 'OPERATIONAL_POOL', 100000.00) ON CONFLICT (account_id) DO NOTHING;")
    else:
        c.execute("INSERT OR IGNORE INTO native_ledger (account_id, account_type, balance) VALUES ('pool_01', 'OPERATIONAL_POOL', 100000.00);")
    conn.commit()
    conn.close()
