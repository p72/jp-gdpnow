#!/usr/bin/env python3
"""
e-Stat（政府統計の総合窓口）API wrapper — all reusable helpers live here.

Import everything from this module — never copy helper code into ad-hoc scripts.
If a helper is missing or wrong, fix it here.

    import sys; sys.path.insert(0, "<this skill's base directory>")
    from scripts.estat_api import (
        EStatError, request_json, check_status, as_list, text_of,
        search_stats, list_tables, get_meta_info, get_classes,
        get_stats_data, get_stats_df, estat_time_to_period, keep_monthly,
        get_data_catalog, catalog_resources, file_download_url,
        download_file, open_workbook, read_meti_ts,
    )

Behaviour baked in (learned from use):
- Rate limiting: Claude's egress proxy returns HTTP 403 after 3-4 rapid requests
  to e-Stat (this is NOT official e-Stat rate limiting). Every request goes through
  polite_get(): it keeps >= DEFAULT_SLEEP (2.5 s) between requests, and on 403
  waits RETRY_SLEEP (20 s) and retries (MAX_RETRIES=2). 429/5xx/network errors are
  retried the same way; 400/404 are never retried. No concurrent requests.
  A persistent 403 can also mean an e-Stat outage.
- Errors raise EStatError (never SystemExit): .status = HTTP code, .api_status =
  e-Stat RESULT.STATUS, .transient = True if a retry may help.
- STATUS=1 ("no data") for some statistics is structural (e.g. 一般職業紹介状況
  00450222): use getDataCatalog + download_file instead.
- e-Stat JSON turns single-element lists into dicts -> always use as_list().
- File downloads need a User-Agent header; downloaded files have no extension and
  ".xls" is often really xlsx -> open_workbook(bytes) sniffs the format.
- The download URL's fileKind depends on the file type (XLS=0, CSV=1, PDF=2,
  XLS_REP=4). A bare statInfId with the wrong fileKind gives 404 -> pass the
  catalog row (or its "url") to download_file(); a bare ID tries the kinds in turn.
- Time codes: monthly is 'YYYY00MMMM' (2026000808 = 2026年8月); tables often mix
  年 / 年度 rows -> keep_monthly().

App ID: env var ESTAT_APP_ID overrides the built-in default key.
"""

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_APP_ID = ""  # jp-gdpnow: set the ESTAT_APP_ID environment variable (GitHub Secret)
APP_ID = os.environ.get("ESTAT_APP_ID") or DEFAULT_APP_ID
BASE = "https://api.e-stat.go.jp/rest/3.0/app/json/"
FILE_DOWNLOAD = "https://www.e-stat.go.jp/stat-search/file-download"
# getDataCatalog FORMAT → fileKind of the download URL. 0/2/4 verified 2026-10-18
# (XLS_REP with fileKind=0 → 404). The catalog "URL" always carries the right one.
FILE_KINDS = {"XLS": 0, "CSV": 1, "PDF": 2, "XLS_REP": 4}

DEFAULT_SLEEP = 2.5      # min. seconds between requests (proxy 403 avoidance; 2-3 s required)
RETRY_SLEEP = 20.0       # wait after 403 / transient error
MAX_RETRIES = 2          # retries after the first attempt
DEFAULT_TIMEOUT = 60
USER_AGENT = "Mozilla/5.0 (estat-api-skill/2.0; research use)"

ENDPOINT_ROOT = {
    "getStatsList": "GET_STATS_LIST",
    "getMetaInfo": "GET_META_INFO",
    "getStatsData": "GET_STATS_DATA",
    "getDataCatalog": "GET_DATA_CATALOG",
    "refDataset": "GET_DATASET",
}

try:
    from .jp_font import setup_japanese_font  # noqa: F401  グラフ用日本語フォント
except ImportError:  # scripts/ を直接 sys.path に入れた場合
    from jp_font import setup_japanese_font  # noqa: F401

__all__ = [
    "setup_japanese_font", "read_stats_csv",
    "APP_ID", "EStatError", "get_app_id", "polite_get", "request_json",
    "check_status", "as_list", "text_of",
    "search_stats", "list_tables", "print_results",
    "get_meta_info", "get_classes", "print_meta_info",
    "fetch_page", "parse_data", "get_stats_data", "get_stats_df",
    "output_csv", "output_json",
    "estat_time_to_period", "keep_monthly",
    "FILE_KINDS", "get_data_catalog", "catalog_resources", "file_download_url",
    "download_file", "sniff_excel", "open_workbook", "read_meti_ts",
]


class EStatError(Exception):
    """Raised for failed requests or e-Stat API errors.

    Attributes:
        status: HTTP status code if any (e.g. 403, 500), else None.
        api_status: e-Stat RESULT.STATUS if any (1 = no data, 100 = bad appId, ...).
        transient: True if retrying later may succeed.
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 api_status: Optional[int] = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.api_status = api_status
        self.transient = transient


def get_app_id() -> str:
    """ESTAT_APP_ID env var (read at call time) or the built-in default."""
    return os.environ.get("ESTAT_APP_ID") or APP_ID


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Polite request core
# ---------------------------------------------------------------------------

_last_request_time = 0.0


def polite_get(url: str, sleep: Optional[float] = None, timeout: float = DEFAULT_TIMEOUT,
               retries: int = MAX_RETRIES, retry_wait: float = RETRY_SLEEP,
               headers: Optional[Dict[str, str]] = None) -> bytes:
    """GET url and return the raw body, with throttling + retry.

    - Keeps >= `sleep` seconds (default DEFAULT_SLEEP) since the previous request
      made through this module (API calls and file downloads alike).
    - HTTP 403 (proxy limit), 429, 5xx and network errors: wait `retry_wait` s and
      retry up to `retries` times. Other HTTP errors are raised immediately.
    """
    global _last_request_time
    sleep = DEFAULT_SLEEP if sleep is None else sleep
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    for attempt in range(retries + 1):
        wait = sleep - (time.time() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.time()
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            transient = e.code in (403, 429) or e.code >= 500
            err = EStatError(f"HTTP {e.code} {e.reason} for {_redact(url)}",
                             status=e.code, transient=transient)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = EStatError(f"Network error: {getattr(e, 'reason', e)} for {_redact(url)}",
                             transient=True)
        finally:
            _last_request_time = time.time()
        if not err.transient or attempt >= retries:
            if err.status == 403:
                err.args = (err.args[0] + " — proxy limit persisted after retries, "
                            "or e-Stat outage / invalid appId",)
            raise err
        what = "403 Forbidden (proxy limit?)" if err.status == 403 else str(err)
        _log(f"⚠️ {what}: waiting {retry_wait:.0f}s then retry ({attempt + 1}/{retries})")
        time.sleep(retry_wait)
    raise EStatError("unreachable")  # pragma: no cover


def _redact(url: str) -> str:
    return re.sub(r"appId=[^&]+", "appId=***", url)


def request_json(endpoint: str, params: Dict[str, Any], sleep: Optional[float] = None,
                 timeout: float = DEFAULT_TIMEOUT, retries: int = MAX_RETRIES) -> Dict[str, Any]:
    """GET an e-Stat JSON endpoint (e.g. "getStatsData") and return the parsed dict.

    appId and lang=J are added automatically. None/"" params are dropped.
    Does NOT check RESULT.STATUS — call check_status() (the high-level helpers do).
    """
    p = {"appId": get_app_id(), "lang": "J"}
    p.update({k: v for k, v in params.items() if v is not None and v != ""})
    url = f"{BASE}{endpoint}?{urllib.parse.urlencode(p)}"
    body = polite_get(url, sleep=sleep, timeout=timeout, retries=retries)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise EStatError(f"Invalid JSON from {endpoint}: {e}; body starts {body[:200]!r}")


def check_status(data: Dict[str, Any], root_key: Optional[str] = None) -> Dict[str, Any]:
    """Validate RESULT.STATUS and return the body under root_key.

    root_key defaults to the single top-level key. STATUS 0 and 2 (partial) pass.
    STATUS 1 (no data) -> EStatError(api_status=1) with a getDataCatalog hint.
    """
    if root_key is None:
        if len(data) != 1:
            raise EStatError(f"Cannot infer root key from {list(data)}")
        root_key = next(iter(data))
    body = data.get(root_key)
    if not isinstance(body, dict):
        raise EStatError(f"Response has no '{root_key}' (keys: {list(data)})")
    result = body.get("RESULT", {})
    status = result.get("STATUS")
    try:
        status = int(status)
    except (TypeError, ValueError):
        pass
    if status in (0, 2):
        return body
    msg = result.get("ERROR_MSG", "不明なエラー")
    if status == 1:
        raise EStatError(
            f"該当データなし (STATUS=1): {msg} → フィルタ条件を確認。統計によっては "
            "getStatsData 非対応（SKILL.md「構造的非対応」参照）→ get_data_catalog + download_file を使う",
            api_status=1)
    transient = isinstance(status, int) and 200 <= status < 300
    raise EStatError(f"e-Stat APIエラー (STATUS={status}): {msg}",
                     api_status=status, transient=transient)


def as_list(obj) -> list:
    """e-Stat JSON returns a dict when there is only one element → always a list."""
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    return [obj]


def text_of(obj, default: str = "") -> str:
    """{"@code":..., "$": "text"} → "text"; plain str/int passes through."""
    if isinstance(obj, dict):
        return str(obj.get("$", default))
    return default if obj is None else str(obj)


def _sleep_kw(sleep):
    return {} if sleep is None else {"sleep": sleep}


# ---------------------------------------------------------------------------
# getStatsList
# ---------------------------------------------------------------------------

def search_stats(keyword: Optional[str] = None, limit: int = 20, stats_code: Optional[str] = None,
                 sleep: Optional[float] = None, **params) -> Dict[str, Any]:
    """getStatsList raw response. keyword supports AND/OR/NOT.

    Extra API params pass through (surveyYears, statsField, updatedDate, ...).
    """
    p = {"searchWord": keyword, "limit": limit, "statsCode": stats_code, **params}
    return request_json("getStatsList", p, **_sleep_kw(sleep))


def list_tables(keyword: Optional[str] = None, limit: int = 20, stats_code: Optional[str] = None,
                sleep: Optional[float] = None, **params) -> List[Dict[str, str]]:
    """getStatsList → list of {id, stat_name, stats_code, title, survey_date, cycle,
    open_date, updated_date}. Empty list if nothing found."""
    try:
        body = check_status(search_stats(keyword, limit, stats_code, sleep, **params),
                            "GET_STATS_LIST")
    except EStatError as e:
        if e.api_status == 1:
            return []
        raise
    out = []
    for t in as_list(body.get("DATALIST_INF", {}).get("TABLE_INF")):
        sn = t.get("STAT_NAME", {})
        out.append({
            "id": t.get("@id", ""),
            "stat_name": text_of(sn),
            "stats_code": sn.get("@code", "") if isinstance(sn, dict) else "",
            "title": text_of(t.get("TITLE")),
            "survey_date": text_of(t.get("SURVEY_DATE")),
            "cycle": text_of(t.get("CYCLE")),
            "open_date": text_of(t.get("OPEN_DATE")),
            "updated_date": text_of(t.get("UPDATED_DATE")),
        })
    return out


def print_results(data: Dict[str, Any]) -> None:
    """Pretty-print a search_stats() response."""
    try:
        body = check_status(data, "GET_STATS_LIST")
    except EStatError as e:
        if e.api_status != 1:
            raise
        body = {}
    tables = as_list(body.get("DATALIST_INF", {}).get("TABLE_INF"))
    if not tables:
        print("該当する統計表が見つかりませんでした。")
        print("ヒント: キーワードを短くする / statsCode指定を試す / "
              "getDataCatalog対象の統計でないか確認する")
        return
    print(f"\n📊 検索結果: {len(tables)}件\n" + "-" * 80)
    for i, t in enumerate(tables, 1):
        print(f"[{i}] ID: {t.get('@id', 'N/A')}")
        print(f"    統計名: {text_of(t.get('STAT_NAME'), 'N/A')}")
        print(f"    表題: {text_of(t.get('TITLE'), 'N/A')}")
        print(f"    調査日: {t.get('SURVEY_DATE', 'N/A')}  周期: {t.get('CYCLE', '')}  "
              f"更新: {t.get('UPDATED_DATE', '')}")
        print("-" * 80)


# ---------------------------------------------------------------------------
# getMetaInfo
# ---------------------------------------------------------------------------

def _check_id(stats_data_id: str) -> str:
    s = str(stats_data_id).strip()
    if not re.fullmatch(r"\d{10}", s):
        raise EStatError(f"statsDataId must be 10 digits (got {stats_data_id!r}); "
                         "keep leading zeros — pass it as a string", status=400)
    return s


def get_meta_info(stats_data_id: str, sleep: Optional[float] = None) -> Dict[str, Any]:
    """getMetaInfo raw response."""
    return request_json("getMetaInfo", {"statsDataId": _check_id(stats_data_id),
                                        "explanationGetFlg": "N"}, **_sleep_kw(sleep))


def _classes_from(class_inf: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for cls in as_list(class_inf.get("CLASS_OBJ")):
        items = [{"code": it.get("@code", ""), "name": it.get("@name", ""),
                  "level": it.get("@level", ""), "unit": it.get("@unit", ""),
                  "parent": it.get("@parentCode", "")} for it in as_list(cls.get("CLASS"))]
        cid = cls.get("@id", "")
        out[cid] = {"name": cls.get("@name", ""), "filter": _filter_name(cid), "items": items}
    return out


def _filter_name(cls_id: str) -> str:
    return f"cd{cls_id[:1].upper()}{cls_id[1:]}" if cls_id else ""


def get_classes(stats_data_id: str, sleep: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """Classification codes of a table:
    {cls_id: {"name", "filter" (e.g. "cdCat01"), "items": [{code,name,level,unit,parent}]}}"""
    body = check_status(get_meta_info(stats_data_id, sleep), "GET_META_INFO")
    return _classes_from(body.get("METADATA_INF", {}).get("CLASS_INF", {}))


def print_meta_info(data: Dict[str, Any], show_all: bool = False) -> None:
    """Pretty-print a get_meta_info() response incl. the filter arg for each class."""
    body = check_status(data, "GET_META_INFO")
    metadata = body.get("METADATA_INF", {})
    t = metadata.get("TABLE_INF", {})
    print("\n📋 統計表情報\n" + "=" * 60)
    print(f"統計名: {text_of(t.get('STAT_NAME'), 'N/A')}")
    print(f"表題: {text_of(t.get('TITLE'), 'N/A')}")
    print(f"総件数: {t.get('OVERALL_TOTAL_NUMBER', 'N/A')}  更新日: {t.get('UPDATED_DATE', 'N/A')}")
    print("\n📂 分類項目（フィルタに使うコード）\n" + "=" * 60)
    for cid, c in _classes_from(metadata.get("CLASS_INF", {})).items():
        items = c["items"]
        print(f"\n▶ {c['name']} (@id={cid}, {len(items)}件) → フィルタ引数: "
              f"--{c['filter']}（Python: {c['filter']}=...）")
        print("-" * 40)
        limit = len(items) if show_all else 20
        for it in items[:limit]:
            lv = f" [L{it['level']}]" if it["level"] else ""
            print(f"  {it['code']}: {it['name']}{lv}")
        if len(items) > limit:
            print(f"  ... 他 {len(items) - limit} 件（--all で全件表示）")


# ---------------------------------------------------------------------------
# getStatsData
# ---------------------------------------------------------------------------

_FILTER_RE = re.compile(r"^(lv|cd)(Tab|Time|Area|Cat(0[1-9]|1[0-5]))(From|To)?$")
_OTHER_PARAMS = {"limit", "startPosition", "metaGetFlg", "cntGetFlg", "explanationGetFlg",
                 "annotationGetFlg", "replaceSpChar", "sectionHeaderFlg", "dataSetId"}


def _validate_params(params: Dict[str, Any]) -> None:
    bad = [k for k in params if not (_FILTER_RE.match(k) or k in _OTHER_PARAMS)]
    if bad:
        raise EStatError(f"Unknown getStatsData params: {bad} (filters are camelCase: "
                         "cdTab, cdCat01..cdCat15, cdArea, cdTime, cdTimeFrom, lvCat01, ...)",
                         status=400)
    if "limit" in params and params["limit"] is not None and int(params["limit"]) < 1:
        raise EStatError("limit must be >= 1", status=400)


def fetch_page(stats_data_id: str, params: Dict[str, Any], sleep: Optional[float] = None) -> Dict[str, Any]:
    """One getStatsData page (raw). params are API params (cdCat01=..., limit=..., ...)."""
    _validate_params(params)
    p = {"statsDataId": _check_id(stats_data_id), "metaGetFlg": "Y", "cntGetFlg": "N",
         "explanationGetFlg": "N", "annotationGetFlg": "N", **params}
    return request_json("getStatsData", p, **_sleep_kw(sleep))


def parse_data(data: Dict[str, Any]) -> Tuple[List[str], List[list], int, Optional[int]]:
    """getStatsData JSON → (headers, rows, total, next_key).

    headers: <cls>_code, <cls>_name for each class (tab, cat01.., area, time), value, unit.
    """
    body = check_status(data, "GET_STATS_DATA")
    sd = body.get("STATISTICAL_DATA", {})
    classes = _classes_from(sd.get("CLASS_INF", {}))
    names = {(cid, it["code"]): it["name"] for cid, c in classes.items() for it in c["items"]}
    class_ids = list(classes)
    headers = [h for cid in class_ids for h in (f"{cid}_code", f"{cid}_name")] + ["value", "unit"]
    rows = []
    for v in as_list(sd.get("DATA_INF", {}).get("VALUE")):
        row = []
        for cid in class_ids:
            code = v.get(f"@{cid}", v.get(f"@{cid.lower()}", ""))
            row += [code, names.get((cid, code), "")]
        row += [v.get("$", ""), v.get("@unit", "")]
        rows.append(row)
    ri = sd.get("RESULT_INF", {})
    total = int(ri.get("TOTAL_NUMBER", len(rows)) or 0)
    nk = ri.get("NEXT_KEY")
    return headers, rows, total, (int(nk) if nk else None)


def get_stats_data(stats_data_id: str, all_pages: bool = False, limit: Optional[int] = 100,
                   sleep: Optional[float] = None, max_pages: Optional[int] = None,
                   verbose: bool = True, **filters) -> Tuple[List[str], List[list]]:
    """getStatsData with optional NEXT_KEY pagination → (headers, rows).

    filters: cdTab="3", cdCat01="0001", cdArea="00000", cdTimeFrom="2020000101", ...
    all_pages=True follows NEXT_KEY (limit = rows per page; use filters!).
    Progress goes to stderr.
    """
    params = dict(filters)
    if limit:
        params["limit"] = limit
    headers: List[str] = []
    rows: List[list] = []
    page, total = 1, 0
    while True:
        headers, r, total, next_key = parse_data(fetch_page(stats_data_id, params, sleep))
        rows += r
        if verbose:
            _log(f"📄 page {page}: {len(r)} rows (累計 {len(rows)} / 全{total})")
        if not all_pages or not next_key or (max_pages and page >= max_pages):
            break
        params["startPosition"] = next_key
        page += 1
    if all_pages and len(rows) < total and verbose:
        _log(f"⚠️ 全{total}件のうち{len(rows)}件で終了")
    return headers, rows


def get_stats_df(stats_data_id: str, all_pages: bool = True, limit: Optional[int] = 100000,
                 monthly: bool = False, sleep: Optional[float] = None, **filters):
    """get_stats_data() → pandas DataFrame (codes stay strings).

    'value' becomes float (special chars like '-', '***' → NaN); original in 'value_raw'.
    monthly=True applies keep_monthly() (PeriodIndex, ascending, 年/年度 rows dropped).
    """
    import pandas as pd
    headers, rows = get_stats_data(stats_data_id, all_pages=all_pages, limit=limit,
                                   sleep=sleep, **filters)
    df = pd.DataFrame(rows, columns=headers, dtype=str)
    df["value_raw"] = df["value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    if monthly and "time_code" in df.columns:
        df = keep_monthly(df)
    return df


def output_csv(headers, rows, filename: Optional[str] = None) -> None:
    """Write CSV (utf-8-sig) to filename, or to stdout."""
    if filename:
        with open(filename, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)
        _log(f"💾 保存完了: {filename} ({len(rows)} rows)")
    else:
        w = csv.writer(sys.stdout)
        w.writerow(headers)
        w.writerows(rows)


def read_stats_csv(path: str):
    """CLI（`data --file x.csv`）や output_csv() で保存したCSVを読み戻す → DataFrame。

    コード列は文字列のまま（先頭0を守る）、`value` は float に変換、原文は `value_raw`。
    ※ pd.read_csv(path, dtype=str) だけだと value も文字列になり、計算が NaN や型エラーになる。
    """
    import pandas as pd
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    if "value" in df.columns:
        df["value_raw"] = df["value"]
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def output_json(headers, rows, filename: Optional[str] = None) -> None:
    """Write list-of-dicts JSON to filename, or to stdout."""
    s = json.dumps([dict(zip(headers, r)) for r in rows], ensure_ascii=False, indent=2)
    if filename:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(s)
        _log(f"💾 保存完了: {filename} ({len(rows)} rows)")
    else:
        print(s)


# ---------------------------------------------------------------------------
# Time codes
# ---------------------------------------------------------------------------

_NON_MONTHLY_NAME = re.compile(r"(年度|期|四半期|半期|年$|年平均|年計)")


def estat_time_to_period(code, name=""):
    """e-Stat time code (+ optional time_name) → pandas.Period('YYYY-MM', 'M'),
    or None for non-monthly rows (年・年度・四半期 …).

    Monthly code is 'YYYY00MMMM' (2026000808 = 2026年8月; never use code[:6]).
    年 = 'YYYY000000', 年度 = 'YYYY100000'. time_name wins when it is recognisable.
    """
    import pandas as pd
    n = "" if name is None else str(name).strip()
    if n.lower() == "nan":
        n = ""
    m = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月", n)
    if m:
        return pd.Period(f"{m[1]}-{int(m[2]):02d}", freq="M")
    if n and _NON_MONTHLY_NAME.search(n):
        return None
    c = str(code).strip()
    if (len(c) == 10 and c.isdigit() and c[4:6] == "00" and c[6:8] == c[8:10]
            and 1 <= int(c[6:8]) <= 12):
        return pd.Period(f"{c[:4]}-{c[6:8]}", freq="M")
    return None


def keep_monthly(df, code_col: str = "time_code", name_col: str = "time_name"):
    """Keep only monthly rows, set a monthly PeriodIndex and sort ascending.

    Works on get_stats_df() output or a fetch CSV read with pd.read_csv(..., dtype=str).
    """
    import pandas as pd
    names = df[name_col] if name_col in df.columns else [""] * len(df)
    p = [estat_time_to_period(c, n) for c, n in zip(df[code_col].astype(str), names)]
    out = df.assign(_period=p).dropna(subset=["_period"])
    idx = pd.PeriodIndex(out.pop("_period").tolist(), freq="M", name="period")
    return out.set_axis(idx, axis=0).sort_index()


# ---------------------------------------------------------------------------
# getDataCatalog / file download
# ---------------------------------------------------------------------------

def get_data_catalog(stats_code: Optional[str] = None, search_word: Optional[str] = None,
                     limit: int = 20, sleep: Optional[float] = None, **params) -> Dict[str, Any]:
    """getDataCatalog raw response (files: XLS/CSV/PDF ...). Extra params pass through
    (dataType="XLS", updatedDate=..., startPosition=...)."""
    p = {"statsCode": stats_code, "searchWord": search_word, "limit": limit, **params}
    return request_json("getDataCatalog", p, **_sleep_kw(sleep))


def catalog_resources(stats_code: Optional[str] = None, search_word: Optional[str] = None,
                      name_contains: Optional[str] = None, limit: int = 50,
                      sleep: Optional[float] = None, data: Optional[Dict[str, Any]] = None,
                      **params) -> List[Dict[str, str]]:
    """Flatten getDataCatalog into one row per file, newest LAST_MODIFIED_DATE first.

    Rows: {name, dataset, format, url, stat_inf_id, file_kind, last_modified,
    release_date, resource_id, catalog_id, landing_page}. Download with
    download_file(row) / download_file(row["url"]) — the URL has the right fileKind. name_contains filters on the file name.
    Pass data= to reuse an existing get_data_catalog() response (no request).
    """
    if data is None:
        data = get_data_catalog(stats_code, search_word, limit, sleep, **params)
    try:
        body = check_status(data, "GET_DATA_CATALOG")
    except EStatError as e:
        if e.api_status == 1:
            return []
        raise
    out = []
    for cat in as_list(body.get("DATA_CATALOG_LIST_INF", {}).get("DATA_CATALOG_INF")):
        ds = cat.get("DATASET", {})
        for r in as_list(cat.get("RESOURCES", {}).get("RESOURCE")):
            url = r.get("URL", "")
            m = re.search(r"statInfId=(\d+)", url)
            k = re.search(r"fileKind=(\d+)", url)
            out.append({
                "name": text_of(r.get("TITLE", {}).get("NAME")),
                "dataset": text_of(ds.get("TITLE", {}).get("NAME")),
                "format": r.get("FORMAT", ""),
                "url": url,
                "stat_inf_id": m.group(1) if m else "",
                "file_kind": int(k.group(1)) if k else None,
                "last_modified": r.get("LAST_MODIFIED_DATE", ""),
                "release_date": r.get("RELEASE_DATE", ""),
                "resource_id": r.get("@id", ""),
                "catalog_id": cat.get("@id", ""),
                "landing_page": ds.get("LANDING_PAGE", ""),
            })
    if name_contains:
        out = [r for r in out if name_contains in r["name"]]
    out.sort(key=lambda r: r["last_modified"], reverse=True)
    return out


def file_download_url(stat_inf_id: str, file_kind=0) -> str:
    """statInfId → e-Stat file download URL. file_kind is an int or a catalog FORMAT
    name ("XLS"=0, "CSV"=1, "PDF"=2, "XLS_REP"=4). A wrong fileKind gives 404, so prefer
    the catalog row's own "url". IDs can change on update → re-find with catalog_resources()."""
    if isinstance(file_kind, str):
        file_kind = FILE_KINDS[file_kind.upper()]
    return f"{FILE_DOWNLOAD}?&statInfId={stat_inf_id}&fileKind={file_kind}"


def _is_html(content: bytes) -> bool:
    return content[:15].lstrip().lower().startswith((b"<!doctype html", b"<html"))


def download_file(target, path: Optional[str] = None, sleep: Optional[float] = None,
                  timeout: float = 120, file_kind=None) -> bytes:
    """Download an e-Stat file politely (User-Agent, throttling) and return its bytes.

    target: a catalog_resources() row (dict), a full URL, or a bare statInfId.
      - row / URL: used as is — the catalog URL already has the right fileKind
        (recommended; XLS_REP files are fileKind=4 and 404 with fileKind=0).
      - bare statInfId: uses file_kind if given, otherwise tries 0 (XLS), 4 (XLS_REP),
        1 (CSV), 2 (PDF) until one returns a file (each miss costs one polite request).
    Writes to `path` if given. Raises EStatError if nothing but 404/HTML comes back.
    """
    if isinstance(target, dict):
        target = target.get("url") or target.get("stat_inf_id") or ""
    s = str(target).strip()
    if not re.fullmatch(r"\d{6,}", s):
        urls = [s]
    elif file_kind is not None:
        urls = [file_download_url(s, file_kind)]
    else:
        urls = [file_download_url(s, k) for k in (0, 4, 1, 2)]

    content, tried = None, []
    for url in urls:
        try:
            body = polite_get(url, sleep=sleep, timeout=timeout)
        except EStatError as e:
            if e.status == 404 and len(urls) > 1:
                tried.append(f"fileKind={url.rsplit('=', 1)[-1]}: 404")
                continue
            raise
        if _is_html(body):
            tried.append(f"fileKind={url.rsplit('=', 1)[-1]}: HTML")
            continue
        content = body
        if len(urls) > 1 and url != urls[0]:
            _log(f"ℹ️ statInfId {s}: fileKind={url.rsplit('=', 1)[-1]} で取得 "
                 f"({', '.join(tried)})")
        break
    if content is None:
        raise EStatError(f"No file for {_redact(urls[-1]) if len(urls) == 1 else s} "
                         f"({'; '.join(tried) or 'HTML page'}). statInfId outdated or wrong "
                         "fileKind → re-find it with catalog_resources() and pass row['url']",
                         status=404)
    if path:
        with open(path, "wb") as f:
            f.write(content)
    return content


def sniff_excel(content: bytes) -> str:
    """'xlsx' (zip, Excel 2007+), 'xls' (OLE2 legacy), 'pdf', 'csv/text' or 'unknown'.
    e-Stat '.xls' files are often really xlsx."""
    if content[:4] == b"PK\x03\x04":
        return "xlsx"
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    if content[:5] == b"%PDF-":
        return "pdf"
    try:
        content[:2000].decode("utf-8")
        return "csv/text"
    except UnicodeDecodeError:
        try:
            content[:2000].decode("shift_jis")
            return "csv/text"
        except UnicodeDecodeError:
            return "unknown"


def open_workbook(content: bytes, read_only: bool = True):
    """Open downloaded bytes as a workbook (no extension needed; sniffs the format).

    xlsx → openpyxl Workbook (data_only). legacy xls → xlrd Book (needs `pip install xlrd`).
    """
    kind = sniff_excel(content)
    if kind == "xlsx":
        import openpyxl
        return openpyxl.load_workbook(io.BytesIO(content), read_only=read_only, data_only=True)
    if kind == "xls":
        try:
            import xlrd
        except ImportError:
            raise EStatError("Legacy .xls (OLE2) file needs xlrd: "
                             "pip install xlrd --break-system-packages "
                             "(then pandas.read_excel(io.BytesIO(b), engine='xlrd') also works)") from None
        return xlrd.open_workbook(file_contents=content)
    raise EStatError(f"Not an Excel file (detected: {kind})")


def read_meti_ts(content: bytes, sheet: str):
    """Read one sheet of a 商業動態統計 long time-series file (e-Stat download).

    Layout: A=time code (YYYY00MMMM etc.), B=display name, C..=series; the row just
    above the first data row holds series names. Monthly sheets get a PeriodIndex and
    are sorted ascending (files are newest-first). Values are 百万円 / ratios as in file.
    Sheet names use full-width spaces, e.g. "百貨店\\u3000販売額\\u3000月次".
    """
    import pandas as pd
    wb = open_workbook(content)
    if sheet not in wb.sheetnames:
        raise EStatError(f"Sheet {sheet!r} not found. Sheets: {wb.sheetnames}")
    rows = list(wb[sheet].iter_rows(values_only=True))
    is_data = lambda r: bool(r) and r[0] is not None and re.fullmatch(r"\d{10}", str(r[0]).strip())
    first = next((i for i, r in enumerate(rows) if is_data(r)), None)
    if first is None:
        raise EStatError(f"No time-code rows found in sheet {sheet!r}")
    names = [str(c).strip() if c is not None else f"col{j}" for j, c in enumerate(rows[first - 1])]
    df = pd.DataFrame([r[:len(names)] for r in rows[first:] if is_data(r)], columns=names)
    df = df.rename(columns={names[0]: "time_code", names[1]: "time_name"})
    df["time_code"] = df["time_code"].astype(str).str.strip()
    m = df["time_name"].astype(str).str.extract(r"(\d{4})年\s*(\d{1,2})月")
    if len(df) and m.notna().all().all():
        df.index = pd.PeriodIndex([f"{y}-{int(mm):02d}" for y, mm in m.values], freq="M")
        df = df.sort_index()
    return df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_data_args(p) -> None:
    p.add_argument("stats_data_id", help="統計表ID（10桁）")
    p.add_argument("--limit", type=int, default=100,
                   help="取得件数（--all時は1ページの件数。デフォルト100）")
    p.add_argument("--start", type=int, help="開始位置")
    p.add_argument("--all", action="store_true", help="NEXT_KEYを辿って全件取得（フィルタ併用推奨）")
    p.add_argument("--output", choices=["csv", "json"], default="csv")
    p.add_argument("--file", help="出力ファイル名（省略時stdout。進捗はstderr）")
    for k in ["cdTab", "cdArea", "cdTime", "cdTimeFrom", "cdTimeTo"] + \
             [f"cdCat{i:02d}" for i in range(1, 16)]:
        p.add_argument(f"--{k}", help=f"{k} フィルタ（カンマ区切り可）")


def _data_filters(a) -> Dict[str, Any]:
    keys = ["cdTab", "cdArea", "cdTime", "cdTimeFrom", "cdTimeTo"] + \
           [f"cdCat{i:02d}" for i in range(1, 16)]
    f = {k: getattr(a, k) for k in keys if getattr(a, k, None)}
    if a.start:
        f["startPosition"] = a.start
    return f


def cli_search(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="e-Stat統計表検索 (getStatsList)")
    p.add_argument("keyword", help="検索キーワード（AND/OR/NOT使用可）")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--statsCode", help="政府統計コード（5桁=機関 / 8桁=統計）")
    p.add_argument("--sleep", type=float, default=None)
    a = p.parse_args(argv)
    return _run(lambda: print_results(search_stats(a.keyword, a.limit, a.statsCode, a.sleep)))


def cli_meta(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="e-Statメタ情報取得 (getMetaInfo)")
    p.add_argument("stats_data_id")
    p.add_argument("--all", action="store_true", help="分類コードを全件表示")
    p.add_argument("--sleep", type=float, default=None)
    a = p.parse_args(argv)
    return _run(lambda: print_meta_info(get_meta_info(a.stats_data_id, a.sleep), a.all))


def cli_data(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="e-Stat統計データ取得 (getStatsData)")
    _add_data_args(p)
    p.add_argument("--sleep", type=float, default=None)
    a = p.parse_args(argv)

    def go():
        h, rows = get_stats_data(a.stats_data_id, all_pages=a.all, limit=a.limit,
                                 sleep=a.sleep, **_data_filters(a))
        (output_csv if a.output == "csv" else output_json)(h, rows, a.file)
    return _run(go)


def cli_catalog(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="e-Statファイルカタログ (getDataCatalog)")
    p.add_argument("--statsCode")
    p.add_argument("--word", help="searchWord")
    p.add_argument("--name", help="ファイル名の部分一致で絞る")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("-n", type=int, default=20, help="表示件数")
    p.add_argument("--sleep", type=float, default=None)
    a = p.parse_args(argv)

    def go():
        res = catalog_resources(a.statsCode, a.word, a.name, a.limit, a.sleep)
        print(f"{len(res)} files (newest first)")
        for r in res[:a.n]:
            print(f"{r['last_modified']}  {r['stat_inf_id']}  [{r['format']}]  {r['name']}"
                  f"  <{r['dataset']}>")
    return _run(go)


def cli_download(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="e-Statファイルダウンロード")
    p.add_argument("target", help="statInfId または URL（catalog の URL 推奨）")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--kind", default=None,
                   help="fileKind（0/1/2/4 または XLS/CSV/PDF/XLS_REP）。省略時は順に試す")
    a = p.parse_args(argv)

    def go():
        kind = int(a.kind) if a.kind and a.kind.isdigit() else a.kind
        c = download_file(a.target, a.out, file_kind=kind)
        t = sniff_excel(c)
        print(f"saved {a.out}: {len(c)} bytes, type={t}")
        if t in ("xlsx", "xls"):
            wb = open_workbook(c)
            print("sheets:", wb.sheetnames if t == "xlsx" else wb.sheet_names())
    return _run(go)


def _run(fn) -> int:
    try:
        fn()
        return 0
    except EStatError as e:
        _log(f"❌ {e}")
        return 1
    except BrokenPipeError:  # e.g. piped into `head`
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0


def main(argv=None) -> int:
    cmds = {"search": cli_search, "meta": cli_meta, "data": cli_data,
            "catalog": cli_catalog, "download": cli_download}
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in cmds:
        print("usage: estat_api.py {search|meta|data|catalog|download} ... (-h for help)\n"
              "  search 消費者物価指数 [--statsCode 00200573]\n"
              "  meta 0003427113 [--all]\n"
              "  data 0003427113 --cdCat01 0001 --cdArea 00000 --cdTab 3 --all --file cpi.csv\n"
              "  catalog --statsCode 00550030 --word 百貨店\n"
              "  download 000031387994 -o dept.xlsx", file=sys.stderr)
        return 2
    return cmds[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
