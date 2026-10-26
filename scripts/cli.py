"""Command line entry.

python3 -m scripts.cli nowcast  [--quarter 2026-07-01] [--model v2] [--out DIR] [--no-save]
                                   # --out で1枚まとめ画像 report_<期>_<日付>.png を出力
python3 -m scripts.cli nowcast  --asof 2026-10-22      # 保存済みヴィンテージで当時を再現
python3 -m scripts.cli history  [--quarter 2026-07-01] [--actual 0.4] [--out DIR]
python3 -m scripts.cli backtest [--start 2021-07-01] [--mode dates|months] [--model v2] [--out DIR]
python3 -m scripts.cli bands                            # 予測区間テーブルを作り直す（数十分）

nowcast prints/plots two engines: ① QE1予想（需要積み上げ＋供給側, v2）and
② 景気因子（混合頻度DFM, scripts/factor.py）, each with a 90% interval
(scripts/error_bands.csv), plus a news decomposition of ① vs the previous run.

Every `nowcast` run (without --asof / --no-save) stores today's data snapshot and
the result in $JP_GDPNOW_HOME/vintages.sqlite (default ~/.jp-gdpnow).
"""
from __future__ import annotations

import argparse
import dataclasses
import functools
import os
from datetime import date

import pandas as pd

from . import backtest, bands, data, model, news, plots, vintage


def _target_from_qe(qe: pd.DataFrame) -> pd.Timestamp:
    return qe.index[-1] + pd.offsets.QuarterBegin(1, startingMonth=1)


def _config(a):
    cfg = model.CONFIGS[a.model]
    return dataclasses.replace(cfg, fill=a.fill) if getattr(a, "fill", None) else cfg


def cmd_nowcast(a):
    if a.asof:
        monthly = data.transform_monthly(vintage.load_asof(a.asof))
        qe = vintage.load_asof(a.asof, prefix="qe:")
        label = f"vintage {a.asof}"
        if monthly.empty or qe.empty:
            raise SystemExit(f"no stored vintage on or before {a.asof}")
    else:
        raw = data.get_monthly_raw()
        monthly = data.transform_monthly(raw)
        qe = data.get_qe_levels()
        label = data.latest_qe_info().get("label", "")
        if not a.no_save:
            n1 = vintage.save_snapshot(raw)
            n2 = vintage.save_snapshot(qe, prefix="qe:")
            print(f"[vintage] {date.today()} snapshot: {n1 + n2} new/revised values -> {vintage.home()}")
            if a.only_if_new and n1 + n2 == 0:
                print("[nowcast] no new data since the last snapshot -> nothing to do")
                return None
    target = pd.Timestamp(a.quarter) if a.quarter else _target_from_qe(qe)
    asof_ts = pd.Timestamp(a.asof) if a.asof else None
    today = pd.Timestamp(a.asof or date.today())
    cfg = _config(a)
    nc = model.nowcast(qe, monthly, target, config=cfg, asof=asof_ts)

    # ② factor engine
    fnc = None
    if not a.no_factor:
        try:
            from .factor import factor_nowcast
            fnc = factor_nowcast(qe, monthly, target, asof=asof_ts)
        except Exception as ex:
            print(f"[factor] failed: {ex}")

    # prediction intervals (90%)
    bd = {"bridge": bands.interval("bridge", nc.saar, target, today)}
    if fnc is not None:
        bd["factor"] = bands.interval("factor", fnc.saar, target, today)

    print(f"QE: {label}")
    print(nc.summary())
    if nc.supply_saar == nc.supply_saar:
        print(f"  内訳: 需要側 {nc.demand_saar:+.1f}% / 供給側 {nc.supply_saar:+.1f}%（50:50で合成）")
    lo, hi = bd["bridge"]
    print(f"  ① QE1予想 {nc.saar:+.2f}%  90%区間 [{lo:+.1f}, {hi:+.1f}]")
    if fnc is not None:
        lo, hi = bd["factor"]
        print(f"  ② 景気因子 {fnc.saar:+.2f}%  90%区間 [{lo:+.1f}, {hi:+.1f}]  （{fnc.n_series}系列）")
    print(nc.table().to_string())

    # news decomposition of ① vs the previous run (real vintage) or, if none, a pseudo vintage
    eng = lambda q, m, T: model.nowcast(q, m, T, config=cfg).saar
    nw, nw_label, prev = None, "", None
    try:
        now = pd.Timestamp(a.asof) if a.asof else pd.Timestamp.now()
        mname = a.model + (f"-{a.fill}" if a.fill else "")
        prev = None if a.news_from else vintage.last_run_before(str(target.date()), now, model=mname)
        if prev is not None:
            t0 = pd.Timestamp(prev["run_at"])
            m_old = data.transform_monthly(vintage.load_asof(t0))
            q_old = vintage.load_asof(t0, prefix="qe:")
            nw = news.decompose(eng, target, q_old, m_old, qe, monthly)
            nw_label = f"{t0:%-m/%-d %H:%M}→{now:%-m/%-d %H:%M}"
        else:
            from .release import update_dates
            if a.news_from:
                past = [pd.Timestamp(a.news_from)]
            else:  # default: the last release round at least two weeks back
                past = [d for d in update_dates(target, today) if d <= today - pd.Timedelta(days=14)]
            if past:
                d0 = past[-1]
                nw = news.pseudo(eng, target, qe, monthly, d0, today)
                nw_label = f"{d0:%-m/%-d}→{today:%-m/%-d}・再構成"
        if nw is not None:
            print(f"ニュース分解（①, {nw_label}）"); print(nw.round(2).to_string(index=False))
    except Exception as ex:
        print(f"[news] failed: {ex}")

    if not a.asof and not a.no_save:
        mname = a.model + (f"-{a.fill}" if a.fill else "")
        run_at = vintage.log_run(nc, mname, qe_label=label,
                                 factor_saar=None if fnc is None else fnc.saar)
        basis = ("初回" if nw is None else
                 f"前回 {pd.Timestamp(prev['run_at']).isoformat(timespec='seconds')}" if prev is not None
                 else f"再構成 {nw_label}")
        vintage.log_news(run_at, target, mname, basis, nw)
        # human-readable records next to the DB (diff-friendly in git)
        vintage.runs().to_csv(os.path.join(vintage.home(), "runs.csv"), index=False,
                              encoding="utf-8-sig")
        try:
            from . import history
            print(f"[history] {history.build(a.model)}")
        except Exception as ex:
            print(f"[history] failed: {ex}")
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        stamp = str(today.date()).replace("-", "")
        nc.table().to_csv(os.path.join(a.out, "nowcast_components.csv"), encoding="utf-8-sig")
        if nw is not None:
            nw.to_csv(os.path.join(a.out, "nowcast_news.csv"), index=False, encoding="utf-8-sig")
        try:
            runs = vintage.runs(str(target.date()))
            runs = runs[runs["model"].str.split("-").str[0] == a.model]  # one model's record only
        except Exception:
            runs = None
        rp = os.path.join(a.out, f"report_{target.year}Q{(target.month - 1) // 3 + 1}_{stamp}.png")
        recon = None
        if not a.no_recon:
            recon = model.reconstruct_history(qe, monthly, target, until=today, config=cfg,
                                              with_factor=fnc is not None)
            if runs is not None and not runs.empty:  # drop reconstructed points on/after the first real run
                first = pd.to_datetime(runs["run_at"]).min().normalize()
                recon = recon[recon["d"] < first]
        plots.plot_report(nc, monthly, runs, rp, asof=today, qe_label=label, model_name=a.model,
                          recon=recon, factor=fnc, bands=bd, news=nw, news_label=nw_label)
        import shutil
        shutil.copyfile(rp, os.path.join(a.out, "latest.png"))  # fixed name for the README
        print(f"[report] {rp}")
    return nc


def cmd_bands(a):
    """Rebuild scripts/error_bands.csv from calendar-date backtests of both engines."""
    import functools as ft
    from .factor import factor_nowcast
    monthly = data.get_monthly()
    res = {}
    for start, end in [("2016-04-01", "2019-10-01"), (a.start, None)]:
        b1 = backtest.run(start=start, end=end, monthly=monthly, mode="dates",
                          nowcast_fn=ft.partial(model.nowcast, config=model.CONFIGS[a.model]))
        b2 = backtest.run_engine(lambda q, m, T, d: factor_nowcast(q, m, T, asof=d).saar,
                                 start=start, end=end, monthly=monthly)
        res.setdefault("bridge", []).append(b1)
        res.setdefault("factor", []).append(b2)
    res = {k: pd.concat(v) for k, v in res.items()}
    print(bands.build(res).round(2).to_string(index=False))


def cmd_history(a):
    r = vintage.runs(a.quarter)
    if r.empty:
        raise SystemExit("no logged runs yet")
    target = a.quarter or r["target"].iloc[-1]
    r = r[r["target"] == str(pd.Timestamp(target).date())]
    print(r[["run_at", "model", "saar", "demand_saar", "supply_saar"]].round(2).to_string(index=False))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        plots.plot_evolution(r, os.path.join(a.out, "nowcast_evolution.png"), actual=a.actual)


def cmd_backtest(a):
    bt = backtest.run(start=a.start, mode=a.mode,
                      nowcast_fn=functools.partial(model.nowcast, config=_config(a)))
    sc = backtest.scores(bt)
    print(bt.round(2).to_string())
    print(sc.to_string())
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        bt.round(3).to_csv(os.path.join(a.out, "backtest.csv"), encoding="utf-8-sig")
        sc.to_csv(os.path.join(a.out, "backtest_scores.csv"), encoding="utf-8-sig")
        if a.mode == "months":
            plots.plot_backtest(bt, os.path.join(a.out, "backtest.png"))
    return bt


def main(argv=None):
    p = argparse.ArgumentParser(prog="jp-gdpnow")
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("nowcast")
    n.add_argument("--quarter", help="target quarter start, e.g. 2026-07-01")
    n.add_argument("--asof", help="re-run on the stored vintage of this date")
    n.add_argument("--no-save", action="store_true")
    n.add_argument("--only-if-new", action="store_true",
                   help="stop (no run logged) when today's snapshot added no new/revised values")
    n.add_argument("--no-recon", action="store_true", help="omit the reconstructed (pseudo) history")
    n.add_argument("--no-factor", action="store_true", help="skip the factor engine (②)")
    n.add_argument("--news-from", help="news decomposition vs this date (pseudo vintage)")
    bb = sub.add_parser("bands", help="rebuild prediction-interval table (slow)")
    bb.add_argument("--start", default="2021-07-01")
    bb.add_argument("--model", default="v3", choices=list(model.CONFIGS))
    h = sub.add_parser("history")
    h.add_argument("--quarter")
    h.add_argument("--actual", type=float, help="QE1 value to draw once it is out")
    b = sub.add_parser("backtest")
    b.add_argument("--start", default="2021-07-01")
    b.add_argument("--mode", default="dates", choices=["dates", "months"])
    for sp in (n, b):
        sp.add_argument("--model", default="v3", choices=list(model.CONFIGS))
        sp.add_argument("--fill", choices=["ar", "dfm"], help="override missing-month method")
    for sp in (n, h, b):
        sp.add_argument("--out")
    a = p.parse_args(argv)
    {"nowcast": cmd_nowcast, "history": cmd_history, "backtest": cmd_backtest,
     "bands": cmd_bands}[a.cmd](a)


if __name__ == "__main__":
    main()
