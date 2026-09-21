from __future__ import annotations


def _num(value, default=0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return float(default)


def position_net_pnl(position: dict) -> float:
    """Return realized net PnL for a persisted position.

    PositionManager stores realized_pnl gross of exit fees. PaperExecutionEngine
    accounts fees separately, so API metrics normalize both demo and live records
    by subtracting persisted entry/exit fees and adding funding PnL.
    """
    return (
        _num(position.get("realized_pnl"))
        - _num(position.get("entry_fee"))
        - _num(position.get("exit_fee"))
        + _num(position.get("funding_pnl"))
    )


def calculate_performance(positions: list[dict], starting_capital: float) -> dict:
    closed = [p for p in positions if str(p.get("status", "OPEN")).upper() != "OPEN"]
    closed.sort(key=lambda p: (_num(p.get("closed_at")), str(p.get("position_id", ""))))

    pnls = [position_net_pnl(p) for p in closed]
    total_pnl = sum(pnls)
    wins = sum(1 for pnl in pnls if pnl > 0)
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))

    capital = max(_num(starting_capital), 0.0)
    equity = capital
    peak = capital
    max_drawdown_pct = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            drawdown_pct = max(0.0, (peak - equity) / peak * 100.0)
            max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

    trades = len(pnls)
    return {
        "pnl": total_pnl,
        "pnl_pct": (total_pnl / capital * 100.0) if capital else 0.0,
        "drawdown": max_drawdown_pct,
        "win_rate": (wins / trades * 100.0) if trades else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0),
        "trades": trades,
        "current_capital": capital + total_pnl,
    }
