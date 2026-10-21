"""Japan GDPNow: bridge equations + AR fill-in of missing months (+ supply-side blend in v2).

Steps
1. Monthly indicators: months not yet released are filled with an AR(2)
   on monthly log-differences (last 10 years).
2. Quarterly average -> quarter-on-quarter growth (%).
3. Bridge equation per demand component (OLS, 2005Q2-, 2020 quarters dropped).
   Components without a monthly indicator: average growth of last 4 quarters.
   Inventories: AR(1) on the level (share of GDP).
4. Growth contributions = previous-quarter share x component growth;
   chain-linking discrepancy = mean of the last 8 quarters' gap.
5. Sum -> q/q %, annualized ((1+g)^4-1).
6. (v2) Supply side: GDP q/q ~ IIP + labour input; blended 50:50 with step 5.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# component -> list of (monthly indicator, quarterly lag)
BRIDGES = {
    "PrivateConsumption": [("cai_real", 0)],
    "PrivateResidential": [("housing_starts", 1), ("housing_starts", 2)],
    "PrivateCapex": [("invest_goods", 0), ("machine_orders", 1)],
    "Exports": [("real_exports", 0)],
    "Imports": [("real_imports", 0)],
}
INVENTORY_COMPS = ["PrivateInventories", "PublicInventories"]
JP_NAMES = {
    "PrivateConsumption": "個人消費",
    "PrivateResidential": "住宅投資",
    "PrivateCapex": "設備投資",
    "PrivateInventories": "民間在庫",
    "GovConsumption": "政府消費",
    "PublicInvestment": "公共投資",
    "PublicInventories": "公的在庫",
    "Exports": "輸出",
    "Imports": "輸入（控除）",
    "Discrepancy": "連鎖不突合等",
    "SupplyBlend": "供給側との合成調整",
}
EXCLUDE = pd.date_range("2020-01-01", "2020-10-01", freq="QS")
EST_START = pd.Timestamp("2005-04-01")


# ---------------------------------------------------------------- monthly
def fill_ar(s: pd.Series, until: pd.Timestamp, p: int = 2, window: int = 120) -> pd.Series:
    """Extend a monthly level series to `until` with AR(p) on log-diffs."""
    s = s.dropna()
    if s.empty or s.index[-1] >= until:
        return s[s.index <= until]
    d = np.log(s).diff().dropna().iloc[-window:]
    d = d[~d.index.isin(pd.date_range("2020-02-01", "2020-12-01", freq="MS"))]
    X = np.column_stack([np.ones(len(d) - p)] + [d.values[p - i - 1:len(d) - i - 1] for i in range(p)])
    beta = np.linalg.lstsq(X, d.values[p:], rcond=None)[0]
    hist = list(np.log(s).diff().dropna().values[-p:])
    level = np.log(s.iloc[-1])
    out = s.copy()
    m = s.index[-1]
    while m < until:
        m = m + pd.offsets.MonthBegin(1)
        nxt = beta[0] + sum(beta[i + 1] * hist[-i - 1] for i in range(p))
        hist.append(nxt)
        level += nxt
        out.loc[m] = np.exp(level)
    return out


def fill_yoy(s: pd.Series, until: pd.Timestamp) -> pd.Series:
    """ESRI 1次QE-style extension: months missing in the current quarter are set to
    last year's same month × the average year-on-year ratio of the quarter's observed
    months (解説書: 建設総合統計・政府雇用者報酬). Falls back to AR(2) when no month
    of the quarter is observed or last-year values are missing."""
    s = s.dropna()
    if s.empty or s.index[-1] >= until:
        return s[s.index <= until]
    q0 = until - pd.offsets.MonthBegin(2)
    obs = s[(s.index >= q0) & (s.index <= until)]
    ly = s.reindex(obs.index - pd.DateOffset(years=1))
    if obs.empty or ly.isna().any():
        return fill_ar(s, until)
    ratio = float(np.mean(obs.values / ly.values))
    out = s.copy()
    m = s.index[-1]
    while m < until:
        m = m + pd.offsets.MonthBegin(1)
        prev = s.get(m - pd.DateOffset(years=1), np.nan)
        if prev != prev:
            return fill_ar(s, until)
        out.loc[m] = prev * ratio
    return out


SURVEY_TARGETS = ("iip", "invest_goods", "durable_ship", "prod_goods_ship")


def _ols_fit(y, x):
    X = np.column_stack([np.ones(len(x)), x])
    return np.linalg.lstsq(X, y, rcond=None)[0]


def fill_survey(s: pd.Series, plan_this: pd.Series, plan_next: pd.Series, until: pd.Timestamp,
                p: int = 2, window: int = 120) -> pd.Series:
    """Extend a production series with the 製造工業生産予測指数 plans.
    Month k growth = a + b × plan (separate fits for 'this-month' and 'next-month' plans,
    estimated on history excluding 2020); months without a plan fall back to AR(p)."""
    s = s.dropna()
    if s.empty or s.index[-1] >= until:
        return s[s.index <= until]
    g = np.log(s).diff() * 100
    hist = g[~g.index.isin(pd.date_range("2020-02-01", "2020-12-01", freq="MS"))]
    fits = {}
    for key, pl in (("this", plan_this), ("next", plan_next)):
        d = pd.concat([hist, pl], axis=1).dropna()
        fits[key] = _ols_fit(d.iloc[:, 0].values, d.iloc[:, 1].values) if len(d) >= 24 else None
    dd = hist.dropna().iloc[-window:]
    X = np.column_stack([np.ones(len(dd) - p)] + [dd.values[p - i - 1:len(dd) - i - 1] for i in range(p)])
    ar = np.linalg.lstsq(X, dd.values[p:], rcond=None)[0]
    gl = list(g.dropna().values[-p:])
    level, out, m = np.log(s.iloc[-1]), s.copy(), s.index[-1]
    while m < until:
        m = m + pd.offsets.MonthBegin(1)
        if fits["this"] is not None and m in plan_this.index and plan_this[m] == plan_this[m]:
            nxt = fits["this"][0] + fits["this"][1] * plan_this[m]
        elif fits["next"] is not None and m in plan_next.index and plan_next[m] == plan_next[m]:
            nxt = fits["next"][0] + fits["next"][1] * plan_next[m]
        else:
            nxt = ar[0] + sum(ar[i + 1] * gl[-i - 1] for i in range(p))
        gl.append(nxt)
        level += nxt / 100
        out.loc[m] = np.exp(level)
    return out


def quarterly_growth(monthly: pd.DataFrame, cutoff, q_end: pd.Timestamp,
                     fill: str = "ar", survey: bool = False, yoy: tuple = ()):
    """Fill each series to q_end (data after `cutoff` hidden), return q/q % growth
    and a table of how many months were actually observed in the target quarter.
    fill: "ar" (per-series AR(2)) or "dfm" (dynamic factor model, falls back to AR)."""
    out, observed = {}, {}
    q_start = q_end - pd.offsets.MonthBegin(2)
    filled = None
    cut_of = (lambda c: cutoff.get(c, q_end)) if isinstance(cutoff, dict) else (lambda c: cutoff)
    if fill == "dfm":
        try:
            from .dfm import fill_dfm
            filled = fill_dfm(monthly, cutoff, q_end)
        except Exception as ex:  # EM failure etc. -> AR
            import sys
            print(f"[jp-gdpnow] DFM failed ({ex}); falling back to AR", file=sys.stderr)
    for c in monthly.columns:
        if c.startswith("plan_"):
            continue
        s = monthly[c].dropna()
        s = s[s.index <= cut_of(c)]
        observed[c] = int(((s.index >= q_start) & (s.index <= q_end)).sum())
        if survey and c in SURVEY_TARGETS and f"plan_this_{c}" in monthly:
            pt = monthly[f"plan_this_{c}"].dropna(); pt = pt[pt.index <= cut_of(f"plan_this_{c}")]
            pn = monthly[f"plan_next_{c}"].dropna(); pn = pn[pn.index <= cut_of(f"plan_next_{c}")]
            f = fill_survey(s, pt, pn, q_end)
        elif c in yoy:
            f = fill_yoy(s, q_end)
        else:
            f = filled[c].dropna() if filled is not None and c in filled else fill_ar(s, q_end)
        if c.startswith("stk_"):
            # stock series (end-of-month inventories): GDP's inventory change is the flow
            # ΔStock, its contribution to growth is Δ²Stock -> second difference of the
            # quarter-end level, in % of the previous quarter-end level
            q = f.resample("QS").last()
            q = q[q.index <= q_start]
            out[c] = q.diff().diff() / q.shift(1) * 100
            continue
        q = f.resample("QS").mean()
        q = q[q.index <= q_start]
        out[c] = q.pct_change() * 100
    return pd.DataFrame(out), observed


# ---------------------------------------------------------------- bridges
def _ols(y, X):
    X1 = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X1, y, rcond=None)
    resid = y - X1 @ beta
    r2 = 1 - resid.var() / y.var() if y.var() > 0 else np.nan
    return beta, r2


def _design(xq: pd.DataFrame, spec):
    cols = {f"{v}_L{l}": xq[v].shift(l) for v, l in spec}
    return pd.DataFrame(cols)


@dataclass
class Nowcast:
    quarter: pd.Timestamp
    saar: float
    qoq: float
    contributions: pd.Series
    comp_growth: pd.Series
    observed_months: dict
    r2: dict = field(default_factory=dict)

    def table(self) -> pd.DataFrame:
        t = pd.DataFrame({"前期比(%)": self.comp_growth, "寄与度(%pt, 前期比)": self.contributions})
        t.index = [JP_NAMES.get(i, i) for i in t.index]
        return t.round(2)

    def summary(self) -> str:
        q = self.quarter
        lab = f"{q.year}年{q.month}-{q.month + 2}月期"
        obs = ", ".join(f"{k}:{v}/3" for k, v in self.observed_months.items())
        return (f"日本版GDPNow {lab}: 実質GDP 前期比年率 {round(self.saar, 1) + 0.0:+.1f}% "
                f"(前期比 {self.qoq:+.2f}%)\n観測済み月数 {obs}")


@dataclass
class Config:
    """Model specification. Presets: CONFIG_V1 (初版), CONFIG_V2 (改良版)."""
    bridges: dict = field(default_factory=lambda: dict(BRIDGES))
    inventory: str = "ar"            # "ar" | "zero" (contribution 0) | "bridge"
    inv_spec: list = field(default_factory=list)   # private inventory bridge regressors
    inv_ar: bool = False             # bridge: also use last quarter's inventory/GDP share
    supply_spec: list = field(default_factory=list)   # GDP ~ supply indicators
    supply_weight: float = 0.0       # combined = (1-w)*demand + w*supply
    fill: str = "ar"                 # missing months: "ar" | "dfm"
    survey: bool = False             # production series: extrapolate with 製造工業生産予測指数
    yoy_fill: tuple = ()             # series extended ESRI-style (y/y of observed months)


CONFIG_V1 = Config()


def _bridge(gq_y: pd.Series, xq: pd.DataFrame, spec, target):
    X = _design(xq, spec)
    df = pd.concat([gq_y.rename("y"), X], axis=1)
    est = df[(df.index >= EST_START) & (df.index < target) & ~df.index.isin(EXCLUDE)].dropna()
    beta, r2 = _ols(est["y"].values, est.drop(columns="y").values)
    return float(beta[0] + beta[1:] @ X.loc[target].values), r2


def nowcast(qe: pd.DataFrame, monthly: pd.DataFrame, target: pd.Timestamp,
            cutoff: pd.Timestamp | None = None, config: Config | None = None,
            asof: pd.Timestamp | None = None) -> Nowcast:
    """qe: QE levels (real, SA) known at nowcast time, last row = target-1Q.
    monthly: indicator levels.
    cutoff: last month treated as released (same for every series), or
    asof: a calendar date -> each series cut at its own release lag (scripts/release.py)."""
    cfg = config or CONFIG_DEFAULT
    target = pd.Timestamp(target)
    q_end = target + pd.offsets.MonthBegin(2)
    if cutoff is None:
        cutoff = monthly.dropna(how="all").index.max()
    qe = qe[qe.index < target]
    prev = target - pd.offsets.QuarterBegin(1, startingMonth=1)
    assert qe.index[-1] == prev, f"QE must end at {prev.date()}, got {qe.index[-1].date()}"

    needed = ({v for spec in cfg.bridges.values() for v, _ in spec} | {v for v, _ in cfg.supply_spec}
              | {v for v, _ in cfg.inv_spec})
    # DFM uses every available indicator (more series -> better factor); AR only what is needed
    if cfg.survey:
        needed |= {f"plan_{w}_{c}" for c in SURVEY_TARGETS for w in ("this", "next") if c in needed}
    cols = list(monthly.columns) if cfg.fill == "dfm" else [c for c in monthly.columns if c in needed]
    if asof is not None:
        from .release import cutoffs
        cut = cutoffs(cols, asof)
    else:
        cut = pd.Timestamp(cutoff)
    xq, observed = quarterly_growth(monthly[cols], cut, q_end, fill=cfg.fill, survey=cfg.survey,
                                    yoy=tuple(cfg.yoy_fill))
    observed = {k: v for k, v in observed.items() if k in needed}
    growth, r2 = {}, {}
    gq = qe.pct_change() * 100

    for comp, spec in cfg.bridges.items():
        growth[comp], r2[comp] = _bridge(gq[comp], xq, spec, target)

    trend = [c for c in ["GovConsumption", "PublicInvestment"] if c not in cfg.bridges]
    for comp in trend:
        growth[comp] = float(gq[comp].iloc[-4:].mean())

    # contributions to q/q growth
    gdp_prev = qe["GDP"].iloc[-1]
    contrib = {}
    for comp, g in growth.items():
        sign = -1 if comp == "Imports" else 1
        contrib[comp] = sign * qe[comp].iloc[-1] / gdp_prev * g

    for comp in INVENTORY_COMPS:
        if cfg.inventory == "bridge" and comp == "PrivateInventories" and cfg.inv_spec:
            y = (qe[comp].diff() / qe["GDP"].shift(1) * 100).rename("y")
            X = _design(xq, cfg.inv_spec)
            if cfg.inv_ar:
                X["share_L1"] = (qe[comp] / qe["GDP"] * 100).shift(1).reindex(X.index)
                X.loc[target, "share_L1"] = qe[comp].iloc[-1] / gdp_prev * 100
            df = pd.concat([y, X], axis=1)
            est = df[(df.index >= EST_START) & (df.index < target) & ~df.index.isin(EXCLUDE)].dropna()
            beta, r2["PrivateInventories"] = _ols(est["y"].values, est.drop(columns="y").values)
            contrib[comp] = float(beta[0] + beta[1:] @ X.loc[target].values)
        elif cfg.inventory in ("zero", "bridge"):
            contrib[comp] = 0.0
        else:
            share = (qe[comp] / qe["GDP"]).loc[EST_START:]
            share = share[~share.index.isin(EXCLUDE)]
            rho = _ols(share.values[1:], share.values[:-1])[0]
            level_t = (rho[0] + rho[1] * share.iloc[-1]) * gdp_prev
            contrib[comp] = (level_t - qe[comp].iloc[-1]) / gdp_prev * 100
        growth[comp] = np.nan

    # chain-linking discrepancy (actual GDP growth - sum of actual contributions)
    hist = pd.Series(0.0, index=qe.index)
    for comp in set(cfg.bridges) | set(trend):
        sign = -1 if comp == "Imports" else 1
        hist += sign * qe[comp].shift(1) / qe["GDP"].shift(1) * gq[comp]
    for comp in INVENTORY_COMPS:
        hist += qe[comp].diff() / qe["GDP"].shift(1) * 100
    gap = (gq["GDP"] - hist).dropna()
    gap = gap[~gap.index.isin(EXCLUDE)]
    contrib["Discrepancy"] = float(gap.iloc[-8:].mean())
    growth["Discrepancy"] = np.nan

    order = ["PrivateConsumption", "PrivateResidential", "PrivateCapex", "PrivateInventories",
             "GovConsumption", "PublicInvestment", "PublicInventories", "Exports", "Imports",
             "Discrepancy"]
    contrib = pd.Series(contrib)[order]
    demand_qoq = float(contrib.sum())
    qoq, supply_qoq = demand_qoq, np.nan
    if cfg.supply_spec and cfg.supply_weight > 0:
        supply_qoq, r2["Supply"] = _bridge(gq["GDP"], xq, cfg.supply_spec, target)
        w = cfg.supply_weight
        qoq = (1 - w) * demand_qoq + w * supply_qoq
        # show the blend as an adjustment line so contributions still add up
        contrib["SupplyBlend"] = qoq - demand_qoq
        growth["SupplyBlend"] = np.nan
    saar = ((1 + qoq / 100) ** 4 - 1) * 100
    nc = Nowcast(target, saar, qoq, contrib, pd.Series(growth)[contrib.index], observed, r2)
    nc.demand_saar = ((1 + demand_qoq / 100) ** 4 - 1) * 100
    nc.supply_saar = ((1 + supply_qoq / 100) ** 4 - 1) * 100 if supply_qoq == supply_qoq else np.nan
    return nc


# 改良版 (2026-10-16): サービス輸出入・公共工事出来高を追加、在庫寄与ゼロ、
# 供給側（鉱工業生産＋労働投入量）と 50:50 合成。2021Q3-2026Q2 と 2016Q2-2019Q4 の両期間で検証済み。
CONFIG_V2 = Config(
    bridges={**BRIDGES,
             "Exports": [("real_exports", 0), ("services_credit", 0)],
             "Imports": [("real_imports", 0), ("services_debit", 0)],
             "PublicInvestment": [("public_const", 0)]},
    inventory="zero",
    supply_spec=[("iip", 0), ("labor_input", 0)],
    supply_weight=0.5,
)
# v3 (2026-10-16): 個人消費に総消費動向指数（CTI）を追加、生産系の未公表月を
# 製造工業生産予測指数で補外。QE1直前MAE 2021–26: 1.29→1.15, 2016–19: 1.06→1.01;
# 期の2か月目時点 2021–26: 2.02→1.75。
CONFIG_V3 = Config(
    bridges={**CONFIG_V2.bridges,
             "PrivateConsumption": [("cai_real", 0), ("cti_real", 0)]},
    inventory="zero",
    supply_spec=[("iip", 0), ("labor_input", 0)],
    supply_weight=0.5,
    survey=True,
)
CONFIGS = {"v1": CONFIG_V1, "v2": CONFIG_V2, "v3": CONFIG_V3}
CONFIG_DEFAULT = CONFIG_V3


def reconstruct_history(qe: pd.DataFrame, monthly: pd.DataFrame, target, until=None,
                        config: Config | None = None, with_factor: bool = True) -> pd.DataFrame:
    """Pseudo history: re-run the engines on past dates using the release calendar
    on *today's* (revised) data. Not a true real-time record — label it as such."""
    from .release import update_dates
    until = pd.Timestamp(until or pd.Timestamp.today().normalize())
    rows, last = [], None
    for d in update_dates(target, until):
        n = nowcast(qe, monthly, target, config=config, asof=d)
        key = round(n.saar, 3)
        if key != last:  # keep only dates where something changed
            row = dict(d=d, saar=n.saar, demand_saar=n.demand_saar, supply_saar=n.supply_saar)
            if with_factor:
                try:
                    from .factor import factor_nowcast
                    row["factor_saar"] = factor_nowcast(qe, monthly, target, asof=d).saar
                except Exception:
                    row["factor_saar"] = float("nan")
            rows.append(row)
            last = key
    return pd.DataFrame(rows)
