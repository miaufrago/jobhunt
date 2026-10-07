"""Find insurer ATS endpoints by reading their careers pages.

One or two requests per company instead of a brute-force matrix. The matrix
approach starves on the per-platform rate limiter -- thousands of guesses
serialized at over a second each -- and it is impolite besides.
"""
import concurrent.futures as cf
import re
import sys

sys.path.insert(0, ".")
from jobhunt.sources.http import session  # noqa: E402

WD = re.compile(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")
GH = re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)")
LV = re.compile(r"jobs\.lever\.co/([a-z0-9-]+)")
ASH = re.compile(r"jobs\.ashbyhq\.com/([a-z0-9-]+)")
SR = re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9]+)")
ICIMS = re.compile(r"([a-z0-9-]+)\.icims\.com")

CAREERS = {
    # Puget Sound presence
    "Symetra": "https://symetra.eightfold.ai/careers",
    "Trupanion": "https://trupanion.com/about-trupanion/careers",
    "PEMCO": "https://www.pemco.com/about/careers",
    "Safeco / Liberty Mutual": "https://jobs.libertymutualgroup.com/",
    "Milliman": "https://www.milliman.com/en/careers",
    "Premera": "https://www.premera.com/wa/visitor/about-premera/careers/",
    "Regence / Cambia": "https://www.cambiahealth.com/careers",
    "Kaiser Permanente WA": "https://www.kaiserpermanentejobs.org/",
    "Alaska National": "https://www.alaskanational.com/careers",
    # National carriers
    "Chubb": "https://careers.chubb.com/",
    "Travelers": "https://careers.travelers.com/",
    "The Hartford": "https://www.thehartford.com/careers",
    "Zurich NA": "https://www.zurichna.com/careers",
    "CNA": "https://www.cna.com/en/about/careers.html",
    "Nationwide": "https://jobs.nationwide.com/",
    "Allstate": "https://www.allstate.jobs/",
    "Markel": "https://careers.markel.com/",
    "AIG": "https://www.aig.com/careers",
    "W. R. Berkley": "https://www.berkley.com/careers",
    # Brokers
    "Aon": "https://jobs.aon.com/",
    "Marsh McLennan": "https://careers.marshmclennan.com/",
    "WTW": "https://careers.wtwco.com/",
    "Gallagher": "https://jobs.ajg.com/",
    "Brown & Brown": "https://www.bbinsurance.com/careers/",
    "USI": "https://www.usi.com/careers/",
    "HUB International": "https://www.hubinternational.com/careers/",
    "Alliant": "https://alliant.com/careers/",
    "Lockton": "https://global.lockton.com/us/en/careers",
    "Woodruff Sawyer": "https://woodruffsawyer.com/careers",
    # Insurtech
    "Newfront": "https://www.newfront.com/careers",
    "Coalition": "https://www.coalitioninc.com/careers",
    "At-Bay": "https://www.at-bay.com/careers/",
    "Vouch": "https://www.vouch.us/careers",
    "Openly": "https://www.openly.com/careers/",
    "Next Insurance": "https://www.nextinsurance.com/careers/",
    "Pie Insurance": "https://pieinsurance.com/careers/",
    "Sedgwick": "https://www.sedgwick.com/careers/",
}


def scan(item):
    name, url = item
    s = session()
    s.headers["Accept"] = "text/html,*/*"
    try:
        r = s.get(url, timeout=(8, 20), allow_redirects=True)
    except Exception as exc:
        return name, [f"ERR {type(exc).__name__}"]
    blob = r.url + "\n" + r.text
    hits = []
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
    for slug in set(ICIMS.findall(blob)):
        hits.append(f"icims (no public API) {slug}")
    return name, sorted(set(hits))[:4] or [f"none found (final={r.url[:70]})"]


if __name__ == "__main__":
    out = []
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for name, hits in pool.map(scan, CAREERS.items()):
            for h in hits:
                out.append(f"{name:<24} {h}")
    out.sort()
    open("tools/discover_insurers.out", "w", encoding="utf8").write("\n".join(out))
    usable = [x for x in out if any(k in x for k in
              ("workday", "greenhouse", "lever", "ashby", "smartrecruiters"))]
    print("\n".join(usable) if usable else "(nothing usable)")
    print(f"\n{len(usable)} usable endpoints from {len(CAREERS)} employers")
