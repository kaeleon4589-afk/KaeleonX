from types import SimpleNamespace
from app.logging.logger import _ALWAYS_INFO, _STATE_INFO


def test_terminal_rejections_are_never_throttled():
    assert "RISK_REJECTED" in _ALWAYS_INFO
    assert "EXECUTION_REJECTED" in _ALWAYS_INFO
    assert "RISK_REJECTED" not in _STATE_INFO
    assert "EXECUTION_REJECTED" not in _STATE_INFO


def test_pipeline_error_is_visible():
    assert "PIPELINE_ERROR" in _ALWAYS_INFO
