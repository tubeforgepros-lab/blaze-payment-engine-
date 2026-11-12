import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="blaze-test-")
os.environ["BLAZE_DB_PATH"] = os.path.join(_TMP, "test_ledgers.db")
os.environ["BLAZE_ENV"] = "test"
os.environ["BLAZE_SEED_BALANCE"] = "1000.00"
os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
os.environ.pop("BLAZE_API_KEY", None)
os.environ.pop("CELERY_BROKER_URL", None)
os.environ.pop("REDIS_HOST", None)

import pytest  # noqa: E402

from init_ledgers import DB_PATH, setup_database  # noqa: E402


@pytest.fixture()
def client():
    import sqlite3

    from fastapi.testclient import TestClient

    import server

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    setup_database(DB_PATH)
    with TestClient(server.app) as c:
        yield c

    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM track_ledger;")
    conn.execute("DELETE FROM webhook_events;")
    conn.execute("DELETE FROM plaid_items;")
    conn.execute("UPDATE native_ledger SET balance = 1000.00 WHERE account_id = 'pool_01';")
    conn.commit()
    conn.close()
