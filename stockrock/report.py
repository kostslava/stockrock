from __future__ import annotations

from dataclasses import dataclass, field

from stockrock.broker import TradeReceipt


@dataclass
class HoldingRow:
    symbol: str
    shares: int
    avg_price: float
    last_price: float

    @property
    def market_value(self) -> float:
        return self.shares * self.last_price


@dataclass
class CycleReport:
    run_at: str
    decision: str
    message: str
    forecast_expected_return: float
    forecast_provider: str
    forecast_confidence: float
    strategy_reason: str
    target_symbol: str | None
    equity: float
    cash: float
    loan_balance: float
    budget: float
    risk_dial: float
    trade_fee: float
    base_currency: str
    bull_symbol: str
    bear_symbol: str
    bull_price: float
    bear_price: float
    oil_proxy: str
    oil_last: float
    holdings: list[HoldingRow] = field(default_factory=list)
    receipts: list[TradeReceipt] = field(default_factory=list)
