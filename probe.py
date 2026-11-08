"""Temporary check (removed after): what BSE's announcement data says about results XBRL files."""
import json
import results_bot as rb

http = rb.Http()
for cat in ("Result", "-1"):
    for scrip, frm, to in (("526433", "20261107", "20261108"), ("526433", "20251101", "20251130")):
        j = http.get_json(f"{rb.BSE_API}/AnnSubCategoryGetData/w", "bse", referer=rb.BSE + "/corporates/ann",
                          params={"pageno": 1, "strCat": cat, "strPrevDate": frm, "strScrip": scrip, "strSearch": "P",
                                  "strToDate": to, "strType": "C", "subcategory": "-1"})
        rows = (j or {}).get("Table", []) if isinstance(j, dict) else []
        print(f"\n### cat={cat} scrip={scrip} {frm}-{to}: {len(rows)} rows", flush=True)
        for r in rows[:8]:
            print(json.dumps({k: v for k, v in r.items() if v not in (None, "", 0, "0")})[:900], flush=True)
