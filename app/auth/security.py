import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Argon2id, via argon2-cffi, with its default (RFC 9106 low-memory)
# parameters. Unlike a hand-rolled hash, each stored value records its
# own parameters ("$argon2id$v=19$m=65536,t=3,p=4$..."), so they can be
# raised later without breaking existing passwords: check_needs_rehash
# spots the old ones, and they're upgraded at the next login.
_password_hasher = PasswordHasher()

# The scheme used before Argon2: scrypt with these parameters, stored as
# "salt_hex$digest_hex". Kept only to verify passwords that haven't been
# upgraded yet (see needs_rehash).
_LEGACY_SCRYPT_PARAMS = {"n": 2**14, "r": 8, "p": 1}


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
    database: a deliberately slow, memory-hard Argon2id hash with a
    random salt, so the same password never produces the same stored
    value twice."""
    return _password_hasher.hash(password)


def _is_legacy(stored: str) -> bool:
    return not stored.startswith("$argon2")


def verify_password(password: str, stored: str) -> bool:
    """Checks a plain-text password against a stored hash, either an
    Argon2 one or a legacy scrypt one. Both comparisons take constant
    time, so a near-miss isn't faster to reject than a far one."""
    if _is_legacy(stored):
        salt_hex, _, digest_hex = stored.partition("$")
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), **_LEGACY_SCRYPT_PARAMS
        )
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    try:
        return _password_hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    """True if a stored hash is legacy scrypt, or Argon2 with parameters
    weaker than the current ones. Call only after verify_password
    succeeds: replacing it needs the plain-text password."""
    return _is_legacy(stored) or _password_hasher.check_needs_rehash(stored)
