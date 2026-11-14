"""Temporary: inspect BSE XBRL files and the BSE results feed."""
from pathlib import Path
import results_bot as rb
OUT = Path("samples"); OUT.mkdir(exist_ok=True)
http = rb.Http()
r = http.get(rb.BSE_RESULTS_RSS, "bse", referer=rb.BSE + "/corporates/Comp_Resultsnew.aspx",
             accept="application/rss+xml, application/xml, text/xml, */*")
if r is not None:
    (OUT / "rss.xml").write_bytes(r.content)
for name, url in (("afcom.xml", "https://www.bseindia.com/XBRLFILES/IFIndasDuplicateUploadDocument/Integrated_Finance_Ind_As_544224_12112026224950_IFIndAs.xml"),
                  ("kwality.xml", "https://www.bseindia.com/XBRLFILES/IFIndasDuplicateUploadDocument/Integrated_Finance_Ind_As_539997_1211202620454_IFIndAs.xml"),
                  ("amic.xml", "https://www.bseindia.com/XBRLFILES/IFOtherthanDuplicateUploadDocument/IFOtherthan_544037_12112026235127_IFOtherthan.xml")):
    x = http.get(url, "bse", referer=rb.BSE + "/", timeout=90, quiet=True)
    print(name, None if x is None else (x.status_code, len(x.content)))
    if x is not None and x.status_code == 200:
        (OUT / name).write_bytes(x.content)
