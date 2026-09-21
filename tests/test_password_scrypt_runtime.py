from app.auth.service import hash_password, verify_password


def test_scrypt_hash_and_verify_with_explicit_memory_limit():
    encoded = hash_password("StrongPass123!")
    assert encoded.startswith("scrypt$32768$8$1$")
    assert verify_password("StrongPass123!", encoded) is True
    assert verify_password("WrongPass123!", encoded) is False
