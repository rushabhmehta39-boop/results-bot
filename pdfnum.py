"""Read quarterly results numbers from a results PDF (SEBI format), with arithmetic cross-checks.

read_results_pdf(content, qe) -> {"ok": True, "basis": "C"|"S", "unit": str, "page": int,
                                  "cols": {"cur": m, "prev": m, "yoy": m}}   (m = metrics in Rs crore)
                               or {"ok": False, "why": str}
Only returns numbers when every check passes for all three columns; anything doubtful -> not ok.
"""
from __future__ import annotations

import calendar
import io
import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_PATS = [
    re.compile(r"(?<![\d.])(\d{1,2})\s?[./-]\s?(\d{1,2})\s?[./-]\s?(\d{4}|\d{2})(?![\d])"),           # 30.09.2026
    re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s,.'-]*" + _MON + r"(?:[\s,.'-]*(\d{4})|[-'’]\s?(\d{2}))(?![\d,])", re.I),
    re.compile(_MON + r"[\s.]*(\d{1,2})(?:st|nd|rd|th)?[\s,.'-]*(\d{4})(?!\d)", re.I),               # Sep 30, 2026
    re.compile(r"(?<![a-z])" + _MON + r"(?:[\s'’.,-]*(\d{4})|[-'’]\s?(\d{2}))(?![\d,])", re.I),             # Sep-26
    re.compile(r"\bq\s?([1-4])\s?[-']?\s?fy\s?'?(\d{4}|\d{2})(?:\s?-\s?(\d{2,4}))?", re.I),              # Q2 FY27
]
UNIT_PAT = re.compile(
    r"(?:₹|rs\.?|inr|rupees|amount|amounts|figures|all)\s*[`'\"]?\s*(?:in\s*)?(?:₹|rs\.?|inr|rupees)?\s*\.?\s*"
    r"(lakhs?|lacs?|lakh|crores?|crs?\.?|millions?|mn|mio|thousands?|'000|000s|billions?|bn)\b"
    r"|\(\s*in\s*(lakhs?|lacs?|crores?|crs?\.?|millions?|mn|thousands?|billions?)\s*\)"
    r"|\bin\s*(lakhs?|lacs?|crores?|millions?|thousands?|billions?)\b", re.I)
UNIT_RUPEES = re.compile(r"(?:amount|amounts|figures|all amounts?)\s*(?:are\s*)?in\s*(?:₹|rs\.?|inr|rupees)\s*[)\]]?"
                         r"(?!\s*(?:lakh|lac|crore|cr|million|mn|thousand|billion))", re.I)
FACTOR = {"lakh": 0.01, "crore": 1.0, "million": 0.1, "thousand": 0.0001, "billion": 100.0}


def _unit_word(w: str) -> str | None:
    w = w.lower().strip(". ")
    if w.startswith(("lakh", "lac")):
        return "lakh"
    if w.startswith(("crore", "cr")):
        return "crore"
    if w.startswith(("million", "mn", "mio")):
        return "million"
    if w.startswith(("thousand", "'000", "000")):
        return "thousand"
    if w.startswith(("billion", "bn")):
        return "billion"
    return None


def month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _year(s: str) -> int:
    y = int(s)
    return y + 2000 if y < 100 else y


_OCR_DIGIT = str.maketrans({"O": "0", "o": "0", "Q": "0", "D": "0", "l": "1", "I": "1", "|": "1", "i": "1",
                            "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2"})


def parse_num(t: str):
    """'1,234.56' -> (1234.56, 2); '(12.5)' -> (-12.5, 1); '-' -> (0.0, 0); else None."""
    t = t.strip().strip("*#^@$").replace("−", "-").replace("–", "-").replace("—", "-")
    if t in ("-", "--", "---", "nil", "Nil", "NIL", "-0", "(-)", "=", "_", "—"):
        return 0.0, 0
    if re.search(r"[A-Za-z|]", t):
        letters = len(re.findall(r"[A-Za-z|]", t))
        digits = len(re.findall(r"\d", t))
        if letters > 1 or digits < 2:
            return None
        t = t.translate(_OCR_DIGIT)  # scanned PDFs: one misread character, e.g. "Q.11", "1,2O4"
    t = t.rstrip("'’")
    if t.startswith("(") and not t.endswith(")"):
        t += ")"  # scans often lose the closing bracket
    elif t.endswith(")") and not t.startswith("("):
        t = "(" + t
    m = re.fullmatch(r"(\()?(-)?(\d+),(\d{1,2})(\))?", t)
    if m and bool(m.group(1)) == bool(m.group(5)):  # "45,14": a decimal comma
        v = float(f"{m.group(3)}.{m.group(4)}")
        return (-v if m.group(1) or m.group(2) else v), len(m.group(4))
    m = re.fullmatch(r"(\()?(-)?(\d{1,3}(?:,\d{2})*,\d{3}|\d{1,3}(?:,\d{3})+|\d+)?(\.\d+)?(\))?", t)
    if not m or not (m.group(3) or m.group(4)) or bool(m.group(1)) != bool(m.group(5)):
        return None
    v = float((m.group(3) or "0").replace(",", "") + (m.group(4) or ""))
    if m.group(1) or m.group(2):
        v = -v
    return v, len(m.group(4)) - 1 if m.group(4) else 0


def _is_alpha(t: str) -> bool:
    return bool(re.search(r"[A-Za-z]{2,}", t))


def _lines(words: list[dict]) -> list[list[dict]]:
    """Group words into text lines by vertical position."""
    ws = sorted(words, key=lambda w: ((w["top"] + w["bottom"]) / 2, w["x0"]))
    lines, cur, cy = [], [], None
    for w in ws:
        y = (w["top"] + w["bottom"]) / 2
        h = max(4.0, w["bottom"] - w["top"])
        if cur and abs(y - cy) > 0.45 * h:
            lines.append(sorted(cur, key=lambda w: w["x0"]))
            cur = []
        if not cur:
            cy = y
        cur.append(w)
    if cur:
        lines.append(sorted(cur, key=lambda w: w["x0"]))
    return lines


VOCAB = ("revenue operations other income total expenses expenditure finance costs cost depreciation amortisation "
         "amortization exceptional items profit loss before after tax taxes current deferred period quarter year "
         "comprehensive share associates associate joint ventures venture net sales interest attributable owners "
         "controlling continuing discontinued earlier years expense charges borrowing particulars standalone "
         "consolidated provision results result half from for the and of").split()
_FUZZ: dict = {}


_SHORT_OCR = {"tnx": "tax", "trx": "tax", "tox": "tax", "tex": "tax", "iax": "tax", "lax": "tax", "md": "and",
              "hss": "loss", "ioss": "loss", "lcss": "loss"}


def _fix_word(w: str) -> str:
    if w in _SHORT_OCR:
        return _SHORT_OCR[w]
    if len(w) < 4 or w in VOCAB:
        return w
    if w not in _FUZZ:
        import difflib
        m = difflib.get_close_matches(w, VOCAB, n=1, cutoff=0.8)
        _FUZZ[w] = m[0] if m and abs(len(m[0]) - len(w)) <= 2 else w
    return _FUZZ[w]


def _norm_label(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z ]+", " ", s)
    s = " ".join(_fix_word(w) for w in s.split())
    s = re.sub(r"\b(i{1,3}|iv|v|vi{1,3}|ix|x|xi{1,3}|xiv|xv|xvi{0,3}|xix|xx)\b", " ", s)  # roman indices
    s = re.sub(r"\b[a-h]\b", " ", s)  # (a) (b) indices
    return re.sub(r"\s+", " ", s).strip()


def _find_dates(line: list[dict]):
    """Dates in a line with the x-centre of each: [(date|('Q', q, fy), x)]."""
    text, spans, pos = "", [], 0
    for w in line:
        if text:
            text += " "
            pos += 1
        spans.append((pos, pos + len(w["text"]), w))
        text += w["text"]
        pos += len(w["text"])
    out, taken = [], []
    for i, pat in enumerate(DATE_PATS):
        for m in pat.finditer(text):
            if any(m.start() < b and m.end() > a for a, b in taken):
                continue
            try:
                if i == 0:
                    d, mo, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
                    if not (1 <= mo <= 12 and 1 <= d <= 31):
                        continue
                    dt = date(y, mo, d)
                elif i == 1:
                    dt = date(_year(m.group(3) or m.group(4)), MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
                elif i == 2:
                    dt = date(_year(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
                elif i == 3:
                    dt = month_end(_year(m.group(2) or m.group(3)), MONTHS[m.group(1).lower()[:3]])
                else:
                    q, fy = int(m.group(1)), _year(m.group(2))
                    if m.group(3):
                        fy = _year(m.group(3)) if len(m.group(3)) in (2, 4) else fy
                        if fy < _year(m.group(2)):
                            fy = _year(m.group(2)) + 1
                    y = fy - 1 if q < 4 else fy
                    dt = month_end(y, {1: 6, 2: 9, 3: 12, 4: 3}[q])
            except ValueError:
                continue
            ws = [w for a, b, w in spans if a < m.end() and b > m.start()]
            if not ws:
                continue
            taken.append((m.start(), m.end()))
            out.append((dt, (min(w["x0"] for w in ws) + max(w["x1"] for w in ws)) / 2))
    return out


def _cluster(xs: list[float], gap: float) -> list[list[float]]:
    xs = sorted(xs)
    groups = []
    for x in xs:
        if groups and x - groups[-1][-1] <= gap:
            groups[-1].append(x)
        else:
            groups.append([x])
    return groups


ROW_RULES = [  # key, must match, must not match
    ("rev_total", r"\btotal (net )?revenue from operations\b", r"other"),
    ("rev", r"\b(net )?revenue from operations\b|\b(net )?income from operations\b|\bnet sales\b|\bsales income from operations\b",
     r"\bother\b|\btotal income\b|\bexpenses?\b"),
    ("oi", r"\bother (operating )?income\b", r"comprehensive|\btotal\b|revenue from operations"),
    ("ti", r"\btotal income\b|\btotal revenue\b", r"comprehensive|from operations|\bother\b|expenses?"),
    ("te", r"\btotal expenses\b|\btotal expenditure\b", r"\btax\b"),
    ("fin", r"\bfinance costs?\b|\binterest expenses?\b|\bfinance charges\b|\binterest and finance\b|\bborrowing costs?\b|^interest$",
     r"income|profit"),
    ("dep", r"\bdepreciation\b", r"profit"),
    ("jv", r"\bshare (in|of) (the )?(net )?(profit|loss|results?|income)\b", r"comprehensive|non controlling|minority|owners|\bbefore\b"),
    ("pbe", r"\b(profit|loss|earnings)\b.*\bbefore\b.*\bexceptional\b", r"comprehensive"),
    ("exc", r"\bexceptional\b", r"\bbefore\b|\bafter\b|\bprofit\b|\bloss\b(?! on)"),
    ("pbt", r"\b(profit|loss)\b.*\bbefore (income )?tax(ation)?\b", r"\bbefore\b.*\bexceptional\b|comprehensive|share of"),
    ("tax_total", r"\btotal tax\b|\btotal income tax\b|\btax expenses? total\b", r"comprehensive|before|after"),
    ("tax", r"^(income )?tax (expenses?|expense credit|credit expense)\b|^tax$|^income tax$|^provision for tax|\btax expenses?\b(?!.*profit)",
     r"comprehensive|before|after|deferred tax (assets?|liabilit)|relating to items|\bprofit\b|\bother\b"),
    ("tax_cur", r"^current( income)? tax\b|^current year tax\b", r"comprehensive"),
    ("tax_def", r"^deferred tax\b|^deferred tax (charge|credit)", r"comprehensive|items"),
    ("tax_old", r"\b(earlier|prior|previous) (years?|periods?)\b|\bshort excess provision\b|\bexcess short provision\b",
     r"comprehensive"),
    ("pat_cont", r"\b(profit|loss)\b.*\bfrom continuing operations\b", r"discontinued|\bbefore\b|comprehensive|\btax expense"),
    ("pat", r"\b(net )?(profit|loss|income)\b.*\bfor the\b|\b(net )?(profit|loss|income)\b.*\b(for|after tax for) the (period|quarter|year|half)\b|\b(net )?(profit|loss)\b.*\bafter tax(es|ation)?\b",
     r"comprehensive|attributable|\bowners\b|non controlling|minority|\bbefore\b|continuing|discontinued|"
     r"equity holders|share of|\bper share\b|earnings per"),
]
_ROW_RE = [(k, re.compile(a), re.compile(b) if b else None) for k, a, b in ROW_RULES]


COMPACT_RULES = [  # scans run words together: "revenuefrom operations", "profit for theyear"
    ("rev", r"revenuefromoperations|incomefromoperations", r"other|totalincome|expense"),
    ("oi", r"^otherincome|^otheroperatingincome", r"comprehensive"),
    ("ti", r"^totalincome|^totalrevenue$", r"comprehensive|fromoperations"),
    ("te", r"^totalexpen", r"tax"),
    ("fin", r"^financecosts?|^financecharges|^interestexpenses?", r"income"),
    ("dep", r"depreciation", r"profit"),
    ("pbe", r"(profit|loss).*beforeexceptional", r"comprehensive"),
    ("exc", r"^exceptional", r"before|after|profit"),
    ("pbt", r"(profit|loss).*beforetax", r"exceptional|comprehensive|shareof"),
    ("tax_total", r"^totaltax", r"comprehensive"),
    ("tax", r"^taxexpenses?$|^incometaxexpenses?$", r"comprehensive"),
    ("pat", r"(profit|loss).*(forthe(period|year|quarter|half)|aftertax)",
     r"comprehensive|attributable|owners|controlling|before|continuing|discontinued|pershare"),
]
_COMPACT_RE = [(k, re.compile(a), re.compile(b) if b else None) for k, a, b in COMPACT_RULES]


def _classify(label: str) -> str | None:
    for k, inc, exc in _ROW_RE:
        if inc.search(label) and not (exc and exc.search(label)):
            return k
    c = label.replace(" ", "")
    for k, inc, exc in _COMPACT_RE:
        if inc.search(c) and not (exc and exc.search(c)):
            return k
    return None


def _quarter_cols(col_dates: list, qe: date):
    """Which column is this quarter, last quarter, same quarter last year (leftmost match of each)."""
    def first(d):
        for i, ds in enumerate(col_dates):
            if d in ds:
                return i
        return None
    pq = qe.replace(day=1)
    pm, py = (pq.month - 3, pq.year) if pq.month > 3 else (pq.month + 9, pq.year - 1)
    prev = month_end(py, pm)
    yoy = month_end(qe.year - 1, qe.month)
    return first(qe), first(prev), first(yoy)


def _expected(qe: date, n: int):
    """Standard SEBI column order for a results table with n columns ending at quarter qe."""
    def back(d, months):
        y, m = d.year, d.month - months
        while m <= 0:
            m += 12
            y -= 1
        return month_end(y, m)
    prev, yoy = back(qe, 3), back(qe, 12)
    fy_end = month_end(qe.year if qe.month >= 4 else qe.year - 1, 3)  # last completed year end
    if qe.month in (9, 12):
        full = [qe, prev, yoy, qe, yoy, fy_end if qe.month != 3 else back(qe, 12)]
    elif qe.month == 6:
        full = [qe, prev, yoy, fy_end]
    else:  # March quarter: quarter, Dec quarter, last year's March quarter, this year, last year
        full = [qe, prev, yoy, qe, yoy]
    return full if n == len(full) else None


def _merge_split_numbers(ln: list[dict]) -> list[dict]:
    """Scans split '10,298' into '10' '298': join number pieces that touch."""
    out = []
    for w in ln:
        if out and re.fullmatch(r"[\d,.()\-]+", out[-1]["text"]) and re.fullmatch(r"[\d,.()\-]+", w["text"]) \
                and w["x0"] - out[-1]["x1"] <= 2.2 and not out[-1]["text"].endswith(")"):
            prev = out.pop()
            out.append(dict(w, text=prev["text"] + w["text"], x0=prev["x0"]))
            continue
        if out and re.fullmatch(r"\(?-?\d{1,3}", out[-1]["text"]) and re.fullmatch(r"\d{3}(?:[.,]\d+)?\)?", w["text"]) \
                and 0 <= w["x0"] - out[-1]["x1"] <= 4.5:
            prev = out.pop()
            w = dict(w, text=prev["text"] + "," + w["text"], x0=prev["x0"])
        out.append(w)
    return out


def _table(lines, qe: date, page_text_lower: str):
    """Parse one results table from a page's lines. Returns (candidates per basis group, error)."""
    rows = []
    stmt_top = None  # the statement starts at the revenue / income line
    for ln in lines:
        lab = _norm_label(" ".join(w["text"] for w in ln if _is_alpha(w["text"])))
        if _classify(lab) in ("rev", "rev_total", "oi", "ti") and any(parse_num(w["text"]) for w in ln[-3:]):
            stmt_top = ln[0]["top"]
            break
    for ln in lines:
        ln = _merge_split_numbers(ln)
        dates = _find_dates(ln)
        alpha_idx = [i for i, w in enumerate(ln) if _is_alpha(w["text"])]
        start = alpha_idx[0] + 1 if alpha_idx else 0
        nums = []
        if not dates:
            for w in ln[start:]:
                pn = parse_num(w["text"])
                if pn is not None:
                    nums.append((w["x1"], (w["x0"] + w["x1"]) / 2, pn[0], pn[1], w))
        label_end = nums[0][4]["x0"] if nums else 1e9
        label = " ".join(w["text"] for i, w in enumerate(ln)
                         if w["x1"] <= label_end + 1 and not (i >= start and parse_num(w["text"]) is not None))
        # a line of only days / years / column numbers ("30 30 30", "2026 2026 2025", "(1) (2) (3)") is heading
        headish = (bool(nums) and (stmt_top is None or ln[0]["top"] < stmt_top - 1)
                   and all(re.fullmatch(r"\(?\d{1,4}\)?\*?", w["text"]) and (v <= 31 or 1990 <= abs(v) <= 2100)
                           for _, _, v, dec, w in nums))
        if headish:
            nums = []
        rows.append({"ln": ln, "nums": nums, "label": label, "top": ln[0]["top"], "dates": dates,
                     "h": max(w["bottom"] - w["top"] for w in ln), "headish": headish})
    data = [r for r in rows if len(r["nums"]) >= 3]
    if len(data) < 6:
        return [], "too few number lines"
    xs = [n[0] for r in data for n in r["nums"]]
    groups = _cluster(xs, 9.0)
    need = max(3, int(0.3 * len(data)))
    cols = [sorted(g)[len(g) // 2] for g in groups if len(g) >= need]
    if len(cols) < 3:
        return [], f"columns unclear ({len(cols)})"
    gaps = [b - a for a, b in zip(cols, cols[1:])]
    tol = max(10.0, 0.4 * min(gaps)) if gaps else 15.0
    for r in rows:
        vals = {}
        for x1, xc, v, dec, w in r["nums"]:
            j = min(range(len(cols)), key=lambda j: abs(cols[j] - x1))
            if abs(cols[j] - x1) <= tol and j not in vals:
                vals[j] = (v, dec)
        r["vals"] = vals
    # tilted scans: the right-hand numbers of a row land on a line of their own just below it
    merged = []
    for r in rows:
        p = merged[-1] if merged else None
        if (p is not None and r["vals"] and p["vals"] and not _is_alpha(r["label"])
                and not set(r["vals"]) & set(p["vals"]) and r["top"] - p["top"] < 1.3 * max(p["h"], r["h"])
                and min(r["vals"]) > max(p["vals"])):
            p["vals"].update(r["vals"])
            continue
        merged.append(r)
    rows = merged
    first_data = min(r["top"] for r in rows if len(r["vals"]) >= 3)
    header = [r for r in rows if r["top"] < first_data]
    split = None
    for r in header:
        st = [w for w in r["ln"] if _fix_word(re.sub(r"[^a-z]", "", w["text"].lower())) == "standalone"]
        co = [w for w in r["ln"] if _fix_word(re.sub(r"[^a-z]", "", w["text"].lower())) == "consolidated"]
        if st and co and abs(st[0]["x0"] - co[0]["x0"]) > 120:
            sx, cx = (st[0]["x0"] + st[0]["x1"]) / 2, (co[0]["x0"] + co[0]["x1"]) / 2
            split = ((sx + cx) / 2, "S" if sx < cx else "C", "C" if sx < cx else "S")
            break
    x0s = [[] for _ in cols]
    for r in rows:
        for x1, xc, v, dec, w in r["nums"]:
            j = min(range(len(cols)), key=lambda j: abs(cols[j] - x1))
            if abs(cols[j] - x1) <= tol:
                x0s[j].append(w["x0"])
    centres = [((sorted(a)[len(a) // 2] if a else c - 25) + c) / 2 for a, c in zip(x0s, cols)]
    half = 0.6 * min(gaps) if gaps else 30.0
    col_words = [[] for _ in cols]
    for r in header[-18:]:
        for w in r["ln"]:
            xc = (w["x0"] + w["x1"]) / 2
            j = min(range(len(cols)), key=lambda j: abs(centres[j] - xc))
            if abs(centres[j] - xc) <= half:
                col_words[j].append(w)
    lo, hi = qe.year - 2, qe.year + 1
    col_dates = []
    for ws in col_words:
        ws = [w for ln in _lines(ws) for w in ln]  # line by line, left to right
        found = [d for d, _ in _find_dates(ws) if lo <= d.year <= hi]
        col_dates.append(found[:1])
    if not any(col_dates):  # fall back on dates read line by line
        for r in header[-25:]:
            for dt, x in r["dates"]:
                j = min(range(len(cols)), key=lambda j: abs(cols[j] - x))
                if -tol * 2.2 <= x - cols[j] <= tol and lo <= dt.year <= hi and not col_dates[j]:
                    col_dates[j].append(dt)
    head_text = " ".join(w["text"] for r in header for w in r["ln"]).lower()
    if split:
        parts = [(split[1], [j for j, x in enumerate(cols) if x < split[0]]),
                 (split[2], [j for j, x in enumerate(cols) if x >= split[0]])]
    else:
        parts = [(None, list(range(len(cols))))]
    out = []
    for basis, idxs in parts:
        cd = [col_dates[j] if j in idxs else [] for j in range(len(cols))]
        c, pq, y = _quarter_cols(cd, qe)
        how = "dates"
        if c is None or pq is None or y is None:
            # fall back on the standard column order, if every date we could read agrees with it
            exp = _expected(qe, len(idxs))
            seen = {k: set(col_dates[j]) for k, j in enumerate(idxs) if col_dates[j]}
            if exp and seen and all(ds == {exp[k]} for k, ds in seen.items()) and \
                    (len([k for k in seen if k < 3]) >= 2 or len(seen) >= 3):
                c, pq, y = idxs[0], idxs[1], idxs[2]
                how = "position"
            else:
                out.append({"basis": basis, "why": f"column dates unclear {[sorted(set(map(str, d))) for d in cd]}"})
                continue
        if c != idxs[0] or not (c < pq < y):
            out.append({"basis": basis, "why": f"unusual column order {c, pq, y}"})
            continue
        # September quarter: this quarter + last quarter must add up to the half-year column
        h1 = idxs[3] if len(idxs) == 6 and qe.month == 9 and col_dates[idxs[3]] in ([], [qe]) else None
        out.append({"basis": basis, "cols": (c, pq, y), "how": how, "rows": rows, "ncols": len(cols),
                    "head_text": head_text, "h1": h1})
    return out, None


def _extract(rows, cidx):
    """Pick the statement rows (first occurrence, in order) and their values for one column."""
    found, order = {}, []
    labels = []
    prev_label = ""
    for i, r in enumerate(rows):
        own = _norm_label(r["label"])
        if not r["vals"]:
            prev_label = own if own else prev_label
            continue
        lab = own
        k = _classify(lab) if lab else None
        if k is None and prev_label:
            lab2 = (prev_label + " " + own).strip()
            k = _classify(lab2)
            if k:
                lab = lab2
        if k is None and i + 1 < len(rows) and not rows[i + 1]["vals"]:
            lab3 = (own + " " + _norm_label(rows[i + 1]["label"])).strip()
            k3 = _classify(lab3)
            if k3:
                k, lab = k3, lab3
        prev_label = ""
        labels.append((lab, k))
        if k and k not in found:
            if k in ("pat", "pat_cont") and "pbt" not in found and "pbe" not in found:
                continue  # a "profit for the period" line before PBT is not the bottom line
            if k in ("rev", "rev_total", "oi", "ti") and "pbt" in found:
                continue
            found[k] = r["vals"]
            order.append(k)
        if "pat" in found:
            break
    out = {}
    for k, vals in found.items():
        out[k] = vals.get(cidx)
    if out.get("pat") is None and out.get("pat_cont") is not None:
        out["pat"] = out["pat_cont"]  # no discontinued operations line: continuing = the bottom line
    return out, order, labels


def _check_col(v: dict, dec: int):
    """Arithmetic checks for one column (values in PDF units). Returns (metrics, problems)."""
    g = lambda k: None if v.get(k) is None else v[k][0]  # noqa: E731
    probs = []
    unit_tol = 2 * (10 ** -dec) if dec else 2.0

    def close(a, b, terms=3):
        return abs(a - b) <= max(unit_tol * terms, 0.004 * max(abs(a), abs(b)))
    rev = g("rev_total") if g("rev_total") is not None else g("rev")
    oi, ti, te = g("oi"), g("ti"), g("te")
    fin, dep, jv = g("fin"), g("dep"), g("jv")
    pbe, exc, pbt, pat = g("pbe"), g("exc"), g("pbt"), g("pat")
    if rev is None and ti is not None and oi is not None:
        rev = ti - oi  # revenue line unreadable: total income - other income
    if pbt is None and pbe is not None:
        pbt = pbe + (exc or 0)
    if rev is None or pbt is None or pat is None:
        return None, ["revenue/PBT/PAT row missing"]
    taxes = [g("tax_total"), g("tax")]
    if g("tax_cur") is not None:
        taxes += [g("tax_cur") + (g("tax_def") or 0) + (g("tax_old") or 0), g("tax_cur") + (g("tax_def") or 0)]
    taxes = [t for t in taxes if t is not None]
    taxes += [-t for t in taxes if t]  # some statements show tax as a deduction, e.g. "(18.71)"
    tax = next((t for t in taxes if close(pbt - t, pat, 4)), taxes[0] if taxes else None)
    if oi is None and ti is not None:
        oi = ti - rev
    if oi is None:
        return None, ["other income / total income missing"]
    if ti is None:
        ti = rev + oi
    elif not close(rev + oi, ti):
        probs.append(f"revenue {rev} + other income {oi} != total income {ti}")
    if tax is None:
        return None, ["tax row missing"]
    if not close(pbt - tax, pat, 4):
        probs.append(f"PBT {pbt} - tax {tax} != PAT {pat}")
    if te is None:
        if pbe is None:
            return None, ["total expenses missing"]
        te = ti - pbe + (jv or 0) * 0  # no total line: expenses = income - profit before exceptional items
    # operating profit before exceptional items, with or without the share of associates / JVs
    op = ti - te
    if pbe is not None:
        ok = close(op, pbe) or (jv is not None and close(op + jv, pbe))
        if not ok:
            probs.append(f"total income - expenses {op:.2f} != profit before exceptional {pbe}")
        e2 = pbt - pbe
        if exc is not None and not close(e2, exc) and not (jv is not None and close(e2, exc + jv)):
            probs.append(f"PBT - PBE {e2:.2f} != exceptional {exc}")
    else:
        cands = [op + (exc or 0), op + (exc or 0) + (jv or 0)]
        if not any(close(c, pbt, 4) for c in cands):
            probs.append(f"total income - expenses (+ exceptional) {cands[0]:.2f} != PBT {pbt}")
        pbe = pbt - (exc or 0)
        if jv is not None and close(op + (exc or 0) + jv, pbt, 4):
            pbe = pbt - (exc or 0)
    if fin is None:
        fin = 0.0
    if dep is None:
        dep = 0.0
    m = {"revenue": rev, "other_income": oi, "total_income": ti, "total_expenses": te, "finance": fin,
         "depreciation": dep, "pbe": pbe if pbe is not None else pbt - (exc or 0), "pbt": pbt, "tax": tax, "pat": pat}
    if jv is not None:
        # share of associates / JVs sits between expenses and PBT: keep it out of EBITDA but inside PBE
        m["pbe"] = pbe
    return m, probs


_UNIT_FUZZY = [("crore", re.compile(r"cror|\bcrs?\b\.?")),
               ("lakh", re.compile(r"lakh|\blacs?\b|\blakhs?\b|\blacs\b|la[ck]hs")),
               ("million", re.compile(r"million|milion|mill?ions|mi[lw1i|]{1,3}[oi]?ons?|m[il1|]{2,4}ion|\bmn\b|\bmio\b|rllrons")),
               ("thousand", re.compile(r"thousand|\b'?000s?\b\)")),
               ("billion", re.compile(r"billion|\bbn\b"))]


def _unit(text: str):
    m = UNIT_PAT.search(text)
    if m:
        w = next(g for g in m.groups() if g)
        return _unit_word(w)
    found = {u for u, rx in _UNIT_FUZZY if rx.search(text)}
    if len(found) == 1 and re.search(r"₹|\brs\b|\brs\.|\binr\b|rupee|\bin\b|amount|r\s?in|\(", text):
        return found.pop()
    if UNIT_RUPEES.search(text):
        return "rupee"
    return None


def _matches_known(cols: dict, fac: float, kn: dict) -> tuple[int, list]:
    """How many known figures (from the company's earlier XBRL filings) the PDF columns agree with."""
    agree, probs = 0, []
    for name in ("prev", "yoy"):
        k = kn.get(name) or {}
        for key, rel in (("revenue", 0.01), ("pat", 0.02)):
            x, y = k.get(key), cols[name].get(key)
            if x is None or y is None:
                continue
            y *= fac
            if abs(x - y) <= max(rel * abs(x), 0.06):
                agree += 1
            else:
                probs.append(f"{name} {key} {y:.2f} != earlier filing {x:.2f}")
    return agree, probs


def read_results_pdf(content: bytes, qe: date, known: dict | None = None, max_pages: int = 30,
                     time_limit: float = 45.0) -> dict:
    """known: {"C"/"S": {"prev": metrics, "yoy": metrics}} in Rs crore, from earlier XBRL filings (optional).
    PDF columns must agree with it when given; it also settles an unstated unit or basis."""
    import time as _time
    try:
        import pdfplumber
    except ImportError:
        return {"ok": False, "why": "pdfplumber not installed"}
    t0 = _time.monotonic()
    known = known or {}
    reasons, good = [], []
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"cannot open PDF: {type(e).__name__}"}
    with pdf:
        doc_text = []
        npages = len(pdf.pages)
        any_text = False
        for pno, page in enumerate(pdf.pages[:max_pages], 1):
            if _time.monotonic() - t0 > time_limit:
                reasons.append(f"stopped after {pno - 1} pages (time limit)")
                break
            try:
                if len(page.chars) > 25000:
                    continue
                words = page.extract_words(x_tolerance=2.5, y_tolerance=2, keep_blank_chars=False)
            except Exception as e:  # noqa: BLE001
                reasons.append(f"p{pno}: {type(e).__name__}")
                continue
            if words:
                any_text = True
            low = " ".join(w["text"] for w in words).lower()
            doc_text.append(low)
            squeezed = low.replace(" ", "")
            if not (("revenuefromoperations" in squeezed or "incomefromoperations" in squeezed
                     or "totalincome" in squeezed) and "tax" in low and ("profit" in low or "loss" in low)):
                continue
            if "interest earned" in low and ("advances" in low or "bills" in low):
                return {"ok": False, "why": "bank format (not read from PDF)"}
            if "net premium" in low or "premium earned" in low:
                return {"ok": False, "why": "insurance format (not read from PDF)"}
            groups, err = _table(_lines(words), qe, low)
            if err:
                reasons.append(f"p{pno}: {err}")
                continue
            head_text = next((g["head_text"] for g in groups if "head_text" in g), "")
            unit = _unit(head_text) or _unit(low)
            for g in groups:
                if "why" in g:
                    reasons.append(f"p{pno}: {g['why']}")
                    continue
                basis = g["basis"]
                if basis is None:
                    hc, hs = head_text.rfind("consolidated"), head_text.rfind("standalone")
                    if hc < 0 and hs < 0 and len(doc_text) > 1:
                        prev = doc_text[-2][-600:]
                        hc, hs = prev.rfind("consolidated"), prev.rfind("standalone")
                    basis = ("C" if hc > hs else "S") if (hc >= 0 or hs >= 0) else "?"
                raw, problems = {}, []
                for name, ci in zip(("cur", "prev", "yoy"), g["cols"]):
                    vals, order, labels = _extract(g["rows"], ci)
                    decs = [x[1] for x in vals.values() if x is not None]
                    dec = max(set(decs), key=decs.count) if decs else 2
                    m, probs = _check_col(vals, dec)
                    if m is None:
                        problems += [f"{name}: {x}" for x in probs]
                        break
                    problems += [f"{name}: {x}" for x in probs]
                    raw[name] = m
                if not problems and len(raw) == 3 and g.get("h1") is not None:
                    hv, _, _ = _extract(g["rows"], g["h1"])
                    hm, _ = _check_col(hv, 2)
                    if hm is None:
                        problems.append("half-year column unreadable")
                    else:
                        for k in ("revenue", "pat"):
                            a2, b2 = raw["cur"][k] + raw["prev"][k], hm[k]
                            if abs(a2 - b2) > max(0.06, 0.004 * max(abs(a2), abs(b2))):
                                problems.append(f"quarter + last quarter {k} {a2:.2f} != half year {b2:.2f}")
                if problems or len(raw) < 3:
                    reasons.append(f"p{pno} {basis}: " + "; ".join(problems[:3]))
                    continue
                # unit, basis and agreement with the company's earlier filings
                options = [basis] if basis in ("C", "S") else ["C", "S"]
                facs = ([1e-7 if unit == "rupee" else FACTOR[unit]] if unit
                        else [FACTOR["lakh"], FACTOR["crore"], FACTOR["million"], FACTOR["thousand"], 1e-7])
                best = None
                for b in options:
                    kn = known.get(b) or {}
                    for fac in facs:
                        agree, probs = _matches_known(raw, fac, kn)
                        if probs:
                            continue
                        if agree or (unit and b == basis):
                            if best is None or agree > best[2]:
                                best = (b, fac, agree)
                if best is None and basis == "?" and unit and not any(known.get(b) for b in ("C", "S")):
                    best = ("?", 1e-7 if unit == "rupee" else FACTOR[unit], 0)
                if best is None:
                    why = "unit not stated" if not unit else "does not match the company's earlier filing"
                    if basis == "?" and unit:
                        why = "standalone or consolidated not stated"
                    reasons.append(f"p{pno} {basis}: {why}")
                    continue
                b, fac, agree = best
                cols = {n: {k: (None if x is None else round(x * fac, 4)) for k, x in m.items()} for n, m in raw.items()}
                unit_name = unit or next(k for k, v in FACTOR.items() if v == fac) if fac != 1e-7 else (unit or "rupee")
                good.append({"ok": True, "basis": b, "unit": unit_name, "page": pno, "cols": cols, "how": g["how"],
                             "ncols": g["ncols"], "history_checks": agree})
        if not any_text:
            return {"ok": False, "why": "scanned PDF (no text)"}
    full = " ".join(doc_text)
    for want in ("C", "S"):
        for r in good:
            if r["basis"] == want:
                return r
    for r in good:
        if r["basis"] == "?" and "consolidated" not in full:
            r["basis"] = "S"
            return r
    if good:
        return {"ok": False, "why": "standalone or consolidated not stated"}
    return {"ok": False, "why": "; ".join(reasons[:6]) or f"no results table found in {min(npages, max_pages)} pages"}
