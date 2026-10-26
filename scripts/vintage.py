"""Vintage store (SQLite): every value as it was published, plus a log of nowcasts.

Location: $JP_GDPNOW_HOME/vintages.sqlite (default ~/.jp-gdpnow). Keep this file
somewhere persistent (your own PC / a synced folder / a git repo) — cloud
sessions are temporary.

Tables
  obs(series, ref_date, vintage, value)   -- append-only, a row only when a value
                                            is new or revised (diff storage)
  runs(run_at, target, model, asof_data, saar, qoq, demand_saar, supply_saar,
       contributions, observed, qe_label)  -- one row per nowcast run
  news(run_at, target, model, basis, item, contrib)
                                         -- news decomposition of each logged run vs the
                                            previous one (basis = "前回 YYYY-MM-DD")

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
CREATE TABLE IF NOT EXISTS news (
  run_at TEXT NOT NULL, target TEXT NOT NULL, model TEXT, basis TEXT,
  item TEXT NOT NULL, contrib REAL
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


def _stamp(x=None) -> str:
    """Vintage key. New snapshots are time-stamped ("YYYY-MM-DDTHH:MM:SS", JST in Actions);
    older ones are plain dates. Keys compare as strings, so both kinds sort correctly."""
    if x is None:
        return datetime.now().isoformat(timespec="seconds")
    if isinstance(x, str) and len(x) == 10:
        return x
    if isinstance(x, date) and not isinstance(x, datetime):
        return x.isoformat()
    return pd.Timestamp(x).isoformat(timespec="seconds") if not isinstance(x, str) else x


def _asof(x) -> str:
    """'As of' bound: a bare date means the end of that day; a timestamp is used as is."""
    if isinstance(x, (date,)) and not isinstance(x, (datetime, pd.Timestamp)):
        return f"{x.isoformat()}T23:59:59"
    s = str(x)
    if len(s) == 10:
        return f"{s}T23:59:59"
    return pd.Timestamp(s).isoformat(timespec="seconds")


def _latest(con, series: str, asof: str) -> dict:
    q = """SELECT ref_date, value FROM obs o WHERE series=? AND vintage = (
             SELECT MAX(vintage) FROM obs i WHERE i.series=o.series
             AND i.ref_date=o.ref_date AND i.vintage<=?)"""
    return dict(con.execute(q, (series, asof)).fetchall())


def save_snapshot(df: pd.DataFrame, vintage: str | date | None = None, prefix: str = "",
                  con: sqlite3.Connection | None = None, rtol: float = 1e-9) -> int:
    """Store the columns of `df` (index = ref dates) as published on `vintage`
    (default: now, time-stamped). Only new or changed values are written. Returns rows added."""
    vintage = _stamp(vintage)
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
    """Wide DataFrame of the values as they stood on `asof` (a date = end of that day,
    or a timestamp such as a run's run_at)."""
    asof = _asof(asof)
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
            con: sqlite3.Connection | None = None, factor_saar: float | None = None) -> str:
    """Append one nowcast to `runs`; returns its run_at (the key for log_news)."""
    own = con is None
    con = con or connect()
    run_at = datetime.now().isoformat(timespec="seconds")
    con.execute("INSERT INTO runs (run_at, target, model, asof_data, saar, qoq, demand_saar, "
                "supply_saar, contributions, observed, qe_label, factor_saar) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
        run_at, str(nc.quarter.date()), model_name,
        asof_data, float(nc.saar), float(nc.qoq),
        float(getattr(nc, "demand_saar", np.nan)), float(getattr(nc, "supply_saar", np.nan)),
        json.dumps({k: round(float(v), 4) for k, v in nc.contributions.items()}, ensure_ascii=False),
        json.dumps(nc.observed_months), qe_label,
        None if factor_saar is None else float(factor_saar)))
    con.commit()
    if own:
        con.close()
    return run_at


def log_news(run_at: str, target, model_name: str, basis: str, news_df: pd.DataFrame | None,
             con: sqlite3.Connection | None = None):
    """Store a news decomposition (columns 統計, 寄与; the 合計 row is skipped) for one run."""
    own = con is None
    con = con or connect()
    con.execute("DELETE FROM news WHERE run_at=? AND model=?", (run_at, model_name))
    rows = []
    if news_df is not None:
        rows = [(run_at, str(pd.Timestamp(target).date()), model_name, basis, str(i), float(v))
                for i, v in zip(news_df["統計"], news_df["寄与"]) if i != "合計"]
    if not rows:  # keep a marker so the history shows why there is no breakdown
        rows = [(run_at, str(pd.Timestamp(target).date()), model_name, basis, "（内訳なし）", 0.0)]
    con.executemany("INSERT INTO news VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    if own:
        con.close()


def news_log(con: sqlite3.Connection | None = None) -> pd.DataFrame:
    own = con is None
    con = con or connect()
    df = pd.read_sql("SELECT * FROM news ORDER BY run_at", con)
    if own:
        con.close()
    return df


def arrivals(after, until, con: sqlite3.Connection | None = None) -> pd.DataFrame:
    """What arrived in the store with vintage in (after, until]: one row per series with
    the reference dates that are new (never seen before) and those that were revised."""
    after = _asof(after) if after is not None else "0000-00-00"
    until = _asof(until)
    own = con is None
    con = con or connect()
    q = """SELECT o.series, o.ref_date,
                  EXISTS (SELECT 1 FROM obs p WHERE p.series=o.series AND p.ref_date=o.ref_date
                          AND p.vintage<=?) AS seen,
                  EXISTS (SELECT 1 FROM obs p WHERE p.series=o.series AND p.vintage<=?) AS series_seen
           FROM obs o WHERE o.vintage>? AND o.vintage<=?
           GROUP BY o.series, o.ref_date"""
    df = pd.read_sql(q, con, params=(after, after, after, until))
    if own:
        con.close()
    if df.empty:
        return pd.DataFrame(columns=["series", "new", "revised", "new_series"])
    out = []
    for s, g in df.groupby("series"):
        out.append({"series": s,
                    "new": sorted(g.loc[g.seen == 0, "ref_date"]),
                    "revised": sorted(g.loc[g.seen == 1, "ref_date"]),
                    "new_series": not bool(g.series_seen.iloc[0])})
    return pd.DataFrame(out)


def last_run_before(target, before, con: sqlite3.Connection | None = None, model: str | None = None):
    """Most recent logged run for `target` strictly before `before` (date = start of that day,
    or a timestamp), optionally for one model."""
    r = runs(target, con=con)
    if model is not None and not r.empty:
        r = r[r["model"] == model]
    if r.empty:
        return None
    r = r[r["run_at"] < pd.Timestamp(before)]
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
