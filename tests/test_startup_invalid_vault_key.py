from pathlib import Path


def test_invalid_vault_key_does_not_hard_crash_startup():
    source = Path("app/api/app.py").read_text(encoding="utf-8")
    assert 'raise RuntimeError("invalid_credential_encryption_key_in_production")' not in source
    assert "credential-dependent endpoints are temporarily unavailable" in source
    assert 'vault_status = "invalid_or_unconfigured"' in source
