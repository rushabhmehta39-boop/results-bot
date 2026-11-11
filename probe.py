"""Temporary: download two specific results PDFs."""
from pathlib import Path
import results_bot as rb
OUT = Path("samples"); OUT.mkdir(exist_ok=True)
http = rb.Http()
for name, url in (("bannari.pdf", "https://nsearchives.nseindia.com/corporate/BANARISUG_11112026113844_ResultSept2026.pdf"),
                  ("greenpower.pdf", "https://nsearchives.nseindia.com/corporate/GREENPOWER_11112026123315_SEIntimationResults.pdf")):
    r = http.get(url, kind="nse", referer=rb.NSE + "/", timeout=90, quiet=True)
    print(name, None if r is None else (r.status_code, len(r.content)))
    if r is not None and r.status_code == 200:
        (OUT / name).write_bytes(r.content)
