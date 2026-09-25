import asyncio
import math
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.config.settings import Settings
from app.execution.demo import DemoExecutionEngine
from app.execution.live import LiveExecutionEngine
from app.models.enums import Direction, Strategy
from app.models.trading import TradeIntent, Position
from app.position.manager import PositionManager
from app.position.exit_engine import ExitEngine
from app.risk.manager import RiskManager
from app.storage.database import Database
from app.security.credential_vault import CredentialVault
from app.trading.demo_account import DemoAccount
from app.trading.profile import UserTradingProfileService
from app.trading.runtime import UserTradingRuntimeManager
from app.api.user_api import _config_payload


def profile(db, initial=100):
    service=UserTradingProfileService(db,CredentialVault(Fernet.generate_key().decode()),initial_demo_equity=initial)
    service.save('u',execution_mode='demo',trading_enabled=False,api_key='test-key',api_secret='test-secret')
    service.mark_verified('u',1000)
    return service


@pytest.mark.parametrize('side,exit_price,positive', [
    (Direction.LONG,102,True),(Direction.LONG,98,False),
    (Direction.SHORT,98,True),(Direction.SHORT,102,False)])
def test_realized_balance_fees_restart_and_duplicate_save(side,exit_price,positive):
    db=Database(); account=DemoAccount(db,100); assert account.balance('u')==100
    ex=DemoExecutionEngine(initial_equity=100,slippage_bps=0,leverage=10)
    stop,target=(99,102) if side==Direction.LONG else (101,98)
    intent=TradeIntent('d','BTC',Strategy.BREAKOUT_RETEST,side,100,stop,target,90,1,'5m')
    p=ex.submit(intent,200,{'bid':100,'ask':100,'ts':1})['position']
    manager=PositionManager(ExitEngine(),db=db,owner_user_id='u',owner_mode='demo',on_realized=ex.on_realized)
    manager.add(p)
    assert account.balance('u')==pytest.approx(99.88)
    manager.mark('BTC',exit_price,2)
    gross=(exit_price-100)*2*(1 if side==Direction.LONG else -1)
    expected=100+gross-.12-exit_price*2*.0006
    assert p.status=='CLOSED'
    assert ex.equity==pytest.approx(expected)
    assert account.balance('u')==pytest.approx(expected)
    assert (expected>100)==positive
    manager._persist(p,force=True)
    assert DemoAccount(db,999).balance('u')==pytest.approx(expected) # Restart never seeds again.


def test_fixed_allocation_and_full_account_follow_balance_without_polluting_live():
    db=Database(); profiles=profile(db)
    profiles.save('u',execution_mode='demo',trading_enabled=True,operating_capital=100)
    db.upsert('positions',{'position_id':'loss'},{'user_id':'u','mode':'demo','status':'CLOSED','realized_pnl':-7,'entry_fee':1,'exit_fee':2})
    p=profiles.public('u')
    assert p.demo_auto_compound and p.operating_capital==90 and p.demo_available_equity==90
    assert p.coinw_available_equity==1000
    profiles.save('u',execution_mode='demo',trading_enabled=True,operating_capital=20)
    db.upsert('positions',{'position_id':'win'},{'user_id':'u','mode':'demo','status':'CLOSED','realized_pnl':5})
    p=profiles.public('u')
    assert not p.demo_auto_compound and p.operating_capital==20 and p.demo_available_equity==95
    payload=_config_payload(p,SimpleNamespace(live_allowed=True,live_state='active'),Settings())
    assert payload['available_equity']==95 and payload['leverage']==10
    with pytest.raises(ValueError,match='exceeds_virtual_balance'):
        profiles.save('u',execution_mode='demo',trading_enabled=True,operating_capital=100)


def test_account_includes_more_than_a_history_page():
    db=Database(); account=DemoAccount(db,100)
    for i in range(120):
        db.upsert('positions',{'position_id':str(i)},{'user_id':'u','mode':'demo','realized_pnl':1,'status':'CLOSED'})
    assert account.balance('u')==220


def test_10x_risk_parity_and_no_second_pnl_multiplier():
    assert Settings().fixed_leverage==10
    demo=DemoExecutionEngine(); live=LiveExecutionEngine(object())
    assert demo.leverage==live.leverage==10
    intent=TradeIntent('d','BTC',Strategy.BREAKOUT_RETEST,Direction.LONG,100,99.4,100.72,90,1,'5m')
    risk=RiskManager()
    assert risk.evaluate(intent,100,leverage=demo.leverage)==risk.evaluate(intent,100,leverage=live.leverage)
    assert PositionManager._pnl(Position('p','d','BTC',Direction.LONG,2,100,99,102),101,2)==2


def test_late_open_write_cannot_undo_closed_position():
    db=Database()
    db.upsert('positions',{'position_id':'p'},{'status':'CLOSED','revision':2,'realized_pnl':5})
    db.upsert('positions',{'position_id':'p'},{'status':'OPEN','revision':1,'realized_pnl':0})
    assert db.find_one('positions',{'position_id':'p'})['realized_pnl']==5


def test_runtime_updates_closes_even_when_account_below_entry_minimum():
    async def scenario():
        from test_core_pipeline_refactor import CapturingAudit, snapshot
        db=Database(); profiles=profile(db)
        profiles.save('u',execution_mode='demo',trading_enabled=True,operating_capital=3)
        settings=Settings(credential_encryption_key=Fernet.generate_key().decode())
        manager=UserTradingRuntimeManager(settings,db,CapturingAudit(),profiles)
        row=profiles.get('u'); runtime=await manager._build_runtime('u',row,'fp')
        p=Position('p','d','BTC',Direction.LONG,.1,100,99,102)
        runtime.position_manager.add(p)
        runtime.execution.equity=2
        manager._runtimes={'u':runtime}
        async def no_refresh():pass
        manager.refresh=no_refresh
        await manager.run_snapshot(snapshot('BTC',98))
        await asyncio.gather(*runtime.orchestrator.persistence._background)
        assert p.status=='CLOSED'
        assert runtime.execution.equity<2
        assert 'BTC' not in manager.tracked_symbols()
    asyncio.run(scenario())


def test_complete_demo_runtime_close_api_and_restart(monkeypatch):
    async def run():
        from test_core_pipeline_refactor import CapturingAudit, DynamicRouter, FixedRegime, snapshot
        from app.api import user_api
        from app.api.app import app
        from fastapi.testclient import TestClient
        db=Database(); profiles=profile(db)
        db.upsert('users',{'user_id':'u'},{'status':'active'})
        profiles.save('u',execution_mode='demo',trading_enabled=True,operating_capital=100)
        settings=Settings(credential_encryption_key=Fernet.generate_key().decode())
        manager=UserTradingRuntimeManager(settings,db,CapturingAudit(),profiles)
        await manager.refresh()
        runtime=manager._runtimes['u']
        runtime.orchestrator.router=DynamicRouter();runtime.orchestrator.regime_engine=FixedRegime()
        await manager.run_snapshot(snapshot())
        p=next(iter(runtime.position_manager.positions.values()))
        assert p.leverage==10 and p.status=='OPEN'
        runtime.trading_enabled=False
        await manager.run_snapshot(snapshot(price=98))
        await asyncio.gather(*runtime.orchestrator.persistence._background)
        expected=100+p.realized_pnl-p.entry_fee-p.exit_fee
        assert p.status=='CLOSED' and runtime.execution.equity==pytest.approx(expected)
        monkeypatch.setattr(user_api,'current',lambda token:(db,{'user_id':'u'},profiles,SimpleNamespace(entitlement=lambda uid:SimpleNamespace(live_allowed=True,live_state='active'))))
        monkeypatch.setattr(user_api,'get_settings',lambda:settings)
        client=TestClient(app)
        config=client.get('/user/trading-config').json()
        assert config['demo_available_equity']==pytest.approx(expected)
        assert config['operating_capital']==pytest.approx(expected)
        perf=client.get('/user/performance').json()
        assert perf['capital']==100 and perf['current_capital']==pytest.approx(expected)
        assert len(client.get('/user/operations').json()['closed'])==1
        rebuilt=await manager._build_runtime('u',profiles.get('u'),'new')
        assert rebuilt.execution.equity==pytest.approx(expected)
        assert not rebuilt.position_manager.positions
    asyncio.run(run())
