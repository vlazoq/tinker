"""
infra/security/encryption.py
======================
Application-level encryption for Tinker artifacts.

Why application-level (not SQLCipher)?
---------------------------------------
SQLCipher requires a custom compiled sqlite3 extension, which is not
available in all Python distributions and adds a complex build dependency.
Application-level encryption encrypts the artifact *content* before
writing to the database, leaving the schema and metadata in plaintext.
This protects confidential architecture designs while keeping the
infrastructure simple.

Algorithm
---------
AES-256-GCM (authenticated encryption).
Key derivation: PBKDF2-HMAC-SHA256 from a master secret + per-artifact salt.

Usage
-----
::

    enc = ArtifactEncryptor(master_key=os.getenv("TINKER_ARTIFACT_KEY"))
    encrypted = enc.encrypt("my secret design")
    plaintext = enc.decrypt(encrypted)  # "my secret design"

The encrypted payload is a base64-encoded JSON string containing:
  {"v": 1, "salt": "<hex>", "nonce": "<hex>", "ciphertext": "<hex>", "tag": "<hex>"}

This format is self-contained — the decryptor needs only the master key.

If TINKER_ARTIFACT_KEY is not set, encryption is a no-op (plaintext passthrough).
"""

import base64
import json
import logging
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger(__name__)

# Sentinel prefix embedded in payload JSON to distinguish encrypted payloads
# from plain text during decryption.  Any valid payload will decode as JSON
# and contain this version key; plain strings will not.
_PAYLOAD_VERSION = 1


def _derive_key(master_key: str, salt: bytes) -> bytes:
    """Derive a 32-byte AES key from master_key + salt via PBKDF2-HMAC-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    return kdf.derive(master_key.encode())


class ArtifactEncryptor:
    """
    AES-256-GCM encryptor for Tinker artifact content.

    Parameters
    ----------
    master_key : Optional[str]
        Master secret used for key derivation.  If None, falls back to the
        ``TINKER_ARTIFACT_KEY`` environment variable.  If neither is set,
        the encryptor is disabled and all operations are no-ops.
    """

    def __init__(self, master_key: Optional[str] = None) -> None:
        resolved = master_key or os.getenv("TINKER_ARTIFACT_KEY")
        if resolved:
            self._master_key: Optional[str] = resolved
            self._enabled = True
        else:
            self._master_key = None
            self._enabled = False

    @property
    def is_enabled(self) -> bool:
        """True if a master key is configured and encryption is active."""
        return self._enabled

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt *plaintext* and return a self-contained payload string.

        Returns *plaintext* unchanged if encryption is not enabled.

        Parameters
        ----------
        plaintext : str
            The artifact content to encrypt.

        Returns
        -------
        str
            Base64-encoded JSON payload, or the original plaintext if disabled.
        """
        if not self._enabled:
            return plaintext

        salt = os.urandom(16)
        key = _derive_key(self._master_key, salt)  # type: ignore[arg-type]

        nonce = os.urandom(12)  # 96-bit, standard for AES-GCM
        aesgcm = AESGCM(key)

        # aesgcm.encrypt appends the 16-byte GCM tag to the ciphertext
        ct_with_tag = aesgcm.encrypt(nonce, plaintext.encode(), None)

        # Separate ciphertext body from GCM authentication tag
        ciphertext = ct_with_tag[:-16]
        tag = ct_with_tag[-16:]

        payload = {
            "v": _PAYLOAD_VERSION,
            "salt": salt.hex(),
            "nonce": nonce.hex(),
            "ciphertext": ciphertext.hex(),
            "tag": tag.hex(),
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def decrypt(self, payload: str) -> str:
        """
        Decrypt an encrypted payload string back to plaintext.

        Gracefully passes through unencrypted (legacy) data — if the input
        does not look like a valid encrypted payload, it is returned as-is.

        Parameters
        ----------
        payload : str
            The value to decrypt, as produced by :meth:`encrypt`.

        Returns
        -------
        str
            Decrypted plaintext, or *payload* unchanged if not enabled or
            if the payload is not in encrypted format.
        """
        if not self._enabled:
            return payload

        # Attempt to detect and decode an encrypted payload.
        # Any failure means the data is plaintext (legacy passthrough).
        try:
            decoded = base64.b64decode(payload.encode())
            data = json.loads(decoded)
            if not isinstance(data, dict) or data.get("v") != _PAYLOAD_VERSION:
                return payload  # Not our format — return as-is
        except Exception:
            return payload  # Plaintext passthrough

        try:
            salt = bytes.fromhex(data["salt"])
            nonce = bytes.fromhex(data["nonce"])
            ciphertext = bytes.fromhex(data["ciphertext"])
            tag = bytes.fromhex(data["tag"])
        except (KeyError, ValueError) as exc:
            logger.debug("Malformed encryption payload, returning as-is: %s", exc)
            return payload

        key = _derive_key(self._master_key, salt)  # type: ignore[arg-type]
        aesgcm = AESGCM(key)

        # Reconstitute ct_with_tag for AESGCM.decrypt
        ct_with_tag = ciphertext + tag
        plaintext_bytes = aesgcm.decrypt(nonce, ct_with_tag, None)
        return plaintext_bytes.decode()


class NullEncryptor(ArtifactEncryptor):
    """
    No-op encryptor always in disabled state.

    Useful in tests and environments where encryption is deliberately
    not configured.  All encrypt/decrypt calls return input unchanged.
    """

    def __init__(self) -> None:
        # Do not call super().__init__() — we force disabled state directly
        self._master_key = None
        self._enabled = False
