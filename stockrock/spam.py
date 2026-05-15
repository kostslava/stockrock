"""Spam (a.k.a. "goon") mode: drain account balance via maximum-fee trades.

This module implements the inverse of the normal StockRock strategy. It is
used only when invoked via `--spam` on the CLI. It does not depend on the
forecaster, advisor, or approval flow.

InvestJA permits up to 3 transactions per stock per day. Per stock per day,
this engine executes a 3-trade sequence that ends as close to flat as
possible:
  - if currently holding the stock: SELL, BUY, SELL  (ends 0 shares)
  - if currently flat:               BUY,  SELL, BUY (ends +1 share)
Either pattern burns ``3 * TRADE_FEE`` per stock per day.

Tickers are processed cheapest-first so the engine can keep trading 1-share
lots once cash gets thin.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Literal, Optional, cast

SpamTradeMode = Literal["buy", "sell", "combo"]

from stockrock.broker import Broker, TradeReceipt
from stockrock.config import Settings
from stockrock.data import YahooDataClient
from stockrock.investja import SellHoldError
from stockrock.notifier import TelegramNotifier
from stockrock.portfolio import PortfolioState
from stockrock.universe import StockSpec

logger = logging.getLogger(__name__)


DEFAULT_STATE_PATH = Path.home() / ".stockrock" / "spam_state.json"
MAX_DAILY_TXNS_PER_SYMBOL = 3


def _inter_trade_sleep_sec() -> float:
    raw = (os.environ.get("SPAM_INTER_TRADE_SLEEP_SEC") or "0").strip() or "0"
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


INTER_TRADE_SLEEP_SEC = _inter_trade_sleep_sec()
MAX_CONSECUTIVE_FAILS_PER_TICKER = 2
# After this many consecutive failed trade attempts on the same side, give up
# on retrying that side and flip to the other one (instead of looping on
# unsellable / unbuyable candidates forever).
MAX_CONSECUTIVE_SIDE_FAILS = 5

# Cache Yahoo last-price lookups for this long. Spam still hits Yahoo for
# symbols without CSV ``screened_price`` (e.g. env STOCK_UNIVERSE).
PRICE_CACHE_TTL_SEC = 180.0

# InvestJA refuses trades on symbols priced at or below ~$2 CAD (~$1.50 USD).
# Price is computed in the configured BASE_CURRENCY (CAD by default).
MIN_TRADE_PRICE = 2.0

# InvestJA requires 1 hour to elapse between BUY and SELL of the same ticker.
HOLD_SECONDS_AFTER_BUY = 3600
# After a *successful* SELL we historically debounced re-sells; failed sells
# must not lock the ticker for an hour. Set SPAM_SELL_ATTEMPT_COOLDOWN_SEC>0 to
# restore a cooldown (seconds) between sell attempts on the same symbol.
def _sell_attempt_cooldown_sec() -> float:
    raw = (os.environ.get("SPAM_SELL_ATTEMPT_COOLDOWN_SEC") or "0").strip() or "0"
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


SELL_ATTEMPT_COOLDOWN_SEC = _sell_attempt_cooldown_sec()

NETWORK_ERROR_MARKERS = (
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET",
    "ERR_NETWORK_CHANGED",
    "ERR_PROXY_CONNECTION_FAILED",
    "net::ERR_",
)


def _looks_like_network_error(text: str) -> bool:
    if not text:
        return False
    upper = text.upper()
    return any(marker in upper for marker in NETWORK_ERROR_MARKERS)


@dataclass
class DailySpamState:
    """Persisted (per-day, single JSON file) state for the spam loop.

    Tracks:
      * ``counts`` — transactions executed per ticker today (caps at 3).
      * ``last_buy`` — timestamp of the bot's last successful BUY per ticker.
        Drives the 1-hour post-BUY hold check before a SELL.
      * ``last_sell_attempt`` — optional debounce between SELL attempts on the
        same ticker when ``SPAM_SELL_ATTEMPT_COOLDOWN_SEC`` is set (>0). Default
        is 0 (no sell-attempt cooldown). When enabled, timestamps live in the
        same JSON file across restarts.
    """

    state_date: str
    counts: dict[str, int] = field(default_factory=dict)
    last_buy: dict[str, float] = field(default_factory=dict)
    last_sell_attempt: dict[str, float] = field(default_factory=dict)
    path: Path = DEFAULT_STATE_PATH
    # Lock guards in-memory dict mutations and the JSON snapshot used for
    # disk writes. Disk I/O is debounced so five swarm workers do not
    # serialize on ``write_text`` after every single trade.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    _timer_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _save_timer: Optional[threading.Timer] = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path = DEFAULT_STATE_PATH) -> "DailySpamState":
        today = date.today().isoformat()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if data.get("date") == today:
                    return cls(
                        state_date=today,
                        counts=dict(data.get("counts", {})),
                        last_buy={k: float(v) for k, v in data.get("last_buy", {}).items()},
                        last_sell_attempt={
                            k: float(v) for k, v in data.get("last_sell_attempt", {}).items()
                        },
                        path=path,
                    )
            except Exception as exc:
                logger.warning("Could not parse spam state at %s (%s); starting fresh", path, exc)
        return cls(state_date=today, counts={}, last_buy={}, last_sell_attempt={}, path=path)

    def _write_file_under_lock(self) -> None:
        """Must be called with ``self._lock`` held."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "date": self.state_date,
                    "counts": self.counts,
                    "last_buy": self.last_buy,
                    "last_sell_attempt": self.last_sell_attempt,
                },
                indent=2,
            )
        )

    def save(self) -> None:
        """Immediate flush to disk (blocks). Prefer ``_schedule_save`` on hot paths."""
        with self._lock:
            self._write_file_under_lock()

    def _schedule_save(self) -> None:
        """Coalesce rapid writes from parallel swarm workers into one disk flush."""

        def _flush() -> None:
            try:
                with self._lock:
                    self._write_file_under_lock()
            finally:
                with self._timer_lock:
                    self._save_timer = None

        with self._timer_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            t = threading.Timer(0.18, _flush)
            t.daemon = True
            self._save_timer = t
            t.start()

    def flush_now(self) -> None:
        """Cancel any pending debounced save and persist immediately. Call on
        process shutdown so no in-memory updates are lost."""
        with self._timer_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
        self.save()

    def used(self, ticker: str) -> int:
        with self._lock:
            return self.counts.get(ticker.upper(), 0)

    def remaining(self, ticker: str) -> int:
        return max(0, MAX_DAILY_TXNS_PER_SYMBOL - self.used(ticker))

    def record(self, ticker: str) -> None:
        key = ticker.upper()
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + 1
        self._schedule_save()

    def record_buy(self, ticker: str, now: float | None = None) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self.last_buy[ticker.upper()] = ts
        self._schedule_save()

    def record_sell_attempt(self, ticker: str, now: float | None = None) -> None:
        """Persist the last SELL attempt time for optional per-ticker cooldown."""
        ts = float(now if now is not None else time.time())
        with self._lock:
            self.last_sell_attempt[ticker.upper()] = ts
        self._schedule_save()

    def hold_remaining(self, ticker: str, hold_seconds: float = HOLD_SECONDS_AFTER_BUY) -> float:
        with self._lock:
            ts = self.last_buy.get(ticker.upper())
        if ts is None:
            return 0.0
        return max(0.0, hold_seconds - (time.time() - ts))

    def in_hold(self, ticker: str, hold_seconds: float = HOLD_SECONDS_AFTER_BUY) -> bool:
        return self.hold_remaining(ticker, hold_seconds) > 0.0

    def sell_cooldown_remaining(
        self, ticker: str, cooldown_seconds: float = SELL_ATTEMPT_COOLDOWN_SEC
    ) -> float:
        if cooldown_seconds <= 0:
            return 0.0
        with self._lock:
            ts = self.last_sell_attempt.get(ticker.upper())
        if ts is None:
            return 0.0
        return max(0.0, cooldown_seconds - (time.time() - ts))

    def in_sell_cooldown(
        self, ticker: str, cooldown_seconds: float = SELL_ATTEMPT_COOLDOWN_SEC
    ) -> bool:
        return self.sell_cooldown_remaining(ticker, cooldown_seconds) > 0.0

    def reset_daily_txns_for_flat_buy(self, flat_tickers: list[str]) -> int:
        """Clear per-ticker daily txn counts (and buy-hold timestamps) for names
        we are not holding.

        Used in ``--spam --spam-mode buy`` so the buy queue is not stuck at
        ``0/1`` after a prior combo/sell session burned local ``counts`` for
        every symbol. Does not change sell URL / sell automation code paths.

        Returns how many tickers had a non-zero ``counts`` entry cleared.
        """
        cleared_nonzero = 0
        with self._lock:
            for raw in flat_tickers:
                k = raw.strip().upper()
                if not k:
                    continue
                prev = self.counts.get(k, 0)
                if prev:
                    cleared_nonzero += 1
                self.counts.pop(k, None)
                self.last_buy.pop(k, None)
        self._schedule_save()
        return cleared_nonzero


def _spam_investja_buy_sync_interval_sec() -> float:
    raw = (os.environ.get("SPAM_INVESTJA_BUY_SYNC_INTERVAL_SEC") or "90").strip() or "90"
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 90.0


@dataclass
class SpamTradeOutcome:
    symbol: str
    side: str
    success: bool
    shares: int
    price: float
    cash_after: float
    error: Optional[str] = None


@dataclass
class SpamPassSummary:
    run_at: str
    tickers_attempted: int
    trades_executed: int
    fees_burned: float
    starting_cash: float
    ending_cash: float
    outcomes: list[SpamTradeOutcome]


class SpamEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        data_client: YahooDataClient,
        broker: Broker,
        portfolio: PortfolioState,
        specs: list[StockSpec],
        state: DailySpamState,
        notifier: Optional[TelegramNotifier] = None,
    ) -> None:
        self.settings = settings
        self.data_client = data_client
        self.broker = broker
        self.portfolio = portfolio
        self.specs = specs
        self.state = state
        self.notifier = notifier
        self._exchange_map = {s.ticker: s.exchange for s in specs}
        self._unpriceable: set[str] = set()
        # Tickers currently being skipped due to the post-BUY hold. Used only
        # to suppress per-pass log spam — we log on entry/exit, not every pass.
        self._held_in_hold_logged: set[str] = set()
        # TTL cache: yahoo_symbol -> (ts, price). Saves ~240 network round trips
        # per pass for a universe of that size; eliminates the dominant cost of
        # the ranking step.
        self._price_cache: dict[str, tuple[float, float]] = {}
        self._fx_cache: tuple[float, float] | None = None  # (ts, rate)
        self._spam_mode_lock = threading.Lock()
        self._spam_trade_mode: SpamTradeMode = "combo"
        # Optional external source — when set, ``get_spam_trade_mode`` reads
        # from this callable (used by ``SpamSwarm`` to broadcast the active
        # mode to all worker engines via a single ``SwarmController``).
        self._mode_source: Optional[Callable[[], str]] = None
        # InvestJA ``broker.sync()`` is expensive; throttling avoids a "stuck on
        # syncing portfolio" UI while still refreshing state periodically.
        self._spam_last_sync_mono: float = 0.0

    def set_mode_source(self, source: Optional[Callable[[], str]]) -> None:
        self._mode_source = source

    def get_spam_trade_mode(self) -> SpamTradeMode:
        src = self._mode_source
        if src is not None:
            try:
                m = (src() or "").strip().lower()
                if m in ("buy", "sell", "combo"):
                    return cast(SpamTradeMode, m)
            except Exception:
                pass
        with self._spam_mode_lock:
            return self._spam_trade_mode

    def set_spam_trade_mode(self, mode: str) -> None:
        m = (mode or "").strip().lower()
        if m not in ("buy", "sell", "combo"):
            return
        with self._spam_mode_lock:
            if self._spam_trade_mode == m:
                return
            self._spam_trade_mode = cast(SpamTradeMode, m)
        logger.info("Spam trade mode → %s (←/→ or ↑/↓ in dashboard)", m.upper())

    def _cached_price(self, yahoo_symbol: str) -> float:
        now = time.time()
        hit = self._price_cache.get(yahoo_symbol)
        if hit is not None and now - hit[0] < PRICE_CACHE_TTL_SEC:
            return hit[1]
        price = self.data_client.get_last_price(yahoo_symbol)
        self._price_cache[yahoo_symbol] = (now, price)
        return price

    def _usd_to_base_rate(self) -> float:
        if self.settings.base_currency.strip().upper() != "CAD":
            return 1.0
        now = time.time()
        if self._fx_cache is not None and now - self._fx_cache[0] < PRICE_CACHE_TTL_SEC:
            return self._fx_cache[1]
        try:
            rate = self.data_client.get_last_price("USDCAD=X")
        except Exception as exc:
            logger.warning("USDCAD rate fetch failed (%s); defaulting to 1.0", exc)
            rate = 1.0
        self._fx_cache = (now, rate)
        return rate

    def _price_in_base(self, exchange: str, raw_price: float, usd_to_base: float) -> float:
        if self.settings.base_currency.strip().upper() == "CAD" and exchange in {"NASDAQ", "NYSE"}:
            return raw_price * usd_to_base
        return raw_price

    def _screened_to_base_px(self, spec: StockSpec, usd_rate: float) -> float | None:
        """Turn screener CSV ``regularMarketPrice`` into ``base_currency`` for ranking."""
        if spec.screened_price is None:
            return None
        raw = float(spec.screened_price)
        cur = (spec.screened_currency or "").strip().upper()
        base = self.settings.base_currency.strip().upper()
        # Yahoo: TSX rows are CAD; US listings are USD unless currency says CAD.
        is_cad_quote = spec.exchange == "TSX" or cur == "CAD"
        if base == "CAD":
            return raw if is_cad_quote else raw * usd_rate
        return (raw / usd_rate) if is_cad_quote and usd_rate > 1e-12 else raw

    def _rank_by_price_asc(self) -> list[tuple[StockSpec, float]]:
        usd_rate = self._usd_to_base_rate()
        priced: list[tuple[StockSpec, float]] = []
        held_syms = set(self.portfolio.positions.keys())
        for spec in self.specs:
            if spec.ticker in self._unpriceable:
                continue
            try:
                screened_px = self._screened_to_base_px(spec, usd_rate)
                if screened_px is not None:
                    px = screened_px
                else:
                    raw = self._cached_price(spec.yahoo_symbol)
                    px = self._price_in_base(spec.exchange, raw, usd_rate)
                if px <= 0:
                    self._unpriceable.add(spec.ticker)
                    continue
                # Below platform price floor: skip for new BUYs, but keep
                # priced=True for held positions so we can still sell out.
                if px <= MIN_TRADE_PRICE and spec.ticker not in held_syms:
                    logger.info(
                        "Spam: %s @ %.2f %s below floor %.2f; excluding for session",
                        spec.ticker, px, self.settings.base_currency, MIN_TRADE_PRICE,
                    )
                    self._unpriceable.add(spec.ticker)
                    continue
                priced.append((spec, px))
            except Exception as exc:
                logger.warning("Could not price %s (%s); skipping for this pass", spec.ticker, exc)
                self._unpriceable.add(spec.ticker)

        # Include any held position not already in the priced list so we can still unwind it.
        seen = {s.ticker for s, _ in priced}
        for held_sym, pos in self.portfolio.positions.items():
            if held_sym in seen:
                continue
            pseudo = StockSpec(
                ticker=held_sym,
                yahoo_symbol=held_sym,
                exchange=self._exchange_map.get(held_sym, "NASDAQ"),
            )
            priced.append((pseudo, max(pos.avg_price, 0.01)))

        priced.sort(key=lambda x: x[1])
        return priced

    def _loan_headroom(self) -> float:
        # Prefer the broker's own view if available (it tracks max_loan in
        # one place), otherwise fall back to the configured cap.
        broker_headroom = getattr(self.broker, "_loan_headroom", None)
        if callable(broker_headroom):
            return float(broker_headroom())
        cap = max(0.0, float(getattr(self.settings, "investja_loan_cap", 0.0)))
        used = float(getattr(self.broker, "last_loan_balance", 0.0))
        return max(0.0, cap - used)

    def _can_afford_buy(self, price: float) -> bool:
        # Buying power = cash + remaining loan headroom. The platform charges
        # interest on margin, which is good for the lose-the-most objective.
        needed = price + self.settings.trade_fee
        return (self.portfolio.cash + self._loan_headroom()) >= needed

    def _plan_for_ticker(self, ticker: str, remaining_caps: int, price: float) -> list[str]:
        # `broker.sell_all` clears the entire position in one transaction, and
        # `broker.buy` adds 1 share at a time in spam mode. Model that exactly
        # so we don't queue redundant sells that the broker will silently no-op.
        # Also: if price is below the platform floor we can sell out an existing
        # position but cannot buy, so stop planning once we would need a BUY.
        pos = self.portfolio.positions.get(ticker)
        holding = pos.shares if pos else 0
        can_buy = price > MIN_TRADE_PRICE
        plan: list[str] = []
        for _ in range(remaining_caps):
            if holding > 0:
                plan.append("sell")
                holding = 0
            else:
                if not can_buy:
                    break
                plan.append("buy")
                holding = 1
        return plan

    def _execute_one(self, spec: StockSpec, side: str, price: float) -> SpamTradeOutcome:
        receipt: TradeReceipt | None = None
        error: str | None = None
        try:
            if side == "buy":
                if not self._can_afford_buy(price):
                    return SpamTradeOutcome(
                        symbol=spec.ticker,
                        side="buy",
                        success=False,
                        shares=0,
                        price=price,
                        cash_after=self.portfolio.cash,
                        error="insufficient cash",
                    )
                # Budget exactly one share + fee + a small slack for intraday drift.
                budget = price + self.settings.trade_fee + max(1.0, price * 0.02)
                receipt = self.broker.buy(spec.ticker, price, budget=budget)
            else:
                receipt = self.broker.sell_all(spec.ticker, price)
        except SellHoldError as exc:
            # Platform refused: 1hr cooldown after a recent BUY. Persist the
            # timestamp so we stop attempting this ticker until it clears.
            self.state.record_buy(spec.ticker)
            error = "platform 1hr hold (recent BUY)"
            logger.info("Spam SELL on %s blocked: %s", spec.ticker, exc)
        except Exception as exc:
            error = str(exc)
            logger.warning("Spam %s on %s failed: %s", side.upper(), spec.ticker, exc)

        if receipt is not None and side == "sell" and SELL_ATTEMPT_COOLDOWN_SEC > 0:
            self.state.record_sell_attempt(spec.ticker)

        return SpamTradeOutcome(
            symbol=spec.ticker,
            side=side,
            success=receipt is not None,
            shares=receipt.shares if receipt else 0,
            price=receipt.price if receipt else price,
            cash_after=self.portfolio.cash,
            error=error,
        )

    def run_pass(
        self,
        *,
        pass_no: int = 1,
        progress: Callable[[SpamPassSummary], None] | None = None,
        ui: object | None = None,
    ) -> SpamPassSummary:
        # Optional Rich-style dashboard. The engine stays headless if no UI
        # is supplied (e.g. --plain).
        def _ui(method: str, **kwargs) -> None:
            if ui is None:
                return
            fn = getattr(ui, method, None)
            if callable(fn):
                try:
                    fn(**kwargs)
                except Exception:
                    pass

        trade_mode = self.get_spam_trade_mode()
        sync_interval = (
            _spam_investja_buy_sync_interval_sec()
            if trade_mode == "buy" and self.settings.broker_mode == "investja"
            else 20.0
        )
        now_mono = time.monotonic()
        need_sync = (
            self.settings.broker_mode != "investja"
            or self._spam_last_sync_mono == 0.0
            or (now_mono - self._spam_last_sync_mono) >= sync_interval
        )
        if need_sync:
            _ui("set_status", label="syncing portfolio…", icon="⟳")
            try:
                self.broker.sync()
                self._spam_last_sync_mono = time.monotonic()
            except Exception as exc:
                logger.warning("Spam broker sync failed; using last known state: %s", exc)
        else:
            age = int(now_mono - self._spam_last_sync_mono)
            _ui(
                "set_status",
                label=f"portfolio cache {age}s (sync every {int(sync_interval)}s)",
                icon="·",
            )

        if trade_mode == "buy":
            held = set(self.portfolio.positions.keys())
            flat = [s.ticker for s in self.specs if s.ticker not in held]
            if flat:
                cleared = self.state.reset_daily_txns_for_flat_buy(flat)
                if cleared:
                    logger.info(
                        "Spam buy mode: cleared prior local txn counts for %s flat symbols "
                        "(re-open CSV buy queue; platform may still enforce its own cap)",
                        cleared,
                    )
                else:
                    logger.debug(
                        "Spam buy mode: refreshed buy-eligibility for %s flat symbols",
                        len(flat),
                    )

        starting_cash = self.portfolio.cash
        outcomes: list[SpamTradeOutcome] = []
        trades_executed = 0

        _ui("set_status", label="pricing universe…", icon="$")
        ranked = self._rank_by_price_asc()
        # Local daily quota (3 txns/symbol) must not hide open positions: we
        # still need to attempt SELLs after the cap so the platform can accept
        # or refuse. Buys stay gated by ``remaining`` so we do not plan illegal
        # extra purchases.
        priced: list[tuple[StockSpec, float]] = []
        for spec, px in ranked:
            pos = self.portfolio.positions.get(spec.ticker)
            held = pos is not None and pos.shares > 0
            if held or self.state.remaining(spec.ticker) > 0:
                priced.append((spec, px))
        if not priced:
            _ui(
                "start_pass",
                pass_no=pass_no,
                cash=self.portfolio.cash,
                loan=float(getattr(self.broker, "last_loan_balance", 0.0)),
                loan_cap=float(getattr(self.settings, "investja_loan_cap", 0.0)),
                total_tickers=0,
                platform_fees=float(getattr(self.broker, "platform_fees_paid", 0.0)),
                holdings_value=float(getattr(self.broker, "platform_holdings_value", 0.0)),
            )
            logger.info("Spam: nothing to do (no priced symbols with remaining daily quota)")
            return SpamPassSummary(
                run_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                tickers_attempted=0,
                trades_executed=0,
                fees_burned=0.0,
                starting_cash=starting_cash,
                ending_cash=starting_cash,
                outcomes=[],
            )

        # Build interleaved BUY/SELL queue.
        # - SELL queue: every currently-held position with quota left, minus
        #   tickers in optional sell-attempt cooldown (``SPAM_SELL_ATTEMPT_COOLDOWN_SEC``;
        #   default 0 = disabled). When enabled, ``last_sell_attempt`` is persisted.
        # - BUY queue: unheld tickers with quota left, sorted cheapest-first
        #   so each buy ties up the minimum capital.
        # Then we zip them ("sell, buy, sell, buy, …") so the global trade
        # stream literally alternates BUY/SELL — covering the user's
        # "buy sell buy sell loop" requirement and keeping the fee burn rate
        # roughly symmetric between the two sides.
        held_syms = set(self.portfolio.positions.keys())
        sell_q: list[tuple[StockSpec, float]] = []
        buy_q: list[tuple[StockSpec, float]] = []
        sell_cooldown_skipped = 0
        for spec, price in priced:
            if spec.ticker in held_syms:
                if self.state.in_sell_cooldown(spec.ticker):
                    sell_cooldown_skipped += 1
                    continue
                sell_q.append((spec, price))
            else:
                if price <= MIN_TRADE_PRICE:
                    continue
                buy_q.append((spec, price))
        random.shuffle(sell_q)
        # buy_q stays in price-asc order so we tie up minimum capital per buy
        if sell_cooldown_skipped and SELL_ATTEMPT_COOLDOWN_SEC > 0:
            logger.info(
                "Spam: %s held positions skipped (SELL cooldown %.0fs)",
                sell_cooldown_skipped,
                SELL_ATTEMPT_COOLDOWN_SEC,
            )

        queue: list[tuple[str, StockSpec, float]] = []
        i = j = 0
        while i < len(sell_q) or j < len(buy_q):
            if i < len(sell_q):
                spec, price = sell_q[i]
                queue.append(("sell", spec, price))
                i += 1
            if j < len(buy_q):
                spec, price = buy_q[j]
                queue.append(("buy", spec, price))
                j += 1

        # Now that we know the real queue length, kick off the pass UI with
        # the correct total so the progress bar lands on 100% at the end.
        _ui(
            "start_pass",
            pass_no=pass_no,
            cash=self.portfolio.cash,
            loan=float(getattr(self.broker, "last_loan_balance", 0.0)),
            loan_cap=float(getattr(self.settings, "investja_loan_cap", 0.0)),
            total_tickers=max(1, len(queue)),
            platform_fees=float(getattr(self.broker, "platform_fees_paid", 0.0)),
            holdings_value=float(getattr(self.broker, "platform_holdings_value", 0.0)),
        )
        cooldown_tail = (
            f" · {sell_cooldown_skipped} sells deferred" if sell_cooldown_skipped else ""
        )
        _ui(
            "set_status",
            label=f"queue: {len(sell_q)} sells · {len(buy_q)} buys{cooldown_tail}",
            icon="≡",
        )

        tickers_attempted = 0
        consec_network_fails = 0
        sell_idx = 0
        buy_idx = 0
        # Combo: start with BUY when possible so the pass does not look
        # "sell-only" on the first stretch (that was confusing after the
        # sell-attempt cooldown work — sells were still first in line).
        turn: str = "buy" if buy_q else ("sell" if sell_q else "buy")
        consec_sell_fails = 0
        consec_buy_fails = 0

        while sell_idx < len(sell_q) or buy_idx < len(buy_q):
            mode = self.get_spam_trade_mode()
            side: str
            spec: StockSpec
            price: float

            if mode == "buy":
                if buy_idx >= len(buy_q):
                    break
                spec, price = buy_q[buy_idx]
                buy_idx += 1
                side = "buy"
            elif mode == "sell":
                if sell_idx >= len(sell_q):
                    break
                spec, price = sell_q[sell_idx]
                sell_idx += 1
                side = "sell"
            else:
                can_sell = sell_idx < len(sell_q)
                can_buy = buy_idx < len(buy_q)
                if turn == "sell" and not can_sell:
                    turn = "buy"
                elif turn == "buy" and not can_buy:
                    turn = "sell"

                if turn == "sell":
                    if not can_sell:
                        break
                    spec, price = sell_q[sell_idx]
                    sell_idx += 1
                    side = "sell"
                else:
                    if not can_buy:
                        break
                    spec, price = buy_q[buy_idx]
                    buy_idx += 1
                    side = "buy"

            # Daily quota: only BUYs are skipped when exhausted; SELLs still run
            # so every holding can be unwound (platform enforces hard limits).
            if side == "buy" and self.state.remaining(spec.ticker) <= 0:
                _ui("finish_ticker")
                continue

            # Per-side gates — these are "this attempt is a no-op", not
            # failures, so we DON'T change ``turn`` here. The next iteration
            # picks the next entry from the same queue.
            if side == "sell":
                # Cooldown was applied at queue-build, so anything reaching
                # here is fair game. We only re-check that we still actually
                # hold the position (the broker may have updated cache between
                # passes).
                if spec.ticker not in self.portfolio.positions:
                    _ui("finish_ticker")
                    continue
            else:
                if not self._can_afford_buy(price):
                    _ui(
                        "trade_skipped",
                        ticker=spec.ticker,
                        side="buy",
                        reason="insufficient buying power",
                    )
                    _ui("finish_ticker")
                    continue

            tickers_attempted += 1
            _ui("start_ticker", ticker=spec.ticker, price=price, plan=[side])
            _ui("trade_attempt", ticker=spec.ticker, side=side)
            outcome = self._execute_one(spec, side, price)
            outcomes.append(outcome)

            if outcome.success:
                self.state.record(spec.ticker)
                if side == "buy":
                    # Persisted deferred-sell ledger: next pass will see this
                    # ticker as in-hold and skip it for an hour.
                    self.state.record_buy(spec.ticker)
                trades_executed += 1
                consec_network_fails = 0
                # Successful trade clears the same-side failure streak.
                if side == "sell":
                    consec_sell_fails = 0
                else:
                    consec_buy_fails = 0
                logger.info(
                    "Spam OK %s %s qty=%s @ %.2f | cash=%.2f",
                    side.upper(), spec.ticker, outcome.shares, outcome.price, outcome.cash_after,
                )
                _ui(
                    "trade_success",
                    ticker=spec.ticker,
                    side=side,
                    shares=outcome.shares,
                    price=outcome.price,
                    cash_after=outcome.cash_after,
                    loan_after=float(getattr(self.broker, "last_loan_balance", 0.0)),
                    platform_fees=float(getattr(self.broker, "platform_fees_paid", 0.0)),
                    holdings_value=float(getattr(self.broker, "platform_holdings_value", 0.0)),
                )
                # Successful trade → flip turn so the next attempt is the
                # other side, keeping a 1:1 BUY/SELL ratio over time.
                turn = "buy" if side == "sell" else "sell"
            else:
                # Platform's 1-hour rule reported by the broker is a skip,
                # not a bug. record_buy() inside _execute_one already pushed
                # the hold timestamp, so JSON state defers this for an hour.
                if outcome.error and "1hr hold" in outcome.error:
                    _ui(
                        "trade_skipped",
                        ticker=spec.ticker,
                        side=side,
                        reason="platform 1hr cooldown after recent BUY",
                    )
                else:
                    _ui(
                        "trade_fail",
                        ticker=spec.ticker,
                        side=side,
                        reason=outcome.error or "unknown",
                    )
                # Bump the same-side failure counter; if we've burned the
                # quota, force a flip so we don't keep retrying the same
                # broken side indefinitely.
                if side == "sell":
                    consec_sell_fails += 1
                    if consec_sell_fails >= MAX_CONSECUTIVE_SIDE_FAILS:
                        logger.info(
                            "Spam: %d consecutive SELL failures; flipping to BUY",
                            consec_sell_fails,
                        )
                        consec_sell_fails = 0
                        turn = "buy"
                    # else: keep turn = "sell" to retry another candidate
                else:
                    consec_buy_fails += 1
                    if consec_buy_fails >= MAX_CONSECUTIVE_SIDE_FAILS:
                        logger.info(
                            "Spam: %d consecutive BUY failures; flipping to SELL",
                            consec_buy_fails,
                        )
                        consec_buy_fails = 0
                        turn = "sell"

                if _looks_like_network_error(outcome.error or ""):
                    consec_network_fails += 1
                    wait_s = min(60.0, 2.0 * (2 ** min(5, consec_network_fails - 1)))
                    logger.warning(
                        "Spam: network error on %s %s (#%d); sleeping %.1fs before retrying",
                        side.upper(), spec.ticker, consec_network_fails, wait_s,
                    )
                    _ui("set_status", label=f"network down · backoff {wait_s:.0f}s", icon="⚠")
                    time.sleep(wait_s)
                else:
                    consec_network_fails = 0

            _ui("finish_ticker")
            time.sleep(INTER_TRADE_SLEEP_SEC)

            if progress:
                progress(
                    SpamPassSummary(
                        run_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        tickers_attempted=tickers_attempted,
                        trades_executed=trades_executed,
                        fees_burned=trades_executed * self.settings.trade_fee,
                        starting_cash=starting_cash,
                        ending_cash=self.portfolio.cash,
                        outcomes=list(outcomes),
                    )
                )

        summary = SpamPassSummary(
            run_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            tickers_attempted=tickers_attempted,
            trades_executed=trades_executed,
            fees_burned=trades_executed * self.settings.trade_fee,
            starting_cash=starting_cash,
            ending_cash=self.portfolio.cash,
            outcomes=outcomes,
        )
        _ui("end_pass", fees_burned=summary.fees_burned)
        self._notify_summary(summary)
        return summary

    def all_quota_exhausted(self) -> bool:
        return all(self.state.remaining(s.ticker) <= 0 for s in self.specs)

    def _notify_summary(self, summary: SpamPassSummary) -> None:
        if not self.notifier:
            return
        msg = (
            f"🗑️ GOON pass complete | trades={summary.trades_executed} "
            f"fees_burned≈${summary.fees_burned:.0f} | "
            f"cash {summary.starting_cash:.2f} → {summary.ending_cash:.2f}"
        )
        with contextlib.suppress(Exception):
            self.notifier.send(msg)
