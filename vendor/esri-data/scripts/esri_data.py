#!/usr/bin/env python3
"""
内閣府 経済社会総合研究所（ESRI）統計データ取得モジュール
ESRI (Cabinet Office, Japan) statistics downloader / parser.

Import everything from here — do NOT copy helpers into ad-hoc scripts.
If a helper is missing or wrong, fix this module, not a local copy.

    import sys; sys.path.insert(0, "<skill base dir>")
    from scripts.esri_data import (
        EsriDataError, polite_get, get_html, list_file_links, source_url,
        # QE (四半期別GDP速報)
        get_latest_qe_tables_url, get_latest_qe_info, qe_csv_url,
        get_qe_raw, qe_header, parse_qe_column, get_qe_df,
        # 景気動向指数 / 消費者態度指数
        get_latest_ci_mmdd, get_ci_df, get_ci_component, get_ci_component_df,
        get_consumer_sentiment,
        # 機械受注
        get_juchu_chouki, get_juchu_df, get_juchu_monthly_csv,
        # 法人企業景気予測調査（財務省サイト）
        get_bos_timeseries, get_bos_bsi,
        # 年次推計（確報）
        get_latest_kakuhou_year, kakuhou_top_url, list_kakuhou_tables,
        get_kakuhou_excel, parse_kakuhou_sheet, get_kakuhou_table, kakuhou_row,
        # グラフ用日本語フォント
        setup_japanese_font,
    )

Behaviour baked in (learned from use):
- Every download goes through polite_get(): User-Agent, timeout, sleep after
  each request, one retry on 429 / 5xx / network errors, no retry on other 4xx.
- Errors raise EsriDataError (.status, .transient, .url).
- URLs ending in "/" (folder URLs) return 403 on esri.cao.go.jp — rejected
  locally with a clear error. Use .html pages or real .xlsx/.csv files.
- File names that change every release (QE folder, CI {MMDD}, 機械受注 {YYMM},
  確報 {YEAR}) are always scraped from index pages — never guessed.
- ESRI CSVs are Shift-JIS; HTML pages are UTF-8.
- QE CSVs are read header=None; column positions differ between
  ritu / gaku / def files → use qe_header() or label lookup in parse_qe_column().
- Pages and files are cached in-process (per URL) to avoid repeat downloads.
"""

from __future__ import annotations

import html as _html
import io
import re
import time
import urllib.parse
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

import pandas as pd
import requests

try:
    from .jp_font import setup_japanese_font  # noqa: F401
except ImportError:  # 直接 scripts/ を sys.path に入れた場合
    from jp_font import setup_japanese_font  # noqa: F401

__all__ = [
    "EsriDataError", "polite_get", "get_html", "list_file_links", "source_url",
    "clear_cache",
    "get_latest_qe_tables_url", "get_latest_qe_info", "qe_csv_url", "get_qe_raw",
    "qe_header", "parse_qe_column", "get_qe_df", "QE_MAIN_COLS",
    "get_latest_ci_mmdd", "get_ci_df", "get_ci_component", "get_ci_component_df",
    "get_consumer_sentiment",
    "get_juchu_chouki", "get_juchu_df", "JUCHU_MAIN_COLS", "get_juchu_monthly_csv",
    "get_bos_timeseries", "get_bos_bsi",
    "get_latest_kakuhou_year", "kakuhou_top_url", "list_kakuhou_tables",
    "get_kakuhou_excel", "parse_kakuhou_sheet", "get_kakuhou_table", "kakuhou_row",
    "setup_japanese_font",
]

ESRI = "https://www.esri.cao.go.jp"
SNA_MENU = ESRI + "/jp/sna/menu.html"
QE_TOP = ESRI + "/jp/sna/sokuhou/sokuhou_top.html"
QE_FILES = ESRI + "/jp/sna/data/data_list/sokuhou/files"
KAKUHOU_FILES = ESRI + "/jp/sna/data/data_list/kakuhou/files"
DI_PAGE = ESRI + "/jp/stat/di/di.html"
DI_BASE = ESRI + "/jp/stat/di/"
JUCHU_BASE = ESRI + "/jp/stat/juchu/"
BOS_BASE = "https://www.mof.go.jp/pri/reference/bos/results/"

DEFAULT_TIMEOUT = 30
DEFAULT_SLEEP = 1.0
RETRY_WAIT = 10
USER_AGENT = "esri-data-skill/2.0 (research use; +https://www.esri.cao.go.jp/)"

# Citation pages (never cite folder URLs ending in "/": they return 403)
SOURCE_PAGES = {
    "qe": QE_TOP,
    "kakuhou": KAKUHOU_FILES + "/{year}/{year}_kaku_top.html",
    "di": ESRI + "/jp/stat/di/menu_di.html",
    "juchu": ESRI + "/jp/stat/juchu/menu_juchu.html",
    "shouhi": ESRI + "/jp/stat/shouhi/menu_shouhi.html",
    "hojin": ESRI + "/jp/stat/hojin/menu_hojin.html",
    "schedule": ESRI + "/jp/stat/stat-schedule.html",
}


class EsriDataError(Exception):
    """Raised for failed downloads, bad input, or unexpected site structure.

    Attributes:
        status: HTTP status code if available (403, 404, 429, 503 ...), else None.
        transient: True if a retry may succeed (429 / 5xx / network error).
        url: the URL involved, if any.
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 transient: bool = False, url: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.url = url


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_CACHE: Dict[str, bytes] = {}
_SESSION: Optional[requests.Session] = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        _SESSION = s
    return _SESSION


def clear_cache() -> None:
    """Drop the in-process page/file cache (e.g. right after a release at 8:50/14:00)."""
    _CACHE.clear()


def _check_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise EsriDataError("URL must be a non-empty string")
    url = url.strip()
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise EsriDataError(f"Not an absolute http(s) URL: {url!r}", url=url)
    if p.path == "" or p.path.endswith("/"):
        raise EsriDataError(
            f"Folder URL (ends in '/') is not allowed: {url}\n"
            "ESRI returns 403 for folder URLs. Use an .html page URL or a real "
            ".xlsx/.csv file URL (see source_url() / list_file_links()).",
            status=403, url=url)
    return url


def polite_get(url: str, sleep: float = DEFAULT_SLEEP, retries: int = 1,
               retry_wait: float = RETRY_WAIT, timeout: float = DEFAULT_TIMEOUT,
               use_cache: bool = True) -> bytes:
    """GET url → bytes, politely.

    - Rejects folder URLs ("/"-terminated) locally.
    - Sleeps `sleep` s after each real request (no bursts).
    - Retries `retries` times after `retry_wait` s on 429 / 5xx / network errors.
      Other 4xx (403, 404 ...) are raised immediately.
    - Cached per URL within the process unless use_cache=False.
    """
    url = _check_url(url)
    if use_cache and url in _CACHE:
        return _CACHE[url]
    for attempt in range(retries + 1):
        try:
            try:
                r = _session().get(url, timeout=timeout)
            except requests.RequestException as e:
                raise EsriDataError(f"Network error for {url}: {e}",
                                    transient=True, url=url) from None
            finally:
                time.sleep(sleep)
            if r.status_code >= 400:
                hint = ""
                if r.status_code == 404:
                    hint = (" (file names change every release — scrape the index page "
                            "instead of guessing)")
                raise EsriDataError(
                    f"HTTP {r.status_code} for {url}{hint}", status=r.status_code,
                    transient=(r.status_code == 429 or r.status_code >= 500), url=url)
            data = r.content
            if use_cache:
                _CACHE[url] = data
            return data
        except EsriDataError as e:
            if not e.transient or attempt == retries:
                raise
            time.sleep(retry_wait)
    raise EsriDataError("unreachable")  # pragma: no cover


def get_html(url: str, **kw) -> str:
    """Fetch an HTML page and decode it (ESRI pages are UTF-8; MOF pages are
    detected from the meta charset)."""
    raw = polite_get(url, **kw)
    m = re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', raw[:2000])
    enc = m.group(1).decode() if m else "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _abs(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, _html.unescape(href.strip()))


def _clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = re.sub(r"（(?:Excel|CSV|PDF)形式：[^）]*）", "", s)
    return " ".join(s.split())


def list_file_links(page_url: str,
                    exts: Sequence[str] = (".xlsx", ".xls", ".csv"),
                    pattern: Optional[str] = None) -> pd.DataFrame:
    """All links to data files on a page → DataFrame[url, file, text].

    exts: file extensions to keep; pattern: optional regex on the file name.
    """
    h = get_html(page_url)
    rows = []
    for m in re.finditer(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', h, re.S | re.I):
        href = m.group(1)
        path = href.split("#")[0].split("?")[0]
        if not path.lower().endswith(tuple(e.lower() for e in exts)):
            continue
        fname = path.rsplit("/", 1)[-1]
        if pattern and not re.search(pattern, fname):
            continue
        rows.append({"url": _abs(page_url, href), "file": fname,
                     "text": _clean_text(m.group(2))})
    df = pd.DataFrame(rows, columns=["url", "file", "text"])
    return df.drop_duplicates("url").reset_index(drop=True)


def source_url(kind: str, year: Optional[int] = None) -> str:
    """Recommended citation page (.html) for a dataset.

    kind: "qe", "kakuhou" (needs year, default = latest), "di", "juchu",
          "shouhi", "hojin", "schedule".
    """
    if kind not in SOURCE_PAGES:
        raise EsriDataError(f"Unknown kind {kind!r}; choose from {sorted(SOURCE_PAGES)}")
    if kind == "kakuhou":
        return kakuhou_top_url(year)
    return SOURCE_PAGES[kind]


def _read_csv_sjis(data: bytes) -> pd.DataFrame:
    text = data.decode("shift_jis", errors="replace")
    return pd.read_csv(io.StringIO(text), header=None, dtype=str)


def _to_num(s) -> pd.Series:
    s = pd.Series(s)
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False).str.strip(),
                         errors="coerce")


# ---------------------------------------------------------------------------
# 1. QE (四半期別GDP速報)
# ---------------------------------------------------------------------------

_QE_RE = re.compile(
    r'href="(/jp/sna/data/data_list/sokuhou/files/(\d{4})/qe(\d{2})(\d)(_2)?/tables/)'
    r'[a-z\-]+\d+\.csv"')


def get_latest_qe_info() -> Dict[str, object]:
    """Latest published QE, scraped from sna/menu.html.

    Returns dict(year, quarter, revision (1=1次, 2=2次), tables_url, menu_url, label).
    """
    h = get_html(SNA_MENU)
    m = _QE_RE.search(h)
    if not m:
        raise EsriDataError("QE CSVリンクが sna/menu.html に見つかりません。"
                            "ESRIサイト構造が変わった可能性があります。", url=SNA_MENU)
    year, q, rev = int(m.group(2)), int(m.group(4)), 2 if m.group(5) else 1
    tables = ESRI + m.group(1)
    return {
        "year": year, "quarter": q, "revision": rev, "tables_url": tables,
        "menu_url": tables.rsplit("tables/", 1)[0] + "gdemenuja.html",
        "label": f"{year}年{['1-3','4-6','7-9','10-12'][q-1]}月期 {rev}次速報",
    }


def get_latest_qe_tables_url() -> str:
    """URL of the latest QE tables/ folder (prefix for CSV files only —
    never fetch or cite it directly; folder URLs return 403)."""
    return str(get_latest_qe_info()["tables_url"])


def qe_csv_url(year: int, quarter: int, series: str, revision: int = 1) -> str:
    """Build a QE CSV URL. Folder: qe{YY}{Q} (1次) / qe{YY}{Q}_2 (2次);
    file: {series}{YY}{Q}{N}.csv. {YEAR} is the reference year (10-12月期 → same year)."""
    if quarter not in (1, 2, 3, 4):
        raise EsriDataError(f"quarter must be 1-4 (got {quarter})")
    if revision not in (1, 2):
        raise EsriDataError(f"revision must be 1 (1次) or 2 (2次) (got {revision})")
    if not re.fullmatch(r"[a-z]+-[a-z]+", series or ""):
        raise EsriDataError(f"Bad series name {series!r} (e.g. 'gaku-jk', 'ritu-jk', 'def-qg')")
    year = int(year)
    if not 1994 <= year <= 2100:
        raise EsriDataError(f"year out of range: {year}")
    yy = f"{year % 100:02d}"
    folder = f"qe{yy}{quarter}" + ("_2" if revision == 2 else "")
    return f"{QE_FILES}/{year}/{folder}/tables/{series}{yy}{quarter}{revision}.csv"


def get_qe_raw(year: Optional[int] = None, quarter: Optional[int] = None,
               series: str = "ritu-jk", revision: Optional[int] = None) -> pd.DataFrame:
    """QE CSV as a raw DataFrame (header=None, all cells str).

    year/quarter omitted → latest published QE (recommended: seasonally adjusted
    series are re-estimated back to 1994 at every release).
    revision: 1=1次, 2=2次. Default: latest's revision if year omitted, else 1.

    series: "ritu-jk" 実質前期比, "ritu-mk" 名目前期比, "nritu-jk" 実質前期比年率,
            "gaku-jk"/"gaku-mk" 実質/名目 季調実額, "gaku-jg"/"gaku-mg" 原系列,
            "gaku-jcy"/"gaku-jfy" 暦年/年度, "kiyo-jk" 寄与度, "def-qg"/"def-qk"
            デフレーター(原/季調), "def-cy"/"def-fy" ... (see SKILL.md).
    """
    if year is None or quarter is None:
        if (year is None) != (quarter is None):
            raise EsriDataError("Give both year and quarter, or neither (= latest)")
        info = get_latest_qe_info()
        year, quarter = int(info["year"]), int(info["quarter"])
        if revision is None:
            revision = int(info["revision"])
    if revision is None:
        revision = 1
    url = qe_csv_url(year, quarter, series, revision)
    df = _read_csv_sjis(polite_get(url))
    df.attrs["url"] = url
    df.attrs["series"] = series
    return df


def qe_header(df: pd.DataFrame) -> Dict[int, str]:
    """{column: "日本語ラベル / English label"} from header rows 2–6 of a QE raw df.

    Multi-row Japanese headers are joined with '・' (e.g. '財貨・サービス・輸出').
    """
    out: Dict[int, str] = {}
    head = df.iloc[:7]
    for c in range(1, df.shape[1]):
        cells = [str(v).strip() for v in head[c].tolist()
                 if isinstance(v, str) and v.strip() and v.strip() not in ("<参考>", "<cf>")]
        if not cells:
            continue
        jp = [s for s in cells if re.search(r"[぀-ヿ一-鿿]", s)
              and not s.startswith("(")]
        en = [s for s in cells if not re.search(r"[぀-ヿ一-鿿]", s)
              and not s.startswith("(")]
        lab = "・".join(jp) + (" / " + " ".join(en) if en else "")
        if lab.strip(" /"):
            out[c] = lab
    return out


def _qe_col(df: pd.DataFrame, col: Union[int, str]) -> int:
    if isinstance(col, (int,)) and not isinstance(col, bool):
        if not 1 <= col < df.shape[1]:
            raise EsriDataError(f"column {col} out of range (1..{df.shape[1]-1})")
        return col
    if isinstance(col, str):
        hdr = qe_header(df)
        key = col.strip().lower()
        exact, partial = [], []
        for c, lab in hdr.items():
            jp, _, en = lab.partition(" / ")
            names = {jp.lower(), jp.split("・")[-1].lower(), en.strip().lower()}
            if key in names:
                exact.append(c)
            elif key in lab.lower():
                partial.append(c)
        hits = exact or partial
        if not hits:
            raise EsriDataError(f"No QE column matches {col!r}. Available: {hdr}")
        return hits[0]
    raise EsriDataError(f"col must be int or str (got {type(col).__name__})")


_Q_MAP = {"1- 3": 1, "4- 6": 2, "7- 9": 3, "10-12": 4}


def parse_qe_column(df: pd.DataFrame, col: Union[int, str]) -> pd.DataFrame:
    """One column of a QE raw df → DataFrame[year, quarter, value, date].

    col: column number, or a label ('輸出', '国内需要', 'GDP', ...) looked up via
         qe_header() — safer, because columns ≥14 differ between ritu/gaku/def.
    Quarterly rows ('1994/ 1- 3.', ' 4- 6.' ...): year is carried forward.
    Annual rows ('1994/1-12.' 暦年, '1994/4-3.' 年度): quarter = NaN,
    date = Jan 1 (暦年) or Apr 1 (年度) of that year.
    Footnote rows and '***' cells are skipped / NaN.
    """
    c = _qe_col(df, col)
    years, quarters, values, dates = [], [], [], []
    cur = None
    for _, row in df.iterrows():
        d = str(row[0]).strip()
        m = re.match(r"^(\d{4})/", d)
        if m:
            cur = int(m.group(1))
        ann = re.match(r"^(\d{4})/\s*(1-12|4-3)\.", d)
        if ann:
            y = int(ann.group(1))
            years.append(y); quarters.append(None)
            dates.append(pd.Timestamp(y, 1 if ann.group(2) == "1-12" else 4, 1))
        else:
            q = next((v for k, v in _Q_MAP.items() if k in d), None)
            if not (q and cur):
                continue
            years.append(cur); quarters.append(q)
            dates.append(pd.Timestamp(cur, 3 * q - 2, 1))
        values.append(row[c])
    out = pd.DataFrame({"year": years, "quarter": pd.array(quarters, dtype="Int64"),
                        "value": _to_num(values).values, "date": dates})
    return out


# Columns 1–13 are common to ritu / gaku / def / kiyo files.
QE_MAIN_COLS = {
    "GDP": 1, "PrivateConsumption": 2, "HouseholdConsumption": 3,
    "HouseholdConsumptionExImputedRent": 4, "PrivateResidential": 5,
    "PrivateCapex": 6, "PrivateInventories": 7, "GovConsumption": 8,
    "PublicInvestment": 9, "PublicInventories": 10, "NetExports": 11,
    "Exports": 12, "Imports": 13,
}


def get_qe_df(series: str = "gaku-jk",
              cols: Optional[Dict[str, Union[int, str]]] = None,
              year: Optional[int] = None, quarter: Optional[int] = None,
              revision: Optional[int] = None) -> pd.DataFrame:
    """Tidy wide QE table: date, year, quarter + one column per item.

    cols: {output_name: column number or label}. Default = QE_MAIN_COLS (cols 1–13).
    Extra examples: {"DomesticDemand": "国内需要", "GDI": "国内総所得"}.
    """
    raw = get_qe_raw(year, quarter, series, revision)
    cols = cols or QE_MAIN_COLS
    base = None
    for name, c in cols.items():
        s = parse_qe_column(raw, c)
        if base is None:
            base = s[["date", "year", "quarter"]].copy()
        base[name] = s["value"].values
    base.attrs["url"] = raw.attrs.get("url")
    return base


# ---------------------------------------------------------------------------
# 2. 景気動向指数（CI/DI）/ 4. 消費者態度指数
# ---------------------------------------------------------------------------

def get_latest_ci_mmdd() -> str:
    """Latest {MMDD} of the CI Excel files, scraped from di.html."""
    h = get_html(DI_PAGE)
    links = re.findall(r'(\d{4})ci\.xlsx', h)
    if not links:
        raise EsriDataError("CI Excelリンクが di.html に見つかりません。"
                            "サイト構造が変わった可能性があります。", url=DI_PAGE)
    return links[0]


def _ci_url(suffix: str = "") -> str:
    return f"{DI_BASE}{get_latest_ci_mmdd()}ci{suffix}.xlsx"


def _ym_tidy(df: pd.DataFrame, start_row: int, colmap: Dict[str, int]) -> pd.DataFrame:
    data = df.iloc[start_row:].copy()
    out = pd.DataFrame({"year": pd.to_numeric(data[1], errors="coerce"),
                        "month": pd.to_numeric(data[2], errors="coerce")})
    for name, c in colmap.items():
        out[name] = pd.to_numeric(data[c], errors="coerce")
    out = out.dropna(subset=["year", "month"])
    out = out[(out["month"] >= 1) & (out["month"] <= 12)]
    out["year"] = out["year"].astype(int)
    out["month"] = out["month"].astype(int)
    out.insert(0, "date", pd.to_datetime(dict(year=out["year"], month=out["month"], day=1)))
    return out.reset_index(drop=True)


def get_ci_df(include_di: bool = False, include_reference: bool = False) -> pd.DataFrame:
    """景気動向指数 → DataFrame[date, leading, coincident, lagging] (CI, 2020=100).

    ci.xlsx sheet '指数 Indexes': col1=年, col2=月, col3-5=CI 先行/一致/遅行,
    col6-8=(参考)外れ値処理なしCI, col9-11=DI, col12-14=DI累積。データは行6から。
    include_reference → *_noadj columns; include_di → di_* and di_cum_* columns.
    CI starts 1985-01 (DI from 1980-01); rows without CI are NaN.
    """
    url = _ci_url()
    df = pd.read_excel(io.BytesIO(polite_get(url)), sheet_name=0, header=None)
    cm = {"leading": 3, "coincident": 4, "lagging": 5}
    if include_reference:
        cm.update({"leading_noadj": 6, "coincident_noadj": 7, "lagging_noadj": 8})
    if include_di:
        cm.update({"di_leading": 9, "di_coincident": 10, "di_lagging": 11,
                   "di_cum_leading": 12, "di_cum_coincident": 13, "di_cum_lagging": 14})
    out = _ym_tidy(df, 6, cm)
    if not include_di:
        out = out.dropna(subset=["leading", "coincident", "lagging"], how="all")
        out = out.reset_index(drop=True)
    out = out.drop(columns=["year", "month"])
    out.attrs["url"] = url
    return out


def get_ci_component(component: int) -> Tuple[pd.DataFrame, str]:
    """個別系列ファイル ci{1,2,3}.xlsx as raw DataFrame (header=None).

    component: 1=先行, 2=一致, 3=遅行. Returns (df, mmdd).
    Rows 0–4 = header (row0 code 'L1'.., row1 日本語名, row2 English), data from row 5.
    """
    if component not in (1, 2, 3):
        raise EsriDataError(f"component must be 1 (先行), 2 (一致) or 3 (遅行) (got {component})")
    mmdd = get_latest_ci_mmdd()
    url = f"{DI_BASE}{mmdd}ci{component}.xlsx"
    df = pd.read_excel(io.BytesIO(polite_get(url)), header=None)
    df.attrs["url"] = url
    return df, mmdd


def get_ci_component_df(component: int, labels: str = "code") -> pd.DataFrame:
    """個別系列 → tidy DataFrame[date, year, month, <series>...].

    labels: "code" → 'L6', 'C10', 'Lg8'; "jp" → 'L6 消費者態度指数'.
    Column codes are read from the file header, so revisions of the
    indicator set are picked up automatically.
    """
    raw, _ = get_ci_component(component)
    cm = {}
    for c in range(3, raw.shape[1]):
        code = re.sub(r"\(注\)|（注）", "", str(raw.iat[0, c])).strip()
        if not code or code == "nan":
            continue
        name = str(raw.iat[1, c]).strip()
        cm[code if labels == "code" else f"{code} {name}"] = c
    out = _ym_tidy(raw, 5, cm)
    out.attrs["url"] = raw.attrs.get("url")
    return out


def get_consumer_sentiment() -> pd.DataFrame:
    """消費者態度指数（CSI）月次 → DataFrame[date, year, month, CSI] (1975〜).

    Source: CI先行系列 ci1.xlsx の L6 列（列位置はヘッダから自動検出、通常 col 8）。
    Before 2001-11 the source is quarterly (values only in 3,6,9,12月 → others NaN
    or interpolated per the file note).
    """
    raw, _ = get_ci_component(1)
    col = None
    for c in range(3, raw.shape[1]):
        if str(raw.iat[0, c]).startswith("L6") or "消費者態度指数" in str(raw.iat[1, c]):
            col = c
            break
    if col is None:
        raise EsriDataError("消費者態度指数(L6)の列が ci1.xlsx に見つかりません", url=raw.attrs.get("url"))
    out = _ym_tidy(raw, 5, {"CSI": col})
    out.attrs["url"] = raw.attrs.get("url")
    return out


# ---------------------------------------------------------------------------
# 3. 機械受注
# ---------------------------------------------------------------------------

def get_juchu_chouki(n: Union[int, str] = 1) -> Tuple[pd.ExcelFile, str]:
    """機械受注 長期系列Excel {YYMM}chouki-{n}.xlsx (linked from juchu.html).

    n: 1=主要需要者別, 2=機種別, "2p"=民需機種別, 3=製造業業種別, 4=非製造業業種別,
       5=官公需業種別, 6=見通し達成率, "uchiwakegoukei"=内訳合計の船舶・電力を除く民需.
    Sheets: ['表紙','季調・月次','原系列・月次','季調・四半期','原系列・四半期','年度・暦年'].
    Returns (ExcelFile, url).
    """
    n = str(n)
    if not re.fullmatch(r"[0-9a-z]+", n):
        raise EsriDataError(f"Bad chouki id {n!r}")
    page = JUCHU_BASE + "juchu.html"
    h = get_html(page)
    m = re.search(rf'href="((?:\d{{4}}/)?\d{{4}}chouki-{re.escape(n)}\.xlsx)"', h)
    if not m:
        avail = sorted(set(re.findall(r'chouki-([0-9a-z]+)\.xlsx', h)))
        raise EsriDataError(f"chouki-{n}.xlsx のリンクが juchu.html に見つかりません (available: {avail})",
                            url=page)
    url = _abs(page, m.group(1))
    return pd.ExcelFile(io.BytesIO(polite_get(url))), url


JUCHU_MAIN_COLS = {
    "total": 3, "total_ex_ships": 4, "overseas": 5, "government": 6,
    "private": 7, "private_ex_ships": 8, "core": 9,  # core = 民需(船舶・電力を除く)
    "manufacturing": 10, "nonmanufacturing": 11, "nonmanufacturing_ex_ships": 12,
    "nonmanufacturing_ex_volatile": 13, "agencies": 14, "domestic": 15,
}


def get_juchu_df(sheet: str = "季調・月次") -> pd.DataFrame:
    """chouki-1.xlsx（主要需要者別）→ tidy DataFrame (単位: 百万円, 2005年4月〜).

    sheet: '季調・月次' (default) or '原系列・月次'. Columns = JUCHU_MAIN_COLS keys;
    'core' = 船舶・電力を除く民需（コア機械受注）. Header text of the core column is
    verified before returning (raises EsriDataError if the layout moved).
    """
    if sheet not in ("季調・月次", "原系列・月次"):
        raise EsriDataError("sheet must be '季調・月次' or '原系列・月次' "
                            "(for quarterly/annual sheets read get_juchu_chouki() directly)")
    x, url = get_juchu_chouki(1)
    raw = pd.read_excel(x, sheet_name=sheet, header=None)
    hdr = "".join(str(v) for v in raw.iloc[3:9, JUCHU_MAIN_COLS["core"]].tolist())
    if "電力" not in hdr or "民　　　需" not in "".join(str(v) for v in raw.iloc[3:6, 7]):
        raise EsriDataError(f"chouki-1 '{sheet}' の列構成が想定と異なります（ヘッダを確認）", url=url)
    out = _ym_tidy(raw, 9, JUCHU_MAIN_COLS)
    out.attrs["url"] = url
    return out


def get_juchu_monthly_csv(k: int = 1) -> Tuple[pd.DataFrame, str]:
    """Latest monthly release table {YYMM}juchu-{k}.csv (k=1..9) as raw df (header=None).

    The latest release page /jp/stat/juchu/{YYYY}/{YYMM}juchu.html is found from
    menu_juchu.html. Returns (df, url). Check header rows — layouts differ by table.
    """
    menu = JUCHU_BASE + "menu_juchu.html"
    h = get_html(menu)
    m = re.search(r'href="(/jp/stat/juchu/\d{4}/\d{4}juchu\.html)"', h)
    if not m:
        raise EsriDataError("最新月の機械受注ページが menu_juchu.html に見つかりません", url=menu)
    page = _abs(menu, m.group(1))
    links = list_file_links(page, exts=(".csv",), pattern=rf"juchu-{int(k)}\.csv$")
    if links.empty:
        raise EsriDataError(f"juchu-{k}.csv が {page} に見つかりません", url=page)
    url = links.iloc[0]["url"]
    df = _read_csv_sjis(polite_get(url))
    df.attrs["url"] = url
    return df, url


# ---------------------------------------------------------------------------
# 5. 法人企業景気予測調査（財務省サイト）
# ---------------------------------------------------------------------------

def get_bos_timeseries(which: int = 2) -> pd.ExcelFile:
    """法人企業景気予測調査 時系列Excel（財務省サイト、固定URL）.

    which=2 (default): jikeiretsu2.xlsx — 2004年4-6月期〜最新, 4 sheets
        参考-1 貴社の景況判断BSI / 参考-2 国内の景況判断BSI / 参考-3 設備判断BSI /
        参考-4 従業員数判断BSI.  Use get_bos_bsi() for a tidy frame.
    which=1: jikeiretsu.xlsx — ARCHIVE ONLY (平成16年4-6月期〜平成31年1-3月期,
        15 BSI tables stacked in one sheet, years in 平成 2-digit). Not updated.
    """
    if which not in (1, 2):
        raise EsriDataError(f"which must be 1 or 2 (got {which})")
    url = BOS_BASE + ("jikeiretsu2.xlsx" if which == 2 else "jikeiretsu.xlsx")
    x = pd.ExcelFile(io.BytesIO(polite_get(url)))
    return x


_BOS_SHEETS = {1: "貴社の景況判断BSI", 2: "国内の景況判断BSI", 3: "設備判断BSI", 4: "従業員数判断BSI"}


def get_bos_bsi(sheet: int = 1) -> pd.DataFrame:
    """jikeiretsu2.xlsx の参考-{sheet} → tidy DataFrame.

    sheet: 1=貴社の景況判断BSI, 2=国内の景況判断BSI, 3=設備判断BSI(期末), 4=従業員数判断BSI(期末).
    Columns: date, year, period + MultiIndex-like names '{規模}_{業種}_{当期|翌期|翌々期}'
    e.g. '大企業_全産業_当期'. BSI in %pt. date = first month of the quarter.
    """
    if sheet not in _BOS_SHEETS:
        raise EsriDataError(f"sheet must be 1-4 (got {sheet})")
    x = get_bos_timeseries(2)
    raw = pd.read_excel(x, sheet_name=x.sheet_names[sheet - 1], header=None)
    size = raw.iloc[3].ffill()
    ind = raw.iloc[4].ffill()
    hor = raw.iloc[5]
    cm = {}
    for c in range(2, raw.shape[1]):
        if pd.isna(hor[c]):
            continue
        cm[f"{str(size[c]).strip()}_{str(ind[c]).strip()}_{str(hor[c]).strip()}"] = c
    data = raw.iloc[6:]
    yr = pd.to_numeric(data[0], errors="coerce")
    per = data[1].astype(str).str.strip()
    ok = yr.notna()
    out = pd.DataFrame({"year": yr[ok].astype(int), "period": per[ok]})

    def _month(p: str) -> int:
        m = re.match(r"(\d+)", p.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        if not m:
            return 1
        v = int(m.group(1))
        return v - 2 if "末" in p else v  # '6月末' → quarter starting in April
    out.insert(0, "date", pd.to_datetime(dict(year=out["year"],
                                              month=out["period"].map(_month), day=1)))
    for k, c in cm.items():
        out[k] = pd.to_numeric(data.loc[ok, c], errors="coerce").values
    out = out.reset_index(drop=True)
    out.attrs["url"] = BOS_BASE + "jikeiretsu2.xlsx"
    out.attrs["title"] = _BOS_SHEETS[sheet]
    return out


# ---------------------------------------------------------------------------
# 6. 国民経済計算 年次推計（確報）
# ---------------------------------------------------------------------------

def get_latest_kakuhou_year() -> int:
    """Latest 年次推計 year, scraped from sna/menu.html ({YEAR}_kaku_top.html link)."""
    h = get_html(SNA_MENU)
    ys = [int(y) for y in re.findall(r"/kakuhou/files/(\d{4})/\1_kaku_top\.html", h)]
    if not ys:
        raise EsriDataError("確報トップページへのリンクが sna/menu.html に見つかりません", url=SNA_MENU)
    return max(ys)


def kakuhou_top_url(year: Optional[int] = None) -> str:
    """https://www.esri.cao.go.jp/jp/sna/data/data_list/kakuhou/files/{YEAR}/{YEAR}_kaku_top.html
    (this page, not the tables/ folder, is the URL to cite)."""
    y = int(year) if year is not None else get_latest_kakuhou_year()
    if not 2000 <= y <= 2100:
        raise EsriDataError(f"year out of range: {y}")
    return f"{KAKUHOU_FILES}/{y}/{y}_kaku_top.html"


def list_kakuhou_tables(year: Optional[int] = None) -> pd.DataFrame:
    """All Excel tables of a 年次推計 → DataFrame[code, file, url, text].

    code = file name without year prefix / '_jp.xlsx' (e.g. 'ffm1n', 'i5', 's18', 'ss1').
    """
    page = kakuhou_top_url(year)
    df = list_file_links(page, exts=(".xlsx", ".xls"))
    df = df[df["url"].str.contains("/tables/")].copy()
    df.insert(0, "code", df["file"].str.replace(r"^\d{4}", "", regex=True)
              .str.replace(r"_jp\.xlsx?$", "", regex=True))
    return df.reset_index(drop=True)


def get_kakuhou_excel(code: str, year: Optional[int] = None) -> Tuple[pd.ExcelFile, str]:
    """One 年次推計 Excel by code ('ffm1n', 'i5', 's18' ...) or full file name.

    The URL is taken from the kaku_top page listing (not guessed). Returns (ExcelFile, url).
    """
    tables = list_kakuhou_tables(year)
    c = re.sub(r"^\d{4}", "", code.strip()).replace("_jp.xlsx", "")
    hit = tables[tables["code"] == c]
    if hit.empty:
        raise EsriDataError(f"確報の表コード {code!r} が見つかりません。"
                            f"list_kakuhou_tables() で確認: {tables['code'].tolist()[:20]}...")
    url = hit.iloc[0]["url"]
    return pd.ExcelFile(io.BytesIO(polite_get(url))), url


_ERA = {"昭和": 1925, "平成": 1988, "令和": 2018}
_ZEN = str.maketrans("０１２３４５６７８９－．", "0123456789-.")


def _era_year(s: str) -> Optional[int]:
    m = re.search(r"(昭和|平成|令和)(元|\d+)年", str(s).translate(_ZEN))
    if not m:
        return None
    n = 1 if m.group(2) == "元" else int(m.group(2))
    return _ERA[m.group(1)] + n


def _year_of_cell(v) -> Optional[int]:
    """'平成6年度（1994）' / '令和6年（2024）' / 1994 → int year, else None."""
    if isinstance(v, (int, float)) and not pd.isna(v) and 1900 <= v <= 2100:
        return int(v)
    y = _era_year(v)
    if y is not None:
        return y
    m = re.search(r"[（(](\d{4})[）)]", str(v).translate(_ZEN))
    return int(m.group(1)) if m else None


def _is_note(label) -> bool:
    s = str(label).strip().replace("　", "")
    return s.startswith(("（注）", "(注)", "注）", "（備考）"))


def _parse_kakuhou_year_sector(raw: pd.DataFrame) -> Optional[pd.DataFrame]:
    """2段ヘッダ（年 × 部門）の年次推計シート → columns=MultiIndex(year, sector).

    例: 付表6「一般政府の部門別勘定」は年度ごとに 中央政府/地方政府/社会保障基金/合計 の4列。
    年の行が飛び飛び（各ブロックの先頭列だけ）なので右方向に埋め、直下の部門行と組み合わせる。
    注記行（（注）…）以降は別表なので読まない。検出できなければ None。
    """
    ncol = raw.shape[1]
    norm = lambda v: re.sub(r"\s+", "", str(v).replace("　", ""))
    is_txt = lambda v: isinstance(v, str) and v.strip() != ""
    for r in range(min(15, len(raw))):
        ys = raw.iloc[r, 1:].map(_year_of_cell).dropna()
        if len(ys) < 3:
            continue
        year_list = [int(y) for y in ys.values]
        for r2 in range(r + 1, min(r + 4, len(raw))):
            sec = raw.iloc[r2, 1:]
            if sec.map(is_txt).sum() >= 2 * len(year_list):
                break
        else:
            continue
        # 年ラベルはブロック中央寄せのことがあるので、部門名の繰り返しでブロックを切る
        sec_cols = [c for c in range(1, ncol) if is_txt(raw.iloc[r2, c])]
        s0 = norm(raw.iloc[r2, sec_cols[0]])
        starts = [c for c in sec_cols if norm(raw.iloc[r2, c]) == s0]
        if len(starts) != len(year_list):
            continue
        cols, keys = [], []
        for k, st in enumerate(starts):
            end = starts[k + 1] if k + 1 < len(starts) else ncol
            for c in sec_cols:
                if st <= c < end:
                    cols.append(c)
                    keys.append((year_list[k], norm(raw.iloc[r2, c])))
        items, rows = [], []
        for i in range(r2 + 1, len(raw)):
            lab = raw.iloc[i, 0]
            if _is_note(lab):
                break
            if not isinstance(lab, str) or not lab.strip():
                continue
            nums = pd.to_numeric(raw.iloc[i, cols], errors="coerce")
            if nums.notna().sum() == 0:
                continue
            items.append(re.sub(r"\s+", "", lab.replace("　", " ")))
            rows.append(nums.values)
        out = pd.DataFrame(rows, index=items,
                           columns=pd.MultiIndex.from_tuples(keys, names=["year", "sector"]))
        out.index.name = "item"
        return out
    return None


def kakuhou_row(df: pd.DataFrame, pattern: str, nth: int = 0,
                regex: bool = False) -> Union[pd.Series, pd.DataFrame]:
    """項目名の部分一致で1行を取り出す（行番号を手で数えない）。

    - pattern は空白を除いた項目名に対して照合（'プライマリーバランス'、'純貸出' など）。
    - 同名項目が複数ある場合（'（１）利子' など）は nth で何番目かを指定。
    - 2段ヘッダ（年×部門）の表なら DataFrame（index=年, columns=部門）を返す。
      1段の表なら Series（index=年）を返す。
    例: kakuhou_row(get_kakuhou_table("s6"), "プライマリーバランス")["合計"]
    """
    labels = pd.Series(df.index.astype(str))
    hit = labels[labels.str.contains(pattern, regex=regex)]
    if len(hit) <= nth:
        raise EsriDataError(f"項目 {pattern!r} が見つかりません（{len(hit)}件）。候補: "
                            f"{labels.tolist()[:40]}")
    row = df.iloc[hit.index[nth]]
    if isinstance(df.columns, pd.MultiIndex):
        out = row.unstack(level=1)
        sectors = list(dict.fromkeys(df.columns.get_level_values(1)))
        out = out[sectors]
        out.name = row.name
        return out
    return row


def parse_kakuhou_sheet(raw: pd.DataFrame) -> pd.DataFrame:
    """Wide 年次推計 sheet (items in rows, periods in columns) → DataFrame.

    index = item label (whitespace stripped; duplicates kept),
    columns = int years (年度/暦年 sheets) or 'YYYYQn' strings (四半期 sheets),
    or MultiIndex(year, sector) for 2-level 年×部門 sheets (付表6 一般政府の部門別勘定 など).
    Header detection: a row of 4-digit years, or 元号年 + '１－３．' quarter rows,
    or a sparse 元号年 row + sector-label row underneath.
    Use kakuhou_row(df, '項目名') to pick a row by label.
    """
    ncol = raw.shape[1]
    hdr_row, labels = None, None
    for r in range(min(15, len(raw))):
        vals = pd.to_numeric(raw.iloc[r, 1:], errors="coerce")
        if (vals.between(1900, 2100)).sum() >= max(2, (ncol - 1) // 2):
            hdr_row, labels = r, [int(v) if pd.notna(v) else None for v in vals]
            break
    if hdr_row is None:
        qrow = None
        for r in range(min(15, len(raw))):
            cells = raw.iloc[r, 1:].astype(str).str.translate(_ZEN)
            if cells.str.match(r"^(1-3|4-6|7-9|10-12)\.?$").sum() >= 4:
                qrow = r
                break
        if qrow is None:
            two = _parse_kakuhou_year_sector(raw)
            if two is not None:
                return two
            raise EsriDataError("期間ヘッダ行を検出できません（シート構成を確認）")
        yrow = next((r for r in range(qrow) if raw.iloc[r, 1:].map(_era_year).notna().sum() >= 4), None)
        if yrow is None:
            raise EsriDataError("四半期シートの年ヘッダ（元号）を検出できません")
        years = raw.iloc[yrow, 1:].map(_era_year).ffill()
        qmap = {"1-3": 1, "4-6": 2, "7-9": 3, "10-12": 4}
        labels = []
        for y, q in zip(years, raw.iloc[qrow, 1:].astype(str).str.translate(_ZEN).str.rstrip(".")):
            labels.append(f"{int(y)}Q{qmap[q]}" if pd.notna(y) and q in qmap else None)
        hdr_row = qrow
    body = raw.iloc[hdr_row + 1:]
    keep_cols = [i + 1 for i, l in enumerate(labels) if l is not None]
    items, rows = [], []
    for _, row in body.iterrows():
        lab = row[0]
        if not isinstance(lab, str) or not lab.strip():
            continue
        nums = pd.to_numeric(row[keep_cols], errors="coerce")
        if nums.notna().sum() == 0:
            continue
        items.append(re.sub(r"\s+", "", lab.replace("　", " ")))
        rows.append(nums.values)
    out = pd.DataFrame(rows, index=items, columns=[labels[c - 1] for c in keep_cols])
    out.index.name = "item"
    return out


def get_kakuhou_table(code: str, sheet: Union[int, str] = 0,
                      year: Optional[int] = None) -> pd.DataFrame:
    """get_kakuhou_excel() + parse_kakuhou_sheet() for one sheet.

    e.g. get_kakuhou_table("i5", "年度（４）a").loc["（参考）貯蓄率"] → 家計貯蓄率(年度).
    Use `ExcelFile.sheet_names` from get_kakuhou_excel() to see sheet names.
    """
    x, url = get_kakuhou_excel(code, year)
    raw = pd.read_excel(x, sheet_name=sheet, header=None)
    out = parse_kakuhou_sheet(raw)
    out.attrs["url"] = url
    out.attrs["sheet"] = sheet if isinstance(sheet, str) else x.sheet_names[sheet]
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: Optional[Iterable[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="ESRI data quick check")
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("qe", help="latest QE table (tail)")
    q.add_argument("series", nargs="?", default="gaku-jk")
    q.add_argument("-n", type=int, default=6)
    sub.add_parser("ci", help="CI leading/coincident/lagging (tail)")
    sub.add_parser("csi", help="消費者態度指数 (tail)")
    j = sub.add_parser("juchu", help="machinery orders chouki sheets")
    j.add_argument("n", nargs="?", default="1")
    k = sub.add_parser("kakuhou", help="list 年次推計 tables")
    k.add_argument("--year", type=int)
    b = sub.add_parser("bos", help="BSI (jikeiretsu2) tail")
    b.add_argument("sheet", nargs="?", type=int, default=1)
    a = p.parse_args(argv)
    pd.set_option("display.width", 200, "display.max_columns", 20)
    try:
        if a.cmd == "qe":
            info = get_latest_qe_info()
            df = get_qe_df(a.series)
            print(info["label"], df.attrs["url"])
            print(df.tail(a.n).to_string(index=False))
        elif a.cmd == "ci":
            df = get_ci_df()
            print(df.attrs["url"]); print(df.tail(6).to_string(index=False))
        elif a.cmd == "csi":
            df = get_consumer_sentiment()
            print(df.attrs["url"]); print(df.tail(6).to_string(index=False))
        elif a.cmd == "juchu":
            x, url = get_juchu_chouki(a.n)
            print(url); print(x.sheet_names)
            if a.n == "1":
                df = get_juchu_df()
                print(df[["date", "total", "private", "core"]].tail(6).to_string(index=False))
        elif a.cmd == "kakuhou":
            print(kakuhou_top_url(a.year))
            print(list_kakuhou_tables(a.year)[["code", "text"]].to_string(index=False))
        elif a.cmd == "bos":
            df = get_bos_bsi(a.sheet)
            print(df.attrs["title"], df.attrs["url"])
            print(df.iloc[-4:, :6].to_string(index=False))
    except EsriDataError as e:
        print(f"エラー: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
