from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StockSpec:
    ticker: str
    yahoo_symbol: str
    exchange: str
    # From ``screener.py`` CSV: avoids a Yahoo round-trip per symbol in spam
    # ranking when both are set (huge for 2000+ names).
    screened_price: float | None = None
    screened_currency: str | None = None


def normalize_exchange(exchange: str) -> str:
    ex = (exchange or "").strip().upper()
    if ex in {"NASDAQ", "NYSE", "TSX"}:
        return ex
    return "NASDAQ"


# Map Yahoo's per-listing exchange codes to our canonical exchange names.
# These are what `screener.py` writes into the `exchange` column.
_YAHOO_EXCHANGE_CODE_TO_NAME = {
    "NYQ": "NYSE",
    "NYS": "NYSE",
    "NMS": "NASDAQ",  # NASDAQ Global Select
    "NGM": "NASDAQ",  # NASDAQ Global Market
    "NCM": "NASDAQ",  # NASDAQ Capital Market
    "NAS": "NASDAQ",
    "TOR": "TSX",
}


def _exchange_from_csv(raw: str) -> str | None:
    """Translate a Yahoo exchange code (or canonical name) to our enum."""
    ex = (raw or "").strip().upper()
    if not ex:
        return None
    if ex in {"NASDAQ", "NYSE", "TSX"}:
        return ex
    return _YAHOO_EXCHANGE_CODE_TO_NAME.get(ex)


def parse_universe(raw: str) -> list[StockSpec]:
    """
    Format: TICKER|YAHOO_SYMBOL|EXCHANGE, ...
    Example: SNDL|SNDL|NASDAQ,ACB|ACB|NASDAQ
    """
    result: list[StockSpec] = []
    for chunk in raw.split(","):
        part = chunk.strip()
        if not part:
            continue
        pieces = [p.strip() for p in part.split("|")]
        if len(pieces) != 3:
            continue
        result.append(
            StockSpec(
                ticker=pieces[0].upper(),
                yahoo_symbol=pieces[1],
                exchange=normalize_exchange(pieces[2]),
                screened_price=None,
                screened_currency=None,
            )
        )
    return result


def load_universe_from_csv(path: str | Path) -> list[StockSpec]:
    """Load a tradable universe from the CSV produced by ``screener.py``.

    Expected columns: symbol, shortName, regularMarketPrice, currency,
    exchange, sector, marketCap. TSX symbols on Yahoo carry a ``.TO`` suffix
    which is preserved as the ``yahoo_symbol`` while the platform ticker is
    the bare base (e.g. ``RY.TO`` -> ticker ``RY``, yahoo ``RY.TO``).
    """
    p = Path(path)
    if not p.exists():
        return []
    specs: list[StockSpec] = []
    seen: set[str] = set()
    with p.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yahoo_symbol = (row.get("symbol") or "").strip()
            exchange = _exchange_from_csv(row.get("exchange", ""))
            if not yahoo_symbol or exchange is None:
                continue
            screened_price: float | None = None
            screened_currency: str | None = None
            raw_px = (row.get("regularMarketPrice") or "").strip()
            if raw_px:
                try:
                    screened_price = float(raw_px)
                except ValueError:
                    screened_price = None
            cur = (row.get("currency") or "").strip().upper()
            if cur:
                screened_currency = cur
            # InvestJA buys/sells by the displayed ticker, not the Yahoo
            # variant. Drop the ``.TO`` (and similar) suffix for the ticker.
            ticker = yahoo_symbol.split(".")[0].upper()
            if ticker in seen:
                continue
            seen.add(ticker)
            specs.append(
                StockSpec(
                    ticker=ticker,
                    yahoo_symbol=yahoo_symbol,
                    exchange=exchange,
                    screened_price=screened_price,
                    screened_currency=screened_currency,
                )
            )
    return specs
