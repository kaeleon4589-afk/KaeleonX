from pathlib import Path


def test_api_embeds_worker_behind_flag():
    text = Path('app/api/app.py').read_text()
    assert 'settings.trading_worker_enabled' in text
    assert "asyncio.create_task" in text
    assert "WORKER_STARTED" in text


def test_health_exposes_worker_status():
    text = Path('app/api/app.py').read_text()
    assert 'trading_worker_running' in text


def test_railway_disables_access_log():
    text = Path('railway.toml').read_text()
    assert '--no-access-log' in text
