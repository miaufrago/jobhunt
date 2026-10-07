"""Deterministic fit scoring. No model, no network, no cost.

This is not keyword similarity. It scores five things a job seeker actually cares
about -- level, domain, compensation, geography, and listing quality -- and reports
each separately so the number can be argued with.

Nothing here is specific to one candidate. Weights, target level band, domain
phrases, pay policy, and verdict thresholds all come from `profile` in config.yaml;
the constants below are only fallbacks.
"""
import re

from . import hard_requirements
from .locations import in_scope, is_remote

# ---------------------------------------------------------------- seniority

LEVELS = [
    (0, r"\b(intern|internship|co-?op|apprentice|entry.level|associate|analyst\s+i\b)\b"),
    (1, r"\b(engineer|analyst|specialist|administrator|consultant|architect)\b"),
    (2, r"\b(senior|sr\.?|lead|staff)\b.{0,25}\b(engineer|analyst|architect|consultant)\b"),
    (3, r"\b(principal|distinguished|senior\s+staff)\b"),
    (4, r"\b(manager|mgr)\b"),
    (5, r"\b(senior\s+manager|sr\.?\s+manager|group\s+manager)\b"),
    (6, r"\b(director|head\s+of)\b"),
    (7, r"\b(senior\s+director|sr\.?\s+director|executive\s+director)\b"),
    (8, r"\b(vice\s+president|vp|svp|evp)\b"),
    (9, r"\b(chief|ciso|cso|cto|ceo|c-level)\b"),
]

LEVEL_NAMES = {0: "entry", 1: "IC", 2: "senior IC", 3: "principal IC", 4: "manager",
               5: "senior manager", 6: "director", 7: "senior director",
               8: "VP", 9: "chief"}


def title_level(title: str) -> int:
    """Highest level signal present in the title. -1 when nothing matches."""
    best = -1
    for lvl, pat in LEVELS:
        if re.search(pat, title or "", re.I):
            best = max(best, lvl)
    return best


# ---------------------------------------------------------------- domains
# Fallback only. Every profile overrides this via `profile.domains` -- it is the
# most candidate-specific thing in the scorer, and a different career means a
# completely different list.

DEFAULT_DOMAINS = {
    "security assurance / SDL": (10, [
        "security assurance", "secure development", "sdl", "security design review",
        "threat model", "security review", "secure by design", "product security"]),
    "application / offensive security": (9, [
        "application security", "appsec", "penetration test", "pentest", "red team",
        "offensive security", "vulnerability management", "bug bounty"]),
    "identity & access": (8, [
        "identity", "iam", "access management", "authentication", "authorization",
        "sso", "federation", "adfs", "okta", "entra", "active directory", "zero trust"]),
    "org leadership": (10, [
        "build and lead", "grow the team", "hire", "org design", "people manager",
        "manage managers", "leadership team", "headcount", "budget", "roadmap",
        "cross-functional", "executive", "okr", "kpi"]),
    "governance / risk / compliance": (5, [
        "governance", "risk management", "compliance", "audit", "soc 2", "fedramp",
        "iso 27001", "policy", "controls"]),
}


def domains_for(cfg: dict) -> dict:
    """Weighted domain phrases from the profile, falling back to the defaults."""
    raw = (cfg.get("profile") or {}).get("domains")
    if not raw:
        return DEFAULT_DOMAINS
    out = {}
    for name, spec in raw.items():
        if isinstance(spec, dict):
            out[name] = (int(spec.get("weight", 5)),
                         [p.lower() for p in spec.get("phrases", [])])
        elif isinstance(spec, (list, tuple)) and len(spec) == 2:
            out[name] = (int(spec[0]), [p.lower() for p in spec[1]])
    return out or DEFAULT_DOMAINS


# Things that make a posting not worth the click regardless of keyword overlap.
# `profile.extra_disqualifiers` appends to this; the built-ins are generic.
DISQUALIFIERS = [
    (30, "staffing-agency listing", [
        "our client", "on behalf of our client", "c2c", "corp to corp", "w2 only",
        "third party resumes", "1099", "recruiting firm", "staffing"]),
    (25, "contract, not a permanent role", [
        "contract position", "contract role", "months contract", "month contract",
        "contract-to-hire", "temporary assignment", "hourly rate"]),
    (20, "clearance required", [
        "active clearance", "security clearance required", "ts/sci", "top secret",
        "must be a us citizen with", "polygraph"]),
    (10, "fully on-site mandate", [
        "5 days a week in office", "fully onsite", "100% on-site", "onsite daily"]),
]

_PHRASE_CACHE: dict[str, "re.Pattern"] = {}


def _phrase_re(phrase: str):
    """Word-boundary matcher for one domain phrase.

    Plain substring containment matched "iam" inside "Miami", "sso" inside
    "associate" and "hire" inside "Hampshire", which scored postings with no
    real domain content high enough to be queued for a paid assessment.
    """
    pat = _PHRASE_CACHE.get(phrase)
    if pat is None:
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", re.I)
        _PHRASE_CACHE[phrase] = pat
    return pat


def _hits(text: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases if _phrase_re(p).search(text)]


def _score_shape(text: str, prof: dict) -> tuple[float, list[str], list[str]]:
    """Does the posting want breadth and leadership, or specialist depth?

    Seniority alone does not say. A principal role can mean "sets direction
    across an org" or "personally writes the policy engine", and the candidate
    may be right for one and wrong for the other. The level scale cannot express
    that, because both are the same rung.

    So this reads the requirements rather than the title. A posting demanding
    sustained hands-on depth in a named technology loses points; one that asks
    for people leadership gains a few. Both lists live in the profile, because
    which depth a candidate lacks is entirely personal.
    """
    shape = prof.get("role_shape") or {}
    depth_cost = float(shape.get("depth_penalty", 12))
    lead_bonus = float(shape.get("leadership_bonus", 6))

    depth_hits = _hits(text, [p.lower() for p in shape.get("depth_markers", [])])
    lead_hits = _hits(text, [p.lower() for p in shape.get("leadership_markers", [])])

    strengths: list[str] = []
    gaps: list[str] = []
    pts = 0.0

    if lead_hits:
        pts += lead_bonus
        strengths.append(f"Carries people leadership: {lead_hits[0]}.")

    if depth_hits:
        # Scale with how many independent depth demands the posting makes: one
        # mention is a preference, four is the whole job.
        scale = min(1.0, len(depth_hits) / 3.0)
        pts -= depth_cost * scale
        gaps.append("Asks for sustained hands-on specialist depth: "
                    + ", ".join(depth_hits[:3]) + ".")
        if scale >= 1.0 and not lead_hits:
            gaps.append("Reads as a deep specialist seat rather than a "
                        "breadth-and-leadership one.")
    return pts, strengths, gaps


# ---------------------------------------------------------------- rules gate

def passes_rules(job: dict, cfg: dict) -> tuple[bool, str]:
    """Cheap structural filter, applied before anything scores."""
    p = cfg.get("profile") or {}
    title = job.get("title") or ""
    for pat in p.get("exclude_title_patterns", []):
        if re.search(pat, title):
            return False, "title excluded by config pattern"
    if p.get("include_title_patterns"):
        if not any(re.search(pat, title) for pat in p["include_title_patterns"]):
            return False, "title matched no include pattern"
    return in_scope(job, cfg)


# ---------------------------------------------------------------- scorer

# Relative weights. Overridable via `profile.scoring_weights`. They are
# normalized, so they need not add up to 100.
DEFAULT_WEIGHTS = {"level": 25, "domain": 25, "pay": 30, "location": 12, "listing": 8}

DEFAULT_VERDICTS = {"strong": 72, "possible": 55, "weak": 35}

# Where a posting lands once it fails a hard requirement. Below every verdict
# threshold and below the queue's deep_score_threshold, so it stops consuming
# assessment slots -- but non-zero, so the row still shows why when read directly.
HARD_FAIL_SCORE = 20


def _weights(cfg: dict) -> dict:
    w = dict(DEFAULT_WEIGHTS)
    w.update((cfg.get("profile") or {}).get("scoring_weights") or {})
    total = sum(max(0.0, float(v)) for v in w.values()) or 1.0
    return {k: 100 * max(0.0, float(v)) / total for k, v in w.items()}


def _pay_policy(cfg: dict) -> dict:
    p = dict((cfg.get("profile") or {}).get("pay") or {})
    p.setdefault("target_base", None)
    p.setdefault("floor", None)
    # What an unstated range scores, as a fraction of the pay weight. Half credit
    # by default: silence is not evidence either way, and about half of postings
    # omit it.
    p.setdefault("unstated_credit", 0.5)
    # A stated range below the floor caps the verdict however good the fit is.
    p.setdefault("floor_caps_verdict_at", "weak")
    return p


def _k(v) -> str:
    return f"${v / 1000:.0f}K"


def _score_pay(job, policy, weight, strengths, gaps, red_flags):
    """Points for compensation, and whether the floor was breached."""
    target = policy.get("target_base")
    floor = policy.get("floor")
    # Fall back to the minimum when only one bound is stated. Reading salary_max
    # alone treated a min-only posting as "pay unstated", so a $120K role scored
    # strong against a $250K floor.
    hi = job.get("salary_max") or job.get("salary_min")
    lo = job.get("salary_min") or hi

    if not hi:
        gaps.append("No pay range stated.")
        return weight * float(policy.get("unstated_credit", 0.5)), False

    if floor and hi < floor:
        red_flags.append(f"Pay tops out at {_k(hi)}, below the {_k(floor)} floor.")
        return 0.0, True

    if not target:
        return weight * float(policy.get("unstated_credit", 0.5)), False

    if lo >= target:
        strengths.append(f"Pay starts at {_k(lo)}, at or above the {_k(target)} target.")
        return weight, False

    if hi >= target:
        # The range straddles the target: credit the share of the band above it.
        span = max(hi - lo, 1)
        share = min(1.0, max(0.0, (hi - target) / span))
        strengths.append(f"Pay range {_k(lo)}-{_k(hi)} reaches the {_k(target)} target.")
        return weight * (0.55 + 0.45 * share), False

    # Below target but above the floor: scale across the gap.
    base = floor or target * 0.6
    frac = min(1.0, max(0.0, (hi - base) / max(target - base, 1)))
    gaps.append(f"Pay tops out at {_k(hi)}, under the {_k(target)} target.")
    return weight * 0.55 * frac, False


def heuristic_score(job: dict, cfg: dict) -> dict:
    """Same shape a model backend returns, so the UI stays identical."""
    prof = cfg.get("profile") or {}
    w = _weights(cfg)
    policy = _pay_policy(cfg)
    title = job.get("title") or ""
    text = f"{title}\n{job.get('description') or ''}".lower()

    strengths: list[str] = []
    gaps: list[str] = []
    red_flags: list[str] = []

    # --- level --------------------------------------------------------------
    # `target_levels` is an explicit set and wins when present; it can express
    # things a range cannot, such as "principal IC, or director and above" while
    # skipping the manager rungs in between.
    allowed = prof.get("target_levels")
    if allowed:
        allowed = sorted(int(x) for x in allowed)
        lo_lvl, hi_lvl = allowed[0], allowed[-1]
    else:
        lo_lvl, hi_lvl = prof.get("target_level_range", [6, 9])
        allowed = list(range(lo_lvl, hi_lvl + 1))

    lvl = title_level(title)
    if lvl < 0:
        level_pts = w["level"] * 0.45
        gaps.append("Title carries no clear seniority signal.")
    elif lvl in allowed:
        level_pts = w["level"]
        strengths.append(f"Level matches: posting reads as {LEVEL_NAMES[lvl]}.")
    elif lvl > hi_lvl:
        level_pts = w["level"] * 0.65
        gaps.append(f"Posting reads as {LEVEL_NAMES[lvl]}, above the target band.")
    elif lo_lvl < lvl < hi_lvl:
        # Inside the overall span but on a rung that was deliberately skipped.
        level_pts = w["level"] * 0.6
        gaps.append(f"Posting reads as {LEVEL_NAMES[lvl]}, a rung outside the target.")
    else:
        level_pts = max(0.0, w["level"] * (1 - 0.33 * (lo_lvl - lvl)))
        gaps.append(f"Posting reads as {LEVEL_NAMES[lvl]}, a downlevel.")
        if lo_lvl - lvl >= 2:
            red_flags.append(f"Downlevel: {LEVEL_NAMES[lvl]} against a "
                             f"{LEVEL_NAMES[lo_lvl]}-plus target.")

    # --- domain overlap -----------------------------------------------------
    domains = domains_for(cfg)
    total_w = sum(x for x, _ in domains.values()) or 1
    earned = 0
    matched: list[str] = []
    for name, (weight, phrases) in domains.items():
        found = _hits(text, phrases)
        if found:
            earned += weight
            matched.append(f"{name} ({', '.join(found[:3])})")
    domain_pts = w["domain"] * earned / total_w
    if matched:
        strengths.append("Domain overlap: " + "; ".join(matched[:4]) + ".")
    top_weight = max((x for x, _ in domains.values()), default=0)
    missing = [n for n, (x, ph) in domains.items()
               if x >= max(1, top_weight - 1) and not _hits(text, ph)]
    if missing:
        gaps.append("No mention of " + ", ".join(missing[:3]) + ".")

    # --- compensation -------------------------------------------------------
    pay_pts, below_floor = _score_pay(job, policy, w["pay"], strengths, gaps, red_flags)

    # --- geography ----------------------------------------------------------
    loc = (job.get("location") or "").lower()
    remote = is_remote(job.get("location", ""), job.get("description", ""),
                       bool(job.get("remote")))
    wanted = [x.lower() for x in prof.get("locations", []) if x.lower() != "remote"]
    if any(x in loc for x in wanted):
        geo_pts = w["location"]
        strengths.append(f"Location is in the target area ({job.get('location')}).")
    elif remote:
        geo_pts = w["location"] * 0.87
        strengths.append("Remote-eligible.")
    elif not loc:
        geo_pts = w["location"] * 0.45
        gaps.append("Posting states no location.")
    else:
        geo_pts = w["location"] * 0.2
        gaps.append(f"Location '{job.get('location')}' is outside the target area.")

    # --- listing quality ----------------------------------------------------
    penalty = 0
    extra = [(int(d.get("cost", 15)), d.get("label", "disqualifier"),
              [p.lower() for p in d.get("phrases", [])])
             for d in (prof.get("extra_disqualifiers") or [])]
    for cost, label, phrases in DISQUALIFIERS + extra:
        found = _hits(text, phrases)
        if found:
            penalty += cost
            red_flags.append(f"{label} ({found[0]}).")
    listing_pts = w["listing"] if penalty == 0 else 0.0

    # --- industry preference ------------------------------------------------
    # A nudge, never a gate. The candidate may want out of an industry while
    # still wanting to see what it offers, so a disfavoured industry loses a few
    # points and says so, rather than disappearing.
    industry_pts = 0.0
    pref = prof.get("industry_preference") or {}
    bonus = float(pref.get("bonus", 6))
    blob = f"{job.get('company', '')} {title} {(job.get('description') or '')[:1500]}".lower()
    if any(p.lower() in blob for p in pref.get("prefer", [])):
        industry_pts = bonus
        strengths.append("Outside the industry she is moving away from.")
    elif any(p.lower() in blob for p in pref.get("deprioritize", [])):
        industry_pts = -bonus
        gaps.append("In the industry she would rather move out of, though still "
                    "worth seeing.")

    # --- role shape ---------------------------------------------------------
    shape_pts, shape_str, shape_gaps = _score_shape(text, prof)
    strengths.extend(shape_str)
    gaps.extend(shape_gaps)

    raw = (level_pts + domain_pts + pay_pts + geo_pts + listing_pts
           + industry_pts + shape_pts - penalty)
    fit = int(max(0, min(100, round(raw))))

    thresholds = dict(DEFAULT_VERDICTS)
    thresholds.update(prof.get("verdict_thresholds") or {})
    verdict = ("strong" if fit >= thresholds["strong"] else
               "possible" if fit >= thresholds["possible"] else
               "weak" if fit >= thresholds["weak"] else "no")

    # Comp is a gate, not only a score: a posting that cannot pay the floor is
    # not a strong match however well it fits otherwise.
    cap = policy.get("floor_caps_verdict_at")
    if below_floor and cap:
        order = ["no", "weak", "possible", "strong"]
        if cap in order and order.index(verdict) > order.index(cap):
            verdict = cap

    # Stated bars the keyword scorer cannot read -- years of experience and
    # onsite/residency mandates buried in the body. These are gates, not signals,
    # so they cap the score below the queue threshold rather than subtracting
    # from it. See scoring/hard_requirements.py for why both rules are lenient.
    hard = hard_requirements.check(job, prof)
    if hard["fatal"]:
        fit = min(fit, HARD_FAIL_SCORE)
        verdict = "no"
        red_flags[:0] = [f"Hard requirement not met: {r}" for r in hard["reasons"]]
    gaps.extend(hard["flags"])

    rationale = (f"Level {level_pts:.0f}/{w['level']:.0f}, "
                 f"domain {domain_pts:.0f}/{w['domain']:.0f}, "
                 f"pay {pay_pts:.0f}/{w['pay']:.0f}, "
                 f"location {geo_pts:.0f}/{w['location']:.0f}, "
                 f"listing {listing_pts:.0f}/{w['listing']:.0f}"
                 + (f", shape {shape_pts:+.0f}" if shape_pts else "")
                 + (f", penalties -{penalty}" if penalty else "") + ".")

    return {
        "fit_score": fit,
        "verdict": verdict,
        "headline": _headline(verdict, lvl, matched, red_flags),
        "rationale": rationale,
        "strengths": strengths,
        "gaps": gaps,
        "red_flags": red_flags,
    }


def _headline(verdict: str, lvl: int, matched: list[str], flags: list[str]) -> str:
    if flags:
        return flags[0].rstrip(".")
    if verdict in ("strong", "possible") and matched:
        return f"{LEVEL_NAMES.get(lvl, 'unclear level')} role, {matched[0].split(' (')[0]}"
    if verdict == "no":
        return "Level, comp or domain does not match"
    return "Partial match, worth a look"
