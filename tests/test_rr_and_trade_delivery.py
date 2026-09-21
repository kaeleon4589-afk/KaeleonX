from types import SimpleNamespace

from app.models.enums import Direction
from app.models.trading import Position
from app.telegram.notifications import TelegramTradeNotifier


def test_execution_rr_matches_entry_stop_target_geometry():
    entry = 0.20954
    stop = 0.20807322
    target = 0.210799632571394
    rr = abs(target-entry) / abs(entry-stop)
    assert 0.85 < rr < 0.87


def test_trade_telegram_messages_include_strategy_and_precise_prices():
    notifier = object.__new__(TelegramTradeNotifier)
    sent = []
    notifier._schedule = lambda user_id, text: sent.append((user_id, text))
    p = Position('p1','d1','ENA',Direction.LONG,12.5,0.20954,0.20807322,0.210799632571394)
    p.strategy = 'LIQUIDITY_SWEEP'
    p.execution_rr = 0.8588
    notifier.position_opened('u1','demo',p)
    assert 'Estrategia: LIQUIDITY_SWEEP' in sent[-1][1]
    assert 'Entrada: 0.209540' in sent[-1][1]
    assert 'RR ejecución:' in sent[-1][1]

    p.status = 'CLOSED'
    p.exit_price = 0.210799632571394
    p.exit_reason = 'TP2'
    p.realized_pnl = 0.15
    notifier.position_closed('u1','demo',p)
    assert 'Operación cerrada' in sent[-1][1]
    assert 'Estrategia: LIQUIDITY_SWEEP' in sent[-1][1]
    assert 'PnL neto: +0.15 USDT' in sent[-1][1]
