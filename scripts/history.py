"""Human-readable record of how the nowcast moved: data/HISTORY.md (+ data/news_log.csv).

    python -m scripts.history            # rebuild from the vintage store
    python -m scripts.history --backfill # recompute news for logged runs that have none

For every logged run of the default model: the nowcast, the change from the previous
run, the news decomposition (which statistic moved it, %pt SAAR) and which data arrived
in the store since the previous run (new months / revisions).
"""
from __future__ import annotations

import argparse
import os

import pandas as pd

from . import vintage

LABELS = {
    "cai_real": "消費活動指数（日銀）", "cti_real": "総消費動向指数",
    "real_exports": "実質輸出（日銀）", "real_imports": "実質輸入（日銀）",
    "real_ex_capital": "実質輸出・資本財（日銀）",
    "services_credit": "サービス受取（国際収支）", "services_debit": "サービス支払（国際収支）",
    "bop_goods_ex": "財輸出（国際収支）", "bop_goods_im": "財輸入（国際収支）",
    "px_export": "輸出物価", "px_import": "輸入物価", "px_services": "企業向けサービス価格",
    "iip": "鉱工業生産", "invest_goods": "投資財出荷（除輸送機械）",
    "prod_goods_ship": "生産財出荷", "durable_ship": "耐久消費財出荷",
    "machine_orders": "コア機械受注", "housing_starts": "住宅着工床面積",
    "labor_input": "労働投入量", "new_job_offers": "新規求人数", "jobs_ratio": "有効求人倍率",
    "consumer_conf": "消費者態度指数", "sme_sales_di": "中小企業売上見通しDI",
    "export_volume": "輸出数量指数", "inv_ratio_final": "最終需要財在庫率",
    "inv_ratio_prod": "生産財在庫率",
    "public_const_raw": "公共工事出来高", "priv_res_const_raw": "民間住宅工事出来高",
    "priv_nonres_const_raw": "民間非住宅工事出来高",
    "tert_total": "第3次産業活動指数", "tert_personal": "第3次産業・広義対個人",
    "tert_business": "第3次産業・広義対事業所",
}
PLAN_PREFIX = ("plan_this_", "plan_next_")
QE_LABEL = "QE"


def _month(ref: str, quarterly: bool = False) -> str:
    t = pd.Timestamp(ref)
    if quarterly:
        q = (t.month - 1) // 3 + 1
        return f"{t.year}年{q * 3 - 2}-{q * 3}月期"
    return f"{t.month}月"


def describe_arrivals(arr: pd.DataFrame, keep_revisions: int = 6) -> str:
    """Compact Japanese description, e.g. '鉱工業生産 8月改定、労働投入量 8月'."""
    if arr is None or arr.empty:
        return "なし"
    parts, plan_new, n_old_rev, n_new_series = [], set(), 0, 0
    for _, r in arr.iterrows():
        s = r["series"]
        if r["new_series"]:
            n_new_series += 1
            continue
        if s.startswith(PLAN_PREFIX):  # survey plans: one entry for the whole group
            plan_new.update(r["new"])
            continue
        if s.startswith("qe:"):
            if s == "qe:GDP":
                bits = [f"{_month(d, True)} 新規" for d in r["new"]]
                bits += [f"{_month(d, True)} 改定" for d in r["revised"][-2:]]
                parts.append("QE " + "・".join(bits))
            continue
        latest = max(r["new"] + r["revised"]) if (r["new"] or r["revised"]) else None
        recent_rev = [d for d in r["revised"]
                      if latest and pd.Timestamp(d) > pd.Timestamp(latest) - pd.DateOffset(months=keep_revisions)]
        n_old_rev += len(r["revised"]) - len(recent_rev)
        bits = []
        if r["new"]:
            bits.append("・".join(_month(d) for d in r["new"][-3:]) + "分")
        if recent_rev:
            bits.append("・".join(_month(d) for d in recent_rev[-3:]) + "改定")
        if bits:
            parts.append(f"{LABELS.get(s, s)} " + "／".join(bits))
    if plan_new:
        parts.append("製造工業生産予測 " + "・".join(_month(d) for d in sorted(plan_new)[-2:]) + "見込み")
    if n_new_series:
        parts.append(f"（系列の新規追加 {n_new_series}）")
    if n_old_rev:
        parts.append(f"（ほか過去分の改定 {n_old_rev}件）")
    return "、".join(parts) if parts else "なし"


def _qlabel(target) -> str:
    return _month(target, True)


def build(model: str = "v3", out_dir: str | None = None) -> str:
    """Write HISTORY.md and news_log.csv next to the vintage DB; returns the md path."""
    out_dir = out_dir or vintage.home()
    con = vintage.connect()
    runs = vintage.runs(con=con)
    runs = runs[runs["model"].str.split("-").str[0] == model].sort_values("run_at")
    news = vintage.news_log(con=con)
    news.to_csv(os.path.join(out_dir, "news_log.csv"), index=False, encoding="utf-8-sig")

    lines = ["# 予想の変化の記録（自動生成）", "",
             f"モデル {model} の記録（リアルタイム）。新しい順。寄与は ① QE1予想への寄与（前期比年率, %pt）。",
             "「入ったデータ」は前回の記録から今回までにデータベースに入った新しい月・改定。",
             "記録の日時は自動実行の時刻で、GitHub の混雑により統計の公表日より遅れることがある。", ""]
    for target, g in runs.groupby("target", sort=False):
        lines += [f"## {_qlabel(target)}", "",
                  "| 記録日時 | ① QE1予想 | 変化 | ② 景気因子 | 主な要因（①への寄与） | 入ったデータ |",
                  "|---|---|---|---|---|---|"]
        rows, prev = [], None
        for _, r in g.iterrows():
            run_at = r["run_at"]
            key = run_at.isoformat(timespec="seconds")
            nw = news[(news["run_at"] == key) & (news["model"].str.split("-").str[0] == model)]
            items = nw[nw["item"] != "（内訳なし）"].copy()
            items = items[items["contrib"].abs() >= 0.005].sort_values("contrib", key=abs, ascending=False)
            drivers = "、".join(f"{i} {v:+.2f}" for i, v in zip(items["item"], items["contrib"])) or (
                "初回" if prev is None else "変化なし")
            arr = vintage.arrivals(prev, run_at, con=con)
            got = "初回（全データ取得）" if prev is None else describe_arrivals(arr)
            change = "—" if prev is None else f"{r['saar'] - prev_saar:+.2f}"
            fac = "" if pd.isna(r.get("factor_saar")) else f"{r['factor_saar']:+.2f}%"
            rows.append(f"| {run_at:%Y-%m-%d %H:%M} | {r['saar']:+.2f}% | {change} | {fac} | {drivers} | {got} |")
            prev, prev_saar = run_at, r["saar"]
        lines += rows[::-1] + [""]
    con.close()
    path = os.path.join(out_dir, "HISTORY.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def backfill(model: str = "v3"):
    """Recompute the news decomposition for logged runs without one, from the stored vintages."""
    from . import data, model as mdl, news as nws
    con = vintage.connect()
    runs = vintage.runs(con=con)
    runs = runs[runs["model"] == model].sort_values("run_at")
    have = set(vintage.news_log(con=con)["run_at"])
    cfg = mdl.CONFIGS[model]
    eng = lambda q, m, T: mdl.nowcast(q, m, T, config=cfg).saar
    prev = None
    for _, r in runs.iterrows():
        key = r["run_at"].isoformat(timespec="seconds")
        if key not in have:
            if prev is None:
                vintage.log_news(key, r["target"], model, "初回", None, con=con)
            else:
                d0, d1 = prev, r["run_at"]
                nw = nws.decompose(eng, r["target"],
                                   vintage.load_asof(d0, prefix="qe:", con=con),
                                   data.transform_monthly(vintage.load_asof(d0, con=con)),
                                   vintage.load_asof(d1, prefix="qe:", con=con),
                                   data.transform_monthly(vintage.load_asof(d1, con=con)))
                vintage.log_news(key, r["target"], model,
                                 f"前回 {d0.isoformat(timespec='seconds')}", nw, con=con)
                print(f"[history] {key}: " + ", ".join(f"{a} {b:+.3f}" for a, b in zip(nw['統計'], nw['寄与'])))
        prev = r["run_at"]
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="v3")
    ap.add_argument("--backfill", action="store_true")
    a = ap.parse_args()
    if a.backfill:
        backfill(a.model)
    print(build(a.model))
