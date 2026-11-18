"""
Optional at-rest encryption for secrets stored in the database (provider API
keys/secrets, Mr Ai Pay keys). Enabled when SECRET_ENCRYPTION_KEY is set.

  * Any string works as the key — it is stretched with SHA-256 into a Fernet key.
  * Values are stored as  enc:v1:<token>.  Values without the prefix are treated
    as legacy plaintext, so turning encryption on later is safe.
  * If you LOSE the key, encrypted values cannot be recovered — keep a backup.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from config import SECRET_ENCRYPTION_KEY

PREFIX = "enc:v1:"

_fernet: Fernet | None = None
if SECRET_ENCRYPTION_KEY:
    _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_ENCRYPTION_KEY.encode()).digest()))


def encryption_enabled() -> bool:
    return _fernet is not None


def encrypt(value: str | None) -> str | None:
    if value is None or _fernet is None or value.startswith(PREFIX):
        return value
    return PREFIX + _fernet.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if value is None or not value.startswith(PREFIX):
        return value
    if _fernet is None:
        raise RuntimeError("Encrypted value found in the database but SECRET_ENCRYPTION_KEY is not set.")
    try:
        return _fernet.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("SECRET_ENCRYPTION_KEY does not match the key used to encrypt the database values.") from exc
