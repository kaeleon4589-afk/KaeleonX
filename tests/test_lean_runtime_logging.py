
from app.logging.logger import AuditLogger


def test_execution_rejected_is_info(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    a = AuditLogger()
    mod = __import__("app.logging.logger", fromlist=["_ALWAYS_INFO", "_STATE_INFO"])
    assert "EXECUTION_REJECTED" in mod._ALWAYS_INFO
    assert "EXECUTION_REJECTED" not in mod._STATE_INFO


def test_market_snapshot_error_is_throttled_warning():
    m = __import__("app.logging.logger", fromlist=["_THROTTLED_WARNING"])
    assert "MARKET_SNAPSHOT_ERROR" in m._THROTTLED_WARNING


def test_strategy_trace_compaction_contract():
    src = open("app/orchestrator.py", encoding="utf-8").read()
    assert "compact_trace" in src
    assert "strategy_trace=strategy_trace" not in src
