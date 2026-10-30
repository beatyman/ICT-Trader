"""Kronos adapter — offline wrapper around the Kronos K-line model.

Design contract
---------------
* Physical isolation: this module is the *only* place that imports Kronos.
* Offline + cached: forecasts are generated once into ``cache/*.csv``.
* Degradable: ``enable_mock()`` uses a momentum stub when Kronos is absent.

The exact Kronos API (verified against ``external/Kronos/model/kronos.py``):

    from model import Kronos, KronosTokenizer, KronosPredictor
    tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
    model     = Kronos.from_pretrained("NeoQuasar/Kronos-small")
    predictor = KronosPredictor(model, tokenizer, device=..., max_context=512)
    pred_df = predictor.predict(df=..., x_timestamp=..., y_timestamp=...,
                                pred_len=N, T=1.0, top_p=0.9, sample_count=1)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd


@dataclass
class KronosForecast:
    as_of: str
    horizon: int
    predicted_direction: str          # "long" | "short" | "flat"
    expected_return: float            # mean future return over horizon
    confidence: float                 # 0..1
    predicted_high: float             # mean predicted high (long target)
    predicted_low: float              # mean predicted low  (short target)
    predicted_max_drawdown: float     # signed worst excursion (<=0)

    def as_row(self) -> dict:
        return asdict(self)


class KronosAdapter:
    def __init__(
        self,
        tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base",
        model_name: str = "NeoQuasar/Kronos-small",
        context_len: int = 512,
        horizon: int = 12,
        n_samples: int = 24,
        device: str | None = None,        # None -> auto (cuda if available)
        repo_path: str | None = None,     # Kronos repo root for `from model import`
    ) -> None:
        self.tokenizer_name = tokenizer_name
        self.model_name = model_name
        self.context_len = context_len
        self.horizon = horizon
        self.n_samples = n_samples
        self.device = device
        self.repo_path = repo_path
        self._predictor = None
        self._mock = False

    def _load(self) -> None:
        if self.repo_path:
            import sys
            sys.path.insert(0, self.repo_path)
        try:
            from model import Kronos, KronosTokenizer, KronosPredictor  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Kronos not importable. Pass repo_path=... (repo root) or "
                "clone shiyu-coder/Kronos and add it to sys.path."
            ) from exc

        tokenizer = KronosTokenizer.from_pretrained(self.tokenizer_name)
        model = Kronos.from_pretrained(self.model_name)
        self._predictor = KronosPredictor(
            model, tokenizer, device=self.device, max_context=self.context_len
        )

    # ------------------------------------------------------------------ #
    def generate_offline_forecasts(
        self, df_15m: pd.DataFrame, out_csv: str, step: int = 4
    ) -> pd.DataFrame:
        df = df_15m.reset_index(drop=True)
        need = {"date", "open", "high", "low", "close", "volume"}
        if not need.issubset(df.columns):
            raise ValueError(f"df_15m must contain {need}")

        rows: list[dict] = []
        end = len(df) - self.horizon
        for i in range(self.context_len, end, step):
            window = df.iloc[i - self.context_len : i].reset_index(drop=True)
            rows.append(self._predict_window(window, df["date"].iloc[i - 1]).as_row())

        out = pd.DataFrame(rows)
        out.to_csv(out_csv, index=False)
        return out

    def _predict_window(self, window: pd.DataFrame, as_of) -> KronosForecast:
        if self._mock:
            return self._mock_predict(window, as_of)
        if self._predictor is None:
            self._load()
        return self._call_kronos(window, as_of)

    def predict_latest(self, df_15m: pd.DataFrame) -> KronosForecast:
        """Forecast as of the last closed bar (single point)."""
        df = df_15m.reset_index(drop=True)
        window = df.iloc[-self.context_len :].reset_index(drop=True)
        return self._predict_window(window, df["date"].iloc[-1])

    # ------------------------------------------------------------------ #
    def _call_kronos(self, window: pd.DataFrame, as_of) -> KronosForecast:
        import numpy as np

        x_df = window[["open", "high", "low", "close", "volume"]].copy()
        x_ts = pd.to_datetime(window["date"]).reset_index(drop=True)
        last_ts = x_ts.iloc[-1]
        freq = pd.infer_freq(x_ts) or "15min"
        # Kronos `calc_time_stamps` needs a Series (uses .dt), not a DatetimeIndex.
        y_ts = pd.Series(pd.date_range(start=last_ts, periods=self.horizon + 1, freq=freq)[1:]).reset_index(drop=True)

        cur_close = float(window["close"].iloc[-1])
        closes, highs, lows = [], [], []
        for _ in range(self.n_samples):
            pred = self._predictor.predict(
                df=x_df,
                x_timestamp=x_ts,
                y_timestamp=y_ts,
                pred_len=self.horizon,
                T=1.0,
                top_p=0.9,
                sample_count=1,
                verbose=False,
            )
            closes.append(pred["close"].to_numpy())
            highs.append(pred["high"].to_numpy())
            lows.append(pred["low"].to_numpy())

        closes = np.stack(closes)          # (n_samples, horizon)
        highs = np.stack(highs)
        lows = np.stack(lows)

        final_closes = closes[:, -1]
        mean_close = float(final_closes.mean())
        expected_return = (mean_close - cur_close) / cur_close

        direction = "flat"
        if expected_return > 0.002:
            direction = "long"
        elif expected_return < -0.002:
            direction = "short"

        rets = (final_closes - cur_close) / cur_close
        if direction == "long":
            confidence = float((rets > 0).mean())
        elif direction == "short":
            confidence = float((rets < 0).mean())
        else:
            confidence = 0.5

        predicted_high = float(highs.max(axis=1).mean())
        predicted_low = float(lows.min(axis=1).mean())
        worst_excursion = float((lows.min(axis=1) - cur_close).min())

        return KronosForecast(
            as_of=str(as_of),
            horizon=self.horizon,
            predicted_direction=direction,
            expected_return=expected_return,
            confidence=confidence,
            predicted_high=predicted_high,
            predicted_low=predicted_low,
            predicted_max_drawdown=worst_excursion,
        )

    # ------------------------------------------------------------------ #
    def enable_mock(self) -> None:
        self._mock = True

    def _mock_predict(self, window: pd.DataFrame, as_of) -> KronosForecast:
        close = window["close"].to_numpy()
        ret = (close[-1] - close[-12]) / close[-12] if len(close) >= 12 else 0.0
        direction = "long" if ret > 0.002 else ("short" if ret < -0.002 else "flat")
        confidence = min(0.9, 0.5 + abs(ret) * 10)
        return KronosForecast(
            as_of=str(as_of),
            horizon=self.horizon,
            predicted_direction=direction,
            expected_return=ret,
            confidence=confidence,
            predicted_high=close[-1] * (1 + abs(ret)),
            predicted_low=close[-1] * (1 - abs(ret)),
            predicted_max_drawdown=-abs(ret) * close[-1],
        )


if __name__ == "__main__":
    import numpy as np

    n = 600
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    rng = np.random.default_rng(0)
    px = 100 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame({
        "date": idx,
        "open": px, "high": px + 0.3, "low": px - 0.3, "close": px,
        "volume": rng.integers(100, 1000, n).astype(float),
    })
    a = KronosAdapter()
    a.enable_mock()
    out = a.generate_offline_forecasts(df, "cache/_smoke.csv", step=8)
    print(out.head().to_string())
