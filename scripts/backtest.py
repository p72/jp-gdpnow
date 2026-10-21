"""Pseudo real-time backtest against QE 1次速報.

For each target quarter T:
- QE levels = the 2次速報 of T-1 (what was available during quarter T)
- monthly indicators = latest vintage, truncated at the cutoff month
  (h=1: only month 1 of T observed / h=3: all three months observed)
- target value = real GDP q/q SAAR published in the 1次速報 of T
- benchmarks: naive (previous quarter's growth) and mean (average SAAR of
  2013Q1..T-1 in the T-1 vintage, 2020 excluded)

Caveat: monthly indicators are the *latest* vintage (revised), so errors are
somewhat understated relative to a true real-time exercise.
"""
from __future__ import annotations

import os
import pickle

import numpy as np
import pandas as pd

from . import data, model

# English labels are stable across vintages (Japanese names changed, e.g. 在庫品増加→在庫変動)
KIYO_COLS = {
    "PrivateConsumption": "PrivateConsumption", "PrivateResidential": "PrivateResidential",
    "PrivateCapex": "Private Non-Resi", "PrivateInventories": "PrivateInventories",
    "GovConsumption": "GovernmentConsumption", "PublicInvestment": "PublicInvestment",
    "PublicInventories": "PublicInventories", "Exports": "Exports", "Imports": "Imports",
}


def quarters(start: str, end: str):
    return pd.date_range(start, end, freq="QS")


def _cache_path():
    d = os.environ.get("JP_GDPNOW_CACHE", os.path.expanduser("~/.cache/jp-gdpnow"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "vintages.pkl")


def load_vintages(targets) -> dict:
    """{T: dict(qe=levels of T-1 2次, actual=QE1 SAAR, kiyo=QE1 q/q contributions)}"""
    path = _cache_path()
    cache = pickle.load(open(path, "rb")) if os.path.exists(path) else {}
    e = data.esri()
    changed = False
    for T in targets:
        if T in cache:
            continue
        prev = T - pd.offsets.QuarterBegin(1, startingMonth=1)
        y, q = T.year, (T.month - 1) // 3 + 1
        try:
            qe = data.get_qe_levels(prev.year, (prev.month - 1) // 3 + 1, revision=2)
            actual = data.get_qe1_growth(y, q)
            raw = e.get_qe_raw(y, q, "kiyo-jk", 1)
        except Exception as ex:
            print(f"skip {T.date()}: {ex}")
            continue
        kiyo = {}
        for k, lab in KIYO_COLS.items():
            s = e.parse_qe_column(raw, lab)
            v = s[(s["year"] == y) & (s["quarter"] == q)]["value"]
            kiyo[k] = float(v.iloc[0])
        cache[T] = {"qe": qe[qe.index <= prev], "actual": actual, "kiyo": pd.Series(kiyo)}
        changed = True
    if changed:
        pickle.dump(cache, open(path, "wb"))
    return {T: cache[T] for T in targets if T in cache}


def _saar(qoq):
    return ((1 + qoq / 100) ** 4 - 1) * 100


def run(start="2021-07-01", end=None, monthly=None, horizons=(1, 3),
        nowcast_fn=None, verbose=False, detail=False, mode="months"):
    """Returns DataFrame (one row per quarter). With detail=True also returns
    a dict of per-component q/q contribution errors for the last horizon.

    mode="months": horizons = number of months of the quarter observed, the same
                   for every series (columns nowcast_h1, nowcast_h3, ...)
    mode="dates":  realistic ragged edge — nowcasts on calendar dates d1..d4
                   (scripts/release.py), each series cut at its own release lag
                   (columns nowcast_d1 ... nowcast_d4; d4 = just before QE1)."""
    if monthly is None:
        monthly = data.get_monthly()
    if end is None:
        info = data.latest_qe_info()
        end = pd.Timestamp(year=int(info["year"]), month=3 * int(info["quarter"]) - 2, day=1)
    nowcast_fn = nowcast_fn or model.nowcast
    vint = load_vintages(list(quarters(start, end)))
    rows, comp = [], {}
    for T, v in vint.items():
        qe = v["qe"]
        g = qe["GDP"].pct_change() * 100
        hist = g[(g.index >= "2013-01-01") & ~g.index.isin(model.EXCLUDE)]
        row = {"quarter": T, "actual_QE1": v["actual"],
               "naive_prevQ": _saar(g.iloc[-1]), "mean_hist": _saar(hist.mean())}
        if mode == "dates":
            from .release import nowcast_dates
            points = [(k, dict(asof=d)) for k, d in nowcast_dates(T).items()]
        else:
            points = [(f"h{h}", dict(cutoff=T + pd.offsets.MonthBegin(h - 1))) for h in horizons]
        for i, (key, kw) in enumerate(points):
            n = nowcast_fn(qe, monthly, T, **kw)
            row[f"nowcast_{key}"] = n.saar
            h = key
            if i == len(points) - 1:
                c = n.contributions
                actual_c = v["kiyo"]
                comp[T] = pd.DataFrame({"nowcast": c, "QE1": actual_c})
                inv = ["PrivateInventories", "PublicInventories"]
                row[f"nowcast_h{h}_exinv"] = _saar(n.qoq - c[inv].sum())
                row["actual_exinv"] = _saar(((1 + v["actual"] / 100) ** 0.25 - 1) * 100
                                             - actual_c[inv].sum())
        rows.append(row)
        if verbose:
            print({k: (round(x, 2) if isinstance(x, float) else x) for k, x in row.items()})
    bt = pd.DataFrame(rows).set_index("quarter")
    return (bt, comp) if detail else bt


def scores(bt: pd.DataFrame, target="actual_QE1", cols=None) -> pd.DataFrame:
    cols = cols or [c for c in bt.columns if not c.startswith("actual") and "exinv" not in c]
    out = {}
    for c in cols:
        e = bt[c] - bt[target]
        out[c] = {
            "MAE": e.abs().mean(),
            "RMSE": np.sqrt((e ** 2).mean()),
            "bias": e.mean(),
            "符号一致率": (np.sign(bt[c]) == np.sign(bt[target])).mean(),
            "相関": bt[c].corr(bt[target]) if bt[c].std() > 0 else np.nan,
        }
    return pd.DataFrame(out).T.round(2)


def component_errors(comp: dict) -> pd.DataFrame:
    """MAE of q/q contribution errors (%pt, ×4 ≈ annualized) by component."""
    err = pd.DataFrame({T: d["nowcast"] - d["QE1"] for T, d in comp.items()}).T
    err = err.drop(columns=[c for c in err.columns if c not in KIYO_COLS])
    return pd.DataFrame({"MAE(年率近似)": (err.abs().mean() * 4),
                         "bias(年率近似)": err.mean() * 4}).round(2)


def run_engine(fn, start="2021-07-01", end=None, monthly=None, verbose=False) -> pd.DataFrame:
    """Calendar-date backtest (d1..d4) for any engine: fn(qe, monthly, T, asof) -> SAAR."""
    from .release import nowcast_dates
    if monthly is None:
        monthly = data.get_monthly()
    if end is None:
        info = data.latest_qe_info()
        end = pd.Timestamp(year=int(info["year"]), month=3 * int(info["quarter"]) - 2, day=1)
    vint = load_vintages(list(quarters(start, end)))
    rows = []
    for T, v in vint.items():
        row = {"quarter": T, "actual_QE1": v["actual"]}
        for k, d in nowcast_dates(T).items():
            try:
                row[f"nowcast_{k}"] = float(fn(v["qe"], monthly, T, d))
            except Exception as ex:  # keep going; record the failure
                row[f"nowcast_{k}"] = np.nan
                if verbose:
                    print(T.date(), k, ex)
        rows.append(row)
        if verbose:
            print({k: (round(x, 2) if isinstance(x, float) else x) for k, x in row.items()})
    return pd.DataFrame(rows).set_index("quarter")
