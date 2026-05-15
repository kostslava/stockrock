"""Rich-powered live dashboard for ``--spam`` mode.

Shows pass progress, the current ticker/action, recent trade outcomes, and
running stats. Designed to be poked by ``SpamEngine`` via a small callback
protocol so the engine itself stays UI-agnostic.
"""

from __future__ import annotations

import logging
import os
import select
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, ProgressColumn, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text


logger = logging.getLogger(__name__)

# Order when cycling with arrow keys: ← / ↑ previous, → / ↓ next
_SPAM_MODE_CYCLE = ("buy", "sell", "combo")


def _cycle_spam_mode(current: str, delta: int) -> str:
    cur = (current or "combo").lower().strip()
    if cur not in _SPAM_MODE_CYCLE:
        cur = "combo"
    i = _SPAM_MODE_CYCLE.index(cur)
    return _SPAM_MODE_CYCLE[(i + delta) % len(_SPAM_MODE_CYCLE)]


def start_spam_trade_mode_listener(
    *,
    engine: object,
    dashboard: SpamDashboard | None,
    stop_event: threading.Event,
) -> threading.Thread | None:
    """Background thread: ←/→ or ↑/↓ cycles BUY ONLY → SELL ONLY → COMBO.

    Implementation notes (why this is fiddly):
      * We read the *raw* tty fd with ``os.read``. ``sys.stdin.read(1)`` goes
        through a ``TextIOWrapper`` whose internal buffer will happily swallow
        the keystroke and block waiting for more bytes, even though
        ``select`` says data is ready.
      * cbreak mode disables line buffering on the tty but keeps ISIG, so
        Ctrl-C still works.
      * Rich's ``Live`` with ``transient=False`` does *not* take the
        alternate screen, so stdin reads are unaffected by the dashboard.
      * Fallback keys (``h``/``l``, ``,``/``.``) exist for terminals that
        mangle CSI escape sequences.
    """
    if not sys.stdin.isatty():
        logger.info("Spam keyboard: stdin is not a TTY; arrow keys disabled")
        return None
    if os.environ.get("SPAM_KEYBOARD", "1").strip().lower() in ("0", "false", "no", "off"):
        logger.info("Spam keyboard: disabled (set SPAM_KEYBOARD=1 to enable)")
        return None
    try:
        import termios  # noqa: PLC0415
        import tty  # noqa: PLC0415
    except ImportError:
        logger.warning("Spam keyboard: termios unavailable; use --spam-mode instead")
        return None

    def runner() -> None:
        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except Exception as exc:
            logger.warning("Spam keyboard: could not read tty attrs: %s", exc)
            return

        first_key_logged = False

        def apply_delta(delta: int) -> None:
            nonlocal first_key_logged
            getm = getattr(engine, "get_spam_trade_mode", None)
            setm = getattr(engine, "set_spam_trade_mode", None)
            if not callable(getm) or not callable(setm):
                return
            nxt = _cycle_spam_mode(str(getm()), delta)
            setm(nxt)
            if dashboard is not None:
                fn = getattr(dashboard, "set_spam_trade_mode", None)
                if callable(fn):
                    fn(nxt)
            if not first_key_logged:
                first_key_logged = True
                logger.info("Spam keyboard: arrow keys active (mode=%s)", nxt.upper())

        try:
            tty.setcbreak(fd, termios.TCSANOW)
            while not stop_event.is_set():
                r, _, _ = select.select([fd], [], [], 0.25)
                if not r:
                    continue
                try:
                    chunk = os.read(fd, 32)
                except OSError:
                    break
                if not chunk:
                    continue

                i = 0
                n = len(chunk)
                while i < n:
                    b = chunk[i:i + 1]

                    if b == b"\x1b" and i + 2 < n and chunk[i + 1:i + 2] == b"[":
                        code = chunk[i + 2:i + 3]
                        if code in (b"A", b"D"):
                            apply_delta(-1)
                        elif code in (b"B", b"C"):
                            apply_delta(1)
                        i += 3
                        continue

                    if b in (b"h", b"H", b","):
                        apply_delta(-1)
                    elif b in (b"l", b"L", b"."):
                        apply_delta(1)
                    elif b == b"\x03":
                        os.kill(os.getpid(), 2)  # SIGINT — let main handle shutdown
                    i += 1
        except Exception as exc:
            logger.warning("Spam keyboard listener stopped: %s", exc)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass

    t = threading.Thread(target=runner, name="spam-trade-mode-keys", daemon=True)
    t.start()
    time.sleep(0.05)
    return t


@dataclass
class TradeLine:
    ts: float
    icon: str
    color: str
    text: str


class SpamDashboard:
    """Encapsulates the live Rich rendering for spam mode."""

    def __init__(self, console: Console, total_tickers: int) -> None:
        self.console = console
        self.total_tickers = max(1, total_tickers)
        self.pass_no = 0
        self.tickers_done = 0
        self.cash = 0.0
        self.loan = 0.0
        self.loan_cap = 0.0
        self.holdings_value = 0.0
        self.fees_burned = 0.0
        self.starting_cash = 0.0
        self.trades_executed = 0
        self.current: str = "[dim]idle[/]"
        self.activity_icon = "•"
        self.recent: deque[TradeLine] = deque(maxlen=10)
        self._t_pass_start = time.time()
        self.spam_trade_mode: str = "combo"

        self.progress = Progress(
            SpinnerColumn(style="bold magenta"),
            TextColumn("[bold]{task.description}", justify="left"),
            BarColumn(bar_width=None, complete_style="bold magenta", finished_style="bold green"),
            TextColumn("{task.completed}/{task.total}"),
            TextColumn("[dim]{task.fields[rate]}[/]"),
            expand=True,
            console=console,
        )
        self.task_id = self.progress.add_task("tickers", total=self.total_tickers, rate="")
        self._live: Optional[Live] = None

    # Lifecycle ----------------------------------------------------------------
    def __enter__(self) -> "SpamDashboard":
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live is not None:
            self._live.__exit__(exc_type, exc, tb)
            self._live = None

    # Hooks called by the engine -----------------------------------------------
    def start_pass(
        self,
        *,
        pass_no: int,
        cash: float,
        loan: float,
        loan_cap: float,
        total_tickers: int,
        platform_fees: float = 0.0,
        holdings_value: float = 0.0,
    ) -> None:
        self.pass_no = pass_no
        self.cash = cash
        self.loan = loan
        self.loan_cap = loan_cap
        self.holdings_value = holdings_value
        self.starting_cash = cash
        self.tickers_done = 0
        self.total_tickers = max(1, total_tickers)
        # Authoritative fee total comes from the platform's "Transaction fees
        # paid" panel — survives bot restarts and matches what the user sees.
        if platform_fees > 0.0:
            self.fees_burned = platform_fees
        self._t_pass_start = time.time()
        self.progress.reset(self.task_id, total=self.total_tickers)
        self._set_status("warming up…", "•")
        self._refresh()

    def set_status(self, label: str, icon: str = "•") -> None:
        self._set_status(label, icon)
        self._refresh()

    def set_spam_trade_mode(self, mode: str) -> None:
        m = (mode or "combo").lower().strip()
        if m not in ("buy", "sell", "combo"):
            m = "combo"
        self.spam_trade_mode = m
        self._refresh()

    def start_ticker(self, ticker: str, price: float, plan: list[str]) -> None:
        plan_str = ",".join(plan)
        self._set_status(
            f"[cyan]{ticker}[/] @ [bold]{price:.2f}[/] · plan=[magenta]{plan_str}[/]",
            "›",
        )
        self._refresh()

    def trade_attempt(self, ticker: str, side: str) -> None:
        color = "green" if side == "buy" else "red"
        self._set_status(
            f"[{color}]{side.upper()}[/] [cyan]{ticker}[/] · placing order…",
            "⚡",
        )
        self._refresh()

    def trade_success(
        self,
        *,
        ticker: str,
        side: str,
        shares: int,
        price: float,
        cash_after: float,
        loan_after: float,
        platform_fees: float = 0.0,
        holdings_value: float = 0.0,
    ) -> None:
        self.cash = cash_after
        self.loan = loan_after
        if platform_fees > 0.0:
            self.fees_burned = platform_fees
        if holdings_value > 0.0:
            self.holdings_value = holdings_value
        self.trades_executed += 1
        # Show net cash (cash - loan) in the trade row so it matches the
        # header and goes negative when on margin — same metric as the
        # "Cash" line up top.
        net_cash = cash_after - loan_after
        net_style = "red" if net_cash < 0 else "dim"
        self._add_trade(
            "✓",
            "bold green",
            f"[bold green]{side.upper():<4}[/] [cyan]{ticker:<6}[/] qty=[bold]{shares}[/] @ {price:.2f}  "
            f"[{net_style}]cash→ {net_cash:,.2f}[/]",
        )

    def trade_fail(self, *, ticker: str, side: str, reason: str) -> None:
        short = reason.splitlines()[0][:80] if reason else "(no detail)"
        self._add_trade(
            "✕",
            "bold red",
            f"[red]{side.upper():<4}[/] [cyan]{ticker:<6}[/] [dim]failed: {short}[/]",
        )

    def trade_skipped(self, *, ticker: str, side: str, reason: str) -> None:
        self._add_trade(
            "⏸",
            "yellow",
            f"[yellow]{side.upper():<4}[/] [cyan]{ticker:<6}[/] [dim]skipped: {reason}[/]",
        )

    def finish_ticker(self) -> None:
        self.tickers_done += 1
        elapsed = max(1e-3, time.time() - self._t_pass_start)
        rate = self.tickers_done / elapsed
        eta = (self.total_tickers - self.tickers_done) / rate if rate > 0 else 0
        self.progress.update(
            self.task_id,
            completed=self.tickers_done,
            rate=f"{rate:.1f}/s · eta {eta:5.0f}s",
        )
        self._refresh()

    def end_pass(self, *, fees_burned: float) -> None:
        # We trust the platform's running total over the local estimate, so
        # only fall back to accumulating locally if the snapshot didn't yield
        # a platform fee value (rare — usually means the snapshot parser
        # missed the panel).
        if self.fees_burned <= 0.0:
            self.fees_burned += fees_burned
        self._set_status(
            f"[dim]pass {self.pass_no} done · {self.trades_executed} trades · "
            f"${fees_burned:,.2f} burned this pass[/]",
            "✓",
        )
        self._refresh()

    # Rendering ----------------------------------------------------------------
    def _set_status(self, label: str, icon: str) -> None:
        self.current = label
        self.activity_icon = icon

    def _add_trade(self, icon: str, color: str, text: str) -> None:
        self.recent.append(TradeLine(ts=time.time(), icon=icon, color=color, text=text))
        self._refresh()

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _header(self) -> Panel:
        loan_pct = (self.loan / self.loan_cap * 100.0) if self.loan_cap > 0 else 0.0
        # Effective balance = cash minus outstanding loan. Goes negative as
        # the bot taps margin past depleted cash — exactly what the
        # lose-the-most leaderboard rewards.
        net_cash = self.cash - self.loan
        # Total equity = net cash + value of currently held positions. This
        # is what InvestJA shows as net worth, and what determines ranking.
        equity = net_cash + self.holdings_value
        net_style = "bold red" if net_cash < 0 else ("bold" if net_cash == 0 else "bold green")
        equity_style = "bold red" if equity < 0 else ("bold" if equity == 0 else "bold green")
        body = Text()
        body.append("Cash  ", style="dim")
        body.append(f"${net_cash:,.2f}  ", style=net_style)
        body.append("·  Loan  ", style="dim")
        body.append(f"${self.loan:,.0f} / ${self.loan_cap:,.0f}  ", style="bold")
        body.append(f"({loan_pct:.0f}%)  ", style="dim")
        body.append("·  Holdings  ", style="dim")
        body.append(f"${self.holdings_value:,.2f}  ", style="bold")
        body.append("·  Equity  ", style="dim")
        body.append(f"${equity:,.2f}  ", style=equity_style)
        body.append("·  Fees burned  ", style="dim")
        body.append(f"${self.fees_burned:,.2f}", style="bold red")
        mode_key = self.spam_trade_mode.lower()
        mode_lbl = {"buy": "BUY ONLY", "sell": "SELL ONLY", "combo": "COMBO (BUY↔SELL)"}.get(
            mode_key, "COMBO"
        )
        subtitle = (
            f"[dim]Trade [/][bold yellow]{mode_lbl}[/]"
            f"[dim]  ·  ←/→ or ↑/↓ (or h/l) to cycle  ·  [/]"
            f"[bold cyan]stockrock --spam --spam-mode buy|sell|combo[/]"
        )
        return Panel(
            body,
            title=f"[bold magenta]💸 GOON pass #{self.pass_no}[/]",
            subtitle=subtitle,
            box=box.ROUNDED,
            border_style="magenta",
        )

    def _status_panel(self) -> Panel:
        line = Text.from_markup(f" [bold magenta]{self.activity_icon}[/] {self.current}")
        return Panel(line, box=box.MINIMAL, border_style="dim", padding=(0, 1))

    def _recent_panel(self) -> Panel:
        if not self.recent:
            body: RenderableType = Text("(no trades yet)", style="dim italic")
        else:
            tbl = Table.grid(padding=(0, 1), expand=True)
            tbl.add_column(width=2, no_wrap=True)
            tbl.add_column(ratio=1)
            for line in reversed(self.recent):
                tbl.add_row(Text(line.icon, style=line.color), Text.from_markup(line.text))
            body = tbl
        return Panel(
            body,
            title="[bold]Recent trades[/]",
            box=box.ROUNDED,
            border_style="dim",
        )

    def _render(self) -> RenderableType:
        return Group(
            self._header(),
            self._status_panel(),
            self.progress,
            self._recent_panel(),
        )


# =============================================================================
# Swarm dashboard — 5 worker tiles + 1 unified log (2×3 grid), fixed tile body.
# =============================================================================

# Lines of body text inside each of the six tiles (excluding panel borders).
_SWARM_TILE_BODY_LINES = 9
_SWARM_LOG_MAX = 48


@dataclass
class SwarmLogLine:
    ts: float
    worker: str
    icon: str
    color: str
    text: str


@dataclass
class _WorkerView:
    """Per-worker tile state (no per-ticker log — that goes to ``_unified_log``)."""

    name: str
    cash: float = 0.0
    loan: float = 0.0
    holdings_value: float = 0.0
    fees_burned: float = 0.0
    pass_no: int = 0
    tickers_done: int = 0
    total_tickers: int = 1
    activity: str = "[dim]waiting…[/]"
    icon: str = "•"
    trades_executed: int = 0
    t_pass_start: float = field(default_factory=time.time)


_WORKER_BORDER_STYLES = ("cyan", "blue", "green", "yellow", "magenta")


def _pad_body_lines(lines: list[str], height: int) -> str:
    while len(lines) < height:
        lines.append("[dim]·[/]")
    return "\n".join(lines[:height])


class _WorkerUIProxy:
    """Routes engine hooks into one worker tile + the shared unified log."""

    def __init__(self, dashboard: "SwarmDashboard", view: _WorkerView) -> None:
        self._dash = dashboard
        self._view = view

    def start_pass(
        self,
        *,
        pass_no: int,
        cash: float,
        loan: float,
        loan_cap: float,
        total_tickers: int,
        platform_fees: float = 0.0,
        holdings_value: float = 0.0,
    ) -> None:
        v = self._view
        v.pass_no = pass_no
        v.cash = cash
        v.loan = loan
        v.holdings_value = holdings_value
        v.tickers_done = 0
        v.total_tickers = max(1, total_tickers)
        v.t_pass_start = time.time()
        if platform_fees > 0.0:
            v.fees_burned = platform_fees
        self._dash.loan_cap = max(self._dash.loan_cap, loan_cap)
        self._dash._refresh()

    def set_status(self, label: str, icon: str = "•") -> None:
        self._view.activity = label
        self._view.icon = icon
        self._dash._refresh()

    def set_spam_trade_mode(self, mode: str) -> None:
        self._dash.set_spam_trade_mode(mode)

    def start_ticker(self, ticker: str, price: float, plan: list[str]) -> None:
        plan_str = ",".join(plan)
        v = self._view
        v.activity = f"[cyan]{ticker}[/] @ [bold]{price:.2f}[/] · [magenta]{plan_str}[/]"
        v.icon = "›"
        self._dash.push_log(
            v.name, "›", "dim", f"[cyan]{ticker}[/] @ {price:.2f}  [dim]plan {plan_str}[/]"
        )
        self._dash._refresh()

    def trade_attempt(self, ticker: str, side: str) -> None:
        color = "green" if side == "buy" else "red"
        v = self._view
        v.activity = f"[{color}]{side.upper()}[/] [cyan]{ticker}[/] · placing…"
        v.icon = "⚡"
        self._dash.push_log(
            v.name, "…", color, f"[{color}]{side.upper()}[/] [cyan]{ticker}[/]  [dim]placing…[/]"
        )
        self._dash._refresh()

    def trade_success(
        self,
        *,
        ticker: str,
        side: str,
        shares: int,
        price: float,
        cash_after: float,
        loan_after: float,
        platform_fees: float = 0.0,
        holdings_value: float = 0.0,
    ) -> None:
        v = self._view
        v.cash = cash_after
        v.loan = loan_after
        if platform_fees > 0.0:
            v.fees_burned = platform_fees
        if holdings_value > 0.0:
            v.holdings_value = holdings_value
        v.trades_executed += 1
        net_cash = cash_after - loan_after
        net_style = "red" if net_cash < 0 else "green"
        self._dash.push_log(
            v.name,
            "✓",
            "bold green",
            (
                f"[bold green]{side.upper():<4}[/] [cyan]{ticker:<6}[/] "
                f"×{shares} @ {price:.2f}  [{net_style}]net ${net_cash:,.0f}[/]"
            ),
        )
        self._dash._refresh()

    def trade_fail(self, *, ticker: str, side: str, reason: str) -> None:
        short = (reason or "").splitlines()[0][:52] if reason else "(no detail)"
        v = self._view
        self._dash.push_log(
            v.name,
            "✕",
            "bold red",
            f"[red]{side.upper():<4}[/] [cyan]{ticker:<6}[/]  [dim]{short}[/]",
        )
        self._dash._refresh()

    def trade_skipped(self, *, ticker: str, side: str, reason: str) -> None:
        v = self._view
        self._dash.push_log(
            v.name,
            "⏸",
            "yellow",
            f"[yellow]{side.upper():<4}[/] [cyan]{ticker:<6}[/]  [dim]{reason[:44]}[/]",
        )
        self._dash._refresh()

    def finish_ticker(self) -> None:
        self._view.tickers_done += 1
        self._dash._refresh()

    def end_pass(self, *, fees_burned: float) -> None:
        v = self._view
        if v.fees_burned <= 0.0:
            v.fees_burned += fees_burned
        v.activity = f"[dim]pass {v.pass_no} done · {v.trades_executed} trades[/]"
        v.icon = "✓"
        self._dash.push_log(
            v.name, "✓", "dim", f"[dim]pass {v.pass_no} done · {v.trades_executed} trades[/]"
        )
        self._dash._refresh()


class SwarmDashboard:
    """Five worker tiles + one unified log in a 2×3 grid (equal-sized cells)."""

    def __init__(self, console: Console, worker_names: list[str]) -> None:
        self.console = console
        self._order = list(worker_names)
        self.views: dict[str, _WorkerView] = {n: _WorkerView(name=n) for n in worker_names}
        self.spam_trade_mode: str = "combo"
        self.loan_cap: float = 0.0
        self._live: Optional[Live] = None
        self._refresh_lock = threading.Lock()
        self._last_refresh = 0.0
        self._log_lock = threading.Lock()
        self._unified_log: deque[SwarmLogLine] = deque(maxlen=_SWARM_LOG_MAX)

    def __enter__(self) -> "SwarmDashboard":
        use_screen = os.environ.get("SPAM_LIVE_SCREEN", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=8,
            transient=False,
            screen=use_screen,
        )
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live is not None:
            self._live.__exit__(exc_type, exc, tb)
            self._live = None

    def proxy_for(self, name: str) -> _WorkerUIProxy:
        if name not in self.views:
            self.views[name] = _WorkerView(name=name)
            if name not in self._order:
                self._order.append(name)
        return _WorkerUIProxy(self, self.views[name])

    def set_spam_trade_mode(self, mode: str) -> None:
        m = (mode or "combo").lower().strip()
        if m not in ("buy", "sell", "combo"):
            m = "combo"
        self.spam_trade_mode = m
        self._refresh()

    def push_log(self, worker: str, icon: str, color: str, text: str) -> None:
        with self._log_lock:
            self._unified_log.append(
                SwarmLogLine(ts=time.time(), worker=worker, icon=icon, color=color, text=text)
            )

    def _refresh(self) -> None:
        with self._refresh_lock:
            now = time.time()
            if now - self._last_refresh < 0.028:
                return
            self._last_refresh = now
            if self._live is not None:
                self._live.update(self._render())

    # Rendering ----------------------------------------------------------------
    def _aggregate(self) -> tuple[float, float, float, float, float, int]:
        cash = 0.0
        loan = 0.0
        holdings = 0.0
        fees = 0.0
        trades = 0
        for v in self.views.values():
            cash = max(cash, v.cash)
            loan = max(loan, v.loan)
            holdings = max(holdings, v.holdings_value)
            fees = max(fees, v.fees_burned)
            trades += v.trades_executed
        equity = (cash - loan) + holdings
        return cash, loan, holdings, fees, equity, trades

    def _header(self) -> Panel:
        cash, loan, holdings, fees, equity, trades = self._aggregate()
        net_cash = cash - loan
        net_style = "bold red" if net_cash < 0 else ("bold" if net_cash == 0 else "bold green")
        equity_style = "bold red" if equity < 0 else ("bold" if equity == 0 else "bold green")
        loan_pct = (loan / self.loan_cap * 100.0) if self.loan_cap > 0 else 0.0
        body = Text()
        body.append("Cash  ", style="dim")
        body.append(f"${net_cash:,.2f}  ", style=net_style)
        body.append("·  Loan  ", style="dim")
        body.append(f"${loan:,.0f} / ${self.loan_cap:,.0f}  ", style="bold white")
        body.append(f"({loan_pct:.0f}%)  ", style="dim")
        body.append("·  Holdings  ", style="dim")
        body.append(f"${holdings:,.2f}  ", style="bold white")
        body.append("·  Equity  ", style="dim")
        body.append(f"${equity:,.2f}  ", style=equity_style)
        body.append("·  Fees  ", style="dim")
        body.append(f"${fees:,.2f}  ", style="bold red")
        body.append("·  Trades  ", style="dim")
        body.append(f"{trades}", style="bold white")

        mode_key = self.spam_trade_mode.lower()
        mode_lbl = {"buy": "BUY ONLY", "sell": "SELL ONLY", "combo": "COMBO (BUY↔SELL)"}.get(
            mode_key, "COMBO"
        )
        subtitle = (
            f"[dim]Mode [/][bold yellow]{mode_lbl}[/]"
            f"[dim]  ·  ←/→ / ↑/↓ / h·l  ·  all workers  ·  [/]"
            f"[bold cyan]--spam-mode buy|sell|combo[/]"
        )
        return Panel(
            body,
            title=f"[bold magenta]💸 GOON SWARM[/]  [dim]·[/]  [bold]{len(self.views)}[/][dim] workers[/]",
            subtitle=subtitle,
            box=box.DOUBLE,
            border_style="magenta",
            padding=(0, 1),
        )

    def _worker_tile(self, name: str) -> Panel:
        view = self.views.get(name) or _WorkerView(name=name)
        try:
            idx = self._order.index(name)
        except ValueError:
            idx = 0
        border = _WORKER_BORDER_STYLES[idx % len(_WORKER_BORDER_STYLES)]

        net_cash = view.cash - view.loan
        net_style = "red" if net_cash < 0 else "green"
        pct = int(100 * view.tickers_done / max(1, view.total_tickers))
        bar_w = 14
        filled = max(0, min(bar_w, int(bar_w * view.tickers_done / max(1, view.total_tickers))))
        bar = "[bold " + border + "]" + ("━" * filled) + "[/][dim]" + ("╺" * (bar_w - filled)) + "[/]"

        elapsed = max(1e-3, time.time() - view.t_pass_start)
        rate = view.tickers_done / elapsed

        lines = [
            f"[bold {border}]{view.icon}[/] [bold white]{view.activity}[/]",
            f"[dim]pass[/] [bold]#{view.pass_no}[/]   {bar} [dim]{view.tickers_done}/{view.total_tickers} ({pct}%)[/]",
            f"[dim]{rate:.1f} steps/s[/]   [dim]·[/]   [bold {net_style}]${net_cash:,.0f}[/] net   [dim]·[/]   [red]${view.fees_burned:,.0f}[/] fees   [dim]·[/]   [white]{view.trades_executed}[/][dim] tx[/]",
        ]
        body = _pad_body_lines(lines, _SWARM_TILE_BODY_LINES)
        return Panel(
            body,
            title=f"[bold white on {border}] {name} [/]",
            border_style=border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _log_tile(self) -> Panel:
        with self._log_lock:
            tail = list(self._unified_log)[-_SWARM_TILE_BODY_LINES:]
        lines: list[str] = []
        for row in tail:
            wtag = f"[bold white]{row.worker:<3}[/]"
            lines.append(f"{wtag} [{row.color}]{row.icon}[/] {row.text}")
        body = _pad_body_lines(lines, _SWARM_TILE_BODY_LINES)
        return Panel(
            body,
            title="[bold white on bright_black] LOG [/]  [dim]all workers[/]",
            border_style="white",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _render(self) -> RenderableType:
        from rich.layout import Layout  # noqa: PLC0415

        order = self._order[:5]
        while len(order) < 5:
            order.append(f"W{len(order) + 1}")

        top = Layout()
        top.split_row(
            Layout(self._worker_tile(order[0]), ratio=1, name="w0"),
            Layout(self._worker_tile(order[1]), ratio=1, name="w1"),
            Layout(self._worker_tile(order[2]), ratio=1, name="w2"),
        )
        bottom = Layout()
        bottom.split_row(
            Layout(self._worker_tile(order[3]), ratio=1, name="w3"),
            Layout(self._worker_tile(order[4]), ratio=1, name="w4"),
            Layout(self._log_tile(), ratio=1, name="log"),
        )
        body = Layout()
        body.split_column(
            Layout(top, ratio=1, name="row_a"),
            Layout(bottom, ratio=1, name="row_b"),
        )
        root = Layout()
        root.split_column(
            Layout(self._header(), name="hdr"),
            Layout(body, ratio=1, name="grid"),
        )
        return root
