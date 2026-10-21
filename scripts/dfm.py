"""Ragged-edge fill-in with a monthly dynamic factor model.

x_it = λ_i f_t + e_it,  f_t ~ AR(2),  e_it ~ AR(1)   (statsmodels DynamicFactorMQ, EM)

The panel is monthly log-differences (×100) of every indicator. Values after the
cutoff month, and 2020-02..2020-12 (COVID outliers), are set to NaN; the Kalman
smoother then gives E[x_it | all observed data] for every missing cell, so a
series whose latest month is not out yet borrows information from series that
are. Levels are rebuilt from each series' last observed level.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

DFM_START = pd.Timestamp("2005-02-01")
COVID = pd.date_range("2020-02-01", "2020-12-01", freq="MS")


def fill_dfm(monthly: pd.DataFrame, cutoff, until: pd.Timestamp,
             factors: int = 1, factor_order: int = 2) -> pd.DataFrame:
    """Return monthly levels for all columns, extended to `until`.
    cutoff: Timestamp (same for all series) or {series: last released month};
    later observations are treated as not yet released."""
    from statsmodels.tsa.statespace.dynamic_factor_mq import DynamicFactorMQ

    lv = monthly.copy()
    for c in lv.columns:
        cut = cutoff.get(c, max(cutoff.values())) if isinstance(cutoff, dict) else cutoff
        lv.loc[lv.index > cut, c] = np.nan
    lv = lv[lv.index <= until]
    lv = lv[lv.index >= DFM_START - pd.offsets.MonthBegin(1)]
    x = np.log(lv).diff() * 100
    idx = pd.date_range(DFM_START, until, freq="MS")
    x = x.reindex(idx)
    observed = x.notna()
    x.loc[x.index.isin(COVID)] = np.nan
    x = x.loc[:, x.notna().sum() > 60]  # need enough history to estimate a loading
    xp = x.copy()
    xp.index = pd.PeriodIndex(xp.index, freq="M")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mod = DynamicFactorMQ(xp, factors=factors, factor_orders=factor_order,
                              idiosyncratic_ar1=True, standardize=True)
        res = mod.fit(disp=False, maxiter=500)
    sm = pd.DataFrame(res.smoother_results.smoothed_forecasts.T, index=x.index, columns=x.columns)
    sm = sm * mod._endog_std + mod._endog_mean

    out = {}
    for c in monthly.columns:
        s = lv[c].dropna()
        if c not in sm.columns or s.empty:
            out[c] = s
            continue
        s = s.copy()
        level = np.log(s.iloc[-1])
        m = s.index[-1]
        while m < until:
            m = m + pd.offsets.MonthBegin(1)
            level += sm.at[m, c] / 100
            s.loc[m] = np.exp(level)
        out[c] = s
    return pd.DataFrame(out)
