from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import yfinance as yf

from stockrock.universe import StockSpec


@dataclass(frozen=True)
class PriceSeries:
    symbol: str
    close: np.ndarray


class YahooDataClient:
    def get_close_prices(
        self,
        symbol: str,
        period: str = "6mo",
        interval: str = "1h",
        *,
        min_points: int = 20,
    ) -> PriceSeries:
        history = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True, raise_errors=True)
        if history.empty or "Close" not in history:
            raise ValueError(f"No price data for symbol: {symbol}")
        close = history["Close"].dropna().to_numpy(dtype=float)
        close = close[np.isfinite(close)]
        if close.size < min_points:
            raise ValueError(f"Insufficient data for symbol: {symbol}")
        # Guard against stale/delisted symbols that return zeros.
        if float(close[-1]) <= 0.0:
            raise ValueError(f"Invalid non-positive last price for symbol: {symbol}")
        return PriceSeries(symbol=symbol, close=close)

    def get_last_price(self, symbol: str, period: str = "5d", interval: str = "1d") -> float:
        series = self.get_close_prices(symbol=symbol, period=period, interval=interval, min_points=1)
        return float(series.close[-1])

    def is_symbol_supported(self, symbol: str) -> bool:
        try:
            # Short lookback is enough to detect invalid/delisted symbols.
            self.get_close_prices(symbol=symbol, period="1mo", interval="1d")
            return True
        except Exception:
            return False

    def filter_valid_specs(self, specs: list[StockSpec]) -> tuple[list[StockSpec], list[str]]:
        valid: list[StockSpec] = []
        invalid: list[str] = []
        for spec in specs:
            if self.is_symbol_supported(spec.yahoo_symbol):
                valid.append(spec)
            else:
                invalid.append(spec.ticker)
        return valid, invalid

    def filter_specs_above_min_price(
        self,
        specs: list[StockSpec],
        *,
        min_price: float,
        base_currency: str,
    ) -> tuple[list[StockSpec], list[str]]:
        if min_price <= 0:
            return specs, []
        normalized_base = (base_currency or "").strip().upper()
        usd_to_cad = 1.0
        if normalized_base == "CAD":
            try:
                usd_to_cad = self.get_last_price("USDCAD=X")
            except Exception:
                usd_to_cad = 1.0
        kept: list[StockSpec] = []
        dropped: list[str] = []
        for spec in specs:
            try:
                px = self.get_last_price(spec.yahoo_symbol)
                if normalized_base == "CAD" and spec.exchange in {"NASDAQ", "NYSE"}:
                    px = px * usd_to_cad
                if px > min_price:
                    kept.append(spec)
                else:
                    dropped.append(spec.ticker)
            except Exception:
                dropped.append(spec.ticker)
        return kept, dropped
