from abc import ABC, abstractmethod
from app.models.regime import BreadthResult
class BreadthEngine(ABC):
    @abstractmethod
    def evaluate(self) -> BreadthResult: ...
class DefaultBreadthEngine(BreadthEngine):
    def evaluate(self) -> BreadthResult: return BreadthResult()
