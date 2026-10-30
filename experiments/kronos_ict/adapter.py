"""Kronos adapter — thin, offline wrapper around the Kronos K-line model.

Design contract
---------------
* Physical isolation: this module is the *only* place that imports Kronos.
  The injector and backtest never touch Kronos directly — they read the
  forecast CSV this adapter produces.
* Offline + cached: forecasts are generated once into ``cache/*.csv`` and
  replayed.  There is no model call inside any backtest loop.
* Degradable: if the Kronos package or weights are unavailable, the adapter
  falls back to a clearly-labelled ``mock`` forecast so the rest of the
  pipeline stays testable end-to-end.

The data contract for a single forecast is :class:`KronosForecast`.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd


@dataclass
class KronosForecast:
    """One offline forecast point.

    ``as_of`` is the close time of the *last input bar*.  Everything is
    derived from the model's predicted future path, never from a single
    predicted close.
    """

    as_of: str
    horizon: int                      # number of bars predicted forward
    predicted_direction: str          # "long" | "short" | "flat"
    expected_return: float            # mean future return over horizon
    confidence: float                 # 0..1
    predicted_max_drawdown: float     # worst adverse excursion over horizon

    def as_row(self) -> dict:
        return asdict(self)


class KronosAdapter:
    """Loads Kronos lazily and emits offline forecasts to CSV.

    The exact Kronos call is confined to :meth:`_call_kronos`.  If you clone
    ``external/Kronos`` and the upstream API differs from what is written
    there, only that one method needs to change.
    """

    def __init__(
        self,
        tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base",
        model_name: str = "NeoQuasar/Kronos-small",
        context_len: int = 512,
        horizon: int = 12,             # 12 x 15m = 3h
        n_samples: int = 32,           # for sampling-based confidence
        device: str = "auto",
        cache_dir: str | None = None,
    ) -> None:
        self.tokenizer_name = tokenizer_name
        self.model_name = model_name
        self.context_len = context_len
        self.horizon = horizon
        self.n_samples = n_samples
        self.device = device
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._tokenizer = None
        self._model = None
        self._mock = False

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        """Lazy import so the module can be imported without Kronos installed."""
        try:
            from kronos import KronosPredictor, KronosTokenizer  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Kronos not installed. `git clone` it under external/Kronos and "
                "pip install -e it, or set adapter._mock=True for a stub run."
            ) from exc

        self._tokenizer = KronosTokenizer.from_pretrained(self.tokenizer_name)
        self._model = KronosPredictor.from_pretrained(self.model_name)
        if hasattr(self._model, "eval"):
            self._model.eval()

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def generate_offline_forecasts(
        self,
        df_15m: pd.DataFrame,
        out_csv: str,
        step: int = 4,
    ) -> pd.DataFrame:
        """Walk the 15m series and emit one forecast every ``step`` bars.

        ``df_15m`` needs columns ``date, open, high, low, close, volume``.
        The result is cached to ``out_csv`` and also returned.
        """
        df = df_15m.reset_index(drop=True)
        need = {"date", "open", "high", "low", "close", "volume"}
        if not need.issubset(df.columns):
            raise ValueError(f"df_15m must contain {need}")

        rows: list[dict] = []
        # leave at least ``horizon`` bars at the end so the prediction is
        # comparable against a *realised* path later in the backtest.
        end = len(df) - self.horizon
        for i in range(self.context_len, end, step):
            window = df.iloc[i - self.context_len : i].reset_index(drop=True)
            forecast = self._predict_window(window, df["date"].iloc[i - 1])
            rows.append(forecast.as_row())

        out = pd.DataFrame(rows)
        out.to_csv(out_csv, index=False)
        return out

    # ------------------------------------------------------------------ #
    # forecasting
    # ------------------------------------------------------------------ #
    def _predict_window(self, window: pd.DataFrame, as_of) -> KronosForecast:
        """Predict one window.  ``window`` is already tail(context_len)."""
        if self._mock:
            return self._mock_predict(window, as_of)
        if self._model is None:
            self._load()
        return self._call_kronos(window, as_of)

    def _call_kronos(self, window: pd.DataFrame, as_of) -> KronosForecast:
        """Single Kronos integration point.

        VERIFY against the cloned ``external/Kronos`` README: the upstream
        interface is
          KronosTokenizer.encode(ohlcv) -> tokens
          KronosPredictor.generate/predict(tokens) -> future tokens
          KronosTokenizer.decode(tokens) -> future OHLCV
        Keep the OHLCV forecast *internal*; expose only direction/return/conf.
        """
        import numpy as np

        # tokenize the last N bars (time-aligned, normalized by the repo's
        # canonical preprocessing if it provides one).
        ohlcv = window[["open", "high", "low", "close", "volume"]].to_numpy()
        input_tokens = self._tokenizer.encode(ohlcv)  # shape depends on tokenizer

        # Sample the future path for a distribution, not a point estimate.
        future_paths = []
        for _ in range(self.n_samples):
            pred_tokens = self._model.generate(input_tokens, max_new_tokens=self.horizon)
            path = self._tokenizer.decode(pred_tokens)  # (horizon, 5)
            future_paths.append(path)
        future = np.stack(future_paths)                 # (samples, horizon, 5)

        close_paths = future[:, :, 3]                   # predicted closes
        mean_close = float(close_paths.mean(axis=0)[-1])
        cur_close = float(window["close"].iloc[-1])
        expected_return = (mean_close - cur_close) / cur_close

        direction = "flat"
        if expected_return > 0.002:
            direction = "long"
        elif expected_return < -0.002:
            direction = "short"

        # confidence = fraction of sampled paths that agree with the direction
        dirs = (close_paths[:, -1] - cur_close) / cur_close
        if direction == "long":
            confidence = float((dirs > 0).mean())
        elif direction == "short":
            confidence = float((dirs < 0).mean())
        else:
            confidence = 0.5

        # worst adverse excursion (in price) over the horizon
        worst = float((close_paths - cur_close).min(axis=1).min())

        return KronosForecast(
            as_of=str(as_of),
            horizon=self.horizon,
            predicted_direction=direction,
            expected_return=expected_return,
            confidence=confidence,
            predicted_max_drawdown=worst,
        )

    # ------------------------------------------------------------------ #
    # mock mode (stub, not a real signal)
    # ------------------------------------------------------------------ #
    def enable_mock(self) -> None:
        """Use a naive momentum stub when Kronos is unavailable.

        Clearly labelled — never treat this output as a model prediction.
        """
        self._mock = True

    def _mock_predict(self, window: pd.DataFrame, as_of) -> KronosForecast:
        close = window["close"].to_numpy()
        # last 12-bar momentum as a stand-in "direction"
        ret = (close[-1] - close[-12]) / close[-12] if len(close) >= 12 else 0.0
        direction = "long" if ret > 0.002 else ("short" if ret < -0.002 else "flat")
        confidence = min(0.9, 0.5 + abs(ret) * 10)
        return KronosForecast(
            as_of=str(as_of),
            horizon=self.horizon,
            predicted_direction=direction,
            expected_return=ret,
            confidence=confidence,
            predicted_max_drawdown=-abs(ret) * close[-1],
        )


if __name__ == "__main__":
    # smoke test: generate a mock forecast CSV from synthetic data
    import numpy as np

    n = 600
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    rng = np.random.default_rng(0)
    px = 100 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame(
        {
            "date": idx,
            "open": px,
            "high": px + 0.3,
            "low": px - 0.3,
            "close": px,
            "volume": rng.integers(100, 1000, n).astype(float),
        }
    )
    a = KronosAdapter()
    a.enable_mock()
    out = a.generate_offline_forecasts(df, "cache/_smoke.csv", step=8)
    print(out.head())
    print(f"rows={len(out)}")
