#!/usr/bin/env python3
"""
日本銀行 時系列統計データ検索サイト API wrapper (no API key)
BOJ Time-Series Data Search API: https://www.stat-search.boj.or.jp/

Import everything from here — do not copy helpers into ad-hoc scripts.

    import sys; sys.path.insert(0, "<skill base dir>")
    from scripts.boj_api import (
        BojApiError, boj_request, polite_request,
        get_metadata, find_series, get_data_by_code, get_data_by_layer,
        get_csv, to_dataframe, get_series,
    )

Behaviour baked in (learned from use):
- Errors raise BojApiError (e.status = HTTP/API status, e.messageid = M1810xxE,
  e.transient = True for 429/5xx/network). HTTP 4xx is never retried.
- Error bodies are gzip-compressed even when you did not ask for gzip
  (Content-Encoding: gzip on 400s) -> the module always sniffs & decompresses.
- polite_request() sleeps after every request (default 1.5 s) and retries once
  on transient errors (after 10 s).
- Code API: codes are auto-chunked to 250 per request; NEXTPOSITION pagination
  (250 series / 60,000 data points per response) is followed automatically.
- Codes with a DB prefix ("IR01'MADR1Z@D") are stripped to "MADR1Z@D" (else M181001E).
- Forbidden chars (< > " ! | \\ ; ') and full-width chars are rejected locally.
- FREQUENCY accepts short codes only (CY FY CH FH Q M W D); formal names
  (QUARTERLY, MONTHLY, DAILY ...) are mapped to the short code automatically.
- Dates: YYYY / YYYYMM / YYYYQQ / YYYYHH. Daily & weekly series take YYYYMM,
  so "20260901" / "2026-09-01" are trimmed to "202609".
- A code must belong to the DB you pass (FXERD01 is FM08, STRDCLUCON is FM01),
  and one code request cannot mix frequencies (M181014E): split by DB/frequency.
- Layer API: > 1,250 matching series (counted before the frequency filter)
  -> M181007E. Narrow the LAYER.
- Metadata RESULTSET contains heading rows with an empty SERIES_CODE; they are
  dropped by default (keep_headings=True keeps them). CO (短観) metadata is huge
  (~168k rows, slow) — cached per process.
- Daily series include weekends/holidays as null -> NaN (dropna() for plots).
"""

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

BASE_URL = "https://www.stat-search.boj.or.jp/api/v1"
ENDPOINTS = ("getDataCode", "getDataLayer", "getMetadata")
MAX_CODES_PER_REQUEST = 250
MAX_LAYER_SERIES = 1250
MAX_DATA_POINTS = 60000
FREQUENCIES = ("CY", "FY", "CH", "FH", "Q", "M", "W", "D")
FREQ_ALIASES = {
    "ANNUAL": "CY", "ANNUAL(MAR)": "FY", "SEMIANNUAL": "CH",
    "SEMIANNUAL(SEP)": "FH", "QUARTERLY": "Q", "MONTHLY": "M",
    "WEEKLY": "W", "DAILY": "D",
}
FORBIDDEN_CHARS = set('<>"!|\\;\'')
DEFAULT_TIMEOUT = 120
DEFAULT_SLEEP = 1.5
RETRY_WAIT = 10
USER_AGENT = "boj-api-skill/2.0 (research use; python-urllib)"

try:
    from .jp_font import setup_japanese_font  # noqa: F401  グラフ用日本語フォント
except ImportError:  # scripts/ を直接 sys.path に入れた場合
    from jp_font import setup_japanese_font  # noqa: F401

__all__ = [
    "setup_japanese_font",
    "BojApiError", "BASE_URL", "FREQUENCIES", "MAX_CODES_PER_REQUEST",
    "boj_request", "polite_request", "get_metadata", "find_series",
    "get_data_by_code", "get_data_by_layer", "get_csv",
    "to_dataframe", "get_series", "normalize_frequency", "normalize_date",
]


class BojApiError(Exception):
    """API error or failed request.

    Attributes:
        status: HTTP / API STATUS (400, 500, 503 ...) or None for network errors.
        messageid: BOJ MESSAGEID (e.g. "M181013E") when available.
        transient: True if a retry may succeed (429 / 5xx / network).
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 messageid: Optional[str] = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.messageid = messageid
        self.transient = transient


# ---------------------------------------------------------------------------
# Validation / normalisation
# ---------------------------------------------------------------------------

def normalize_frequency(freq: str) -> str:
    """'QUARTERLY' / 'q' / 'WEEKLY(MONDAY)' -> 'Q' / 'Q' / 'W'. Raises on unknown."""
    f = str(freq).strip().upper()
    if f.startswith("WEEKLY") or re.fullmatch(r"W[0-6]", f):
        return "W"
    f = FREQ_ALIASES.get(f, f)
    if f not in FREQUENCIES:
        raise BojApiError(f"Invalid frequency '{freq}'. Use one of {FREQUENCIES}.",
                          status=400, messageid="M181018E")
    return f


def normalize_date(d: Optional[Union[str, int]]) -> Optional[str]:
    """Normalise a start/end period.

    Accepts YYYY, YYYYMM, YYYYQQ, YYYYHH; "2024-01" -> "202401";
    "20240115" / "2024-01-15" -> "202401" (daily/weekly series take YYYYMM).
    """
    if d is None or d == "":
        return None
    s = re.sub(r"[-/.\s]", "", str(d))
    if not s.isdigit() or len(s) not in (4, 6, 8):
        raise BojApiError(f"Invalid date '{d}'. Use YYYY, YYYYMM, YYYYQQ or YYYYHH.",
                          status=400, messageid="M181008E")
    if len(s) == 8:
        s = s[:6]
    if not 1850 <= int(s[:4]) <= 2050:
        raise BojApiError(f"Year out of range 1850-2050: '{d}'", status=400,
                          messageid="M181010E")
    return s


def _clean_code(code: str, db: str) -> str:
    c = str(code).strip()
    if "'" in c:  # "IR01'MADR1Z@D" -> "MADR1Z@D"
        c = c.split("'", 1)[1]
    return c


def _check_chars(params: Dict[str, str]) -> None:
    for k, v in params.items():
        bad = [ch for ch in v if ch in FORBIDDEN_CHARS or ord(ch) > 0x7E]
        if bad:
            raise BojApiError(
                f"Parameter {k}='{v}' contains forbidden/full-width characters {bad!r}",
                status=400, messageid="M181001E")


# ---------------------------------------------------------------------------
# Core request
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":  # BOJ sends gzip bodies (esp. errors) regardless
        raw = gzip.decompress(raw)
    return raw


_HINTS = {
    "M181015E": " (hint: date format must match the series frequency: CY/FY=YYYY, "
                "M/D/W=YYYYMM, Q=YYYYQQ, CH/FH=YYYYHH)",
    "M181016E": " (hint: see M181015E)",
    "M181013E": " (hint: code must exist in THIS db; check find_series(db, ...))",
    "M181014E": " (hint: codes with different frequencies -> separate requests)",
    "M181007E": " (hint: > 1,250 series match this LAYER; add deeper layer levels)",
}


def _error_from_body(body: bytes, http_status: Optional[int]) -> BojApiError:
    try:
        j = json.loads(body.decode("utf-8"))
        st = int(j.get("STATUS", http_status or 0))
        mid = j.get("MESSAGEID")
        hint = _HINTS.get(mid, "")
        return BojApiError(f"BOJ API error {st} {mid}: {j.get('MESSAGE')}{hint}",
                           status=st, messageid=j.get("MESSAGEID"),
                           transient=(st == 429 or st >= 500))
    except Exception:
        txt = body[:300].decode("utf-8", errors="replace")
        st = http_status
        return BojApiError(f"HTTP {st}: {txt}", status=st,
                           transient=bool(st and (st == 429 or st >= 500)))


def boj_request(endpoint: str, params: Dict[str, Any], fmt: str = "json",
                timeout: float = DEFAULT_TIMEOUT) -> Union[Dict[str, Any], str]:
    """Send ONE request (no sleep, no retry).

    endpoint: "getDataCode" / "getDataLayer" / "getMetadata"
    params: API params (case-insensitive names), e.g. {"db": "FM08", "code": "FXERD01"}
    fmt: "json" -> dict; "csv" -> decoded str (JP=Shift-JIS, EN=UTF-8)
    Raises BojApiError (also when JSON STATUS != 200).
    """
    if endpoint not in ENDPOINTS:
        raise BojApiError(f"Unknown endpoint {endpoint}; use one of {ENDPOINTS}")
    p = {k.lower(): str(v) for k, v in params.items() if v is not None and v != ""}
    p["format"] = fmt.lower()
    _check_chars(p)
    url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(p, safe=',*@')}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = _decode(resp.read())
    except urllib.error.HTTPError as e:
        raise _error_from_body(_decode(e.read() or b""), e.code) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise BojApiError(f"Network error: {getattr(e, 'reason', e)}",
                          transient=True) from None

    if fmt.lower() == "csv":
        if body.lstrip().startswith(b"{"):  # errors are JSON even for CSV
            raise _error_from_body(body, None)
        lang = p.get("lang", "jp").lower()
        return body.decode("utf-8" if lang == "en" else "cp932", errors="replace")
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise BojApiError(f"Failed to parse JSON: {e}") from None
    if int(data.get("STATUS", 200)) != 200:
        raise _error_from_body(body, None)
    return data


def polite_request(endpoint: str, params: Dict[str, Any], fmt: str = "json",
                   sleep: float = DEFAULT_SLEEP, retries: int = 1,
                   retry_wait: float = RETRY_WAIT, **kw):
    """boj_request() + sleep after each request + retry once on transient errors."""
    for attempt in range(retries + 1):
        try:
            r = boj_request(endpoint, params, fmt=fmt, **kw)
            time.sleep(sleep)
            return r
        except BojApiError as e:
            if not e.transient or attempt == retries:
                raise
            time.sleep(retry_wait)
    raise BojApiError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

_META_CACHE: Dict[tuple, dict] = {}


def get_metadata(db: str, lang: str = "jp", format: str = "json",
                 keep_headings: bool = False, use_cache: bool = True) -> dict:
    """Metadata API: all series of a DB with LAYER1-5, start/end, notes.

    Returns the full response dict; RESULTSET heading rows (empty SERIES_CODE)
    are dropped unless keep_headings=True. Cached per process.
    format="csv" returns the raw CSV text instead.
    """
    if format.lower() == "csv":
        return polite_request("getMetadata", {"db": db, "lang": lang}, fmt="csv")
    key = (db.upper(), lang.lower())
    if use_cache and key in _META_CACHE:
        data = _META_CACHE[key]
    else:
        data = polite_request("getMetadata", {"db": db, "lang": lang})
        _META_CACHE[key] = data
    if keep_headings:
        return data
    out = dict(data)
    out["RESULTSET"] = [r for r in data.get("RESULTSET", []) if r.get("SERIES_CODE")]
    return out


def find_series(db_or_meta: Union[str, dict], *keywords: str,
                frequency: Optional[str] = None, lang: str = "jp",
                limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    """Search series in a DB's metadata by name keywords (AND, case-insensitive).

    find_series("FM08", "ドル", "スポット")  /  find_series("MD02", "M2", frequency="M")
    Returns metadata rows (SERIES_CODE, NAME_OF_TIME_SERIES_J, FREQUENCY, ...).
    """
    meta = get_metadata(db_or_meta, lang=lang) if isinstance(db_or_meta, str) else db_or_meta
    f = normalize_frequency(frequency) if frequency else None
    rows = []
    for r in meta.get("RESULTSET", []):
        if not r.get("SERIES_CODE"):
            continue
        name = (r.get("NAME_OF_TIME_SERIES_J") or r.get("NAME_OF_TIME_SERIES") or "")
        if not all(k.lower() in name.lower() for k in keywords):
            continue
        if f and normalize_frequency(r.get("FREQUENCY", "")) != f:
            continue
        rows.append(r)
        if limit and len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _paginate(endpoint: str, params: Dict[str, Any], sleep: float) -> List[dict]:
    results: List[dict] = []
    pos = None
    while True:
        p = dict(params)
        if pos is not None:
            p["startPosition"] = pos
        data = polite_request(endpoint, p, sleep=sleep)
        results.extend(data.get("RESULTSET", []) or [])
        nxt = data.get("NEXTPOSITION")
        if nxt in (None, "", "null"):
            break
        if pos is not None and int(nxt) <= int(pos):
            raise BojApiError(f"NEXTPOSITION did not advance ({pos} -> {nxt})")
        pos = int(nxt)
    return results


def _merge_pages(results: List[dict]) -> List[dict]:
    """A series split across pages (60,000-point cap) is merged back into one."""
    merged: Dict[str, dict] = {}
    order: List[str] = []
    for s in results:
        code = s.get("SERIES_CODE")
        if code not in merged:
            merged[code] = s
            order.append(code)
            continue
        a = merged[code].setdefault("VALUES", {"SURVEY_DATES": [], "VALUES": []})
        b = s.get("VALUES") or {}
        a["SURVEY_DATES"] = list(a.get("SURVEY_DATES", [])) + list(b.get("SURVEY_DATES", []))
        a["VALUES"] = list(a.get("VALUES", [])) + list(b.get("VALUES", []))
    return [merged[c] for c in order]


def get_data_by_code(db: str, codes: Union[str, Sequence[str]],
                     start_date: Optional[Union[str, int]] = None,
                     end_date: Optional[Union[str, int]] = None,
                     lang: str = "jp", sleep: float = DEFAULT_SLEEP) -> List[dict]:
    """Code API. Returns the list of RESULTSET series dicts.

    codes: list or comma-separated str; auto-chunked by 250; NEXTPOSITION followed.
    All codes must be in `db` and share one frequency (else M181013E / M181014E).
    """
    if isinstance(codes, str):
        codes = [c for c in codes.split(",") if c.strip()]
    codes = [_clean_code(c, db) for c in codes]
    if not codes:
        raise BojApiError("No series codes given", status=400, messageid="M181006E")
    base = {"db": db, "lang": lang, "startDate": normalize_date(start_date),
            "endDate": normalize_date(end_date)}
    out: List[dict] = []
    for i in range(0, len(codes), MAX_CODES_PER_REQUEST):
        chunk = codes[i:i + MAX_CODES_PER_REQUEST]
        out += _paginate("getDataCode", dict(base, code=",".join(chunk)), sleep)
    return _merge_pages(out)


def get_data_by_layer(db: str, frequency: str, layer: Union[str, Sequence],
                      start_date: Optional[Union[str, int]] = None,
                      end_date: Optional[Union[str, int]] = None,
                      lang: str = "jp", sleep: float = DEFAULT_SLEEP) -> List[dict]:
    """Layer API. layer: "1,1,1" / "*" / [1, "*", 1] (max 5 levels).

    Raises BojApiError(M181007E) if > 1,250 series match: narrow the layer.
    """
    if not isinstance(layer, str):
        layer = ",".join(str(x) for x in layer)
    parts = layer.replace(" ", "").split(",")
    if not 1 <= len(parts) <= 5 or not all(re.fullmatch(r"\d+|\*", x) for x in parts):
        raise BojApiError(f"Invalid layer '{layer}' (1-5 comma-separated ints or '*')",
                          status=400, messageid="M181020E")
    params = {"db": db, "frequency": normalize_frequency(frequency),
              "layer": ",".join(parts), "lang": lang,
              "startDate": normalize_date(start_date), "endDate": normalize_date(end_date)}
    return _merge_pages(_paginate("getDataLayer", params, sleep))


def get_csv(endpoint: str, lang: str = "jp", **params) -> str:
    """Raw CSV (decoded text) from any endpoint, e.g.
    get_csv("getDataLayer", db="BP01", frequency="M", layer="1,1,1", startDate="202504").
    Note: no pagination is done for CSV — use JSON helpers for large pulls.
    """
    if "frequency" in params:
        params["frequency"] = normalize_frequency(params["frequency"])
    for k in ("startDate", "endDate", "start_date", "end_date"):
        if k in params:
            params["startDate" if k.startswith("start") else "endDate"] = normalize_date(params.pop(k))
    return polite_request(endpoint, dict(params, lang=lang), fmt="csv")


# ---------------------------------------------------------------------------
# pandas
# ---------------------------------------------------------------------------

def _parse_period(d, freq: str):
    import pandas as pd
    s = str(d)
    f = normalize_frequency(freq) if freq else ""
    try:
        if len(s) == 8:
            return pd.Timestamp(s)
        if f == "Q":
            return pd.Timestamp(year=int(s[:4]), month=(int(s[4:]) - 1) * 3 + 1, day=1)
        if f == "CH":
            return pd.Timestamp(year=int(s[:4]), month=1 if s[4:] == "01" else 7, day=1)
        if f == "FH":
            return pd.Timestamp(year=int(s[:4]), month=4 if s[4:] == "01" else 10, day=1)
        if f == "FY":
            return pd.Timestamp(year=int(s[:4]), month=4, day=1)
        if len(s) == 6:
            return pd.Timestamp(year=int(s[:4]), month=int(s[4:]), day=1)
        if len(s) == 4:
            return pd.Timestamp(year=int(s), month=1, day=1)
    except Exception:
        pass
    return pd.NaT


def to_dataframe(results: List[dict], parse_dates: bool = True,
                 columns: str = "name"):
    """RESULTSET list -> DataFrame (index = period, one column per series).

    parse_dates=True: index -> Timestamp (period start: Q 202402 -> 2024-04-01,
      FY 2024 -> 2024-04-01, D 20260901 -> 2026-09-01). False keeps raw ints.
    columns: "name" -> "名称（単位）" (original behaviour), "code" -> SERIES_CODE.
    null -> NaN. Series with no data are skipped.
    """
    import pandas as pd
    cols = []
    for s in results:
        v = s.get("VALUES") or {}
        dates, vals = v.get("SURVEY_DATES") or [], v.get("VALUES") or []
        if not dates:
            continue
        code = s["SERIES_CODE"]
        if columns == "code":
            col = code
        else:
            name = s.get("NAME_OF_TIME_SERIES_J") or s.get("NAME_OF_TIME_SERIES") or code
            unit = s.get("UNIT_J") or s.get("UNIT") or ""
            col = f"{name}（{unit}）" if unit else name
        idx = [_parse_period(d, s.get("FREQUENCY", "")) for d in dates] if parse_dates else dates
        ser = pd.to_numeric(pd.Series(vals, index=idx, name=col), errors="coerce")
        ser = ser[~ser.index.duplicated(keep="last")]
        cols.append(ser)
    if not cols:
        return pd.DataFrame()
    df = pd.concat(cols, axis=1).sort_index()
    if parse_dates:
        df.index.name = "date"
    return df


def get_series(db: str, codes: Union[str, Sequence[str]],
               start_date=None, end_date=None, lang: str = "jp",
               columns: str = "code", dropna: bool = False):
    """One-liner: get_data_by_code() -> to_dataframe(). Default columns = codes."""
    df = to_dataframe(get_data_by_code(db, codes, start_date, end_date, lang=lang),
                      columns=columns)
    return df.dropna(how="all") if dropna else df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="BOJ stat-search API quick tool")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find", help="search series names in a DB")
    f.add_argument("db"); f.add_argument("keywords", nargs="+")
    f.add_argument("--freq"); f.add_argument("-n", type=int, default=20)
    g = sub.add_parser("get", help="fetch series by code and print tail")
    g.add_argument("db"); g.add_argument("codes", help="comma-separated")
    g.add_argument("--start"); g.add_argument("--end")
    g.add_argument("-n", type=int, default=10)
    a = p.parse_args()
    try:
        if a.cmd == "find":
            for r in find_series(a.db, *a.keywords, frequency=a.freq, limit=a.n):
                print(f"{r['SERIES_CODE']:<24} {r['FREQUENCY']:<14} "
                      f"{r.get('START_OF_THE_TIME_SERIES')}-{r.get('END_OF_THE_TIME_SERIES')}  "
                      f"{r.get('NAME_OF_TIME_SERIES_J') or r.get('NAME_OF_TIME_SERIES')}")
        else:
            df = get_series(a.db, a.codes, a.start, a.end, dropna=True)
            print(df.tail(a.n).to_string())
    except BojApiError as e:
        print(f"エラー: {e}")


if __name__ == "__main__":
    _main()
