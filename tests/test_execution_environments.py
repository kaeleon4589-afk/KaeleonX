import pytest

from app.execution.control import TradingController, TradingState
from app.execution.environments import CoinWCredentials, TradingEnvironment
from app.execution.profile_manager import UserCoinWProfiles
from app.execution.demo import DemoExecutionEngine


def test_coinw_profile_value_object_supports_demo_and_live():
    profiles = UserCoinWProfiles()
    with pytest.raises(ValueError):
        profiles.require(TradingEnvironment.DEMO)

    profiles.set("coinw-key", "coinw-secret")
    demo = profiles.require(TradingEnvironment.DEMO)
    live = profiles.require(TradingEnvironment.LIVE)
    assert demo.credentials == live.credentials
    assert demo.rest_base_url == live.rest_base_url


def test_credentials_require_key_and_secret():
    with pytest.raises(ValueError):
        CoinWCredentials("", "secret").validate()
    with pytest.raises(ValueError):
        CoinWCredentials("key", "").validate()


def test_demo_is_internal_and_never_live_adapter():
    assert DemoExecutionEngine.mode == "demo"


def test_pause_is_blocked_by_open_position():
    controller = TradingController(TradingState.ACTIVE)
    result = controller.request_pause(1)
    assert result["blocked"] is True
    assert result["paused"] is False
    assert controller.state == TradingState.ACTIVE


def test_pause_works_without_open_position():
    controller = TradingController(TradingState.ACTIVE)
    result = controller.request_pause(0)
    assert result["paused"] is True
    assert controller.state == TradingState.PAUSED
