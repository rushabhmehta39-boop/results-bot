"""Temporary: collect recent results PDFs (+ XBRL numbers where filed) to build the PDF reader against."""
import json
import random
from datetime import date
from pathlib import Path

import results_bot as rb

OUT = Path("samples")
OUT.mkdir(exist_ok=True)
http = rb.Http()
days = [date(2026, 11, 7), date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)]
QE = date(2026, 9, 30)

anns = []
for d in days:
    a, ok = rb.discover_nse_announcements(http, "equities", d)
    print("nse ann", d, len(a), ok)
    anns += a
    b, ok = rb.discover_bse_announcements(http, d)
    print("bse ann", d, len(b), ok)
    anns += b

xb = {}
fs, ok = rb.discover_nse(http, "equities", date(2026, 11, 6), date(2026, 11, 11))
print("nse filings", len(fs), ok)
for f in fs:
    if f.period_end == QE and f.xbrl_url:
        xb.setdefault(("N", f.symbol), []).append(f)
bs, ok = rb.discover_bse(http)
print("bse filings", len(bs), ok)
for f in bs:
    if f.xbrl_url:
        xb.setdefault(("B", f.scrip), []).append(f)


def key_of(a):
    return ("N", a["symbol"]) if a.get("symbol") else ("B", a.get("scrip", ""))


seen, with_x, without = set(), [], []
for a in anns:
    if not a.get("pdf"):
        continue
    k = key_of(a)
    if k in seen:
        continue
    seen.add(k)
    (with_x if k in xb else without).append(a)
random.seed(7)
random.shuffle(with_x)
random.shuffle(without)
pick = with_x[:60] + without[:20]
pick += [a for a in without if "500041" in str(a.get("scrip")) or "BANARI" in str(a.get("symbol"))][:1]
print("picked", len(pick), "with xbrl", len(with_x), "without", len(without))

manifest = []
for i, a in enumerate(pick):
    url = a["pdf"]
    kind = "bse" if "bseindia" in url else "nse"
    r = http.get(url, kind=kind, referer=(rb.BSE + "/") if kind == "bse" else rb.NSE + "/", timeout=60, quiet=True)
    if r is None or r.status_code != 200 or not r.content.startswith(b"%PDF") or len(r.content) > 9_000_000:
        print("  skip pdf", url, None if r is None else (r.status_code, len(r.content)))
        continue
    name = f"{i:03d}_{(a.get('symbol') or a.get('scrip') or 'x')}.pdf".replace("/", "_")
    (OUT / name).write_bytes(r.content)
    truth = {}
    for f in xb.get(key_of(a), []):
        rx = http.get(f.xbrl_url, kind="bse" if f.source == "BSE" else "nse",
                      referer=(rb.BSE + "/") if f.source == "BSE" else rb.NSE + "/", timeout=60, quiet=True)
        if rx is None or rx.status_code != 200:
            continue
        try:
            p = rb.parse_xbrl(rx.content)
        except Exception as e:  # noqa: BLE001
            print("  xbrl parse fail", e)
            continue
        basis = "C" if "consol" in str(p["info"].get("NatureOfReportStandaloneConsolidated", "")).lower() else "S"
        truth[basis] = {"periods": p["periods"], "kind": p.get("kind"), "url": f.xbrl_url}
    manifest.append({"file": name, "company": a["company"], "symbol": a.get("symbol"), "scrip": a.get("scrip"),
                     "isin": a.get("isin"), "source": a["source"], "pdf": url, "cons": a.get("cons"),
                     "ts": a["ts"].isoformat() if a.get("ts") else None, "truth": truth})
    print("  ok", name, len(r.content), list(truth))
(OUT / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str))
print("saved", len(manifest))
