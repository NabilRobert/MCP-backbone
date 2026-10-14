import hashlib
import hmac
import os
import secrets

# Parameters for scrypt, the algorithm used to hash passwords below. These
# control how much CPU/memory work it takes to check one password --
# deliberately slow, which is exactly what you want for something an
# attacker might try to guess millions of times.
_SCRYPT_PARAMS = {"n": 2**14, "r": 8, "p": 1}


def generate_api_key() -> str:
    """Makes a brand-new, random API key. secrets.token_urlsafe gives a
    long random string that's safe to put in a URL; the "dbmcp_" prefix
    is just so a key is recognizable at a glance (e.g. in logs)."""
    return f"dbmcp_{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    # API keys are already high-entropy random tokens, so a fast hash is
    # fine here -- unlike passwords, there's no weak-secret brute-force risk.
    return hashlib.sha256(key.encode()).hexdigest()


def hash_password(password: str) -> str:
    """Turns a plain-text password into something safe to store in the
    database. A random "salt" is mixed in so the same password never
    produces the same stored value twice -- this is what stops an
    attacker from pre-computing a table of common password hashes."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT_PARAMS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Checks a plain-text password against a previously stored hash
    (from hash_password above). Re-does the same hashing with the same
    salt and compares the result -- hmac.compare_digest is used instead
    of a plain == so the comparison always takes the same amount of
    time, regardless of where the mismatch happens (a timing attack
    defense)."""
    salt_hex, _, digest_hex = stored.partition("$")
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT_PARAMS)
    return hmac.compare_digest(actual, expected)
