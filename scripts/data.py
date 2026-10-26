"""Data loaders for the Japan GDPNow (簡易版) model.

Sources
- ESRI QE (実質季調実額 gaku-jk, 1次速報の前期比年率 nritu-jk)  → esri-data skill
- ESRI 景気動向指数 個別系列 (住宅着工床面積 L5, 投資財出荷 C5) → esri-data skill
- ESRI 機械受注 (コア)                                        → esri-data skill
- 日本銀行 実質輸出入 rs_reri.xlsx / 消費活動指数 cai.xlsx      → direct download

The esri-data / boj-api / estat-api skill modules are imported from vendor/<skill>/scripts
(bundled copies, used on GitHub Actions), else from an installed skill directory or
<SKILL>_SKILL_DIR. e-Stat needs the env var ESTAT_APP_ID.
"""
from __future__ import annotations

import glob
import io
import os
import sys
import time
import urllib.request
from functools import lru_cache

import numpy as np
import pandas as pd

BOJ_REAL_TRADE_URL = "https://www.boj.or.jp/research/research_data/reri/rs_reri.xlsx"
BOJ_CAI_URL = "https://www.boj.or.jp/research/research_data/cai/cai.xlsx"
BOJ_REAL_TRADE_PAGE = "https://www.boj.or.jp/research/research_data/reri/index.htm"
BOJ_CAI_PAGE = "https://www.boj.or.jp/research/research_data/cai/index.htm"


# ---------------------------------------------------------------- sibling skills
def _find_skill(name: str, module_file: str) -> str:
    env = os.environ.get(name.upper().replace("-", "_") + "_SKILL_DIR")
    cands = [env] if env else []
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands += [os.path.join(here, "vendor", name)]   # bundled copy (GitHub / cloud runs)
    cands += [os.path.join(os.path.dirname(here), name)]
    cands += sorted(glob.glob(f"/mnt/skills/*/{name}"))
    cands += sorted(glob.glob(os.path.expanduser(f"~/.claude/skills/{name}")))
    for c in cands:
        if c and os.path.exists(os.path.join(c, "scripts", module_file)):
            return c
    raise ImportError(f"{name} skill not found. Set {name.upper().replace('-', '_')}_SKILL_DIR.")


def _load_skill_module(name: str, module_file: str):
    """Import <skill>/scripts/<module_file> as a package member (relative imports work)."""
    import importlib.util

    pkg = "_skill_" + name.replace("-", "_")
    modname = f"{pkg}.{module_file[:-3]}"
    if modname in sys.modules:
        return sys.modules[modname]
    sdir = os.path.join(_find_skill(name, module_file), "scripts")
    if pkg not in sys.modules:
        import types

        p = types.ModuleType(pkg)
        p.__path__ = [sdir]  # namespace-like package so `from .jp_font import ...` resolves
        sys.modules[pkg] = p
    spec = importlib.util.spec_from_file_location(modname, os.path.join(sdir, module_file))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def esri():
    """esri_data module from the esri-data skill."""
    return _load_skill_module("esri-data", "esri_data.py")


def boj():
    """boj_api module from the boj-api skill."""
    return _load_skill_module("boj-api", "boj_api.py")


def estat():
    """estat_api module from the estat-api skill."""
    return _load_skill_module("estat-api", "estat_api.py")


# ---------------------------------------------------------------- BOJ files
@lru_cache(maxsize=None)
def _boj_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (jp-gdpnow)"})
    for i in range(2):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                time.sleep(1)
                return r.read()
        except Exception:
            if i == 1:
                raise
            time.sleep(10)


def _boj_monthly(url: str, cols: dict) -> pd.DataFrame:
    raw = pd.read_excel(io.BytesIO(_boj_get(url)), sheet_name="Data1", header=None)
    out = raw.iloc[5:, [0] + list(cols)].copy()
    out.columns = ["date"] + list(cols.values())
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"]).set_index("date").astype(float)
    return out.dropna(how="all")


def get_real_trade() -> pd.DataFrame:
    """日銀 実質輸出・実質輸入（月次・季調済, 2020=100）＋ 財別の実質輸出（資本財）"""
    df = _boj_monthly(BOJ_REAL_TRADE_URL, {1: "real_exports", 2: "real_imports"})
    raw = pd.read_excel(io.BytesIO(_boj_get(BOJ_REAL_TRADE_URL)), sheet_name="Data2", header=None)
    col = [j for j in range(raw.shape[1]) if str(raw.iloc[2, j]) == "資本財"]
    if col:
        s = raw.iloc[7:, [0, col[0]]].copy()
        s.columns = ["date", "real_ex_capital"]
        s["date"] = pd.to_datetime(s["date"], errors="coerce")
        s = s.dropna(subset=["date"]).set_index("date").astype(float)
        df = pd.concat([df, s], axis=1)
    return df


def get_cai() -> pd.DataFrame:
    """日銀 実質消費活動指数（旅行収支調整済, 月次・季調済）"""
    return _boj_monthly(BOJ_CAI_URL, {4: "cai_real"})


# ---------------------------------------------------------------- ESRI monthly
def get_esri_monthly() -> pd.DataFrame:
    e = esri()
    lead = e.get_ci_component_df(1, labels="code").set_index("date")
    coin = e.get_ci_component_df(2, labels="code").set_index("date")
    jc = e.get_juchu_df().set_index("date")
    df = pd.DataFrame(
        {
            "housing_starts": lead["L5"],
            "invest_goods": coin["C5"],
            "machine_orders": jc["core"],
            # extra CI component series (used by the factor engine)
            "new_job_offers": lead["L3"],        # 新規求人数（除学卒）
            "consumer_conf": lead["L6"],         # 消費者態度指数
            "sme_sales_di": lead["L11"],         # 中小企業売上げ見通しDI
            "prod_goods_ship": coin["C2"],       # 鉱工業用生産財出荷指数
            "durable_ship": coin["C3"],          # 耐久消費財出荷指数
            "jobs_ratio": coin["C9"],            # 有効求人倍率（除学卒）
            "export_volume": coin["C10"],        # 輸出数量指数
            "inv_ratio_final": lead["L1"],       # 最終需要財在庫率指数（逆サイクル表示の元値）
            "inv_ratio_prod": lead["L2"],        # 鉱工業用生産財在庫率指数
        }
    )
    df.index = pd.to_datetime(df.index)
    return df.astype(float)


BOJ_SERVICES = {"BPBP6JYASCX12": "services_credit", "BPBP6JYASDX12": "services_debit"}
ESTAT_PUBLIC_CONST = "0003458439"  # 建設総合統計 月別《全国》出来高


BOJ_BOP_PRICES = {
    "BPBP6JYAEXX12": "bop_goods_ex",    # 国際収支 季調 貿易/輸出（億円）
    "BPBP6JYAIMX12": "bop_goods_im",    # 国際収支 季調 貿易/輸入
    "PRCG20_2400000000": "px_export",   # 輸出物価指数（円ベース）総平均 2020=100
    "PRCG20_2600000000": "px_import",   # 輸入物価指数（円ベース）総平均
    "PRCS20_4200000000": "px_services", # 企業向けサービス価格指数（除く消費税）総平均
}


def get_bop_real() -> pd.DataFrame:
    """QE-style real exports/imports: BOP goods & services (nominal, SA) deflated by
    export/import price indices (goods) and SPPI (services) — boj-api skill.
    Returns raw inputs + bop_real_ex / bop_real_im (億円, 2020 prices)."""
    df = boj().get_series("BP01", ["BPBP6JYAEXX12", "BPBP6JYAIMX12"], start_date="200501")
    pr = boj().get_series("PR01", ["PRCG20_2400000000", "PRCG20_2600000000"], start_date="200501")
    sp = boj().get_series("PR02", ["PRCS20_4200000000"], start_date="200501")
    out = pd.concat([df, pr, sp], axis=1).rename(columns=BOJ_BOP_PRICES)
    out.index = pd.to_datetime(out.index)
    return out.astype(float)


def get_services_trade() -> pd.DataFrame:
    """国際収支 サービス受取・支払（季調済, 億円, 名目）— boj-api skill."""
    df = boj().get_series("BP01", list(BOJ_SERVICES), start_date="200501")
    df = df.rename(columns=BOJ_SERVICES)
    df.index = pd.to_datetime(df.index)
    return df.astype(float)


ESTAT_CTI_WORD = "総消費動向指数"  # 総務省 消費動向指数（CTI）総消費動向指数（GDP家計消費相当）


def get_cti() -> pd.DataFrame:
    """総消費動向指数（CTIマクロ）実質・季調, 2020=100 — latest file found via e-Stat catalog."""
    e = estat()
    rows = []
    for w in (ESTAT_CTI_WORD, "総消費動向指数 2025年基準", "総消費動向指数 2030年基準"):
        try:
            rows += [r for r in e.catalog_resources(None, w, limit=50)
                     if "最新結果" in r["dataset"] and r["format"] == "XLS" and "総消費動向指数" in r["name"]]
        except Exception:
            pass
    rows.sort(key=lambda r: r["last_modified"], reverse=True)  # newest base / release first
    if not rows:
        raise RuntimeError("CTI file not found in e-Stat catalog")
    raw = pd.read_excel(io.BytesIO(e.download_file(rows[0]["url"])), sheet_name="月", header=None)
    body = raw[raw[1].astype(str).str.fullmatch(r"\d{10}")]
    idx = pd.to_datetime(body[1].astype(str).str[:4] + "-" + body[1].astype(str).str[-2:] + "-01")
    return pd.DataFrame({"cti_real": pd.to_numeric(body[4], errors="coerce").values},
                        index=pd.DatetimeIndex(idx.values))


# 製造工業生産予測指数（経産省, e-Stat 時系列ファイル）: sheet -> target series in the model
PLAN_SHEETS = {"製造工業": "iip", "資本財（除．輸送機械）": "invest_goods",
               "耐久消費財": "durable_ship", "鉱工業用生産財": "prod_goods_ship"}


def get_production_plans() -> pd.DataFrame:
    """Survey plans, %m/m (seasonally adjusted).
    plan_this_<s> at month k = survey k's 当月見込み 前月比 (public at end of month k)
    plan_next_<s> at month k = survey k-1's 翌月見込み 前月比 (public at end of month k-1)."""
    e = estat()
    rows = [r for r in e.catalog_resources(None, "製造工業生産予測", limit=50)
            if "時系列データ" in r["dataset"] and r["format"] == "XLS"]
    if not rows:
        raise RuntimeError("production forecast time-series file not found in e-Stat catalog")
    xl = pd.ExcelFile(io.BytesIO(e.download_file(rows[0]["url"])))
    out = {}
    for sh, name in PLAN_SHEETS.items():
        raw = pd.read_excel(xl, sheet_name=sh, header=None)
        body = raw[raw[0].astype(str).str.fullmatch(r"\d{10}")]
        k = pd.to_datetime(body[0].astype(str).str[:4] + "-" + body[0].astype(str).str[-2:] + "-01")
        this = pd.to_numeric(body[6], errors="coerce").values
        nxt = pd.to_numeric(body[7], errors="coerce").values
        out[f"plan_this_{name}"] = pd.Series(this, index=pd.DatetimeIndex(k.values))
        out[f"plan_next_{name}"] = pd.Series(nxt, index=pd.DatetimeIndex(k.values) + pd.offsets.MonthBegin(1))
    return pd.DataFrame(out).sort_index()


def seasonal_adjust(s: pd.Series) -> pd.Series:
    """STL (robust, period 12) on logs; returns the seasonally adjusted level."""
    from statsmodels.tsa.seasonal import STL

    s = s.dropna()
    res = STL(np.log(s), period=12, robust=True).fit()
    return np.exp(res.trend + res.resid)


CONST_SERIES = {  # name -> (cdCat01 種類, cdCat02 民間・公共)
    "public_const_raw": ("100", "120"),        # 計 × 公共
    "priv_res_const_raw": ("120", "110"),      # 建築_居住用 × 民間（＝QEの民間住宅に近い）
    "priv_nonres_const_raw": ("200", "110"),   # 民間非住宅建築＋民間土木（設備投資の建設部分）
}


def get_public_construction() -> pd.DataFrame:
    """建設総合統計 出来高（全国, 億円, 名目, 原系列）— estat-api skill.
    Raw (not seasonally adjusted) so that snapshots keep the source values;
    `transform_monthly` applies the STL adjustment."""
    out = {}
    for name, (c1, c2) in CONST_SERIES.items():
        d = estat().get_stats_df(ESTAT_PUBLIC_CONST, cdTab="100", cdCat01=c1, cdCat02=c2, monthly=True)
        out[name] = pd.Series(d["value"].astype(float).values,
                              index=d.index.to_timestamp() if hasattr(d.index, "to_timestamp")
                              else pd.to_datetime(d.index))
    return pd.DataFrame(out)


# 第3次産業活動指数（経産省）. METI's server returns 403 to non-browser clients,
# so requests carry a browser User-Agent.
METI_TERT_BASE = "https://www.meti.go.jp/statistics/tyo/sanzi/result/excel/"
METI_TERT_LATEST = "b2020_ksmj.xlsx"      # 2020年基準 業種別季調済 (2018-)
METI_TERT_LINK = "b2020_ITA_linkj.xlsx"   # 接続指数 季調済 (1988-2024)
METI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TERT_SERIES = {"K1D000000I": "tert_total",      # 第3次産業総合
               "SBB0000I": "tert_personal",     # 広義対個人サービス
               "SBC0000I": "tert_business"}     # 広義対事業所サービス


@lru_cache(maxsize=None)
def _meti_get(name: str, base: str = METI_TERT_BASE) -> bytes:
    """METI file download; the CDN sometimes answers 202 with an empty body -> retry."""
    req = urllib.request.Request(base + name,
                                 headers={"User-Agent": METI_UA, "Accept-Language": "ja"})
    for i in range(5):
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            if r.status == 200 and body:
                return body
        time.sleep(3 + 3 * i)
    raise RuntimeError(f"METI download failed: {name}")


# 鉱工業指数 財別（経産省, 季調済, 2020=100）: 出荷 and 在庫 by goods class
METI_IIP_BASE = "https://www.meti.go.jp/statistics/tyo/iip/xls/"
METI_IIP_LATEST = "b2020_zsm1j.xlsx"   # 2018- (sheets 生産/出荷/在庫/在庫率)
METI_IIP_LINK = "b2020_szs1j.xlsx"     # 接続指数 1978-2022 (sheets 生産付加価値額/出荷/在庫/在庫率)
IIP_GOODS = {  # (sheet, code) -> series
    ("出荷", "3100010100"): "ship_capital",        # 資本財（輸送機械を含む）
    ("出荷", "3100010200"): "ship_capital_ex",     # 資本財（除．輸送機械）
    ("出荷", "3100010300"): "ship_construction",   # 建設財
    ("出荷", "3100020100"): "ship_durable",        # 耐久消費財
    ("出荷", "3100020200"): "ship_nondurable",     # 非耐久消費財
    ("在庫", "3000000000"): "stk_inv_total",       # 在庫 鉱工業（期末, stock）
    ("在庫", "3100000000"): "stk_inv_final",       # 在庫 最終需要財
    ("在庫", "3200000000"): "stk_inv_prod",        # 在庫 生産財
}


def get_iip_goods() -> pd.DataFrame:
    """経産省 鉱工業指数 財別の出荷・在庫（季調済）. 2018- latest file, earlier from the linked index.
    stk_* are end-of-month stocks (the model uses their quarter-end level, see model.quarterly_growth)."""
    new_x = pd.ExcelFile(io.BytesIO(_meti_get(METI_IIP_LATEST, METI_IIP_BASE)))
    old_x = pd.ExcelFile(io.BytesIO(_meti_get(METI_IIP_LINK, METI_IIP_BASE)))
    out = {}
    for (sheet, code), name in IIP_GOODS.items():
        a = pd.read_excel(new_x, sheet, header=None)
        row = a[a[0].astype(str) == code]
        months = a.iloc[2, 3:].astype(int).astype(str)
        latest = pd.Series(row.iloc[0, 3:].values, index=pd.to_datetime(months.values, format="%Y%m"))
        b = pd.read_excel(old_x, sheet, header=None)
        j = [k for k in range(2, b.shape[1]) if str(b.iloc[1, k]) == code][0]
        body = b.iloc[4:, [1, j]]
        body = body[body[1].astype(str).str.fullmatch(r"\d{6}")]
        linked = pd.Series(body[j].values, index=pd.to_datetime(body[1].astype(str), format="%Y%m"))
        s = pd.concat([linked[linked.index < latest.index.min()], latest])
        out[name] = pd.to_numeric(s, errors="coerce")
    return pd.DataFrame(out).astype(float)


def get_tertiary() -> pd.DataFrame:
    """第3次産業活動指数（季調済, 2020年基準）: 総合・広義対個人・広義対事業所.
    1988-2017 from the linked index, 2018- from the latest file (identical on overlap)."""
    new = pd.read_excel(io.BytesIO(_meti_get(METI_TERT_LATEST)), sheet_name="ITA", header=None)
    months = new.iloc[2, 3:].astype(int).astype(str)
    body = new[new[0].isin(TERT_SERIES)].set_index(0).iloc[:, 2:]
    body.columns = pd.to_datetime(months.values, format="%Y%m")
    latest = body.T.rename(columns=TERT_SERIES)

    old = pd.read_excel(io.BytesIO(_meti_get(METI_TERT_LINK)), sheet_name="季調済指数", header=None)
    cols = {j: TERT_SERIES[old.iloc[1, j]] for j in range(1, old.shape[1])
            if old.iloc[1, j] in TERT_SERIES}
    ob = old.iloc[6:][[0] + list(cols)]
    ob = ob[ob[0].astype(str).str.fullmatch(r"\d{6}")]
    linked = ob.set_index(pd.to_datetime(ob[0].astype(str), format="%Y%m"))[list(cols)].rename(columns=cols)
    out = pd.concat([linked[linked.index < latest.index.min()], latest])
    return out.apply(pd.to_numeric, errors="coerce").astype(float)


def get_supply_monthly() -> pd.DataFrame:
    """供給側: 鉱工業生産指数 (CI C1), 労働投入量指数 (CI C4)."""
    coin = esri().get_ci_component_df(2, labels="code").set_index("date")
    df = pd.DataFrame({"iip": coin["C1"], "labor_input": coin["C4"]})
    df.index = pd.to_datetime(df.index)
    return df.astype(float)


# 鉱工業指数（経産省, 財別 季調済）で、景気動向指数（CI）経由の同じ系列を先取りする。
# CIの個別系列は経産省の値をそのまま転記したもの（2018年以降の水準は一致）だが、
# CI速報・改訂の公表を待つため反映が約1〜2週間遅れる（例: 8月分確報は経産省10/15, CI改訂10/26）。
IIP_METI = {  # model series -> (sheet, code)
    "iip": ("生産", "3000000000"),               # 鉱工業生産（CI C1）
    "prod_goods_ship": ("出荷", "3200000100"),   # 鉱工業用生産財出荷（CI C2）
    "durable_ship": ("出荷", "3100020100"),      # 耐久消費財出荷（CI C3）
}
# 投資財出荷（除．輸送機械）（CI C5）= 資本財（除．輸送機械）と建設財の出荷をウェイトで合成
IIP_INVEST_PARTS = ("3100010200", "3100010300")
IIP_RECENT_MONTHS = 3   # invest_goods: METI-based values replace CI only for the latest months


def get_iip_meti() -> pd.DataFrame:
    """経産省 鉱工業指数（2018-, 季調済）から iip / prod_goods_ship / durable_ship / invest_goods."""
    x = pd.ExcelFile(io.BytesIO(_meti_get(METI_IIP_LATEST, METI_IIP_BASE)))
    sheets = {s: pd.read_excel(x, s, header=None) for s in ("生産", "出荷")}

    def row(sheet, code):
        a = sheets[sheet]
        r = a[a[0].astype(str) == code]
        months = a.iloc[2, 3:].astype(int).astype(str)
        return pd.to_numeric(pd.Series(r.iloc[0, 3:].values,
                                       index=pd.to_datetime(months.values, format="%Y%m")),
                             errors="coerce"), float(r.iloc[0, 2])

    out = {k: row(*v)[0] for k, v in IIP_METI.items()}
    (a, wa), (b, wb) = (row("出荷", c) for c in IIP_INVEST_PARTS)
    out["invest_goods"] = ((wa * a + wb * b) / (wa + wb)).round(1)
    return pd.DataFrame(out).astype(float)


def _merge_iip_meti(df: pd.DataFrame, meti: pd.DataFrame) -> pd.DataFrame:
    """METI values take priority (they are published first); CI history is kept elsewhere.
    invest_goods is a reconstruction (±0.1 rounding vs CI), so it only overrides recent months."""
    df = df.copy()
    for c in meti.columns:
        m = meti[c].dropna()
        if c not in df:
            df[c] = m
            continue
        if c == "invest_goods":
            last_ci = df[c].last_valid_index()
            start = (last_ci - pd.offsets.MonthBegin(IIP_RECENT_MONTHS - 1)) if last_ci is not None else m.index.min()
            m = m[m.index >= start]
        df = df.reindex(df.index.union(m.index))
        df.loc[m.index, c] = m.values
    return df


def get_monthly_raw(extended: bool = True) -> pd.DataFrame:
    """Source values as published (what the vintage store saves)."""
    parts = [get_cai(), get_real_trade(), get_esri_monthly()]
    if extended:
        for fn in (get_services_trade, get_public_construction, get_supply_monthly, get_cti,
                   get_production_plans, get_bop_real, get_tertiary, get_iip_goods):
            try:
                parts.append(fn())
            except Exception as ex:  # keep the core model running if one source fails
                print(f"[jp-gdpnow] warning: {fn.__name__} failed: {ex}", file=sys.stderr)
    df = pd.concat(parts, axis=1).sort_index()
    try:
        df = _merge_iip_meti(df, get_iip_meti())
    except Exception as ex:  # fall back to the CI-based values
        print(f"[jp-gdpnow] warning: get_iip_meti failed: {ex}", file=sys.stderr)
    return df.sort_index()


def transform_monthly(raw: pd.DataFrame) -> pd.DataFrame:
    """Model inputs from raw values (STL-adjust public construction; build BOP real trade)."""
    df = raw.copy()
    for c in [c for c in df.columns if c.endswith("_const_raw")]:
        df[c[:-4]] = seasonal_adjust(df.pop(c))
    need = {"bop_goods_ex", "bop_goods_im", "px_export", "px_import", "px_services",
            "services_credit", "services_debit"}
    if need <= set(df.columns):
        df["bop_real_ex"] = (df["bop_goods_ex"] / df["px_export"] * 100
                             + df["services_credit"] / df["px_services"] * 100)
        df["bop_real_im"] = (df["bop_goods_im"] / df["px_import"] * 100
                             + df["services_debit"] / df["px_services"] * 100)
    return df


def get_monthly(extended: bool = True, save: bool = False) -> pd.DataFrame:
    """All monthly indicators in one frame (index = month start).
    save=True also stores today's snapshot in the vintage DB (scripts/vintage.py)."""
    raw = get_monthly_raw(extended)
    if save:
        from . import vintage
        vintage.save_snapshot(raw)
    return transform_monthly(raw)


# ---------------------------------------------------------------- QE
QE_COLS = [
    "GDP", "PrivateConsumption", "PrivateResidential", "PrivateCapex",
    "PrivateInventories", "GovConsumption", "PublicInvestment",
    "PublicInventories", "Exports", "Imports",
]


def get_qe_levels(year=None, quarter=None, revision=None) -> pd.DataFrame:
    """実質季調実額（年率, 10億円）. Index = quarter start date."""
    e = esri()
    df = e.get_qe_df("gaku-jk", year=year, quarter=quarter, revision=revision)
    df = df.set_index(pd.to_datetime(df["date"]))[QE_COLS].astype(float)
    df.attrs["label"] = getattr(df, "attrs", {}).get("label", "")
    return df


def get_qe1_growth(year: int, quarter: int) -> float:
    """1次速報で公表された当該期の実質GDP 前期比年率（%）."""
    e = esri()
    raw = e.get_qe_raw(year, quarter, "nritu-jk", 1)
    s = e.parse_qe_column(raw, "GDP")
    row = s[(s["year"] == year) & (s["quarter"] == quarter)]
    return float(row["value"].iloc[0])


def latest_qe_info() -> dict:
    return esri().get_latest_qe_info()
