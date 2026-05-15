from __future__ import annotations

import contextlib
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

logger = logging.getLogger(__name__)


def _html_shows_post_buy_sell_hold(html: str) -> bool:
    """True when HTML contains InvestJA's *buy-then-sell* cooldown, not random '1 hour' copy.

    A bare substring search on the whole dashboard was matching unrelated text
    (banners, help copy, etc.) and mis-classifying failures as ``SellHoldError``.
    """
    if not html:
        return False
    h = html.lower()
    if "less then 1 hour ago" not in h and "less than 1 hour ago" not in h:
        return False
    # Require trading / order context near the cooldown phrase.
    return bool(
        re.search(
            r"(stock|share|shares|position|purchase|purchased|bought|sell|order|trade|transaction)"
            r".{0,160}?(less\s+th[ae]n\s+1\s+hour|less\s+then\s+1\s+hour)"
            r"|(less\s+th[ae]n\s+1\s+hour|less\s+then\s+1\s+hour)"
            r".{0,160}?(stock|share|shares|position|purchase|purchased|bought|sell|order|trade|transaction)",
            h,
            re.DOTALL | re.IGNORECASE,
        )
    )


class SellHoldError(RuntimeError):
    """Raised when InvestJA refuses a SELL because the position is still
    inside the 1-hour day-trading cooldown after a prior BUY. This is a
    routine platform refusal, not a transport-layer failure — the engine
    should treat the ticker as hold-blocked rather than retrying."""

    def __init__(self, ticker: str) -> None:
        super().__init__(
            f"InvestJA refused SELL {ticker}: less than 1 hour since BUY (day-trading rule)"
        )
        self.ticker = ticker


@dataclass(frozen=True)
class RemotePosition:
    symbol: str
    shares: int
    avg_price: float


@dataclass(frozen=True)
class RemoteAccountSnapshot:
    cash: float
    loan_balance: float
    positions: list[RemotePosition]
    fees_paid: float = 0.0
    # Platform-reported aggregate value of currently-held positions ("Holdings"
    # in the Account summary panel). Lets us show real total equity in the UI.
    holdings_value: float = 0.0


class InvestJAWebClient:
    def __init__(self, username: str, password: str, base_url: str = "https://investja.org") -> None:
        self.username = username.strip()
        self.password = password.strip()
        self.base_url = base_url.rstrip("/")
        self.headless = os.getenv("INVESTJA_HEADLESS", "true").strip().lower() != "false"
        # Persistent Playwright session: login once, reuse the browser across
        # every snapshot/buy/sell call instead of relaunching chromium per op.
        # That collapses the ~5-10s per-op login overhead to a single one-time
        # cost.
        self._pw = None
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._logged_in = False

    def _abs(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _start_browser(self) -> Page:
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._browser is None or not getattr(self._browser, "is_connected", lambda: True)():
            self._browser = self._pw.chromium.launch(headless=self.headless)
            self._context = None
            self._page = None
            self._logged_in = False
        if self._context is None:
            self._context = self._browser.new_context()
            self._page = None
        if self._page is None or self._page.is_closed():
            self._page = self._context.new_page()
            self._logged_in = False
        return self._page

    def _ensure_session(self) -> Page:
        page = self._start_browser()
        if not self._logged_in:
            self._login_with_page(page)
            self._logged_in = True
        return page

    def _reset_session(self) -> None:
        """Tear down the persistent session so the next call rebuilds it."""
        self._logged_in = False
        for closer_name in ("_page", "_context", "_browser"):
            closer = getattr(self, closer_name, None)
            if closer is None:
                continue
            with contextlib.suppress(Exception):
                closer.close()
            setattr(self, closer_name, None)
        if self._pw is not None:
            with contextlib.suppress(Exception):
                self._pw.stop()
            self._pw = None

    def close(self) -> None:
        self._reset_session()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self._reset_session()

    def _pick_form(self, html: str):
        soup = BeautifulSoup(html, "html.parser")
        for form in soup.find_all("form"):
            text = form.get_text(" ", strip=True).lower()
            if "login" in text or "password" in text:
                return form
        return soup.find("form")

    def _first_visible(self, page: Page, selectors: list[str]):
        for selector in selectors:
            locator = page.locator(selector).first
            with contextlib.suppress(Exception):
                if locator.count() > 0 and locator.is_visible():
                    return locator
        return None

    def _login_with_page(self, page: Page) -> None:
        if not self.username or not self.password:
            raise RuntimeError("Missing InvestJA credentials")
        page.goto(self._abs("/"), wait_until="domcontentloaded")
        username_input = self._first_visible(
            page,
            [
                "input[name*=user i]",
                "input[name*=team i]",
                "input[type='text']",
                "input[type='email']",
            ],
        )
        password_input = self._first_visible(page, ["input[type='password']", "input[name*=pass i]"])
        if username_input is None or password_input is None:
            raise RuntimeError("Could not find InvestJA login inputs")
        username_input.fill(self.username)
        password_input.fill(self.password)

        submit = self._first_visible(
            page,
            [
                "button:has-text('Sign in')",
                "button:has-text('Login')",
                "input[type='submit']",
                "button[type='submit']",
            ],
        )
        if submit is None:
            raise RuntimeError("Could not find login submit button")
        submit.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(400)
        pc = page.content()
        if "You have to log in" in pc and "team" in pc.lower():
            raise RuntimeError("InvestJA login failed")

    def _parse_currency(self, text: str) -> float:
        cleaned = re.sub(r"[^0-9.\-]", "", text)
        return float(cleaned) if cleaned else 0.0

    def _extract_metric(self, html: str, labels: list[str]) -> float | None:
        soup = BeautifulSoup(html, "html.parser")
        lower_labels = [x.lower() for x in labels]
        for row in soup.select("table tr"):
            cols = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
            if len(cols) < 2:
                continue
            key = cols[0].strip().lower()
            if any(lbl in key for lbl in lower_labels):
                return self._parse_currency(cols[1])
        for lbl in labels:
            m = re.search(rf"{re.escape(lbl)}\s*[:\-]?\s*\$?\(?\s*([\-0-9,]+\.\d+)\s*\)?", html, flags=re.IGNORECASE)
            if m:
                val = self._parse_currency(m.group(1))
                # Handle accounting-style negatives "(123.45)" near label.
                if m.group(0).find("(") != -1 and val > 0:
                    val = -val
                return val
        return None

    def _first_visible_exchange_select(self, page: Page):
        """Return the exchange <select> the user actually sees (purchase modal first)."""
        preferred = page.locator("#stock-purchase-modal select[name='exchange']")
        for i in range(preferred.count()):
            cand = preferred.nth(i)
            with contextlib.suppress(Exception):
                if cand.is_visible():
                    return cand
        candidates = page.locator("select[name='exchange']")
        for i in range(candidates.count()):
            cand = candidates.nth(i)
            with contextlib.suppress(Exception):
                if cand.is_visible():
                    return cand
        return None

    # InvestJA sell URLs and buy-form <option value="…"> use these spellings.
    _EXCHANGE_URL_NAME = {"NASDAQ": "Nasdaq", "NYSE": "NYSE", "TSX": "TSX"}

    def _select_exchange(self, page: Page, exchange: str, side: str) -> None:
        # Prefer a *visible* exchange select. SELL via the row-action button opens
        # a modal that has no exchange dropdown (exchange is implicit), so the
        # only matching <select> in the DOM is a hidden one from the purchase
        # modal. In that case we just skip exchange selection.
        sel = None
        if side == "buy":
            for _ in range(55):
                sel = self._first_visible_exchange_select(page)
                if sel is not None:
                    break
                page.wait_for_timeout(150)
        else:
            sel = self._first_visible_exchange_select(page)
        if sel is None:
            if side == "sell":
                logger.info("No visible exchange dropdown for SELL %s — skipping (prefilled by row action)", exchange)
                return
            raise RuntimeError("Could not find a visible exchange dropdown for BUY")
        options = sel.locator("option").all()
        values: list[str] = []
        labels: list[str] = []
        for opt in options:
            with contextlib.suppress(Exception):
                values.append((opt.get_attribute("value") or "").strip())
                labels.append((opt.inner_text() or "").strip())
        # Match InvestJA sell URLs / real <option value="…"> (e.g. NASDAQ → "Nasdaq").
        url_form = self._EXCHANGE_URL_NAME.get(exchange, exchange)
        targets: list[str] = []
        for t in (url_form, exchange, exchange.capitalize(), exchange.title()):
            if t and t not in targets:
                targets.append(t)
        picked = False
        for t in targets:
            with contextlib.suppress(Exception):
                sel.select_option(value=t)
                picked = True
            if picked:
                break
            with contextlib.suppress(Exception):
                sel.select_option(label=t)
                picked = True
            if picked:
                break
        if not picked:
            # fallback by partial match in visible labels
            ex_u = exchange.strip().upper()
            for opt in options:
                label = (opt.inner_text() or "").strip().upper()
                val = (opt.get_attribute("value") or "").strip()
                if ex_u in label or label in ex_u or ex_u in val.upper():
                    sel.select_option(value=val)
                    picked = True
                    break
        selected_text = (sel.locator("option:checked").first.inner_text() or "").strip().upper()
        if not picked or exchange not in selected_text:
            raise RuntimeError(
                f"Failed to select exchange {exchange}. Options labels={labels} values={values} selected={selected_text}"
            )

    def _set_ticker(self, page: Page, ticker: str) -> None:
        """InvestJA uses a <select> for ticker in the purchase flow; plain fill() misses it."""
        target = ticker.strip().upper()
        raw = ticker.strip()
        select_css = [
            "#stock-purchase-modal select[name='ticker']",
            "#stock-purchase-modal select[name='symbol']",
            "select[name='ticker']",
            "select[name='symbol']",
            "select[name='stock']",
        ]
        for css in select_css:
            loc = page.locator(css)
            for i in range(loc.count()):
                el = loc.nth(i)
                visible = False
                with contextlib.suppress(Exception):
                    visible = el.is_visible()
                if not visible:
                    continue
                for attempt in (
                    {"value": target},
                    {"label": target},
                    {"value": raw},
                    {"label": raw},
                ):
                    with contextlib.suppress(Exception):
                        el.select_option(**attempt, timeout=5000)
                        return
                opts = el.locator("option").all()
                for opt in opts:
                    with contextlib.suppress(Exception):
                        val = (opt.get_attribute("value") or "").strip()
                        lab = (opt.inner_text() or "").strip().upper()
                        if val.upper() == target or lab == target:
                            el.select_option(value=val)
                            return
        inp = self._first_visible(
            page,
            [
                "#stock-purchase-modal input[name='ticker']",
                "#stock-purchase-modal input[name*='ticker' i]",
                "input[name='ticker']",
                "input[name*=ticker i]",
                "input[name*=symbol i]",
            ],
        )
        if inp is not None:
            inp.fill(target)
            return
        raise RuntimeError(f"Could not set ticker field for {target!r} (no visible select option or input)")

    def _page_is_logged_out(self, page: Page) -> bool:
        """Cheap check: did the platform bounce us back to the public site?"""
        with contextlib.suppress(Exception):
            url = (page.url or "").lower()
            if "/private/" not in url:
                return True
        with contextlib.suppress(Exception):
            if "You have to log in" in page.content():
                return True
        return False

    def parse_game_page_html_to_snapshot(
        self, html: str, *, log_if_suspicious: bool = True
    ) -> RemoteAccountSnapshot:
        """Parse a ``/private/game`` (or post-trade redirect) HTML body into a snapshot."""
        soup = BeautifulSoup(html, "html.parser")
        cash = self._extract_metric(html, ["cash balance", "cash", "available cash", "buying power"])
        loan_balance = self._extract_metric(html, ["loan balance", "loan", "margin loan", "debt"]) or 0.0
        fees_paid = self._extract_metric(
            html, ["transaction fees paid", "fees paid", "transaction fees"]
        ) or 0.0
        holdings_value = self._extract_metric(
            html, ["holdings", "total holdings", "portfolio value"]
        ) or 0.0
        cash_missing = cash is None
        if cash_missing:
            cash = 0.0

        positions: list[RemotePosition] = []
        for table in soup.select("table"):
            header = [c.get_text(" ", strip=True).lower() for c in table.select("tr th")]
            if not header:
                continue
            if "exchange" not in header or "ticker" not in header or "quantity" not in header:
                continue
            for row in table.select("tr")[1:]:
                cols = [c.get_text(" ", strip=True) for c in row.find_all("td")]
                if len(cols) < 5:
                    continue
                ticker = cols[1].strip().upper()
                qty_text = cols[2].replace(",", "")
                if not ticker or not qty_text.isdigit():
                    continue
                qty = int(qty_text)
                price = self._parse_currency(cols[4])
                if qty > 0:
                    positions.append(RemotePosition(symbol=ticker, shares=qty, avg_price=price))

        if log_if_suspicious and (cash_missing or (cash == 0.0 and not positions)):
            sample = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:400]
            logger.warning(
                "InvestJA snapshot parsed empty (cash_missing=%s, cash=%.2f, positions=%d). "
                "Page text sample: %s",
                cash_missing, cash, len(positions), sample,
            )

        return RemoteAccountSnapshot(
            cash=cash,
            loan_balance=loan_balance,
            positions=positions,
            fees_paid=fees_paid,
            holdings_value=holdings_value,
        )

    def get_snapshot(self) -> RemoteAccountSnapshot:
        page = self._ensure_session()
        try:
            try:
                page.goto(self._abs("/private/game"), wait_until="domcontentloaded", timeout=12000)
            except PlaywrightTimeoutError:
                logger.warning("snapshot: /private/game load timed out; continuing")
            # Detect a silent session expiry that bounced us back to login.
            if self._page_is_logged_out(page):
                logger.info("InvestJA session expired; re-authenticating")
                self._logged_in = False
                self._ensure_session()
                page.goto(self._abs("/private/game"), wait_until="domcontentloaded", timeout=12000)
            with contextlib.suppress(PlaywrightTimeoutError, Exception):
                page.wait_for_selector("text=/cash/i", timeout=2000)
            page.wait_for_timeout(80)
            html = page.content()
        except Exception:
            self._reset_session()
            raise

        return self.parse_game_page_html_to_snapshot(html, log_if_suspicious=True)

    def _page_has_position(self, html: str, ticker: str, min_qty: int) -> bool:
        return self._extract_position_qty(html, ticker) >= min_qty

    def _extract_position_qty(self, html: str, ticker: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        target = ticker.strip().upper()
        for table in soup.select("table"):
            header = [c.get_text(" ", strip=True).lower() for c in table.select("tr th")]
            if "ticker" not in header or "quantity" not in header:
                continue
            for row in table.select("tr")[1:]:
                cols = [c.get_text(" ", strip=True) for c in row.find_all("td")]
                if len(cols) < 3:
                    continue
                row_ticker = cols[1].strip().upper() if len(cols) > 1 else ""
                qty_text = cols[2].replace(",", "") if len(cols) > 2 else ""
                qty = int(qty_text) if qty_text.isdigit() else 0
                if row_ticker == target:
                    return qty
        return 0

    def _holdings_exchange_and_qty_from_html(self, html: str, ticker: str) -> tuple[str, int] | None:
        """Parse the Holdings table: return (exchange cell text, quantity) for ``ticker``, or None.

        Equivalent to visually confirming the symbol is in the portfolio section
        before building the ``/private/game/sell?...`` URL.
        """
        target = re.sub(r"[^A-Z0-9\.]", "", ticker.strip().upper())
        soup = BeautifulSoup(html, "html.parser")
        for table in soup.select("table"):
            header = [c.get_text(" ", strip=True).lower() for c in table.select("tr th")]
            if "exchange" not in header or "ticker" not in header or "quantity" not in header:
                continue
            ie = header.index("exchange")
            it = header.index("ticker")
            iq = header.index("quantity")
            for row in table.select("tr")[1:]:
                cols = row.find_all("td")
                if max(ie, it, iq) >= len(cols):
                    continue
                raw_t = cols[it].get_text(" ", strip=True)
                norm_t = re.sub(r"[^A-Z0-9\.]", "", raw_t.upper())
                if norm_t != target:
                    continue
                qty_txt = cols[iq].get_text(" ", strip=True).replace(",", "")
                if not qty_txt.isdigit():
                    continue
                q = int(qty_txt)
                if q <= 0:
                    continue
                ex_cell = cols[ie].get_text(" ", strip=True)
                return (ex_cell, q)
        return None

    def _coalesce_exchange_for_sell(self, cell: str, hint: str) -> str:
        """Map a holdings-table exchange label + env hint to NASDAQ / NYSE / TSX."""
        u = (cell or "").strip().upper()
        h = (hint or "NASDAQ").strip().upper()
        if "TSX" in u or "TOR" in u or "TORONTO" in u or "VENTURE" in u:
            return "TSX"
        if "NYSE" in u or u == "NY":
            return "NYSE"
        if "NASDAQ" in u or "NASD" in u or "NQ" == u:
            return "NASDAQ"
        if h in {"NASDAQ", "NYSE", "TSX"}:
            return h
        return "NASDAQ"

    def sell_holding_via_url_after_gamepage_check(
        self, ticker: str, exchange_hint: str, quantity_hint: int
    ) -> RemoteAccountSnapshot:
        """Load ``/private/game``, confirm ``ticker`` is in Holdings, then sell via prefilled sell URL.

        Uses exchange + quantity parsed from the same table row so the query
        string matches what the site expects (e.g. ``exchange=TSX`` for X.TO names).

        Returns a snapshot parsed from the post-trade page (avoids an extra
        ``/private/game`` round-trip for the caller).
        """
        target = ticker.strip().upper()
        page = self._ensure_session()
        try:
            page.goto(self._abs("/private/game"), wait_until="domcontentloaded", timeout=15000)
            if self._page_is_logged_out(page):
                self._logged_in = False
                self._ensure_session()
                page.goto(self._abs("/private/game"), wait_until="domcontentloaded", timeout=15000)
            with contextlib.suppress(PlaywrightTimeoutError, Exception):
                page.wait_for_selector("text=/ticker/i", timeout=2500)
            page.wait_for_timeout(50)
            html = page.content()
        except Exception:
            self._reset_session()
            raise

        row = self._holdings_exchange_and_qty_from_html(html, target)
        if row is None:
            raise RuntimeError(
                f"{target} not found in Holdings on the game page — "
                "refusing sell URL (nothing to match in the portfolio table)"
            )
        ex_cell, qty_table = row
        final_exch = self._coalesce_exchange_for_sell(ex_cell, exchange_hint)
        final_qty = int(qty_table)
        if quantity_hint > 0 and final_qty != int(quantity_hint):
            logger.info(
                "InvestJA: %s Holdings qty=%s differs from cache=%s; using Holdings (sell URL qty)",
                target,
                final_qty,
                quantity_hint,
            )
        if final_qty <= 0:
            raise RuntimeError(f"{target}: quantity in Holdings is 0")
        return self.place_sell_row(ticker=target, exchange=final_exch, quantity=final_qty)

    def place_sell_row(self, ticker: str, exchange: str, quantity: int) -> RemoteAccountSnapshot:
        """Sell ``quantity`` shares of ``ticker`` via the direct sell URL.

        Three-step flow that mirrors the manual user experience exactly:

          1. GET ``/private/game/sell?exchange=<X>&ticker=<T>&quantity=<N>``
             — page renders prefilled (Exchange / Ticker / Quantity).
          2. Click ``<input type="submit" name="Sell" value="Sell">`` — that
             navigates to a "Confirm share sale" page.
          3. Click the final ``#purchase_stock_now`` submit
             (``name="Buy" value="Yes, sell these stocks,"``) which actually
             executes the sale and redirects back to ``/private/game``.

        If the platform refuses with the "less then 1 hour ago" rule, raise
        :class:`SellHoldError` so the spam loop can mark the ticker as
        hold-blocked and log a friendly message instead of treating it as a
        generic failure.

        Returns a :class:`RemoteAccountSnapshot` parsed from the loaded page
        after a successful sell (no extra navigation).
        """
        target = ticker.strip().upper()
        if quantity <= 0:
            raise RuntimeError(f"Refusing SELL {target} with quantity={quantity}")
        ex_url = self._EXCHANGE_URL_NAME.get(exchange.strip().upper(), exchange)
        sell_url = self._abs(
            f"/private/game/sell?exchange={ex_url}&ticker={target}&quantity={quantity}"
        )

        page = self._ensure_session()
        try:
            # Step 1: load the prefilled sell form.
            page.goto(sell_url, wait_until="domcontentloaded", timeout=15000)
            if self._page_is_logged_out(page):
                self._logged_in = False
                self._ensure_session()
                page.goto(sell_url, wait_until="domcontentloaded", timeout=15000)

            # The 1-hour cooldown manifests as a banner alert near the top.
            self._raise_if_hold(page, target)

            # Step 2: submit the prefilled form to reach the confirm page.
            submit = page.locator(
                "input[type='submit'][name='Sell'], "
                "button[name='Sell'], "
                "input[type='submit'][value='Sell']"
            ).first
            try:
                submit.wait_for(state="visible", timeout=6000)
            except PlaywrightTimeoutError:
                # If the submit isn't there, either the URL was rejected or
                # the markup changed. Surface the visible reason.
                self._raise_if_hold(page, target)
                alerts = self._visible_alerts(page)
                detail = " | ".join(alerts) if alerts else "no message"
                raise RuntimeError(f"Sell submit not found for {target}: {detail}")
            submit.click()
            page.wait_for_load_state("domcontentloaded")

            # The hold rule can also be reported on the confirm page (rare),
            # so check again after navigation.
            self._raise_if_hold(page, target)

            # Step 3: the final "Yes, sell these stocks," confirm. Note the
            # platform reuses ``name="Buy"`` for both buy/sell confirmations.
            confirm = page.locator(
                "input[type='submit']#purchase_stock_now, "
                "input[type='submit'][value*='sell these stocks' i], "
                "button#purchase_stock_now"
            ).first
            try:
                confirm.wait_for(state="visible", timeout=6000)
            except PlaywrightTimeoutError:
                self._raise_if_hold(page, target)
                alerts = self._visible_alerts(page)
                detail = " | ".join(alerts) if alerts else "no message"
                raise RuntimeError(f"Sell final confirm not found for {target}: {detail}")
            confirm.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(50)
            page_html = page.content()
            body = page_html.lower()
        except SellHoldError:
            # Routine platform refusal — keep the persistent session alive.
            raise
        except Exception:
            self._reset_session()
            raise

        if _html_shows_post_buy_sell_hold(body):
            raise SellHoldError(target)

        success_markers = [
            "sale was completed successfully",
            "your stock sale was completed successfully",
            "your transaction has been completed",
        ]
        if any(m in body for m in success_markers):
            return self.parse_game_page_html_to_snapshot(page_html, log_if_suspicious=False)
        # Fall back: if the holdings row no longer shows the previous quantity,
        # the sale executed and we just got redirected straight back.
        if self._extract_position_qty(page_html, target) < quantity:
            return self.parse_game_page_html_to_snapshot(page_html, log_if_suspicious=False)
        raise RuntimeError(f"InvestJA did not confirm sell for {target}")

    def _raise_if_hold(self, page: Page, target: str) -> None:
        """Inspect alerts / page for the real buy-then-sell cooldown (not unrelated '1 hour' text)."""
        blob_parts: list[str] = []
        try:
            blob_parts.extend(self._visible_alerts(page))
        except Exception:
            pass
        blob = " ".join(blob_parts)
        if _html_shows_post_buy_sell_hold(blob):
            raise SellHoldError(target)
        try:
            html = page.content()
        except Exception:
            return
        if _html_shows_post_buy_sell_hold(html):
            raise SellHoldError(target)

    def _visible_alerts(self, page: Page) -> list[str]:
        try:
            return list(
                page.evaluate(
                    """Array.from(document.querySelectorAll('.alert,.invalid-feedback,.text-danger,.toast,p,h3,h4'))
                    .map(e => (e.innerText || '').trim())
                    .filter(t => t && t.length < 240).slice(0,8)"""
                )
                or []
            )
        except Exception:
            return []

    def _click_holdings_row_sell(self, page: Page, ticker: str) -> bool:
        """Click the SELL control on the dashboard *Holdings* row for ``ticker`` (exact row).

        JA shows Exchange, Ticker, Quantity, … — ticker is typically the 2nd ``<td>``.
        Falls back to any row whose cell ``text-is`` matches the ticker alone.
        """
        t = ticker.strip().upper()
        row = page.locator("table:visible tr").filter(has=page.locator(f"td:nth-child(2):text-is('{t}')")).first
        try:
            row.wait_for(state="visible", timeout=4000)
        except Exception:
            row = page.locator("table:visible tr").filter(has=page.locator(f"td:text-is('{t}')")).first
            try:
                row.wait_for(state="visible", timeout=4000)
            except Exception:
                return False
        sell_ctrl = row.locator("a, button").filter(has_text=re.compile(r"^\s*sell\s*$", re.I)).first
        try:
            sell_ctrl.wait_for(state="visible", timeout=4000)
            sell_ctrl.click(timeout=8000)
            return True
        except Exception:
            return False

    def place_order(self, *, side: str, exchange: str, ticker: str, quantity: int) -> None:
        normalized_exchange = exchange.strip().upper()
        if normalized_exchange not in {"NASDAQ", "NYSE", "TSX"}:
            normalized_exchange = "NASDAQ"

        page = self._ensure_session()
        try:
            page.goto(self._abs("/private/game"), wait_until="domcontentloaded")
            page.wait_for_timeout(120)
            pre_qty = self._extract_position_qty(page.content(), ticker)

            # The game shows the order form only after clicking the top action button.
            if side == "buy":
                if self._first_visible_exchange_select(page) is None:
                    purchase_btn = self._first_visible(
                        page,
                        [
                            "button:has-text('Purchase stocks')",
                            "a:has-text('Purchase stocks')",
                        ],
                    )
                    if purchase_btn is None:
                        raise RuntimeError("Could not find 'Purchase stocks' to open the buy form")
                    purchase_btn.click()
                    page.wait_for_timeout(200)
                    with contextlib.suppress(Exception):
                        page.locator("#stock-purchase-modal").first.wait_for(state="visible", timeout=8000)
            elif side == "sell":
                if self._click_holdings_row_sell(page, ticker):
                    page.wait_for_timeout(150)
                else:
                    row_sell = self._first_visible(
                        page,
                        [
                            f"tr:has(td:text-is('{ticker.upper()}')) button:has-text('Sell')",
                            f"tr:has(td:text-is('{ticker.upper()}')) a:has-text('Sell')",
                            f"tr:has-text('{ticker.upper()}') button:has-text('Sell')",
                            f"tr:has-text('{ticker.upper()}') a:has-text('Sell')",
                        ],
                    )
                    if row_sell is not None:
                        row_sell.click()
                        page.wait_for_timeout(120)
                    else:
                        ticker_probe = self._first_visible(
                            page,
                            ["input[name='ticker']", "input[name*=ticker i]", "input[name*=symbol i]"],
                        )
                        qty_probe = self._first_visible(
                            page,
                            ["input[name='quantity']", "input[name*=quantity i]", "input[name*=qty i]"],
                        )
                        if ticker_probe is None or qty_probe is None:
                            open_btn = self._first_visible(
                                page,
                                ["button:has-text('SELL')", "a:has-text('SELL')"],
                            )
                            if open_btn is not None:
                                open_btn.click()
                                page.wait_for_timeout(120)

            self._select_exchange(page, normalized_exchange, side)

            if side == "buy":
                page.wait_for_timeout(280)

            self._set_ticker(page, ticker)

            qty_input = self._first_visible(
                page,
                [
                    "#stock-purchase-modal input[name='quantity']",
                    "#stock-purchase-modal input[name*='quantity' i]",
                    "input[name='quantity']",
                    "input[name*=quantity i]",
                    "input[name*=qty i]",
                ],
            )
            if qty_input is None:
                raise RuntimeError(f"Could not find quantity field for {side}")
            qty_input.fill(str(max(1, quantity)))

            action_text = "Purchase" if side == "buy" else "Sell"
            buy_selectors = [
                "#stock-purchase-modal button:has-text('Purchase')",
                "#stock-purchase-modal input[type='submit'][value*='Purchase' i]",
                "button:has-text('Purchase'):not(:has-text('Purchase stocks'))",
            ]
            sell_selectors = [
                "button:has-text('Sell')",
                "input[type='submit'][value*='Sell' i]",
            ]
            action_btn = self._first_visible(page, buy_selectors if side == "buy" else sell_selectors)
            if action_btn is None:
                raise RuntimeError(f"Could not find primary {action_text} button")
            action_btn.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(120)

            confirm_locator = page.locator(
                "button:has-text('attempt this'), "
                "a:has-text('attempt this'), "
                "input[type='submit'][value*='attempt this' i]"
            ).first
            try:
                confirm_locator.wait_for(state="visible", timeout=8000)
            except PlaywrightTimeoutError:
                alerts = page.evaluate(
                    """Array.from(document.querySelectorAll('.alert,.invalid-feedback,.text-danger,.modal-body,.toast'))
                    .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0,6)"""
                )
                detail = " | ".join(alerts) if alerts else "no validation message found"
                raise RuntimeError(
                    f"Confirm modal did not appear for {side.upper()} {ticker} qty={quantity}: {detail}"
                )
            label = ""
            with contextlib.suppress(Exception):
                label = (confirm_locator.inner_text() or "").strip()
            if not label:
                with contextlib.suppress(Exception):
                    label = (confirm_locator.get_attribute("value") or "").strip()
            if "attempt this" not in label.lower():
                raise RuntimeError(f"Wrong confirm control matched: {label!r}")
            confirm_locator.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(120)
            body = page.content().lower()
        except Exception:
            self._reset_session()
            raise
        success_markers = [
            "purchase was completed successfully",
            "sale was completed successfully",
            "your stock purchase was completed successfully",
            "your stock sale was completed successfully",
        ]
        # InvestJA can partially fill or cap quantities; any increase in held shares
        # means the buy was executed and should be treated as success.
        inferred_from_positions = side == "buy" and self._page_has_position(body, ticker, pre_qty + 1)
        if not any(m in body for m in success_markers) and not inferred_from_positions:
            raise RuntimeError(f"InvestJA did not confirm {side} success for {ticker}")
