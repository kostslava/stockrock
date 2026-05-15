from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import time

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from stockrock.report import CycleReport, HoldingRow


def _money(n: float, currency: str) -> str:
    return f"{currency} {n:,.2f}"


def _pct(x: float) -> Text:
    style = "bold green" if x > 0 else ("bold red" if x < 0 else "dim")
    return Text(f"{x * 100:+.2f}%", style=style)


def _render_header(report: CycleReport) -> Panel:
    title = Text()
    title.append("  ", style="")
    title.append("STOCKROCK", style="bold white on rgb(30,58,95)")
    title.append("  ", style="")
    title.append("InvestJA oil rotation", style="italic dim")
    subtitle = Text(f"  {report.run_at}", style="dim")
    inner = Group(Align.left(title), Align.left(subtitle))
    return Panel(inner, box=box.ROUNDED, border_style="rgb(180,140,60)", padding=(0, 1))


def _account_table(report: CycleReport) -> Table:
    metrics = Table(show_edge=False, box=None, padding=(0, 1))
    metrics.add_column(style="dim", justify="right", width=14)
    metrics.add_column(justify="left")
    hv = sum(h.market_value for h in report.holdings)
    metrics.add_row("Equity", Text(_money(report.equity, report.base_currency), style="bold cyan"))
    metrics.add_row("Cash", _money(report.cash, report.base_currency))
    metrics.add_row("Loan", _money(report.loan_balance, report.base_currency))
    metrics.add_row("Holdings", _money(hv, report.base_currency))
    metrics.add_row("Risk dial", Text(f"{report.risk_dial:.0%}", style="yellow"))
    metrics.add_row("Order budget", _money(report.budget, report.base_currency))
    metrics.add_row("Trade fee", Text(_money(report.trade_fee, report.base_currency), style="dim"))
    return metrics


def _forecast_table(report: CycleReport) -> Table:
    fc = Table(show_edge=False, box=None, padding=(0, 1))
    fc.add_column(style="dim", justify="right", width=14)
    fc.add_column()
    fc.add_row("Model", Text(report.forecast_provider, style="magenta"))
    fc.add_row("Horizon ret.", _pct(report.forecast_expected_return))
    fc.add_row("Confidence", Text(f"{report.forecast_confidence:.0%}", style="dim"))
    return fc


def _benchmark_line(report: CycleReport) -> Text:
    line = Text()
    line.append("Oil ", style="dim")
    line.append(report.oil_proxy, style="bold")
    line.append("  ·  Last ", style="dim")
    line.append(_money(report.oil_last, "USD"), style="white")
    return line


def _decision_style(report: CycleReport) -> str:
    if report.decision == "hold":
        return "yellow"
    if report.decision in {"rotate", "buy"}:
        return "green"
    if report.decision == "sell":
        return "red"
    return "white"


def _decision_panel(report: CycleReport) -> Panel:
    reason = Text(report.strategy_reason, style="dim")
    target = Text()
    if report.target_symbol:
        target.append("Target: ", style="dim")
        target.append(report.target_symbol, style="bold green")

    decision_text = Text()
    decision_text.append(report.decision.upper(), style=f"bold {_decision_style(report)}")
    if report.target_symbol:
        decision_text.append(" → ", style="dim")
        decision_text.append(report.target_symbol, style="bold")

    return Panel(
        Group(decision_text, reason, target),
        title="[bold]Decision[/]",
        border_style=_decision_style(report),
        box=box.ROUNDED,
    )


def _quotes_table(report: CycleReport) -> Table:
    quotes = Table(show_header=True, box=box.SIMPLE_HEAD, padding=(0, 1), expand=True)
    quotes.add_column("Symbol", style="bold")
    quotes.add_column("Last", justify="right")
    quotes.add_row(report.bull_symbol, _money(report.bull_price, report.base_currency))
    quotes.add_row(report.bear_symbol, _money(report.bear_price, report.base_currency))
    return quotes


def _holdings_table(report: CycleReport) -> Table:
    ht = Table(show_header=True, box=box.SIMPLE_HEAD, padding=(0, 1), expand=True)
    ht.add_column("Ticker", style="bold")
    ht.add_column("Qty", justify="right")
    ht.add_column("Avg", justify="right")
    ht.add_column("Value", justify="right")
    if not report.holdings:
        ht.add_row("—", "—", "—", "—")
    else:
        for h in report.holdings:
            ht.add_row(
                h.symbol,
                str(h.shares),
                _money(h.avg_price, report.base_currency),
                _money(h.market_value, report.base_currency),
            )
    return ht


def _receipts_table(report: CycleReport) -> Table:
    rt = Table(show_header=True, box=box.SIMPLE_HEAD, padding=(0, 1), expand=True)
    rt.add_column("Side", style="bold", width=6)
    rt.add_column("Symbol")
    rt.add_column("Qty", justify="right")
    rt.add_column("Total", justify="right")
    if not report.receipts:
        rt.add_row("—", "—", "—", "—")
    else:
        for r in report.receipts:
            side_style = "green" if r.side == "buy" else "red"
            rt.add_row(
                Text(r.side.upper(), style=side_style),
                r.symbol,
                str(r.shares),
                _money(r.gross_or_cost, report.base_currency),
            )
    return rt


def _footer(report: CycleReport, footer_note: str) -> Text:
    line = Text()
    line.append("Notify  ", style="dim")
    msg = report.message if len(report.message) <= 90 else report.message[:87] + "…"
    line.append(msg, style="white")
    if footer_note:
        line.append("\n", style="")
        line.append(footer_note, style="cyan")
    return line


def render_dashboard(report: CycleReport, footer_note: str = "") -> RenderableType:
    top_row = Columns(
        [
            Panel(_account_table(report), title="[bold]Account[/]", border_style="blue", box=box.ROUNDED),
            Panel(_forecast_table(report), title="[bold]Forecast[/]", border_style="green", box=box.ROUNDED),
        ],
        equal=True,
        expand=True,
    )

    mid_row = Columns(
        [
            Panel(_quotes_table(report), title="[bold]Quotes[/]", border_style="grey42", box=box.ROUNDED),
            Panel(_holdings_table(report), title="[bold]Holdings[/]", border_style="bright_blue", box=box.ROUNDED),
        ],
        equal=True,
        expand=True,
    )

    return Group(
        _render_header(report),
        top_row,
        Panel(_benchmark_line(report), title="[bold]Benchmark[/]", border_style="grey50", box=box.ROUNDED),
        _decision_panel(report),
        mid_row,
        Panel(_receipts_table(report), title="[bold]This cycle[/]", border_style="magenta", box=box.ROUNDED),
        Panel(_footer(report, footer_note), box=box.ROUNDED, border_style="dim", padding=(0, 1)),
    )


def _boot_report(engine, note: str = "Initializing data fetch and model run") -> CycleReport:
    settings = engine.settings
    holdings = [
        HoldingRow(
            symbol=pos.symbol,
            shares=pos.shares,
            avg_price=pos.avg_price,
            last_price=pos.avg_price,
        )
        for pos in engine.portfolio.positions.values()
    ]
    equity = engine.portfolio.cash + sum(h.market_value for h in holdings)
    return CycleReport(
        run_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        decision="hold",
        message=note,
        forecast_expected_return=0.0,
        forecast_provider=settings.model_provider,
        forecast_confidence=0.0,
        strategy_reason=note,
        target_symbol=None,
        equity=equity,
        cash=engine.portfolio.cash,
        loan_balance=float(getattr(engine.broker, "last_loan_balance", 0.0)),
        budget=equity * settings.risk_dial,
        risk_dial=settings.risk_dial,
        trade_fee=settings.trade_fee,
        base_currency=settings.base_currency,
        bull_symbol=settings.bull_symbol,
        bear_symbol=settings.bear_symbol,
        bull_price=0.0,
        bear_price=0.0,
        oil_proxy=settings.oil_proxy_symbol,
        oil_last=0.0,
        holdings=holdings,
        receipts=[],
    )


def print_cycle(console: Console, report: CycleReport, footer_note: str = "") -> None:
    console.print(render_dashboard(report, footer_note))


def run_live_dashboard(console: Console, engine, poll_seconds: int) -> None:
    spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    with Live(console=console, refresh_per_second=2, transient=False, vertical_overflow="ellipsis") as live:
        boot_note = "Startup sync complete."
        try:
            engine.broker.sync()
        except Exception as exc:
            boot_note = f"Startup sync failed ({type(exc).__name__}). Retrying in cycle."
        last_report = _boot_report(engine, boot_note)
        live.update(render_dashboard(last_report, "⚡ Starting first cycle now..."))
        frame_idx = 0
        first_cycle = True
        executor = ThreadPoolExecutor(max_workers=1)
        while True:
            try:
                if last_report is not None and not first_cycle:
                    for remaining in range(poll_seconds, 0, -1):
                        spinner = spinner_frames[frame_idx % len(spinner_frames)]
                        frame_idx += 1
                        foot = f"{spinner} Next cycle in {remaining:>3}s · Ctrl+C to exit"
                        live.update(render_dashboard(last_report, foot))
                        time.sleep(1)

                def _progress_update(report: CycleReport) -> None:
                    nonlocal last_report, frame_idx
                    last_report = report
                    spinner = spinner_frames[frame_idx % len(spinner_frames)]
                    frame_idx += 1
                    live.update(render_dashboard(last_report, f"{spinner} Live update: cycle in progress"))

                future = executor.submit(engine.run_cycle, _progress_update)
                start = time.time()
                while not future.done():
                    spinner = spinner_frames[frame_idx % len(spinner_frames)]
                    frame_idx += 1
                    elapsed = int(time.time() - start)
                    foot = f"{spinner} Running cycle... {elapsed}s elapsed · Waiting for market/API/approval"
                    live.update(render_dashboard(last_report, foot))
                    time.sleep(1)
                report = future.result()
                last_report = report
                first_cycle = False
                foot = f"✅ Cycle complete · Next cycle in {poll_seconds}s · Ctrl+C to exit"
                live.update(render_dashboard(report, foot))
            except Exception:
                console.print_exception()
                time.sleep(2)
