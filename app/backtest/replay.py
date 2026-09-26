from __future__ import annotations

import csv
import math
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from app.models.enums import Direction
from app.models.market import Candle
from app.regime.regime_engine import RegimeEngine
from app.strategy.router import StrategyRouter

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


@dataclass(frozen=True)
class ReplayTrade:
    symbol: str
    strategy: str
    direction: str
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    stop_price: float
    target_price: float
    exit_price: float
    exit_reason: str
    r_multiple: float
    price_return_pct: float
    margin_return_pct: float
    mae_pct: float
    mfe_pct: float


@dataclass(frozen=True)
class ReplayReport:
    summary: dict
    trades: tuple[ReplayTrade, ...]
    rejection_counts: dict

    def as_dict(self) -> dict:
        return {
            "summary": self.summary,
            "trades": [asdict(x) for x in self.trades],
            "rejection_counts": self.rejection_counts,
        }


def _timestamp_ms(value: str | int | float) -> int:
    if isinstance(value, (int, float)):
        raw = float(value)
    else:
        text = str(value).strip()
        try:
            raw = float(text)
        except ValueError:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    return int(raw if raw > 100_000_000_000 else raw * 1000)


def load_candles_csv(path: str | Path) -> list[Candle]:
    """Load timestamp/open/high/low/close/volume CSV rows.

    Timestamp may be epoch seconds, epoch milliseconds or ISO-8601. Duplicate
    timestamps are de-duplicated with the last row winning.
    """
    rows: dict[int, Candle] = {}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        if not reader.fieldnames or not required.issubset({x.strip().lower() for x in reader.fieldnames}):
            raise ValueError(f"csv_missing_columns:{','.join(sorted(required))}")
        keymap = {x.strip().lower(): x for x in reader.fieldnames}
        for row in reader:
            try:
                ts = _timestamp_ms(row[keymap["timestamp"]])
                o = float(row[keymap["open"]])
                h = float(row[keymap["high"]])
                low = float(row[keymap["low"]])
                c = float(row[keymap["close"]])
                volume = float(row[keymap["volume"]])
            except (TypeError, ValueError, KeyError):
                continue
            if not all(math.isfinite(x) for x in (o, h, low, c, volume)):
                continue
            if min(o, h, low, c) <= 0 or volume < 0 or low > min(o, c) or h < max(o, c):
                continue
            rows[ts] = Candle(ts, o, h, low, c, volume)
    return [rows[k] for k in sorted(rows)]


def _close_times(candles: Iterable[Candle], timeframe: str) -> list[int]:
    span = TF_MS[timeframe]
    return [int(x.timestamp) + span for x in candles]


def _summary(trades: list[ReplayTrade]) -> dict:
    values = [float(t.r_multiple) for t in trades]
    positives = [x for x in values if x > 0]
    negatives = [x for x in values if x < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    gross_profit = sum(positives)
    gross_loss = abs(sum(negatives))
    return {
        "trades": len(trades),
        "wins": len(positives),
        "losses": len(negatives),
        "breakeven": len(values) - len(positives) - len(negatives),
        "win_rate": round(len(positives) / len(values), 4) if values else 0.0,
        "profit_factor_r": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (None if not positives else float("inf")),
        "expectancy_r": round(sum(values) / len(values), 4) if values else 0.0,
        "total_r": round(sum(values), 4),
        "max_drawdown_r": round(max_drawdown, 4),
        "avg_mae_pct": round(sum(t.mae_pct for t in trades) / len(trades), 6) if trades else 0.0,
        "avg_mfe_pct": round(sum(t.mfe_pct for t in trades) / len(trades), 6) if trades else 0.0,
        "avg_margin_return_pct": round(sum(t.margin_return_pct for t in trades) / len(trades), 6) if trades else 0.0,
    }


class StrategyReplay:
    """Deterministic single-symbol replay of the live v6 setup/trigger lifecycle.

    This is a calibration tool, not an exchange fill simulator. It uses candle
    OHLC and a configurable synthetic spread. If stop and target are both touched
    by the same 1m candle, the stop is assumed first (conservative ordering).
    """

    def __init__(self, *, router=None, regime_engine=None, spread_bps: float = 2.0,
                 leverage: int = 10, fee_rate: float = 0.0006):
        self.router = router or StrategyRouter()
        self.regime_engine = regime_engine or RegimeEngine()
        self.spread_bps = max(0.0, float(spread_bps))
        self.leverage = max(1, int(leverage))
        self.fee_rate = max(0.0, float(fee_rate))

    def _snapshot(self, symbol: str, frames: dict[str, list[Candle]], indices: dict[str, int],
                  price: float, btc_candles=None):
        visible = {tf: values[:indices[tf]] for tf, values in frames.items() if tf in indices}
        half = self.spread_bps / 20_000.0
        return SimpleNamespace(
            symbol=symbol,
            timeframe="5m",
            candles=visible.get("5m", []),
            timeframes=visible,
            btc_candles=btc_candles,
            bid=price * (1.0 - half),
            ask=price * (1.0 + half),
            last=price,
            data_complete=True,
            orderbook_valid=True,
        )

    def run(self, frames: dict[str, list[Candle]], *, symbol: str = "BTC") -> ReplayReport:
        needed = {tf: sorted(list(frames.get(tf, [])), key=lambda x: x.timestamp) for tf in TF_MS}
        if any(not needed[tf] for tf in ("1m", "5m", "15m", "1h")):
            raise ValueError("replay_requires_1m_5m_15m_1h")
        close_times = {tf: _close_times(needed[tf], tf) for tf in TF_MS}
        trades: list[ReplayTrade] = []
        rejects: Counter[str] = Counter()
        armed = None
        position = None
        last_5m_index = -1
        decision_seq = 0

        for one_minute in needed["1m"]:
            now_ms = int(one_minute.timestamp) + TF_MS["1m"]
            indices = {tf: bisect_right(close_times[tf], now_ms) for tf in TF_MS}
            if indices["5m"] < 260 or indices["15m"] < 200 or indices["1h"] < 200:
                continue

            if position is not None:
                direction = position["direction"]
                entry = position["entry"]
                stop = position["stop"]
                target = position["target"]
                if direction == Direction.LONG:
                    adverse = max(0.0, entry - float(one_minute.low)) / entry
                    favorable = max(0.0, float(one_minute.high) - entry) / entry
                    stop_hit = float(one_minute.low) <= stop
                    target_hit = float(one_minute.high) >= target
                else:
                    adverse = max(0.0, float(one_minute.high) - entry) / entry
                    favorable = max(0.0, entry - float(one_minute.low)) / entry
                    stop_hit = float(one_minute.high) >= stop
                    target_hit = float(one_minute.low) <= target
                position["mae"] = max(position["mae"], adverse)
                position["mfe"] = max(position["mfe"], favorable)
                if stop_hit or target_hit:
                    # Conservative same-bar ordering.
                    exit_price = stop if stop_hit else target
                    reason = "SL" if stop_hit else "TP"
                    risk_abs = abs(entry - stop)
                    signed_move = (exit_price - entry) if direction == Direction.LONG else (entry - exit_price)
                    r_value = signed_move / max(risk_abs, 1e-12)
                    price_return = signed_move / max(entry, 1e-12)
                    margin_return = price_return * self.leverage - (2.0 * self.fee_rate * self.leverage)
                    trades.append(ReplayTrade(
                        symbol=symbol,
                        strategy=position["strategy"],
                        direction=direction.value,
                        entry_time_ms=position["entry_time_ms"],
                        exit_time_ms=now_ms,
                        entry_price=entry,
                        stop_price=stop,
                        target_price=target,
                        exit_price=exit_price,
                        exit_reason=reason,
                        r_multiple=round(r_value, 6),
                        price_return_pct=round(price_return * 100.0, 6),
                        margin_return_pct=round(margin_return * 100.0, 6),
                        mae_pct=round(position["mae"] * 100.0, 6),
                        mfe_pct=round(position["mfe"] * 100.0, 6),
                    ))
                    position = None
                continue

            snap = self._snapshot(symbol, needed, indices, float(one_minute.close))
            decision_seq += 1
            decision_id = f"replay-{decision_seq}"

            if armed is not None:
                status, intent, trace = self.router.trigger_armed(armed, snap, decision_id)
                if status == "cancelled":
                    rejects[f"armed_cancelled:{(trace or {}).get('reason', 'unknown')}"] += 1
                    armed = None
                elif status == "triggered" and intent is not None:
                    position = {
                        "direction": intent.direction,
                        "strategy": getattr(intent.strategy, "value", str(intent.strategy)),
                        "entry": float(intent.entry_price),
                        "stop": float(intent.stop_price),
                        "target": float(intent.target_price),
                        "entry_time_ms": now_ms,
                        "mae": 0.0,
                        "mfe": 0.0,
                    }
                    armed = None
                else:
                    rejects[f"armed_pending:{(trace or {}).get('reason', 'unknown')}"] += 1
                continue

            # Discover only once per newly closed 5m candle, matching the live
            # structural stage while still checking an armed trigger every minute.
            if indices["5m"] == last_5m_index:
                continue
            last_5m_index = indices["5m"]
            regime = self.regime_engine.evaluate_snapshot(snap)
            regime_meta = getattr(self.regime_engine, "last_metadata", {}) or {}
            armed = self.router.discover_armed(
                regime, snap, symbol, "5m", regime_metadata=regime_meta,
            )
            if armed is None:
                trace = getattr(self.router, "last_trace", {}) or {}
                branch = trace.get("armed") if isinstance(trace.get("armed"), dict) else {}
                rejects[f"strategy:{branch.get('reason') or trace.get('reason') or 'no_armable_setup'}"] += 1

        if position is not None:
            rejects["position_open_at_end"] += 1
        if armed is not None:
            rejects["setup_armed_at_end"] += 1
        return ReplayReport(_summary(trades), tuple(trades), dict(rejects.most_common()))
