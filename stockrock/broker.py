from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Protocol

from stockrock.investja import InvestJAWebClient, SellHoldError
from stockrock.portfolio import PortfolioState, Position

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeReceipt:
    side: str
    symbol: str
    shares: int
    price: float
    gross_or_cost: float


class Broker(Protocol):
    def sync(self) -> None:
        ...

    def sell_all(self, symbol: str, price: float) -> TradeReceipt | None:
        ...

    def buy(self, symbol: str, price: float, budget: float) -> TradeReceipt | None:
        ...


class PaperBroker:
    def __init__(self, portfolio: PortfolioState, *, min_gross_notional: float = 0.0) -> None:
        self.portfolio = portfolio
        self.min_gross_notional = max(0.0, float(min_gross_notional))

    def sync(self) -> None:
        return

    def sell_all(self, symbol: str, price: float) -> TradeReceipt | None:
        shares, net = self.portfolio.sell_all(symbol, price)
        if shares <= 0:
            return None
        return TradeReceipt(side="sell", symbol=symbol, shares=shares, price=price, gross_or_cost=net)

    def buy(self, symbol: str, price: float, budget: float) -> TradeReceipt | None:
        shares, cost = self.portfolio.buy_with_budget(
            symbol, price, budget, min_gross_notional=self.min_gross_notional
        )
        if shares <= 0:
            return None
        return TradeReceipt(side="buy", symbol=symbol, shares=shares, price=price, gross_or_cost=cost)


class InvestJABroker(PaperBroker):
    def __init__(
        self,
        portfolio: PortfolioState,
        username: str,
        password: str,
        exchange_map: dict[str, str],
        *,
        min_gross_notional: float = 0.0,
        max_loan: float = 0.0,
    ) -> None:
        super().__init__(portfolio, min_gross_notional=min_gross_notional)
        self.client = InvestJAWebClient(username=username, password=password)
        self.exchange_map = exchange_map
        self.last_loan_balance = 0.0
        self.max_loan = max(0.0, float(max_loan))
        # Platform-reported running total of transaction fees ("Transaction
        # fees paid: $X" on the home page). Authoritative source of truth.
        self.platform_fees_paid = 0.0
        # Aggregate market value of currently-held positions, scraped from
        # the "Holdings" row of the Account summary panel.
        self.platform_holdings_value = 0.0

    def _loan_headroom(self) -> float:
        return max(0.0, self.max_loan - self.last_loan_balance)

    def effective_buying_power(self) -> float:
        return self.portfolio.cash + self._loan_headroom()

    def _snapshot_with_retry(self, attempts: int = 3):
        last_exc: Exception | None = None
        for _ in range(max(1, attempts)):
            try:
                return self.client.get_snapshot()
            except Exception as exc:
                last_exc = exc
                logger.warning("InvestJA snapshot fetch failed: %s", exc)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("InvestJA snapshot fetch failed")

    def _safe_sync(self, *, context: str) -> bool:
        try:
            self.sync()
            return True
        except Exception as exc:
            logger.warning("InvestJA sync failed (%s): %s", context, exc)
            return False

    def _apply_snapshot(self, snapshot) -> None:
        """Update internal portfolio state from a pre-fetched snapshot."""
        self.portfolio.cash = snapshot.cash
        self.last_loan_balance = snapshot.loan_balance
        self.platform_fees_paid = float(getattr(snapshot, "fees_paid", 0.0))
        self.platform_holdings_value = float(getattr(snapshot, "holdings_value", 0.0))
        self.portfolio.positions.clear()
        for item in snapshot.positions:
            self.portfolio.positions[item.symbol] = Position(
                symbol=item.symbol,
                shares=item.shares,
                avg_price=item.avg_price,
            )

    def sync(self) -> None:
        self._apply_snapshot(self._snapshot_with_retry())

    def sell_all(self, symbol: str, price: float) -> TradeReceipt | None:
        # Use the cached portfolio for pre-state. The spam loop already syncs
        # once per pass; skipping the per-op pre-snapshot saves a full
        # navigation+parse on every sell.
        pre_qty = self.portfolio.positions.get(symbol).shares if symbol in self.portfolio.positions else 0
        pre_cash = self.portfolio.cash
        if pre_qty <= 0:
            # The cache might be stale: pass-start sync may have hit a network
            # blip, the snapshot parser may have missed a position type, or
            # external manual trades happened. Do one fresh fetch before
            # giving up so we don't silently skip real holdings.
            try:
                fresh = self._snapshot_with_retry()
                self._apply_snapshot(fresh)
                pre_qty = self.portfolio.positions.get(symbol).shares if symbol in self.portfolio.positions else 0
                pre_cash = self.portfolio.cash
            except Exception as exc:
                logger.warning("SELL %s: fresh snapshot failed (%s); skipping", symbol, exc)
                return None
            if pre_qty <= 0:
                logger.info("SELL %s skipped: platform confirms 0 shares held", symbol)
                return None
        exchange = self.exchange_map.get(symbol, "NASDAQ")
        try:
            post = self.client.sell_holding_via_url_after_gamepage_check(
                ticker=symbol,
                exchange_hint=exchange,
                quantity_hint=pre_qty,
            )
        except SellHoldError as exc:
            # Routine: don't downgrade to an error; let the caller mark the
            # ticker as hold-blocked and move on. Re-raise so the spam engine
            # sees this distinct exception type.
            logger.info("SELL %s blocked by platform 1hr rule: %s", symbol, exc)
            raise
        except Exception as exc:
            logger.error("InvestJA SELL failed for %s: %s", symbol, exc)
            self._safe_sync(context=f"post-failed-sell {symbol}")
            return None

        self._apply_snapshot(post)
        post_qty = next((p.shares for p in post.positions if p.symbol == symbol), 0)
        sold_qty = max(0, pre_qty - post_qty)
        if sold_qty <= 0:
            logger.info(
                "Post-sell HTML parse inconclusive for %s (sold_qty=0); fetching full snapshot",
                symbol,
            )
            try:
                post = self._snapshot_with_retry()
            except Exception as exc:
                logger.warning("Post-sell snapshot failed for %s: %s", symbol, exc)
                return None
            self._apply_snapshot(post)
            post_qty = next((p.shares for p in post.positions if p.symbol == symbol), 0)
            sold_qty = max(0, pre_qty - post_qty)
            if sold_qty <= 0:
                return None
        proceeds = max(0.0, post.cash - pre_cash)
        logger.info("InvestJA executed SELL %s %s shares", symbol, sold_qty)
        return TradeReceipt(side="sell", symbol=symbol, shares=sold_qty, price=price, gross_or_cost=proceeds)

    def buy(self, symbol: str, price: float, budget: float) -> TradeReceipt | None:
        if price <= 0:
            logger.warning("BUY %s skipped: invalid price %.4f", symbol, price)
            return None
        # Use the cached portfolio (already synced at pass start). This drops
        # one full login+snapshot round-trip per buy.
        buying_power = self.effective_buying_power()
        max_budget = min(buying_power, budget)
        if max_budget <= self.portfolio.trade_fee:
            logger.warning(
                "BUY %s skipped: max_budget=%.2f <= fee=%.2f "
                "(cash=%.2f, loan_balance=%.2f, max_loan=%.2f, budget=%.2f)",
                symbol, max_budget, self.portfolio.trade_fee,
                self.portfolio.cash, self.last_loan_balance, self.max_loan, budget,
            )
            return None
        base_qty = int((max_budget - self.portfolio.trade_fee) // price)
        if base_qty <= 0:
            logger.warning(
                "BUY %s skipped: base_qty=%s after fee (max_budget=%.2f, price=%.2f, fee=%.2f)",
                symbol, base_qty, max_budget, price, self.portfolio.trade_fee,
            )
            return None
        min_gross = self.min_gross_notional
        min_qty = 1
        if min_gross > 0.0:
            min_qty = max(1, math.ceil(min_gross / price))
        if base_qty < min_qty:
            logger.warning(
                "Skipping BUY %s: need at least %s shares (~%.2f gross) for fee economics; budget allows %s",
                symbol,
                min_qty,
                min_qty * price,
                base_qty,
            )
            return None

        pre_qty = self.portfolio.positions.get(symbol).shares if symbol in self.portfolio.positions else 0
        pre_cash = self.portfolio.cash
        exchange = self.exchange_map.get(symbol, "NASDAQ")

        attempts: list[int] = []
        for q in [base_qty, max(min_qty, base_qty // 2), max(min_qty, base_qty // 4), max(min_qty, base_qty // 10)]:
            qq = max(min_qty, int(q))
            if qq <= base_qty and qq not in attempts:
                attempts.append(qq)
        if base_qty not in attempts:
            attempts.insert(0, base_qty)

        for qty in attempts:
            try:
                self.client.place_order(side="buy", exchange=exchange, ticker=symbol, quantity=qty)
                post = self._snapshot_with_retry()
                self._apply_snapshot(post)
                post_qty = next((p.shares for p in post.positions if p.symbol == symbol), 0)
                filled = max(0, post_qty - pre_qty)
                gross = filled * price
                if filled > 0 and min_gross > 0.0 and gross < min_gross - 1e-6:
                    logger.warning(
                        "InvestJA BUY %s fill too small (%s sh, ~%.2f gross); below min %.2f — rejecting trade",
                        symbol,
                        filled,
                        gross,
                        min_gross,
                    )
                    return None
                if filled > 0:
                    cost = max(0.0, pre_cash - post.cash)
                    logger.info("InvestJA executed BUY %s %s shares", symbol, filled)
                    return TradeReceipt(side="buy", symbol=symbol, shares=filled, price=price, gross_or_cost=cost)
            except Exception as exc:
                logger.warning("InvestJA BUY attempt failed for %s qty=%s: %s", symbol, qty, exc)
                self._safe_sync(context=f"post-failed-buy-attempt {symbol} qty={qty}")
                continue

        logger.error("InvestJA BUY failed for %s after retries", symbol)
        return None
