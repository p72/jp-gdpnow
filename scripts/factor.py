"""Business-cycle factor engine (景気因子エンジン): mixed-frequency dynamic factor model.

  x_it  = λ_i f_t + e_it          (monthly indicators, standardized growth / changes)
  GDP_q = Mariano–Murasawa aggregation of λ_g f_t + e_gt   (quarterly real GDP q/q)
  f_t ~ AR(2), e ~ AR(1)          — statsmodels DynamicFactorMQ, EM estimation

Outputs
  * GDP nowcast for the target quarter (q/q, SAAR) — the model's own estimate,
    independent from the demand-side bridge engine
  * the common factor itself (景気の芯), monthly, standardized
Series are cut at their own release lags when `asof` is given (scripts/release.py).
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

START = pd.Timestamp("2005-01-01")
COVID_M = pd.date_range("2020-02-01", "2020-12-01", freq="MS")
COVID_Q = pd.date_range("2020-01-01", "2020-10-01", freq="QS")

# series -> transform ("dlog" = monthly log-diff ×100, "diff" = first difference, "level")
FACTOR_SERIES = {
    "iip": "dlog", "prod_goods_ship": "dlog", "durable_ship": "dlog", "invest_goods": "dlog",
    "labor_input": "dlog", "new_job_offers": "dlog", "jobs_ratio": "diff",
    "cai_real": "dlog", "real_exports": "dlog", "real_imports": "dlog", "export_volume": "dlog",
    "services_credit": "dlog", "services_debit": "dlog", "housing_starts": "dlog",
    "consumer_conf": "diff", "sme_sales_di": "level",
}


@dataclass
class FactorNowcast:
    quarter: pd.Timestamp
    qoq: float
    saar: float
    factor: pd.Series          # monthly common factor (smoothed)
    loadings: pd.Series
    n_series: int


def _transform(monthly: pd.DataFrame) -> pd.DataFrame:
    out = {}
    for c, how in FACTOR_SERIES.items():
        if c not in monthly:
            continue
        s = monthly[c].astype(float)
        if how == "dlog":
            out[c] = np.log(s).diff() * 100
        elif how == "diff":
            out[c] = s.diff()
        else:
            out[c] = s
    return pd.DataFrame(out)


def factor_nowcast(qe: pd.DataFrame, monthly: pd.DataFrame, target, asof=None,
                   cutoff=None, factor_order: int = 2) -> FactorNowcast:
    """qe: QE real levels known at nowcast time (last row = target-1Q).
    asof: calendar date (series-specific release lags) or cutoff: last month for all."""
    from statsmodels.tsa.statespace.dynamic_factor_mq import DynamicFactorMQ
    from .release import cut_monthly

    target = pd.Timestamp(target)
    q_end = target + pd.offsets.MonthBegin(2)
    m = monthly.copy()
    if asof is not None:
        m = cut_monthly(m, asof)
    elif cutoff is not None:
        m = m[m.index <= pd.Timestamp(cutoff)]
    x = _transform(m)
    idx = pd.date_range(START, q_end, freq="MS")
    x = x.reindex(idx)
    x.loc[x.index.isin(COVID_M)] = np.nan
    x = x.loc[:, x.notna().sum() > 60]

    qe = qe[qe.index < target]
    g = (np.log(qe["GDP"]).diff() * 100)
    g = g[(g.index >= START) & ~g.index.isin(COVID_Q)]
    gq = pd.DataFrame({"GDP": g.reindex(pd.date_range(START, target, freq="QS"))})
    gq.loc[target, "GDP"] = np.nan

    xm = x.copy(); xm.index = pd.PeriodIndex(xm.index, freq="M")
    xq = gq.copy(); xq.index = pd.PeriodIndex(xq.index, freq="Q")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mod = DynamicFactorMQ(xm, endog_quarterly=xq, factors=1, factor_orders=factor_order,
                              idiosyncratic_ar1=True, standardize=True)
        res = mod.fit(disp=False, maxiter=300)
    names = list(mod.endog_names)
    sm = pd.DataFrame(res.smoother_results.smoothed_forecasts.T, index=xm.index, columns=names)
    mean, std = mod._endog_mean, mod._endog_std
    gcol = "GDP"
    qoq_log = float(sm.loc[pd.Period(q_end, "M"), gcol] * std[gcol] + mean[gcol])
    qoq = (np.exp(qoq_log / 100) - 1) * 100
    saar = ((1 + qoq / 100) ** 4 - 1) * 100
    st = res.smoother_results.smoothed_state
    f = pd.Series(st[0], index=x.index, name="factor")
    lam = res.params.filter(like="loading")
    # sign normalisation: factor rises with IIP
    lip = [k for k in lam.index if k.endswith("->iip")]
    if lip and lam[lip[0]] < 0:
        f, lam = -f, -lam
    return FactorNowcast(target, qoq, saar, f, lam, x.shape[1])
