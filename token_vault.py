"""At-rest protection for Plaid access tokens.

If BLAZE_TOKEN_ENCRYPTION_KEY is set (a Fernet key), tokens are encrypted
before being written to SQLite. Otherwise they are stored in plaintext and a
warning is emitted once — acceptable for sandbox, not for production.
"""
import os

_PREFIX = "enc:v1:"
_warned = False


def _fernet():
    key = os.getenv("BLAZE_TOKEN_ENCRYPTION_KEY")
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(token: str) -> str:
    global _warned
    f = _fernet()
    if f is None:
        if not _warned:
            print(
                "[VAULT] WARNING: BLAZE_TOKEN_ENCRYPTION_KEY not set; "
                "Plaid access tokens are stored in plaintext."
            )
            _warned = True
        return token
    return _PREFIX + f.encrypt(token.encode()).decode()


def decrypt(stored: str) -> str:
    if not stored.startswith(_PREFIX):
        return stored
    f = _fernet()
    if f is None:
        raise RuntimeError(
            "Token is encrypted but BLAZE_TOKEN_ENCRYPTION_KEY is not configured."
        )
    return f.decrypt(stored[len(_PREFIX) :].encode()).decode()


def generate_key() -> str:
    """Utility for operators: print a fresh Fernet key."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
