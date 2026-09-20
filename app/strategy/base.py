from abc import ABC,abstractmethod
class Strategy(ABC):
    @abstractmethod
    def evaluate(self,*args,**kwargs): ...
