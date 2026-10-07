"""Employer ATS adapters.

These hit the employer's own public job-board JSON. First-party, complete, and the
JD text is full length -- aggregators truncate descriptions, which degrades scoring.
"""
from ..normalize import strip_html
from .http import TIMEOUT, session

# Description fetches are the expensive, rate-limited part of a run. Spend them on
# postings whose title already matches the profile instead of the first N returned:
# fewer requests to the platform, and no loss, since a title that fails the filter
# is never scored anyway.
MAX_DETAIL = 40

_relevant = None  # set by set_relevance(); a plain callable(title) -> bool


def set_relevance(fn) -> None:
    """Install the title predicate used to prioritize description fetches."""
    global _relevant
    _relevant = fn


def _worth_detail(out: list[dict], cap: int = MAX_DETAIL) -> list[dict]:
    """Matching titles first, then the rest, capped."""
    if _relevant is None:
        return out[:cap]
    hits = [j for j in out if _relevant(j.get("title", ""))]
    if len(hits) >= cap:
        return hits[:cap]
    rest = [j for j in out if j not in hits]
    return hits + rest[:max(0, cap - len(hits))]


def _greenhouse(e: dict, terms: list[str]) -> list[dict]:
    s = session()
    url = f"https://boards-api.greenhouse.io/v1/boards/{e['slug']}/jobs?content=true"
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "source": "greenhouse",
            "company": e["name"],
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url"),
            "posted_at": j.get("updated_at"),
            "description": strip_html(j.get("content", "")),
            "_compensation": j.get("pay_input_ranges") or j.get("metadata"),
        })
    return out


def _lever(e: dict, terms: list[str]) -> list[dict]:
    s = session()
    r = s.get(f"https://api.lever.co/v0/postings/{e['slug']}?mode=json", timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        out.append({
            "source": "lever",
            "company": e["name"],
            "title": j.get("text", ""),
            "location": cat.get("location", ""),
            "url": j.get("hostedUrl"),
            "posted_at": j.get("createdAt"),
            "description": j.get("descriptionPlain") or strip_html(j.get("description", "")),
            "_compensation": j.get("salaryRange"),
        })
    return out


def _ashby(e: dict, terms: list[str]) -> list[dict]:
    s = session()
    r = s.get(f"https://api.ashbyhq.com/posting-api/job-board/{e['slug']}",
              params={"includeCompensation": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "source": "ashby",
            "company": e["name"],
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("jobUrl"),
            "posted_at": j.get("publishedAt"),
            "description": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            "_compensation": j.get("compensation") or j.get("compensationTierSummary"),
        })
    return out


def _smartrecruiters(e: dict, terms: list[str]) -> list[dict]:
    s = session()
    base = f"https://api.smartrecruiters.com/v1/companies/{e['slug']}/postings"
    r = s.get(base, params={"limit": 100}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        city = loc.get("city", "")
        region = loc.get("region", "")
        out.append({
            "source": "smartrecruiters",
            "company": e["name"],
            "title": j.get("name", ""),
            "location": ", ".join(x for x in (city, region) if x),
            "url": f"https://jobs.smartrecruiters.com/{e['slug']}/{j.get('id')}",
            "posted_at": j.get("releasedDate"),
            "description": "",
            "_detail_id": j.get("id"),
        })
    for j in _worth_detail(out):
        try:
            d = s.get(f"{base}/{j['_detail_id']}", timeout=TIMEOUT).json()
            secs = (d.get("jobAd") or {}).get("sections") or {}
            keys = ("companyDescription", "jobDescription", "qualifications",
                    "additionalInformation")
            j["description"] = strip_html(
                " ".join((secs.get(k) or {}).get("text", "") for k in keys))
        except Exception:
            pass
    for j in out:
        j.pop("_detail_id", None)
    return out


def _workday(e: dict, terms: list[str]) -> list[dict]:
    """Workday CXS. Search per term -- the tenant-wide listing is far too large."""
    s = session()
    s.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    base = f"https://{e['host']}/wday/cxs/{e['tenant']}/{e['site']}"
    seen: set[str] = set()
    out: list[dict] = []
    page = 20
    for term in terms:
        offset = 0
        while offset < 60:          # 3 pages is plenty for one search term
            body = {"appliedFacets": {}, "limit": page, "offset": offset,
                    "searchText": term}
            r = s.post(base + "/jobs", json=body, timeout=TIMEOUT)
            if r.status_code != 200:
                if offset == 0 and term == terms[0]:
                    r.raise_for_status()
                break
            posts = r.json().get("jobPostings", [])
            if not posts:
                break
            # A short page is the last page. Paginating past it burns a request
            # per term per employer, and every one of those waits on the shared
            # per-platform slot.
            last_page = len(posts) < page
            for j in posts:
                path = j.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append({
                    "source": "workday",
                    "company": e["name"],
                    "title": j.get("title", ""),
                    "location": j.get("locationsText", ""),
                    "url": f"https://{e['host']}/{e['site']}{path}",
                    "posted_at": j.get("postedOn", ""),
                    "description": "",
                    "_path": path,
                })
            if last_page:
                break
            offset += page
    for j in _worth_detail(out):
        try:
            d = s.get(base + j["_path"], timeout=TIMEOUT).json()
            info = d.get("jobPostingInfo") or {}
            j["description"] = strip_html(info.get("jobDescription", ""))
            j["posted_at"] = info.get("startDate") or j["posted_at"]
        except Exception:
            pass
    for j in out:
        j.pop("_path", None)
    return out


def _amazon(e: dict, terms: list[str]) -> list[dict]:
    s = session()
    out: list[dict] = []
    seen: set[str] = set()
    for term in terms:
        r = s.get("https://www.amazon.jobs/en/search.json",
                  params={"base_query": term,
                          "loc_query": "Seattle, Washington, United States",
                          "radius": "100mi", "result_limit": 100, "sort": "recent"},
                  timeout=TIMEOUT)
        if r.status_code != 200:
            if not out:
                r.raise_for_status()
            break
        for j in r.json().get("jobs", []):
            path = j.get("job_path")
            if not path or path in seen:
                continue
            seen.add(path)
            parts = [j.get("description"), j.get("basic_qualifications"),
                     j.get("preferred_qualifications")]
            out.append({
                "source": "amazon",
                "company": "Amazon",
                "title": j.get("title", ""),
                "location": j.get("location") or j.get("normalized_location", ""),
                "url": "https://www.amazon.jobs" + path,
                "posted_at": j.get("posted_date"),
                "description": strip_html(" ".join(p for p in parts if p)),
            })
    return out


def _microsoft(e: dict, terms: list[str]) -> list[dict]:
    s = session()
    s.headers.update({"Accept": "application/json",
                      "Referer": "https://jobs.careers.microsoft.com/"})
    search = "https://gcsservices.careers.microsoft.com/search/api/v1/search"
    detail = "https://gcsservices.careers.microsoft.com/search/api/v1/job"
    out: list[dict] = []
    seen: set[str] = set()
    for term in terms:
        r = s.get(search, params={"q": term, "lc": "Washington, United States",
                                  "l": "en_us", "pg": 1, "pgSz": 20,
                                  "o": "Recent", "flt": "true"}, timeout=TIMEOUT)
        if r.status_code != 200:
            if not out:
                r.raise_for_status()
            break
        result = ((r.json().get("operationResult") or {}).get("result") or {})
        for j in result.get("jobs") or []:
            jid = str(j.get("jobId"))
            if not jid or jid in seen:
                continue
            seen.add(jid)
            locs = (j.get("properties") or {}).get("locations") or []
            out.append({
                "source": "microsoft",
                "company": "Microsoft",
                "title": j.get("title", ""),
                "location": locs[0] if locs else "Redmond, WA",
                "url": f"https://jobs.careers.microsoft.com/global/en/job/{jid}",
                "posted_at": j.get("postingDate"),
                "description": "",
                "_id": jid,
            })
    for j in _worth_detail(out):
        try:
            d = s.get(f"{detail}/{j['_id']}", params={"lang": "en_us"}, timeout=TIMEOUT).json()
            res = ((d.get("operationResult") or {}).get("result") or {})
            parts = [res.get("description"), res.get("responsibilities"),
                     res.get("qualifications")]
            j["description"] = strip_html(" ".join(p for p in parts if p))
        except Exception:
            pass
    for j in out:
        j.pop("_id", None)
    return out


ADAPTERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "smartrecruiters": _smartrecruiters,
    "workday": _workday,
    "amazon": _amazon,
    "microsoft": _microsoft,
}


def fetch_employer(e: dict, terms: list[str]) -> list[dict]:
    fn = ADAPTERS.get(e.get("type"))
    if fn is None:
        raise ValueError(f"unknown employer type: {e.get('type')}")
    return fn(e, terms)
