from dataclasses import dataclass


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    quantity: float = 0.0  # quote-currency notional (CoinW quantityUnit=0)
    reason: str = ""
    base_quantity: float = 0.0
    margin_required: float = 0.0
    leverage: int = 1


class RiskManager:
    def __init__(self, max_risk_per_trade=.01, max_leverage=5, min_quality=60,
                 max_margin_fraction=.90):
        self.max_risk_per_trade = max_risk_per_trade
        self.max_leverage = max_leverage
        self.min_quality = min_quality
        self.max_margin_fraction = max_margin_fraction

    def evaluate(self, intent, equity, leverage=1):
        if intent.quality < self.min_quality:
            return RiskDecision(False, 0, "quality_below_threshold")
        if equity <= 0:
            return RiskDecision(False, 0, "equity_unavailable")
        if leverage < 1 or leverage > self.max_leverage:
            return RiskDecision(False, 0, "invalid_leverage")

        stop_distance = abs(intent.entry_price - intent.stop_price)
        if stop_distance <= 0 or intent.entry_price <= 0:
            return RiskDecision(False, 0, "invalid_stop")

        multiplier = max(0.0, min(1.0, intent.risk_multiplier))
        risk_cap = equity * self.max_risk_per_trade * multiplier
        base_quantity = risk_cap / stop_distance
        if base_quantity <= 0:
            return RiskDecision(False, 0, "invalid_quantity")

        # CoinW quantityUnit=0 is denominated in quote currency (USDT).
        quote_notional = base_quantity * intent.entry_price

        # Leverage controls required margin. Never let the trade consume more
        # than the configured fraction of available equity.
        max_notional = equity * leverage * self.max_margin_fraction
        if quote_notional > max_notional:
            quote_notional = max_notional
            base_quantity = quote_notional / intent.entry_price
            if base_quantity <= 0:
                return RiskDecision(False, 0, "margin_cap_zero")

        margin_required = quote_notional / leverage
        if margin_required > equity * self.max_margin_fraction:
            return RiskDecision(False, 0, "margin_limit")

        return RiskDecision(
            approved=True,
            quantity=quote_notional,
            reason="approved",
            base_quantity=base_quantity,
            margin_required=margin_required,
            leverage=int(leverage),
        )
