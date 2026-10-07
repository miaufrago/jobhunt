"""Probe insurance carriers, brokers, health plans and insurtechs for public feeds.

Boards first (one GET each, fast), then Workday with a trimmed shard list so the
sweep finishes in minutes rather than timing out. Writes tools/probe_insurers.out.
"""
import concurrent.futures as cf
import sys

sys.path.insert(0, ".")
from jobhunt.sources.http import session  # noqa: E402

SHARDS = ["wd1", "wd3", "wd5", "wd12", "wd101", "wd103", "wd105", "wd108", "wd501"]

SITE_WORDS = ["External", "Careers", "careers", "External_Careers", "Search",
              "ExternalCareerSite", "External_Career_Site", "CareerSite", "jobs"]

# Puget Sound presence first, then national carriers and brokers she would know.
WORKDAY = {
    "Symetra": ["symetra"],
    "Trupanion": ["trupanion"],
    "PEMCO": ["pemco"],
    "Liberty Mutual / Safeco": ["libertymutual", "lmig"],
    "Milliman": ["milliman"],
    "Regence / Cambia": ["cambia", "cambiahealth"],
    "Kaiser Permanente": ["kaiser", "kaiserpermanente"],
    "Chubb": ["chubb"],
    "AIG": ["aig"],
    "Travelers": ["travelers"],
    "The Hartford": ["thehartford", "hartford"],
    "Zurich NA": ["zurich", "zurichna"],
    "CNA": ["cna"],
    "Nationwide": ["nationwide"],
    "Allstate": ["allstate"],
    "Markel": ["markel"],
    "W. R. Berkley": ["wrberkley", "berkley"],
    "Arch Capital": ["archcapital", "archinsurance"],
    "Aon": ["aon"],
    "Marsh McLennan": ["mmc", "marshmclennan"],
    "WTW": ["wtw", "willistowerswatson"],
    "Gallagher": ["ajg", "gallagher"],
    "Brown & Brown": ["bbinsurance", "brownandbrown"],
    "USI": ["usi"],
    "HUB International": ["hubinternational", "hubintl"],
    "Alliant": ["alliant", "alliantinsurance"],
    "Molina Healthcare": ["molina", "molinahealthcare"],
    "UnitedHealth": ["unitedhealthgroup", "uhg"],
    "Swiss Re": ["swissre"],
    "Munich Re": ["munichre"],
}

BOARDS = {
    "Newfront": ["newfront"],
    "Coalition": ["coalition", "coalitioninc"],
    "At-Bay": ["atbay"],
    "Vouch": ["vouch", "vouchinsurance"],
    "Openly": ["openly"],
    "Kin Insurance": ["kininsurance", "kin"],
    "Next Insurance": ["nextinsurance"],
    "Hippo": ["hippo", "hippoinsurance"],
    "Root": ["rootinsurance", "root"],
    "Lemonade": ["lemonade"],
    "Oscar Health": ["oscarhealth"],
    "Ethos Life": ["ethoslife", "ethos"],
    "Pie Insurance": ["pieinsurance"],
    "Corvus": ["corvusinsurance"],
    "Sedgwick": ["sedgwick"],
    "Lockton": ["lockton"],
    "Woodruff Sawyer": ["woodruffsawyer"],
    "Trupanion": ["trupanion"],
    "Assurance IQ": ["assuranceiq"],
}


def probe_board(name, slugs):
    s = session()
    hits = []
    for slug in slugs:
        for label, url, key in (
            ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "jobs"),
            ("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json", None),
            ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}", "jobs"),
            ("smartrecruiters",
             f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1", "content"),
        ):
            try:
                r = s.get(url, timeout=(8, 20))
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


def probe_wd(name, tenants):
    s = session()
    s.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    body = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": "risk"}
    for t in tenants:
        extra = [f"{t.capitalize()}_Careers", f"{t}_careers", f"{t}careers", t,
                 t.capitalize(), f"{t}jobs"]
        for sh in SHARDS:
            host = f"{t}.{sh}.myworkdayjobs.com"
            try:
                if s.get(f"https://{host}/", timeout=(6, 10)).status_code not in (
                        200, 301, 302, 401, 403, 406):
                    continue
            except Exception:
                continue
            for site in extra + SITE_WORDS:
                try:
                    r = s.post(f"https://{host}/wday/cxs/{t}/{site}/jobs",
                               json=body, timeout=(6, 12))
                except Exception:
                    continue
                if r.status_code == 200:
                    return name, [f"workday host={host} tenant={t} site={site} "
                                  f"total={r.json().get('total')}"]
    return name, ["NOT FOUND"]


if __name__ == "__main__":
    lines = []
    with cf.ThreadPoolExecutor(max_workers=10) as pool:
        futs = [pool.submit(probe_board, k, v) for k, v in BOARDS.items()]
        futs += [pool.submit(probe_wd, k, v) for k, v in WORKDAY.items()]
        for f in cf.as_completed(futs):
            name, hits = f.result()
            for h in hits:
                lines.append(f"{name:<24} {h}")
    lines.sort()
    open("tools/probe_insurers.out", "w", encoding="utf8").write("\n".join(lines))
    found = [x for x in lines if "NOT FOUND" not in x]
    print("\n".join(found))
    print(f"\n{len(found)} with a public feed; "
          f"{len(lines) - len(found)} without")
