import sqlite3

def audit_ledgers():
    conn = sqlite3.connect("blaze_ledgers.db")
    cursor = conn.cursor()
    
    print("=== NATIVE LEDGER (Internal Balance Pool) ===")
    cursor.execute("SELECT account_id, account_type, balance, updated_at FROM native_ledger;")
    for row in cursor.fetchall():
        acct_id, acct_type, balance, updated_at = row
        print(f"Account: {acct_id} | Type: {acct_type} | Available Balance: ${balance:,.2f} | Last Updated: {updated_at}")
        
    print("\n=== SHADOW TRACKING LEDGER (System Transaction Logs) ===")
    cursor.execute("SELECT job_id, amount, status, logged_at FROM track_ledger;")
    rows = cursor.fetchall()
    if not rows:
        print("[Empty Ledger] No transactions have committed to the tracking ledger yet.")
    for row in rows:
        job_id, amount, status, logged_at = row
        print(f"Job ID: {job_id} | Amount: ${amount:,.2f} | Status: {status} | Timestamp: {logged_at}")
        
    conn.close()

if __name__ == "__main__":
    audit_ledgers()
