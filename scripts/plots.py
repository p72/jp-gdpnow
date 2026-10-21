"""Charts (white default style, Japanese labels via esri-data's jp_font)."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from . import data

BLUE, ORANGE, GRAY, AQUA = "#2a78d6", "#eb6834", "#555555", "#1baf7a"


def _font():
    try:
        return data.esri().setup_japanese_font() is not None
    except Exception:
        return False


def plot_backtest(bt: pd.DataFrame, path: str):
    jp = _font()
    fig, ax = plt.subplots(figsize=(10, 4.8))
    x = range(len(bt))
    labels = [f"{d.year}Q{(d.month - 1) // 3 + 1}" for d in bt.index]
    ax.bar(x, bt["actual_QE1"], color="#c9c9c9", width=0.6,
           label="QE1次速報（実績）" if jp else "QE 1st release")
    ax.plot(x, bt["nowcast_h3"], color=BLUE, lw=2, marker="o", ms=6,
            label="ナウキャスト（3か月分観測）" if jp else "Nowcast (3 months)")
    ax.plot(x, bt["nowcast_h1"], color=ORANGE, lw=2, ls="--", marker="s", ms=5,
            label="ナウキャスト（1か月分観測）" if jp else "Nowcast (1 month)")
    ax.axhline(0, color=GRAY, lw=0.8)
    ax.set_axisbelow(True)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("実質GDP 前期比年率（%）" if jp else "Real GDP q/q SAAR (%)")
    ax.set_title("日本版GDPNow（簡易版）バックテスト" if jp else "Japan GDPNow backtest")
    ax.grid(axis="y", color="#e5e5e5")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_contributions(nc, path: str):
    jp = _font()
    t = nc.table()["寄与度(%pt, 前期比)"]
    saar_c = t * 4  # approx annualized contribution
    fig, ax = plt.subplots(figsize=(8, 4.5))
    colors = [BLUE if v >= 0 else ORANGE for v in saar_c]
    ax.barh(saar_c.index[::-1], saar_c.values[::-1], color=colors[::-1], height=0.6)
    for i, v in enumerate(saar_c.values[::-1]):
        ax.text(v + (0.05 if v >= 0 else -0.05), i, f"{v:+.2f}",
                va="center", ha="left" if v >= 0 else "right", fontsize=8, color="#333")
    ax.axvline(0, color=GRAY, lw=0.8)
    ax.set_axisbelow(True)
    lo, hi = min(saar_c.min(), 0), max(saar_c.max(), 0)
    pad = (hi - lo) * 0.12
    ax.set_xlim(lo - pad, hi + pad)
    q = nc.quarter
    ttl = f"{q.year}年{q.month}-{q.month + 2}月期 ナウキャスト {round(nc.saar, 1) + 0.0:+.1f}%（年率）"
    ax.set_title(ttl if jp else f"Nowcast {q.date()} {nc.saar:+.1f}% SAAR")
    ax.set_xlabel("寄与度（%pt, 年率換算の近似）" if jp else "Contribution (pp, annualized approx.)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", color="#e5e5e5")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_compare(results: dict, path: str, title: str | None = None):
    """results: {label: backtest DataFrame}. Bars = QE1, one line per model (h=3)."""
    jp = _font()
    first = next(iter(results.values()))
    fig, ax = plt.subplots(figsize=(10, 4.8))
    x = range(len(first))
    labels = [f"{d.year}Q{(d.month - 1) // 3 + 1}" for d in first.index]
    ax.bar(x, first["actual_QE1"], color="#c9c9c9", width=0.6,
           label="QE1次速報（実績）" if jp else "QE 1st release")
    styles = [dict(color="#9a9a9a", ls="--", marker="s", ms=4), dict(color=BLUE, ls="-", marker="o", ms=6)]
    for (lab, bt), st in zip(results.items(), styles):
        mae = (bt["nowcast_h3"] - bt["actual_QE1"]).abs().mean()
        ax.plot(x, bt["nowcast_h3"], lw=2, label=f"{lab}（MAE {mae:.2f}）", **st)
    ax.axhline(0, color=GRAY, lw=0.8)
    ax.set_axisbelow(True)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("実質GDP 前期比年率（%）" if jp else "Real GDP q/q SAAR (%)")
    ax.set_title(title or ("日本版GDPNow 初版と改良版の比較（3か月分観測）" if jp else "v1 vs v2"))
    ax.grid(axis="y", color="#e5e5e5")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_evolution(runs: pd.DataFrame, path: str, actual: float | None = None):
    """GDPNow-style chart: how the nowcast for one quarter evolved run by run."""
    jp = _font()
    r = runs.sort_values("run_at")
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(r["run_at"], r["saar"], color=BLUE, lw=2, marker="o", ms=6,
            label="ナウキャスト" if jp else "Nowcast")
    if r["supply_saar"].notna().any():
        ax.plot(r["run_at"], r["demand_saar"], color="#9a9a9a", lw=1.2, ls="--",
                label="需要側" if jp else "Demand side")
        ax.plot(r["run_at"], r["supply_saar"], color=ORANGE, lw=1.2, ls=":",
                label="供給側" if jp else "Supply side")
    if actual is not None:
        ax.axhline(actual, color="#333", lw=1, ls="-.",
                   label=f"QE1次速報 {actual:+.1f}%" if jp else f"QE1 {actual:+.1f}%")
    last = r.iloc[-1]
    ax.annotate(f"{last['saar']:+.1f}%", (last["run_at"], last["saar"]),
                textcoords="offset points", xytext=(6, 6), fontsize=9, color="#333")
    ax.axhline(0, color=GRAY, lw=0.8)
    ax.set_axisbelow(True)
    t = pd.Timestamp(last["target"])
    ax.set_title(f"日本版GDPNow {t.year}年{t.month}-{t.month + 2}月期の推移" if jp
                 else f"Japan GDPNow evolution {t.date()}")
    ax.set_ylabel("実質GDP 前期比年率（%）" if jp else "Real GDP q/q SAAR (%)")
    ax.grid(axis="y", color="#e5e5e5")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- one-page report
INPUT_LABELS = [  # (series, 表示名, 単位, 割る数)
    ("cai_real", "実質消費活動指数", "2020=100", 1),
    ("cti_real", "総消費動向指数（CTI）", "2025=100", 1),
    ("real_exports", "実質輸出", "2020=100", 1),
    ("real_imports", "実質輸入", "2020=100", 1),
    ("services_credit", "サービス受取", "億円", 1),
    ("services_debit", "サービス支払", "億円", 1),
    ("invest_goods", "投資財出荷（除輸送機械）", "2020=100", 1),
    ("machine_orders", "コア機械受注 ※1期ラグ", "億円", 100),
    ("housing_starts", "住宅着工床面積 ※1・2期ラグ", "千㎡", 1),
    ("public_const", "公共工事出来高", "億円", 1),
    ("iip", "鉱工業生産", "2020=100", 1),
    ("labor_input", "労働投入量", "2020=100", 1),
]


def _fmt(v, big):
    return f"{v:,.0f}" if big else f"{v:,.1f}"


def plot_report(nc, monthly: pd.DataFrame, runs: pd.DataFrame | None, path: str,
                asof=None, qe_label: str = "", model_name: str = "v3", watermark: str | None = None,
                recon: pd.DataFrame | None = None, factor=None, bands: dict | None = None,
                news: pd.DataFrame | None = None, news_label: str = ""):
    """One 16:9 slide: headline, input table, contributions, evolution."""
    from .model import fill_ar

    jp = _font()
    INK, INK2, MUTED, GRID = "#1f1f1f", "#555555", "#8a8a8a", "#e5e5e5"
    t = nc.quarter
    q_months = pd.date_range(t, periods=3, freq="MS")
    p_months = pd.date_range(t - pd.offsets.MonthBegin(3), periods=3, freq="MS")
    asof = pd.Timestamp(asof or pd.Timestamp.today()).date()

    fig = plt.figure(figsize=(16, 9), dpi=120)
    fig.patch.set_facecolor("white")
    gs = fig.add_gridspec(3, 2, height_ratios=[0.21, 0.41, 0.38], width_ratios=[1.0, 1.08],
                          left=0.085, right=0.98, top=0.96, bottom=0.07, hspace=0.42, wspace=0.12)

    # ---- headline
    bands = bands or {}
    ax = fig.add_subplot(gs[0, :]); ax.axis("off")
    ax.text(0, 1.0, f"日本版GDPNow  {t.year}年{t.month}-{t.month + 2}月期 実質GDP（前期比年率）",
            fontsize=17, color=INK, va="top", weight="bold")

    def _big(x0, label, val, color, band):
        ax.text(x0, 0.56, label, fontsize=11, color=INK2, va="bottom")
        ax.text(x0, 0.0, f"{round(val, 1) + 0.0:+.1f}%", fontsize=38, color=color, va="bottom", weight="bold")
        if band and band[0] == band[0]:
            ax.text(x0 + 0.135, 0.06, f"90%区間\n{band[0]:+.1f} 〜 {band[1]:+.1f}",
                    fontsize=9.5, color=MUTED, va="bottom", linespacing=1.3)

    _big(0.0, "① QE1予想（需要積み上げ＋供給）", nc.saar, BLUE, bands.get("bridge"))
    if factor is not None:
        _big(0.255, "② 景気因子（混合頻度DFM）", factor.saar, AQUA, bands.get("factor"))
    sub = []
    if nc.supply_saar == nc.supply_saar:
        sub.append(f"①の内訳：需要側 {nc.demand_saar:+.1f}% ／ 供給側 {nc.supply_saar:+.1f}%")
    sub.append(f"データ時点 {asof}　前期の基準 {qe_label}　モデル {model_name}")
    ax.text(0.51, 0.22, "\n".join(sub), fontsize=10.5, color=INK2, va="center", linespacing=1.7)
    if news is not None and len(news) > 1:
        body = news[news["統計"] != "合計"].copy()
        body = body.reindex(body["寄与"].abs().sort_values(ascending=False).index).head(4)
        tot = float(news.loc[news["統計"] == "合計", "寄与"].iloc[0])
        lines = [f"①の改定 {news_label}：{tot:+.2f}pt"]
        lines += [f"  {r['統計']} {r['寄与']:+.2f}" for _, r in body.iterrows()]
        ax.text(1.0, 0.98, "\n".join(lines), fontsize=9.5, color=INK2, va="top", ha="right",
                linespacing=1.45)

    # ---- input table (right, spans two rows)
    ax = fig.add_subplot(gs[1:, 1]); ax.axis("off")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.text(0, 1.0, "使用した月次指標", fontsize=13, color=INK, weight="bold", va="bottom")
    cols = ["統計", "単位", "前期平均"] + [f"{m.month}月" for m in q_months] + ["前期比"]
    xs = [0.0, 0.37, 0.50, 0.63, 0.73, 0.83, 0.93]
    aligns = ["left", "left", "right", "right", "right", "right", "right"]
    xr = [x if a == "left" else x + 0.085 for x, a in zip(xs, aligns)]
    xr[-1] = 1.0
    y = 0.95
    for x, a, c in zip(xr, aligns, cols):
        ax.text(x, y, c, fontsize=10, color=INK2, ha=a, va="top", weight="bold")
    ax.plot([0, 1], [y - 0.045, y - 0.045], color=INK2, lw=0.8)
    row_h = 0.043
    y -= 0.045
    for s, name, unit, div in INPUT_LABELS:
        if s not in monthly:
            continue
        obs = monthly[s].dropna()
        f = fill_ar(obs, q_months[-1]) / div
        prev_avg = f.reindex(p_months).mean()
        cur_avg = f.reindex(q_months).mean()
        big = f.abs().max() >= 1000
        cells = [name, unit, _fmt(prev_avg, big)]
        styles = [{}, {}, {}]
        for m in q_months:
            v = f.get(m, float("nan"))
            cells.append(_fmt(v, big))
            styles.append({} if m in obs.index else {"color": MUTED, "style": "italic"})
        g = (cur_avg / prev_avg - 1) * 100
        cells.append(f"{g:+.1f}%")
        styles.append({"color": BLUE if g >= 0 else ORANGE, "weight": "bold"})
        for x, a, c, st in zip(xr, aligns, cells, styles):
            kw = dict(fontsize=10.5, color=INK, ha=a, va="center")
            kw.update(st)
            ax.text(x, y - row_h / 2 + 0.008, c, **kw)
        ax.plot([0, 1], [y - row_h + 0.01, y - row_h + 0.01], color=GRID, lw=0.6)
        y -= row_h
    ax.text(0, y - 0.005, "灰色の数字＝未公表月（AR(2)で補完）。前期比＝当期3か月平均／前期3か月平均。"
            "政府消費は直近4期平均、在庫は寄与ゼロ。",
            fontsize=8.5, color=MUTED, va="top")

    # ---- upcoming releases (compact)
    from .release import upcoming
    up = upcoming(asof, 12)
    if not up.empty:
        y -= 0.04
        ax.text(0, y, "今後の公表予定", fontsize=12, color=INK, weight="bold", va="top")
        y -= 0.038
        sx = [0.0, 0.46, 0.60, 0.88]
        for x, h in zip(sx, ["統計", "次の対象", "公表日", "確度"]):
            ax.text(x, y, h, fontsize=9, color=INK2, weight="bold", va="top")
        y -= 0.024
        ax.plot([0, 1], [y, y], color=INK2, lw=0.6)
        step = max(0.024, min(0.028, (y + 0.03) / max(len(up), 1)))
        for _, rw in up.iterrows():
            y -= 0.005
            star = "QE" in rw["items"]
            st = dict(fontsize=9, va="top", color=BLUE if star else INK, weight="bold" if star else "normal")
            ax.text(sx[0], y, rw["items"], **st)
            ax.text(sx[1], y, rw["month"], **st)
            ax.text(sx[2], y, rw["display"], **st)
            ax.text(sx[3], y, rw["est"], fontsize=9, va="top",
                    color=MUTED if rw["est"] == "推定" else INK2)
            y -= step - 0.005

    # ---- contributions (left middle)
    ax = fig.add_subplot(gs[1, 0])
    c = nc.table()["寄与度(%pt, 前期比)"] * 4
    c = c[c.abs() > 1e-6].rename(index={"供給側との合成調整": "供給側との合成", "連鎖不突合等": "不突合等"})
    colors = [BLUE if v >= 0 else ORANGE for v in c]
    ax.barh(c.index[::-1], c.values[::-1], color=colors[::-1], height=0.62)
    for i, v in enumerate(c.values[::-1]):
        ax.text(v + (0.04 if v >= 0 else -0.04), i, f"{v:+.2f}", va="center",
                ha="left" if v >= 0 else "right", fontsize=9, color=INK2)
    lo, hi = min(c.min(), 0), max(c.max(), 0)
    pad = (hi - lo) * 0.16
    ax.set_xlim(lo - pad, hi + pad)
    ax.axvline(0, color=GRAY, lw=0.8)
    ax.set_title("需要項目別の寄与度（%pt, 年率換算の近似）", fontsize=12, color=INK, loc="left")
    ax.tick_params(labelsize=10)
    ax.grid(axis="x", color=GRID); ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)

    # ---- evolution (left bottom)
    import matplotlib.dates as mdates
    ax = fig.add_subplot(gs[2, 0])
    ax.set_title("推計の推移（白抜き＝再構成・参考、塗り＝実際の記録）", fontsize=12, color=INK, loc="left")
    vals = []
    series = [("saar", BLUE, "① QE1予想", "bridge"), ("factor_saar", AQUA, "② 景気因子", "factor")]
    for col, color, lab, eng in series:
        if recon is not None and not recon.empty and col in recon:
            ax.plot(recon["d"], recon[col], color=color, lw=1.4, ls="--", marker="o", ms=5.5,
                    mfc="white", mew=1.4)
            vals += list(recon[col].dropna())
        last_d, last_v = None, None
        if runs is not None and not runs.empty and col in runs:
            r = runs.sort_values("run_at").copy()
            r["d"] = pd.to_datetime(r["run_at"]).dt.normalize()
            r = r.groupby("d").last().reset_index().dropna(subset=[col])
            if not r.empty:
                ax.plot(r["d"], r[col], color=color, lw=2.2, marker="o", ms=8, label=lab)
                vals += list(r[col])
                last_d, last_v = r["d"].iloc[-1], r[col].iloc[-1]
        if last_d is None and recon is not None and not recon.empty and col in recon:
            ax.plot([], [], color=color, lw=2.2, marker="o", ms=8, label=lab)
            last_d, last_v = recon["d"].iloc[-1], recon[col].iloc[-1]
        bd = bands.get(eng)
        if last_d is not None and bd and bd[0] == bd[0]:
            off = pd.Timedelta(days=2 if eng == "factor" else -2)
            ax.errorbar([last_d + off], [last_v], yerr=[[last_v - bd[0]], [bd[1] - last_v]],
                        color=color, alpha=0.45, lw=1.2, capsize=4)
            vals += [bd[0], bd[1]]
        if last_d is not None:
            ax.annotate(f"{last_v:+.1f}", (last_d, last_v), textcoords="offset points",
                        xytext=(12, 10 if eng == "factor" else -10), ha="left",
                        va="center", fontsize=9.5, color=color, weight="bold")
    if vals:
        vals = [v for v in vals if v == v]
        lo, hi = min(min(vals), 0), max(max(vals), 0)
        pad = max((hi - lo) * 0.2, 0.5)
        ax.set_ylim(lo - pad, hi + pad * 2.2)
        qe1 = t + pd.offsets.MonthBegin(4) + pd.Timedelta(days=15)
        ax.set_xlim(t, qe1)
        ax.axvline(qe1 - pd.Timedelta(days=2), color=MUTED, lw=0.8, ls=":")
        ax.text(qe1 - pd.Timedelta(days=3), ax.get_ylim()[0], "QE1次速報 ", fontsize=8.5,
                color=MUTED, ha="right", va="bottom")
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m月"))
        ax.legend(frameon=False, fontsize=8.5, loc="upper left", ncol=2,
                  title="縦線＝90%区間", title_fontsize=8)
    ax.axhline(0, color=GRAY, lw=0.8)
    ax.set_ylabel("%", fontsize=10)
    ax.tick_params(labelsize=10)
    ax.grid(axis="y", color=GRID); ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)

    fig.text(0.085, 0.032, "出所：内閣府ESRI（QE・景気動向指数・機械受注）、日本銀行（実質輸出入・消費活動指数・国際収支）、"
             "総務省 消費動向指数、経済産業省 鉱工業指数・製造工業生産予測指数、国土交通省 建設総合統計 を加工して作成。",
             fontsize=8.5, color=MUTED)
    fig.text(0.085, 0.01, "個人のモデル推計（公的機関の見通しではない）。区間はバックテスト誤差から算出。"
             "このサービスは、政府統計総合窓口(e-Stat)のAPI機能を使用していますが、サービスの内容は国によって保証されたものではありません。",
             fontsize=8.5, color=MUTED)
    if watermark:
        fig.text(0.5, 0.5, watermark, fontsize=60, color="#d0d0d0", alpha=0.6,
                 ha="center", va="center", rotation=20, weight="bold")
    fig.savefig(path, dpi=120, facecolor="white")
    plt.close(fig)
