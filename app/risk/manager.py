from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    quantity: float = 0.0  # quote-currency notional (CoinW quantityUnit=0)
    reason: str = ""
    base_quantity: float = 0.0
    margin_required: float = 0.0
    leverage: int = 1


class RiskManager:
    def __init__(self, max_risk_per_trade=.01, max_leverage=10, min_quality=60,
                 max_margin_fraction=.90, fee_rate=.0006, exit_slippage_bps=0.0):
        # Retained for callers loading older configurations; position size is
        # now allocated from the user's configured margin, not a stop-risk cap.
        self.max_risk_per_trade = max_risk_per_trade
        self.max_leverage = max_leverage
        self.min_quality = min_quality
        self.max_margin_fraction = max_margin_fraction
        self.fee_rate = fee_rate
        self.exit_slippage_bps = exit_slippage_bps

    def evaluate(self, intent, equity, leverage=1, available_equity=None):
        if not all(math.isfinite(float(v)) for v in (equity, intent.entry_price, intent.stop_price,
                                                       intent.quality, intent.risk_multiplier,
                                                       self.fee_rate, self.exit_slippage_bps)):
            return RiskDecision(False, 0, 'invalid_numeric_input')
        if intent.quality < self.min_quality:
            return RiskDecision(False, 0, "quality_below_threshold")
        if equity <= 0:
            return RiskDecision(False, 0, "equity_unavailable")
        if leverage < 1 or leverage > self.max_leverage:
            return RiskDecision(False, 0, "invalid_leverage")

        stop_distance = abs(intent.entry_price - intent.stop_price)
        if stop_distance <= 0 or intent.entry_price <= 0:
            return RiskDecision(False, 0, "invalid_stop")

        if self.fee_rate < 0 or self.exit_slippage_bps < 0:
            return RiskDecision(False, 0, "invalid_cost_assumptions")
        available = equity if available_equity is None else float(available_equity)
        if not math.isfinite(available) or available <= 0:
            return RiskDecision(False, 0, 'equity_unavailable')
        # Use the entire configured capital as margin. If it is also the entire
        # wallet, reserve the entry taker fee so CoinW can accept the order.
        # Extra unallocated wallet balance may cover that fee instead.
        quote_notional = min(equity * leverage,
                             available / (1.0 / leverage + self.fee_rate) * 0.999999)
        base_quantity = quote_notional / intent.entry_price
        if not math.isfinite(quote_notional) or quote_notional <= 0:
            return RiskDecision(False, 0, 'margin_cap_zero')
        margin_required = quote_notional / leverage
        if margin_required > equity + 1e-8 or margin_required + quote_notional * self.fee_rate > available + 1e-8:
            return RiskDecision(False, 0, "margin_limit")

        return RiskDecision(
            approved=True,
            quantity=quote_notional,
            reason="approved",
            base_quantity=base_quantity,
            margin_required=margin_required,
            leverage=int(leverage),
        )
