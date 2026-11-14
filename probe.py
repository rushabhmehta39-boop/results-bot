"""Temporary: download results PDFs for the companies in question (12-14 Nov)."""
import json
from datetime import date
from pathlib import Path
import results_bot as rb
OUT = Path("samples"); OUT.mkdir(exist_ok=True)
http = rb.Http()
want_scrip = {"544037": "amic", "544224": "afcom", "544826": "millworks", "544150": "kpgreen", "539997": "kwality"}
want_sym = {"INDOMIM": "indomim", "FUSIONCX": "fusioncx"}
got = {}
for d in (date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 14)):
    for a in rb.discover_bse_announcements(http, d)[0]:
        n = want_scrip.get(str(a.get("scrip")))
        if n and a.get("pdf") and n not in got:
            got[n] = a
    for idx in ("equities", "sme"):
        for a in rb.discover_nse_announcements(http, idx, d)[0]:
            n = want_sym.get(a.get("symbol"))
            if n and a.get("pdf") and n not in got:
                got[n] = a
for n, a in got.items():
    r = http.get(a["pdf"], kind="bse" if "bseindia" in a["pdf"] else "nse", referer=rb.NSE + "/", timeout=120, quiet=True)
    print(n, a["company"], a["pdf"], None if r is None else (r.status_code, len(r.content)))
    if r is not None and r.status_code == 200:
        (OUT / f"{n}.pdf").write_bytes(r.content)
(OUT / "list.json").write_text(json.dumps({n: {"company": a["company"], "pdf": a["pdf"]} for n, a in got.items()}, indent=1))
