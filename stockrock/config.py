from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    risk_dial: float
    trade_fee: float
    base_currency: str
    poll_seconds: int
    model_provider: str
    oil_proxy_symbol: str
    bull_symbol: str
    bear_symbol: str
    stock_universe: str
    stock_universe_csv: str
    min_expected_profit_multiple: float
    min_position_dollars: float
    lookback_bars: int
    forecast_horizon: int
    max_symbols_per_cycle: int
    buy_threshold: float
    min_forecast_confidence: float
    max_active_positions: int
    sell_threshold: float
    min_take_profit_pct: float
    initial_cash: float
    broker_mode: str
    investja_username: str
    investja_password: str
    investja_loan_cap: float
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_approval_timeout_sec: int
    telegram_require_approval: bool
    openai_api_key: str
    openai_model: str


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    load_dotenv()
    risk = max(0.0, min(1.0, _env_float("RISK_DIAL", 0.35)))
    return Settings(
        risk_dial=risk,
        trade_fee=_env_float("TRADE_FEE", 25.0),
        base_currency=os.getenv("BASE_CURRENCY", "CAD"),
        poll_seconds=_env_int("POLL_SECONDS", 60),
        model_provider=os.getenv("MODEL_PROVIDER", "timesfm").strip().lower(),
        oil_proxy_symbol=os.getenv("OIL_PROXY_SYMBOL", "CL=F"),
        bull_symbol=os.getenv("BULL_SYMBOL", "HOU.TO"),
        bear_symbol=os.getenv("BEAR_SYMBOL", "HOD.TO"),
        stock_universe_csv=os.getenv("STOCK_UNIVERSE_CSV", "stocks.csv"),
        stock_universe=os.getenv(
            "STOCK_UNIVERSE",
            (
                "PLTR|PLTR|NASDAQ,CENN|CENN|NASDAQ,SNDL|SNDL|NASDAQ,ACB|ACB|NASDAQ,OGI|OGI|NASDAQ,"
                "SOFI|SOFI|NASDAQ,RIVN|RIVN|NASDAQ,LCID|LCID|NASDAQ,IONQ|IONQ|NYSE,ACHR|ACHR|NYSE,"
                "RKLB|RKLB|NASDAQ,JOBY|JOBY|NYSE,NIO|NIO|NYSE,QS|QS|NYSE,BB|BB|NYSE,F|F|NYSE,"
                "AAL|AAL|NASDAQ,CCL|CCL|NYSE,NOK|NOK|NYSE,TLRY|TLRY|NASDAQ,CGC|CGC|NASDAQ,"
                "HIVE|HIVE|TSX,HUT|HUT|TSX,BBD.B|BBD.B|TSX,AMD|AMD|NASDAQ,NVDA|NVDA|NASDAQ,"
                "TSLA|TSLA|NASDAQ,META|META|NASDAQ,AMZN|AMZN|NASDAQ,MSFT|MSFT|NASDAQ,"
                "AAPL|AAPL|NASDAQ,GOOGL|GOOGL|NASDAQ,SMCI|SMCI|NASDAQ,MARA|MARA|NASDAQ,"
                "RIOT|RIOT|NASDAQ,COIN|COIN|NASDAQ,PYPL|PYPL|NASDAQ,SHOP|SHOP|NASDAQ,"
                "UBER|UBER|NYSE,SNAP|SNAP|NYSE,DKNG|DKNG|NASDAQ,CRWD|CRWD|NASDAQ,NET|NET|NYSE"
            ),
        ),
        min_expected_profit_multiple=_env_float("MIN_EXPECTED_PROFIT_MULTIPLE", 2.5),
        min_position_dollars=_env_float("MIN_POSITION_DOLLARS", 1000.0),
        lookback_bars=_env_int("LOOKBACK_BARS", 256),
        forecast_horizon=_env_int("FORECAST_HORIZON", 8),
        max_symbols_per_cycle=max(3, _env_int("MAX_SYMBOLS_PER_CYCLE", 200)),
        buy_threshold=_env_float("BUY_THRESHOLD", 0.004),
        min_forecast_confidence=_env_float("MIN_FORECAST_CONFIDENCE", 0.20),
        max_active_positions=max(1, _env_int("MAX_ACTIVE_POSITIONS", 2)),
        sell_threshold=_env_float("SELL_THRESHOLD", -0.004),
        min_take_profit_pct=_env_float("MIN_TAKE_PROFIT_PCT", 0.03),
        initial_cash=_env_float("INITIAL_CASH", 100000.0),
        broker_mode=os.getenv("BROKER_MODE", "paper").strip().lower(),
        investja_username=os.getenv("INVESTJA_USERNAME", ""),
        investja_password=os.getenv("INVESTJA_PASSWORD", ""),
        investja_loan_cap=_env_float("INVESTJA_LOAN_CAP", 50000.0),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_approval_timeout_sec=_env_int("TELEGRAM_APPROVAL_TIMEOUT_SEC", 300),
        telegram_require_approval=_env_bool("TELEGRAM_REQUIRE_APPROVAL", False),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5-nano-2025-08-07"),
    )
