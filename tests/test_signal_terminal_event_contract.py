from pathlib import Path


def test_post_submit_nonfill_is_visible_execution_rejection():
    source = Path('app/orchestrator.py').read_text()
    assert 'if not result.get("filled"):' in source
    assert '"EXECUTION_REJECTED", decision_id' in source
    assert 'reason = str(result.get("reason") or "not_filled")' in source


def test_signal_pipeline_has_visible_terminal_events():
    source = Path('app/orchestrator.py').read_text()
    assert '"RISK_REJECTED"' in source
    assert '"EXECUTION_REJECTED"' in source
    assert '"POSITION_OPENED"' in source
