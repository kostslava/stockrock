from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForecastResult:
    expected_return: float
    confidence: float
    provider: str


class Forecaster(Protocol):
    def predict_direction(self, prices: np.ndarray, horizon: int) -> ForecastResult:
        ...


class MomentumForecaster:
    def predict_direction(self, prices: np.ndarray, horizon: int) -> ForecastResult:
        lookback = min(48, prices.size - 1)
        if lookback < 5:
            return ForecastResult(expected_return=0.0, confidence=0.0, provider="momentum")
        recent = prices[-lookback:]
        start = float(recent[0])
        end = float(recent[-1])
        expected_return = (end - start) / start
        confidence = min(1.0, abs(expected_return) * 12.0)
        return ForecastResult(expected_return=expected_return, confidence=confidence, provider="momentum")


class TimesFMForecaster:
    def __init__(self) -> None:
        self._fallback = MomentumForecaster()
        self._model = None
        self._provider_name = "timesfm"
        try:
            import timesfm
            self._timesfm_module = timesfm
            self._load_model()
        except Exception as exc:
            self._timesfm_module = None
            logger.warning(
                "TimesFM unavailable on Python %s, using fallback: %s",
                ".".join(str(v) for v in sys.version_info[:3]),
                exc,
            )

    def _load_model(self) -> None:
        if self._timesfm_module is None:
            return
        timesfm = self._timesfm_module
        model_cls = getattr(timesfm, "TimesFM_2p5_200M_torch", None)
        if model_cls is None:
            raise RuntimeError("Installed timesfm package does not expose TimesFM_2p5_200M_torch")
        self._model = model_cls.from_pretrained("google/timesfm-2.5-200m-pytorch")
        self._model.compile(
            timesfm.ForecastConfig(
                max_context=1024,
                max_horizon=256,
                normalize_inputs=True,
                use_continuous_quantile_head=True,
                force_flip_invariance=True,
                infer_is_positive=False,
                fix_quantile_crossing=True,
            )
        )
        self._provider_name = "timesfm-2.5-200m-pytorch"

    def predict_direction(self, prices: np.ndarray, horizon: int) -> ForecastResult:
        if self._timesfm_module is None or self._model is None:
            return self._fallback.predict_direction(prices, horizon)

        try:
            horizon = max(1, int(horizon))
            recent = prices[-512:] if prices.size > 512 else prices
            if recent.size < 32:
                return self._fallback.predict_direction(prices, horizon)
            point, _quantiles = self._model.forecast(
                horizon=max(1, min(256, horizon)),
                inputs=[recent.astype(float)],
            )
            if point.size == 0:
                return self._fallback.predict_direction(prices, horizon)
            # point shape: (batch, horizon_len)
            forecast_price = float(point[0][min(horizon - 1, point.shape[1] - 1)])
            current_price = float(recent[-1])
            if current_price <= 0:
                return self._fallback.predict_direction(prices, horizon)
            expected_return = (forecast_price - current_price) / current_price
            confidence = min(1.0, abs(expected_return) * 15.0)
            return ForecastResult(expected_return=expected_return, confidence=confidence, provider=self._provider_name)
        except Exception as exc:
            logger.warning("TimesFM inference failed, fallback active: %s", exc)
            return self._fallback.predict_direction(prices, horizon)


def build_forecaster(provider: str) -> Forecaster:
    normalized = provider.strip().lower()
    if normalized == "timesfm":
        return TimesFMForecaster()
    return MomentumForecaster()
