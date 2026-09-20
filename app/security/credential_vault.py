from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    """Encrypt/decrypt user exchange credentials with one server-side master key.

    The CoinW API key/secret are never returned through the API once stored and are
    kept encrypted at rest. The master key is the only credential that belongs in
    Railway environment variables.
    """

    def __init__(self, key: str):
        key = (key or "").strip()
        if not key:
            raise ValueError("credential_encryption_key_required")
        try:
            self._fernet = Fernet(key.encode())
        except Exception as exc:
            raise ValueError("invalid_credential_encryption_key") from exc

    def encrypt(self, value: str) -> str:
        value = str(value or "")
        if not value:
            raise ValueError("credential_value_required")
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(str(token).encode()).decode()
        except (InvalidToken, ValueError, TypeError) as exc:
            raise ValueError("credential_decryption_failed") from exc

    @staticmethod
    def mask(value: str) -> str:
        value = str(value or "")
        if len(value) <= 8:
            return "••••••••"
        return f"{value[:4]}••••{value[-4:]}"
