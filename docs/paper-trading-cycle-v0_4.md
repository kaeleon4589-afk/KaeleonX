# KAELEON Paper Trading Cycle v0.4

Implemented pipeline:

CoinW REST market data -> MarketCoordinator -> normalized market snapshot
-> SignalFactory -> RegimeEngine -> StrategyRouter
-> RiskManager -> PaperExecutionEngine
-> PositionManager / ExitEngine -> MongoDB Atlas + structured audit.

Paper execution uses live CoinW bid/ask data while keeping capital virtual.

Safety:
- No live order execution is enabled.
- One open paper position per symbol is enforced.
- Decision cooldown prevents duplicate decisions.
- Wide spread / invalid market data can block execution.
- Every decision carries a decision_id.

TP/SL:
- Structural SL plus ATR buffer.
- TP1 = 1R, 50% reduction.
- After TP1, stop moves to breakeven.
- TP2 = 2R by default, with a configurable minimum of 1.8R.
- SL/TP execution is simulated from live market prices.

MongoDB:
- decisions
- orders
- positions
- events
- pnl/index support

Next implementation stage:
- private CoinW authentication/signing
- live order/position reconciliation
- CoinW native SL/TP mapping
- multi-symbol / multi-timeframe orchestration
- richer MTF structure/breadth engines
- production recovery tests
- frontend only after the user defines the visual design.
