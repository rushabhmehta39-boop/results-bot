"""Temporary check (removed after setup): NSE listing page size and last year's filing count."""
import time
import results_bot as rb

http = rb.Http()
for size in (100, 500):
    t0 = time.monotonic()
    j = http.get_json(f"{rb.NSE_API}/integrated-filing-results", "nse", tries=1, referer=rb.NSE_IF_REFERER,
                      params={"index": "equities", "from_date": "01-10-2025", "to_date": "31-10-2025", "page": 1, "size": size})
    rows = rb._rows(j)
    print(f"size={size}: got {len(rows)} rows, size field={j.get('size') if isinstance(j, dict) else None}, "
          f"totalCount={j.get('totalCount') if isinstance(j, dict) else None} ({time.monotonic()-t0:.1f}s)", flush=True)
    if rows:
        print("  sample:", {k: rows[0].get(k) for k in ("symbol", "qe_Date", "consolidated", "creation_Date", "xbrl")})
for frm, to in (("01-11-2025", "30-11-2025"), ("01-12-2025", "31-12-2025")):
    t0 = time.monotonic()
    j = http.get_json(f"{rb.NSE_API}/integrated-filing-results", "nse", tries=1, referer=rb.NSE_IF_REFERER,
                      params={"index": "equities", "from_date": frm, "to_date": to, "page": 1, "size": 100})
    print(f"{frm}..{to}: totalCount={j.get('totalCount') if isinstance(j, dict) else None} ({time.monotonic()-t0:.1f}s)", flush=True)
j = http.get_json(f"{rb.NSE_API}/corporates-financial-results", "nse", tries=1, referer=rb.NSE_FR_REFERER,
                  params={"index": "equities", "period": "Quarterly", "from_date": "01-10-2025", "to_date": "31-10-2025"})
rows = rb._rows(j)
print("legacy Oct-2025 rows:", len(rows), "with xbrl:", sum(1 for r in rows if rb._clean_url(r.get("xbrl"))))
