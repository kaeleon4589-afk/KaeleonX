import asyncio
from types import SimpleNamespace

import pytest
from app.storage.database import Database
from app.models.enums import Direction
from app.models.trading import Position
from app.position.manager import PositionManager
from app.position.exit_engine import ExitEngine
from app.execution.live import LiveExecutionEngine
from app.telegram.notifications import TelegramTradeNotifier
from app.trading.worker_lease import WorkerLease
from app.coinw.normalization import base_quantity
from app.coinw.executor import CoinWExecutor
from test_core_pipeline_refactor import build_orchestrator, snapshot, CapturingAudit


class LiveStub:
    mode='live'
    leverage=10
    def __init__(self,fail_sync=False,fail_submit=False):
        self.calls=0;self.fail_sync=fail_sync;self.fail_submit=fail_submit
    async def sync_symbol(self,symbol):
        if self.fail_sync: raise RuntimeError('exchange_unreachable')
        return []
    async def submit(self,*a,**kw):
        self.calls+=1
        if self.fail_submit: raise TimeoutError('ambiguous_exchange_response')
        return {'accepted':True,'filled':False,'order_id':'o1'}
    async def pending_order_status(self,order_id):return {'status':'unknown'}


def test_live_sync_failure_prevents_submission():
    async def run():
        ex=LiveStub(fail_sync=True);orch,*_=build_orchestrator(ex,mode='live')
        await orch.on_snapshot(snapshot(),100,user_id='u1')
        assert ex.calls==0 and orch.last_rejection=='position_sync_failed'
    asyncio.run(run())


def test_ambiguous_live_request_persists_reservation_and_blocks_next_symbol():
    async def run():
        ex=LiveStub(fail_submit=True);orch,pm,audit,db,_=build_orchestrator(ex,mode='live')
        await orch.on_snapshot(snapshot(),100,user_id='u1')
        assert ex.calls==1
        assert db.find_one('execution_pending',{'user_id':'u1'})['active']
        await orch.on_snapshot(snapshot('ETH'),100,user_id='u1')
        assert ex.calls==1
    asyncio.run(run())


def test_live_close_waits_for_confirmed_settlement():
    manager=PositionManager(ExitEngine(),evaluate_local_exits=False)
    p=Position('p','d','BTC',Direction.LONG,1,100,99,102)
    manager.add(p,persist=False)
    changes=manager.reconcile_exchange([],100,symbol='BTC',persist=False,notify=False,settlements={})
    assert not changes['closed'] and p.status=='OPEN' and p.settlement_pending
    manager.reconcile_exchange([],101,symbol='BTC',persist=False,notify=False,
                               settlements={'p':{'exit_price':102,'net_pnl':1.8,'closed_at':100}})
    assert p.status=='CLOSED' and p.net_pnl==1.8 and p.exit_price==102 and not p.settlement_pending


def test_contract_face_value_is_multiplied_by_contract_count():
    assert base_quantity({'baseSize':.001,'currentPiece':20},100)==.02
    assert base_quantity({'quantityUnit':0,'quantity':20},100)==.2
    assert base_quantity({'quantityUnit':1,'quantity':20,'baseSize':.001},100)==.02
    assert base_quantity({'quantityUnit':2,'quantity':.02},100)==.02


def test_historical_settlement_ignores_opening_and_canceled_events():
    class Positions:
        async def history(self,symbol):
            return {'data':{'rows':[
                {'openId':'p','orderId':'a','status':'open','netProfit':5},
                {'openId':'p','orderId':'b','status':'close','orderStatus':'cancel','netProfit':50},
                {'openId':'p','orderId':'c','status':'close','orderStatus':'finish','netProfit':'2.5',
                 'tradePiece':'2','totalPiece':'2','avgClosePrice':'102','tradeStartDate':100},
            ]}}
    executor=CoinWExecutor(None,Positions())
    settled=asyncio.run(executor.settlements('BTC',['p']))
    assert settled['p']['net_pnl']==2.5 and settled['p']['exit_price']==102


def test_worker_lease_excludes_second_owner():
    db=Database(); first=WorkerLease(db); second=WorkerLease(db)
    assert first.renew() and first.valid()
    assert not second.renew()
    first.release()
    assert second.renew()


def test_telegram_outbox_retries_and_deduplicates():
    async def run():
        db=Database(); db.write('users',{'user_id':'u','telegram_chat_id':'fake-chat'})
        settings=SimpleNamespace(telegram_enabled=False,telegram_bot_token='')
        notifier=TelegramTradeNotifier(settings,db,None,CapturingAudit())
        class Bot:
            def __init__(self):self.calls=0
            async def send_message(self,chat,text):
                self.calls+=1
                if self.calls==1:raise RuntimeError('temporary')
        bot=Bot();notifier.bot=bot
        await notifier._enqueue('u','test-event')
        row=db.find_one('notification_outbox',{'status':'pending'})
        assert row['attempts']==1
        db.upsert('notification_outbox',{'event_id':row['event_id']},{'next_attempt_at':0})
        await notifier.deliver_pending()
        await notifier._enqueue('u','test-event')
        assert bot.calls==2
        assert db.find_one('notification_outbox',{'event_id':row['event_id']})['status']=='sent'
    asyncio.run(run())


def test_monitor_tracks_symbol_outside_scanner_and_reconciles_without_quote():
    from app.market.coordinator import MultiMarketCoordinator
    class FailingQuotes:
        async def depth(self,symbol): raise RuntimeError('depth_unavailable')
    async def run():
        received=[]
        async def callback(snap):
            received.append(snap)
            raise asyncio.CancelledError()
        coordinator=MultiMarketCoordinator(FailingQuotes(),None,poll_seconds=.01)
        with pytest.raises(asyncio.CancelledError):
            await coordinator.monitor(callback,lambda:['OUTSIDE_RANKING'])
        assert received[0].symbol=='OUTSIDE_RANKING' and received[0].monitor_only
        assert received[0].last is None
    asyncio.run(run())


def test_persisted_live_reservation_restored_on_restart(monkeypatch):
    from app.trading import runtime as runtime_module
    from app.trading.runtime import UserTradingRuntimeManager
    from app.config.settings import Settings
    from test_demo_balance_10x import profile
    async def run():
        db=Database(); profiles=profile(db)
        profiles.save('u',execution_mode='live',trading_enabled=False,operating_capital=20)
        db.upsert('execution_pending',{'user_id':'u'},{'active':True,'symbol':'BTC','decision_id':'d','order_id':'o'})
        monkeypatch.setattr(runtime_module,'build_live_adapter_from_credentials',lambda *a,**kw:object())
        manager=UserTradingRuntimeManager(Settings(),db,CapturingAudit(),profiles)
        runtime=await manager._build_runtime('u',profiles.get('u'),'fp')
        assert runtime.orchestrator.pending_execution['order_id']=='o'
        manager._runtimes={'u':runtime}
        assert manager.tracked_symbols()=={'BTC'}
    asyncio.run(run())
