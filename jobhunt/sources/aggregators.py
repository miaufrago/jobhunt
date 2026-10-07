"""LinkedIn / Indeed / Glassdoor / ZipRecruiter / Google Jobs via JobSpy.

JobSpy needs pandas, which has no wheel for Python 3.14. The Docker image pins
3.12 so it works there; locally the import degrades to a no-op with a clear reason.
"""
from ..normalize import clean, strip_html

try:
    from jobspy import scrape_jobs
    AVAILABLE = True
    REASON = ""
except Exception as exc:  # pragma: no cover - depends on the interpreter
    scrape_jobs = None
    AVAILABLE = False
    REASON = f"jobspy unavailable: {exc}"


def _num(v):
    try:
        f = float(v)
        return None if f != f else f     # NaN is the only value unequal to itself
    except (TypeError, ValueError):
        return None


def fetch_aggregators(terms: list[str], cfg: dict) -> list[dict]:
    if not AVAILABLE:
        raise RuntimeError(REASON)
    a = cfg["sources"]["aggregators"]
    out: list[dict] = []
    for term in terms:
        df = scrape_jobs(
            site_name=a["sites"],
            search_term=term,
            google_search_term=f"{term} jobs near {a['location']}",
            location=a["location"],
            results_wanted=a.get("results_per_term", 40),
            hours_old=a.get("hours_old", 168),
            country_indeed="USA",
            description_format="markdown",
            linkedin_fetch_description=True,
        )
        if df is None or len(df) == 0:
            continue
        for rec in df.to_dict("records"):
            loc = clean(rec.get("location"))
            if rec.get("is_remote"):
                loc = f"{loc} (remote)".strip()
            out.append({
                "source": clean(rec.get("site")) or "unknown",
                "company": clean(rec.get("company")),
                "title": clean(rec.get("title")),
                "location": loc,
                "url": clean(rec.get("job_url_direct")) or clean(rec.get("job_url")),
                "posted_at": clean(rec.get("date_posted")),
                "description": strip_html(rec.get("description") or ""),
                "salary_min": _num(rec.get("min_amount")),
                "salary_max": _num(rec.get("max_amount")),
                "remote": bool(rec.get("is_remote")),
            })
    return out
