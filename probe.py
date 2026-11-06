"""Temporary check (removed after setup): one-day NSE listing for last year; BSE announcement fields."""
import json
import time
import results_bot as rb
from datetime import date

http = rb.Http()
for day in ("14-11-2025", "12-11-2025"):
    t0 = time.monotonic()
    j = http.get_json(f"{rb.NSE_API}/integrated-filing-results", "nse", tries=1, referer=rb.NSE_IF_REFERER,
                      params={"index": "equities", "from_date": day, "to_date": day, "page": 1})
    rows = rb._rows(j)
    print(f"NSE one day {day}: rows={len(rows)} total={j.get('totalCount') if isinstance(j, dict) else None} "
          f"({time.monotonic()-t0:.1f}s)", flush=True)
    if rows:
        print("  sample:", {k: rows[0].get(k) for k in ("symbol", "qe_Date", "consolidated", "xbrl")}, flush=True)
t0 = time.monotonic()
j = http.get_json(f"{rb.BSE_API}/AnnSubCategoryGetData/w", "bse", referer=rb.BSE + "/corporates/ann",
                  params={"pageno": 1, "strCat": "Result", "strPrevDate": "20251114", "strScrip": "540743",
                          "strSearch": "P", "strToDate": "20251114", "strType": "C", "subcategory": "-1"})
rows = (j or {}).get("Table", []) if isinstance(j, dict) else []
print(f"BSE ann Godrej 14-11-2025: rows={len(rows)} ({time.monotonic()-t0:.1f}s)")
for r in rows[:4]:
    print("  ", json.dumps({k: v for k, v in r.items() if v not in (None, "", 0)})[:700])
got, ok = rb.discover_bse_announcements(http, date.today())
print("BSE today result anns:", len(got))
