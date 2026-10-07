"""Regressions for bugs that reached production and cost real data.

Each test names the failure it prevents. They are cheap and deterministic; none
touches the network or the deployed database.

    python -m pytest tests/ -q
"""
import os
import re
import subprocess
import sys

import pytest

from jobhunt import db, pipeline
from jobhunt.normalize import dedupe_key, norm_title
from jobhunt.scoring.locations import (SUBDIVISION_CODE, US_MARKERS,
                                       country_allowed, in_scope)


# --- C1: the hash that destroyed assessments -------------------------------

def test_text_hash_is_stable_across_processes():
    """str(hash()) is randomized per process.

    Every new process hashed a byte-identical description differently, so
    score_pending believed every posting had changed and overwrote real
    assessments with the keyword score. Morgan's board lost 17 that way.
    """
    text = "Director of Product Security. Seattle."
    here = db.text_hash(text)
    other = subprocess.run(
        [sys.executable, "-c",
         "from jobhunt import db; print(db.text_hash(%r))" % text],
        capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONHASHSEED": "random"},
    ).stdout.strip()
    assert here == other, "desc_hash differs between processes"


def test_text_hash_ignores_surrounding_whitespace_only():
    assert db.text_hash(" a ") == db.text_hash("a")
    assert db.text_hash("a") != db.text_hash("b")


# --- H1: distinct roles collapsed into one row -----------------------------

def test_distinct_roles_at_one_company_stay_distinct():
    """`Director - Product Security` and `Director - Data Engineering` were one
    row, because the title was split on the first dash before hashing."""
    a = dedupe_key("Acme", "Director - Product Security", "Seattle, WA")
    b = dedupe_key("Acme", "Director - Data Engineering", "Seattle, WA")
    assert a != b


def test_seniority_is_part_of_the_identity():
    """Principal Security Architect and Security Architect are different jobs."""
    assert norm_title("Principal Security Architect") != norm_title("Security Architect")
    assert dedupe_key("Acme", "Principal Security Architect", "Remote") != \
           dedupe_key("Acme", "Security Architect", "Remote")


def test_title_synonyms_still_collapse():
    """Spelling variants of the same role must remain one posting."""
    assert norm_title("Sr. Director, Security") == norm_title("Senior Director, Security")


# --- M6 / M7 / MH1: matching that ignored word boundaries ------------------

def test_domain_phrase_needs_a_word_boundary():
    """`iam` matched inside `Miami`, awarding identity-domain credit to any
    posting in Florida."""
    pat = re.compile(r"(?<![a-z])" + re.escape("iam") + r"(?![a-z])", re.I)
    assert not pat.search("Miami, FL")
    assert pat.search("IAM Program Lead")


@pytest.mark.parametrize("loc", ["Remote, ON", "Remote, BC", "Remote, SG", "Remote, KA"])
def test_foreign_subdivisions_are_not_us(loc):
    assert SUBDIVISION_CODE.findall(loc)
    assert not US_MARKERS.search(loc)


@pytest.mark.parametrize("loc", ["Seattle, WA", "Boston, MA", "Remote - USA",
                                 "United States"])
def test_us_locations_still_read_as_us(loc):
    assert US_MARKERS.search(loc)


def test_country_only_remote_outside_the_us_is_dropped():
    cfg = {"profile": {"remote_scope": "us_only", "locations": ["Seattle"]}}
    ok, _ = country_allowed("Remote, Brazil", cfg)
    assert not ok
    ok, _ = country_allowed("Remote, United States", cfg)
    assert ok


def test_kent_does_not_match_kentucky():
    """Plain containment gave a Kentucky posting full credit for target metro
    `Kent`, which is a Seattle suburb."""
    cfg = {"profile": {"remote_scope": "us_only", "locations": ["Kent"]}}
    ok, _ = in_scope({"location": "Louisville, KY", "remote": 0,
                      "description": ""}, cfg)
    assert not ok
    ok, _ = in_scope({"location": "Kent, WA", "remote": 0, "description": ""}, cfg)
    assert ok


def test_washington_dc_is_not_washington_state():
    cfg = {"profile": {"remote_scope": "us_only", "locations": ["Washington"]}}
    ok, _ = in_scope({"location": "Washington, DC", "remote": 0,
                      "description": ""}, cfg)
    assert not ok


# --- MH3: the run lock a restart could not clear ---------------------------

def test_lock_token_includes_more_than_the_pid():
    """The app runs as PID 1 under `exec`. A PID-only token meant the new
    process after a restart answered for the dead run's lock forever."""
    token = pipeline._proc_boot_token()
    assert token.startswith(f"{os.getpid()}:")


def test_a_dead_process_token_is_not_alive():
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True)
    pid = int(dead.stdout.strip())
    assert not pipeline._token_alive(f"{pid}:1")


def test_current_process_token_is_alive():
    assert pipeline._token_alive(pipeline._proc_boot_token())


# --- Politeness: one platform must not monopolise the fairness order -------

def test_employers_are_spread_across_platforms():
    """Least-recently-collected ordering put four Workday tenants next to each
    other; all four timed out in a row."""
    employers = ([{"name": f"wd{i}", "type": "workday"} for i in range(4)] +
                 [{"name": f"gh{i}", "type": "greenhouse"} for i in range(3)])
    order = [e["type"] for e in pipeline._spread_by_platform(employers)]
    runs = max(len(list(g)) for g in _runs(order))
    assert runs == 1, f"same platform repeated consecutively: {order}"
    assert len(order) == 7, "interleaving must not drop or duplicate employers"


def _runs(seq):
    import itertools
    return [list(g) for _, g in itertools.groupby(seq)]


def test_slow_platforms_get_a_longer_timeout_not_a_faster_rate():
    """Workday answers in ~20s; a 10s connect timeout recorded it as broken.
    The fix must extend the timeout without touching the rate limit."""
    from jobhunt.sources import http

    assert http.PLATFORM_TIMEOUTS["myworkdayjobs.com"][0] > http.TIMEOUT[0]
    assert http.PLATFORM_LIMITS["myworkdayjobs.com"] >= 1.2, \
        "Workday's minimum interval must not be relaxed"


# --- H2 / H4: merging that made a bad row permanent ------------------------

@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A throwaway database on the real schema."""
    from jobhunt import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init()
    with db.connect() as con:
        yield con


def _posting(**over):
    base = {"company": "Acme", "title": "Director of Security",
            "location": "Seattle, WA", "source": "greenhouse",
            "description": "Own the security program.", "url": "https://x/1"}
    base.update(over)
    return base


def test_longer_description_does_not_overwrite_the_employer_text(store):
    """Longest-wins let LinkedIn boilerplate replace a clean employer JD, and
    made a bad description permanent -- no later fetch could ever beat it."""
    db.upsert_job(store, _posting())
    db.upsert_job(store, _posting(source="linkedin",
                                  description="About us. " * 200))
    row = store.execute("SELECT description FROM jobs").fetchone()
    assert row["description"] == "Own the security program."


def test_the_same_source_may_correct_its_own_description(store):
    db.upsert_job(store, _posting(description="stale text"))
    db.upsert_job(store, _posting(description="corrected text"))
    row = store.execute("SELECT description FROM jobs").fetchone()
    assert row["description"] == "corrected text"


def test_a_better_source_replaces_the_title_not_only_the_url(store):
    """Merges updated url and description but never the title, so the board
    kept whichever spelling an aggregator happened to file first.

    Only variants that normalise the same can merge at all -- the title is part
    of the dedupe key -- so this covers the case that actually occurs: the
    employer's own wording replacing an aggregator's abbreviation.
    """
    db.upsert_job(store, _posting(source="linkedin", title="Sr. Director, Security"))
    db.upsert_job(store, _posting(source="greenhouse",
                                  title="Senior Director, Security"))
    rows = store.execute("SELECT title, best_source FROM jobs").fetchall()
    assert len(rows) == 1, "variants of one title must not become two postings"
    assert rows[0]["title"] == "Senior Director, Security"
    assert rows[0]["best_source"] == "greenhouse"


def test_a_different_title_is_a_different_posting(store):
    """The counterpart: a genuinely different role must never merge, however
    similar the company and location."""
    db.upsert_job(store, _posting(title="Director - Product Security"))
    db.upsert_job(store, _posting(title="Director - Data Engineering"))
    assert store.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 2


def test_pay_is_taken_as_a_pair_never_mixed(store):
    """Mixing a new minimum with an old maximum produced inverted ranges such
    as min=260000, max=200000."""
    db.upsert_job(store, _posting(salary_min=200000, salary_max=260000,
                                  salary_period="yearly", salary_source="feed"))
    db.upsert_job(store, _posting(source="linkedin", salary_min=260000,
                                  salary_max=None))
    row = store.execute("SELECT salary_min, salary_max FROM jobs").fetchone()
    assert row["salary_min"] <= row["salary_max"]
    assert (row["salary_min"], row["salary_max"]) == (200000, 260000)


def test_an_assessment_does_not_erase_what_the_gate_predicted(store):
    """The gate's score was overwritten by the assessment that judges it, so
    the gate's accuracy could not be measured after the fact."""
    from jobhunt import score
    jid, _, _ = db.upsert_job(store, _posting())
    store.commit()
    score._save(jid, "heuristic", {"fit_score": 88, "verdict": "strong"},
                "heuristic", "h1")
    score._save(jid, "scored", {"fit_score": 31, "verdict": "no"},
                "claude-code", "h1")
    with db.connect() as con:
        row = con.execute("SELECT fit_score, heuristic_score FROM scores "
                          "WHERE job_id=?", (jid,)).fetchone()
    assert row["fit_score"] == 31
    assert row["heuristic_score"] == 88


def test_the_heuristic_pass_never_overwrites_an_assessment(store):
    """A changed description or a newly added exclusion pattern used to replace
    a real assessment with a keyword score. Seven were lost in one run."""
    from jobhunt import score
    jid, _, _ = db.upsert_job(store, _posting())
    store.commit()
    score._save(jid, "scored", {"fit_score": 76, "verdict": "strong",
                                "headline": "real read"}, "claude-code", "h1")
    score._save(jid, "heuristic", {"fit_score": 40, "verdict": "weak",
                                   "headline": "keyword guess"}, "heuristic", "h2")
    score._save(jid, "filtered", {"fit_score": 0, "verdict": "no",
                                  "headline": "title excluded"}, "rules", "h2")
    with db.connect() as con:
        row = con.execute("SELECT * FROM scores WHERE job_id=?", (jid,)).fetchone()
    assert row["stage"] == "scored"
    assert row["fit_score"] == 76
    assert row["headline"] == "real read"
    assert row["heuristic_score"] == 0, "the gate's latest view is still recorded"


# --- one job listed in many cities is still one job ------------------------

def test_one_role_in_many_cities_shares_a_role_key(store):
    """Databricks lists a single role across forty-three cities. Those are
    forty-three rows by dedupe key and one job to a human."""
    ids = []
    for city in ("Seattle, WA", "Austin, TX", "Remote - US"):
        jid, _, _ = db.upsert_job(store, _posting(location=city))
        ids.append(jid)
    assert len(set(ids)) == 3, "distinct locations remain distinct rows"
    keys = {r["role_key"] for r in store.execute("SELECT role_key FROM jobs")}
    assert len(keys) == 1, "but they group under one role"


def test_different_roles_do_not_share_a_role_key(store):
    db.upsert_job(store, _posting(title="Director of Security"))
    db.upsert_job(store, _posting(title="Director of Data Engineering"))
    keys = {r["role_key"] for r in store.execute("SELECT role_key FROM jobs")}
    assert len(keys) == 2


def test_role_key_survives_a_merge_from_a_better_source(store):
    db.upsert_job(store, _posting(source="linkedin", title="Sr. Director, Security"))
    db.upsert_job(store, _posting(source="greenhouse", title="Senior Director, Security"))
    row = store.execute("SELECT title, role_key FROM jobs").fetchone()
    assert row["role_key"] == db.role_key("Acme", row["title"])


# --- phase split ------------------------------------------------------------

def test_the_two_phases_split_by_how_a_source_is_read():
    """Employers' own ATS feeds are cheap and authoritative; keyword searches
    are expensive and starve them when the two share a thread pool."""
    import inspect
    from jobhunt import pipeline

    src = inspect.getsource(pipeline.collect)
    assert '("searches", search_tasks, 3)' in src
    assert '("employers", employer_tasks, 6)' in src
    assert '"feeds"' not in src, "the old phase name survived the rename"


def test_every_search_source_is_in_the_search_phase():
    """A source added to the pipeline but left out of SEARCH_SOURCES would run
    in the employers phase and starve the ATS feeds."""
    import inspect
    from jobhunt import pipeline

    src = inspect.getsource(pipeline.collect)
    for name in ("aggregators", "dice", "linkedin"):
        assert f'"{name}"' in src.split("SEARCH_SOURCES = ")[1].split(")")[0], \
            f"{name} is not classified as a search source"


# --- LinkedIn: filter at the source, and know what the tool can do ---------

def test_search_filters_are_passed_through_to_linkedin():
    """LinkedIn's own seniority and employment-type filters cost nothing and
    remove noise the rules gate would otherwise discard."""
    import inspect
    from jobhunt.sources import linkedin_mcp

    src = inspect.getsource(linkedin_mcp.fetch_linkedin)
    assert '("experience_level", "job_type", "work_type")' in src
    assert "**extra," in src


def test_company_scoped_search_is_gone():
    """search_jobs has no company parameter. Putting a company name in keywords
    is just another keyword: 'Apple engineering manager' returned Amazon,
    Microsoft and Tanium postings, and 'Apple security' returned Apple retail
    store jobs. The feature was removed rather than left to mislead."""
    from jobhunt.sources import linkedin_mcp

    assert not hasattr(linkedin_mcp, "company_queries")
    import inspect
    src = inspect.getsource(linkedin_mcp.fetch_linkedin)
    assert "company_searches_per_run" not in src
