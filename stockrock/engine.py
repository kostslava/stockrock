from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

import numpy as np

from stockrock.broker import Broker, TradeReceipt
from stockrock.config import Settings
from stockrock.data import YahooDataClient
from stockrock.model import Forecaster
from stockrock.portfolio import PortfolioState
from stockrock.report import CycleReport, HoldingRow
from stockrock.strategy import CandidateForecast, MultiStockStrategy, StrategyDecision
from stockrock.universe import StockSpec

logger = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        data_client: YahooDataClient,
        forecaster: Forecaster,
        strategy: MultiStockStrategy,
        broker: Broker,
        portfolio: PortfolioState,
        notify,
        advisor,
        specs: list[StockSpec],
    ) -> None:
        self.settings = settings
        self.data_client = data_client
        self.forecaster = forecaster
        self.strategy = strategy
        self.broker = broker
        self.portfolio = portfolio
        self.notify = notify
        self.advisor = advisor
        self.specs = specs
        self._exchange_map = {s.ticker: s.exchange for s in specs}
        self._buy_blocklist: set[str] = set()

    def _usd_to_base_rate(self) -> float:
        if self.settings.base_currency.strip().upper() != "CAD":
            return 1.0
        try:
            return self.data_client.get_last_price("USDCAD=X")
        except Exception as exc:
            logger.warning("Failed to fetch USDCAD rate, defaulting to 1.0: %s", exc)
            return 1.0

    def _price_in_base(self, symbol: str, raw_price: float, usd_to_base_rate: float) -> float:
        exchange = self._exchange_map.get(symbol, "NASDAQ")
        if self.settings.base_currency.strip().upper() == "CAD" and exchange in {"NASDAQ", "NYSE"}:
            return raw_price * usd_to_base_rate
        return raw_price

    def _holding_rows(self, current_prices: dict[str, float]) -> list[HoldingRow]:
        rows: list[HoldingRow] = []
        for sym, pos in self.portfolio.positions.items():
            last = float(current_prices.get(sym, pos.avg_price))
            rows.append(
                HoldingRow(
                    symbol=sym,
                    shares=pos.shares,
                    avg_price=pos.avg_price,
                    last_price=last,
                )
            )
        return sorted(rows, key=lambda r: r.symbol)

    def _base_report(
        self,
        *,
        forecast: CandidateForecast,
        decision,
        equity: float,
        budget: float,
        current_prices: dict[str, float],
        quote_symbol_a: str,
        quote_symbol_b: str,
        oil_last: float,
        receipts: list[TradeReceipt],
        message: str,
    ) -> CycleReport:
        return CycleReport(
            run_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            decision=decision.action,
            message=message,
            forecast_expected_return=forecast.expected_return,
            forecast_provider=forecast.provider,
            forecast_confidence=forecast.confidence,
            strategy_reason=decision.reason,
            target_symbol=decision.target_symbol or None,
            equity=equity,
            cash=self.portfolio.cash,
            loan_balance=float(getattr(self.broker, "last_loan_balance", 0.0)),
            budget=budget,
            risk_dial=self.settings.risk_dial,
            trade_fee=self.settings.trade_fee,
            base_currency=self.settings.base_currency,
            bull_symbol=quote_symbol_a,
            bear_symbol=quote_symbol_b,
            bull_price=current_prices.get(quote_symbol_a, 0.0),
            bear_price=current_prices.get(quote_symbol_b, 0.0),
            oil_proxy=self.settings.oil_proxy_symbol,
            oil_last=oil_last,
            holdings=self._holding_rows(current_prices),
            receipts=receipts,
        )

    def _forecast_candidates(self) -> tuple[list[CandidateForecast], dict[str, float]]:
        forecasts: list[CandidateForecast] = []
        prices: dict[str, float] = {}
        all_specs = {s.ticker: s for s in self.specs}
        for held_symbol in self.portfolio.positions:
            if held_symbol not in all_specs:
                all_specs[held_symbol] = StockSpec(ticker=held_symbol, yahoo_symbol=held_symbol, exchange="NASDAQ")
        usd_to_base_rate = self._usd_to_base_rate()
        series_by_ticker: dict[str, tuple[StockSpec, list[float]]] = {}
        momentum_score: dict[str, float] = {}
        for spec in all_specs.values():
            if spec.ticker in self._buy_blocklist and spec.ticker not in self.portfolio.positions:
                continue
            try:
                series = self.data_client.get_close_prices(spec.yahoo_symbol)
                close = series.close.astype(float)
                series_by_ticker[spec.ticker] = (spec, close.tolist())
                last = float(close[-1])
                back = float(close[-min(8, close.size)])
                momentum_score[spec.ticker] = (last - back) / back if back > 0 else 0.0
                prices[spec.ticker] = self._price_in_base(spec.ticker, last, usd_to_base_rate)
            except Exception as exc:
                logger.warning("Skipping %s forecast due to data/model error: %s", spec.ticker, exc)

        held = set(self.portfolio.positions.keys())
        ranked = sorted(momentum_score.items(), key=lambda x: abs(x[1]), reverse=True)
        shortlist: list[str] = []
        for sym in held:
            if sym in series_by_ticker and sym not in shortlist:
                shortlist.append(sym)
        for sym, _ in ranked:
            if sym not in shortlist:
                shortlist.append(sym)
            if len(shortlist) >= self.settings.max_symbols_per_cycle:
                break

        for symbol in shortlist:
            if symbol in self._buy_blocklist and symbol not in held:
                continue
            spec, close_list = series_by_ticker[symbol]
            close = close_list
            np_close = np.array(close, dtype=float)
            f = self.forecaster.predict_direction(np_close, self.settings.forecast_horizon)
            raw_last = float(close[-1])
            last = self._price_in_base(spec.ticker, raw_last, usd_to_base_rate)
            if not np.isfinite(last) or last <= 0.0:
                logger.warning("Skipping %s because price is invalid: %s", symbol, last)
                continue
            if symbol in held:
                # For held names, use model-based short-horizon downside checks.
                near_f = self.forecaster.predict_direction(np_close, max(1, self.settings.forecast_horizon // 2))
                near_term_return = near_f.expected_return
                near_term_confidence = near_f.confidence
            else:
                near_steps = max(2, min(6, len(close) // 16))
                near_anchor = float(close[-near_steps])
                near_term_return = (last - near_anchor) / near_anchor if near_anchor > 0 else 0.0
                near_term_confidence = min(1.0, abs(near_term_return) * 12.0)
            forecasts.append(
                CandidateForecast(
                    symbol=spec.ticker,
                    expected_return=f.expected_return,
                    near_term_return=near_term_return,
                    confidence=f.confidence,
                    near_term_confidence=near_term_confidence,
                    last_price=last,
                    provider=f.provider,
                )
            )
        forecasts.sort(key=lambda x: x.expected_return, reverse=True)
        return forecasts, prices

    def _symbol_to_exchange(self) -> dict[str, str]:
        return {s.ticker: s.exchange for s in self.specs}

    def run_cycle(self, progress: Callable[[CycleReport], None] | None = None) -> CycleReport:
        try:
            self.broker.sync()
        except Exception as exc:
            logger.warning("Broker sync failed; using last known portfolio state: %s", exc)
        proxy = self.data_client.get_close_prices(self.settings.oil_proxy_symbol)
        forecasts, current_prices = self._forecast_candidates()
        if not forecasts:
            raise RuntimeError("No forecast candidates available")

        equity = self.portfolio.market_value(current_prices)
        budget = equity * self.settings.risk_dial
        oil_last = float(proxy.close[-1])
        top = forecasts[0]
        quote_a = forecasts[0].symbol
        quote_b = forecasts[1].symbol if len(forecasts) > 1 else forecasts[0].symbol

        receipts: list[TradeReceipt] = []
        if progress:
            progress(
                self._base_report(
                    forecast=top,
                    decision=StrategyDecision(
                        target_symbol="",
                        action="hold",
                        reason="Cycle running: evaluating sell opportunities",
                    ),
                    equity=equity,
                    budget=budget,
                    current_prices=current_prices,
                    quote_symbol_a=quote_a,
                    quote_symbol_b=quote_b,
                    oil_last=oil_last,
                    receipts=[],
                    message="Cycle running: evaluating positions",
                )
            )
        sold_any = False
        for symbol, pos in list(self.portfolio.positions.items()):
            match = next((f for f in forecasts if f.symbol == symbol), None)
            if not match:
                continue
            current = current_prices.get(symbol, pos.avg_price)
            unrealized = (current - pos.avg_price) * pos.shares
            unrealized_pct = (current - pos.avg_price) / pos.avg_price if pos.avg_price > 0 else 0.0
            if self.strategy.should_sell(match, unrealized, unrealized_pct):
                sold = self.broker.sell_all(symbol, current_prices.get(symbol, pos.avg_price))
                if sold:
                    receipts.append(sold)
                    sold_any = True

        decision = self.strategy.pick_buy(forecasts, budget)
        if len(self.portfolio.positions) >= self.settings.max_active_positions:
            decision = StrategyDecision(
                target_symbol="",
                action="hold",
                reason=(
                    f"Max active positions reached ({len(self.portfolio.positions)}/"
                    f"{self.settings.max_active_positions}); no new buys"
                ),
            )
        elif decision.action == "buy":
            buy_candidates = sorted(forecasts, key=lambda f: f.expected_return, reverse=True)
            for candidate in buy_candidates:
                if candidate.symbol in self.portfolio.positions or candidate.symbol in self._buy_blocklist:
                    continue
                candidate_decision = self.strategy.pick_buy([candidate], budget)
                if candidate_decision.action != "buy":
                    continue
                target_symbol = candidate.symbol
                target_price = current_prices[target_symbol]
                shares_est = int(max(0.0, min(self.portfolio.cash, budget) - self.settings.trade_fee) // target_price)
                expected_profit = shares_est * target_price * candidate.expected_return
                summary_payload = {
                    "symbol": target_symbol,
                    "action": "BUY",
                    "exchange": self._symbol_to_exchange().get(target_symbol, "NASDAQ"),
                    "shares_estimate": shares_est,
                    "price": round(target_price, 4),
                    "expected_return": round(candidate.expected_return, 6),
                    "expected_profit_dollars": round(expected_profit, 2),
                    "budget": round(budget, 2),
                    "trade_fee": self.settings.trade_fee,
                    "forecasts": [
                        {
                            "symbol": f.symbol,
                            "expected_return": round(f.expected_return, 6),
                            "near_term_return": round(f.near_term_return, 6),
                            "confidence": round(f.confidence, 3),
                            "near_term_confidence": round(f.near_term_confidence, 3),
                            "last_price": round(f.last_price, 4),
                        }
                        for f in forecasts[:5]
                    ],
                }
                summary, explain = self.advisor.summarize_decision(summary_payload)
                pretty_summary = (
                    f"📈 *StockRock Trade Proposal*\n"
                    f"*Action:* BUY `{target_symbol}` ({self._symbol_to_exchange().get(target_symbol, 'NASDAQ')})\n"
                    f"*Expected return:* `{candidate.expected_return*100:.2f}%`\n"
                    f"*Confidence:* `{candidate.confidence*100:.1f}%`\n"
                    f"*Planned budget:* `{budget:.2f}`\n"
                    f"*Fee:* `{self.settings.trade_fee:.2f}`\n\n"
                    f"{summary[:2200]}"
                )
                approved = True
                if self.settings.telegram_require_approval:
                    if progress:
                        progress(
                            self._base_report(
                                forecast=candidate,
                                decision=StrategyDecision(
                                    target_symbol=target_symbol,
                                    action="buy",
                                    reason="Awaiting Telegram approval for trade execution",
                                ),
                                equity=self.portfolio.market_value(current_prices),
                                budget=budget,
                                current_prices=current_prices,
                                quote_symbol_a=quote_a,
                                quote_symbol_b=quote_b,
                                oil_last=oil_last,
                                receipts=receipts,
                                message=f"Awaiting approval: BUY {target_symbol}",
                            )
                        )
                    approved = self.notify.request_approval(
                        summary=pretty_summary,
                        explain=explain,
                        timeout_sec=self.settings.telegram_approval_timeout_sec,
                    )
                else:
                    self.notify.send(f"🤖 *Auto mode*: executing without approval.\n\n{pretty_summary}", markdown=True)
                if not approved:
                    decision = StrategyDecision(target_symbol="", action="hold", reason="User rejected trade in Telegram")
                    break
                bought = self.broker.buy(target_symbol, target_price, budget=max(budget, self.settings.min_position_dollars))
                if bought:
                    receipts.append(bought)
                    decision = StrategyDecision(
                        target_symbol=target_symbol,
                        action="buy",
                        reason=(
                            f"{target_symbol} selected ({candidate.expected_return:.4f} expected return, "
                            f"conf {candidate.confidence:.2f})"
                        ),
                    )
                    break
                self._buy_blocklist.add(target_symbol)
                decision = StrategyDecision(
                    target_symbol="",
                    action="hold",
                    reason=f"Buy execution failed for {target_symbol} (symbol blocked this session)",
                )

        action_label = "TRADE" if receipts else "HOLD"
        lines = [f"{action_label}", f"reason={decision.reason}", f"top={top.symbol} {top.expected_return:.4f} ({top.provider})"]
        if sold_any and not receipts:
            lines.append("sold positions but no new buy")
        for receipt in receipts:
            lines.append(
                f"{receipt.side.upper()} {receipt.symbol} qty={receipt.shares} "
                f"price={receipt.price:.2f} value={receipt.gross_or_cost:.2f}"
            )
        lines.append(f"cash={self.portfolio.cash:.2f}")
        message = " | ".join(lines)
        report = self._base_report(
            forecast=top,
            decision=decision,
            equity=self.portfolio.market_value(current_prices),
            budget=budget,
            current_prices=current_prices,
            quote_symbol_a=quote_a,
            quote_symbol_b=quote_b,
            oil_last=oil_last,
            receipts=receipts,
            message=message,
        )
        logger.info(message)
        self.notify.send(message)
        return report
