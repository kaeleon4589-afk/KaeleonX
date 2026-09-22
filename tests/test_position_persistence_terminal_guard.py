from pathlib import Path


def test_order_persistence_strips_runtime_position_object():
    source = Path('app/orchestrator.py').read_text()
    assert 'order_doc = {k: v for k, v in result.items() if k != "position"}' in source
    assert '"orders"' in source


def test_filled_result_without_position_is_terminal_error():
    source = Path('app/orchestrator.py').read_text()
    assert 'if result.get("filled") and not result.get("position"):' in source
    assert 'filled_result_missing_position' in source


def test_accepted_signal_finally_has_terminal_guard():
    source = Path('app/orchestrator.py').read_text()
    assert 'accepted_signal = True' in source
    assert 'accepted_signal and not terminal_event_emitted' in source
    assert 'accepted_signal_without_terminal_event' in source


def test_position_persist_failure_is_visible():
    source = Path('app/orchestrator.py').read_text()
    assert 'stage="position_persist"' in source
    assert 'reason": "position_persist_error"' in source
