#!/usr/bin/env python3
"""
Results Desk
============
Each run:
  1. Lists new financial-result filings on NSE (main board), NSE SME (Emerge) and BSE.
  2. Reads the numbers straight from each filing's XBRL file (the machine-readable
     copy every listed company must file along with the PDF). No AI, no PDF guessing.
  3. Keeps only companies with market cap >= MIN_MCAP_CR (default 700 Cr).
  4. Builds a result card (Revenue, Other income, EBITDA, EBITDA margin, PBT,
     Exceptional items, PAT, with YoY and QoQ) and sends it to Telegram.
  5. Updates the website (docs/index.html).

Commands
  python results_bot.py                  normal run
  python results_bot.py --dry-run        everything except sending to Telegram
  python results_bot.py --sample         save a sample card to out/sample_card.png
  python results_bot.py --test-telegram  send the sample card to Telegram (checks your settings)

Settings come from environment variables or config.ini (see README.md).
Uses unofficial NSE/BSE web endpoints: for personal use; may break if the exchanges
change their websites. Not investment advice.
"""
from __future__ import annotations

import argparse
import calendar
import configparser
import csv
import html
import io
import json
import os
import re
import sys
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

try:  # Browser-like TLS fingerprint: helps get past NSE/BSE bot protection.
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:  # pragma: no cover - optional
    cffi_requests = None

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageFont = None

VERSION = "1.0"
IST = timezone(timedelta(hours=5, minutes=30))
HERE = Path(__file__).resolve().parent

NSE = "https://www.nseindia.com"
NSE_API = NSE + "/api"
NSE_ARCHIVE = "https://nsearchives.nseindia.com"
NSE_IF_REFERER = NSE + "/companies-listing/corporate-integrated-filing"
NSE_FR_REFERER = NSE + "/companies-listing/corporate-filings-financial-results"
BSE = "https://www.bseindia.com"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_RESULTS_RSS = BSE + "/Data/XML/FinancialResultsFeed.aspx"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

DEFAULTS = {
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "MIN_MCAP_CR": "700",
    "SOURCES": "nse,nse_sme,bse",
    "INSTANT_ALERTS": "on",
    "ALERT_MAX_AGE_MINUTES": "90",
    "CARD_MAX_AGE_HOURS": "4",
    "ACTIVE_HOURS": "08:00-23:00",
    "ACTIVE_DAYS": "Mon,Tue,Wed,Thu,Fri,Sat",
    "WEBSITE": "off",
    "SETTLE_MINUTES": "15",
    "LOOKBACK_DAYS": "1",
    "MAX_RUN_MINUTES": "12",
    "POLL_SECONDS": "40",
    "KEEP_DAYS": "100",
    "SITE_DIR": "docs",
    "STATE_DIR": "state",
    "OUT_DIR": "out",
    "SITE_TITLE": "Results Desk",
    "CARD_BRAND": "Results Desk",
    "CARD_FOOTER": "",
}


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def active_window(cfg: dict):
    """(start time, end time, set of weekday numbers) from ACTIVE_HOURS / ACTIVE_DAYS."""
    a, b = (x.strip() for x in cfg["ACTIVE_HOURS"].split("-"))
    start = datetime.strptime(a, "%H:%M").time()
    end = datetime.strptime(b, "%H:%M").time()
    days = {_DAYS.index(d.strip()[:3].lower()) for d in cfg["ACTIVE_DAYS"].split(",") if d.strip()[:3].lower() in _DAYS}
    return start, end, days or set(range(7))


def last_window_end(now: datetime, cfg: dict) -> datetime:
    """End of the previous active day's window (e.g. Saturday 11 PM when it's Monday morning)."""
    _, end, days = active_window(cfg)
    d = now.date() - timedelta(days=1)
    for _ in range(7):
        if d.weekday() in days:
            break
        d -= timedelta(days=1)
    return datetime.combine(d, end, tzinfo=IST)


def is_active(now: datetime, cfg: dict) -> bool:
    start, end, days = active_window(cfg)
    return now.weekday() in days and start <= now.time() <= end


def _on(v) -> bool:
    return str(v).strip().lower() in ("on", "yes", "true", "1")


def log(msg: str) -> None:
    print(f"[{datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    ini = HERE / "config.ini"
    if ini.exists():
        cp = configparser.ConfigParser()
        cp.optionxform = str  # keep key case
        cp.read(ini, encoding="utf-8")
        for sec in cp.sections():
            for k, v in cp.items(sec):
                if v.strip():
                    cfg[k.strip().upper()] = v.strip()
    for k in list(cfg):
        if os.environ.get(k, "").strip():
            cfg[k] = os.environ[k].strip()
    return cfg


# --------------------------------------------------------------------------- #
# Dates and periods
# --------------------------------------------------------------------------- #

def parse_date(v) -> date | None:
    if not v or not isinstance(v, str):
        return None
    v = v.strip()[:20]
    for f in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y", "%d %b %Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(v.title() if "b" in f.lower() else v, f).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", v)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    return None


def parse_ts(v) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    t = v.strip().replace("IST", "").strip()
    fmts = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d-%b-%Y")
    for cand in (t, re.sub(r"\s+\d{2}:\d{2}$", "", t)):
        for f in fmts:
            try:
                return datetime.strptime(cand.title() if "%b" in f else cand, f).replace(tzinfo=IST)
            except ValueError:
                continue
    return None


def pkey(s: date, e: date) -> str:
    return f"{s.isoformat()}_{e.isoformat()}"


def unkey(k: str) -> tuple[date, date]:
    a, b = k.split("_")
    return date.fromisoformat(a), date.fromisoformat(b)


def month_span(s: date, e: date) -> int:
    return (e.year - s.year) * 12 + e.month - s.month + 1


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def shift_period(s: date, e: date, months: int) -> tuple[date, date]:
    ns = add_months(s.replace(day=1), months)
    ne1 = add_months(e.replace(day=1), months)
    ne = ne1.replace(day=calendar.monthrange(ne1.year, ne1.month)[1])
    return ns, ne


def fy_of(d: date) -> int:
    return d.year + 1 if d.month >= 4 else d.year


def period_label(s: date, e: date) -> str:
    n = month_span(s, e)
    fy = f"FY{fy_of(e) % 100:02d}"
    if n == 3:
        q = {6: 1, 9: 2, 12: 3, 3: 4}.get(e.month)
        return f"Q{q} {fy}" if q else f"3M {e:%b%y}"
    if n == 6:
        h = {9: 1, 3: 2}.get(e.month)
        return f"H{h} {fy}" if h else f"6M {e:%b%y}"
    if n == 12 and e.month == 3:
        return fy
    if n == 9 and e.month == 12:
        return f"9M {fy}"
    return f"{n}M {e:%b%y}"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Http:
    """Polite HTTP client with one browser-like session per exchange."""

    GAP = {"nse": 1.1, "bse": 1.1, "plain": 0.3}

    def __init__(self):
        self.sessions: dict = {}
        self.last: dict = {}
        self.nse_seeded = False
        self.fails = {"nse": 0, "bse": 0}
        self.down: set = set()   # exchange APIs that stopped answering during this run

    def _session(self, kind):
        if kind not in self.sessions:
            s = None
            if cffi_requests is not None and kind != "plain":
                try:
                    s = cffi_requests.Session(impersonate="chrome")
                except Exception:
                    s = None
            if s is None:
                s = requests.Session()
                s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            self.sessions[kind] = s
        return self.sessions[kind]

    def _is_plain_requests(self, kind) -> bool:
        return isinstance(self._session(kind), requests.Session)

    def _pace(self, kind):
        wait = self.last.get(kind, 0) + self.GAP.get(kind, 0.3) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last[kind] = time.monotonic()

    def _headers(self, kind, referer, accept):
        h = {"Accept": accept}
        if kind == "nse":
            h["Referer"] = referer or NSE + "/"
        elif kind == "bse":
            h.update({"Origin": BSE, "Referer": referer or BSE + "/",
                      "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"})
            if self._is_plain_requests(kind):
                h.update({"sec-ch-ua": '"Chromium";v="141", "Not(A:Brand";v="24"',
                          "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'})
        return h

    def seed_nse(self):
        s = self._session("nse")
        for url in (NSE + "/", NSE_IF_REFERER):
            try:
                self._pace("nse")
                s.get(url, headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}, timeout=30)
            except Exception as e:
                log(f"  NSE warm-up failed: {e}")
        self.nse_seeded = True

    def get(self, url, kind="plain", params=None, referer=None,
            accept="application/json, text/plain, */*", timeout=20, tries=2, quiet=False):
        api = kind in ("nse", "bse") and ("/api" in url or "BseIndiaAPI" in url)
        if api and kind in self.down:
            return None  # stopped answering earlier in this run: don't waste time
        if kind == "nse" and api:
            timeout = max(timeout, 40)  # NSE often takes ~20 s per request from cloud servers
        if kind == "nse" and not self.nse_seeded:
            self.seed_nse()
        s = self._session(kind)
        last_err = None
        for attempt in range(tries):
            self._pace(kind)
            try:
                r = s.get(url, params=params, headers=self._headers(kind, referer, accept), timeout=timeout)
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:90]}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                if api:
                    self.fails[kind] = 0
                return r
            last_err = f"HTTP {r.status_code}"
            if r.status_code == 404:
                break
            if r.status_code in (401, 403) and kind == "nse":
                self.seed_nse()
            time.sleep(2 * (attempt + 1))
        if api and last_err != "HTTP 404":
            self.fails[kind] += 1
            if self.fails[kind] >= 3 and kind not in self.down:
                self.down.add(kind)
                log(f"  ! {kind.upper()} stopped answering - skipping its lookups for the rest of this run")
        if not quiet:
            log(f"  ! {last_err} for {url.split('?')[0]}")
        return None

    def get_json(self, url, kind="plain", **kw):
        r = self.get(url, kind, **kw)
        if r is None:
            return None
        try:
            return json.loads(r.content.decode("utf-8-sig"))
        except Exception:
            log(f"  ! response was not JSON: {url}")
            return None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

class Filing:
    __slots__ = ("source", "index", "uid", "symbol", "scrip", "company", "isin", "period_end",
                 "consolidated", "xbrl_url", "view_url", "filed_at", "revised")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__slots__}
        d["period_end"] = self.period_end.isoformat() if self.period_end else None
        d["filed_at"] = self.filed_at.isoformat() if self.filed_at else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Filing":
        d = dict(d)
        d["period_end"] = date.fromisoformat(d["period_end"]) if d.get("period_end") else None
        d["filed_at"] = datetime.fromisoformat(d["filed_at"]) if d.get("filed_at") else None
        return cls(**{k: d.get(k) for k in cls.__slots__})

    @property
    def company_id(self) -> str:
        return self.isin or (f"NSE:{self.symbol}" if self.symbol else f"BSE:{self.scrip}")

    @property
    def exchange_key(self) -> str:
        return self.symbol or self.scrip or self.company


def _clean_url(v) -> str | None:
    if not isinstance(v, str):
        return None
    t = v.strip()
    if not t or t.upper() in {"NA", "N/A", "NULL", "-"}:
        return None
    if t.startswith("/"):
        t = NSE + t
    if not t.startswith("http"):
        return None
    return None if t.rstrip("/").rsplit("/", 1)[-1] in {"-", ""} else t


def _consolidated_flag(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if not isinstance(v, str) or not v.strip():
        return None
    t = v.strip().lower()
    if t.startswith(("non", "stand")):
        return False
    if "consol" in t:
        return True
    return None


def _nse_row(row: dict, index: str, kind: str) -> Filing | None:
    blob = " ".join(str(v) for v in row.values() if isinstance(v, str)).lower()
    if "governance" in blob and "financ" not in blob:
        return None
    xbrl = _clean_url(row.get("xbrl"))
    ixbrl = _clean_url(row.get("ixbrl"))
    seq = str(row.get("seq_Id") or row.get("seqNumber") or row.get("filingId") or row.get("seq_id") or "").strip()
    sym = str(row.get("symbol") or row.get("sm_symbol") or "").strip().upper()
    if not sym:
        return None
    uid = f"NSE-{index}-{kind}-{seq or xbrl or ''}"
    return Filing(
        source="NSE SME" if index == "sme" else "NSE", index=index, uid=uid, symbol=sym, scrip="",
        company=str(row.get("smName") or row.get("companyName") or row.get("company")
                    or row.get("sm_name") or sym).strip(),
        isin=(str(row.get("isin") or row.get("sm_isin") or "").strip().upper() or None),
        period_end=parse_date(row.get("qe_Date") or row.get("toDate") or row.get("to") or row.get("periodTo")),
        consolidated=_consolidated_flag(row.get("consolidated") or row.get("consol")),
        xbrl_url=xbrl, view_url=ixbrl or xbrl,
        filed_at=parse_ts(row.get("creation_Date") or row.get("broadCastDate") or row.get("filingDate")
                          or row.get("filingDateTs") or row.get("exchdisstime")),
        revised="revis" in str(row.get("type_Sub") or "").lower(),
    )


def _rows(j) -> list:
    if isinstance(j, list):
        return [r for r in j if isinstance(r, dict)]
    if isinstance(j, dict) and isinstance(j.get("data"), list):
        return [r for r in j["data"] if isinstance(r, dict)]
    return []


def discover_nse(http: Http, index: str, d_from: date, d_to: date) -> tuple[list[Filing], bool]:
    """Results filed between d_from and d_to (inclusive). Returns (filings, ok)."""
    out: list[Filing] = []
    ok_any = False
    fmt = "%d-%m-%Y"
    # 1) Integrated Filing - Financials (the format used since 2025), 50 per page.
    page, got, total = 1, 0, None
    while page <= 60:
        j = http.get_json(f"{NSE_API}/integrated-filing-results", "nse", referer=NSE_IF_REFERER,
                          params={"index": index, "from_date": d_from.strftime(fmt),
                                  "to_date": d_to.strftime(fmt), "page": page, "size": 50})
        if j is None:
            break
        ok_any = True
        rows = _rows(j)
        if not rows:
            break
        got += len(rows)
        out += [f for f in (_nse_row(r, index, "IF") for r in rows) if f]
        try:
            total = int(j.get("totalCount")) if isinstance(j, dict) else None
        except (TypeError, ValueError):
            total = None
        if not isinstance(j, dict) or total is None or got >= total or len(rows) < 50:
            break
        page += 1
    if total and got < total:
        log(f"  ! NSE {index}: got {got} of {total} listed filings (the rest come next run)")
    # 2) Older "Financial Results" format (still used by some filers).
    periods = ("Quarterly", "Half-Yearly") if index == "sme" else ("Quarterly",)
    for per in periods:
        j = http.get_json(f"{NSE_API}/corporates-financial-results", "nse", referer=NSE_FR_REFERER,
                          params={"index": index, "period": per, "from_date": d_from.strftime(fmt),
                                  "to_date": d_to.strftime(fmt)})
        if j is None:
            continue
        ok_any = True
        out += [f for f in (_nse_row(r, index, "FR") for r in _rows(j)) if f]
    return out, ok_any


def backfill_index(cfg: dict, d_from: date, d_to: date) -> int:
    """One-off: list NSE result filings made between two dates (one day at a time, main board and SME)
    and remember where each XBRL file is, so last year's figures can fill the YoY column."""
    http, st = Http(), State(HERE / cfg["STATE_DIR"])
    deadline = time.monotonic() + float(os.environ.get("BACKFILL_MINUTES", "52")) * 60
    total, day, done_to = 0, d_from, None
    while day <= d_to and time.monotonic() < deadline:
        for index in ("equities", "sme"):
            got, ok = discover_nse(http, index, day, day)
            if not ok:  # NSE hiccup: fresh session, one more try
                http = Http()
                got, ok = discover_nse(http, index, day, day)
            added = st.add_to_index(got, target=st.bindex)
            total += added
            if got or not ok:
                log(f"  {index:8} {day:%d %b %Y}: {len(got)} filings listed, {added} new"
                    + ("" if ok else " (NSE did not respond)"))
        st.save_backfill()
        done_to = day
        day += timedelta(days=1)
    log(f"Backfill {'finished' if day > d_to else 'paused'}: covered {d_from:%d %b %Y} to "
        f"{done_to:%d %b %Y} · {total} filings added · {len(st.bindex)} NSE symbols indexed"
        if done_to else "Backfill could not start (NSE did not respond)")
    if day <= d_to:
        log(f"Run the backfill again from {day.isoformat()} to {d_to.isoformat()} to finish.")
    return 0


def _rss_field(item: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", item, re.S | re.I)
    if not m:
        return ""
    t = m.group(1).strip()
    t = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", t, flags=re.S)
    return html.unescape(t).strip()


def discover_bse(http: Http) -> tuple[list[Filing], bool]:
    r = http.get(BSE_RESULTS_RSS, "bse", referer=BSE + "/corporates/Comp_Resultsnew.aspx",
                 accept="application/rss+xml, application/xml, text/xml, */*")
    if r is None:
        return [], False
    text = r.content.decode("utf-8-sig", errors="replace")
    out, seen = [], set()
    for item in re.findall(r"<item>(.*?)</item>", text, re.S | re.I):
        title, link, desc = _rss_field(item, "title"), _rss_field(item, "link"), _rss_field(item, "description")
        if not link:
            continue
        m = re.search(r"\((\d{6})\)\s*$", title)
        scrip = m.group(1) if m else ""
        xml_url = link[:-5] + ".xml" if link.lower().endswith(".html") else link
        if not xml_url.lower().endswith(".xml") or xml_url in seen:
            continue
        seen.add(xml_url)
        pe = re.search(r"PERIOD END DATE\s*:\s*([\d\-/A-Za-z]+)", desc, re.I)
        dl = desc.lower()
        cons = True if "consolidated" in dl and "standalone" not in dl else (False if "standalone" in dl else None)
        out.append(Filing(
            source="BSE", index="bse", uid="BSE-" + xml_url.rsplit("/", 1)[-1], symbol="", scrip=scrip,
            company=re.sub(r"\s*\(\d{6}\)\s*$", "", title).strip(), isin=None,
            period_end=parse_date(pe.group(1)) if pe else None, consolidated=cons,
            xbrl_url=xml_url, view_url=link, filed_at=None, revised=False))
    return out, True


_ANN_SKIP_DESC = ("intimation", "postpone", "newspaper", "investor presentation", "press release", "analyst",
                  "earnings call", "conference call", "integrated filing", "trading window", "record date")
_ANN_SKIP_TEXT = ("to consider", "will be held", "is scheduled", "shall be held", "intimation of board",
                  "trading window", "newspaper")


def is_result_announcement(row: dict) -> bool:
    """True for the 'results are out' PDF announcement (not the advance notice of the meeting)."""
    desc = str(row.get("desc") or "").lower()
    txt = str(row.get("attchmntText") or "").lower()
    if any(w in desc for w in _ANN_SKIP_DESC) or any(w in txt for w in _ANN_SKIP_TEXT):
        return False
    if "financial result" in desc:
        return True
    return "outcome of board meeting" in desc and any(
        w in txt for w in ("financial result", "unaudited result", "audited result"))


def discover_nse_announcements(http: Http, index: str, day: date, d_from: date | None = None) -> tuple[list[dict], bool]:
    j = http.get_json(f"{NSE_API}/corporate-announcements", "nse",
                      referer=NSE + "/companies-listing/corporate-filings-announcements",
                      params={"index": index, "from_date": (d_from or day).strftime("%d-%m-%Y"),
                              "to_date": day.strftime("%d-%m-%Y")})
    if j is None:
        return [], False
    out = []
    for r in _rows(j):
        if not is_result_announcement(r):
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        out.append({"uid": f"ANN-{index}-{r.get('seq_id') or r.get('an_dt')}-{sym}", "symbol": sym,
                    "isin": str(r.get("sm_isin") or "").strip().upper(),
                    "company": str(r.get("sm_name") or sym).strip(), "pdf": _clean_url(r.get("attchmntFile")),
                    "ts": parse_ts(r.get("an_dt") or r.get("exchdisstime")),
                    "source": "NSE SME" if index == "sme" else "NSE"})
    return out, True


def discover_bse_announcements(http: Http, day: date, d_from: date | None = None) -> tuple[list[dict], bool]:
    """BSE 'Result' category announcements for the day (the feed behind bseindia.com/corporates/ann)."""
    out, ok, got = [], False, 0
    for page in range(1, 21):
        j = http.get_json(f"{BSE_API}/AnnSubCategoryGetData/w", "bse", referer=BSE + "/corporates/ann",
                          params={"pageno": page, "strCat": "Result", "strPrevDate": (d_from or day).strftime("%Y%m%d"),
                                  "strScrip": "", "strSearch": "P", "strToDate": day.strftime("%Y%m%d"),
                                  "strType": "C", "subcategory": "-1"})
        if not isinstance(j, dict):
            break
        ok = True
        rows = [r for r in j.get("Table", []) if isinstance(r, dict)]
        if not rows:
            break
        got += len(rows)
        for r in rows:
            subject = f"{r.get('NEWSSUB') or ''} {r.get('HEADLINE') or ''}"
            low = subject.lower()
            if "result" not in low or any(w in low for w in _ANN_SKIP_DESC + _ANN_SKIP_TEXT):
                continue
            scrip = str(r.get("SCRIP_CD") or "").strip()
            if not scrip:
                continue
            att = str(r.get("ATTACHMENTNAME") or "").strip()
            out.append({"uid": f"BANN-{r.get('NEWSID')}", "symbol": "", "scrip": scrip, "isin": "",
                        "company": str(r.get("SLONGNAME") or scrip).strip(),
                        "pdf": f"{BSE}/xml-data/corpfiling/AttachLive/{att}" if att else None,
                        "ts": parse_ts(str(r.get("NEWS_DT") or r.get("DissemDT") or "")), "source": "BSE"})
        total = (j.get("Table1") or [{}])[0].get("ROWCNT") if isinstance(j.get("Table1"), list) else None
        if not isinstance(total, int) or got >= total:
            break
    return out, ok


def bse_file_time(url: str | None) -> datetime | None:
    """Filing time from a BSE XBRL file name (..._<scrip>_<d>dmmyyyyHHMMSS_IFIndAs.xml). The clock part is
    sometimes 12-hour, so treat the time as the latest it could be."""
    m = re.search(r"_\d{6}_(\d{1,2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})_", url or "")
    if not m:
        return None
    d, mo, y, hh, mi, ss = (int(x) for x in m.groups())
    try:
        t = datetime(y, mo, d, hh, mi, ss, tzinfo=IST)
    except ValueError:
        return None
    if hh < 12 and t + timedelta(hours=12) <= datetime.now(IST):
        t += timedelta(hours=12)
    return t


_NOT_SYMBOLS = {"NOTLISTED", "NOT LISTED", "NA", "N.A.", "N/A", "NIL", "NONE", "-", "0"}


def clean_symbol(v) -> str:
    t = str(v or "").strip().upper()
    if not t or t in _NOT_SYMBOLS or not re.fullmatch(r"[A-Z0-9&._\-]{1,20}", t):
        return ""
    return t


def norm_name(s: str) -> str:
    s = (s or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\b(LIMITED|LTD|THE)\b", " ", s)
    return re.sub(r"\s+", "", s)


# --------------------------------------------------------------------------- #
# XBRL parsing
# --------------------------------------------------------------------------- #

CANDIDATES = {
    "revenue": ["RevenueFromOperations", "RevenueFromOperationsNet", "TotalRevenueFromOperations"],
    "interest_earned": ["InterestEarned", "TotalInterestEarned"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income", "TotalIncome"],
    "total_expenses": ["Expenses", "TotalExpenses"],
    "finance": ["FinanceCosts", "FinanceCost"],
    "depreciation": ["DepreciationDepletionAndAmortisationExpense", "DepreciationAndAmortisationExpense"],
    "pbe": ["ProfitBeforeExceptionalItemsAndTax", "ProfitLossBeforeExceptionalItemsAndTax"],
    "exceptional": ["ExceptionalItemsBeforeTax", "ExceptionalItems"],
    "pbt": ["ProfitBeforeTax", "ProfitLossBeforeTax", "ProfitLossFromContinuingOperationsBeforeTax",
            "ProfitLossFromOrdinaryActivitiesBeforeTax"],
    "tax": ["TaxExpense", "TotalTaxExpense"],
    "pat": ["ProfitLossForPeriod", "ProfitLoss", "NetProfitLossForThePeriod", "ProfitLossForThePeriod",
            "ProfitLossForPeriodFromContinuingOperations"],
    "pat_owners": ["ProfitOrLossAttributableToOwnersOfParent"],
    "op_profit": ["OperatingProfitBeforeProvisionAndContingencies",
                  "OperatingProfitBeforeProvisionsAndContingencies"],
}
INFO_TAGS = ("Symbol", "ScripCode", "ISIN", "NameOfTheCompany", "NatureOfReportStandaloneConsolidated",
             "WhetherResultsAreAuditedOrUnaudited", "LevelOfRounding", "MSEISymbol")
PAIDUP_TAGS = ("PaidUpValueOfEquityShareCapital", "PaidUpEquityShareCapital")
FACE_TAGS = ("FaceValueOfEquityShareCapital", "FaceValueOfEquityShare")
ROUNDING = {"actual": 1, "thousands": 1e3, "lakhs": 1e5, "lakh": 1e5, "millions": 1e6,
            "crores": 1e7, "crore": 1e7, "billions": 1e9}
CRORE = 1e7


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.split(":")[-1]


def _num(t: str):
    try:
        return float(t.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def parse_xbrl(content: bytes) -> dict:
    """Return {'info', 'periods' {key: metrics in Cr}, 'current' key, 'shares', 'kind'}."""
    root = ET.fromstring(content)
    contexts = {}
    facts: dict[str, list] = defaultdict(list)
    nil = "{http://www.w3.org/2001/XMLSchema-instance}nil"
    for el in root.iter():
        ln = _local(el.tag)
        if ln == "context":
            s = e = inst = None
            dims = False
            for c in el.iter():
                cl = _local(c.tag)
                if cl == "startDate":
                    s = parse_date((c.text or "")[:10])
                elif cl == "endDate":
                    e = parse_date((c.text or "")[:10])
                elif cl == "instant":
                    inst = parse_date((c.text or "")[:10])
                elif cl in ("explicitMember", "typedMember"):
                    dims = True
            contexts[el.get("id")] = (s, e, inst, dims)
        elif el.get("contextRef") is not None and el.get(nil) != "true":
            facts[ln].append((el.get("contextRef"), (el.text or "").strip()))

    plain = {cid for cid, (_, _, _, d) in contexts.items() if not d}
    dur = {cid: (s, e) for cid, (s, e, _, d) in contexts.items() if s and e and not d}

    info = {}
    for t in INFO_TAGS:
        vals = [v for cid, v in facts.get(t, []) if v]
        pref = [v for cid, v in facts.get(t, []) if v and cid == "OneD"]
        if pref or vals:
            info[t] = (pref or vals)[0]

    periods: dict[tuple, dict] = {}
    for key, names in CANDIDATES.items():
        for nm in names:
            for cid, txt in facts.get(nm, []):
                if cid not in dur:
                    continue
                v = _num(txt)
                if v is None:
                    continue
                periods.setdefault(dur[cid], {}).setdefault(key, v)

    def first_plain(tags):
        for t in tags:
            for cid, txt in facts.get(t, []):
                if cid in plain:
                    v = _num(txt)
                    if v:
                        return v
        return None

    paidup, face = first_plain(PAIDUP_TAGS), first_plain(FACE_TAGS)

    # Values should be in rupees. Some filers type them "as presented" (e.g. in lakhs);
    # if every number is implausibly small, scale by the declared rounding level.
    allv = [abs(v) for pm in periods.values() for v in pm.values()]
    factor = ROUNDING.get(str(info.get("LevelOfRounding", "")).strip().lower(), 1)
    scale = factor if (allv and max(allv) < 1e5 and factor > 1) else 1

    out_periods = {}
    for (s, e), pm in periods.items():
        out_periods[pkey(s, e)] = {k: round(v * scale / CRORE, 4) for k, v in pm.items()}

    shares = None
    if paidup and face and face > 0:
        sh = paidup * scale / face
        if 1e4 < sh < 5e11:
            shares = sh

    # Current period: context "OneD" in NSE/BSE filings; otherwise the shortest
    # period that ends on the latest date.
    current = None
    if "OneD" in dur and pkey(*dur["OneD"]) in out_periods:
        current = pkey(*dur["OneD"])
    elif out_periods:
        latest = max(unkey(k)[1] for k in out_periods)
        cands = [k for k in out_periods if unkey(k)[1] == latest]
        current = min(cands, key=lambda k: month_span(*unkey(k)))

    kind = "standard"
    if current:
        cm = out_periods[current]
        if cm.get("revenue") is None and cm.get("interest_earned") is not None:
            kind = "bank"
    return {"info": info, "periods": out_periods, "current": current, "shares": shares, "kind": kind}


# --------------------------------------------------------------------------- #
# Numbers -> card rows
# --------------------------------------------------------------------------- #

def derive(m: dict | None, kind: str) -> dict:
    if not m:
        return {}
    rev = m.get("revenue")
    if kind == "bank" and rev is None:
        rev = m.get("interest_earned")
    oi = m.get("other_income")
    if oi is None and m.get("total_income") is not None and rev is not None:
        oi = m["total_income"] - rev
    exp, fin, dep, pbe = m.get("total_expenses"), m.get("finance"), m.get("depreciation"), m.get("pbe")
    if kind == "bank":
        ebitda = m.get("op_profit")
    elif rev is not None and exp is not None:
        ebitda = rev - (exp - (fin or 0) - (dep or 0))
    elif pbe is not None:
        ebitda = pbe + (fin or 0) + (dep or 0) - (oi or 0)
    else:
        ebitda = None
    pbt, exc = m.get("pbt"), m.get("exceptional")
    if pbt is not None and pbe is not None:
        exc = pbt - pbe  # + gain / - loss, as it actually moved PBT
    elif pbt is None and pbe is not None:
        pbt = pbe + (exc or 0)
    margin = (ebitda / rev * 100) if (rev and rev > 0 and ebitda is not None) else None
    return {"revenue": rev, "other_income": oi, "ebitda": ebitda, "margin": margin,
            "pbt": pbt, "exceptional": exc, "pat": m.get("pat")}


ROWS_STD = [("revenue", "Revenue", "amt"), ("other_income", "Other income", "amt"),
            ("ebitda", "EBITDA", "amt"), ("margin", "EBITDA margin %", "pct"),
            ("pbt", "PBT", "amt"), ("exceptional", "Exceptional items", "exc"), ("pat", "PAT", "amt")]
ROWS_BANK = [("revenue", "Interest earned", "amt"), ("other_income", "Other income", "amt"),
             ("ebitda", "Operating profit", "amt"), ("margin", "Op. profit margin %", "pct"),
             ("pbt", "PBT", "amt"), ("exceptional", "Exceptional items", "exc"), ("pat", "PAT", "amt")]


def indian(n: int) -> str:
    s = str(abs(int(n)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return s


def fmt_cr(v, signed=False) -> str:
    if v is None:
        return "–"
    a = abs(v)
    if a >= 100:
        body = indian(round(a))
    elif a >= 10:
        body = f"{a:.1f}"
    else:
        body = f"{a:.2f}"
    if float(body.replace(",", "")) == 0:
        return "0"
    sign = "−" if v < 0 else ("+" if signed and v > 0 else "")
    return sign + body


def growth(cur, base, kind) -> tuple[str, str, float | None]:
    """(text, direction up/down/flat/none, numeric % for sorting)."""
    if cur is None or base is None or kind == "exc":
        return "–", "none", None
    if kind == "pct":
        bps = (cur - base) * 100
        if abs(bps) < 0.5:
            return "0 bps", "flat", None
        return f"{'▲' if bps > 0 else '▼'} {abs(bps):.0f} bps", "up" if bps > 0 else "down", None
    if base == 0:
        return "–", "none", None
    if base > 0 and cur < 0:
        return "Turned −ve", "down", None
    if base < 0:
        if cur >= 0:
            return "Turned +ve", "up", None
        return ("Loss narrowed", "up", None) if cur > base else ("Loss widened", "down", None)
    g = (cur - base) / base * 100
    if abs(g) < 0.05:
        return "0.0%", "flat", 0.0
    if g >= 1000:
        return f"▲ {cur / base:.1f}x", "up", g
    return f"{'▲' if g > 0 else '▼'} {abs(g):.1f}%", "up" if g > 0 else "down", g


def get_period(cache: dict, s: date, e: date) -> dict | None:
    k = pkey(s, e)
    if k in cache:
        return cache[k]
    # Derive from two cumulative periods, e.g. Q4 = FY - 9M, or Q1 = H1 - Q2.
    prior_end = s - timedelta(days=1)
    next_start = e + timedelta(days=1)
    for k1, m1 in cache.items():
        s1, e1 = unkey(k1)
        if e1 == e and s1 < s:
            m2 = cache.get(pkey(s1, prior_end))
        elif s1 == s and e1 > e:
            m2 = cache.get(pkey(next_start, e1))
        else:
            continue
        if m2:
            return {f: round(m1[f] - m2[f], 4) for f in m1 if f in m2}
    return None


def build_result(f: Filing, parsed: dict, cache: dict, basis: str, mcap, price, price_date) -> dict:
    info, kind = parsed["info"], parsed["kind"]
    cs, ce = unkey(parsed["current"])
    span = month_span(cs, ce)
    prev_p = shift_period(cs, ce, -span) if span < 12 else None
    yoy_p = shift_period(cs, ce, -12)
    cur = derive(cache.get(parsed["current"]) or parsed["periods"][parsed["current"]], kind)
    prev = derive(get_period(cache, *prev_p), kind) if prev_p else {}
    yoy = derive(get_period(cache, *yoy_p), kind)
    seq_name = {3: "QoQ", 6: "HoH"}.get(span, "vs prev")
    rows, rev_yoy, pat_yoy = [], None, None
    for key, label, k in (ROWS_BANK if kind == "bank" else ROWS_STD):
        c, p, y = cur.get(key), prev.get(key), yoy.get(key)
        if k == "pct":
            vals = [("–" if v is None else f"{v:.1f}") for v in (c, p, y)]
        else:
            vals = [fmt_cr(v, signed=(k == "exc")) for v in (c, p, y)]
        gy, gyd, gyn = growth(c, y, k)
        gq, gqd, _ = growth(c, p, k) if prev_p else ("–", "none", None)
        if key == "revenue":
            rev_yoy = gyn
        if key == "pat":
            pat_yoy = gyn
        rows.append([label, *vals, gy, gyd, gq, gqd])
    name = (info.get("NameOfTheCompany") or f.company or "").strip()
    sym = clean_symbol(info.get("Symbol")) or clean_symbol(f.symbol)
    scrip = (str(info.get("ScripCode") or f.scrip or "")).strip()
    if scrip and not scrip.isdigit():
        scrip = ""
    isin = (info.get("ISIN") or f.isin or "").strip().upper()
    filed = f.filed_at or datetime.now(IST)
    aud = str(info.get("WhetherResultsAreAuditedOrUnaudited") or "").strip().title()
    return {
        "id": f"{isin or f.company_id}|{ce.isoformat()}|{basis}",
        "name": name, "sym": sym, "scrip": scrip, "isin": isin,
        "ex": f.source, "basis": "Consolidated" if basis == "C" else "Standalone", "aud": aud,
        "per": period_label(cs, ce), "prevLbl": period_label(*prev_p) if prev_p else "",
        "yoyLbl": period_label(*yoy_p), "seq": seq_name, "kind": kind,
        "pend": ce.isoformat(), "filed": filed.isoformat(timespec="minutes"), "date": filed.date().isoformat(),
        "mcap": round(mcap, 1) if mcap else None, "price": price, "pdate": price_date,
        "rows": rows, "revYoy": rev_yoy, "patYoy": pat_yoy,
        "url": f.view_url or f.xbrl_url, "xbrl": f.xbrl_url,
    }


# --------------------------------------------------------------------------- #
# Market cap
# --------------------------------------------------------------------------- #

class Prices:
    """Closing prices from the exchanges' daily bhavcopy files (one download per day)."""

    def __init__(self, http: Http, cache_dir: Path):
        self.http, self.dir = http, cache_dir
        self.maps: dict = {}

    def _url(self, exch, d):
        if exch == "NSE":
            return f"{NSE_ARCHIVE}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
        return f"{BSE}/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"

    _MEM: dict = {}   # shared across runs in always-on mode: exch -> (loaded_at, result)
    _BSE_CODES: dict = {}  # ISIN -> BSE scrip code (from BSE's bhavcopy)

    def bse_code(self, isin: str) -> str:
        if not isin:
            return ""
        self._load("BSE")
        return Prices._BSE_CODES.get(isin.upper(), "")

    def _load(self, exch):
        if exch in self.maps:
            return self.maps[exch]
        mem = Prices._MEM.get(exch)
        if mem and time.monotonic() - mem[0] < 1200:
            self.maps[exch] = mem[1]
            return mem[1]
        self.maps[exch] = self._load_fresh(exch)
        Prices._MEM[exch] = (time.monotonic(), self.maps[exch])
        return self.maps[exch]

    def _load_fresh(self, exch):
        self.dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(IST).date()
        result = (None, {}, {})
        for back in range(0, 8):
            d = today - timedelta(days=back)
            if d.weekday() >= 5:
                continue
            fp = self.dir / f"{exch}_{d:%Y%m%d}.csv"
            text = fp.read_text(encoding="utf-8") if fp.exists() else None
            if text is None:
                r = self.http.get(self._url(exch, d), "nse" if exch == "NSE" else "bse",
                                  accept="*/*", tries=1, timeout=60, quiet=True)
                if r is None or len(r.content) < 500:
                    continue
                raw = r.content
                if raw[:2] == b"PK":
                    with zipfile.ZipFile(io.BytesIO(raw)) as z:
                        raw = z.read(z.namelist()[0])
                text = raw.decode("utf-8-sig", errors="replace")
                if "ISIN" not in text[:2000]:
                    continue
                fp.write_text(text, encoding="utf-8")
                for old in self.dir.glob(f"{exch}_*.csv"):
                    if old != fp:
                        old.unlink(missing_ok=True)
            by_isin, by_code = {}, {}
            isin_code = {}
            for row in csv.DictReader(io.StringIO(text)):
                row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
                px = _num(row.get("ClsPric") or row.get("CLOSE") or row.get("LastPric") or "")
                if not px:
                    continue
                isin = row.get("ISIN", "").upper()
                if isin and (isin not in by_isin or row.get("SctySrs") in ("EQ", "SM", "ST", "A", "B")):
                    by_isin[isin] = px
                for code in (row.get("TckrSymb"), row.get("FinInstrmId")):
                    if code:
                        by_code[code.upper()] = px
                if exch == "BSE" and isin and (row.get("FinInstrmId") or "").isdigit():
                    isin_code[isin] = row["FinInstrmId"]
            result = (d, by_isin, by_code)
            if exch == "BSE":
                Prices._BSE_CODES = isin_code
            log(f"  {exch} prices loaded for {d:%d %b} ({len(by_isin)} stocks)")
            break
        self.maps[exch] = result
        return result

    def close(self, exch, isin, code):
        d, by_isin, by_code = self._load(exch)
        px = by_isin.get(isin or "") or by_code.get((code or "").upper())
        return (px, d) if px else (None, None)


def bse_mcap(http: Http, scrip: str):
    """Full market cap in Cr as published by BSE, or None."""
    if not scrip:
        return None
    j = http.get_json(f"{BSE_API}/StockTrading/w", "bse", tries=1, referer=BSE + "/",
                      params={"flag": "", "quotetype": "EQ", "scripcode": scrip})
    try:
        mc = float(str(j.get("MktCapFull", "")).replace(",", ""))
        return mc if mc > 0 else None
    except Exception:
        return None


def market_cap(http: Http, prices: Prices, f: Filing, isin: str, sym: str, scrip: str, shares):
    """Returns (mcap in Cr, price, price date) or (None, None, None).
    BSE's published market cap first (share counts inside filings are sometimes wrong),
    then shares x last close, then NSE's quote."""
    px = pdate = None
    for exch in (["BSE", "NSE"] if f.source == "BSE" else ["NSE", "BSE"]):
        px, d = prices.close(exch, isin, sym if exch == "NSE" else scrip)
        if px:
            pdate = d.isoformat()
            break
    mc = bse_mcap(http, scrip or prices.bse_code(isin))
    if mc:
        return mc, px, pdate
    if shares and px:
        return shares * px / CRORE, px, pdate
    return None, None, None


# --------------------------------------------------------------------------- #
# Card image
# --------------------------------------------------------------------------- #

BG, INK, MUTED, SOFT = (247, 248, 246), (17, 20, 18), (108, 115, 111), (70, 76, 72)
GREEN, GREEN_LT, UP, DOWN, LINE = (40, 102, 82), (226, 238, 231), (27, 128, 74), (196, 50, 50), (226, 229, 226)


class Fonts:
    FILES = {"Regular": ["Inter-Regular.ttf"], "SemiBold": ["Inter-SemiBold.ttf", "Inter-Medium.ttf"],
             "Bold": ["Inter-Bold.ttf", "Inter-ExtraBold.ttf"]}
    SYSTEM = {
        "Regular": ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
                    "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
        "SemiBold": ["C:/Windows/Fonts/seguisb.ttf", "C:/Windows/Fonts/arialbd.ttf",
                     "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
        "Bold": ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    }

    def __init__(self):
        self.paths = {w: self._find(w) for w in self.FILES}
        self.cache = {}

    def _find(self, w):
        for d in (HERE / "fonts", HERE):
            for n in self.FILES[w]:
                if (d / n).exists():
                    return str(d / n)
        for p in self.SYSTEM[w]:
            if Path(p).exists():
                return p
        return None

    def get(self, w, size):
        key = (w, size)
        if key not in self.cache:
            try:
                self.cache[key] = (ImageFont.truetype(self.paths[w], size) if self.paths[w]
                                   else ImageFont.load_default(size=size))
            except Exception:
                self.cache[key] = ImageFont.load_default()
        return self.cache[key]


def _tracked(d, xy, text, font, fill, spacing=2.5, anchor_right=False):
    width = sum(d.textlength(ch, font=font) for ch in text) + spacing * (len(text) - 1)
    x, y = xy
    if anchor_right:
        x -= width
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + spacing
    return width


def _fit(d, fonts, text, w, size, max_w, min_size=16):
    while size > min_size and d.textlength(text, font=fonts.get(w, size)) > max_w:
        size -= 1
    f = fonts.get(w, size)
    if d.textlength(text, font=f) > max_w:
        while len(text) > 3 and d.textlength(text + "…", font=f) > max_w:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text, f


def _pill(d, x, y, text, font, fill, color, pad_x=18, h=42):
    tw = d.textlength(text, font=font)
    d.rounded_rectangle((x, y, x + tw + 2 * pad_x, y + h), radius=h // 2, fill=fill)
    bbox = d.textbbox((0, 0), text, font=font)
    d.text((x + pad_x, y + (h - (bbox[3] - bbox[1])) / 2 - bbox[1]), text, font=font, fill=color)
    return tw + 2 * pad_x


def render_card(res: dict, cfg: dict, fonts: Fonts | None = None) -> bytes:
    if Image is None:
        raise RuntimeError("Pillow is not installed (pip install pillow)")
    fonts = fonts or Fonts()
    W, P = 1080, 56
    img = Image.new("RGB", (W, 1700), BG)
    d = ImageDraw.Draw(img)
    y = 46
    # Brand line
    d.polygon([(P, y + 17), (P + 11, y + 6), (P + 22, y + 17), (P + 11, y + 28)], fill=GREEN)
    d.text((P + 34, y), cfg.get("CARD_BRAND") or "Results Desk", font=fonts.get("Bold", 30), fill=GREEN)
    _tracked(d, (W - P, y + 8), "INDIAN RESULTS · XBRL", fonts.get("Bold", 19), MUTED, 3, anchor_right=True)
    y += 58
    d.line((P, y, W - P, y), fill=LINE, width=2)
    y += 30
    # Badges + filing time
    x = P
    x += _pill(d, x, y, res["per"], fonts.get("Bold", 22), GREEN_LT, GREEN) + 12
    x += _pill(d, x, y, res["basis"].upper(), fonts.get("Bold", 21), GREEN, (255, 255, 255)) + 12
    if res.get("ex") == "NSE SME":
        _pill(d, x, y, "SME", fonts.get("Bold", 21), (255, 236, 205), (150, 90, 10))
    try:
        filed = datetime.fromisoformat(res["filed"])
        when = f"Results on {filed.day} {filed:%b %Y}"
    except Exception:
        when = ""
    fnt = fonts.get("Regular", 24)
    d.text((W - P - d.textlength(when, font=fnt), y + 8), when, font=fnt, fill=MUTED)
    y += 70
    # Company name
    name, nf = _fit(d, fonts, res["name"] or res["sym"] or res["scrip"], "Bold", 64, W - 2 * P, 38)
    d.text((P, y), name, font=nf, fill=INK)
    y += nf.size + 18
    ids = []
    if res.get("sym"):
        ids.append(f"{'NSE SME' if res.get('ex') == 'NSE SME' else 'NSE'}: {res['sym']}")
    if res.get("scrip"):
        ids.append(f"BSE: {res['scrip']}")
    if res.get("isin"):
        ids.append(res["isin"])
    d.text((P, y), " · ".join(ids), font=fonts.get("Regular", 25), fill=MUTED)
    y += 54
    # Market cap box
    bh = 146
    d.rounded_rectangle((P, y, W - P, y + bh), radius=20, fill=GREEN_LT)
    _tracked(d, (P + 30, y + 24), "MARKET CAP", fonts.get("Bold", 19), MUTED, 3)
    mc = f"₹{indian(round(res['mcap']))} Cr" if res.get("mcap") else "n/a"
    d.text((P + 28, y + 52), mc, font=fonts.get("Bold", 50), fill=INK)
    if res.get("price"):
        pt = f"Price ₹{res['price']:,.2f}"
        if res.get("pdate"):
            pd_ = date.fromisoformat(res["pdate"])
            pt += f" · close {pd_.day} {pd_:%b}"
        d.text((P + 30, y + 112), pt, font=fonts.get("Regular", 21), fill=SOFT)
    rows = {r[0]: r for r in res["rows"]}
    lines = [("Revenue YoY", rows.get(res["rows"][0][0])), ("PAT YoY", rows.get("PAT"))]
    ry = y + 30
    for label, r in lines:
        txt, dirn = (r[4], r[5]) if r else ("–", "none")
        col = UP if dirn == "up" else DOWN if dirn == "down" else SOFT
        gf, lf = fonts.get("Bold", 34), fonts.get("SemiBold", 24)
        gw = d.textlength(txt, font=gf)
        d.text((W - P - 30 - gw, ry), txt, font=gf, fill=col)
        lw = d.textlength(label, font=lf)
        d.text((W - P - 30 - gw - 18 - lw, ry + 7), label, font=lf, fill=SOFT)
        ry += 50
    y += bh + 40
    # Table heading
    span_word = {"QoQ": "QUARTERLY P&L", "HoH": "HALF-YEARLY P&L"}.get(res["seq"], "P&L")
    _tracked(d, (P, y), span_word, fonts.get("Bold", 21), INK, 3.5)
    unit = f"₹ Cr · {res['basis'].lower()}"
    uf = fonts.get("Regular", 21)
    d.text((W - P - d.textlength(unit, font=uf), y), unit, font=uf, fill=MUTED)
    y += 40
    # Table
    tx0, tx1 = P, W - P
    ip = 26
    cols = [270, 118, 118, 118, 156, 156]  # label, cur, prev, yoy-period, YoY, QoQ
    scale = (tx1 - tx0 - 2 * ip) / sum(cols)
    cols = [c * scale for c in cols]
    edges = [tx0 + ip]
    for c in cols:
        edges.append(edges[-1] + c)
    row_h, head_h = 70, 60
    table_h = head_h + row_h * len(res["rows"]) + 14
    d.rounded_rectangle((tx0, y, tx1, y + table_h), radius=18, fill=(255, 255, 255), outline=LINE, width=2)
    hy = y + 20
    hf = fonts.get("Bold", 20)
    heads = ["", res["per"], res["prevLbl"] or "", res["yoyLbl"], "YoY", res["seq"] if res["prevLbl"] else ""]
    for i, h in enumerate(heads):
        if not h:
            continue
        t = h.upper()
        _tracked(d, (edges[i + 1] - 4, hy), t, hf, SOFT, 1.5, anchor_right=True)
    y2 = y + head_h
    d.line((tx0 + ip, y2, tx1 - ip, y2), fill=INK, width=3)
    for ri, r in enumerate(res["rows"]):
        label, c, p, yv, gy, gyd, gq, gqd = r
        cy = y2 + ri * row_h
        mid = cy + row_h / 2
        if ri:
            d.line((tx0 + ip, cy, tx1 - ip, cy), fill=LINE, width=1)
        lt, lf = _fit(d, fonts, label, "SemiBold", 29, cols[0] - 8, 20)
        d.text((edges[0], mid), lt, font=lf, fill=INK, anchor="lm")
        for i, (val, w8, col) in enumerate([(c, "Bold", INK), (p, "Regular", SOFT), (yv, "Regular", SOFT)]):
            vt, vf = _fit(d, fonts, val, w8, 30 if i == 0 else 29, cols[i + 1] - 8, 18)
            d.text((edges[i + 2] - 4, mid), vt, font=vf, fill=col, anchor="rm")
        for i, (gt, gd) in enumerate([(gy, gyd), (gq, gqd)]):
            col = UP if gd == "up" else DOWN if gd == "down" else MUTED
            t, gf = _fit(d, fonts, gt, "Bold", 25, cols[4 + i] - 8, 17)
            d.text((edges[5 + i] - 4, mid), t, font=gf, fill=col, anchor="rm")
    y += table_h + 34
    # Footer
    d.line((P, y, W - P, y), fill=LINE, width=2)
    y += 22
    _tracked(d, (P, y), "FOR EDUCATION ONLY · NOT INVESTMENT ADVICE", fonts.get("Bold", 19), INK, 3)
    y += 36
    src = f"Source: {res['ex'].split()[0]} filing (XBRL)"
    try:
        filed = datetime.fromisoformat(res["filed"])
        src += f" · filed {filed.day} {filed:%b %Y, %H:%M}"
    except Exception:
        pass
    if res.get("aud"):
        src += f" · {res['aud']}"
    ff = fonts.get("Regular", 20)
    d.text((P, y), src, font=ff, fill=MUTED)
    y += 30
    note = ("PBT as filed (after exceptional items) · Exceptional: + gain, − loss · Operating profit is before provisions"
            if res.get("kind") == "bank" else
            "PBT as filed (after exceptional items) · Exceptional: + gain, − loss · EBITDA excludes other income")
    d.text((P, y), note, font=fonts.get("Regular", 18), fill=MUTED)
    y += 28
    if cfg.get("CARD_FOOTER"):
        cf = fonts.get("Bold", 26)
        t = cfg["CARD_FOOTER"]
        d.text((W - P - d.textlength(t, font=cf), y + 6), t, font=cf, fill=GREEN)
        y += 44
    y += 30
    img = img.crop((0, 0, W, int(y)))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool):
        self.token, self.chat, self.dry = token, chat_id, dry_run
        self.last = 0.0
        self.enabled = bool(token and chat_id) and not dry_run

    def _call(self, method, data, files=None):
        """Returns the sent message id (or -1 when Telegram is switched off), None on failure."""
        if not self.enabled:
            return -1
        wait = self.last + 3.2 - time.monotonic()  # stay under ~20 messages/minute per chat
        if wait > 0:
            time.sleep(wait)
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for _ in range(5):
            try:
                r = requests.post(url, data=data, files=files, timeout=60)
            except Exception as e:
                log(f"  ! Telegram error: {e}")
                time.sleep(5)
                continue
            self.last = time.monotonic()
            if r.status_code == 200:
                try:
                    return int(r.json()["result"]["message_id"])
                except Exception:
                    return -1
            if r.status_code == 429:
                try:
                    retry = int(r.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    retry = 5
                time.sleep(retry + 1)
                continue
            log(f"  ! Telegram {method} failed: HTTP {r.status_code} {r.text[:200]}")
            return None
        return None

    @staticmethod
    def _reply(data, reply_to):
        if reply_to and reply_to > 0:
            data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
        return data

    def photo(self, png: bytes, caption: str, reply_to=None):
        data = {"chat_id": self.chat, "caption": caption[:1020], "parse_mode": "HTML"}
        return self._call("sendPhoto", self._reply(data, reply_to), files={"photo": ("result.png", png, "image/png")})

    def text(self, msg: str, reply_to=None):
        data = {"chat_id": self.chat, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        return self._call("sendMessage", self._reply(data, reply_to))


def caption_for(res: dict, site_url: str = "") -> str:
    e = html.escape
    ident = res["sym"] or (f"BSE {res['scrip']}" if res["scrip"] else "")
    rows = {r[0]: r for r in res["rows"]}
    rev, pat = res["rows"][0], rows.get("PAT")
    parts = [f"<b>{e(res['name'])}</b>" + (f" ({e(ident)})" if ident else ""),
             f"{e(res['per'])} · {res['basis']} · {e(res['ex'])}"]
    if res.get("mcap"):
        parts.append(f"M-cap ₹{indian(round(res['mcap']))} Cr")
    def val(r):
        if not r:
            return "–"
        return f"{r[1]} ({e(r[4])} YoY)" if r[4] not in ("–", "") else r[1]
    parts.append(f"{e(rev[0])} {val(rev)} · PAT {val(pat)}")
    if res.get("url"):
        parts.append(f'<a href="{e(res["url"])}">Open filing</a>')
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# State + website
# --------------------------------------------------------------------------- #

class State:
    def __init__(self, folder: Path):
        self.folder = folder
        self.path = folder / "seen.json"
        self.ppath = folder / "periods.json"
        self.ipath = folder / "nse_index.json"
        self.bpath = folder / "nse_backfill.json"
        self.exists = self.path.exists()
        self.data = self._read(self.path, {})
        self.periods = self._read(self.ppath, {})
        self.index = self._read(self.ipath, {})   # NSE symbol -> {"period_end|C/S": [xbrl_url, filed]}
        self.bindex = self._read(self.bpath, {})  # same, from the one-off backfill job
        for k in ("filings", "posted", "first_seen", "warned", "alerts", "ann", "mcap", "shares", "scrip_isin",
                  "fresh", "pending"):
            self.data.setdefault(k, {})

    @staticmethod
    def _read(p, default):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def add_to_index(self, filings, target=None) -> int:
        target = self.index if target is None else target
        added = 0
        for f in filings:
            if not (f.xbrl_url and f.period_end and f.consolidated is not None):
                continue
            ident = f.symbol if f.source in ("NSE", "NSE SME") else (f"BSE:{f.scrip}" if f.scrip else "")
            if not ident:
                continue
            k = f"{f.period_end.isoformat()}|{'C' if f.consolidated else 'S'}"
            filed = (f.filed_at or datetime.now(IST)).isoformat(timespec="minutes")
            cur = target.setdefault(ident, {}).get(k)
            if cur is None or filed >= cur[1]:
                added += cur is None
                target[ident][k] = [f.xbrl_url, filed]
        return added

    def save_backfill(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        _write_if_changed(self.bpath, json.dumps(self.bindex, separators=(",", ":"), sort_keys=True))

    def resend_today(self) -> int:
        """Forget today's sent cards so the next run sends them again (e.g. after a backfill)."""
        today = datetime.now(IST).date().isoformat()
        keys = [k for k, v in self.data["posted"].items() if v.get("t", "")[:10] == today]
        for k in keys:
            del self.data["posted"][k]
        for uid, v in list(self.data["filings"].items()):
            if v.get("s") in ("posted", "superseded", "dup") and v.get("t", "")[:10] == today:
                del self.data["filings"][uid]
        return len(keys)

    def mark(self, uid, status, attempts=None):
        if not status.startswith("retry"):
            self.data["pending"].pop(uid, None)
        rec = {"s": status, "t": datetime.now(IST).isoformat(timespec="minutes")}
        if attempts:
            rec["n"] = attempts
        self.data["filings"][uid] = rec

    RETAIN_DAYS = {"filings": 7, "posted": 60, "first_seen": 3, "alerts": 4, "ann": 3, "mcap": 2, "shares": 400,
                   "scrip_isin": 400, "fresh": 3, "pending": 4}

    def save(self):
        now = datetime.now(IST)
        for k, days in self.RETAIN_DAYS.items():
            cutoff = (now - timedelta(days=days)).isoformat()
            d = self.data[k]  # prune in place: other code may hold a reference to this dict
            for u in [u for u, v in d.items() if (v.get("t", "") if isinstance(v, dict) else str(v)) < cutoff]:
                del d[u]
        old_cut = (now.date() - timedelta(days=800)).isoformat()
        for ck in list(self.periods):
            self.periods[ck] = {k: v for k, v in self.periods[ck].items() if k.split("_")[1] >= old_cut}
            if not self.periods[ck]:
                del self.periods[ck]
        self.folder.mkdir(parents=True, exist_ok=True)
        _write_if_changed(self.path, json.dumps(self.data, indent=0, sort_keys=True, ensure_ascii=False))
        _write_if_changed(self.ppath, json.dumps(self.periods, separators=(",", ":"), sort_keys=True))
        for sym in list(self.index):
            self.index[sym] = {k: v for k, v in self.index[sym].items() if k.split("|")[0] >= old_cut}
            if not self.index[sym]:
                del self.index[sym]
        _write_if_changed(self.ipath, json.dumps(self.index, separators=(",", ":"), sort_keys=True))


def _write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except Exception:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_site(site: Path, new_results: list, cfg: dict, status: dict, offline_html: Path | None = None):
    """Website = static index.html + data/index.json + one small JSON file per day."""
    days_dir = site / "data" / "days"
    days_dir.mkdir(parents=True, exist_ok=True)
    by_day = defaultdict(list)
    for r in new_results:
        by_day[r["date"]].append(r)
    for day, rs in by_day.items():
        fp = days_dir / f"{day}.json"
        merged = {r["id"]: r for r in _read_json(fp, {}).get("results", [])}
        merged.update({r["id"]: r for r in rs})
        items = sorted(merged.values(), key=lambda r: r.get("filed", ""), reverse=True)
        _write_if_changed(fp, json.dumps({"date": day, "results": items}, ensure_ascii=False, separators=(",", ":")))
    cutoff = (datetime.now(IST).date() - timedelta(days=int(cfg["KEEP_DAYS"]))).isoformat()
    days = []
    for fp in sorted(days_dir.glob("*.json")):
        if fp.stem < cutoff:
            fp.unlink()
            continue
        days.append({"date": fp.stem, "count": len(_read_json(fp, {}).get("results", []))})
    index = {"title": cfg["SITE_TITLE"], "minMcap": float(cfg["MIN_MCAP_CR"]),
             "checked": datetime.now(IST).isoformat(timespec="minutes"), "status": status,
             "days": sorted(days, key=lambda x: x["date"], reverse=True)}
    _write_if_changed(site / "data" / "index.json", json.dumps(index, ensure_ascii=False, separators=(",", ":")))
    page = SITE_HTML.replace("__TITLE__", html.escape(cfg["SITE_TITLE"]))
    _write_if_changed(site / "index.html", page)
    _write_if_changed(site / ".nojekyll", "")
    if offline_html:  # same page with the data inside, opens straight from the PC (no internet server needed)
        payload = dict(index)
        payload["daysData"] = [{"date": x["date"], "results": _read_json(days_dir / f"{x['date']}.json", {})
                                .get("results", [])} for x in index["days"]]
        data_js = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        offline_html.parent.mkdir(parents=True, exist_ok=True)
        offline_html.write_text(page.replace("/*__DATA__*/null", data_js), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main run
# --------------------------------------------------------------------------- #

def fill_history(http, st, cache_key: str, basis: str, need_ends: set, sym: str, scrip: str = ""):
    """Fill comparison columns from earlier filings (NSE or BSE) of the same company and basis."""
    entries = {}
    for ident in ([f"BSE:{scrip}"] if scrip else []) + ([sym] if sym else []):
        entries.update(st.bindex.get(ident, {}))
        entries.update(st.index.get(ident, {}))
    for pe in sorted(need_ends):
        ent = entries.get(f"{pe.isoformat()}|{basis}")
        if not ent:
            continue
        kind = "bse" if "bseindia.com" in ent[0] else "nse"
        r = http.get(ent[0], kind, accept="application/xml,text/xml,*/*", timeout=40)
        if r is None:
            continue
        try:
            p = parse_xbrl(r.content)
        except Exception as e:
            log(f"    could not read earlier filing: {e}")
            continue
        st.periods.setdefault(cache_key, {}).update(p["periods"])


def process_filing(f: Filing, ctx: dict) -> str:
    http, st, cfg, prices = ctx["http"], ctx["st"], ctx["cfg"], ctx["prices"]
    kind = "bse" if f.source == "BSE" else "nse"
    r = http.get(f.xbrl_url, kind, accept="application/xml,text/xml,*/*",
                 referer=(BSE + "/") if kind == "bse" else NSE + "/")
    if r is None:
        return "retry"
    try:
        parsed = parse_xbrl(r.content)
    except Exception as e:
        log(f"  ! could not read XBRL for {f.company}: {e}")
        return "bad"
    if not parsed["current"]:
        return "nodata"
    info = parsed["info"]
    cm = parsed["periods"][parsed["current"]]
    if cm.get("revenue") is None and cm.get("interest_earned") is None and cm.get("pat") is None:
        return "nodata"
    isin = (info.get("ISIN") or f.isin or "").strip().upper()
    if f.consolidated is None:
        f.consolidated = _consolidated_flag(info.get("NatureOfReportStandaloneConsolidated"))
    basis = "C" if f.consolidated else "S"
    ce = unkey(parsed["current"])[1]
    ident = isin or f.company_id
    post_key = f"{ident}|{ce.isoformat()}|{basis}"
    if post_key in st.data["posted"]:
        return "dup"
    if basis == "S" and f"{ident}|{ce.isoformat()}|C" in st.data["posted"]:
        return "superseded"
    nn0 = norm_name(info.get("NameOfTheCompany") or f.company)
    for v in st.data["posted"].values():
        if v.get("n") == nn0 and v.get("pe") == ce.isoformat() and v.get("b") in (basis, "C"):
            return "dup" if v.get("b") == basis else "superseded"
    cache_key = f"{ident}|{basis}"
    st.periods.setdefault(cache_key, {}).update(parsed["periods"])
    cache = st.periods[cache_key]
    stamp = datetime.now(IST).isoformat(timespec="minutes")
    if parsed["shares"] and isin:
        st.data["shares"][isin] = {"v": round(parsed["shares"]), "t": stamp}
    scrip0 = str(info.get("ScripCode") or f.scrip or "").strip()
    if isin and scrip0.isdigit():
        st.data["scrip_isin"][scrip0] = {"v": isin, "t": stamp}

    sym = clean_symbol(info.get("Symbol")) or clean_symbol(f.symbol)
    scrip = str(info.get("ScripCode") or f.scrip or "").strip()
    mcap, price, pdate = market_cap(http, prices, f, isin, sym, scrip, parsed["shares"])
    if mcap is None:
        return "retry_mcap"
    name = info.get("NameOfTheCompany") or f.company
    if mcap < float(cfg["MIN_MCAP_CR"]):
        log(f"  - {name}: M-cap ₹{mcap:,.0f} Cr, below limit")
        return "small"

    cs, ce = unkey(parsed["current"])
    span = month_span(cs, ce)
    needed = [shift_period(cs, ce, -12)] + ([shift_period(cs, ce, -span)] if span < 12 else [])
    need_ends = {e for (s, e) in needed if get_period(cache, s, e) is None}
    if need_ends and (sym or scrip):
        fill_history(http, st, cache_key, basis, need_ends, sym, scrip if scrip.isdigit() else "")
        cache = st.periods[cache_key]

    res = build_result(f, parsed, cache, basis, mcap, price, pdate)
    png = render_card(res, cfg, ctx["fonts"])
    out = ctx["cards_dir"] / (re.sub(r"[^A-Za-z0-9_.-]+", "_", res["id"]) + ".png")
    out.write_bytes(png)
    al = st.data["alerts"]
    nn = norm_name(name)
    alert = (al.get(isin) or al.get(sym) or (al.get(f"BSE:{scrip}") if scrip else None)
             or next((v for v in al.values() if isinstance(v, dict) and v.get("n") == nn), None) or {})
    mid = ctx["tg"].photo(png, caption_for(res), reply_to=alert.get("mid"))
    if mid is None:
        return "retry_tg"
    ctx["new_results"].append(res)
    st.data["posted"][post_key] = {"t": datetime.now(IST).isoformat(timespec="minutes"), "n": nn,
                                   "pe": ce.isoformat(), "b": basis}
    if not ctx.get("dry_run"):
        st.save()  # record each card right away, so a stopped run never re-sends it
    log(f"  + {res['name']} {res['per']} {res['basis']}: M-cap ₹{mcap:,.0f} Cr -> card sent")
    return "posted"


def _alert_mcap(a: dict, ctx: dict, stamp: str, today: str):
    st, http, prices = ctx["st"], ctx["http"], ctx["prices"]
    ck = a["symbol"] or f"BSE:{a['scrip']}"
    cached = st.data["mcap"].get(ck)
    if cached and cached.get("t", "")[:10] == today:
        return cached["v"]
    scrip = a.get("scrip") or next((k for k, v in st.data["scrip_isin"].items()
                                     if a["isin"] and v.get("v") == a["isin"]), "") or prices.bse_code(a["isin"])
    mcap = bse_mcap(http, scrip)
    if mcap is None:
        shares = st.data["shares"].get(a["isin"], {}).get("v") if a["isin"] else None
        if shares:
            px, _ = prices.close("BSE" if a["source"] == "BSE" else "NSE", a["isin"], a["symbol"] or scrip)
            mcap = shares * px / CRORE if px else None
    if mcap is not None:
        st.data["mcap"][ck] = {"v": round(mcap, 1), "t": stamp}
    return mcap


def company_keys(isin="", symbol="", scrip="", name="") -> set:
    keys = {isin.upper() if isin else "", symbol.upper() if symbol else "",
            f"BSE:{scrip}" if scrip else "", f"N:{norm_name(name)}" if name else ""}
    return keys - {"", "N:"}


def mark_fresh(st: State, anns: list[dict], since: datetime) -> None:
    """Remember companies whose results announcement came out after the bot went live."""
    for a in anns:
        if a["ts"] is None or a["ts"] < since:
            continue
        isin = a.get("isin") or (st.data["scrip_isin"].get(a.get("scrip", ""), {}).get("v", "") if a.get("scrip") else "")
        rec = {"t": a["ts"].isoformat(timespec="minutes")}
        for k in company_keys(isin, a.get("symbol", ""), a.get("scrip", ""), a.get("company", "")):
            st.data["fresh"].setdefault(k, rec)


def is_fresh(st: State, keys: set) -> bool:
    return any(k in st.data["fresh"] for k in keys)


def process_alerts(anns: list[dict], ctx: dict, counts: dict) -> None:
    """One short Telegram alert as soon as a results PDF is out (company M-cap >= limit).
    NSE announcements are handled first; a BSE one is skipped if the same company already got an alert."""
    st, tg, cfg = ctx["st"], ctx["tg"], ctx["cfg"]
    now = datetime.now(IST)
    today, stamp = now.date().isoformat(), now.isoformat(timespec="minutes")
    alerts = st.data["alerts"]
    for a in sorted(anns, key=lambda x: (x["source"] == "BSE", x["ts"] or now)):
        if a["uid"] in st.data["ann"]:
            continue
        st.data["ann"][a["uid"]] = {"t": stamp}
        if (a["ts"] is None or a["ts"] < ctx["alert_cutoff"] or a["ts"] < ctx["since"]
                or time.monotonic() > ctx["deadline"]):
            continue  # only fresh results get an alert; older ones just get their card
        a.setdefault("scrip", "")
        if not a["isin"] and a["scrip"]:
            a["isin"] = st.data["scrip_isin"].get(a["scrip"], {}).get("v", "")
        key = a["isin"] or a["symbol"] or f"BSE:{a['scrip']}"
        nn = norm_name(a["company"])
        if alerts.get(key, {}).get("d") == today or any(
                v.get("d") == today and v.get("n") == nn for v in alerts.values() if isinstance(v, dict)):
            continue
        if any((k.startswith(key + "|") or v.get("n") == nn) and v.get("t", "")[:10] == today
               for k, v in st.data["posted"].items()):
            continue  # the numbers card already went out
        mcap = _alert_mcap(a, ctx, stamp, today)
        if mcap is None:
            log(f"  ? {a['company']}: results PDF out, market cap unknown - the card will still check later")
            continue
        if mcap < float(cfg["MIN_MCAP_CR"]):
            continue
        e = html.escape
        ident = a["symbol"] + (" · SME" if a["source"] == "NSE SME" else "") if a["symbol"] else f"BSE {a['scrip']}"
        msg = (f"🔔 <b>{e(a['company'])}</b> ({e(ident)}) filed results · M-cap ₹{indian(round(mcap))} Cr\n"
               + (f'<a href="{e(a["pdf"])}">Open PDF</a> · ' if a["pdf"] else "")
               + "numbers card follows as a reply")
        mid = tg.text(msg)
        if mid is not None:
            rec = {"t": stamp, "d": today, "mid": mid, "n": nn}
            for k in {key, a["symbol"], f"BSE:{a['scrip']}" if a["scrip"] else ""} - {""}:
                alerts[k] = rec
            counts["alert"] += 1
            log(f"  🔔 {a['company']}: results PDF out (M-cap ₹{mcap:,.0f} Cr) -> alert sent")
            if not ctx.get("dry_run"):
                st.save()


def run(cfg: dict, dry_run: bool = False, http: Http | None = None) -> int:
    started = time.monotonic()
    deadline = started + float(cfg["MAX_RUN_MINUTES"]) * 60
    now = datetime.now(IST)
    st = State(HERE / cfg["STATE_DIR"])
    out_dir = HERE / cfg["OUT_DIR"]
    cards_dir = out_dir / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    http = http or Http()
    tg = Telegram(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_CHAT_ID"], dry_run)
    if not tg.enabled and not dry_run:
        log("Telegram is not set up yet: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "(GitHub: Settings -> Secrets and variables -> Actions). Nothing was checked or saved.")
        return 0
    first_run = not st.exists
    if first_run:
        tg.text(f"✅ <b>{html.escape(cfg['CARD_BRAND'])} is live.</b>\nI check NSE, NSE SME and BSE every few minutes "
                f"and send results of companies with market cap ₹{indian(round(float(cfg['MIN_MCAP_CR'])))} Cr "
                f"and above here." + (" A 🔔 alert comes when the results PDF is out; the numbers card follows "
                                     "as a reply." if _on(cfg["INSTANT_ALERTS"]) else ""))
    # Anything that came in while the bot was off (overnight, Sunday) is still new to you.
    quiet_from = last_window_end(now, cfg) - timedelta(minutes=15)
    card_cutoff = min(now - timedelta(hours=float(cfg["CARD_MAX_AGE_HOURS"])), quiet_from)
    alert_cutoff = min(now - timedelta(minutes=float(cfg["ALERT_MAX_AGE_MINUTES"])), quiet_from)
    d_to = now.date()
    d_from = d_to if first_run else min(d_to - timedelta(days=max(0, int(cfg["LOOKBACK_DAYS"]))), card_cutoff.date())
    sources = [s.strip().lower() for s in cfg["SOURCES"].split(",") if s.strip()]
    log(f"Results Desk v{VERSION} · checking {', '.join(sources)} for {d_from:%d %b}–{d_to:%d %b}"
        f"{' (first run)' if first_run else ''}{' (dry run)' if dry_run else ''}")

    filings: list[Filing] = []
    health = {}
    for name, fn in (("nse", lambda: discover_nse(http, "equities", d_from, d_to)),
                     ("nse_sme", lambda: discover_nse(http, "sme", d_from, d_to)),
                     ("bse", lambda: discover_bse(http))):
        if name not in sources:
            continue
        try:
            got, ok = fn()
        except Exception as e:
            log(f"  ! {name} listing failed: {e}")
            got, ok = [], False
        health[name] = ok
        filings += got
        log(f"  {name}: {len(got)} result filings listed" + ("" if ok else " (SITE DID NOT RESPOND)"))

    # Warn once a day on Telegram if an exchange is not responding.
    today = now.date().isoformat()
    for name, ok in health.items():
        if not ok and st.data["warned"].get(name) != today:
            label = {"nse": "NSE", "nse_sme": "NSE SME", "bse": "BSE"}[name]
            tg.text(f"⚠️ {label} did not respond to the results checker this run. "
                    f"Results from other exchanges still come through; it will keep retrying.")
            st.data["warned"][name] = today

    # Results announcements (the PDFs). A company only gets a card if its announcement came out after the
    # bot went live, so results announced earlier never show up.
    if "since" not in st.data:
        st.data["since"] = now.isoformat(timespec="seconds")
    since = datetime.fromisoformat(st.data["since"])
    anns = []
    for name, index in (("nse", "equities"), ("nse_sme", "sme")):
        if name in sources and health.get(name, True):
            anns += discover_nse_announcements(http, index, d_to, max(alert_cutoff.date(), since.date()))[0]
    if "bse" in sources and health.get("bse", True):
        anns += discover_bse_announcements(http, d_to, max(alert_cutoff.date(), since.date()))[0]
    mark_fresh(st, anns, since)

    filings = [f for f in filings if f.xbrl_url]
    listed = {f.uid for f in filings}
    for uid, d in list(st.data["pending"].items()):
        if uid not in listed and st.data["filings"].get(uid, {}).get("s", "new").startswith(("new", "retry")):
            try:
                filings.append(Filing.from_dict(d))
            except Exception:
                st.data["pending"].pop(uid, None)
    st.add_to_index(filings)
    todo = [f for f in filings if st.data["filings"].get(f.uid, {}).get("s", "new").startswith(("new", "retry"))]
    # Only fresh results: anything filed before the cut-off is skipped quietly.
    fresh = []
    for f in todo:
        when = f.filed_at or bse_file_time(f.xbrl_url)
        if when is not None and when < card_cutoff and f.uid not in st.data["pending"]:
            st.mark(f.uid, "old")
        else:
            fresh.append(f)
    todo = fresh
    if first_run:
        for f in todo:
            if f.filed_at and f.filed_at.date() < d_to:
                st.mark(f.uid, "old")
        todo = [f for f in todo if not (f.filed_at and f.filed_at.date() < d_to)]

    # Group by exchange company + period; prefer the consolidated filing.
    order = {"NSE": 0, "NSE SME": 1, "BSE": 2}
    groups: dict[tuple, list[Filing]] = defaultdict(list)
    for f in todo:
        groups[(f.source, f.exchange_key, str(f.period_end))].append(f)
    settle = timedelta(minutes=float(cfg["SETTLE_MINUTES"]))
    ctx = {"http": http, "st": st, "cfg": cfg, "tg": tg, "fonts": Fonts(), "cards_dir": cards_dir, "dry_run": dry_run,
           "prices": Prices(http, out_dir / "prices"), "new_results": [], "since": since,
           "alert_cutoff": alert_cutoff}
    counts = defaultdict(int)
    keys = sorted(groups, key=lambda k: (order.get(k[0], 9),
                                         min((f.filed_at or now) for f in groups[k])))
    for gk in keys:
        if time.monotonic() > deadline:
            log("Time limit reached; the rest will be done next run.")
            break
        fs = groups[gk]
        if not any(is_fresh(st, company_keys(f.isin or st.data["scrip_isin"].get(f.scrip, {}).get("v", ""),
                                             f.symbol, f.scrip, f.company)) for f in fs):
            counts["announced earlier"] += 1
            continue  # results announced before the bot went live (or not announced yet): no card
        gid = "|".join(gk)
        st.data["first_seen"].setdefault(gid, {"t": now.isoformat(timespec="minutes")})
        for f in fs:  # keep it even if the exchange's list moves on before we finish
            st.data["pending"].setdefault(f.uid, {**f.to_dict(), "t": now.isoformat(timespec="minutes")})
        newest = lambda xs: max(xs, key=lambda f: (f.revised, f.filed_at or now))  # noqa: E731
        cons = [f for f in fs if f.consolidated is True]
        if cons:
            chosen = newest(cons)
        else:
            chosen = newest(fs)
            seen_at = chosen.filed_at or datetime.fromisoformat(st.data["first_seen"][gid]["t"])
            hist = {}
            for ident in (chosen.symbol, f"BSE:{chosen.scrip}" if chosen.scrip else ""):
                if ident:
                    hist.update(st.bindex.get(ident, {}))
                    hist.update(st.index.get(ident, {}))
            past_c = any(k.endswith("|C") and not k.startswith(str(chosen.period_end)) for k in hist)
            past_s = any(k.endswith("|S") and not k.startswith(str(chosen.period_end)) for k in hist)
            # usually consolidated -> wait up to 6 h; usually standalone only -> 15 min; unknown -> 1 h
            wait = timedelta(hours=6) if past_c else (settle if past_s else max(settle, timedelta(hours=1)))
            if now - seen_at < wait:
                counts["waiting for consolidated"] += 1
                continue
        status = process_filing(chosen, ctx)
        counts[status] += 1
        if status.startswith("retry"):
            n = int(st.data["filings"].get(chosen.uid, {}).get("n", 0)) + 1
            st.mark(chosen.uid, status if n < 6 else "failed", n)
            continue
        for f in fs:
            st.mark(f.uid, status if f is chosen else "superseded")

    # Instant "results are out" alerts from the announcement feeds (the PDF comes before the XBRL).
    ctx["deadline"] = deadline + 120
    if _on(cfg["INSTANT_ALERTS"]):
        if first_run:  # don't alert on everything from earlier today on the very first run
            for a in anns:
                st.data["ann"][a["uid"]] = {"t": now.isoformat(timespec="minutes")}
        else:
            process_alerts(anns, ctx, counts)

    status = {k: bool(v) for k, v in health.items()}
    on_github = os.environ.get("GITHUB_ACTIONS") == "true"
    if dry_run:  # preview only: never touch the real website or the saved state
        write_site(out_dir / "site", ctx["new_results"], cfg, status, offline_html=out_dir / "Results.html")
        log(f"Dry run: preview page at {out_dir / 'Results.html'} (state not saved)")
    else:
        if _on(cfg["WEBSITE"]):
            write_site(HERE / cfg["SITE_DIR"], ctx["new_results"], cfg, status,
                       offline_html=None if on_github else out_dir / "Results.html")
        st.save()
    log(("Done: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))) if counts else "Done: nothing new")
    return 0


# --------------------------------------------------------------------------- #
# Sample card (for testing Telegram and the look)
# --------------------------------------------------------------------------- #

def sample_result() -> dict:
    cache = {
        "2026-07-01_2026-09-30": {"revenue": 659.4, "other_income": 14.2, "total_expenses": 542.6,
                                  "finance": 18.1, "depreciation": 36.7, "pbe": 131.0, "pbt": 126.5, "pat": 94.1},
        "2026-04-01_2026-06-30": {"revenue": 554.1, "other_income": 11.8, "total_expenses": 467.9,
                                  "finance": 19.0, "depreciation": 35.1, "pbe": 98.0, "pbt": 98.0, "pat": 73.3},
        "2025-07-01_2025-09-30": {"revenue": 537.2, "other_income": 9.6, "total_expenses": 434.8,
                                  "finance": 21.4, "depreciation": 30.2, "pbe": 112.0, "pbt": 112.0, "pat": 83.8},
    }
    f = Filing(source="NSE", symbol="SAMPLEIND", scrip="500000", company="Sample Industries Ltd",
               isin="INE000X01010", filed_at=datetime(2026, 11, 5, 18, 42, tzinfo=IST),
               view_url="https://www.nseindia.com", xbrl_url="", consolidated=True)
    parsed = {"info": {"NameOfTheCompany": "Sample Industries Ltd", "Symbol": "SAMPLEIND",
                       "ScripCode": "500000", "ISIN": "INE000X01010",
                       "WhetherResultsAreAuditedOrUnaudited": "Unaudited"},
              "periods": cache, "current": "2026-07-01_2026-09-30", "kind": "standard"}
    return build_result(f, parsed, cache, "C", 15234.6, 794.3, "2026-11-05")


def skip_backlog(cfg: dict) -> int:
    """Mark everything currently listed as already seen, so only results filed from now on are sent."""
    http, st = Http(), State(HERE / cfg["STATE_DIR"])
    today = datetime.now(IST).date()
    stamp = datetime.now(IST).isoformat(timespec="minutes")
    n_f = n_a = 0
    for fn in (lambda: discover_nse(http, "equities", today - timedelta(days=1), today),
               lambda: discover_nse(http, "sme", today - timedelta(days=1), today),
               lambda: discover_bse(http)):
        got, _ = fn()
        st.add_to_index(got)
        for f in got:
            if st.data["filings"].get(f.uid, {}).get("s", "new").startswith(("new", "retry")):
                st.mark(f.uid, "old")
                n_f += 1
    anns = []
    for index in ("equities", "sme"):
        anns += discover_nse_announcements(http, index, today)[0]
    anns += discover_bse_announcements(http, today)[0]
    for a in anns:
        if a["uid"] not in st.data["ann"]:
            st.data["ann"][a["uid"]] = {"t": stamp}
            n_a += 1
    st.data["since"] = datetime.now(IST).isoformat(timespec="seconds")
    st.data["fresh"] = {}
    st.save()
    log(f"Skipped backlog: {n_f} filings and {n_a} announcements marked as seen")
    Telegram(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_CHAT_ID"], False).text(
        "👍 All caught up. From now on you'll only get results as they are filed.")
    return 0


def loop(cfg: dict, dry_run: bool = False) -> int:
    """Always-on mode: check every POLL_SECONDS during ACTIVE_HOURS/ACTIVE_DAYS, every 10 minutes otherwise."""
    poll = max(20.0, float(cfg["POLL_SECONDS"]))
    cfg = dict(cfg, MAX_RUN_MINUTES=str(min(float(cfg["MAX_RUN_MINUTES"]), 4)))
    http = Http()
    log(f"Always-on mode: checking every {poll:.0f} seconds. Press Ctrl+C to stop.")
    runs = 0
    while True:
        t0 = time.monotonic()
        try:
            run(cfg, dry_run, http=http)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # keep going whatever happens
            log(f"  ! run failed: {type(e).__name__}: {e}")
        runs += 1
        if runs % 90 == 0:
            http = Http()  # fresh cookies now and then
        wait = poll if is_active(datetime.now(IST), cfg) else 600
        try:
            time.sleep(max(5.0, wait - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Post NSE/BSE result cards to Telegram and a website.")
    ap.add_argument("--dry-run", action="store_true", help="do everything except send to Telegram")
    ap.add_argument("--sample", action="store_true", help="save a sample card to out/sample_card.png")
    ap.add_argument("--test-telegram", action="store_true", help="send the sample card to Telegram")
    ap.add_argument("--backfill", nargs=2, metavar=("FROM", "TO"),
                    help="one-off: index NSE result filings made between two dates (YYYY-MM-DD) for YoY figures")
    ap.add_argument("--skip-backlog", action="store_true",
                    help="mark everything listed right now as seen; only newer results get sent")
    ap.add_argument("--resend-today", action="store_true",
                    help="send today's cards again on the next run (e.g. after a backfill)")
    ap.add_argument("--loop", action="store_true",
                    help="keep running and check every POLL_SECONDS (for an always-on phone/PC/server)")
    a = ap.parse_args(argv)
    cfg = load_config()
    if a.skip_backlog:
        return skip_backlog(cfg)
    if a.resend_today:
        st = State(HERE / cfg["STATE_DIR"])
        n = st.resend_today()
        st.save()
        log(f"{n} of today's cards will be sent again on the next run")
        return 0
    if a.backfill:
        return backfill_index(cfg, date.fromisoformat(a.backfill[0]), date.fromisoformat(a.backfill[1]))
    if a.loop:
        return loop(cfg, a.dry_run)
    if a.sample or a.test_telegram:
        res = sample_result()
        png = render_card(res, cfg)
        out = HERE / cfg["OUT_DIR"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "sample_card.png").write_bytes(png)
        log(f"Sample card saved to {out / 'sample_card.png'}")
        if a.test_telegram:
            tg = Telegram(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_CHAT_ID"], False)
            if not tg.enabled:
                log("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
                return 1
            ok = tg.photo(png, "🧪 Test card (sample company, not real numbers)\n" + caption_for(res)) is not None
            log("Telegram test sent ✔" if ok else "Telegram test FAILED - check token and chat id")
            return 0 if ok else 1
        return 0
    return run(cfg, dry_run=a.dry_run)


# --------------------------------------------------------------------------- #
# Website template (written to docs/index.html on every run)
# --------------------------------------------------------------------------- #

SITE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#f7f8f6">
<title>__TITLE__</title>
<style>
:root{--bg:#f7f8f6;--card:#fff;--ink:#111412;--muted:#6c736f;--soft:#464c48;--green:#286652;--green-lt:#e2eee7;--up:#1b804a;--down:#c43232;--line:#e2e5e2;--amber:#965a0a;--amber-lt:#ffeccd}
@media (prefers-color-scheme:dark){:root{--bg:#0f1311;--card:#171c19;--ink:#eef2ef;--muted:#9aa39e;--soft:#c3cbc6;--green:#5fbf98;--green-lt:#1d2e26;--up:#4cc483;--down:#ff7070;--line:#2a312d;--amber:#f0b45a;--amber-lt:#3a2c14}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:10px 14px 40px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 2px 8px}
.brand{font-weight:800;color:var(--green);font-size:20px;display:flex;align-items:center;gap:10px}
.brand i{width:12px;height:12px;background:var(--green);transform:rotate(45deg);display:inline-block;border-radius:2px}
.checked{font-size:12px;color:var(--muted);text-align:right}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 0 10px;border-bottom:1px solid var(--line)}
.search{width:100%;padding:11px 14px;border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--ink);font:inherit;font-size:16px}
.bar{display:flex;gap:8px;margin-top:8px;overflow-x:auto;scrollbar-width:none}
.bar::-webkit-scrollbar{display:none}
.chip{border:1px solid var(--line);background:var(--card);color:var(--soft);padding:8px 13px;border-radius:999px;font-weight:600;font-size:13px;line-height:1;white-space:nowrap;cursor:pointer;font-family:inherit}
.chip.on{background:var(--green);border-color:var(--green);color:#fff}
select{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:8px 10px;border-radius:10px;font-weight:600;font-size:13px;font-family:inherit;flex:0 0 auto}
.summary{font-size:13px;color:var(--muted);margin:12px 2px 0}
.day{font-weight:800;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--soft);margin:22px 2px 10px}
.grid{display:grid;grid-template-columns:1fr;gap:12px}
@media(min-width:780px){.grid{grid-template-columns:1fr 1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 14px 10px;min-width:0}
.top{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.pill{font-weight:700;font-size:11px;line-height:1;letter-spacing:.06em;padding:5px 9px;border-radius:999px;background:var(--green-lt);color:var(--green)}
.pill.dark{background:var(--green);color:#fff}
.pill.sme{background:var(--amber-lt);color:var(--amber)}
.when{margin-left:auto;font-size:12px;color:var(--muted)}
h2{font-size:19px;line-height:1.2;margin:9px 0 2px;font-weight:800;overflow-wrap:anywhere}
.ids{font-size:12px;color:var(--muted)}
.mc{display:flex;justify-content:space-between;align-items:center;gap:10px;background:var(--green-lt);border-radius:12px;padding:9px 12px;margin:10px 0}
.mc small{display:block;font-weight:700;font-size:10px;line-height:1;letter-spacing:.12em;color:var(--muted);margin-bottom:5px}
.mc b{font-size:18px}
.mc .g{text-align:right;font-size:12px;color:var(--soft);line-height:1.7}
.mc .g b{font-size:13px}
.up{color:var(--up)}.down{color:var(--down)}.flat,.none{color:var(--muted)}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-weight:700;font-size:10px;line-height:1.2;letter-spacing:.04em;color:var(--soft);text-align:right;padding:4px 3px 7px;border-bottom:2px solid var(--ink);white-space:nowrap}
th:first-child,td:first-child{text-align:left;padding-left:0}
td{padding:7px 3px;border-bottom:1px solid var(--line);text-align:right;font-size:13px;white-space:nowrap}
tr:last-child td{border-bottom:0}
td:first-child{font-weight:600;white-space:normal;min-width:74px}
td.c{font-weight:700}
td.p{color:var(--soft)}
td.g{font-weight:700;font-size:12px}
.foot{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--muted);margin-top:8px;gap:8px}
.foot a{color:var(--green);font-weight:700;text-decoration:none;font-size:12px}
.empty{text-align:center;color:var(--muted);padding:44px 10px}
.status{background:var(--amber-lt);color:var(--amber);border-radius:10px;padding:8px 12px;font-size:13px;margin-top:10px}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:30px;line-height:1.6}
@media(max-width:390px){td,th{padding-left:2px;padding-right:2px}td{font-size:12px}td.g{font-size:11px}h2{font-size:17px}}
</style>
</head>
<body>
<div class="wrap">
<header><div class="brand"><i></i><span id="title">__TITLE__</span></div><div class="checked" id="checked"></div></header>
<div class="controls">
<input id="q" class="search" type="search" placeholder="Search company, symbol or BSE code" autocomplete="off">
<div class="bar" id="ex"></div>
<div class="bar">
<select id="when" aria-label="Period"><option value="1">Latest day</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="0">All</option></select>
<select id="sort" aria-label="Sort"><option value="new">Newest first</option><option value="mcap">Biggest M-cap</option><option value="rev">Revenue growth</option><option value="pat">PAT growth</option></select>
<select id="trend" aria-label="Filter"><option value="">All results</option><option value="patup">PAT up YoY</option><option value="patdown">PAT down YoY</option></select>
</div>
</div>
<div id="status"></div>
<div class="summary" id="summary"></div>
<main id="list"></main>
<footer>Numbers read from NSE / BSE XBRL filings · ₹ crore<br>EBITDA excludes other income · PBT as filed (after exceptional items) · Exceptional: + gain, − loss<br>For education only · Not investment advice</footer>
</div>
<script>
const INLINE = /*__DATA__*/null;
(function () {
  "use strict";
  var $ = function (s) { return document.querySelector(s); };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var st = { ex: "All", q: "", when: "1", sort: "new", trend: "" };
  var INDEX = null, DAYS = {}, EXS = ["All", "NSE", "NSE SME", "BSE"], TZ = { timeZone: "Asia/Kolkata" };

  function indian(n) {
    n = Math.round(n);
    var s = String(Math.abs(n));
    if (s.length <= 3) return s;
    var head = s.slice(0, -3), tail = s.slice(-3), parts = [];
    while (head.length > 2) { parts.unshift(head.slice(-2)); head = head.slice(0, -2); }
    if (head) parts.unshift(head);
    return parts.join(",") + "," + tail;
  }
  function fmtDate(iso) {
    var d = new Date(iso + "T12:00:00+05:30");
    return d.toLocaleDateString("en-IN", Object.assign({ weekday: "short", day: "numeric", month: "short", year: "numeric" }, TZ));
  }
  function fmtTime(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return "";
    return d.toLocaleString("en-IN", Object.assign({ day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }, TZ));
  }
  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }
  function patRow(r) { return r.rows.filter(function (x) { return x[0] === "PAT"; })[0]; }

  function pickDates() {
    var all = (INDEX.days || []).map(function (x) { return x.date; }).sort().reverse();
    var n = +st.when;
    if (!all.length) return [];
    if (n === 0) return all;
    if (n === 1) return [all[0]];
    var latest = new Date(all[0] + "T00:00:00Z");
    return all.filter(function (d) { return (latest - new Date(d + "T00:00:00Z")) / 864e5 < n; });
  }
  function loadDays(dates) {
    if (INLINE) return Promise.resolve();
    return Promise.all(dates.filter(function (d) { return !DAYS[d]; }).map(function (d) {
      return getJSON("data/days/" + d + ".json").then(function (j) { DAYS[d] = j.results || []; })
        .catch(function () { DAYS[d] = []; });
    }));
  }
  function matches(r) {
    if (st.ex !== "All" && r.ex !== st.ex) return false;
    var p = patRow(r), dir = p ? p[5] : "none";
    if (st.trend === "patup" && dir !== "up") return false;
    if (st.trend === "patdown" && dir !== "down") return false;
    if (st.q) {
      var hay = [r.name, r.sym, r.scrip, r.isin].join(" ").toLowerCase();
      if (hay.indexOf(st.q) < 0) return false;
    }
    return true;
  }
  function card(r) {
    var rows = r.rows.map(function (x) {
      return "<tr><td>" + esc(x[0]) + "</td><td class=\"c\">" + esc(x[1]) + "</td><td class=\"p\">" + esc(x[2]) +
        "</td><td class=\"p\">" + esc(x[3]) + "</td><td class=\"g " + esc(x[5]) + "\">" + esc(x[4]) +
        "</td><td class=\"g " + esc(x[7]) + "\">" + esc(x[6]) + "</td></tr>";
    }).join("");
    var ids = [r.sym ? (r.ex === "NSE SME" ? "NSE SME: " : "NSE: ") + r.sym : "", r.scrip ? "BSE: " + r.scrip : "", r.isin]
      .filter(Boolean).map(esc).join(" · ");
    var rev = r.rows[0], pat = patRow(r);
    return "<article class=\"card\"><div class=\"top\"><span class=\"pill\">" + esc(r.per) + "</span><span class=\"pill dark\">" +
      esc(String(r.basis).toUpperCase()) + "</span>" + (r.ex === "NSE SME" ? "<span class=\"pill sme\">SME</span>" : "") +
      "<span class=\"when\">" + esc(fmtTime(r.filed)) + "</span></div>" +
      "<h2>" + esc(r.name) + "</h2><div class=\"ids\">" + ids + "</div>" +
      "<div class=\"mc\"><div><small>MARKET CAP</small><b>" + (r.mcap ? "₹" + indian(r.mcap) + " Cr" : "n/a") + "</b></div>" +
      "<div class=\"g\">" + esc(rev[0]) + " YoY <b class=\"" + esc(rev[5]) + "\">" + esc(rev[4]) + "</b><br>PAT YoY <b class=\"" +
      esc(pat ? pat[5] : "none") + "\">" + esc(pat ? pat[4] : "–") + "</b></div></div>" +
      "<div class=\"tw\"><table><thead><tr><th>₹ Cr</th><th>" + esc(r.per) + "</th><th>" + esc(r.prevLbl) + "</th><th>" +
      esc(r.yoyLbl) + "</th><th>YoY</th><th>" + (r.prevLbl ? esc(r.seq) : "") + "</th></tr></thead><tbody>" + rows +
      "</tbody></table></div><div class=\"foot\"><span>" + esc(r.ex) + (r.aud ? " · " + esc(r.aud) : "") + "</span>" +
      (r.url ? "<a href=\"" + esc(r.url) + "\" target=\"_blank\" rel=\"noopener\">Open filing ↗</a>" : "") + "</div></article>";
  }
  var renderSeq = 0;
  function render() {
    var my = ++renderSeq, dates = pickDates();
    if (!INLINE && dates.some(function (d) { return !DAYS[d]; })) $("#list").innerHTML = "<div class=\"empty\">Loading…</div>";
    return loadDays(dates).then(function () {
      if (my !== renderSeq) return;
      var items = [];
      dates.forEach(function (d) { (DAYS[d] || []).forEach(function (r) { if (matches(r)) items.push(r); }); });
      var num = function (v) { return v == null ? -Infinity : v; };
      if (st.sort === "mcap") items.sort(function (a, b) { return num(b.mcap) - num(a.mcap); });
      else if (st.sort === "rev") items.sort(function (a, b) { return num(b.revYoy) - num(a.revYoy); });
      else if (st.sort === "pat") items.sort(function (a, b) { return num(b.patYoy) - num(a.patYoy); });
      else items.sort(function (a, b) { return String(b.filed || "").localeCompare(String(a.filed || "")); });
      var up = items.filter(function (r) { var p = patRow(r); return p && p[5] === "up"; }).length;
      $("#summary").textContent = items.length ? items.length + " result" + (items.length > 1 ? "s" : "") + " · PAT up YoY in " + up : "";
      if (!items.length) {
        $("#list").innerHTML = "<div class=\"empty\">" + ((INDEX.days || []).length ? "No results match these filters." :
          "No results yet. Cards appear here as companies file results (market cap ₹" + indian(INDEX.minMcap || 700) + " Cr and above).") + "</div>";
        return;
      }
      if (st.sort === "new") {
        var groups = {};
        items.forEach(function (r) { (groups[r.date] = groups[r.date] || []).push(r); });
        $("#list").innerHTML = Object.keys(groups).sort().reverse().map(function (d) {
          return "<div class=\"day\">" + esc(fmtDate(d)) + " · " + groups[d].length + "</div><div class=\"grid\">" +
            groups[d].map(card).join("") + "</div>";
        }).join("");
      } else {
        $("#list").innerHTML = "<div class=\"grid\" style=\"margin-top:12px\">" + items.map(card).join("") + "</div>";
      }
    });
  }
  function controls() {
    $("#ex").innerHTML = EXS.map(function (x) {
      return "<button class=\"chip" + (x === st.ex ? " on" : "") + "\" data-ex=\"" + esc(x) + "\">" + (x === "NSE SME" ? "SME" : esc(x)) + "</button>";
    }).join("");
    Array.prototype.forEach.call(document.querySelectorAll("#ex .chip"), function (b) {
      b.onclick = function () { st.ex = b.getAttribute("data-ex"); controls(); render(); };
    });
  }
  var timer = null;
  $("#q").addEventListener("input", function (e) {
    clearTimeout(timer);
    timer = setTimeout(function () {
      st.q = e.target.value.trim().toLowerCase();
      if (st.q && st.when === "1") { st.when = "30"; $("#when").value = "30"; }
      render();
    }, 200);
  });
  ["when", "sort", "trend"].forEach(function (id) {
    $("#" + id).addEventListener("change", function (e) { st[id] = e.target.value; render(); });
  });

  function init(index) {
    INDEX = index || { days: [] };
    if (INDEX.title) { document.title = INDEX.title; $("#title").textContent = INDEX.title; }
    if (INDEX.checked) $("#checked").textContent = "Last checked " + fmtTime(INDEX.checked);
    var names = { nse: "NSE", nse_sme: "NSE SME", bse: "BSE" };
    var down = Object.keys(INDEX.status || {}).filter(function (k) { return !INDEX.status[k]; })
      .map(function (k) { return names[k] || k; });
    if (down.length) $("#status").innerHTML = "<div class=\"status\">" + esc(down.join(", ")) +
      " did not respond on the last check. It retries automatically.</div>";
    controls();
    render();
  }
  if (INLINE) {
    (INLINE.daysData || []).forEach(function (x) { DAYS[x.date] = x.results || []; });
    init(INLINE);
  } else {
    getJSON("data/index.json").then(init).catch(function () {
      init({ days: [] });
    });
  }
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
