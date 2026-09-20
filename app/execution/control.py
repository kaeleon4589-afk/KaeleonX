from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TradingState(str, Enum):
    PAUSED = "paused"
    ACTIVE = "active"


@dataclass
class TradingController:
    """Controls new entries without ever abandoning an open position."""

    state: TradingState = TradingState.PAUSED

    def activate(self) -> TradingState:
        self.state = TradingState.ACTIVE
        return self.state

    def request_pause(self, open_positions: int) -> dict:
        if open_positions > 0:
            return {
                "paused": False,
                "blocked": True,
                "reason": "open_position",
                "message": "No se puede pausar el trading mientras exista una operación abierta.",
                "state": self.state.value,
            }
        self.state = TradingState.PAUSED
        return {"paused": True, "blocked": False, "state": self.state.value}
