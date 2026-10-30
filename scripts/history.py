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

# series -> (統計名（作成機関）, 指標名, 単位). 単位 "%" の系列は値そのものが変化率。
SERIES = {
    "iip": ("鉱工業指数（経産省）", "鉱工業生産指数", "2020=100"),
    "invest_goods": ("鉱工業指数（経産省）", "投資財出荷指数（除輸送機械）", "2020=100"),
    "prod_goods_ship": ("鉱工業指数（経産省）", "鉱工業用生産財出荷指数", "2020=100"),
    "durable_ship": ("鉱工業指数（経産省）", "耐久消費財出荷指数", "2020=100"),
    "ship_capital": ("鉱工業指数（経産省）", "資本財出荷指数", "2020=100"),
    "ship_capital_ex": ("鉱工業指数（経産省）", "資本財出荷指数（除輸送機械）", "2020=100"),
    "ship_construction": ("鉱工業指数（経産省）", "建設財出荷指数", "2020=100"),
    "ship_durable": ("鉱工業指数（経産省）", "耐久消費財出荷指数", "2020=100"),
    "ship_nondurable": ("鉱工業指数（経産省）", "非耐久消費財出荷指数", "2020=100"),
    "stk_inv_total": ("鉱工業指数（経産省）", "鉱工業在庫指数", "2020=100"),
    "stk_inv_final": ("鉱工業指数（経産省）", "最終需要財在庫指数", "2020=100"),
    "stk_inv_prod": ("鉱工業指数（経産省）", "生産財在庫指数", "2020=100"),
    "plan_this_iip": ("製造工業生産予測指数（経産省）", "製造工業 当月見込み", "前月比%"),
    "plan_next_iip": ("製造工業生産予測指数（経産省）", "製造工業 翌月見込み", "前月比%"),
    "plan_this_invest_goods": ("製造工業生産予測指数（経産省）", "資本財（除輸送機械） 当月見込み", "前月比%"),
    "plan_next_invest_goods": ("製造工業生産予測指数（経産省）", "資本財（除輸送機械） 翌月見込み", "前月比%"),
    "plan_this_durable_ship": ("製造工業生産予測指数（経産省）", "耐久消費財 当月見込み", "前月比%"),
    "plan_next_durable_ship": ("製造工業生産予測指数（経産省）", "耐久消費財 翌月見込み", "前月比%"),
    "plan_this_prod_goods_ship": ("製造工業生産予測指数（経産省）", "鉱工業用生産財 当月見込み", "前月比%"),
    "plan_next_prod_goods_ship": ("製造工業生産予測指数（経産省）", "鉱工業用生産財 翌月見込み", "前月比%"),
    "tert_total": ("第3次産業活動指数（経産省）", "第3次産業総合", "2020=100"),
    "tert_personal": ("第3次産業活動指数（経産省）", "広義対個人サービス", "2020=100"),
    "tert_business": ("第3次産業活動指数（経産省）", "広義対事業所サービス", "2020=100"),
    "real_exports": ("実質輸出入（日銀）", "実質輸出", "2020=100"),
    "real_imports": ("実質輸出入（日銀）", "実質輸入", "2020=100"),
    "real_ex_capital": ("実質輸出入（日銀）", "実質輸出・資本財", "2020=100"),
    "cai_real": ("消費活動指数（日銀）", "実質消費活動指数（旅行収支調整済）", "2020=100"),
    "services_credit": ("国際収支（日銀・財務省）", "サービス受取（季調）", "億円"),
    "services_debit": ("国際収支（日銀・財務省）", "サービス支払（季調）", "億円"),
    "bop_goods_ex": ("国際収支（日銀・財務省）", "輸出（季調）", "億円"),
    "bop_goods_im": ("国際収支（日銀・財務省）", "輸入（季調）", "億円"),
    "px_export": ("企業物価指数（日銀）", "輸出物価指数（円ベース）", "2020=100"),
    "px_import": ("企業物価指数（日銀）", "輸入物価指数（円ベース）", "2020=100"),
    "px_services": ("企業向けサービス価格指数（日銀）", "総平均（除く消費税）", "2020=100"),
    "cti_real": ("消費動向指数（総務省）", "総消費動向指数（実質）", "2025=100"),
    "public_const_raw": ("建設総合統計（国交省）", "公共工事出来高", "億円"),
    "priv_res_const_raw": ("建設総合統計（国交省）", "民間住宅工事出来高", "億円"),
    "priv_nonres_const_raw": ("建設総合統計（国交省）", "民間非住宅建築＋土木出来高", "億円"),
    "machine_orders": ("機械受注統計（内閣府）", "船舶・電力を除く民需（コア）", "百万円"),
    "housing_starts": ("景気動向指数 個別系列（内閣府）", "新設住宅着工床面積", "千㎡"),
    "labor_input": ("景気動向指数 個別系列（内閣府）", "労働投入量指数（調査産業計）", "2020=100"),
    "new_job_offers": ("景気動向指数 個別系列（内閣府）", "新規求人数（除学卒）", "人"),
    "jobs_ratio": ("景気動向指数 個別系列（内閣府）", "有効求人倍率（除学卒）", "倍"),
    "consumer_conf": ("景気動向指数 個別系列（内閣府）", "消費者態度指数", "pt"),
    "sme_sales_di": ("景気動向指数 個別系列（内閣府）", "中小企業売上げ見通しDI", "%pt"),
    "export_volume": ("景気動向指数 個別系列（内閣府）", "輸出数量指数", "2020=100"),
    "inv_ratio_final": ("景気動向指数 個別系列（内閣府）", "最終需要財在庫率指数", "2020=100"),
    "inv_ratio_prod": ("景気動向指数 個別系列（内閣府）", "鉱工業用生産財在庫率指数", "2020=100"),
    "qe:GDP": ("四半期別GDP速報（内閣府）", "実質GDP（季調, 2020年連鎖価格）", "十億円"),
}
LABELS = {k: v[1] for k, v in SERIES.items()}
PLAN_PREFIX = ("plan_this_", "plan_next_")
QE_LABEL = "QE"


def _month(ref: str, quarterly: bool = False) -> str:
    t = pd.Timestamp(ref)
    if quarterly:
        q = (t.month - 1) // 3 + 1
        return f"{t.year}年{q * 3 - 2}-{q * 3}月期"
    return f"{t.month}月"


def describe_arrivals(arr: pd.DataFrame, keep_revisions: int = 6) -> str:
    """Compact summary grouped by statistic, e.g. '鉱工業指数（経産省） 9月分、実質輸出入（日銀） 9月分／6〜8月改定'."""
    if arr is None or arr.empty:
        return "なし"
    groups, n_new_series = {}, 0
    for _, r in arr.iterrows():
        s = r["series"]
        if r["new_series"]:
            n_new_series += 1
            continue
        if s.startswith("qe:") and s != "qe:GDP":
            continue
        stat = SERIES.get(s, (s,))[0]
        q = s.startswith("qe:")
        g = groups.setdefault(stat, {"new": set(), "rev": set(), "q": q})
        latest = max(r["new"] + r["revised"]) if (r["new"] or r["revised"]) else None
        g["new"].update(r["new"])
        g["rev"].update(d for d in r["revised"]
                        if latest and pd.Timestamp(d) > pd.Timestamp(latest) - pd.DateOffset(months=keep_revisions))
    parts = []
    for stat, g in groups.items():
        rev = g["rev"] - g["new"]
        bits = []
        if g["new"]:
            bits.append("・".join(_month(d, g["q"]) for d in sorted(g["new"])[-3:]) + ("" if g["q"] else "分"))
        if rev:
            bits.append("・".join(_month(d, g["q"]) for d in sorted(rev)[-3:]) + "改定")
        if bits:
            parts.append(f"{stat} " + "／".join(bits))
    if n_new_series:
        parts.append(f"（系列の新規追加 {n_new_series}）")
    return "、".join(parts) if parts else "なし"

def _fmt(v, unit):
    if v is None or pd.isna(v):
        return "—"
    if unit in ("億円", "百万円", "人", "千㎡", "十億円"):
        return f"{v:,.0f}"
    if unit == "倍":
        return f"{v:.2f}"
    if unit == "前月比%":
        return f"{v:+.1f}%"
    return f"{v:,.1f}"


def detail_table(vals: pd.DataFrame, keep_revisions: int = 6) -> list[str]:
    """Markdown table: 統計 | 指標 | 対象 | 値 | 前期比 | 改定前 | 単位 (newest months and
    recent revisions only; brand-new series and long-ago revisions are counted, not listed)."""
    if vals is None or vals.empty:
        return []
    v = vals[~vals["new_series"]].copy()
    v = v[~v["series"].str.startswith("qe:") | (v["series"] == "qe:GDP")]
    n_series_added = vals.loc[vals["new_series"], "series"].nunique()
    v["ref"] = pd.to_datetime(v["ref_date"])
    latest = v.groupby("series")["ref"].transform("max")
    old_rev = v["old"].notna() & (v["ref"] <= latest - pd.DateOffset(months=keep_revisions))
    n_old = int(old_rev.sum())
    v = v[~old_rev]
    # skip revisions that are pure float noise
    v = v[v["old"].isna() | ((v["value"] - v["old"]).abs() > 1e-6)]
    rows = []
    order = {k: i for i, k in enumerate(SERIES)}
    v["o"] = v["series"].map(order).fillna(999)
    for _, r in v.sort_values(["o", "ref"]).iterrows():
        stat, name, unit = SERIES.get(r["series"], ("—", r["series"], ""))
        q = r["series"].startswith("qe:")
        when = _month(r["ref_date"], q) if q else f"{r['ref']:%Y年}{r['ref'].month}月"
        if unit == "前月比%" or r["prev"] is None or pd.isna(r["prev"]) or r["prev"] == 0:
            g = "—"
        else:
            g = f"{round((r['value'] / r['prev'] - 1) * 100, 1) + 0.0:+.1f}%"
        val, old = _fmt(r["value"], unit), ("（新規）" if pd.isna(r["old"]) else _fmt(r["old"], unit))
        if val == old:  # revision hidden by rounding -> show two decimals
            val, old = f"{r['value']:,.2f}", f"{r['old']:,.2f}"
        line = f"| {stat} | {name} | {when} | {val} | {g} | {old} | {unit} |"
        if line not in rows:  # same index published in two METI tables (e.g. 耐久消費財出荷)
            rows.append(line)
    out = []
    if rows:
        out += ["| 統計 | 指標 | 対象 | 値 | 前期比 | 改定前 | 単位 |", "|---|---|---|---|---|---|---|"] + rows
    notes = []
    if n_series_added:
        notes.append(f"新しく追加した系列 {n_series_added}（全期間を取得、表は省略）")
    if n_old:
        notes.append(f"{keep_revisions}か月以上前の改定 {n_old}件（表は省略）")
    if notes:
        out += ["", "※ " + "、".join(notes)]
    return out


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
             "各回の下の表は、前回の記録から今回までに入ったデータ（新しい月の値と改定）。前期比は前の月（QEは前の期）からの変化率、"
             "製造工業生産予測指数は値そのものが前月比の見込み。",
             "記録の日時は自動実行の時刻で、GitHub の混雑により統計の公表日より遅れることがある。", ""]
    for target, g in list(runs.groupby("target", sort=False))[::-1]:
        lines += [f"## {_qlabel(target)}", "",
                  "| 記録日時 | ① QE1予想 | 変化 | ② 景気因子 | 主な要因（①への寄与） | 入ったデータ |",
                  "|---|---|---|---|---|---|"]
        rows, details, prev, prev_saar = [], [], None, None
        for _, r in g.iterrows():
            run_at = r["run_at"]
            key = run_at.isoformat(timespec="seconds")
            nw = news[(news["run_at"] == key) & (news["model"].str.split("-").str[0] == model)]
            items = nw[nw["item"] != "（内訳なし）"].copy()
            items = items[items["contrib"].abs() >= 0.005].sort_values("contrib", key=abs, ascending=False)
            drivers = "、".join(f"{i} {v:+.2f}" for i, v in zip(items["item"], items["contrib"])) or (
                "初回" if prev is None else "変化なし")
            change = "—" if prev is None else f"{r['saar'] - prev_saar:+.2f}"
            fac = "" if pd.isna(r.get("factor_saar")) else f"{r['factor_saar']:+.2f}%"
            if prev is None:
                got, det = "初回（全データ取得）", []
            else:
                got = describe_arrivals(vintage.arrivals(prev, run_at, con=con))
                det = detail_table(vintage.arrival_values(prev, run_at, con=con))
            rows.append(f"| {run_at:%Y-%m-%d %H:%M} | {r['saar']:+.2f}% | {change} | {fac} | {drivers} | {got} |")
            head = f"### {run_at:%Y-%m-%d %H:%M}　① {r['saar']:+.2f}%（{change}）"
            details.append([head, ""] + (det or ["入ったデータなし" if prev is not None else "初回（全データ取得）"]) + [""])
            prev, prev_saar = run_at, r["saar"]
        lines += rows[::-1] + ["", "### 各回に入ったデータ", ""]
        for d in details[::-1]:
            lines += ["#" + d[0]] + d[1:]
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
