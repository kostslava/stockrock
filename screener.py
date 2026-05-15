"""Yahoo Finance screener: every NYSE/NASDAQ/TSX stock priced $3–$20 CAD
the screener will hand us. No artificial row cap — we keep paginating each
tier until Yahoo returns an empty / short page.

Run with: python screener.py
Output:   stocks.csv

The CSV is consumed by the trading bot as its tradable universe (see
stockrock/universe.py::load_universe_from_csv).
"""

from __future__ import annotations

import csv
import logging
import sys
import time
from typing import Any

import yfinance as yf

logger = logging.getLogger(__name__)

# CAD/USD screening rate. Close enough — the universe is rebuilt periodically.
USD_PER_CAD = 0.73

# Run three non-overlapping CAD tiers per region so cheap names don't crowd
# out higher-priced liquidity ($3–$20 CAD).
TIER_CHEAP_CAD = (3.0, 7.0)
TIER_MID_CAD = (7.0, 12.0)
TIER_UPPER_CAD = (12.0, 20.0)


def _cad_to_usd_band(low_cad: float, high_cad: float) -> tuple[float, float]:
    return (round(low_cad * USD_PER_CAD, 2), round(high_cad * USD_PER_CAD, 2))

# Yahoo exchange codes per target market. yfinance's `exchange` filter is
# unreliable for these, so we filter the response client-side.
NYSE_CODES = {"NYQ", "NYS"}
NASDAQ_CODES = {"NAS", "NGM", "NCM"}
TSX_CODES = {"TOR"}

PAGE_SIZE = 250
# Hard ceiling on pages per tier query — defends against an unbounded loop
# if Yahoo ever returns a full page indefinitely. Real tier queries terminate
# long before this via the "short page" exit.
MAX_PAGES_PER_GROUP = 80


def _build_query(region: str, lo: float, hi: float) -> yf.EquityQuery:
    return yf.EquityQuery(
        "and",
        [
            yf.EquityQuery("gt", ["eodprice", lo]),
            yf.EquityQuery("lt", ["eodprice", hi]),
            yf.EquityQuery("eq", ["region", region]),
        ],
    )


def _fetch_page(query: yf.EquityQuery, offset: int, size: int) -> list[dict[str, Any]]:
    # yfinance >=1.x exposes a top-level `screen()` helper that returns the
    # response dict directly (vs the older `Screener` class). We pass an
    # EquityQuery and let yfinance build the body / handle the crumb.
    response = yf.screen(
        query,
        offset=offset,
        size=size,
        sortField="eodprice",
        sortAsc=True,
        userId="",
        userIdType="guid",
    ) or {}
    return list(response.get("quotes") or [])


def _fetch_group(label: str, query: yf.EquityQuery, allowed_codes: set[str]) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    for page_idx in range(MAX_PAGES_PER_GROUP):
        offset = page_idx * PAGE_SIZE
        try:
            page = _fetch_page(query, offset=offset, size=PAGE_SIZE)
        except Exception as exc:
            logger.warning("%s: screener page offset=%s failed: %s", label, offset, exc)
            break
        if not page:
            break
        raw.extend(page)
        if len(page) < PAGE_SIZE:
            break
        time.sleep(1.0)

    filtered = [q for q in raw if str(q.get("exchange", "")).upper() in allowed_codes]
    logger.info("%s: %s raw quotes, %s after exchange filter", label, len(raw), len(filtered))
    return filtered


def _normalize_row(q: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": q.get("symbol", ""),
        "shortName": q.get("shortName") or q.get("longName") or "",
        "regularMarketPrice": q.get("regularMarketPrice", ""),
        "currency": q.get("currency", ""),
        "exchange": q.get("exchange", ""),
        "sector": q.get("sector", ""),
        "marketCap": q.get("marketCap", ""),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    # Build per-tier queries. USD tiers are CAD tiers rescaled.
    tiers = []
    for tier_name, (lo_cad, hi_cad) in [
        ("cheap", TIER_CHEAP_CAD),
        ("mid", TIER_MID_CAD),
        ("upper", TIER_UPPER_CAD),
    ]:
        lo_usd, hi_usd = _cad_to_usd_band(lo_cad, hi_cad)
        tiers.append((tier_name, _build_query("us", lo_usd, hi_usd), _build_query("ca", lo_cad, hi_cad)))

    # 3 tiers × 3 regions = 9 independent queries.
    groups: list[tuple[str, "yf.EquityQuery", set[str]]] = []
    for tier_name, us_q, ca_q in tiers:
        groups.append((f"NYSE-{tier_name}", us_q, NYSE_CODES))
        groups.append((f"NASDAQ-{tier_name}", us_q, NASDAQ_CODES))
        groups.append((f"TSX-{tier_name}", ca_q, TSX_CODES))

    merged: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for label, query, codes in groups:
        rows = _fetch_group(label, query, codes)
        kept = 0
        for q in rows:
            sym = (q.get("symbol") or "").strip()
            if not sym:
                continue
            # Dedupe; first-write-wins keeps the cheapest entry since results
            # are sorted ascending by eodprice and we iterate in group order.
            if sym in merged:
                continue
            merged[sym] = _normalize_row(q)
            kept += 1
        counts[label] = kept

    rows_out = list(merged.values())
    fieldnames = ["symbol", "shortName", "regularMarketPrice", "currency", "exchange", "sector", "marketCap"]
    with open("stocks.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)

    logger.info("wrote stocks.csv with %s rows (no cap)", len(rows_out))
    for label, n in counts.items():
        logger.info("  %s: %s rows", label, n)

    return 0


if __name__ == "__main__":
    sys.exit(main())
