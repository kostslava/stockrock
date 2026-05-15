"""Multi-worker ("swarm") runtime for ``--spam`` mode.

The single-engine spam loop is bottlenecked by InvestJA's per-request
latency: every BUY/SELL is a Playwright navigation + form submission. With
500 candidate tickers and ~1.5–3s per trade, a single worker takes minutes
to walk the universe. ``SpamSwarm`` runs N independent ``SpamEngine``
workers in parallel — each with its own Playwright browser, its own broker,
its own slice of the universe — so the wall-clock cost is roughly divided
by N.

What is shared and what is not
------------------------------
Shared (with thread-safe access):
  * ``DailySpamState`` — single JSON file on disk. Mutations are guarded by
    an ``RLock``; disk writes are **debounced** so parallel workers do not
    serialize on ``write_text`` after every trade.
  * ``SwarmController`` — single source of truth for the trade mode
    (``buy`` / ``sell`` / ``combo``). Arrow keys update the controller; all
    engines read from it through ``set_mode_source``.

Not shared (one per worker):
  * ``InvestJAWebClient`` / Playwright session. ``sync_playwright`` is *not*
    thread-safe — each browser must be created and used from a single
    thread, which is exactly how the worker thread owns its client.
  * ``InvestJABroker`` + ``PortfolioState``. Every worker reads the same
    remote account on ``sync()``, but its in-memory snapshot is private. The
    universe is sliced so workers never trade the same ticker, eliminating
    write conflicts on positions.
  * ``SpamEngine`` — purely per-worker state (price cache, FX cache,
    unpriceable set, log-suppress sets). Each worker also gets its own
    ``YahooDataClient`` so five passes pricing in parallel do not contend on
    a single client.

Slicing strategy
----------------
We stride the price-sorted spec list (``specs[i::N]``) so each worker gets a
balanced mix of cheap and mid-priced names. A single hot worker stuck on an
expensive slice would defeat the parallelism.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from stockrock.broker import InvestJABroker, PaperBroker
from stockrock.config import Settings
from stockrock.data import YahooDataClient
from stockrock.notifier import TelegramNotifier
from stockrock.portfolio import PortfolioState
from stockrock.spam import DailySpamState, SpamEngine, SpamPassSummary, SpamTradeMode
from stockrock.universe import StockSpec

logger = logging.getLogger(__name__)


DEFAULT_WORKERS = 1


class SwarmController:
    """Single source of truth for the swarm-wide trade mode (BUY / SELL /
    COMBO). All worker ``SpamEngine`` instances read from this controller so
    one arrow-key press changes every worker on the next loop iteration."""

    def __init__(self, mode: SpamTradeMode = "combo") -> None:
        self._lock = threading.Lock()
        self._mode: SpamTradeMode = mode

    def get_mode(self) -> SpamTradeMode:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> None:
        m = (mode or "").strip().lower()
        if m not in ("buy", "sell", "combo"):
            return
        with self._lock:
            if self._mode == m:
                return
            self._mode = m  # type: ignore[assignment]
        logger.info("Swarm trade mode → %s (broadcast to all workers)", m.upper())


@dataclass
class SwarmWorker:
    name: str
    engine: SpamEngine
    specs: list[StockSpec]
    ui: object | None = None  # per-worker dashboard proxy
    thread: Optional[threading.Thread] = field(default=None, repr=False)
    last_summary: Optional[SpamPassSummary] = field(default=None, repr=False)


def slice_universe(specs: list[StockSpec], num_workers: int) -> list[list[StockSpec]]:
    """Stride-split ``specs`` across ``num_workers`` slices.

    Stride (``specs[i::N]``) keeps each slice a balanced sample of the
    universe. If we used contiguous chunks the cheapest worker would finish
    fast and the priciest worker would dominate runtime, which is exactly
    the imbalance ``SpamSwarm`` is meant to fix.
    """
    n = max(1, int(num_workers))
    slices: list[list[StockSpec]] = [[] for _ in range(n)]
    for i, spec in enumerate(specs):
        slices[i % n].append(spec)
    return slices


class SpamSwarm:
    """Owns N worker threads, each running an isolated ``SpamEngine``."""

    def __init__(
        self,
        *,
        settings: Settings,
        specs: list[StockSpec],
        num_workers: int = DEFAULT_WORKERS,
        controller: Optional[SwarmController] = None,
        notifier: Optional[TelegramNotifier] = None,
        state_path=None,
    ) -> None:
        if num_workers < 1:
            raise ValueError("num_workers must be >= 1")
        if not specs:
            raise ValueError("specs must not be empty")
        self.settings = settings
        self.num_workers = num_workers
        self.controller = controller or SwarmController()
        self.notifier = notifier
        self.shared_state = (
            DailySpamState.load(state_path) if state_path is not None else DailySpamState.load()
        )
        self._stop_event = threading.Event()

        slices = slice_universe(specs, num_workers)
        # Drop empty slices so we never spawn a worker with nothing to do.
        slices = [sl for sl in slices if sl]
        if len(slices) != num_workers:
            logger.info(
                "Swarm: universe too small for %d workers; running %d",
                num_workers, len(slices),
            )
        self.workers: list[SwarmWorker] = []
        for i, slice_specs in enumerate(slices):
            engine = self._build_engine(slice_specs)
            engine.set_mode_source(self.controller.get_mode)
            self.workers.append(
                SwarmWorker(name=f"W{i + 1}", engine=engine, specs=slice_specs)
            )

        # One Playwright browser per worker — stagger logins slightly to
        # spread CPU spikes. Keep this small so workers still overlap hard.
        self._login_stagger_s = 0.0

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    def attach_ui(self, ui_for_worker: Callable[[SwarmWorker], object]) -> None:
        """Attach a per-worker UI proxy (created by ``SwarmDashboard``)."""
        for w in self.workers:
            w.ui = ui_for_worker(w)

    def _build_engine(self, slice_specs: list[StockSpec]) -> SpamEngine:
        # Each worker gets its own broker (and thus its own Playwright
        # session — required because sync_playwright is single-thread).
        portfolio = PortfolioState(cash=self.settings.initial_cash, trade_fee=self.settings.trade_fee)
        exchange_map = {s.ticker: s.exchange for s in slice_specs}
        if self.settings.broker_mode == "investja":
            broker = InvestJABroker(
                portfolio,
                username=self.settings.investja_username,
                password=self.settings.investja_password,
                exchange_map=exchange_map,
                min_gross_notional=0.0,
                max_loan=self.settings.investja_loan_cap,
            )
        else:
            broker = PaperBroker(portfolio, min_gross_notional=0.0)
        return SpamEngine(
            settings=self.settings,
            data_client=YahooDataClient(),
            broker=broker,
            portfolio=portfolio,
            specs=slice_specs,
            state=self.shared_state,
            notifier=self.notifier,
        )

    # Lifecycle -------------------------------------------------------------
    def start(self) -> None:
        for i, w in enumerate(self.workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"spam-{w.name}",
                args=(w, i * self._login_stagger_s),
                daemon=True,
            )
            w.thread = t
            t.start()

    def shutdown(self, *, timeout: float = 10.0) -> None:
        self._stop_event.set()
        for w in self.workers:
            t = w.thread
            if t is None or not t.is_alive():
                continue
            t.join(timeout=timeout)
            if t.is_alive():
                logger.warning("Swarm: worker %s did not stop within %.1fs", w.name, timeout)

        try:
            self.shared_state.flush_now()
        except Exception as exc:
            logger.warning("Swarm: final spam state flush failed: %s", exc)

        # Close every Playwright client from a *single* thread (the calling
        # one). Each client was created in its worker thread, but closing a
        # browser from another thread is allowed by Playwright since we wait
        # for the worker thread to fully exit before we touch the client.
        for w in self.workers:
            client = getattr(getattr(w.engine, "broker", None), "client", None)
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    logger.warning("Swarm: %s close() failed: %s", w.name, exc)

    # Worker loop -----------------------------------------------------------
    def _worker_loop(self, worker: SwarmWorker, login_delay: float) -> None:
        # Staggered login: each worker waits its slot before first sync.
        if login_delay > 0:
            slept = 0.0
            while slept < login_delay:
                if self._stop_event.is_set():
                    return
                time.sleep(min(0.25, login_delay - slept))
                slept += 0.25

        pass_no = 0
        poll_seconds = max(1.0, float(self.settings.poll_seconds))
        ui = worker.ui
        while not self._stop_event.is_set():
            pass_no += 1
            try:
                summary = worker.engine.run_pass(pass_no=pass_no, ui=ui)
                worker.last_summary = summary
            except Exception:
                logger.exception("Swarm: %s pass #%d failed", worker.name, pass_no)
            if self._stop_event.is_set():
                break
            # Sleep in small slices so shutdown is responsive.
            slept = 0.0
            while slept < poll_seconds and not self._stop_event.is_set():
                time.sleep(min(0.25, poll_seconds - slept))
                slept += 0.25
