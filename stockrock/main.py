from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import warnings

from rich.console import Console
from rich.traceback import install as rich_install_traceback

from stockrock.ai import OpenAIAdvisor
from stockrock.broker import InvestJABroker, PaperBroker
from stockrock.config import load_settings
from stockrock.data import YahooDataClient
from stockrock.engine import TradingEngine
from stockrock.model import build_forecaster
from stockrock.notifier import TelegramNotifier
from stockrock.portfolio import PortfolioState
from stockrock.spam import DailySpamState, SpamEngine
from stockrock.strategy import MultiStockStrategy
from stockrock.ui import print_cycle, run_live_dashboard
from stockrock.ui_spam import (
    SpamDashboard,
    SwarmDashboard,
    start_spam_trade_mode_listener,
)
from stockrock.swarm import DEFAULT_WORKERS, SpamSwarm, SwarmController
from stockrock.universe import load_universe_from_csv, parse_universe


def _load_specs(settings) -> tuple[list, bool]:
    """Prefer the CSV produced by ``screener.py`` if present, else fall back
    to the env-defined STOCK_UNIVERSE.

    Returns ``(specs, prevalidated)``. ``prevalidated=True`` means the
    universe came from the screener CSV and has already been confirmed by
    Yahoo's own quote service with a price filter applied — callers can skip
    the per-ticker ``is_symbol_supported`` and ``filter_specs_above_min_price``
    probes, which together take 5+ minutes on a 500-symbol universe.
    """
    csv_specs = load_universe_from_csv(settings.stock_universe_csv)
    if csv_specs:
        logging.info(
            "Universe: loaded %s symbols from %s (CSV, pre-validated)",
            len(csv_specs), settings.stock_universe_csv,
        )
        return csv_specs, True
    env_specs = parse_universe(settings.stock_universe)
    logging.info(
        "Universe: %s not found; using STOCK_UNIVERSE env (%s symbols)",
        settings.stock_universe_csv, len(env_specs),
    )
    return env_specs, False


def _configure_third_party_noise() -> None:
    # Silence noisy HF unauthenticated warnings in normal bot output.
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    logging.getLogger("huggingface_hub.file_download").setLevel(logging.ERROR)
    logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
    logging.getLogger("huggingface_hub.utils._validators").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("yfinance.base").setLevel(logging.CRITICAL)
    logging.getLogger("yfinance.scrapers").setLevel(logging.CRITICAL)
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    warnings.filterwarnings("ignore", message=".*unauthenticated requests to the HF Hub.*")


def build_engine() -> TradingEngine:
    settings = load_settings()
    specs, prevalidated = _load_specs(settings)
    data_client = YahooDataClient()
    if prevalidated:
        # screener.py already used Yahoo's quote service with a price band;
        # the per-ticker probes here would just re-do that work at ~500ms each
        # and add 5+ minutes to startup with no benefit.
        valid_specs = specs
    else:
        valid_specs, invalid_tickers = data_client.filter_valid_specs(specs)
        if invalid_tickers:
            logging.warning(
                "Excluded %s Yahoo-unsupported symbols from universe: %s",
                len(invalid_tickers),
                ", ".join(invalid_tickers),
            )
        if not valid_specs:
            raise RuntimeError("No valid Yahoo symbols left in STOCK_UNIVERSE after validation")
        min_price = 2.0
        valid_specs, below_price_tickers = data_client.filter_specs_above_min_price(
            valid_specs,
            min_price=min_price,
            base_currency=settings.base_currency,
        )
        if below_price_tickers:
            logging.warning(
                "Excluded %s symbols priced at or below %.2f %s (or unavailable quote): %s",
                len(below_price_tickers),
                min_price,
                settings.base_currency.upper(),
                ", ".join(below_price_tickers),
            )
        if not valid_specs:
            raise RuntimeError("No valid Yahoo symbols above minimum price threshold")
    if settings.broker_mode == "investja":
        # InvestJA order flow is exchange-listed equities; skip likely OTC-style symbols.
        investja_specs = [
            s
            for s in valid_specs
            if (
                s.exchange in {"NASDAQ", "NYSE", "TSX"}
                and s.ticker.isascii()
                and s.ticker.replace(".", "").isalnum()
                and (
                (s.exchange in {"NASDAQ", "NYSE"} and len(s.ticker) <= 5)
                or s.exchange == "TSX"
            )
        )
    ]
    excluded_for_investja = [s.ticker for s in valid_specs if s not in investja_specs]
    if excluded_for_investja:
        logging.warning(
            "Excluded %s symbols not suitable for InvestJA execution: %s",
            len(excluded_for_investja),
            ", ".join(excluded_for_investja),
        )
    valid_specs = investja_specs
    if not valid_specs:
        raise RuntimeError("No tradable symbols left after InvestJA filtering")
    portfolio = PortfolioState(cash=settings.initial_cash, trade_fee=settings.trade_fee)
    min_gross_notional = max(
        settings.min_position_dollars,
        settings.trade_fee * settings.min_expected_profit_multiple,
    )
    broker = PaperBroker(portfolio, min_gross_notional=min_gross_notional)
    exchange_map = {s.ticker: s.exchange for s in valid_specs}
    if settings.broker_mode == "investja":
        broker = InvestJABroker(
            portfolio,
            username=settings.investja_username,
            password=settings.investja_password,
            exchange_map=exchange_map,
            min_gross_notional=min_gross_notional,
        )

    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    advisor = OpenAIAdvisor(api_key=settings.openai_api_key, model=settings.openai_model)

    return TradingEngine(
        settings=settings,
        data_client=data_client,
        forecaster=build_forecaster(settings.model_provider),
        strategy=MultiStockStrategy(
            buy_threshold=settings.buy_threshold,
            sell_threshold=settings.sell_threshold,
            min_expected_profit_multiple=settings.min_expected_profit_multiple,
            trade_fee=settings.trade_fee,
            min_take_profit_pct=settings.min_take_profit_pct,
            min_forecast_confidence=settings.min_forecast_confidence,
        ),
        broker=broker,
        portfolio=portfolio,
        notify=notifier,
        advisor=advisor,
        specs=valid_specs,
    )


def run(*, once: bool, plain: bool) -> None:
    if plain:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        rich_install_traceback(show_locals=False, width=min(120, Console().width))

    _configure_third_party_noise()
    engine = build_engine()
    settings = engine.settings
    console = Console(force_terminal=sys.stdout.isatty(), soft_wrap=True)

    if plain:
        while True:
            try:
                engine.run_cycle()
            except Exception:
                logging.exception("Cycle failed")
            if once:
                return
            time.sleep(settings.poll_seconds)
        return

    if once:
        try:
            report = engine.run_cycle()
            print_cycle(console, report)
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped.[/]")
        except Exception:
            console.print_exception()
        return

    try:
        run_live_dashboard(console, engine, settings.poll_seconds)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/]")


def _load_spam_specs(settings):
    """Resolve + filter the tradable spec list for spam mode. Shared by the
    single-engine path and the swarm so we don't duplicate the InvestJA /
    price filtering."""
    specs, prevalidated = _load_specs(settings)
    data_client = YahooDataClient()
    if prevalidated:
        valid_specs = specs
    else:
        valid_specs, invalid_tickers = data_client.filter_valid_specs(specs)
        if invalid_tickers:
            logging.warning(
                "Spam mode: excluded %s Yahoo-unsupported symbols: %s",
                len(invalid_tickers),
                ", ".join(invalid_tickers),
            )
        if not valid_specs:
            raise RuntimeError("Spam mode: no valid Yahoo symbols in STOCK_UNIVERSE")

        min_price = 2.0
        valid_specs, below_price_tickers = data_client.filter_specs_above_min_price(
            valid_specs,
            min_price=min_price,
            base_currency=settings.base_currency,
        )
        if below_price_tickers:
            logging.warning(
                "Spam mode: excluded %s symbols priced at or below %.2f %s: %s",
                len(below_price_tickers),
                min_price,
                settings.base_currency.upper(),
                ", ".join(below_price_tickers),
            )
        if not valid_specs:
            raise RuntimeError("Spam mode: no valid symbols above minimum price threshold")

    if settings.broker_mode == "investja":
        investja_specs = [
            s
            for s in valid_specs
            if (
                s.exchange in {"NASDAQ", "NYSE", "TSX"}
                and s.ticker.isascii()
                and s.ticker.replace(".", "").isalnum()
                and (
                    (s.exchange in {"NASDAQ", "NYSE"} and len(s.ticker) <= 5)
                    or s.exchange == "TSX"
                )
            )
        ]
        excluded = [s.ticker for s in valid_specs if s not in investja_specs]
        if excluded:
            logging.warning(
                "Spam mode: excluded %s symbols not suitable for InvestJA execution: %s",
                len(excluded),
                ", ".join(excluded),
            )
        valid_specs = investja_specs
    if not valid_specs:
        raise RuntimeError("Spam mode: no tradable symbols after InvestJA filtering")
    return valid_specs


def build_spam_engine() -> SpamEngine:
    settings = load_settings()
    valid_specs = _load_spam_specs(settings)
    data_client = YahooDataClient()
    portfolio = PortfolioState(cash=settings.initial_cash, trade_fee=settings.trade_fee)
    exchange_map = {s.ticker: s.exchange for s in valid_specs}
    if settings.broker_mode == "investja":
        broker = InvestJABroker(
            portfolio,
            username=settings.investja_username,
            password=settings.investja_password,
            exchange_map=exchange_map,
            min_gross_notional=0.0,
            max_loan=settings.investja_loan_cap,
        )
    else:
        broker = PaperBroker(portfolio, min_gross_notional=0.0)

    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    state = DailySpamState.load()
    return SpamEngine(
        settings=settings,
        data_client=data_client,
        broker=broker,
        portfolio=portfolio,
        specs=valid_specs,
        state=state,
        notifier=notifier,
    )


def _configure_spam_logging(console: Console, *, plain: bool) -> None:
    if plain or not sys.stdout.isatty():
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
            force=True,
        )
        return
    from rich.logging import RichHandler

    for h in list(logging.root.handlers):
        logging.root.removeHandler(h)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console,
                rich_tracebacks=True,
                show_path=False,
                show_time=False,
                markup=False,
            )
        ],
        force=True,
    )


def _run_spam_single(
    *,
    once: bool,
    plain: bool,
    trade_mode: str,
    console: Console,
) -> None:
    """Single-agent path: one Playwright session, one engine, one dashboard.

    InvestJA appears to only support a single concurrent web session per
    account — multiple workers logging in caused trade failures. This is
    the safe, reliable mode and is now the default.
    """
    engine = build_spam_engine()
    settings = engine.settings
    engine.set_spam_trade_mode(trade_mode)

    if plain:
        logging.info(
            "Spam (goon) active | broker=%s universe=%s symbols | trade=%s",
            settings.broker_mode,
            len(engine.specs),
            engine.get_spam_trade_mode().upper(),
        )

    dashboard: SpamDashboard | None = None
    stop_keys = threading.Event()
    key_thread: threading.Thread | None = None
    if not plain and sys.stdout.isatty():
        console.print(
            f"[bold magenta]💸 GOON[/]  ·  broker=[bold]{settings.broker_mode}[/]  "
            f"·  universe=[bold]{len(engine.specs)}[/] symbols  "
            f"·  poll=[bold]{settings.poll_seconds}s[/]  "
            f"[dim]·  trade=[bold]{engine.get_spam_trade_mode().upper()}[/][/]  "
            f"[dim](←/→ or ↑/↓ · SPAM_KEYBOARD=0 disables keys)[/]"
        )
        dashboard = SpamDashboard(console=console, total_tickers=len(engine.specs))
        dashboard.__enter__()
        dashboard.set_spam_trade_mode(engine.get_spam_trade_mode())
        if sys.stdin.isatty():
            key_thread = start_spam_trade_mode_listener(
                engine=engine, dashboard=dashboard, stop_event=stop_keys
            )

    pass_no = 0
    try:
        while True:
            pass_no += 1
            try:
                summary = engine.run_pass(pass_no=pass_no, ui=dashboard)
                if plain:
                    logging.info(
                        "GOON pass done | tickers=%s trades=%s fees_burned=%.2f cash %.2f -> %.2f",
                        summary.tickers_attempted,
                        summary.trades_executed,
                        summary.fees_burned,
                        summary.starting_cash,
                        summary.ending_cash,
                    )
                if summary.trades_executed == 0 and engine.all_quota_exhausted():
                    logging.info(
                        "GOON: daily quota fully exhausted across universe; sleeping until next pass."
                    )
            except Exception:
                logging.exception("Spam pass failed")
            if once:
                return
            if dashboard is not None:
                dashboard.set_status(f"idle · sleeping {settings.poll_seconds}s", icon="⏸")
            time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        if plain:
            logging.info("GOON: keyboard interrupt; shutting down")
    finally:
        stop_keys.set()
        if key_thread is not None and key_thread.is_alive():
            key_thread.join(timeout=1.5)
        if dashboard is not None:
            try:
                dashboard.__exit__(None, None, None)
            except Exception:
                pass
        # Persist any pending sell-cooldown / buy-time entries.
        try:
            engine.state.flush_now()
        except Exception:
            pass
        client = getattr(getattr(engine, "broker", None), "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                logging.warning("GOON: error closing browser session: %s", exc)


def _run_spam_swarm(
    *,
    once: bool,
    plain: bool,
    trade_mode: str,
    workers: int,
    console: Console,
) -> None:
    """Multi-worker path (opt-in via ``--spam-workers N`` with N > 1).

    Warning: InvestJA seems to only allow a single active session per
    account, so multiple workers will often have their trades rejected. Use
    only if you have separate accounts (one per worker) or know the platform
    accepts concurrent sessions.
    """
    settings = load_settings()
    valid_specs = _load_spam_specs(settings)

    controller = SwarmController(mode=trade_mode)  # type: ignore[arg-type]
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    swarm = SpamSwarm(
        settings=settings,
        specs=valid_specs,
        num_workers=workers,
        controller=controller,
        notifier=notifier,
    )
    actual_workers = len(swarm.workers)

    if plain:
        logging.info(
            "Spam (goon) swarm active | broker=%s universe=%s symbols | workers=%s | trade=%s",
            settings.broker_mode,
            len(valid_specs),
            actual_workers,
            controller.get_mode().upper(),
        )

    swarm_dashboard: SwarmDashboard | None = None
    stop_keys = threading.Event()
    key_thread: threading.Thread | None = None

    class _ControllerAdapter:
        def get_spam_trade_mode(self) -> str:
            return controller.get_mode()

        def set_spam_trade_mode(self, mode: str) -> None:
            controller.set_mode(mode)

    if not plain and sys.stdout.isatty():
        console.print(
            f"[bold magenta]💸 GOON SWARM[/]  ·  broker=[bold]{settings.broker_mode}[/]  "
            f"·  universe=[bold]{len(valid_specs)}[/] symbols  "
            f"·  workers=[bold]{actual_workers}[/]  "
            f"·  poll=[bold]{settings.poll_seconds}s[/]  "
            f"[dim]·  trade=[bold]{controller.get_mode().upper()}[/][/]  "
            f"[yellow](experimental — InvestJA may reject concurrent sessions)[/]"
        )
        worker_names = [w.name for w in swarm.workers]
        swarm_dashboard = SwarmDashboard(console=console, worker_names=worker_names)
        swarm.attach_ui(lambda w: swarm_dashboard.proxy_for(w.name))
        swarm.start()
        swarm_dashboard.__enter__()
        swarm_dashboard.set_spam_trade_mode(controller.get_mode())
        if sys.stdin.isatty():
            key_thread = start_spam_trade_mode_listener(
                engine=_ControllerAdapter(),
                dashboard=swarm_dashboard,
                stop_event=stop_keys,
            )
    else:
        swarm.start()

    try:
        if once:
            while not swarm.stop_event.is_set() and any(
                w.last_summary is None and (w.thread is None or w.thread.is_alive())
                for w in swarm.workers
            ):
                time.sleep(0.5)
            return
        while not swarm.stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        if plain:
            logging.info("GOON: keyboard interrupt; shutting down swarm")
    finally:
        stop_keys.set()
        if key_thread is not None and key_thread.is_alive():
            key_thread.join(timeout=1.5)
        swarm.shutdown(timeout=12.0)
        if swarm_dashboard is not None:
            try:
                swarm_dashboard.__exit__(None, None, None)
            except Exception:
                pass


def run_spam(
    *,
    once: bool,
    plain: bool = False,
    trade_mode: str | None = None,
    workers: int = DEFAULT_WORKERS,
) -> None:
    console = Console(force_terminal=sys.stdout.isatty(), soft_wrap=True)
    _configure_spam_logging(console, plain=plain)
    _configure_third_party_noise()

    initial_mode = (trade_mode or os.environ.get("SPAM_TRADE_MODE") or "combo").lower().strip()
    if initial_mode not in ("buy", "sell", "combo"):
        initial_mode = "combo"

    effective_workers = max(1, int(workers or 1))
    if effective_workers == 1:
        _run_spam_single(once=once, plain=plain, trade_mode=initial_mode, console=console)
    else:
        _run_spam_swarm(
            once=once,
            plain=plain,
            trade_mode=initial_mode,
            workers=effective_workers,
            console=console,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="StockRock trading bot")
    parser.add_argument("--once", action="store_true", help="Run only one cycle")
    parser.add_argument("--plain", action="store_true", help="Plain log output instead of Rich dashboard")
    parser.add_argument(
        "--spam",
        action="store_true",
        help=(
            "Run InvestJA 'goon' mode: maximize fees per stock per day to drain "
            "account balance for the lose-the-most sub-contest. Bypasses forecaster, "
            "advisor, and Telegram approval. Does not touch the normal --once/dashboard flow."
        ),
    )
    parser.add_argument(
        "--spam-mode",
        choices=["buy", "sell", "combo"],
        default=None,
        metavar="MODE",
        help=(
            "With --spam: buy (only buys), sell (only sells holdings), or combo "
            "(alternating). Default: env SPAM_TRADE_MODE or combo. In the Rich "
            "dashboard, ←/→ or ↑/↓ cycles modes live."
        ),
    )
    parser.add_argument(
        "--spam-workers",
        type=int,
        default=DEFAULT_WORKERS,
        metavar="N",
        help=(
            f"With --spam: number of parallel worker bots (default {DEFAULT_WORKERS}, single-agent). "
            "Set >1 to experimentally split the universe across N Playwright sessions — "
            "InvestJA may reject concurrent sessions on the same account, so trades often "
            "fail. Only use if you know your account/platform allows it."
        ),
    )
    return parser.parse_args()


def main() -> None:
    """Console entrypoint for ``stockrock`` (see ``pyproject.toml``)."""
    args = parse_args()
    if args.spam:
        run_spam(
            once=args.once,
            plain=args.plain,
            trade_mode=args.spam_mode,
            workers=args.spam_workers,
        )
    else:
        run(once=args.once, plain=args.plain)


if __name__ == "__main__":
    main()
