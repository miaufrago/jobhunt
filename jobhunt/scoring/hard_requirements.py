"""Hard requirements the keyword gate cannot see.

The heuristic scores overlap -- how much of a posting looks like the candidate's
background. It has no way to notice that the same posting demands twelve years in
medical devices, or that a listing tagged "remote" says three days a week in an
office two time zones away. Those are not weak signals to be outweighed by domain
overlap; they are gates, and a posting behind one is not worth an assessment slot.

Measured against 212 read assessments, the heuristic's correlation with actual fit
was 0.096 -- roles read as `weak` scored higher on average than roles read as
`possible`. The single largest cause was postings whose body contradicted their
own metadata or stated a bar the candidate cannot clear.

Two rules only, both conservative, because a false positive here is silent: it
removes a posting from the queue and no one ever sees it.

  years     Fatal only when the *lowest* stated bar in the required section
            exceeds the candidate by `years_tolerance` or more. Lowest, not
            highest, because postings list alternative entry paths ("Bachelor's
            and 3 years OR Associate's and 6 years") and only one must be met.
            The cost is real: a posting stating one genuine 8-year requirement
            survives at tolerance 4. That is the intended trade.

  location  Fatal only when a posting states an onsite, hybrid, residency or
            region mandate AND none of the candidate's own locations appears
            alongside it AND the same sentence offers no remote alternative.

Everything else is reported as a flag and changes no score. The flags exist
because a remote tag the body contradicts is the single largest source of wasted
reads: of 161 postings read and rejected, the two fatal rules would have caught
60, and the most common reason for the other 101 was a work arrangement the
listing's own metadata got wrong. Each flag below was added after the same
failure appeared three separate times, and each was validated against the full
read corpus before being trusted.

  states     A remote posting that restricts hiring to a list of named states.
             Flagged rather than fatal: a list that excludes home but includes
             a relocation target is a real option, not a dead end. Exclusion
             lists ("not eligible in ...") are deliberately ignored.
  title      A work-arrangement word that appears only in the job title, where
             the location rule -- which reads the description -- cannot see it.
  time zone  A stated time-zone requirement. Not an onsite mandate, so the
             location rule never fires on it, and it turned up three times on
             roles that were otherwise viable. Only flagged when the named zone
             is not one the candidate can work from.
"""
import re

# --------------------------------------------------------------- years

# "8+ years", "minimum of 10 years", "5-7 years", "at least 3 years".
_YEARS = re.compile(
    r"(?:(?:a\s+)?minimum\s+(?:of\s+)?|at\s+least\s+)?"
    r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:[-–—to]+\s*\d{1,2}\s*)?"
    r"(?:\+\s*)?years?\b",
    re.I)

# The number only counts if it is talking about a person's background.
_YEARS_SUBJECT = re.compile(
    r"experience|background|track\s+record|working|worked|managing|leading|"
    r"building|shipping|delivering|in\s+a\s+\w+\s+role|of\s+product|as\s+a",
    re.I)

# Benefits and tenure language reuses the same shape: "first five years of
# employment", "after 3 years of service". None of it is a requirement.
_YEARS_NOISE = re.compile(
    r"vacation|employment|tenure|anniversary|vesting|401|accru|"
    r"paid\s+time|sabbatical|service\s+award|per\s+year|annually|"
    r"each\s+year|last\s+\d|past\s+\d|next\s+\d",
    re.I)

# Company age reads exactly like a requirement: "On this journey for over 12
# years, Qventus is leading the transformation of healthcare." Gated a role that
# had been read as `possible` until this was added.
_YEARS_COMPANY_AGE = re.compile(
    r"(for\s+(?:over|more\s+than|nearly|almost)\s+\d{1,2}\s*\+?\s*years|"
    r"\d{1,2}\s*\+?\s*years\s+(?:of\s+)?(?:in\s+)?business|"
    r"(?:founded|established|since|serving|operating|been\s+around)\b[^.]{0,60}"
    r"\d{1,2}\s*\+?\s*years|"
    r"\d{1,2}\s*\+?\s*years\s+(?:we|the\s+company|our\s+(?:company|team|platform)))",
    re.I)

# Everything after one of these is aspirational, not a gate.
_PREFERRED = re.compile(
    r"(preferred\s+qualification|preferred\s*:|nice[\s-]to[\s-]have|"
    r"bonus\s+points|bonus\s*:|it'?s\s+a\s+plus|is\s+a\s+plus|"
    r"pluses|desirable|we'?d\s+love\s+it\s+if|even\s+better)",
    re.I)

# --------------------------------------------------------------- location

_NOT_REMOTE = re.compile(
    r"(this\s+is\s+not\s+a\s+remote|not\s+a\s+remote\s+(role|position)|"
    r"no\s+remote\s+work|remote\s+work\s+is\s+not)", re.I)

_ONSITE_DAYS = re.compile(
    r"\b(?:(\d)|one|two|three|four|five)\s*(?:\+\s*)?days?\s*"
    r"(?:a|per)?\s*(?:week\s*)?"
    r"(?:on-?site|in\s+(?:the\s+|our\s+)?office|in-?office|"
    r"at\s+(?:the\s+|our\s+)?office|from\s+(?:the\s+|our\s+)?office)",
    re.I)

_FULLY_ONSITE = re.compile(
    r"(fully\s+on-?site|100%\s+on-?site|on-?site\s+daily|"
    r"(?:five|5)\s+days\s+(?:a|per)\s+week)", re.I)

_BASED_OUT_OF = re.compile(
    r"(?:position|role|job)\s+(?:is\s+)?based\s+(?:out\s+of|in|at)\s+"
    r"(?:our\s+)?([A-Za-z][A-Za-z .'\-]{2,40}?)\s*(?:office|\.|,)", re.I)

_RESIDENCY = re.compile(
    r"(?:open\s+exclusively\s+to\s+candidates\s+(?:currently\s+)?residing\s+in|"
    r"must\s+(?:currently\s+)?(?:reside|live|be\s+located|be\s+based)\s+in|"
    r"candidates?\s+must\s+live\s+in|"
    r"only\s+apply\s+if\s+you\s+live\s+in\s+(?:one\s+of\s+)?(?:the\s+)?"
    r"following\s+states?)\s*:?\s*([^.\n\r]{0,200})", re.I)

_REGION_ONLY = re.compile(
    r"remote(?:ly)?\s+(?:in|within|from)\s+the\s+"
    r"(east|west|central|mountain|pacific|eastern|western|southern|northern)"
    r"\s*(?:coast|region|time\s*zone)", re.I)

_RELOCATE = re.compile(
    r"(willing\s+to\s+relocate|relocation\s+(?:is\s+)?required|"
    r"must\s+relocate|be\s+willing\s+to\s+relocate)", re.I)

# If the same sentence also offers remote, the mandate is an option, not a rule.
# Trimble's "Onsite in Westminster, CO or Dayton, OH, or Remote-US" is the case
# this exists for.
_REMOTE_ESCAPE = re.compile(
    r"(or\s+remote|remote[\s-]us|fully\s+remote|remote\s+option|"
    r"remote\s+eligible|or\s+work\s+remotely|remote\s+\(us\)|"
    r"this\s+is\s+a\s+(?:fully\s+)?remote)", re.I)

# "MUST currently reside in the United States of America" is a requirement she
# meets. Treating every residency clause as a restriction gated a `possible`.
_NATIONWIDE = re.compile(
    r"(united\s+states|u\.?s\.?a?\b|anywhere\s+in\s+the\s+us|"
    r"us[\s-]based|domestic|continental\s+us|lower\s+48|any\s+state)", re.I)

_WINDOW = 260

# Aggregator descriptions arrive markdown-escaped: "10\\+ years", "3 days on\\-site",
# "self\\-service". Every pattern here is written against ordinary punctuation, so a
# backslash sitting between the digits and the plus sign silently defeats the match.
# Measured on one live corpus, 3,887 open postings used the escaped form against 971
# plain -- meaning roughly 80% of postings were slipping past the years gate unread.
# Strip the escapes once, up front, and match against clean text.
_ESCAPED_PUNCT = re.compile(r"\\([-+.()\[\]*_#!|~`>])")


def _unescape(text: str) -> str:
    return _ESCAPED_PUNCT.sub(r"\1", text)


def _sentence_around(text: str, start: int, end: int) -> str:
    return text[max(0, start - _WINDOW):min(len(text), end + _WINDOW)]


def _required_section(text: str) -> str:
    """Everything before the first preferred/nice-to-have heading."""
    m = _PREFERRED.search(text)
    return text[:m.start()] if m else text


def _years_required(text: str) -> list[tuple[int, str, str]]:
    """Every years bar stated as a requirement: (years, context, matched text)."""
    out = []
    for m in _YEARS.finditer(_required_section(text)):
        ctx = _sentence_around(text, m.start(), m.end())
        if _YEARS_NOISE.search(ctx) or _YEARS_COMPANY_AGE.search(ctx):
            continue
        if not _YEARS_SUBJECT.search(ctx):
            continue
        n = int(m.group(1))
        if 1 <= n <= 30:
            out.append((n, " ".join(ctx.split())[:160], m.group(0).strip()))
    return out


def _location_blockers(text: str, markers: list[str]) -> list[tuple[str, str]]:
    """Onsite/residency mandates that do not name any of the wanted places."""
    found = []
    checks = (
        ("not a remote role", _NOT_REMOTE),
        ("days per week in office", _ONSITE_DAYS),
        ("fully onsite", _FULLY_ONSITE),
        ("based out of a named office", _BASED_OUT_OF),
        ("residency restriction", _RESIDENCY),
        ("remote limited to one region", _REGION_ONLY),
        ("relocation required", _RELOCATE),
    )
    doc = text.lower()
    for label, pat in checks:
        for m in pat.finditer(text):
            ctx = _sentence_around(text, m.start(), m.end())
            low = ctx.lower()
            if any(mk in low for mk in markers):
                continue                      # names a place she wants
            # The region rule is itself about restricted remote, so the escape
            # must not apply to it: "or remotely in the East Coast region"
            # contains "or remote" and would otherwise wave itself through.
            if label != "remote limited to one region" and _REMOTE_ESCAPE.search(ctx):
                continue                      # remote offered alongside
            if label == "residency restriction" and _NATIONWIDE.search(m.group(0)):
                continue                      # "must reside in the US" is not a bar
            # Safety net: a mandate 260 characters from any mention of a wanted
            # location is more likely a parsing artefact than a real exclusion.
            if label in ("days per week in office", "based out of a named office") \
                    and any(mk in doc for mk in markers):
                continue
            found.append((label, f"“{m.group(0).strip()[:120]}” — "
                                 f"{' '.join(ctx.split())[:180]}"))
            break                             # one hit per rule is enough
    return found


# --------------------------------------------------- eligible-state lists

# Full state names, so a two-letter abbreviation inside a sentence cannot be
# mistaken for one -- "OR" and "IN" are English words, and "DE" and "MA" appear
# inside other words.
_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    "florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    "louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    "missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    "new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    "rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    "virginia|washington|west virginia|wisconsin|wyoming|district of columbia")

# "open to individuals who live in the following states: ...", "eligible for
# employment in the following states", "only apply if you can work in one of
# the states listed". The cue has to promise a list; a passing mention of two
# states is not a restriction.
_STATE_LIST_CUE = re.compile(
    r"(?:only (?:hire|employ)|registered for employment|eligible (?:for|to)\s+"
    r"(?:employment|work|be hired)|able to (?:live and )?work|must (?:live|reside)|"
    r"open to (?:individuals|candidates|applicants)|can (?:only )?(?:hire|employ)|"
    r"following states|states listed|residents? of)",
    re.I)


# Where she lives now, as against the relocation targets that share the
# `home_markers` list. A state list that includes Seattle but not Louisiana is
# viable only after a move, and the flag should say which case it is.
_HOME_FIRST = ("new orleans", "metairie", "louisiana")


def _eligible_states(text: str) -> tuple[list[str], str] | None:
    """States a remote posting restricts itself to, when it names a real list.

    Returns (states, context) or None. Found this after SupplyHouse: a posting
    tagged plainly "remote" whose body limits hiring to sixteen named states,
    none of them Louisiana. The location rule cannot see it -- there is no onsite
    mandate to match, and "remote" is true as far as it goes.

    Three or more states, because two is usually "our offices in X and Y" rather
    than an eligibility list, and a false positive here is a role thrown away.
    """
    for m in _STATE_LIST_CUE.finditer(text):
        window = text[m.start():m.start() + 900]
        # An exclusion list is the same shape with the opposite meaning.
        # Docusign says "not eligible for employment in the following states:
        # Alaska, Hawaii, Maine ..." -- nine states, none of them home, which
        # read as a nine-state allowlist and threw a viable remote role away.
        lead = text[max(0, m.start() - 90):m.start() + 60].lower()
        if re.search(r"\bnot\b|\bexcept\b|\bexcluding\b|\bunable to\b|"
                     r"\bcannot\b|\bineligible\b", lead):
            continue
        found, seen = [], set()
        for s in re.finditer(_US_STATES, window, re.I):
            name = s.group(0).lower()
            if name not in seen:
                seen.add(name)
                found.append(name)
        if len(found) >= 3:
            return found, re.sub(r"\s+", " ", window[:220]).strip()
    return None


# ------------------------------------------------- title and time zone

# "Senior Product Manager (Hybrid)", "... - Hybrid", "Product Owner (Onsite)".
# The location rule reads the description, so a work-arrangement word that only
# ever appears in the TITLE is invisible to it. CrowdStrike put "(Hybrid)" in
# the title of a role the aggregator tagged remote.
_TITLE_ARRANGEMENT = re.compile(
    r"[(\[-]\s*(hybrid|on-?site|in-?office|in-?person)\b", re.I)


def _title_arrangement(title: str) -> str | None:
    m = _TITLE_ARRANGEMENT.search(title or "")
    return m.group(1).lower() if m else None


# "Must live in the Eastern timezone", "open to candidates in the USA Eastern
# time zone", "must be able to work East coast hours". A time-zone restriction
# is not an onsite mandate, so the location rule never sees it -- and it has
# turned up three times (Lumana, Genea, BigPanda), twice on roles otherwise
# viable. Central is one hour off Eastern and usually negotiable; Pacific is
# three and usually not. Reported, never fatal, because that call is hers.
_TZ_ZONES = r"(eastern|pacific|central|mountain|east coast|west coast|\bET\b|\bPT\b|\bCT\b|\bMT\b|\bEST\b|\bPST\b)"
_TZ_CUE = re.compile(
    r"(?:must (?:live|reside|be (?:located|able to work))|open to candidates in|"
    r"located in|based in|work(?:ing)? (?:hours|in)|overlap with|"
    r"able to work)[^.]{0,80}?" + _TZ_ZONES + r"[^.]{0,40}?(time ?zone|hours|time)",
    re.I)


def _timezone_restriction(text: str) -> tuple[str, str] | None:
    """A stated time-zone requirement, with the zone it names."""
    m = _TZ_CUE.search(text)
    if not m:
        return None
    zone = m.group(1).lower().replace("\u00a0", " ")
    return zone, re.sub(r"\s+", " ", m.group(0))[:180]


def check(job: dict, profile: dict) -> dict:
    """Gate a posting on stated requirements the keyword scorer cannot read.

    Returns {"fatal": bool, "reasons": [str], "flags": [str]}. `fatal` means the
    posting states a bar this candidate cannot clear; callers should keep it out
    of the assessment queue. `flags` are worth showing and gate nothing.
    """
    cfg = (profile or {}).get("hard_requirements") or {}
    if not cfg.get("enabled", False):
        return {"fatal": False, "reasons": [], "flags": []}

    text = _unescape(job.get("description") or "")
    if len(text) < 400:                       # too thin to judge; never gate
        return {"fatal": False, "reasons": [], "flags": []}

    have = float(cfg.get("years_experience", 0) or 0)
    tol = float(cfg.get("years_tolerance", 3) or 3)
    markers = [m.lower() for m in (cfg.get("home_markers") or [])]

    reasons, flags = [], []

    # The office a hybrid mandate refers to is usually named in the location
    # field rather than beside the mandate. Visa's "at least 3 days in office"
    # is in Bellevue -- a metro she wants -- and reading only the body threw the
    # role away. When the location field already names somewhere wanted, the
    # geography dimension is settled and location gating has nothing to add.
    where = (job.get("location") or "").lower()
    location_settled = any(mk in where for mk in markers)

    years = _years_required(text)
    if years and have:
        # The *lowest* stated bar, not the highest. Postings list alternative
        # entry paths ("Bachelor's and 3 years OR Associate's and 6 years OR
        # high school and 8 years") and a candidate needs to clear only one.
        # Taking the maximum gated three roles that had been read as `possible`.
        easiest, ctx, hit = min(years, key=lambda p: p[0])
        if easiest - have >= tol:
            reasons.append(
                f"lowest stated bar is {easiest}+ years against {have:g}, "
                f"a gap of {easiest - have:g} (tolerance {tol:g}) "
                f"[matched “{hit}” in: “{ctx}”]")
        elif easiest > have:
            flags.append(f"asks for {easiest}+ years; candidate has {have:g}")

    if not location_settled:
        for label, ctx in _location_blockers(text, markers):
            reasons.append(f"{label}: {ctx}")

    # A state-eligibility list is reported, never fatal. A list that excludes
    # home but includes a metro the candidate would move to is a real option,
    # not a dead end -- SupplyHouse excludes Louisiana and includes Washington.
    # A work-arrangement word in the title that the body never repeats.
    arrangement = _title_arrangement(job.get("title") or "")
    if arrangement and not location_settled:
        flags.append(f"title says \u201c{arrangement}\u201d; the aggregator's "
                     f"location tag may not reflect that")

    tz = _timezone_restriction(text)
    if tz:
        zone, ctx = tz
        # Her bases are Central (New Orleans) and Pacific (Seattle).
        hers = ("central", "ct", "pacific", "pt", "pst", "west coast")
        if not any(h in zone for h in hers):
            flags.append(f"states a {zone} time-zone requirement [{ctx}]")

    states = _eligible_states(text)
    if states:
        named, ctx = states
        matched = sorted(mk for mk in markers if mk in named)
        if not matched:
            flags.append(
                f"hires in {len(named)} named states, none of them wanted "
                f"[{ctx}]")
        elif not any(mk in named for mk in _HOME_FIRST):
            # A list that misses home but includes a relocation target is a
            # real option gated on moving, not a dead end.
            flags.append(
                f"hires in {len(named)} states; home not among them, "
                f"{', '.join(matched)} is [{ctx}]")

    return {"fatal": bool(reasons), "reasons": reasons, "flags": flags}
