"""Approximate release calendar: which month of each series is public on a given date.

RELEASE_LAG[series] = (months after the reference month, day of that month)
e.g. (1, 23) = data for month m are published around the 23rd of month m+1.
Days are typical values (business-day rules vary month to month); good enough to
reproduce the ragged edge of the data set at any nowcast date.
"""
from __future__ import annotations

import pandas as pd

RELEASE_LAG = {
    "real_exports": (1, 23),     # 日銀 実質輸出入: 貿易統計速報(~20日)の翌営業日
    "real_imports": (1, 23),
    "cai_real": (2, 8),          # 日銀 消費活動指数: 原則 第5営業日
    "housing_starts": (2, 8),    # CI速報経由（住宅着工そのものは翌月末）
    "invest_goods": (1, 28),     # 経産省 鉱工業指数速報（翌月末。9月分=10/30）
    "iip": (1, 28),
    "machine_orders": (2, 15),   # 機械受注（翌々月中旬）
    "services_credit": (2, 10),  # 国際収支速報（季調値）
    "services_debit": (2, 10),
    "labor_input": (3, 8),       # CI速報では欠けがち → 1か月遅れ扱い
    "public_const": (2, 17),     # 建設総合統計（7月分=9/17公表）
    "priv_res_const": (2, 17), "priv_nonres_const": (2, 17),
    "real_ex_capital": (1, 23),
    # extra CI component series (factor engine): via CI速報
    "new_job_offers": (2, 8), "consumer_conf": (2, 8), "sme_sales_di": (2, 8),
    "prod_goods_ship": (1, 28), "durable_ship": (1, 28),  # 経産省 鉱工業指数速報
    "jobs_ratio": (2, 8),
    "export_volume": (2, 8),
    "inv_ratio_final": (2, 8), "inv_ratio_prod": (2, 8),
    "bop_goods_ex": (2, 10), "bop_goods_im": (2, 10),
    "bop_real_ex": (2, 10), "bop_real_im": (2, 10),       # 国際収支速報（翌々月上旬）
    "px_export": (1, 12), "px_import": (1, 12),           # 企業物価（翌月中旬）
    "px_services": (1, 26),                               # 企業向けサービス価格（翌月下旬）
    "cti_real": (2, 7),          # 総消費動向指数（6月分=8/7公表）
    # 第3次産業活動指数（経産省, 翌々月中旬。7月分は10/16時点で公表済み・8月分は未公表）
    "tert_total": (2, 18), "tert_personal": (2, 18), "tert_business": (2, 18),
}
# 鉱工業指数 財別（経産省 速報: 翌月末。8月分=9/30）
for _s in ("ship_capital", "ship_capital_ex", "ship_construction", "ship_durable",
           "ship_nondurable", "stk_inv_total", "stk_inv_final", "stk_inv_prod"):
    RELEASE_LAG[_s] = (1, 29)
# production plans: survey of month k is published on the last business day of month k
for _s in ("iip", "invest_goods", "durable_ship", "prod_goods_ship"):
    RELEASE_LAG[f"plan_this_{_s}"] = (1, 1)   # indexed at k   -> public from k+1/1
    RELEASE_LAG[f"plan_next_{_s}"] = (0, 1)   # indexed at k+1 -> public from k+1/1


def cut_monthly(monthly: pd.DataFrame, asof) -> pd.DataFrame:
    """Hide every observation not yet published on date `asof` (series-specific lags)."""
    out = monthly.copy()
    for c in out.columns:
        out.loc[out.index > last_available_month(c, asof), c] = float("nan")
    return out


def last_available_month(series: str, asof: pd.Timestamp) -> pd.Timestamp:
    """Latest reference month of `series` already published on date `asof`."""
    lag_m, day = RELEASE_LAG.get(series, (2, 10))
    asof = pd.Timestamp(asof)
    m = asof.to_period("M").to_timestamp()  # first day of asof's month
    if asof.day < day:
        lag_m += 1
    return m - pd.offsets.MonthBegin(lag_m)


def cutoffs(columns, asof: pd.Timestamp) -> dict:
    return {c: last_available_month(c, asof) for c in columns}


def nowcast_dates(target: pd.Timestamp) -> dict:
    """Representative nowcast dates for quarter `target` (start date).
    d1: 25th of the quarter's 2nd month ... d4: 10th of the 2nd month after the quarter
    (just before the QE 1st release)."""
    t = pd.Timestamp(target)
    return {
        "d1": t + pd.offsets.MonthBegin(1) + pd.Timedelta(days=24),
        "d2": t + pd.offsets.MonthBegin(2) + pd.Timedelta(days=24),
        "d3": t + pd.offsets.MonthBegin(3) + pd.Timedelta(days=24),
        "d4": t + pd.offsets.MonthBegin(4) + pd.Timedelta(days=9),
    }


def update_dates(target: pd.Timestamp, until: pd.Timestamp) -> list:
    """Main data-release days (~9th, ~16th, ~24th of each month) from the quarter's
    2nd month up to `until` — used to reconstruct a pseudo nowcast history."""
    t = pd.Timestamp(target)
    out = []
    m = t + pd.offsets.MonthBegin(1)
    while m <= pd.Timestamp(until):
        for d in (9, 16, 24):
            x = m + pd.Timedelta(days=d - 1)
            if x <= pd.Timestamp(until):
                out.append(x)
        m = m + pd.offsets.MonthBegin(1)
    return out


def upcoming(asof, n: int = 6) -> pd.DataFrame:
    """Next releases after `asof` from scripts/schedule.csv (hand-maintained;
    add the next quarter's dates there)."""
    import os
    path = os.path.join(os.path.dirname(__file__), "schedule.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path, dtype=str).fillna("")
    df["date"] = pd.to_datetime(df["date"])
    return df[df["date"] > pd.Timestamp(asof)].sort_values("date").head(n)
