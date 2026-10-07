"""Pay extraction.

Washington's pay transparency law means most Puget Sound postings state a range,
but almost none of them put it in a structured field -- it sits in the description
prose. So there are two paths: read the ATS field when one exists, otherwise parse
the text.

Everything is normalized to an annual figure so ranges are comparable across
hourly contract listings and salaried roles.
"""
import re

HOURS_PER_YEAR = 2080

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{4,}(?:\.\d{2})?|\d{1,3}(?:\.\d{2})?"
# Aggregators emit markdown-escaped dashes (\-) and boards emit HTML entities.
_SEP = r"\s*(?:\\?-|–|—|&mdash;|&ndash;|to|through|up to)\s*"

# "$249,000.00 to $348,500"  /  "$150,000 - $200,000 USD"
RANGE = re.compile(rf"\$\s*({_NUM})\s*(?:USD)?{_SEP}\$?\s*({_NUM})", re.I)
# "$150K - $200K"
RANGE_K = re.compile(rf"\$\s*(\d{{2,3}}(?:\.\d)?)\s*[kK]{_SEP}\$?\s*(\d{{2,3}}(?:\.\d)?)\s*[kK]")
# "USD 150,000 - 200,000"
RANGE_USD = re.compile(rf"USD\s*({_NUM}){_SEP}({_NUM})", re.I)
SINGLE = re.compile(rf"\$\s*({_NUM})")

HOURLY = re.compile(r"\b(per\s+hour|/\s*hour|hourly|an\s+hour|/hr|per\s+hr)\b", re.I)
MONTHLY = re.compile(r"\b(per\s+month|/\s*month|monthly)\b", re.I)
WEEKLY = re.compile(r"\b(per\s+week|/\s*week|weekly)\b", re.I)
# LOCAL PATCH 2026-09-09: an explicit annual marker beats a stray "monthly" in a
# nearby benefits bullet ("Monthly wellness reimbursement"), which was annualizing
# real annual ranges by 12 and producing $1.8M salaries.
ANNUAL = re.compile(r"\b(per\s+year|/\s*year|annually|annual|per\s+annum|/yr)\b", re.I)

# Where a range is likely to be, so a random dollar figure in benefits text loses.
CUES = re.compile(
    r"(pay\s+range|salary\s+range|base\s+pay|base\s+salary|compensation\s+range|"
    r"expected\s+(?:salary|pay|compensation)|target\s+(?:salary|pay)|"
    r"annual\s+(?:salary|pay)|the\s+range\s+for|pay\s+transparency|"
    r"hiring\s+range|salary\s+band|total\s+cash)", re.I)

# Anything outside this after annualizing is a false positive (equity, revenue,
# budget figures, "$5,000 signing bonus").
MIN_ANNUAL = 25_000
MAX_ANNUAL = 2_000_000


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def _period(window: str) -> str:
    if ANNUAL.search(window):
        return "year"
    if HOURLY.search(window):
        return "hour"
    if MONTHLY.search(window):
        return "month"
    if WEEKLY.search(window):
        return "week"
    return "year"


def annualize(value: float, period: str) -> float:
    if period == "hour":
        return value * HOURS_PER_YEAR
    if period == "month":
        return value * 12
    if period == "week":
        return value * 52
    return value


def _plausible(lo: float, hi: float) -> bool:
    return (MIN_ANNUAL <= lo <= MAX_ANNUAL and MIN_ANNUAL <= hi <= MAX_ANNUAL
            and lo <= hi and hi / max(lo, 1) <= 6)


def parse(text: str) -> dict | None:
    """Best pay range in the text, annualized. None when nothing plausible."""
    if not text or "$" not in text and "USD" not in text:
        return None

    candidates: list[tuple[int, float, float, str]] = []
    for pattern, scale in ((RANGE, 1), (RANGE_K, 1000), (RANGE_USD, 1)):
        for m in pattern.finditer(text):
            lo, hi = _f(m.group(1)) * scale, _f(m.group(2)) * scale
            window = text[max(0, m.start() - 220):m.end() + 120]
            # LOCAL PATCH: period is decided close to the figure, not across 340 chars.
            period = _period(text[max(0, m.start() - 60):m.end() + 60])
            a_lo, a_hi = annualize(lo, period), annualize(hi, period)
            if not _plausible(a_lo, a_hi):
                continue
            # A cue nearby beats a bare figure further down the page.
            rank = 0 if CUES.search(window) else 1
            candidates.append((rank, a_lo, a_hi, period))

    if candidates:
        candidates.sort(key=lambda c: (c[0], -c[2]))
        _, lo, hi, period = candidates[0]
        return {"salary_min": round(lo), "salary_max": round(hi),
                "salary_period": period, "salary_source": "parsed"}

    # No range. A single figure next to an explicit cue is still worth keeping.
    for m in SINGLE.finditer(text):
        window = text[max(0, m.start() - 220):m.end() + 120]
        if not CUES.search(window):
            continue
        period = _period(window)
        val = annualize(_f(m.group(1)), period)
        if MIN_ANNUAL <= val <= MAX_ANNUAL:
            return {"salary_min": round(val), "salary_max": round(val),
                    "salary_period": period, "salary_source": "parsed"}
    return None


def from_structured(raw) -> dict | None:
    """Pay from an ATS field, when the board bothers to expose one."""
    if raw is None:
        return None

    # Lever: {"min": "150000", "max": "200000", "currency": "USD", "interval": "per-year-salary"}
    if isinstance(raw, dict) and ("min" in raw or "minValue" in raw):
        lo = raw.get("min") or raw.get("minValue")
        hi = raw.get("max") or raw.get("maxValue") or lo
        interval = str(raw.get("interval") or raw.get("period") or "year").lower()
        period = ("hour" if "hour" in interval else
                  "month" if "month" in interval else
                  "week" if "week" in interval else "year")
        try:
            a_lo, a_hi = annualize(float(lo), period), annualize(float(hi), period)
        except (TypeError, ValueError):
            return None
        if _plausible(a_lo, a_hi):
            return {"salary_min": round(a_lo), "salary_max": round(a_hi),
                    "salary_period": period, "salary_source": "stated"}
        return None

    # Ashby: compensation tiers carry a rendered string; Dice ships "$60 - $70".
    if isinstance(raw, str):
        got = parse(raw)
        if got:
            got["salary_source"] = "stated"
        return got

    if isinstance(raw, dict):
        for key in ("compensationTierSummary", "summary", "displayValue", "value"):
            if isinstance(raw.get(key), str):
                got = parse(raw[key])
                if got:
                    got["salary_source"] = "stated"
                    return got
    if isinstance(raw, list):
        for item in raw:
            got = from_structured(item)
            if got:
                return got
    return None


def enrich(job: dict) -> dict:
    """Fill pay fields on one raw posting. Structured field first, then the text.

    A source-supplied figure is validated, not trusted. This previously
    short-circuited on any truthy salary_min and stamped period="year", so an
    aggregator's hourly 60-70 became "$0K-$0K/yr" and tripped the pay floor,
    capping a legitimate posting at weak. Implausible values now fall through
    to the description, which usually states the real range.
    """
    supplied_min = job.get("salary_min")
    supplied_max = job.get("salary_max") or supplied_min
    if supplied_min:
        period = job.get("salary_period") or "year"
        lo = annualize(float(supplied_min), period)
        hi = annualize(float(supplied_max), period)
        if _plausible(lo, hi):
            job.update({"salary_min": round(lo), "salary_max": round(hi),
                        "salary_period": period,
                        "salary_source": job.get("salary_source") or "stated"})
            return job
        # Try reading it as an hourly rate before giving up on it.
        h_lo, h_hi = annualize(float(supplied_min), "hour"), annualize(float(supplied_max), "hour")
        if _plausible(h_lo, h_hi):
            job.update({"salary_min": round(h_lo), "salary_max": round(h_hi),
                        "salary_period": "hour", "salary_source": "stated"})
            return job
        for k in ("salary_min", "salary_max", "salary_period", "salary_source"):
            job.pop(k, None)

    got = from_structured(job.pop("_compensation", None))
    if got is None:
        got = parse(f"{job.get('title', '')}\n{job.get('description') or ''}")
    if got:
        job.update(got)
    return job


def fmt(lo, hi, period: str | None = None) -> str:
    """Board-friendly rendering. Always annual, so ranges compare."""
    if not lo:
        return ""
    def k(v):
        return f"${v / 1000:.0f}K" if v < 1_000_000 else f"${v / 1_000_000:.2f}M"
    out = k(lo) if not hi or abs(hi - lo) < 1 else f"{k(lo)}–{k(hi)}"
    return out + ("/yr*" if period and period != "year" else "/yr")
