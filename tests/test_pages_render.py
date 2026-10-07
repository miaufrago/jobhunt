"""Every page must render.

A NameError inside a request handler is invisible to unit tests and to review:
the job detail page referenced a `cfg` that was never loaded there, and every
click returned 500 while the board itself looked fine. These tests exercise the
real routes against a real database.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from jobhunt import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from jobhunt import db, web

    db.init()
    with db.connect() as con:
        jid, _, _ = db.upsert_job(con, {
            "company": "Acme", "title": "Director of Security",
            "location": "Seattle, WA", "source": "greenhouse",
            "description": "Own the security program.",
            "url": "https://example.invalid/1"})
        db.upsert_job(con, {
            "company": "Acme", "title": "Director of Security",
            "location": "Austin, TX", "source": "greenhouse",
            "description": "Own the security program.",
            "url": "https://example.invalid/2"})
        con.commit()
    # Score them, or min_fit_to_show filters them off the board and the render
    # tests pass against an empty page.
    from jobhunt import score
    with db.connect() as con:
        for row in con.execute("SELECT id, desc_hash FROM jobs"):
            score._save(row["id"], "scored",
                        {"fit_score": 80, "verdict": "strong",
                         "headline": "worth a look", "rationale": "fits"},
                        "claude-code", row["desc_hash"])
    c = TestClient(web.app)
    c.job_id = jid
    return c


def test_board_renders(client):
    r = client.get("/")
    assert r.status_code == 200, r.text[:500]


def test_job_detail_renders(client):
    """The regression: this returned 500 for every posting on the board."""
    r = client.get(f"/job/{client.job_id}")
    assert r.status_code == 200, r.text[:800]


def test_job_detail_404s_for_an_unknown_id(client):
    assert client.get("/job/does-not-exist").status_code == 404


@pytest.mark.parametrize("path", ["/mutes", "/sources", "/settings", "/healthz"])
def test_other_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_board_filters_render(client):
    for qs in ("?region=all", "?region=primary", "?sort=score", "?sort=newest",
               "?scored=assessed", "?scored=pending", "?status=any", "?q=acme",
               "?min_pay=100", "?days=7"):
        r = client.get("/" + qs)
        assert r.status_code == 200, f"{qs} -> {r.status_code}: {r.text[:300]}"


def test_api_queue_and_stats_respond(client):
    assert client.get("/api/stats").status_code == 200
    assert client.get("/api/queue?limit=5").status_code == 200


def test_a_multi_location_role_shows_its_other_listings(client):
    """The fixture files one role in Seattle and Austin. The job page must say
    so, instead of presenting each city as a separate opening."""
    r = client.get(f"/job/{client.job_id}")
    assert r.status_code == 200
    assert "Also listed in 1 other location" in r.text
    assert "Austin, TX" in r.text


def test_the_board_shows_one_row_per_role(client):
    """Two rows underneath, one card on the board."""
    r = client.get("/?status=any")
    assert r.status_code == 200
    assert r.text.count("Director of Security") == 1, \
        "the same role appeared more than once on the board"


def test_assessment_health_reports_state(client):
    """Collection self-heals; assessment cannot. A stopped loop must be visible
    on the board rather than looking like a healthy but quiet system."""
    r = client.get("/api/health/assessment")
    assert r.status_code == 200
    d = r.json()
    assert set(d) >= {"last_assessment", "hours_since", "awaiting_assessment",
                      "collected_since", "state"}
    assert d["state"] in ("ok", "late", "stopped", "unknown")


def test_a_stale_assessment_loop_shows_on_the_board(client, monkeypatch):
    from jobhunt import db

    with db.connect() as con:
        con.execute("UPDATE scores SET scored_at = '2020-01-01T00:00:00+00:00'")
        con.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert "Assessment is stopped" in r.text


def test_a_fresh_loop_does_not_nag(client):
    """The panel must stay quiet while the loop is running, or it becomes noise
    and stops carrying signal."""
    r = client.get("/")
    assert "Assessment is stopped" not in r.text
    assert "Assessment is late" not in r.text
