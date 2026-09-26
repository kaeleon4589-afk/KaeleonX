from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.backtest.replay import StrategyReplay, load_candles_csv


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay KAELEON v6 against OHLCV CSV files")
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--1m", dest="one_minute", required=True)
    parser.add_argument("--5m", dest="five_minute", required=True)
    parser.add_argument("--15m", dest="fifteen_minute", required=True)
    parser.add_argument("--1h", dest="one_hour", required=True)
    parser.add_argument("--output", default="replay-report.json")
    parser.add_argument("--spread-bps", type=float, default=2.0)
    parser.add_argument("--leverage", type=int, default=10)
    parser.add_argument("--fee-rate", type=float, default=0.0006)
    args = parser.parse_args()

    frames = {
        "1m": load_candles_csv(args.one_minute),
        "5m": load_candles_csv(args.five_minute),
        "15m": load_candles_csv(args.fifteen_minute),
        "1h": load_candles_csv(args.one_hour),
    }
    report = StrategyReplay(
        spread_bps=args.spread_bps,
        leverage=args.leverage,
        fee_rate=args.fee_rate,
    ).run(frames, symbol=args.symbol.upper())
    target = Path(args.output)
    target.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
    print(json.dumps(report.summary, indent=2, default=str))
    print(f"report={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
