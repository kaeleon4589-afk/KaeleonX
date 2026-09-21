from cryptography.fernet import Fernet

from app.security.credential_vault import CredentialVault
from app.storage.database import Database
from app.trading.profile import UserTradingProfileService
from app.trading.metrics import calculate_performance


def test_capital_requires_verified_coinw_and_is_separate_by_mode():
    db = Database()
    profiles = UserTradingProfileService(db, CredentialVault(Fernet.generate_key().decode()), minimum_operating_capital=3)
    uid = "u-1"

    profiles.save(uid, execution_mode="demo", trading_enabled=False, api_key="key-12345678", api_secret="secret-12345678")
    assert profiles.public(uid).coinw_configured is True
    assert profiles.public(uid).coinw_verified is False

    try:
        profiles.save(uid, execution_mode="demo", trading_enabled=False, operating_capital=25)
        assert False, "unverified capital should fail"
    except ValueError as exc:
        assert str(exc) == "coinw_verification_required_for_capital"

    profiles.mark_verified(uid, 42.5)
    profiles.save(uid, execution_mode="demo", trading_enabled=False, operating_capital=25)
    p = profiles.public(uid)
    assert p.demo_operating_capital == 25
    assert p.live_operating_capital == 0

    profiles.save(uid, execution_mode="live", trading_enabled=False)
    assert profiles.public(uid).operating_capital == 0
    profiles.save(uid, execution_mode="live", trading_enabled=False, operating_capital=20)
    p = profiles.public(uid)
    assert p.live_operating_capital == 20
    assert p.demo_operating_capital == 25

    profiles.save(uid, execution_mode="demo", trading_enabled=False)
    assert profiles.public(uid).operating_capital == 25


def test_changing_credentials_invalidates_verification():
    db = Database()
    profiles = UserTradingProfileService(db, CredentialVault(Fernet.generate_key().decode()))
    uid = "u-2"
    profiles.save(uid, execution_mode="demo", trading_enabled=False, api_key="key-abcdefgh", api_secret="secret-abcdefgh")
    profiles.mark_verified(uid, 100)
    assert profiles.public(uid).coinw_verified is True
    profiles.save(uid, execution_mode="demo", trading_enabled=False, api_key="key-new-abcdefgh", api_secret="secret-new-abcdefgh")
    assert profiles.public(uid).coinw_verified is False


def test_metrics_are_mode_specific_when_filtered():
    positions = [
        {"mode":"demo","status":"CLOSED","realized_pnl":5,"entry_fee":0,"exit_fee":0},
        {"mode":"live","status":"CLOSED","realized_pnl":-2,"entry_fee":0,"exit_fee":0},
    ]
    demo = calculate_performance([p for p in positions if p["mode"] == "demo"], 20)
    live = calculate_performance([p for p in positions if p["mode"] == "live"], 20)
    assert demo["pnl"] == 5
    assert live["pnl"] == -2
