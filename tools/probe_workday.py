"""Brute-probe Workday tenants and Greenhouse/Lever/Ashby slugs.

Workday: resolve {tenant}.wd{N}.myworkdayjobs.com over the known wd shards, then
follow the root redirect -- Workday sends / to /{locale}/{site}, which names the site.
"""
import concurrent.futures as cf
import re
import socket
import sys

sys.path.insert(0, ".")
from jobhunt.sources.http import session  # noqa: E402

SHARDS = ["wd1", "wd2", "wd3", "wd5", "wd10", "wd12", "wd101", "wd102", "wd103",
          "wd105", "wd108", "wd501", "wd502", "wd503", "wd601"]

WD_TENANTS = {
    "Alaska Airlines": ["alaskaair", "alaskaairlines", "airgroup"],
    "Costco": ["costco", "costcowholesale"],
    "Fred Hutch": ["fhcrc", "fredhutch", "fredhutchinsoncancercenter"],
    "PACCAR": ["paccar"],
    "Providence": ["providence", "psjh"],
    "Puget Sound Energy": ["pse", "pugetsoundenergy"],
    "Seattle Children's": ["seattlechildrens", "seattlechildrenshospital"],
    "Starbucks": ["starbucks"],
    "Univ. of Washington": ["uw", "washington", "universityofwashington"],
    "Weyerhaeuser": ["weyerhaeuser", "wy"],
    "Zillow": ["zillow", "zillowgroup"],
    "Snowflake": ["snowflake", "snowflakecomputing"],
    "Nordstrom": ["nordstrom"],
}

BOARD_SLUGS = {
    "Blue Origin": ["blueorigin", "blue-origin", "blueoriginllc"],
    "Highspot": ["highspot"],
    "Outreach": ["outreach", "outreachcorp", "outreachio"],
    "Remitly": ["remitly", "remitlyinc"],
    "Snowflake": ["snowflake", "snowflakecomputing"],
    "Zillow": ["zillow", "zillowgroup"],
    "SpaceX": ["spacex"],
}

SITE_RE = re.compile(r"myworkdayjobs\.com/(?:([a-z]{2}-[A-Z]{2})/)?([A-Za-z0-9_\-]+)")


def probe_workday(name: str, tenants: list[str]) -> tuple[str, list[str]]:
    s = session()
    s.headers["Accept"] = "text/html,*/*"
    hits = []
    for tenant in tenants:
        for shard in SHARDS:
            host = f"{tenant}.{shard}.myworkdayjobs.com"
            try:
                r = s.get(f"https://{host}/", timeout=12, allow_redirects=True)
            except Exception:
                continue
            if "myworkdayjobs.com" not in r.url:
                continue
            m = SITE_RE.search(r.url)
            if m and m.group(2):
                site = m.group(2)
                ok = s.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                            json={"appliedFacets": {}, "limit": 1, "offset": 0,
                                  "searchText": "security"}, timeout=25)
                n = len(ok.json().get("jobPostings", [])) if ok.status_code == 200 else -1
                hits.append(f"host={host} tenant={tenant} site={site} -> HTTP {ok.status_code} n={n}")
    return name, hits or ["no live workday host"]


def probe_boards(name: str, slugs: list[str]) -> tuple[str, list[str]]:
    s = session()
    hits = []
    for slug in slugs:
        for label, url in (
            ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
            ("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json"),
            ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}"),
            ("smartrecruiters", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1"),
        ):
            try:
                r = s.get(url, timeout=20)
            except Exception:
                continue
            if r.status_code == 200:
                try:
                    body = r.json()
                    n = len(body) if isinstance(body, list) else len(
                        body.get("jobs") or body.get("content") or [])
                except Exception:
                    n = "?"
                hits.append(f"{label} slug={slug} n={n}")
    return name, hits or ["no board found"]


if __name__ == "__main__":
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(probe_workday, k, v) for k, v in WD_TENANTS.items()]
        futs += [pool.submit(probe_boards, k, v) for k, v in BOARD_SLUGS.items()]
        for f in cf.as_completed(futs):
            name, hits = f.result()
            print(f"== {name}")
            for h in hits:
                print(f"     {h}")
