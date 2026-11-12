"""Temporary: download Landmark Cars' XBRL files and results PDF."""
from datetime import date
from pathlib import Path
import results_bot as rb
OUT = Path("samples"); OUT.mkdir(exist_ok=True)
http = rb.Http()
for name, url in (("landmark_C.xml", "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1756922_12112026074856_WEB.xml"),
                  ("landmark_S.xml", "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1756910_12112026074223_WEB.xml")):
    r = http.get(url, kind="nse", referer=rb.NSE + "/", timeout=90, quiet=True)
    print(name, None if r is None else (r.status_code, len(r.content)))
    if r is not None and r.status_code == 200:
        (OUT / name).write_bytes(r.content)
anns, ok = rb.discover_nse_announcements(http, "equities", date(2026, 11, 12))
for a in anns:
    if a.get("symbol") == "LANDMARK":
        print(a["company"], a["pdf"], a["ts"])
        r = http.get(a["pdf"], kind="nse", referer=rb.NSE + "/", timeout=90, quiet=True)
        if r is not None and r.status_code == 200:
            (OUT / "landmark.pdf").write_bytes(r.content)
            print("pdf saved", len(r.content))
