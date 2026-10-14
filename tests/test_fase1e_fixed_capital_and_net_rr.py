"""FASE 1E: DEMO capital allocated by the user never auto-compounds."""
from cryptography.fernet import Fernet

from app.security.credential_vault import CredentialVault
from app.storage.database import Database
from app.trading.profile import UserTradingProfileService
from app.risk.manager import RiskManager
from app.models.enums import Direction, Strategy
from app.models.trading import TradeIntent
from app.trading.edge_metrics import edge_estimate


def _profiles():
    db = Database()
    service = UserTradingProfileService(db, CredentialVault(Fernet.generate_key().decode()))
    service.save('u', execution_mode='demo', trading_enabled=False,
                 api_key='key-123456789', api_secret='secret-123456789')
    service.mark_verified('u', 100)
    return db, service


def test_full_wallet_selection_remains_at_100_after_profit_and_loss():
    db, service = _profiles()
    service.save('u', execution_mode='demo', trading_enabled=True, operating_capital=100)
    assert service.get('u')['demo_auto_compound'] is False
    for name, pnl, expected in [('win', 50, 150), ('loss', -70, 80)]:
        db.upsert('positions', {'position_id': name},
                  {'user_id': 'u', 'mode': 'demo', 'status': 'CLOSED', 'realized_pnl': pnl})
        profile = service.public('u')
        assert profile.demo_operating_capital == 100
        assert profile.operating_capital == 100
        assert profile.demo_available_equity == expected
        # Executable capital cannot exceed actual balance. This is not an
        # implicit reconfiguration of the user's desired capital.
        assert min(profile.operating_capital, profile.demo_available_equity) == min(100, expected)
    # User can re-save the existing allocation after a loss to pause/start.
    service.save('u', execution_mode='demo', trading_enabled=False, operating_capital=100)
    assert service.public('u').operating_capital == 100
    service.save('u', execution_mode='demo', trading_enabled=True, operating_capital=40)
    assert service.public('u').operating_capital == 40


def test_legacy_compound_flag_is_disabled_without_resetting_pnl_or_allocation():
    db, service = _profiles()
    service.save('u', execution_mode='demo', trading_enabled=True, operating_capital=100)
    db.upsert('user_trading_profiles', {'user_id':'u'}, {'demo_auto_compound': True})
    db.upsert('positions', {'position_id': 'historic'},
              {'user_id':'u', 'mode':'demo','status':'CLOSED','realized_pnl':37})
    before_balance = service.demo_account.balance('u')
    assert service.get('u')['demo_auto_compound'] is False
    assert service.public('u').operating_capital == 100
    assert service.demo_account.balance('u') == before_balance == 137
    assert db.find_one('positions', {'position_id':'historic'})['realized_pnl'] == 37


def test_cost_aware_net_rr_on_wide_and_tight_stops():
    for stop,target,viable in [(99.5,100.8,True), (99.8,100.3,False)]:
        edge = edge_estimate(entry=100, stop=stop, target=target,
                             direction=Direction.LONG, taker_fee_rate=.0006,
                             exit_slippage_bps=2)
        ratio = edge['estimated_net_tp_bps']/abs(edge['estimated_net_sl_bps'])
        assert (ratio >= 1.0) is viable


def test_fixed_capital_still_respects_actual_margin_and_fee_reserve():
    intent = TradeIntent('d', 'BTC', Strategy.BREAKOUT_RETEST, Direction.LONG,
                         100,99,102,85,1,'5m')
    risk = RiskManager(fee_rate=.0006)
    for available in [100,80]:
        configured = 100
        limit = min(configured, available)
        decision = risk.evaluate(intent, limit, leverage=10, available_equity=available)
        assert decision.approved
        assert decision.margin_required + decision.quantity*.0006 <= available
        assert configured == 100


def test_v2_demo_rejects_weak_net_rr_but_accepts_viable_entry():
    import asyncio
    from app.execution.paper import PaperExecutionEngine
    from test_core_pipeline_refactor import build_orchestrator, snapshot, CapturingAudit

    def once(stop, target):
        audit = CapturingAudit()
        execution = PaperExecutionEngine(initial_equity=100)
        orchestrator, manager, _, _, _ = build_orchestrator(execution, mode='demo', audit=audit)
        orchestrator.router.evaluate = lambda *a, **kw: TradeIntent(
            'd', 'BTC', Strategy.BREAKOUT_RETEST, Direction.LONG, 100, stop, target,
            86, 1, '5m', metadata={'engine_version':'v2','sl_pct':(100-stop)/100,'atr_value':1.0})
        result = asyncio.run(orchestrator.on_snapshot(snapshot('BTC', 100), 100, user_id='u1'))
        return orchestrator, manager, audit, result

    blocked, manager, audit, result = once(99.8, 100.4)
    assert result is None and not manager.positions
    assert blocked.last_rejection == 'net_execution_rr_too_low'
    assert any(name == 'SIGNAL_REJECTED' and data.get('reason') == 'net_execution_rr_too_low'
               for name, _, data in audit.events)
    accepted, manager, audit, result = once(99.0, 102)
    assert result is not None and result.get('filled') is True
    assert accepted.last_rejection is None
    assert len(manager.positions) == 1
