"""Symmetric encryption for secrets at rest (Fernet / AES-128-CBC + HMAC).

The key is derived from the app secret_key, so no extra key management is needed
for a single deployment. Ciphertext carries a prefix marker so plaintext values
(e.g. written before encryption was enabled) pass through unchanged on read.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc::"


class UndecryptableSecret(ValueError):
    pass


class SecretBox:
    def __init__(self, secret_key: str):
        # Fernet needs a 32-byte urlsafe-base64 key; derive one deterministically.
        key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return plaintext
        if plaintext.startswith(_PREFIX):
            return plaintext  # already encrypted
        return _PREFIX + self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value or not value.startswith(_PREFIX):
            return value  # legacy plaintext — pass through
        try:
            return self._fernet.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
        except InvalidToken:
            return value

    def decrypt_strict(self, value: str) -> str:
        """Decrypt or raise. For a value that is only ever written encrypted
        (an OAuth grant blob), a missing prefix or a failed MAC means the row
        was tampered with or the secret_key rotated; passing the ciphertext
        through as if it were plaintext would hand garbage to a JSON parser."""
        if not value.startswith(_PREFIX):
            raise UndecryptableSecret("value is not encrypted")
        try:
            return self._fernet.decrypt(value[len(_PREFIX) :].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise UndecryptableSecret("value cannot be decrypted with this secret_key") from exc

    def encrypt_map(self, data: dict[str, str]) -> dict[str, str]:
        return {k: self.encrypt(str(v)) for k, v in (data or {}).items()}

    def decrypt_map(self, data: dict[str, str]) -> dict[str, str]:
        return {k: self.decrypt(str(v)) for k, v in (data or {}).items()}
