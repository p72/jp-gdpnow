"""News decomposition: why did the nowcast change between two dates?

Starting from the old information set, the new data are swapped in one statistic
at a time (QE revision first, then in release order) and the engine is re-run;
each step's change in the SAAR nowcast is that statistic's "news". The steps add
up exactly to the total revision (order-dependent, like the Atlanta Fed's
sequential attribution).

Works with
  * real vintages: monthly_old / monthly_new from vintage.load_asof(...)
  * pseudo vintages: one data set cut at two dates (release.cut_monthly)
"""
from __future__ import annotations

import pandas as pd

GROUPS = [  # (label, series) in typical release order within a month
    ("実質輸出入（日銀）", ["real_exports", "real_imports"]),
    ("消費活動指数（日銀）", ["cai_real"]),
    ("総消費動向指数（総務省）", ["cti_real"]),
    ("生産予測指数（経産省）", ["plan_this_iip", "plan_next_iip", "plan_this_invest_goods",
                        "plan_next_invest_goods", "plan_this_durable_ship", "plan_next_durable_ship",
                        "plan_this_prod_goods_ship", "plan_next_prod_goods_ship"]),
    ("鉱工業指数（経産省）", ["iip", "invest_goods", "prod_goods_ship", "durable_ship"]),
    ("輸出数量指数（CI経由）", ["export_volume"]),
    ("住宅着工（CI経由）", ["housing_starts"]),
    ("雇用（CI経由）", ["labor_input", "new_job_offers", "jobs_ratio"]),
    ("マインド（CI経由）", ["consumer_conf", "sme_sales_di"]),
    ("国際収支（サービス）", ["services_credit", "services_debit"]),
    ("機械受注", ["machine_orders"]),
    ("建設総合統計", ["public_const"]),
    ("第3次産業活動指数（経産省）", ["tert_total", "tert_personal", "tert_business"]),
    ("鉱工業 財別出荷・在庫（経産省）", ["ship_capital", "ship_capital_ex", "ship_construction",
                                "ship_durable", "ship_nondurable", "stk_inv_total",
                                "stk_inv_final", "stk_inv_prod"]),
]


def decompose(engine, target, qe_old, monthly_old, qe_new, monthly_new) -> pd.DataFrame:
    """engine(qe, monthly, target) -> SAAR. Returns rows: 統計, 寄与(%pt) + 合計."""
    target = pd.Timestamp(target)
    cur_qe, cur_m = qe_old, monthly_old.copy()
    base = engine(cur_qe, cur_m, target)
    rows = []
    if not qe_new.reindex(qe_old.index).equals(qe_old) or len(qe_new) != len(qe_old):
        cur_qe = qe_new
        v = engine(cur_qe, cur_m, target)
        rows.append(("QE改定", v - base)); base = v
    known = {c for _, cs in GROUPS for c in cs}
    groups = GROUPS + [("その他", [c for c in monthly_new.columns if c not in known])]
    for label, cols in groups:
        cols = [c for c in cols if c in monthly_new.columns]
        if not cols:
            continue
        old = cur_m.reindex(columns=cols)
        new = monthly_new[cols]
        if old.reindex(new.index).equals(new):
            continue
        cur_m = cur_m.reindex(index=cur_m.index.union(new.index))
        cur_m[cols] = new.reindex(cur_m.index)
        v = engine(cur_qe, cur_m, target)
        if abs(v - base) > 1e-6:
            rows.append((label, v - base))
        base = v
    df = pd.DataFrame(rows, columns=["統計", "寄与"])
    total = df["寄与"].sum() if len(df) else 0.0
    return pd.concat([df, pd.DataFrame([("合計", total)], columns=["統計", "寄与"])], ignore_index=True)


def pseudo(engine, target, qe, monthly, asof_old, asof_new) -> pd.DataFrame:
    """News between two dates using one (latest) data set cut by the release calendar."""
    from .release import cut_monthly
    return decompose(engine, target, qe, cut_monthly(monthly, asof_old),
                     qe, cut_monthly(monthly, asof_new))
