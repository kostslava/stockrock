# StockRock Memory

## Project Goal

Automate InvestJA trading with:

- Market data + forecasting (TimesFM primary)
- Strategy selection across many stocks
- Live InvestJA browser execution (not paper-only)
- Telegram notifications + approval controls
- Terminal GUI dashboard

## Current Working State

- Bot can run end-to-end and place real InvestJA buys in `BROKER_MODE=investja`.
- TimesFM model is running from local cache after initial download.
- Telegram approvals and explain flow are wired (`Yes / No / Explain why`).
- UI has live updates and status animation while cycles are running.

## Critical Files

- `stockrock/main.py`: app entrypoint, runtime wiring
- `stockrock/engine.py`: core cycle logic (forecast -> decision -> approval -> execution)
- `stockrock/investja.py`: Playwright browser automation for InvestJA
- `stockrock/broker.py`: live/paper broker behavior, retry sizing logic
- `stockrock/model.py`: TimesFM/momentum forecasting
- `stockrock/notifier.py`: Telegram send + callback approval polling
- `stockrock/ai.py`: OpenAI summary/explain generation
- `stockrock/ui.py`: terminal GUI

## InvestJA Execution Notes

- Must use Playwright browser flow (manual-equivalent):
  1. Login
  2. Open `/private/game`
  3. Click `Purchase stocks`
  4. Select exchange (`NASDAQ`/`NYSE`/`TSX`)
  5. Fill ticker + quantity
  6. Click `Purchase`
  7. Click final `Yes, attempt this purchase now...` confirm
- Confirm control text/element type varies (button/anchor + punctuation), selectors already broadened.
- Success text on InvestJA can be inconsistent; bot also infers success via post-action holdings/cash.
- Large order sizes can fail; broker retries smaller quantities automatically.

## Frequent Failure Modes (Already Encountered)

- `BROKER_MODE=paper` accidentally set -> no real orders.
- Stock not actually tradable on InvestJA -> order confirm/success fails.
- Huge quantity attempts rejected intermittently -> retry strategy needed.
- Telegram approval waiting can look like freeze if UI not updating.
- OpenAI response may return incomplete with low output tokens -> solved via higher token budget and robust parsing.

## Environment Variables That Matter Most

- `BROKER_MODE=investja`
- `INVESTJA_USERNAME`, `INVESTJA_PASSWORD`
- `INVESTJA_HEADLESS=true|false`
- `MODEL_PROVIDER=timesfm`
- `STOCK_UNIVERSE=...` (`TICKER|YAHOO_SYMBOL|EXCHANGE` list)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `TELEGRAM_REQUIRE_APPROVAL=true|false`
- `OPENAI_API_KEY`
- `OPENAI_MODEL=gpt-5-nano-2025-08-07`

## Current Stock Universe Approach

- Universe is expanded to many NASDAQ/NYSE/TSX symbols.
- Engine forecasts all candidates, ranks them, and picks the best expected return above thresholds.
- Telegram receives formatted proposal for approval.

## Commands

- One cycle: `stockrock --once`
- Continuous: `stockrock`
- Plain logs: `stockrock --plain`

## Operational Guidance for New Agents

1. Do not revert to paper broker unless explicitly asked.
2. Preserve Playwright execution path; avoid switching back to raw HTTP form posting.
3. If execution fails, inspect actual page state/messages and selector behavior first.
4. Keep decision/report consistency: if buy fails, do not report successful trade.
5. Keep Telegram message formatting readable (Markdown) and callback handling scoped to the current message.
6. Validate changes with a real `--once` run and confirm account snapshot after execution.
