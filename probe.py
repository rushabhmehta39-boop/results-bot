"""Temporary connectivity check (removed after setup). Prints which NSE/BSE services answer from here."""
import json
import re
import time
from datetime import datetime, timedelta

import results_bot as rb

http = rb.Http()
today = datetime.now(rb.IST).date()


def t(label, fn):
    t0 = time.monotonic()
    try:
        out = fn()
    except Exception as e:
        out = f"ERROR {type(e).__name__}: {str(e)[:150]}"
    print(f"\n### {label}  ({time.monotonic() - t0:.1f}s)\n{out}", flush=True)


def bse_mc(code):
    j = http.get_json(f"{rb.BSE_API}/StockTrading/w", "bse", tries=1, timeout=25, referer=rb.BSE + "/",
                      params={"flag": "", "quotetype": "EQ", "scripcode": code})
    return j if j is None else {k: j.get(k) for k in ("MktCapFull", "MktCapFF", "WAP")}


t("BSE mcap Godrej Agrovet 540743", lambda: bse_mc("540743"))
t("BSE mcap Sudarshan 506655", lambda: bse_mc("506655"))


def rss():
    r = http.get(rb.BSE_RESULTS_RSS, "bse", accept="application/xml, */*", tries=1, timeout=25)
    if r is None:
        return None
    txt = r.content.decode("utf-8", "replace")
    items = re.findall(r"<item>(.*?)</item>", txt, re.S)
    hits = [re.sub(r"\s+", " ", i)[:400] for i in items if "godrej agro" in i.lower()]
    return f"items={len(items)} godrej={hits[:3]} first={re.sub(chr(10), ' ', items[0])[:300] if items else ''}"


t("BSE results XBRL RSS", rss)


def bse_ann():
    got, ok = rb.discover_bse_announcements(http, today)
    g = [a for a in got if "godrej agro" in a["company"].lower()]
    return f"ok={ok} result-announcements={len(got)} godrej={g[:2]}"


t("BSE announcements (Result)", bse_ann)


def nse_ann():
    j = http.get_json(f"{rb.NSE_API}/corporate-announcements", "nse", tries=1, timeout=30,
                      referer=rb.NSE + "/companies-listing/corporate-filings-announcements",
                      params={"index": "equities", "from_date": today.strftime("%d-%m-%Y"),
                              "to_date": today.strftime("%d-%m-%Y")})
    rows = rb._rows(j)
    g = [{k: r.get(k) for k in ("symbol", "desc", "an_dt", "attchmntText")} for r in rows if r.get("symbol") == "GODREJAGRO"]
    res = [r for r in rows if rb.is_result_announcement(r)]
    return f"type={type(j).__name__} rows={len(rows)} result-like={len(res)} godrej={json.dumps(g)[:900]}"


t("NSE announcements today", nse_ann)


def nse_if():
    j = http.get_json(f"{rb.NSE_API}/integrated-filing-results", "nse", tries=1, timeout=30, referer=rb.NSE_IF_REFERER,
                      params={"index": "equities", "from_date": today.strftime("%d-%m-%Y"),
                              "to_date": today.strftime("%d-%m-%Y"), "page": 1})
    if not isinstance(j, dict):
        return f"type={type(j).__name__}"
    rows = j.get("data") or []
    g = [r for r in rows if r.get("symbol") == "GODREJAGRO"]
    return (f"keys={list(j.keys())} rows={len(rows)} total={j.get('totalCount')} size={j.get('size')} "
            f"rowkeys={list(rows[0].keys()) if rows else []} godrej={json.dumps(g)[:600]}")


t("NSE integrated listing today p1", nse_if)


def nse_cmp():
    j = http.get_json(f"{rb.NSE_API}/results-comparision", "nse", tries=1, timeout=30, referer=rb.NSE_FR_REFERER,
                      params={"symbol": "GODREJAGRO"})
    if not isinstance(j, dict):
        return f"type={type(j).__name__}"
    rows = j.get("resCmpData") or []
    keep = ("re_from_dt", "re_to_dt", "re_seq_num", "re_net_sale", "re_oth_inc_new", "re_pro_loss_bef_tax",
            "re_net_profit", "re_con_pro_loss", "re_res_type")
    return f"rows={len(rows)} " + json.dumps([{k: r.get(k) for k in keep} for r in rows])[:1500]


t("NSE results comparison GODREJAGRO", nse_cmp)


def bse_page():
    url = rb.BSE + "/corporates/results.aspx?Code=540743&Company=Godrej-Agrovet-Ltd&qtr=127.00&RType="
    r = http.get(url, "bse", accept="text/html,*/*", tries=1, timeout=30)
    if r is None:
        return None
    txt = r.content.decode("utf-8", "replace")
    i = txt.lower().find("revenue from operations")
    return f"len={len(txt)} has_revenue={i >= 0} snippet={re.sub(r'<[^>]+>', ' ', txt[max(0, i - 200):i + 1500])[:900] if i >= 0 else txt[:600]}"


t("BSE results page (Godrej Agrovet, Sep-25)", bse_page)


def bhav():
    out = []
    for back in range(0, 4):
        d = today - timedelta(days=back)
        for exch in ("NSE", "BSE"):
            r = http.get(rb.Prices(http, None)._url(exch, d), "nse" if exch == "NSE" else "bse",
                         accept="*/*", tries=1, timeout=30, quiet=True)
            out.append(f"{exch} {d}: {'ok ' + str(len(r.content)) if r is not None else 'no'}")
    return " | ".join(out)


t("Bhavcopy files", bhav)
print("\nNSE/BSE marked down this run:", http.down)
