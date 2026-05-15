from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class StrategyDecision:
    target_symbol: str
    action: str
    reason: str


@dataclass(frozen=True)
class CandidateForecast:
    symbol: str
    expected_return: float
    near_term_return: float
    confidence: float
    near_term_confidence: float
    last_price: float
    provider: str


class MultiStockStrategy:
    def __init__(
        self,
        buy_threshold: float,
        sell_threshold: float,
        min_expected_profit_multiple: float,
        trade_fee: float,
        min_take_profit_pct: float,
        min_forecast_confidence: float,
    ) -> None:
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.min_expected_profit_multiple = min_expected_profit_multiple
        self.trade_fee = trade_fee
        self.min_take_profit_pct = min_take_profit_pct
        self.min_forecast_confidence = min_forecast_confidence

    def pick_buy(self, forecasts: list[CandidateForecast], budget: float) -> StrategyDecision:
        if not forecasts:
            return StrategyDecision(target_symbol="", action="hold", reason="No candidates")
        ranked = sorted(forecasts, key=lambda c: c.expected_return, reverse=True)
        top = ranked[0]
        expected_profit = budget * top.expected_return
        min_profit = self.trade_fee * self.min_expected_profit_multiple
        if top.expected_return < self.buy_threshold:
            return StrategyDecision(target_symbol="", action="hold", reason="Best forecast below buy threshold")
        if top.confidence < self.min_forecast_confidence:
            return StrategyDecision(
                target_symbol="",
                action="hold",
                reason=f"Top confidence {top.confidence:.2f} below minimum {self.min_forecast_confidence:.2f}",
            )
        if expected_profit < min_profit:
            return StrategyDecision(
                target_symbol="",
                action="hold",
                reason=f"Expected profit {expected_profit:.2f} below minimum {min_profit:.2f}",
            )
        return StrategyDecision(
            target_symbol=top.symbol,
            action="buy",
            reason=f"{top.symbol} selected ({top.expected_return:.4f} expected return, conf {top.confidence:.2f})",
        )

    def should_sell(self, forecast: CandidateForecast, unrealized_profit: float, unrealized_profit_pct: float) -> bool:
        profit_floor = self.trade_fee * self.min_expected_profit_multiple
        confidence_floor = max(0.12, self.min_forecast_confidence * 0.8)

        # Uses the same model outputs as buy logic:
        # - expected_return: main horizon forecast
        # - near_term_return: shorter-horizon forecast
        strong_model_downside = (
            forecast.expected_return <= self.sell_threshold
            and forecast.confidence >= confidence_floor
        )
        strong_near_downside = (
            forecast.near_term_return <= min(-0.003, self.sell_threshold * 0.5)
            and forecast.near_term_confidence >= confidence_floor
        )
        broad_downside = (
            forecast.expected_return < 0
            and forecast.near_term_return < 0
            and (
                forecast.confidence >= confidence_floor
                or forecast.near_term_confidence >= confidence_floor
            )
        )

        # Lock profits when model starts signaling reversal.
        if unrealized_profit_pct >= self.min_take_profit_pct and (strong_near_downside or broad_downside):
            return True

        # Cut losers if both horizon signals are negative with decent confidence.
        if unrealized_profit_pct < 0 and broad_downside:
            return True

        # Allow profitable exits when downside is strong enough to justify fees.
        if unrealized_profit >= profit_floor and (strong_model_downside or strong_near_downside):
            return True

        # Emergency exit on decisive model downside even if fee floor not met.
        return strong_model_downside and strong_near_downside
