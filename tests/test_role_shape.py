"""Role shape: an IC seat is fine, a deep specialist seat is not.

Seniority cannot express this. "Principal" covers both "sets direction across
an org" and "personally writes the policy engine", and the candidate is right
for one and wrong for the other.
"""
from jobhunt.scoring.heuristic import _score_shape

PROF = {"role_shape": {
    "depth_penalty": 12, "leadership_bonus": 6,
    "depth_markers": ["years of coding experience", "develop and use fuzzers",
                      "kubernetes", "principal software engineer"],
    "leadership_markers": ["lead a team of", "hire and mentor", "direct reports"],
}}


def test_a_leadership_role_gains_points():
    pts, strengths, _ = _score_shape(
        "you will lead a team of security engineers and hire and mentor them", PROF)
    assert pts > 0
    assert strengths


def test_a_deep_specialist_role_loses_points():
    pts, _, gaps = _score_shape(
        "as a principal software engineer you will develop and use fuzzers, "
        "harden kubernetes, and bring 8 years of coding experience", PROF)
    assert pts <= -12
    assert any("specialist" in g for g in gaps)


def test_one_depth_mention_is_a_preference_not_the_job():
    """A single passing mention must not sink an otherwise good posting."""
    single, _, _ = _score_shape("some exposure to kubernetes is useful", PROF)
    many, _, _ = _score_shape(
        "principal software engineer; develop and use fuzzers; kubernetes", PROF)
    assert -12 < single < 0
    assert many < single


def test_a_leadership_role_with_technical_depth_is_not_punished_as_a_specialist():
    """A director who owns a Kubernetes platform still leads people."""
    pts, _, gaps = _score_shape(
        "lead a team of engineers owning our kubernetes platform", PROF)
    assert pts > -12
    assert not any("specialist seat" in g for g in gaps)


def test_no_config_means_no_adjustment():
    pts, s, g = _score_shape("anything at all", {})
    assert (pts, s, g) == (0.0, [], [])


def test_a_forced_rescore_refreshes_the_gate_but_keeps_the_assessment(tmp_path,
                                                                      monkeypatch):
    """The guard belongs at the write path, not the query.

    Excluding assessed rows from the forced rescore froze heuristic_score, so
    `calibrate` reported the same correlation no matter how the scorer changed.
    """
    from jobhunt import config, db, score

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init()
    with db.connect() as con:
        jid, _, _ = db.upsert_job(con, {
            "company": "Acme", "title": "Director of Security",
            "location": "Seattle, WA", "source": "greenhouse",
            "description": "Lead a team of security engineers.", "url": "u"})
        con.commit()

    score._save(jid, "heuristic", {"fit_score": 88, "verdict": "strong"},
                "heuristic", "h1")
    score._save(jid, "scored", {"fit_score": 40, "verdict": "weak",
                                "headline": "real read"}, "claude-code", "h1")
    # a later gate pass sees the posting differently
    score._save(jid, "heuristic", {"fit_score": 61, "verdict": "possible"},
                "heuristic", "h1")

    with db.connect() as con:
        row = con.execute("SELECT * FROM scores WHERE job_id=?", (jid,)).fetchone()
    assert row["fit_score"] == 40, "the assessment must survive"
    assert row["headline"] == "real read"
    assert row["heuristic_score"] == 61, "the gate's new view must be recorded"
