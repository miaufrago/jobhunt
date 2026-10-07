"""Company/title/location normalization + the dedupe key."""
import hashlib
import re
import html as _html

from bs4 import BeautifulSoup

_SUFFIXES = r"(inc|inc\.|llc|l\.l\.c\.|corp|corporation|co|company|ltd|limited|plc|group|holdings|technologies|technology|labs|ai)"
_WS = re.compile(r"\s+")


def clean(v) -> str:
    """Coerce whatever a source handed us into a string.

    JobSpy returns pandas NaN (a float) for missing company or location, which
    used to reach norm_company() and blow up mid-ingest.
    """
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v)
        if v.lower() in ("nan", "nat", "none", "<na>"):
            return ""
    return v.strip()


NBSP = " "
ZERO_WIDTH = "​"


def strip_html(s) -> str:
    """Turn a job board's markup into readable text.

    Greenhouse serves HTML-escaped HTML, so one unescape-then-strip pass leaves
    tags behind: unescape first, strip, then unescape the entities that exposes.
    Non-breaking spaces survive all of that and read as invisible junk on the
    page, so they are normalized to ordinary spaces.
    """
    s = clean(s)
    if not s:
        return ""
    if "&lt;" in s or "&gt;" in s or "&amp;" in s:
        s = _html.unescape(s)
    if "<" in s and ">" in s:
        s = BeautifulSoup(s, "html.parser").get_text("\n")
    s = _html.unescape(s)
    s = s.replace(NBSP, " ").replace(ZERO_WIDTH, "")
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def norm_company(s: str) -> str:
    s = clean(s).lower().strip()
    s = re.sub(r"[^a-z0-9\s&]", " ", s)
    s = re.sub(rf"\b{_SUFFIXES}\b", " ", s)
    return _WS.sub(" ", s).strip()


# Employment and posting boilerplate only. Seniority words are deliberately NOT
# here: "Principal Security Architect" and "Security Architect" are different
# jobs at different levels, and stripping the level merged them into one row.
_TITLE_NOISE = re.compile(
    r"\b(remote|hybrid|onsite|on-?site|"
    r"full[- ]?time|part[- ]?time|contract|contractor|temp|temporary|"
    r"w2|c2c|1099|"
    r"us|usa|u\.s\.|united states|remote us|anywhere)\b",
    re.I,
)

# Spellings of the same rank. Normalized so "Sr. Director" and "Senior Director"
# share a key, while "Director" keeps its own.
_TITLE_SYNONYMS = [
    (r"\bsr\.?\b", "senior"),
    (r"\bjr\.?\b", "junior"),
    (r"\bsnr\.?\b", "senior"),
    (r"\bmgr\.?\b", "manager"),
    (r"\bavp\b", "assistant vice president"),
    (r"\bsvp\b", "senior vice president"),
    (r"\bevp\b", "executive vice president"),
    (r"\bvp\b", "vice president"),
    (r"\beng\.?\b", "engineer"),
    (r"\bsw\b", "software"),
    (r"\bsec\b", "security"),
    (r"\b(i{2,3}|iv|vi{0,3}|ix|x)\b", " "),   # trailing roman numerals only
]


def norm_title(s: str) -> str:
    """Normalize a title for dedupe.

    The whole title is kept. An earlier version split on the first dash or
    comma and used only the head, so "Director - Product Security" and
    "Director - Data Engineering" both reduced to "director" and were merged
    into a single row whose title was whichever arrived first.
    """
    s = clean(s).lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)      # "(Remote)", "(Hybrid)"
    s = re.sub(r"[^a-z0-9\s&+/#.]", " ", s)
    for pat, repl in _TITLE_SYNONYMS:
        s = re.sub(pat, repl, s)
    s = _TITLE_NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return _WS.sub(" ", s).strip()


_REMOTE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.I)


def norm_location(s: str) -> str:
    """City if there is one, else 'remote'.

    A parenthetical remote marker must not change the key: "Seattle, WA" and
    "Seattle, WA (remote)" are the same job, and treating them as different
    produced duplicate rows on the board.
    """
    s = clean(s).lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)          # drop "(remote)", "(hybrid)"
    s = re.sub(r"\b(remote|hybrid|on-?site)\b", " ", s)
    s = re.sub(r"[^a-z0-9\s,]", " ", s)
    city = _WS.sub(" ", s.split(",")[0]).strip()
    # "Remote - USA" collapses to a country token, which is the same posting as
    # a bare "Remote"; keep them on one key.
    if city in ("usa", "us", "united states", "united states of america", "nationwide", ""):
        return "remote"
    return city


def is_remote(s) -> bool:
    return bool(_REMOTE.search(clean(s)))


def dedupe_key(company: str, title: str, location: str) -> str:
    raw = f"{norm_company(company)}|{norm_title(title)}|{norm_location(location)}"
    return hashlib.sha1(raw.encode("utf8")).hexdigest()[:20]


# Source ranking: the employer's own ATS beats any aggregator (fresher, full JD,
# and the apply link is the real one).
SOURCE_RANK = {
    "greenhouse": 10, "lever": 10, "ashby": 10, "smartrecruiters": 10,
    "workday": 10, "amazon": 10, "microsoft": 10,
    "dice": 6,
    "linkedin": 4, "indeed": 4, "glassdoor": 3, "zip_recruiter": 3, "google": 2,
}
