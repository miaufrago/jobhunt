"""One-off helper: find an employer's real ATS endpoint.

Strategy 1 -- fetch the public careers page and regex for a known ATS URL.
Strategy 2 -- brute-probe Workday host/site candidates for a tenant guess.

Run:  python tools/discover_ats.py
"""
import concurrent.futures as cf
import re
import sys

sys.path.insert(0, ".")
from jobhunt.sources.http import session  # noqa: E402

WD = re.compile(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)")
GH = re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)")
LV = re.compile(r"jobs\.lever\.co/([a-z0-9-]+)")
ASH = re.compile(r"jobs\.ashbyhq\.com/([a-z0-9-]+)")
SR = re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9]+)")

CAREER_PAGES = {
    "Alaska Airlines": "https://careers.alaskaair.com/",
    "Costco": "https://www.costco.com/careers.html",
    "Expedia Group": "https://careers.expediagroup.com/jobs/",
    "F5": "https://www.f5.com/company/careers",
    "Fred Hutch": "https://careers.fredhutch.org/",
    "PACCAR": "https://www.paccar.com/careers/",
    "Providence": "https://careers.providence.org/",
    "Puget Sound Energy": "https://www.pse.com/en/careers",
    "Seattle Children's": "https://www.seattlechildrens.org/careers/",
    "Starbucks": "https://careers.starbucks.com/",
    "Univ. of Washington": "https://hr.uw.edu/jobs/",
    "Weyerhaeuser": "https://www.weyerhaeuser.com/careers/",
    "Nordstrom": "https://careers.nordstrom.com/",
    "Zillow": "https://www.zillowgroup.com/careers/",
    "Blue Origin": "https://www.blueorigin.com/careers",
    "Highspot": "https://www.highspot.com/careers/",
    "Outreach": "https://www.outreach.io/company/careers",
    "Remitly": "https://www.remitly.com/us/en/careers",
    "Snowflake": "https://careers.snowflake.com/us/en",
    "Smartsheet": "https://www.smartsheet.com/careers",
    "T-Mobile": "https://careers.t-mobile.com/",
}


def scan(name: str, url: str) -> tuple[str, list[str]]:
    s = session()
    s.headers["Accept"] = "text/html,*/*"
    hits: list[str] = []
    try:
        r = s.get(url, timeout=25, allow_redirects=True)
        body = r.text
        final = r.url
    except Exception as exc:
        return name, [f"ERR {type(exc).__name__}: {exc}"[:120]]
    for blob in (final, body):
        for t, wd, site in set(WD.findall(blob)):
            hits.append(f"workday host={t}.{wd}.myworkdayjobs.com tenant={t} site={site}")
        for slug in set(GH.findall(blob)):
            hits.append(f"greenhouse slug={slug}")
        for slug in set(LV.findall(blob)):
            hits.append(f"lever slug={slug}")
        for slug in set(ASH.findall(blob)):
            hits.append(f"ashby slug={slug}")
        for slug in set(SR.findall(blob)):
            hits.append(f"smartrecruiters slug={slug}")
    return name, sorted(set(hits))[:6] or [f"no ATS link found (final={final[:80]})"]


if __name__ == "__main__":
    with cf.ThreadPoolExecutor(max_workers=10) as pool:
        for name, hits in pool.map(lambda kv: scan(*kv), CAREER_PAGES.items()):
            print(f"== {name}")
            for h in hits:
                print(f"     {h}")
