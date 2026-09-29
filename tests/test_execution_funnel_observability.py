from __future__ import annotations

import asyncio
import time

from app.execution.paper import PaperExecutionEngine
from app.trading.rejection_funnel import RejectionFunnel
from test_core_pipeline_refactor import build_orchestrator, snapshot


def _reasons(payload):
    return {item["reason"]: item["count"] for item in payload["top_execution_blockers"]}


def test_rejection_funnel_exposes_execution_blockers_separately():
    funnel = RejectionFunnel()
    funnel.record("analyzed")
    funnel.record("risk_approved")
    funnel.record("execution_rejected", "market_unavailable")
    funnel.record("execution_error", "execution_submit_error")

    payload = funnel.payload()

    assert payload["risk_approved"] == 1
    assert payload["submitted"] == 0
    assert payload["execution_rejected"] == 1
    assert payload["execution_error"] == 1
    assert _reasons(payload) == {
        "execution_rejected:market_unavailable": 1,
        "execution_error:execution_submit_error": 1,
    }


def test_post_risk_stale_quote_is_visible_in_funnel_before_submit():
    execution = PaperExecutionEngine(initial_equity=100, leverage=10)
    orchestrator, _, audit, _, _ = build_orchestrator(execution, mode="demo")
    snap = snapshot("BTC", 100.0)
    snap.quote_received_ms = int(time.time() * 1000) - 20_000

    result = asyncio.run(orchestrator.on_snapshot(snap, 100.0, user_id="u1"))
    payload = orchestrator.rejection_funnel.payload()

    assert result == {"accepted": False, "filled": False, "reason": "market_unavailable"}
    assert payload["risk_approved"] == 1
    assert payload["submitted"] == 0
    assert payload["execution_rejected"] == 1
    assert _reasons(payload)["execution_rejected:market_unavailable"] == 1
    rejected = [
        data for event, _, data in audit.events
        if event == "EXECUTION_REJECTED" and data.get("reason") == "market_unavailable"
    ]
    assert rejected
    assert rejected[-1]["quote_age_ms"] >= 20_000
    assert rejected[-1]["market_bid"] == snap.bid
    assert rejected[-1]["market_ask"] == snap.ask



class ExplodingExecution:
    mode = "demo"
    leverage = 10

    def submit(self, intent, quantity, market=None):
        raise RuntimeError("submit_failed_for_test")


def test_execution_submit_exception_is_visible_in_funnel():
    orchestrator, _, audit, _, _ = build_orchestrator(ExplodingExecution(), mode="demo")
    snap = snapshot("BTC", 100.0)
    snap.quote_received_ms = int(time.time() * 1000)

    result = asyncio.run(orchestrator.on_snapshot(snap, 100.0, user_id="u1"))
    payload = orchestrator.rejection_funnel.payload()

    assert result == {"accepted": False, "filled": False, "reason": "execution_error"}
    assert payload["risk_approved"] == 1
    assert payload["submitted"] == 1
    assert payload["execution_error"] == 1
    assert _reasons(payload)["execution_error:execution_submit_error"] == 1
    assert any(
        event == "PIPELINE_ERROR" and data.get("stage") == "execution_submit"
        for event, _, data in audit.events
    )


def test_all_pre_submit_execution_rejections_are_funnel_instrumented():
    source = open("app/orchestrator.py", encoding="utf-8").read()
    for reason in (
        "market_unavailable",
        "fill_outside_trade_geometry",
        "open_position_exists",
        "execution_pending",
        "setup_already_executed",
    ):
        assert f'self._funnel("execution_rejected", "{reason}"' in source
    assert 'if str(exc) == "trading_worker_lease_expired"' in source
    assert 'self._funnel("execution_error", "execution_submit_error"' in source
