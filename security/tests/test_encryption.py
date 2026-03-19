"""
Tests for security/encryption.py
"""

import pytest
from security.encryption import ArtifactEncryptor, NullEncryptor

MASTER_KEY = "test-master-secret-key-for-unit-tests"


class TestArtifactEncryptor:
    def test_roundtrip(self):
        """encrypt/decrypt roundtrip returns the original text."""
        enc = ArtifactEncryptor(master_key=MASTER_KEY)
        original = "my secret architecture design"
        encrypted = enc.encrypt(original)
        assert encrypted != original
        decrypted = enc.decrypt(encrypted)
        assert decrypted == original

    def test_decrypt_plaintext_passthrough(self):
        """decrypt on plaintext (not an encrypted payload) returns it unchanged."""
        enc = ArtifactEncryptor(master_key=MASTER_KEY)
        plain = "this is just plain text, not encrypted"
        result = enc.decrypt(plain)
        assert result == plain

    def test_two_encryptions_differ(self):
        """Two encryptions of the same text produce different ciphertext (random nonce)."""
        enc = ArtifactEncryptor(master_key=MASTER_KEY)
        text = "same text encrypted twice"
        first = enc.encrypt(text)
        second = enc.encrypt(text)
        assert first != second
        # Both must still decrypt correctly
        assert enc.decrypt(first) == text
        assert enc.decrypt(second) == text

    def test_is_enabled_with_key(self):
        """is_enabled is True when a master key is provided."""
        enc = ArtifactEncryptor(master_key=MASTER_KEY)
        assert enc.is_enabled is True

    def test_is_enabled_without_key(self, monkeypatch):
        """is_enabled is False when no master key is set."""
        monkeypatch.delenv("TINKER_ARTIFACT_KEY", raising=False)
        enc = ArtifactEncryptor(master_key=None)
        assert enc.is_enabled is False

    def test_disabled_encrypt_passthrough(self, monkeypatch):
        """encrypt returns plaintext unchanged when not enabled."""
        monkeypatch.delenv("TINKER_ARTIFACT_KEY", raising=False)
        enc = ArtifactEncryptor(master_key=None)
        text = "should not be encrypted"
        assert enc.encrypt(text) == text

    def test_disabled_decrypt_passthrough(self, monkeypatch):
        """decrypt returns payload unchanged when not enabled."""
        monkeypatch.delenv("TINKER_ARTIFACT_KEY", raising=False)
        enc = ArtifactEncryptor(master_key=None)
        payload = "some payload"
        assert enc.decrypt(payload) == payload

    def test_empty_string_roundtrip(self):
        """Empty string can be encrypted and decrypted."""
        enc = ArtifactEncryptor(master_key=MASTER_KEY)
        encrypted = enc.encrypt("")
        assert enc.decrypt(encrypted) == ""

    def test_unicode_roundtrip(self):
        """Unicode content round-trips correctly."""
        enc = ArtifactEncryptor(master_key=MASTER_KEY)
        text = "日本語テスト 🔐 résumé"
        assert enc.decrypt(enc.encrypt(text)) == text


class TestNullEncryptor:
    def test_encrypt_returns_input_unchanged(self):
        """NullEncryptor.encrypt returns the input unchanged."""
        enc = NullEncryptor()
        text = "plaintext"
        assert enc.encrypt(text) == text

    def test_decrypt_returns_input_unchanged(self):
        """NullEncryptor.decrypt returns the input unchanged."""
        enc = NullEncryptor()
        payload = "anything"
        assert enc.decrypt(payload) == payload

    def test_is_enabled_false(self):
        """NullEncryptor.is_enabled is always False."""
        enc = NullEncryptor()
        assert enc.is_enabled is False
