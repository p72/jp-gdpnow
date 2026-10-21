"""Vintage store (SQLite): every value as it was published, plus a log of nowcasts.

Location: $JP_GDPNOW_HOME/vintages.sqlite (default ~/.jp-gdpnow). Keep this file
somewhere persistent (your own PC / a synced folder / a git repo) — cloud
sessions are temporary.

Tables
  obs(series, ref_date, vintage, value)   -- append-only, a row only when a value
                                            is new or revised (diff storage)
  runs(run_at, target, model, asof_data, saar, qoq, demand_saar, supply_saar,
       contributions, observed, qe_label)  -- one row per nowcast run

Monthly indicators use series names as in data.get_monthly_raw();
QE levels are stored as "qe:<column>" with ref_date = quarter start.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime

import numpy as np
import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs (
  series   TEXT NOT NULL,
  ref_date TEXT NOT NULL,
  vintage  TEXT NOT NULL,
  value    REAL,
  PRIMARY KEY (series, ref_date, vintage)
);
CREATE INDEX IF NOT EXISTS obs_series_vintage ON obs(series, vintage);
CREATE TABLE IF NOT EXISTS runs (
  run_at TEXT NOT NULL, target TEXT NOT NULL, model TEXT, asof_data TEXT,
  saar REAL, qoq REAL, demand_saar REAL, supply_saar REAL,
  contributions TEXT, observed TEXT, qe_label TEXT, factor_saar REAL
);
"""


def home() -> str:
    d = os.environ.get("JP_GDPNOW_HOME", os.path.expanduser("~/.jp-gdpnow"))
    os.makedirs(d, exist_ok=True)
    return d


def connect(path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or os.path.join(home(), "vintages.sqlite"))
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(runs)")]
    if "factor_saar" not in cols:  # migrate DBs created before the factor engine
        con.execute("ALTER TABLE runs ADD COLUMN factor_saar REAL")
    return con


def _latest(con, series: str, asof: str) -> dict:
    q = """SELECT ref_date, value FROM obs o WHERE series=? AND vintage = (
             SELECT MAX(vintage) FROM obs i WHERE i.series=o.series
             AND i.ref_date=o.ref_date AND i.vintage<=?)"""
    return dict(con.execute(q, (series, asof)).fetchall())


def save_snapshot(df: pd.DataFrame, vintage: str | date | None = None, prefix: str = "",
                  con: sqlite3.Connection | None = None, rtol: float = 1e-9) -> int:
    """Store the columns of `df` (index = ref dates) as published on `vintage`
    (default today). Only new or changed values are written. Returns rows added."""
    vintage = str(pd.Timestamp(vintage or date.today()).date())
    own = con is None
    con = con or connect()
    added = 0
    for col in df.columns:
        s = df[col].dropna()
        name = prefix + col
        prev = _latest(con, name, vintage)
        rows = []
        for ref, v in s.items():
            key = str(pd.Timestamp(ref).date())
            old = prev.get(key)
            if old is None or not np.isclose(old, v, rtol=rtol, atol=0):
                rows.append((name, key, vintage, float(v)))
        con.executemany("INSERT OR REPLACE INTO obs VALUES (?,?,?,?)", rows)
        added += len(rows)
    con.commit()
    if own:
        con.close()
    return added


def load_asof(asof: str | date, series: list | None = None, prefix: str = "",
              con: sqlite3.Connection | None = None) -> pd.DataFrame:
    """Wide DataFrame of the values as they stood on `asof`."""
    asof = str(pd.Timestamp(asof).date())
    own = con is None
    con = con or connect()
    if series is None:
        series = [r[0] for r in con.execute(
            "SELECT DISTINCT series FROM obs WHERE series LIKE ?", (prefix + "%",))]
        if not prefix:
            series = [s for s in series if not s.startswith("qe:")]
    out = {}
    for name in series:
        d = _latest(con, name, asof)
        if d:
            out[name[len(prefix):]] = pd.Series(d, dtype=float)
    if own:
        con.close()
    df = pd.DataFrame(out)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def vintages(con: sqlite3.Connection | None = None) -> pd.DataFrame:
    """How many values were added/revised on each snapshot date, per series."""
    own = con is None
    con = con or connect()
    df = pd.read_sql("SELECT vintage, series, COUNT(*) AS n FROM obs GROUP BY vintage, series", con)
    if own:
        con.close()
    return df.pivot(index="vintage", columns="series", values="n").fillna(0).astype(int)


# ---------------------------------------------------------------- nowcast log
def log_run(nc, model_name: str, qe_label: str = "", asof_data: str | None = None,
            con: sqlite3.Connection | None = None, factor_saar: float | None = None):
    own = con is None
    con = con or connect()
    con.execute("INSERT INTO runs (run_at, target, model, asof_data, saar, qoq, demand_saar, "
                "supply_saar, contributions, observed, qe_label, factor_saar) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
        datetime.now().isoformat(timespec="seconds"), str(nc.quarter.date()), model_name,
        asof_data, float(nc.saar), float(nc.qoq),
        float(getattr(nc, "demand_saar", np.nan)), float(getattr(nc, "supply_saar", np.nan)),
        json.dumps({k: round(float(v), 4) for k, v in nc.contributions.items()}, ensure_ascii=False),
        json.dumps(nc.observed_months), qe_label,
        None if factor_saar is None else float(factor_saar)))
    con.commit()
    if own:
        con.close()


def last_run_before(target, before, con: sqlite3.Connection | None = None):
    """Most recent logged run for `target` on an earlier calendar day than `before`."""
    r = runs(target, con=con)
    if r.empty:
        return None
    r = r[r["run_at"].dt.normalize() < pd.Timestamp(before).normalize()]
    return None if r.empty else r.iloc[-1]


def runs(target: str | None = None, con: sqlite3.Connection | None = None) -> pd.DataFrame:
    own = con is None
    con = con or connect()
    q = "SELECT * FROM runs" + (" WHERE target=?" if target else "") + " ORDER BY run_at"
    df = pd.read_sql(q, con, params=(str(pd.Timestamp(target).date()),) if target else ())
    if own:
        con.close()
    df["run_at"] = pd.to_datetime(df["run_at"])
    return df
