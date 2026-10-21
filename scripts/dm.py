"""Diebold-Mariano test with the Harvey-Leybourne-Newbold small-sample correction.

    from scripts.dm import dm_test, compare
    dm_test(e1, e2, loss="abs")   # e = forecast - actual; H1: model 1 more accurate
    compare(bt, "nowcast_d4", ["naive_prevQ", "mean_hist"])

One nowcast per quarter and non-overlapping targets -> h = 1 (no HAC lags needed).
Negative statistic = model 1 has the smaller loss. p is one-sided (model 1 better)
and two-sided, both from the t(n-1) distribution as HLN recommend.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats


def dm_test(e1, e2, loss: str = "sq", h: int = 1) -> dict:
    e1, e2 = pd.Series(e1).astype(float), pd.Series(e2).astype(float)
    ok = e1.notna() & e2.notna()
    e1, e2 = e1[ok].values, e2[ok].values
    f = (lambda e: e ** 2) if loss == "sq" else np.abs
    d = f(e1) - f(e2)
    n = len(d)
    dbar = d.mean()
    # long-run variance with h-1 autocovariances (h=1 -> plain variance)
    gam = [np.sum((d[k:] - dbar) * (d[:n - k] - dbar)) / n for k in range(h)]
    var = (gam[0] + 2 * sum(gam[1:])) / n
    dm = dbar / np.sqrt(var)
    hln = np.sqrt((n + 1 - 2 * h + h * (h - 1) / n) / n)
    t = dm * hln
    return {"n": n, "loss_diff": dbar, "stat": t,
            "p_one": stats.t.cdf(t, df=n - 1), "p_two": 2 * stats.t.sf(abs(t), df=n - 1)}


def compare(bt: pd.DataFrame, model: str, rivals, actual: str = "actual_QE1",
            losses=("abs", "sq")) -> pd.DataFrame:
    """bt: one row per quarter with forecast columns. rivals: column names in bt,
    or (label, Series) pairs aligned on the same index."""
    e_m = bt[model] - bt[actual]
    rows = []
    for r in rivals:
        label, s = (r, bt[r]) if isinstance(r, str) else r
        e_r = s.reindex(bt.index) - bt[actual]
        for L in losses:
            res = dm_test(e_m, e_r, loss=L)
            rows.append({"相手": label, "損失": "絶対誤差" if L == "abs" else "二乗誤差", **res})
    return pd.DataFrame(rows)
