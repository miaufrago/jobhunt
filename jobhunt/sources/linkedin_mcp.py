"""LinkedIn via a self-hosted MCP server holding an authenticated session.

Unauthenticated scraping caps out around the tenth page and returns a handful of
results per employer. A real session removes that ceiling, which is how postings
from employers with no public ATS feed -- Microsoft, Meta, Alphabet, Apple,
Starbucks -- reach the board at all.

That access is also the thing most worth protecting, so this module is
deliberately slow: it uses three of the server's nineteen tools, paces every
call, and caps how much it fetches per run. Cadence is what distinguishes
automation, so none of these limits should be raised to "catch up".
"""
import json
import re
import time

from .. import config

from .. import salary as salary_mod
from ..normalize import clean
from .http import Cancelled
from .mcp import MCPClient

# Only these are ever called. The server also exposes send_message,
# connect_with_person and the rest of the social surface; this module must never
# touch them, and the collector has no reason to.
ALLOWED_TOOLS = ("search_jobs", "get_job_details", "get_saved_jobs")

PAY_LINE = re.compile(r"\$[\d,.]+ ?[KkMm]?(?:/(?:yr|hr|mo))?(?:\s*-\s*\$[\d,.]+ ?[KkMm]?(?:/(?:yr|hr|mo))?)?")
POSTED_LINE = re.compile(r"[·•‧]\s*(Reposted\s+)?([^·•‧]+?ago)\s*([·•‧]|$)", re.I)
# LinkedIn separates metadata with a middle dot, but which one varies by locale.
SEPARATORS = "·•‧|"


def _payload(res: dict) -> dict:
    for block in res.get("content", []) or []:
        if block.get("type") == "text":
            try:
                return json.loads(block.get("text", ""))
            except Exception:
                return {}
    return {}


def _parse_posting(text: str, url: str) -> dict | None:
    """Turn the rendered job page into a posting.

    The block starts with company, title, then a metadata line, an optional pay
    line, and the description after an 'About the job' heading.
    """
    lines = [ln.strip() for ln in (text or "").split("\n")]
    meat = [ln for ln in lines if ln]
    if len(meat) < 3:
        return None

    company, title = meat[0], meat[1]
    location = ""
    posted = ""
    for ln in meat[2:8]:
        if any(sep in ln for sep in SEPARATORS):
            head = ln
            for sep in SEPARATORS:
                head = head.split(sep)[0]
            location = head.strip("  ​	")
            m = POSTED_LINE.search(ln)
            if m:
                posted = m.group(2).strip()
            break

    pay_text = ""
    for ln in meat[2:14]:
        if ln.startswith("$") and PAY_LINE.match(ln):
            pay_text = ln
            break

    body = ""
    low = text.lower()
    idx = low.find("about the job")
    if idx != -1:
        body = text[idx + len("about the job"):].strip()
    if len(body) < 200:
        body = text

    job = {
        "source": "linkedin",
        "company": clean(company),
        "title": clean(title),
        "location": clean(location),
        "url": url,
        "posted_at": posted,
        "description": clean(body),
        "remote": "remote" in (location + " " + text[:400]).lower(),
    }
    if pay_text:
        # LinkedIn writes "$85K/yr - $93K/yr"; drop the period suffix so the
        # shared parser sees a plain range, then let it decide the period.
        got = salary_mod.parse(pay_text.replace("/yr", "").replace("/hr", " per hour")
                               .replace("/mo", " per month"))
        if got:
            job.update(got)
            job["salary_source"] = "stated"
    return job


def _rotate_offset(n_terms: int, per_run: int, name: str = "linkedin_offset") -> int:
    """Where this run's search window starts, advancing each run.

    Kept in a file next to the database rather than in memory: the scheduler and
    the CLI are different processes, and a restart must not reset coverage back
    to the same head terms. Each rotating list keeps its own file.
    """
    if n_terms <= per_run:
        return 0
    path = config.DATA_DIR / name
    try:
        offset = int(path.read_text(encoding="utf8").strip())
    except Exception:
        offset = 0
    try:
        path.write_text(str((offset + per_run) % n_terms), encoding="utf8")
    except Exception:
        pass
    return offset % n_terms


def _window(items: list[str], per_run: int, name: str) -> list[str]:
    """The slice of a rotating list this run should search."""
    if not items:
        return []
    offset = _rotate_offset(len(items), per_run, name)
    return [items[(offset + k) % len(items)] for k in range(min(per_run, len(items)))]


def fetch_linkedin(terms: list[str], cfg: dict) -> list[dict]:
    li = cfg["sources"].get("linkedin_mcp") or {}
    url = li.get("mcp_url")
    if not url:
        raise RuntimeError("sources.linkedin_mcp.mcp_url is not configured")

    per_run = int(li.get("searches_per_run", 6))
    gap = float(li.get("seconds_between_searches", 45))
    detail_gap = float(li.get("seconds_between_details", 6))
    max_details = int(li.get("max_details_per_run", 40))
    location = li.get("location", "Seattle, Washington, United States")
    date_posted = li.get("date_posted", "past_week")

    client = MCPClient(url, client_name="jobhunt-linkedin").connect()
    ids: list[str] = []
    seen: set[str] = set()

    # Rotate the window. terms[:per_run] searched the same head every run, so
    # with 8 terms and a budget of 6 the last two were never searched at all.
    # The offset is persisted per instance, so consecutive runs cover the tail.
    window = _window(terms, per_run, "linkedin_offset")

    # Narrow at the source rather than filtering afterwards. LinkedIn's own
    # filters cost nothing extra and cut the noise the rules gate would
    # otherwise have to throw away: seniority, employment type and work
    # arrangement. Naming a company in `keywords` does NOT do this -- search_jobs
    # has no company parameter, so "Apple security" is two keywords and returns
    # Amazon and Microsoft postings alongside Apple retail jobs. That was tried.
    extra = {k: li[k] for k in ("experience_level", "job_type", "work_type")
             if li.get(k)}
    if extra:
        print(f"linkedin: filters {extra}", flush=True)
    print(f"linkedin: searching {window}", flush=True)

    failed = 0
    for i, term in enumerate(window):
        if i:
            time.sleep(gap)
        try:
            res = client.call("search_jobs", {
                "keywords": term,
                "location": location,
                "max_pages": int(li.get("max_pages", 1)),
                "date_posted": date_posted,
                "sort_by": "date",
                **extra,
            })
        except Cancelled:
            # The phase ended. Keep the ids already collected and stop asking
            # for more -- continuing here would sleep through the remaining
            # searches raising Cancelled on each one.
            print(f"linkedin: phase ended; stopping after {len(ids)} ids",
                  flush=True)
            break
        except Exception as exc:
            # One slow search must not discard the whole pass. A single read
            # timeout used to raise out of this loop and lose every id already
            # collected, so the source reported total failure over one bad call.
            failed += 1
            print(f"linkedin: search {term!r} failed ({type(exc).__name__}); "
                  f"continuing", flush=True)
            if failed >= max(2, len(window) // 2):
                raise RuntimeError(
                    f"linkedin: {failed} of {len(window)} searches failed; "
                    f"stopping rather than hammering a session that is unhappy")
            continue
        for jid in (_payload(res).get("job_ids") or []):
            if jid not in seen:
                seen.add(jid)
                ids.append(jid)

    out: list[dict] = []
    for n, jid in enumerate(ids[:max_details]):
        if n:
            time.sleep(detail_gap)
        try:
            res = client.call("get_job_details", {"job_id": jid})
        except Cancelled:
            print(f"linkedin: phase ended; returning {len(out)} of "
                  f"{len(ids[:max_details])} details", flush=True)
            break
        except Exception:
            continue
        data = _payload(res)
        text = ((data.get("sections") or {}).get("job_posting")
                or (data.get("sections") or {}).get("job_details") or "")
        job = _parse_posting(text, data.get("url") or
                             f"https://www.linkedin.com/jobs/view/{jid}/")
        if job and job["company"] and job["title"]:
            out.append(job)

    if len(ids) > max_details:
        print(f"linkedin: {len(ids)} found, {max_details} fetched "
              f"(max_details_per_run); the rest wait for the next run", flush=True)
    return out
