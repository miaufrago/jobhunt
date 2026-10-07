"""Wider Workday + board sweep for the employers still unresolved.

Writes results to tools/probe_wide.out so it can run in the background.
"""
import concurrent.futures as cf
import sys

sys.path.insert(0, ".")
from jobhunt.sources.http import session  # noqa: E402

SHARDS = ["wd1", "wd2", "wd3", "wd5", "wd10", "wd12", "wd101", "wd102", "wd103",
          "wd105", "wd106", "wd108", "wd501", "wd502", "wd503", "wd601"]

TENANTS = {
    "Alaska Airlines": ["alaskaair", "alaskaairlines", "airgroup", "alaska"],
    "Costco": ["costco", "costcowholesale"],
    "Starbucks": ["starbucks", "starbuckscoffee"],
    "PACCAR": ["paccar", "paccarinc"],
    "Providence": ["providence", "psjh", "providencehealth"],
    "Univ. of Washington": ["uw", "washington", "universityofwashington", "uwashington"],
    "Zillow": ["zillow", "zillowgroup"],
    "Weyerhaeuser": ["weyerhaeuser"],
    "Puget Sound Energy": ["pse", "pugetsoundenergy", "pugetenergy"],
    "Fred Hutch": ["fhcrc", "fredhutch"],
    "Expeditors": ["expeditors"],
    "Symetra": ["symetra"],
    "Premera": ["premera"],
    "Nintendo": ["nintendo", "nintendoofamerica"],
    "Boeing": ["boeing"],
}

SITE_WORDS = ["External", "external", "Careers", "careers", "Search", "jobs", "Jobs",
              "External_Careers", "EXTERNAL_CAREERS", "ExternalCareerSite",
              "External_Career_Site", "CareerSite", "Professional", "Corporate",
              "Campus", "Global", "Main", "career", "Career"]

BOARDS = {
    "Blue Origin": ["blueorigin", "blueoriginllc", "blue-origin", "blueorigincareers"],
    "Highspot": ["highspot", "highspotinc"],
    "Remitly": ["remitly", "remitlyinc", "remitlyglobal"],
    "Zillow": ["zillow", "zillowgroup"],
    "Qualtrics": ["qualtrics"],
    "Rivian": ["rivian", "rivianautomotive"],
    "Convoy": ["convoy"],
    "Auth0/Okta": ["okta", "okta1"],
    "Tableau": ["tableau"],
    "Sana Biotechnology": ["sanabiotechnology", "sanabio"],
    "Accolade": ["accolade"],
    "PitchBook": ["pitchbook", "pitchbookdata"],
    "Chewy": ["chewy"],
    "DoorDash": ["doordash"],
    "Meta": ["meta", "facebook"],
}


def sites_for(t: str) -> list[str]:
    T = t.capitalize()
    extra = [f"{T}_Careers", f"{t}_careers", f"{t}careers", f"{T}Careers", t, T,
             f"{T}_External_Careers", f"{t}_External", f"{T}_Jobs", f"{t}jobs",
             f"{T}_Career_Site", f"{T}_Careers_Site"]
    return extra + SITE_WORDS


def host_live(s, host: str) -> bool:
    try:
        return s.get(f"https://{host}/", timeout=8).status_code in (200, 301, 302, 401, 403, 406)
    except Exception:
        return False


def probe_wd(name: str, tenants: list[str]) -> tuple[str, list[str]]:
    s = session()
    s.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    body = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": "security"}
    for t in tenants:
        for sh in SHARDS:
            host = f"{t}.{sh}.myworkdayjobs.com"
            if not host_live(s, host):
                continue
            for site in sites_for(t):
                try:
                    r = s.post(f"https://{host}/wday/cxs/{t}/{site}/jobs", json=body, timeout=10)
                except Exception:
                    continue
                if r.status_code == 200:
                    total = r.json().get("total")
                    return name, [f"workday host={host} tenant={t} site={site} total={total}"]
    return name, ["NOT FOUND"]


def probe_board(name: str, slugs: list[str]) -> tuple[str, list[str]]:
    s = session()
    hits = []
    for slug in slugs:
        checks = (
            ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "jobs"),
            ("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json", None),
            ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}", "jobs"),
            ("smartrecruiters",
             f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1", "content"),
        )
        for label, url, key in checks:
            try:
                r = s.get(url, timeout=15)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            try:
                b = r.json()
                n = len(b) if isinstance(b, list) else len(b.get(key) or [])
                total = b.get("totalFound") if isinstance(b, dict) else None
            except Exception:
                continue
            if n > 0 or (total or 0) > 0:
                hits.append(f"{label} slug={slug} n={n} total={total}")
    return name, hits or ["NOT FOUND"]


if __name__ == "__main__":
    lines = []
    with cf.ThreadPoolExecutor(max_workers=10) as pool:
        futs = [pool.submit(probe_wd, k, v) for k, v in TENANTS.items()]
        futs += [pool.submit(probe_board, k, v) for k, v in BOARDS.items()]
        for f in cf.as_completed(futs):
            name, hits = f.result()
            for h in hits:
                lines.append(f"{name:<22} {h}")
    lines.sort()
    open("tools/probe_wide.out", "w", encoding="utf8").write("\n".join(lines))
    print("\n".join(lines))
