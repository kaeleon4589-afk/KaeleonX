from abc import ABC, abstractmethod
from app.models.market import MarketSnapshot
from app.models.regime import LiquidityResult
class LiquidityEngine(ABC):
    @abstractmethod
    def evaluate(self, snapshot: MarketSnapshot) -> LiquidityResult: ...
class DefaultLiquidityEngine(LiquidityEngine):
    def evaluate(self, snapshot: MarketSnapshot) -> LiquidityResult: return LiquidityResult(spread=snapshot.spread)
