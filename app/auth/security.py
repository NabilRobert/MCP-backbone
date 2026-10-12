import hashlib
import hmac
import os
import secrets

_SCRYPT_PARAMS = {"n": 2**14, "r": 8, "p": 1}


def generate_api_key() -> str:
    return f"jarvis_{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    # API keys are already high-entropy random tokens, so a fast hash is
    # fine here -- unlike passwords, there's no weak-secret brute-force risk.
    return hashlib.sha256(key.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT_PARAMS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, _, digest_hex = stored.partition("$")
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT_PARAMS)
    return hmac.compare_digest(actual, expected)
