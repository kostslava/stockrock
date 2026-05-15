from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Position:
    symbol: str
    shares: int
    avg_price: float


@dataclass
class PortfolioState:
    cash: float
    trade_fee: float
    positions: dict[str, Position] = field(default_factory=dict)

    def market_value(self, prices: dict[str, float]) -> float:
        total = self.cash
        for symbol, pos in self.positions.items():
            total += prices.get(symbol, 0.0) * pos.shares
        return total

    def sell_all(self, symbol: str, price: float) -> tuple[int, float]:
        pos = self.positions.get(symbol)
        if pos is None or pos.shares <= 0:
            return 0, 0.0
        gross = pos.shares * price
        net = gross - self.trade_fee
        self.cash += max(0.0, net)
        sold = pos.shares
        del self.positions[symbol]
        return sold, net

    def buy_with_budget(
        self,
        symbol: str,
        price: float,
        budget: float,
        *,
        min_gross_notional: float = 0.0,
    ) -> tuple[int, float]:
        if budget <= self.trade_fee or price <= 0.0:
            return 0, 0.0
        spendable = min(self.cash, budget) - self.trade_fee
        shares = int(spendable // price)
        if shares <= 0:
            return 0, 0.0
        if min_gross_notional > 0.0:
            min_shares = max(1, math.ceil(min_gross_notional / price))
            if shares < min_shares:
                return 0, 0.0
        cost = shares * price + self.trade_fee
        if cost > self.cash:
            return 0, 0.0
        self.cash -= cost
        pos = self.positions.get(symbol)
        if pos:
            new_shares = pos.shares + shares
            new_avg = ((pos.avg_price * pos.shares) + (shares * price)) / new_shares
            pos.shares = new_shares
            pos.avg_price = new_avg
        else:
            self.positions[symbol] = Position(symbol=symbol, shares=shares, avg_price=price)
        return shares, cost
