"""Temporary: download today's Power Mech results PDF."""
from datetime import date
from pathlib import Path
import results_bot as rb
OUT = Path("samples"); OUT.mkdir(exist_ok=True)
http = rb.Http()
anns, ok = rb.discover_nse_announcements(http, "equities", date(2026, 11, 11))
b, ok2 = rb.discover_bse_announcements(http, date(2026, 11, 11))
for a in anns + b:
    if "POWER MECH" in a["company"].upper() or a.get("symbol") == "POWERMECH":
        print(a["company"], a["source"], a["pdf"])
        r = http.get(a["pdf"], kind="bse" if "bseindia" in a["pdf"] else "nse", referer=rb.NSE + "/", timeout=90, quiet=True)
        if r is not None and r.status_code == 200:
            (OUT / f"powermech_{a['source'].replace(' ', '')}.pdf").write_bytes(r.content)
            print("saved", len(r.content))
