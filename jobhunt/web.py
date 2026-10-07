"""FastAPI front end. Single container, SQLite on a mounted volume, port 8080.

Auth is a single shared password (APP_PASSWORD) held in a signed cookie. Set it --
the app is meant to sit behind Nginx Proxy Manager on a public hostname.
"""
import json
import os
import secrets
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from . import api, config, db, pipeline, salary, score, verify

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
TZ = os.environ.get("TZ", "America/Los_Angeles")
# Distinguishes instances when more than one runs on the same host.
INSTANCE = os.environ.get("INSTANCE_NAME", "jobhunt")

signer = URLSafeSerializer(SECRET, salt="jobhunt-session")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.globals["instance"] = INSTANCE
scheduler = BackgroundScheduler(timezone=TZ)

STATE = {"running": False, "last": None}


def _is_running() -> bool:
    return STATE["running"] or bool(pipeline.running())


def _run_job(kind: str) -> None:
    """pipeline.run() holds the cross-process lock; STATE is just for the button."""
    STATE["running"] = True
    try:
        STATE["last"] = pipeline.run(kind)
    except Exception as exc:
        STATE["last"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        STATE["running"] = False


# A laptop sleeps; Docker suspends with it. APScheduler's default misfire grace
# is one second, so a cron that fires while the machine is asleep is discarded
# outright rather than run on wake -- silently, with only a "was missed by
# 0:25:13" line in the log. Two consecutive daily sweeps were lost that way.
#
# A long grace lets a missed run start once the machine is back. `coalesce`
# collapses a backlog into a single run, so a weekend asleep produces one sweep
# rather than three stacked ones. `max_instances=1` is belt and braces: the
# pipeline already holds a cross-process lock, and a second run would be refused.
#
# The grace has to exceed how long the machine is actually asleep, and six hours
# did not: runs were still being dropped after being missed by 10, 11 and 17
# hours -- an overnight and a weekend. Each job now gets a grace just under its
# own interval, which is the longest value that cannot let one firing bleed into
# the next. A daily sweep tolerates a machine closed until the following noon.
FULL_SWEEP_GRACE_SECONDS = 23 * 3600   # daily cron, 24h apart
POLL_GRACE_SECONDS = 5 * 3600          # 9/15/18 cron, shortest gap 3h


def _schedule() -> None:
    cfg = config.load()
    scheduler.remove_all_jobs()
    sch = cfg.get("schedule", {})
    common = dict(coalesce=True, max_instances=1, replace_existing=True)
    if sch.get("full_sweep_cron"):
        scheduler.add_job(_run_job, CronTrigger.from_crontab(sch["full_sweep_cron"], timezone=TZ),
                          args=["full"], id="full",
                          misfire_grace_time=FULL_SWEEP_GRACE_SECONDS, **common)
    if sch.get("aggregator_poll_cron"):
        scheduler.add_job(_run_job, CronTrigger.from_crontab(sch["aggregator_poll_cron"], timezone=TZ),
                          args=["poll"], id="poll",
                          misfire_grace_time=POLL_GRACE_SECONDS, **common)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if pipeline.clear_stale_lock():
        print("cleared a run lock left by a previous process", flush=True)
    _schedule()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title=INSTANCE, lifespan=lifespan)
app.include_router(api.router)


# ------------------------------------------------------------------ auth

def require_auth(request: Request):
    if not APP_PASSWORD:
        return True  # no password configured -- LAN-only mode
    token = request.cookies.get("jobhunt")
    if not token:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    try:
        if signer.loads(token) != "ok":
            raise BadSignature("bad")
    except BadSignature:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return True


@app.exception_handler(HTTPException)
async def redirect_handler(request: Request, exc: HTTPException):
    if exc.status_code == 307 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=307)
    return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>",
                        status_code=exc.status_code)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if not secrets.compare_digest(password, APP_PASSWORD):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Wrong password."}, status_code=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("jobhunt", signer.dumps("ok"), httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 30)
    return resp


def _region_tabs(cfg: dict) -> tuple[list[dict], dict]:
    """Region tabs and their notes.

    Written once. This block was pasted into both the board and the job page,
    and the copy on the job page referenced a `cfg` that was never loaded there,
    so every job detail page returned a 500. One caller, one definition.
    """
    floor = (cfg.get("profile") or {}).get("min_fit_to_show", 0)
    with db.connect() as con:
        regions = [dict(r) for r in con.execute(
            """SELECT COALESCE(j.region,'primary') AS region, COUNT(*) AS n
               FROM jobs j LEFT JOIN scores s ON s.job_id=j.id
               LEFT JOIN actions a ON a.job_id=j.id
               WHERE j.closed=0 AND COALESCE(a.status,'new') != 'dismissed'
                 AND COALESCE(s.fit_score,0) >= ?
               GROUP BY 1 ORDER BY n DESC""", (floor,)).fetchall()]
    notes = {k: (v.get("note") if isinstance(v, dict) else "")
             for k, v in ((cfg.get("profile") or {}).get("secondary_regions") or {}).items()}
    return regions, notes


# ------------------------------------------------------------------ board

def _rows(filters: dict) -> list[dict]:
    where = ["j.closed=0"]
    params: list = []
    if filters["status"] == "all":
        # "all" means everything still live -- dismissed postings are gone on
        # purpose; pick the dismissed filter explicitly to review them.
        where.append("COALESCE(a.status,'new') != 'dismissed'")
    elif filters["status"] != "any":
        where.append("COALESCE(a.status,'new') = ?")
        params.append(filters["status"])
    # A muted company hides every posting it files, past and future.
    where.append("j.company_norm NOT IN (SELECT value FROM mutes WHERE kind='company')")
    where.append("COALESCE(s.fit_score,0) >= ?")
    params.append(filters["min_score"])
    if filters["q"]:
        where.append("(j.title LIKE ? OR j.company LIKE ?)")
        params += [f"%{filters['q']}%"] * 2
    if filters.get("region") == "all":
        pass
    elif filters.get("region"):
        where.append("COALESCE(j.region,'primary') = ?")
        params.append(filters["region"])
    if filters.get("scored") == "assessed":
        where.append("s.stage='scored'")
    elif filters.get("scored") == "pending":
        where.append("COALESCE(s.stage,'') != 'scored'")
    if filters.get("min_pay"):
        where.append("j.salary_max >= ?")
        params.append(filters["min_pay"] * 1000)
    if filters["days"]:
        where.append("j.first_seen >= datetime('now', ?)")
        params.append(f"-{int(filters['days'])} days")
    # A real assessment outranks a keyword guess at any score, so assessed rows
    # sort first by default. Without this the heuristic's inflated scores bury
    # the postings something actually read against the resume.
    sort = filters.get("sort") or "assessed"
    if sort == "newest":
        order = "j.first_seen DESC, COALESCE(s.fit_score,-1) DESC"
    elif sort == "score":
        order = "COALESCE(s.fit_score,-1) DESC, j.first_seen DESC"
    else:
        order = ("CASE WHEN s.stage='scored' THEN 0 ELSE 1 END, "
                 "COALESCE(s.fit_score,-1) DESC, j.first_seen DESC")
    # One card per role, not per city. Databricks lists a single job in
    # forty-three locations; without grouping those filled the board and buried
    # everything else. The rows stay separate underneath -- this only collapses
    # the view, so a dismissal or an assessment still targets a real posting.
    sql = f"""SELECT j.*, s.fit_score, s.verdict, s.headline, s.stage, s.rationale,
                     s.strengths, s.gaps, s.red_flags, s.model, s.scored_at,
                     COALESCE(a.status,'new') AS status, a.reason, a.note,
                     CAST(julianday('now') - julianday(j.first_seen) AS INTEGER) AS age_days,
                     COUNT(*) AS n_listings,
                     GROUP_CONCAT(j.location, ' | ') AS all_locations
              FROM jobs j
              LEFT JOIN scores s ON s.job_id=j.id
              LEFT JOIN actions a ON a.job_id=j.id
              WHERE {' AND '.join(where)}
              GROUP BY COALESCE(j.role_key, j.id)
              HAVING COALESCE(s.fit_score,-1) = MAX(COALESCE(s.fit_score,-1))
              ORDER BY {order}
              LIMIT 400"""
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    for r in rows:
        locs = [x.strip() for x in (r.pop("all_locations") or "").split("|")]
        r["locations"] = sorted({x for x in locs if x})
        if r["n_listings"] > 1 and len(r["locations"]) > 1:
            r["location"] = f"{r['locations'][0]} +{len(r['locations']) - 1} more"
    return rows


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _=Depends(require_auth), min_score: int = 0,
          status: str = "all", q: str = "", days: int = 0, min_pay: int = 0,
          sort: str = "assessed", scored: str = "all",
          region: str = "primary"):
    cfg = config.load()
    filters = {"min_score": min_score or cfg["profile"].get("min_fit_to_show", 0),
               "status": status, "q": q, "days": days, "min_pay": min_pay,
               "sort": sort, "scored": scored, "region": region}
    rows = _rows(filters)
    for r in rows:
        r["sources_list"] = json.loads(r["sources"] or "[]")
        r["pay"] = salary.fmt(r.get("salary_min"), r.get("salary_max"),
                              r.get("salary_period"))
    with db.connect() as con:
        counts = dict(con.execute(
            """SELECT COALESCE(a.status,'new') st, COUNT(*) n FROM jobs j
               LEFT JOIN actions a ON a.job_id=j.id WHERE j.closed=0 GROUP BY st"""
        ).fetchall())
        last_run = con.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    regions, region_notes = _region_tabs(cfg)
    from .api import assessment_health
    try:
        assessment = assessment_health(x_api_token=None, token=None)
    except Exception:
        assessment = None
    return templates.TemplateResponse(request, "index.html", {
        "assessment": assessment,
        "rows": rows, "filters": filters, "counts": counts,
        "reasons": db.DISMISS_REASONS, "statuses": db.STATUSES,
        "regions": regions,
        "region_notes": region_notes,
        "last_run": dict(last_run) if last_run else None,
        "running": _is_running(),
        "backend": score.backend_status(cfg),
        "backlog": dict(con.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN j.first_seen >= COALESCE(
                            (SELECT MAX(scored_at) FROM scores WHERE stage='scored'),
                            '1970-01-01') THEN 1 ELSE 0 END) AS since_last
               FROM jobs j JOIN scores s ON s.job_id=j.id
               LEFT JOIN actions a ON a.job_id=j.id
               WHERE j.closed=0 AND s.stage='heuristic' AND s.fit_score >= ?
                 AND COALESCE(a.status,'new') != 'dismissed'""",
            (cfg["scoring"].get("deep_score_threshold", 55),)).fetchone() or {}),
        "last_assessed": (con.execute(
            "SELECT MAX(scored_at) FROM scores WHERE stage='scored'").fetchone()[0]),
        "queue_n": con.execute(
            "SELECT COUNT(*) FROM scores WHERE stage='heuristic' AND fit_score>=?",
            (cfg["scoring"].get("deep_score_threshold", 55),)).fetchone()[0],
    })


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str, _=Depends(require_auth)):
    with db.connect() as con:
        r = con.execute(
            """SELECT j.*, s.fit_score, s.verdict, s.headline, s.rationale, s.stage,
                      s.strengths, s.gaps, s.red_flags, s.model, s.scored_at,
                      COALESCE(a.status,'new') AS status, a.note, a.reason
               FROM jobs j LEFT JOIN scores s ON s.job_id=j.id
               LEFT JOIN actions a ON a.job_id=j.id WHERE j.id=?""", (job_id,)).fetchone()
    if not r:
        raise HTTPException(404, "job not found")
    job = dict(r)
    for k in ("strengths", "gaps", "red_flags"):
        job[k] = json.loads(job.get(k) or "[]")
    job["sources_list"] = json.loads(job["sources"] or "[]")
    job["pay"] = salary.fmt(job.get("salary_min"), job.get("salary_max"),
                            job.get("salary_period"))
    # Every other listing of this same role, so the page can say where else it
    # is posted rather than pretending each city is a separate opening.
    if job.get("role_key"):
        with db.connect() as con:
            job["other_listings"] = [dict(x) for x in con.execute(
                """SELECT id, location, url FROM jobs
                   WHERE role_key=? AND id != ? AND closed=0
                   ORDER BY location""", (job["role_key"], job["id"])).fetchall()]
    else:
        job["other_listings"] = []
    regions, region_notes = _region_tabs(config.load())
    return templates.TemplateResponse(request, "job.html", {
        "job": job, "running": _is_running(),
        "reasons": db.DISMISS_REASONS, "statuses": db.STATUSES,
        "regions": regions,
        "region_notes": region_notes,
    })


@app.post("/job/{job_id}/status")
def set_status(request: Request, job_id: str, status: str = Form(""),
               dismiss: str = Form(""), reason: str = Form(""),
               note: str | None = Form(None),
               back: str = Form("/"), _=Depends(require_auth)):
    """One route for every disposition, from the board or the job page."""
    if dismiss:                              # board's dismiss-with-reason select
        status, reason = "dismissed", dismiss
    elif status.startswith("dismiss:"):      # older combined form, still accepted
        status, reason = "dismissed", status.split(":", 1)[1]
    if status:                               # empty means nothing was chosen
        db.set_disposition(job_id, status, reason, note)
    return RedirectResponse(back or "/", status_code=303)


@app.post("/job/{job_id}/mute-company")
def mute_company(job_id: str, back: str = Form("/"), _=Depends(require_auth)):
    with db.connect() as con:
        row = con.execute("SELECT company, company_norm FROM jobs WHERE id=?",
                          (job_id,)).fetchone()
    if row:
        db.add_mute("company", row["company_norm"], row["company"],
                    "muted from the board")
        db.set_disposition(job_id, "dismissed", "company")
    return RedirectResponse(back or "/", status_code=303)


@app.post("/mutes/{mute_id}/delete")
def unmute(mute_id: int, _=Depends(require_auth)):
    db.remove_mute(mute_id)
    return RedirectResponse("/mutes", status_code=303)


@app.get("/mutes", response_class=HTMLResponse)
def mutes(request: Request, _=Depends(require_auth)):
    """Muted companies, and what the dismissals say about the search."""
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM mutes ORDER BY kind, label").fetchall()]
        breakdown = [dict(r) for r in con.execute(
            """SELECT COALESCE(NULLIF(reason,''),'unspecified') AS reason,
                      COUNT(*) AS n FROM actions
               WHERE status='dismissed' GROUP BY reason ORDER BY n DESC""").fetchall()]
        recent = [dict(r) for r in con.execute(
            """SELECT j.id, j.title, j.company, a.reason, a.note, a.updated
               FROM actions a JOIN jobs j ON j.id=a.job_id
               WHERE a.status='dismissed' ORDER BY a.updated DESC LIMIT 25""").fetchall()]
    return templates.TemplateResponse(request, "mutes.html", {
        "mutes": rows, "breakdown": breakdown, "recent": recent,
        "reasons": db.DISMISS_REASONS, "running": _is_running(),
    })


# ------------------------------------------------------------------ ops

@app.post("/run")
def run_now(background: BackgroundTasks, kind: str = Form("full"), _=Depends(require_auth)):
    background.add_task(_run_job, kind)
    return RedirectResponse("/", status_code=303)


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request, _=Depends(require_auth)):
    with db.connect() as con:
        health = [dict(r) for r in con.execute(
            "SELECT * FROM source_health ORDER BY ok ASC, source ASC").fetchall()]
        runs = [dict(r) for r in con.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 15").fetchall()]
    return templates.TemplateResponse(request, "sources.html",
                                      {"health": health, "runs": runs,
                                       "running": _is_running()})


@app.post("/sources/verify")
def sources_verify(background: BackgroundTasks, _=Depends(require_auth)):
    background.add_task(verify.run)
    return RedirectResponse("/sources", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, _=Depends(require_auth), saved: int = 0, error: str = ""):
    return templates.TemplateResponse(request, "settings.html", {
        "yaml": config.CONFIG_PATH.read_text(encoding="utf8"),
        "resume": config.resume_text(),
        "saved": saved, "error": error, "running": _is_running(),
    })


@app.post("/settings")
def save_settings(config_yaml: str = Form(...), resume: str = Form(...),
                  _=Depends(require_auth)):
    try:
        config.save(config_yaml)
    except Exception as exc:
        return RedirectResponse(f"/settings?error={type(exc).__name__}: {exc}", status_code=303)
    cfg = config.load()
    path = config.ROOT / cfg["profile"]["resume_path"]
    path.write_text(resume, encoding="utf8")
    _schedule()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "instance": INSTANCE, "running": _is_running()}
