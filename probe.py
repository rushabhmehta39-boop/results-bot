"""Temporary check (removed after setup): how NSE's results listing pages work."""
import time
import results_bot as rb

http = rb.Http()
base = {"index": "equities", "from_date": "14-11-2025", "to_date": "14-11-2025"}
for extra in ({"page": 1}, {"page": 2}, {"page": 2, "size": 20}, {"page": 1, "size": 50}, {"page": 3, "size": 50}):
    t0 = time.monotonic()
    j = http.get_json(f"{rb.NSE_API}/integrated-filing-results", "nse", tries=1, referer=rb.NSE_IF_REFERER,
                      params={**base, **extra})
    rows = rb._rows(j)
    syms = [r.get("symbol") for r in rows]
    print(f"{extra}: rows={len(rows)} total={j.get('totalCount') if isinstance(j, dict) else None} "
          f"page={j.get('page') if isinstance(j, dict) else None} size={j.get('size') if isinstance(j, dict) else None} "
          f"first={syms[:3]} last={syms[-2:]} ({time.monotonic()-t0:.1f}s)", flush=True)
