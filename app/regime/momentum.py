from abc import ABC, abstractmethod
from app.models.market import Candle
from app.models.regime import MomentumResult
class MomentumEngine(ABC):
    @abstractmethod
    def evaluate(self, candles: list[Candle]) -> MomentumResult: ...
class DefaultMomentumEngine(MomentumEngine):
    def evaluate(self, candles: list[Candle]) -> MomentumResult: return MomentumResult()
