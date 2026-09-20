from abc import ABC, abstractmethod
from app.models.market import Candle
from app.models.regime import VolatilityResult
class VolatilityEngine(ABC):
    @abstractmethod
    def evaluate(self, candles: list[Candle]) -> VolatilityResult: ...
class DefaultVolatilityEngine(VolatilityEngine):
    def evaluate(self, candles: list[Candle]) -> VolatilityResult: return VolatilityResult()
