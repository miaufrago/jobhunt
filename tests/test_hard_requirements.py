"""Regression tests for the hard-requirements gate.

Every case here is a real posting this board collected. The false-positive cases
are the important ones: a wrong `fatal` is silent, because it drops the posting
below the queue threshold and no one ever reads it. Each was found by replaying
the gate against 212 assessments that had already been read by hand.
"""
from jobhunt.scoring import hard_requirements as hr

PROFILE = {"hard_requirements": {
    "enabled": True,
    "years_experience": 4.5,
    "years_tolerance": 4,
    "home_markers": ["new orleans", "metairie", "louisiana", "seattle",
                     "bellevue", "redmond", "kirkland", "washington"],
}}

PAD = " Responsibilities include working with cross-functional partners. " * 12


def check(body, location=""):
    return hr.check({"description": body + PAD, "location": location}, PROFILE)


# ------------------------------------------------------------------ gates

def test_twelve_years_gates():
    assert check("Requirements: 12+ years of experience in the medical "
                 "devices industry or related consulting.")["fatal"]


def test_ten_years_gates():
    assert check("A minimum of 10+ years of varied PM experience across "
                 "startups and scaled companies.")["fatal"]


def test_not_a_remote_role_gates():
    assert check("This is not a remote role. You must be in the local area "
                 "or be willing to relocate.")["fatal"]


def test_hybrid_days_gates():
    assert check("This position is a hybrid role based with three days "
                 "onsite and two days remote per week.")["fatal"]


def test_state_allowlist_without_her_state_gates():
    assert check("Please only apply if you live in one of the following "
                 "states: Illinois, Michigan, Wisconsin, Texas.")["fatal"]


def test_single_region_remote_gates():
    assert check("This role can be based out of our Palo Alto office or "
                 "remotely in the East Coast region.")["fatal"]


# ----------------------------------------------------- must never gate

def test_four_years_is_reachable():
    """Breakout Learning. She clears 4+; gating it would lose a real match."""
    assert not check("4+ years of product management experience, or "
                     "equivalent experience owning discovery and delivery.")["fatal"]


def test_five_years_within_tolerance():
    assert not check("5+ years of product management experience leading "
                     "enterprise or consumer products.")["fatal"]


def test_company_age_is_not_a_requirement():
    """Qventus: 'On this journey for over 12 years' is the company's age."""
    assert not check("On this journey for over 12 years, Qventus is leading "
                     "the transformation of healthcare.")["fatal"]


def test_degree_alternatives_take_the_lowest_path():
    """Amgen lists OR-ed entry paths; a Master's holder needs the shortest."""
    assert not check(
        "Basic Qualifications: Master's degree and 3 years of experience OR "
        "Associate degree and 6 years of experience OR High school diploma "
        "and 8 years of experience.")["fatal"]


def test_nationwide_residency_is_not_a_restriction():
    """Rentvine: 'MUST currently reside in the United States' — she does."""
    assert not check("You MUST currently reside in the United States of "
                     "America to be considered.")["fatal"]


def test_preferred_section_does_not_gate():
    assert not check("Requirements: 4+ years of product management. "
                     "Preferred qualifications: 12 years of experience "
                     "building developer platforms.")["fatal"]


def test_onsite_in_a_wanted_metro_does_not_gate():
    """Visa: 'at least 3 days in office' — but the office is in Bellevue."""
    assert not check("Visa requires at least 3 days in office each week.",
                     location="Bellevue, WA")["fatal"]


def test_remote_offered_alongside_onsite_does_not_gate():
    """Trimble: 'Onsite in Westminster, CO or Dayton, OH, or Remote-US'."""
    assert not check("Location: Onsite in Westminster, CO or Dayton, OH, "
                     "or Remote-US.")["fatal"]


def test_short_description_never_gates():
    assert not hr.check({"description": "12+ years required."}, PROFILE)["fatal"]


def test_disabled_by_default():
    assert not hr.check({"description": "12+ years of experience required. "
                         + PAD}, {})["fatal"]


# ------------------------------------------------------------- reporting

def test_near_miss_is_a_flag_not_a_gate():
    res = check("7+ years of product management experience with technical "
                "platforms or complex systems.")
    assert not res["fatal"]
    assert res["flags"] and "7" in res["flags"][0]


def test_reason_quotes_what_matched():
    res = check("Requirements: 12+ years of experience in medical devices.")
    assert res["fatal"] and "12" in res["reasons"][0]


# ------------------------------------------- markdown-escaped descriptions

def test_escaped_plus_years_still_gates():
    r"""Aggregators emit "10\+ years". The backslash defeated the years regex,
    and 3,887 of 4,858 open postings in one corpus used the escaped form -- so
    roughly 80% were slipping past the gate entirely."""
    assert check(r"Qualifications: 10\+ years of experience in Product "
                 r"Operations or Business Operations roles.")["fatal"]


def test_escaped_hyphen_onsite_still_gates():
    r"""Success Academy: "on\-site work Tuesday through Thursday" in a listing
    tagged remote. Read as a strong match until the escapes were stripped."""
    assert check(r"This is a hybrid position based in our NYC Financial District "
                 r"office, requiring on\-site work Tuesday through Thursday and "
                 r"remote work on Monday and Friday.")["fatal"]


def test_escaped_text_does_not_create_false_gates():
    assert not check(r"4\+ years of product management experience, or "
                     r"equivalent experience owning discovery and delivery. "
                     r"We are a fully\-remote company.")["fatal"]
