from cryptography.fernet import Fernet
import pytest

from app.security.credential_vault import CredentialVault


def test_credential_vault_accepts_real_fernet_key():
    CredentialVault(Fernet.generate_key().decode())


def test_credential_vault_rejects_non_fernet_secret():
    with pytest.raises(ValueError, match="invalid_credential_encryption_key"):
        CredentialVault("this-is-not-a-fernet-key")
