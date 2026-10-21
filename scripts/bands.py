"""Prediction intervals from calendar-date backtest errors (scripts/error_bands.csv).

For each engine and nowcast point d1..d4 (release.nowcast_dates) the backtest
RMSE is stored together with its distance (days) from the start of the quarter.
For any nowcast date the RMSE is interpolated by days; intervals assume normal
errors: 68% = ±1.0×RMSE, 90% = ±1.645×RMSE. Regenerate the CSV with
`python -m scripts.cli bands` after model changes.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

PATH = os.path.join(os.path.dirname(__file__), "error_bands.csv")


def build(results: dict) -> pd.DataFrame:
    """results: {engine: backtest DataFrame with actual_QE1, nowcast_d1..d4}."""
    from .release import nowcast_dates
    rows = []
    for eng, bt in results.items():
        T0 = pd.Timestamp("2026-07-01")
        days = {k: (d - T0).days for k, d in nowcast_dates(T0).items()}
        for k in ["d1", "d2", "d3", "d4"]:
            e = (bt[f"nowcast_{k}"] - bt["actual_QE1"]).dropna()
            rows.append(dict(engine=eng, point=k, days=days[k], n=len(e),
                             rmse=float(np.sqrt((e ** 2).mean())), mae=float(e.abs().mean()),
                             bias=float(e.mean())))
    df = pd.DataFrame(rows)
    df.to_csv(PATH, index=False)
    return df


def rmse_at(engine: str, target, asof) -> float:
    if not os.path.exists(PATH):
        return float("nan")
    df = pd.read_csv(PATH)
    df = df[df["engine"] == engine].sort_values("days")
    if df.empty:
        return float("nan")
    d = (pd.Timestamp(asof) - pd.Timestamp(target)).days
    return float(np.interp(d, df["days"], df["rmse"]))


def interval(engine: str, point: float, target, asof, level: float = 0.9):
    z = {0.68: 1.0, 0.9: 1.645, 0.95: 1.96}[level]
    r = rmse_at(engine, target, asof)
    return point - z * r, point + z * r
