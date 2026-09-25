from uuid import uuid4
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin_api
from app.storage.database import Database
from app.trading.demo_account import DemoAccount
from app.trading.statistics import TradingStatistics


def position(db, pid, mode='demo', opened=1000, pnl=5, status='CLOSED'):
    db.write('positions', {'position_id': pid, 'user_id': 'u', 'mode': mode,
                          'opened_at': opened, 'closed_at': opened + 10,
                          'status': status, 'realized_pnl': pnl, 'entry_fee': 1})


def test_reset_isolates_modes_resets_demo_balance_and_preserves_history(monkeypatch):
    db = Database()
    position(db, 'old')
    position(db, 'live', 'live')
    position(db, 'other_user', opened=1001, pnl=-3)
    db.update_many('positions', {'position_id': 'other_user'}, {'user_id': 'another'})
    account = DemoAccount(db)
    balance = account.balance('u')
    before = list(db.memory)
    service = TradingStatistics(db)
    monkeypatch.setattr('app.trading.statistics.time_ns', lambda: 2000_000_000)
    receipt = service.reset('demo', 'Setup 2', 'admin', str(uuid4()))
    assert receipt['started_at'] == 2000
    assert service.report('demo')['metrics']['trades'] == 0
    assert service.report('live')['metrics']['trades'] == 1
    assert all(row in db.memory for row in before)
    assert account.balance('u') == 100
    assert account.balance('another') == 100
    assert balance == 104
    position(db, 'new', opened=2001, pnl=-2)
    report = TradingStatistics(db).report('demo')  # fresh service / restart
    assert report['metrics']['trades'] == 1
    assert report['metrics']['pnl'] == -3
    assert report['metrics']['losses'] == 1
    assert report['metrics']['excluded_positions'] == 2
    assert account.balance('u') == 97
    assert service.report('live')['metrics']['trades'] == 1


def test_reset_refuses_open_positions_and_pending_orders(monkeypatch):
    db = Database()
    service = TradingStatistics(db)
    position(db, 'open', status='OPEN', pnl=0)
    with pytest.raises(ValueError, match='reset_requires_no_open_positions'):
        service.reset('demo', 'v2', 'admin', str(uuid4()))
    db.update_many('positions', {'position_id': 'open'}, {'status': 'CLOSED'})
    db.write('execution_pending', {'user_id': 'u', 'active': True})
    with pytest.raises(ValueError, match='reset_requires_no_pending_orders'):
        service.reset('demo', 'v2', 'admin', str(uuid4()))
    assert db.count('statistics_periods') == 0


def test_reset_receipt_idempotency_and_history(monkeypatch):
    db = Database()
    service = TradingStatistics(db)
    rid = str(uuid4())
    monkeypatch.setattr('app.trading.statistics.time_ns', lambda: 2000_000_000)
    first = service.reset('demo', 'v1', 'a', rid)
    monkeypatch.setattr('app.trading.statistics.time_ns', lambda: 3000_000_000)
    assert service.reset('demo', 'v1', 'a', rid) == first
    assert db.count('statistics_periods') == 1
    with pytest.raises(ValueError):
        service.reset('live', 'v1', 'a', rid)
    position(db, 'p', opened=2100)
    second = service.reset('demo', 'v2', 'a', str(uuid4()))
    assert second['previous_metrics']['pnl'] == 4
    assert service.report('demo')['period']['label'] == 'v2'
    assert len(service.report('demo')['history']) == 2


@pytest.fixture
def client(monkeypatch):
    db = Database()
    auth = SimpleNamespace(authenticate_token=lambda token: {
        'user_id': token, 'phone': '+5359494299' if token == 'admin' else '+5359000000'
    })
    audit = SimpleNamespace(event=lambda *a, **kw: None)
    monkeypatch.setattr(admin_api, 'deps', lambda: (db, auth, audit))
    monkeypatch.setattr(admin_api, 'get_settings', lambda: SimpleNamespace(admin_phone='+5359494299'))
    app = FastAPI()
    app.include_router(admin_api.router)
    return TestClient(app), db


def test_admin_authorization_and_request_validation(client):
    http, db = client
    path = '/admin/trading/statistics/reset'
    body = {'mode': 'demo', 'label': 'v2', 'confirm': True, 'request_id': str(uuid4())}
    assert http.post(path, json=body).status_code == 401
    assert http.post(path, json=body, headers={'Authorization': 'Bearer user'}).status_code == 403
    headers = {'Authorization': 'Bearer admin'}
    for change in ({'mode': 'all'}, {'confirm': False}, {'label': '  '}, {'request_id': 'bad'}):
        assert http.post(path, json={**body, **change}, headers=headers).status_code == 422
    assert db.count('statistics_periods') == 0
    assert http.post(path, json=body, headers=headers).status_code == 200
    assert http.post(path, json=body, headers=headers).status_code == 200
    assert db.count('statistics_periods') == 1
    report = http.get('/admin/trading/statistics?mode=demo', headers=headers).json()
    assert report['period']['label'] == 'v2'
    assert http.get('/admin/trading/statistics?mode=live').status_code == 401
    assert http.get('/admin/trading/statistics?mode=both', headers=headers).status_code == 422


def test_only_final_settlements_count_and_missing_open_time_is_excluded(monkeypatch):
    db = Database()
    service = TradingStatistics(db)
    monkeypatch.setattr('app.trading.statistics.time_ns', lambda: 2000_000_000)
    service.reset('live', 'v1', 'a', str(uuid4()))
    position(db, 'missing', 'live', opened=0)
    position(db, 'pending', 'live', opened=2100)
    db.update_many('positions', {'position_id': 'pending'}, {'settlement_pending': True})
    assert service.report('live')['metrics']['trades'] == 0
    db.update_many('positions', {'position_id': 'pending'}, {'settlement_pending': False, 'net_pnl': 7})
    assert service.report('live')['metrics']['pnl'] == 7


def test_user_performance_and_operations_start_new_period(monkeypatch):
    from app.api import user_api
    db = Database()
    position(db, 'demo-old', opened=1000, pnl=-4)
    position(db, 'live-old', 'live', opened=1000, pnl=9)
    account = DemoAccount(db)
    account.opening_balance('u')
    monkeypatch.setattr('app.trading.statistics.time_ns', lambda: 2000_000_000)
    TradingStatistics(db).reset('demo', 'Demo nuevo', 'admin', str(uuid4()))
    TradingStatistics(db).reset('live', 'Live nuevo', 'admin', str(uuid4()))
    class Profiles:
        demo_account = account
        def __init__(self, mode): self.mode = mode
        def public(self, user_id):
            return SimpleNamespace(execution_mode=self.mode, operating_capital=50,
                                   demo_available_equity=account.balance(user_id),
                                   coinw_available_equity=82, trading_enabled=False)
    mode = ['demo']
    monkeypatch.setattr(user_api, 'current', lambda _: (db, {'user_id': 'u'}, Profiles(mode[0]), None))
    assert user_api.performance()['trades'] == 0
    assert user_api.performance()['current_capital'] == 100
    assert user_api.operations()['closed'] == []
    position(db, 'demo-new', opened=2100, pnl=6)
    assert user_api.performance()['pnl'] == 5
    assert user_api.performance()['current_capital'] == 105
    mode[0] = 'live'
    assert user_api.performance()['pnl'] == 0
    assert user_api.performance()['current_capital'] == 82
    position(db, 'live-new', 'live', opened=2100, pnl=-2)
    assert user_api.performance()['pnl'] == -3
    assert user_api.performance()['current_capital'] == 82
