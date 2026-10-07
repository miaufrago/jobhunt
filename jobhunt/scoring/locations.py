"""Where the candidate can actually work.

"Remote" is not a country. A remote posting in Brazil, Japan or Poland is not
reachable, and the location string is usually the only place that says so.

Two questions, in order:
  1. Is the posting in an allowed country at all?  -> country_allowed()
  2. Is it remote, or on-site somewhere in the target metros?  -> in_scope()
"""
import re

US_STATE_NAMES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    "florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    "louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    "missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    "new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    "rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    "virginia|washington|west virginia|wisconsin|wyoming|district of columbia"
)

# Only real US postal codes count, and only after a comma ("Seattle, WA"). An
# earlier `,\s*[A-Za-z]{2}\b` accepted any two-letter subdivision, so
# "Remote, ON", "Remote, BC" and "Remote, UK" all read as US and reached the
# board. A bare two-letter token is never enough -- "la" sits inside "Poland".
US_STATE_CODES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    "MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|"
    "WV|WI|WY|PR|VI|GU|AS|MP"
)

US_MARKERS = re.compile(
    r"\bunited states\b"
    r"|\bu\.?s\.?a\.?\b"
    r"|\bus\b"
    r"|\bnationwide\b"
    r"|,\s*(" + US_STATE_CODES + r")\b"
    r"|\b(" + US_STATE_NAMES + r")\b",
    re.I,
)

# Any ", XX" subdivision code. Used to detect FOREIGN subdivisions: if a code is
# present and no US marker is, the code is not a US state.
SUBDIVISION_CODE = re.compile(r",\s*([A-Za-z]{2})\b")

# Washington the state and Washington DC share a name. A profile targeting the
# state must not collect the District, which is on the other coast.
DC_MARKER = re.compile(r"\bwashington,?\s*(d\.?c\.?|district of columbia)\b"
                       r"|\bdistrict of columbia\b|,\s*DC\b", re.I)

NON_US_MARKERS = re.compile(
    r"\b("
    r"canada|toronto|vancouver|montreal|ottawa|calgary|ontario|quebec|"
    r"british columbia|alberta|"
    r"mexico|guadalajara|brazil|brasil|sao paulo|argentina|buenos aires|"
    r"colombia|bogota|chile|santiago|peru|lima|costa rica|uruguay|"
    r"united kingdom|u\.?k\.?|england|london|manchester|scotland|edinburgh|"
    r"ireland|dublin|belfast|"
    r"germany|berlin|munich|hamburg|frankfurt|france|paris|lyon|"
    r"spain|madrid|barcelona|portugal|lisbon|porto|"
    r"netherlands|amsterdam|rotterdam|belgium|brussels|luxembourg|"
    r"sweden|stockholm|norway|oslo|denmark|copenhagen|finland|helsinki|"
    r"poland|warsaw|krakow|wroclaw|czech|prague|slovakia|"
    r"romania|bucharest|hungary|budapest|bulgaria|sofia|serbia|belgrade|"
    r"croatia|greece|athens|italy|milan|rome|switzerland|zurich|geneva|"
    r"austria|vienna|ukraine|kyiv|estonia|latvia|lithuania|"
    r"israel|tel aviv|jerusalem|turkey|istanbul|uae|dubai|abu dhabi|"
    r"saudi|qatar|egypt|cairo|south africa|cape town|johannesburg|"
    r"nigeria|lagos|kenya|nairobi|morocco|"
    r"india|bangalore|bengaluru|hyderabad|pune|mumbai|delhi|gurgaon|"
    r"noida|chennai|kolkata|"
    r"china|beijing|shanghai|shenzhen|guangzhou|hong kong|taiwan|taipei|"
    r"japan|tokyo|osaka|kyoto|korea|seoul|"
    r"singapore|malaysia|kuala lumpur|indonesia|jakarta|thailand|bangkok|"
    r"vietnam|hanoi|philippines|manila|"
    r"australia|sydney|melbourne|brisbane|perth|new zealand|auckland|"
    r"emea|apac|latam|anz|emea/apac|europe|asia[- ]pacific|"
    r"latin america|middle east"
    r")\b",
    re.I,
)

REMOTE_HINTS = re.compile(
    r"\b(remote|work from home|wfh|distributed|anywhere)\b", re.I)

# A location that names the country and nothing else.
COUNTRY_ONLY = re.compile(
    r"\s*(united states(\s+of\s+america)?|u\.?s\.?a?\.?|nationwide)\s*", re.I)


def region_of(location: str, cfg: dict) -> str | None:
    """Which configured region a posting belongs to, or None.

    A secondary region is somewhere the candidate would genuinely move for the
    right role, but which carries logistics the primary region does not --
    visas, relocation, a partner needing work too. Those postings are worth
    seeing and worth keeping out of the daily list, so they are tagged rather
    than dropped or mixed in.
    """
    loc = (location or "").strip()
    if not loc:
        return "primary"
    for name, spec in ((cfg.get("profile") or {}).get("secondary_regions") or {}).items():
        markers = spec.get("markers") if isinstance(spec, dict) else spec
        for m in markers or []:
            if re.search(r"(?<![a-z])" + re.escape(m.lower()) + r"(?![a-z])", loc, re.I):
                return name
    return "primary"


def country_allowed(location: str, cfg: dict) -> tuple[bool, str]:
    """False when the only location named is outside the allowed countries.

    A multi-region posting ("Remote, Israel; Remote, US") stays -- one acceptable
    location is enough. A posting with no location at all stays; silence is not
    evidence, and the metro check downstream still applies.
    """
    prof = cfg.get("profile") or {}
    if prof.get("remote_scope", "us_only") != "us_only":
        return True, "ok"
    loc = (location or "").strip()
    if not loc:
        return True, "ok"
    for pat in prof.get("exclude_location_patterns", []):
        if re.search(pat, loc, re.I):
            return False, f"location '{loc}' excluded by config pattern"
    if NON_US_MARKERS.search(loc) and not US_MARKERS.search(loc):
        return False, f"location '{loc}' is outside the US"

    # A subdivision code that is not a US state is a foreign one. Naming every
    # foreign region is hopeless -- "Remote, ON", "Remote, SG" and "Remote, KA"
    # named no country and slipped through as remote-eligible. Inverting the
    # test closes the whole class: unknown code, no US marker, not ours.
    codes = SUBDIVISION_CODE.findall(loc)
    if codes and not US_MARKERS.search(loc):
        return False, (f"location '{loc}' names subdivision "
                       f"{codes[0].upper()}, which is not a US state")
    return True, "ok"


def is_remote(location: str, description: str = "", flag: bool = False) -> bool:
    if flag:
        return True
    head = f"{location or ''} {(description or '')[:400]}"
    return bool(REMOTE_HINTS.search(head))


def in_scope(job: dict, cfg: dict) -> tuple[bool, str]:
    """Remote inside an allowed country, or on-site in one of the target metros.

    A posting in a configured secondary region is in scope regardless of the
    primary rules; the board separates it by region rather than by score.
    """
    prof = cfg.get("profile") or {}
    loc = job.get("location") or ""

    region = region_of(loc, cfg)
    if region != "primary":
        return True, f"in secondary region: {region}"

    ok, why = country_allowed(loc, cfg)
    if not ok:
        return False, why

    metros = [m.lower() for m in prof.get("locations", []) if m.lower() != "remote"]
    if not metros:
        return True, "ok"

    # Word-boundary match: plain containment made "Kent" match "Kentucky",
    # awarding full location credit to postings on the other side of the
    # country. DC is excluded separately because it shares a name with the state.
    dc = bool(DC_MARKER.search(loc))
    for m in metros:
        if m == "washington" and dc:
            continue
        if re.search(r"(?<![a-z])" + re.escape(m) + r"(?![a-z])", loc, re.I):
            return True, "ok"
    if is_remote(loc, job.get("description", ""), bool(job.get("remote"))):
        return True, "ok"
    if not loc:
        return True, "ok"
    if COUNTRY_ONLY.fullmatch(loc.strip()):
        # "United States" with no city or state is a national listing, which in
        # practice means remote or multi-site. Dropping these loses real roles.
        return True, "ok"
    return False, f"location '{loc}' is neither remote nor in the target metros"
