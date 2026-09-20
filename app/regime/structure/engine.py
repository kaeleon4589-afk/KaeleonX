from abc import ABC, abstractmethod
from app.models.market import Candle
from app.models.regime import StructureResult
class StructureEngine(ABC):
    @abstractmethod
    def evaluate(self, candles: list[Candle]) -> StructureResult: ...
class DefaultStructureEngine(StructureEngine):
    def evaluate(self, candles: list[Candle]) -> StructureResult:
        return StructureResult()
