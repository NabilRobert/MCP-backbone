"""Encrypts secrets this server has to be able to read back later --
today, the target-database connection URLs (each one carries a database
password). Those can't be hashed like API keys, since the server needs
the real value to connect. Encrypting them means a leaked copy of the
bookkeeping database (or one of its backups) doesn't leak every user's
database password with it.

The key lives in the environment (APP_CONNECTION_ENCRYPTION_KEY), never
in the database it protects. Limit, stated plainly: an attacker with
full control of the running server can read both, so this protects
against a data leak, not a full server compromise."""

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class EncryptionKeyError(Exception):
    """The encryption key is missing, malformed, or isn't the key the
    value was encrypted with."""


def _fernet() -> Fernet:
    key = settings.connection_encryption_key
    if not key:
        raise EncryptionKeyError(
            "APP_CONNECTION_ENCRYPTION_KEY is not set -- see .env.example for how to generate one"
        )
    try:
        return Fernet(key)
    except ValueError as exc:
        raise EncryptionKeyError("APP_CONNECTION_ENCRYPTION_KEY is not a valid Fernet key") from exc


def check_encryption_key() -> None:
    """Raises EncryptionKeyError now if the key is missing or malformed."""
    _fernet()


def encrypt(plaintext: str) -> str:
    """Fernet: AES-128-CBC plus an HMAC, so a tampered value fails to
    decrypt instead of silently decrypting to something else."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise EncryptionKeyError(
            "could not decrypt a stored connection URL -- wrong APP_CONNECTION_ENCRYPTION_KEY?"
        ) from exc
