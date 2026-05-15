# StockRock

StockRock is an automated trading assistant for the InvestJA competition workflow.

It is designed to:

- Pull live market data from Yahoo Finance
- Forecast short-term direction (TimesFM when available, fallback model otherwise)
- Trade fee-aware opportunities from a configurable stock universe
- Apply a configurable risk dial to position sizing
- Send Telegram alerts for all buy/sell actions

## Important Notes

- This project is educational software and not financial advice.
- InvestJA mode now uses authenticated web requests to scrape account data and submit buy/sell forms.
- TimesFM support is optional; if unavailable, StockRock falls back to a momentum baseline model.

## Quick Start

1. Create a Python 3.11 virtual environment and install dependencies:

```bash
python3.11 -m venv .venv311
source .venv311/bin/activate
pip install -r requirements.txt
pip install -e .   # installs the `stockrock` CLI into this venv (same as `python -m stockrock.main`)
pip install torch
pip install "git+https://github.com/google-research/timesfm.git"
playwright install chromium
```

After `pip install -e .`, you can run the bot as `stockrock` instead of `python -m stockrock.main` (same flags: `--once`, `--plain`, `--spam`, etc.).

2. Copy env template:

```bash
cp .env.example .env
```

3. Fill required fields in `.env`.

Telegram setup:

```bash
python -m stockrock.telegram_setup
```

Then message your bot in Telegram (for example, send `/start`) and run the command again to discover your `chat_id`. Put that value in `TELEGRAM_CHAT_ID`.

4. Run one cycle (Rich terminal dashboard by default):

```bash
stockrock --once
# equivalent: python -m stockrock.main --once
```

5. Run continuously (default 15-minute interval; live-refreshed dashboard):

```bash
stockrock
```

6. Plain logs only (no dashboard):

```bash
stockrock --plain
```

7. Goon (spam) mode — drains account balance via maximum-fee trades for the
   InvestJA "lose-the-most" sub-contest. Bypasses forecaster, advisor, and
   Telegram approval. Does **not** affect the normal trading flow above.

```bash
stockrock --spam            # loops, sleeping POLL_SECONDS between passes
stockrock --spam --once     # single pass through the universe
```

Behavior:

- For each tradable ticker, executes 3 transactions per day (the platform cap),
  ending as close to flat as possible: SELL/BUY/SELL if currently long, else
  BUY/SELL/BUY (ends +1 share, cleared next day).
- Tickers are processed cheapest-first so 1-share lots remain affordable as cash
  drains.
- Daily transaction counts per ticker are persisted to `~/.stockrock/spam_state.json`
  so reruns the same day respect the 3-txn cap.
- Uses the same `STOCK_UNIVERSE` env var as normal mode; expand it to drain
  faster (more tickers × $75/day = more fees burned).

## Config

Key environment variables:

- `RISK_DIAL`: float from `0.0` to `1.0`, controls portfolio fraction per trade.
- `TRADE_FEE`: fixed fee per trade in dollars (defaults to `25.0`).
- `BASE_CURRENCY`: reporting currency label.
- `OIL_PROXY_SYMBOL`: market proxy for oil direction (`CL=F` by default).
- `STOCK_UNIVERSE`: comma-separated list in `TICKER|YAHOO_SYMBOL|EXCHANGE` format.
  - Exchange must be one of `NASDAQ`, `NYSE`, `TSX`.
- `MIN_EXPECTED_PROFIT_MULTIPLE`: expected-profit-to-fee minimum, used to avoid weak trades.
- `OPENAI_API_KEY`, `OPENAI_MODEL`: AI summary/explain model for approvals.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: Telegram alert credentials.

## TimesFM

StockRock tries to use TimesFM if `MODEL_PROVIDER=timesfm` and the package is installed.
If TimesFM import or inference fails, it logs a warning and gracefully falls back to momentum forecasting.
The tested path here is Python 3.11 with `torch` and latest TimesFM from GitHub.

## Telegram Approval Flow

Before executing a buy trade, the bot sends:

- AI summary (GPT-5 nano)
- Buttons: `Yes`, `No`, `Explain why`

`Explain why` sends detailed technical metrics and forecast context. If approval times out, trade is cancelled.

## InvestJA Browser Execution

In `BROKER_MODE=investja`, orders are executed in a real browser flow:

1. Log in with team username/password
2. Open `/private/game?tab=purchase|sell&exchange=...&ticker=...&quantity=...`
3. Select exchange from dropdown
4. Fill ticker and quantity
5. Click `Purchase` or `Sell`
6. Click final confirmation (`Yes, attempt this ... now`)

## Project Structure

- `stockrock/config.py`: environment and runtime settings
- `stockrock/data.py`: Yahoo Finance market data adapter
- `stockrock/model.py`: forecast providers (TimesFM + fallback)
- `stockrock/strategy.py`: oil rotation strategy logic
- `stockrock/portfolio.py`: fee-aware paper portfolio state
- `stockrock/broker.py`: execution adapter (paper broker + InvestJA stub)
- `stockrock/notifier.py`: Telegram notifications
- `stockrock/engine.py`: orchestration loop
- `stockrock/main.py`: CLI entrypoint
