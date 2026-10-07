"""Pull-based scoring queue.

The container collects and heuristically ranks around the clock. It cannot summon
an assistant session, so it does not try: it publishes a queue instead, and
whoever is in a session pulls the queue, assesses, and posts results back.

Auth: X-API-Token header, or ?token= on the query string. Defaults to APP_PASSWORD.
"""
import json
from datetime import datetime, timezone
import os

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from . import config, db, pipeline, salary, score
from .scoring.llm import rubric_for

router = APIRouter(prefix="/api", tags=["queue"])


def _token() -> str:
    return os.environ.get("API_TOKEN") or os.environ.get("APP_PASSWORD", "")


def _auth(header: str | None, query: str | None) -> None:
    want = _token()
    if not want:
        return  # no password configured -- LAN-only mode
    if (header or query) != want:
        raise HTTPException(401, "bad or missing API token")


VERDICTS = ("strong", "possible", "weak", "no")


class Assessment(BaseModel):
    job_id: str
    fit_score: int = Field(ge=0, le=100)
    verdict: str

    @field_validator("verdict")
    @classmethod
    def _known_verdict(cls, v: str) -> str:
        """Reject anything outside the four verdicts.

        This was a bare str, so the pull queue -- the default scoring path --
        could write arbitrary values straight over a capped heuristic row.
        """
        if v not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}, got {v!r}")
        return v
    headline: str
    rationale: str
    strengths: list[str] = []
    gaps: list[str] = []
    red_flags: list[str] = []


@router.get("/queue")
def queue(limit: int = Query(20, ge=1, le=100),
          min_score: int | None = None,
          include_scored: bool = False,
          x_api_token: str | None = Header(None),
          token: str | None = None):
    """Postings that cleared the heuristic gate and still need a real assessment."""
    _auth(x_api_token, token)
    cfg = config.load()
    floor = min_score if min_score is not None else cfg["scoring"].get(
        "deep_score_threshold", 55)
    # A 'scored' row whose posting has since changed is stale and needs another
    # read. It is no longer overwritten by the heuristic pass, so the queue is
    # the only thing that can refresh it.
    stages = ("('heuristic','scored')" if include_scored
              else "('heuristic')")
    stale = ("" if include_scored else
             " OR (s.stage='scored' AND s.desc_hash != j.desc_hash)")
    # One row per role, not per location. An employer listing one job across
    # forty-three cities produced forty-three queue entries, and each consumed a
    # scarce assessment slot on a posting already read. GROUP BY picks the
    # highest-scoring row of each role as the representative; the rest travel
    # with it as locations and sibling ids.
    sql = f"""SELECT j.id, j.title, j.company, j.location, j.url, j.posted_at,
                     j.best_source, j.sources, j.description, j.role_key,
                     j.salary_min, j.salary_max, j.salary_period, j.salary_source,
                     s.fit_score, s.verdict, s.red_flags, s.rationale,
                     COUNT(*) AS n_listings,
                     GROUP_CONCAT(j.location, ' | ') AS all_locations,
                     GROUP_CONCAT(j.id, ',') AS sibling_ids
              FROM jobs j JOIN scores s ON s.job_id=j.id
              LEFT JOIN actions a ON a.job_id=j.id
              WHERE j.closed=0 AND (s.stage IN {stages}{stale})
                AND s.fit_score >= ?
                AND COALESCE(a.status,'new') != 'dismissed'
                AND j.company_norm NOT IN
                    (SELECT value FROM mutes WHERE kind='company')
                AND NOT EXISTS (SELECT 1 FROM mutes m WHERE m.kind='title'
                                AND j.title_norm LIKE '%' || m.value || '%')
              GROUP BY COALESCE(j.role_key, j.id)
              HAVING s.fit_score = MAX(s.fit_score)
              ORDER BY s.fit_score DESC, j.first_seen DESC LIMIT ?"""
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(sql, (floor, limit)).fetchall()]
    for r in rows:
        r["sources"] = json.loads(r["sources"] or "[]")
        sibs = [x for x in (r.pop("sibling_ids") or "").split(",") if x]
        r["sibling_ids"] = [x for x in sibs if x != r["id"]]
        locs = [x.strip() for x in (r.pop("all_locations") or "").split("|")]
        r["locations"] = sorted({x for x in locs if x})
        if r["n_listings"] > 1:
            r["note"] = (f"One role listed in {r['n_listings']} places. Assess it "
                         f"once; the assessment is applied to all of them.")
        r["pay"] = salary.fmt(r.get("salary_min"), r.get("salary_max"),
                              r.get("salary_period")) or None
        r["heuristic"] = {"fit_score": r.pop("fit_score"), "verdict": r.pop("verdict"),
                          "rationale": r.pop("rationale"),
                          "red_flags": json.loads(r.pop("red_flags") or "[]")}
    return {
        "instance": os.environ.get("INSTANCE_NAME", "jobhunt"),
        "count": len(rows),
        "threshold": floor,
        "resume": config.resume_text(),
        "rubric": rubric_for(cfg),
        "jobs": rows,
    }


class Disposition(BaseModel):
    """One decision about one posting. `job_id` may be an id prefix."""
    job_id: str
    status: str
    reason: str = ""
    note: str | None = None

    @field_validator("status")
    @classmethod
    def _known_status(cls, v: str) -> str:
        if v not in db.STATUSES:
            raise ValueError(f"status must be one of {db.STATUSES}, got {v!r}")
        return v

    @field_validator("reason")
    @classmethod
    def _known_reason(cls, v: str) -> str:
        if v and v not in db.DISMISS_REASONS:
            raise ValueError(f"unknown dismiss reason {v!r}")
        return v


@router.post("/dispositions")
def post_dispositions(dispositions: list[Disposition],
                      apply_to_role: bool = True,
                      x_api_token: str | None = Header(None),
                      token: str | None = None):
    """Record applied / dismissed / shortlisted decisions made outside the board.

    This exists so nothing but the container ever writes the database. The data
    volume is a Docker bind mount, and a second SQLite writer on the host side of
    that mount corrupted the file once already: WAL mode needs shared-memory
    locking that does not survive the host/VM boundary. Callers on the host --
    tools/sync_apply_state.py, anything reconciling the Five Before Noon
    artifact -- come through here instead of opening jobs.db themselves.

    `job_id` may be an id prefix, since the artifact stores only the first ten
    characters. A prefix matching several postings is reported, never guessed.
    """
    _auth(x_api_token, token)
    written, unknown, ambiguous, propagated = 0, [], [], 0
    with db.connect() as con:
        for item in dispositions:
            rows = con.execute(
                "SELECT id, role_key FROM jobs WHERE id LIKE ?",
                (item.job_id + "%",)).fetchall()
            if not rows:
                unknown.append(item.job_id)
                continue
            if len(rows) > 1:
                ambiguous.append({"prefix": item.job_id,
                                  "matches": [r["id"] for r in rows][:5]})
                continue
            row = rows[0]
            targets = [row["id"]]
            if apply_to_role and row["role_key"]:
                targets = [r["id"] for r in con.execute(
                    "SELECT id FROM jobs WHERE role_key=? AND closed=0",
                    (row["role_key"],))] or targets
            for jid in targets:
                # Never trade a specific reason for a generic one. The artifact
                # has one pass button and sends `not_interested`; if the row
                # already says `wrong_level` because the candidate explained
                # herself, that is the better record and it stays.
                reason = item.reason
                if item.status == "dismissed" and reason in ("", "not_interested"):
                    prior = con.execute(
                        "SELECT status, reason FROM actions WHERE job_id=?",
                        (jid,)).fetchone()
                    if (prior and prior["status"] == "dismissed"
                            and prior["reason"]
                            and prior["reason"] != "not_interested"):
                        reason = prior["reason"]
                con.execute(
                    """INSERT INTO actions (job_id,status,reason,note,updated)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
                         reason=excluded.reason,
                         note=COALESCE(excluded.note, actions.note),
                         updated=excluded.updated""",
                    (jid, item.status, reason or None, item.note,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
                written += 1
            propagated += max(0, len(targets) - 1)
    return {"written": written, "unknown_ids": unknown,
            "ambiguous_prefixes": ambiguous,
            "also_applied_to_other_listings": propagated}


@router.post("/scores")
def post_scores(assessments: list[Assessment],
                model: str = "claude-code",
                apply_to_role: bool = True,
                x_api_token: str | None = Header(None),
                token: str | None = None):
    """Write assessments back. Overwrites whatever the heuristic pass wrote.

    By default an assessment lands on every listing of the same role at the same
    employer, not only the row that was read. One job posted across many cities
    is one job, and scoring each copy separately produced three different
    verdicts for a single Circle role.
    """
    _auth(x_api_token, token)
    written, unknown, propagated = 0, [], 0
    with db.connect() as con:
        for a in assessments:
            row = con.execute("SELECT desc_hash, role_key FROM jobs WHERE id=?",
                              (a.job_id,)).fetchone()
            if row is None:
                unknown.append(a.job_id)
                continue
            targets = [(a.job_id, row["desc_hash"])]
            if apply_to_role and row["role_key"]:
                targets = [(r["id"], r["desc_hash"]) for r in con.execute(
                    "SELECT id, desc_hash FROM jobs WHERE role_key=? AND closed=0",
                    (row["role_key"],))] or targets
            for jid, dhash in targets:
                _write_score(con, jid, a, model, dhash)
                written += 1
            propagated += max(0, len(targets) - 1)
    return {"written": written, "unknown_ids": unknown,
            "also_applied_to_other_listings": propagated}


def _write_score(con, job_id: str, a: "Assessment", model: str,
                 desc_hash: str) -> None:
    con.execute(
                """INSERT INTO scores (job_id,stage,fit_score,verdict,headline,rationale,
                     strengths,gaps,red_flags,model,desc_hash,scored_at)
                   VALUES (?,'scored',?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET stage='scored',
                     fit_score=excluded.fit_score, verdict=excluded.verdict,
                     headline=excluded.headline, rationale=excluded.rationale,
                     strengths=excluded.strengths, gaps=excluded.gaps,
                     red_flags=excluded.red_flags, model=excluded.model,
                     desc_hash=excluded.desc_hash, scored_at=excluded.scored_at""",
                (job_id, a.fit_score, a.verdict, a.headline, a.rationale,
                 json.dumps(a.strengths), json.dumps(a.gaps), json.dumps(a.red_flags),
                 model, desc_hash, db.now()))
    con.execute(
        "INSERT OR IGNORE INTO actions (job_id,status,updated) VALUES (?,'new',?)",
        (job_id, db.now()))


class IncomingJob(BaseModel):
    title: str
    company: str
    location: str | None = None
    url: str | None = None
    description: str = ""
    posted_at: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    remote: bool = False
    source: str | None = None


@router.post("/jobs")
def ingest(jobs: list[IncomingJob],
           source: str = "external",
           rescore: bool = True,
           x_api_token: str | None = Header(None),
           token: str | None = None):
    """Accept postings collected elsewhere -- a LinkedIn MCP session, a browser,
    a one-off script. They join the same dedupe, pay-extraction and scoring path
    as the scheduled sources, so nothing on the board is second class."""
    _auth(x_api_token, token)
    payload = []
    for j in jobs:
        d = j.model_dump()
        d["source"] = d.get("source") or source
        payload.append(d)
    result = pipeline.ingest_jobs(payload)
    if rescore:
        result["scoring"] = score.score_pending(limit=max(50, len(payload) * 2))
    return result


@router.get("/stats")
def stats(x_api_token: str | None = Header(None), token: str | None = None):
    _auth(x_api_token, token)
    with db.connect() as con:
        q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "instance": os.environ.get("INSTANCE_NAME", "jobhunt"),
            "jobs": q("SELECT COUNT(*) FROM jobs WHERE closed=0"),
            "heuristic_pending": q(
                "SELECT COUNT(*) FROM scores WHERE stage='heuristic'"),
            "assessed": q("SELECT COUNT(*) FROM scores WHERE stage='scored'"),
            "filtered": q("SELECT COUNT(*) FROM scores WHERE stage='filtered'"),
            "shortlisted": q("SELECT COUNT(*) FROM actions WHERE status='shortlist'"),
        }


@router.get("/health/assessment")
def assessment_health(x_api_token: str | None = Header(None),
                      token: str | None = None):
    """Is anything still assessing this board, and how far behind is it?

    Collection is cron inside this container and looks after itself. Assessment
    cannot be: it needs a session to pull /api/queue, and that session lives on
    a workstation that reboots. When it stops, nothing here fails -- postings
    simply pile up unread while the board looks healthy.

    So staleness is published as a number. The board shows it, and a value that
    keeps climbing is the signal that the loop is gone.
    """
    _auth(x_api_token, token)
    cfg = config.load()
    floor = cfg["scoring"].get("deep_score_threshold", 55)
    with db.connect() as con:
        last = con.execute(
            "SELECT MAX(scored_at) FROM scores WHERE stage='scored'").fetchone()[0]
        waiting = con.execute(
            """SELECT COUNT(*) FROM jobs j JOIN scores s ON s.job_id=j.id
               LEFT JOIN actions a ON a.job_id=j.id
               WHERE j.closed=0 AND s.stage='heuristic' AND s.fit_score >= ?
                 AND COALESCE(a.status,'new') != 'dismissed'""", (floor,)).fetchone()[0]
        since = con.execute(
            """SELECT COUNT(*) FROM jobs WHERE closed=0 AND first_seen >
               COALESCE((SELECT MAX(scored_at) FROM scores WHERE stage='scored'),'')"""
        ).fetchone()[0]
    hours = None
    if last:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        hours = round(delta.total_seconds() / 3600, 1)
    return {
        "last_assessment": last,
        "hours_since": hours,
        "awaiting_assessment": waiting,
        "collected_since": since,
        # A day is two missed passes. Two days means the loop is gone, not late.
        "state": ("unknown" if hours is None else
                  "ok" if hours < 24 else
                  "late" if hours < 48 else "stopped"),
    }
