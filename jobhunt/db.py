"""SQLite store. One row per deduped job; scores kept separately so a rescore
never loses the posting history."""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config
from .normalize import SOURCE_RANK, dedupe_key, is_remote, norm_company, norm_location, norm_title

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,
  company       TEXT NOT NULL,
  company_norm  TEXT NOT NULL,
  title         TEXT NOT NULL,
  title_norm    TEXT NOT NULL,
  location      TEXT,
  location_norm TEXT,
  remote        INTEGER DEFAULT 0,
  url           TEXT,
  best_source   TEXT,
  sources       TEXT DEFAULT '[]',
  description   TEXT,
  desc_hash     TEXT,
  salary_min    REAL,
  salary_max    REAL,
  salary_period TEXT,
  salary_source TEXT,
  region        TEXT DEFAULT 'primary',
  posted_at     TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  closed        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_seen ON jobs(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_norm);

CREATE TABLE IF NOT EXISTS scores (
  job_id     TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  stage      TEXT,                 -- 'filtered' | 'triaged_out' | 'scored'
  fit_score  INTEGER,
  verdict    TEXT,
  headline   TEXT,
  rationale  TEXT,
  strengths  TEXT DEFAULT '[]',
  gaps       TEXT DEFAULT '[]',
  red_flags  TEXT DEFAULT '[]',
  model      TEXT,
  desc_hash  TEXT,
  scored_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_scores_fit ON scores(fit_score DESC);

CREATE TABLE IF NOT EXISTS actions (
  job_id  TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  status  TEXT,                    -- 'new'|'shortlist'|'applied'|'interviewing'|'dismissed'
  reason  TEXT,                    -- why it was dismissed; see DISMISS_REASONS
  note    TEXT,
  updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);

-- Companies (or title patterns) to stop showing entirely. Dismissing one posting
-- hides one posting; muting the company hides every posting it ever files.
CREATE TABLE IF NOT EXISTS mutes (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  kind    TEXT NOT NULL,           -- 'company' | 'title'
  value   TEXT NOT NULL,           -- normalized company, or a regex for 'title'
  label   TEXT,                    -- what to show in the UI
  note    TEXT,
  created TEXT,
  UNIQUE(kind, value)
);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT,
  started_at  TEXT,
  finished_at TEXT,
  stats       TEXT DEFAULT '{}',
  error       TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
  source   TEXT PRIMARY KEY,
  ok       INTEGER,
  detail   TEXT,
  n_jobs   INTEGER,
  checked  TEXT
);
"""


def text_hash(s: str) -> str:
    """Stable content hash.

    str(hash()) was used here and is randomized per process, so a byte-identical
    description hashed to a different value in every new process. That made
    score_pending believe every posting had changed, and it overwrote real
    assessments with the keyword score on the next sweep.
    """
    return hashlib.sha1((s or "").strip().encode("utf8")).hexdigest()


def role_key(company: str, title: str) -> str:
    """One employer's one role, however many cities it is listed in.

    The dedupe key includes location because two postings in two cities really
    are two postings. But an employer that lists one role across forty-three
    cities produces forty-three rows for one job, and the board then spends a
    scarce assessment slot on each. This groups them without merging them: the
    rows stay, and the board and the queue treat them as one.
    """
    return hashlib.sha1(
        f"{norm_company(company)}|{norm_title(title)}".encode("utf8")
    ).hexdigest()[:20]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


MIGRATIONS = [
    ("jobs", "salary_period", "TEXT"),
    ("jobs", "salary_source", "TEXT"),
    ("actions", "reason", "TEXT"),
    ("jobs", "region", "TEXT DEFAULT 'primary'"),
    ("scores", "heuristic_score", "INTEGER"),
    ("scores", "heuristic_at", "TEXT"),
    ("jobs", "role_key", "TEXT"),
]

# Why a posting was set aside. Kept as data, not free text, so the board can
# report what is actually filling the queue -- ten dismissals for "pay too low"
# is a signal about the search, not ten separate judgements.
DISMISS_REASONS = {
    "not_interested": "Role does not interest me",
    "pay_too_low": "Pay too low",
    "wrong_level": "Wrong level",
    "wrong_domain": "Wrong domain or function",
    "not_a_fit": "Not a good fit for my background",
    "company": "Not interested in this company",
    "location": "Location or on-site requirement",
    "contract": "Contract or staffing listing",
    "stale": "Posting is stale or filled",
    "applied_elsewhere": "Already applied or in progress",
}

STATUSES = ("new", "shortlist", "applied", "interviewing", "dismissed")


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def upsert_job(con: sqlite3.Connection, job: dict) -> tuple[str, bool, bool]:
    """Insert or merge one raw posting. Returns (job_id, is_new, desc_changed)."""
    company, title, loc = job.get("company") or "", job.get("title") or "", job.get("location") or ""
    jid = dedupe_key(company, title, loc)
    src = job.get("source") or "unknown"
    desc = job.get("description") or ""
    dhash = text_hash(desc)
    ts = now()

    row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if row is None:
        con.execute(
            """INSERT INTO jobs (id,company,company_norm,title,title_norm,location,location_norm,
               remote,url,best_source,sources,description,desc_hash,salary_min,salary_max,
               salary_period,salary_source,region,role_key,posted_at,first_seen,last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (jid, company, norm_company(company), title, norm_title(title), loc, norm_location(loc),
             int(is_remote(loc) or bool(job.get("remote"))), job.get("url"), src, json.dumps([src]),
             desc, dhash, job.get("salary_min"), job.get("salary_max"),
             job.get("salary_period"), job.get("salary_source"),
             job.get("region") or "primary", role_key(company, title),
             job.get("posted_at"), ts, ts),
        )
        return jid, True, True

    sources = set(json.loads(row["sources"]))
    sources.add(src)
    old_rank = SOURCE_RANK.get(row["best_source"] or "", 0)
    new_rank = SOURCE_RANK.get(src, 0)
    outranks = new_rank > old_rank

    # The employer's own board is authoritative for everything it reports, so a
    # higher-ranked source replaces the display fields wholesale. Previously
    # only url and description moved, which left the first-seen title and
    # location in place forever and produced rows mixing one posting's title
    # with another's salary.
    best = src if outranks else row["best_source"]
    url = (job.get("url") or row["url"]) if outranks else (row["url"] or job.get("url"))
    keep_title = title if (outranks and title) else row["title"]
    keep_company = company if (outranks and company) else row["company"]
    keep_loc = loc if (outranks and loc) else row["location"]
    keep_remote = int(is_remote(loc) or bool(job.get("remote"))) if outranks else row["remote"]

    # Longest-wins is gone: a longer description is usually more boilerplate,
    # not more content, and it made a bad description permanent because no
    # later fetch from the same source could ever replace it.
    keep_desc = row["description"] or ""
    if outranks and desc:
        keep_desc = desc
    elif src == row["best_source"] and desc:
        keep_desc = desc                      # same source re-filing: trust it
    elif not keep_desc:
        keep_desc = desc

    # Only take a pay pair as a pair. Mixing a new minimum with an old maximum
    # produced inverted ranges such as min=260000, max=200000.
    take_pay = outranks or row["salary_min"] is None
    pay = ((job.get("salary_min"), job.get("salary_max"),
            job.get("salary_period"), job.get("salary_source"))
           if take_pay and job.get("salary_max") is not None
           else (None, None, None, None))

    changed = text_hash(keep_desc) != row["desc_hash"]
    con.execute(
        """UPDATE jobs SET sources=?, best_source=?, url=?, title=?, title_norm=?,
           company=?, company_norm=?, location=?, location_norm=?, remote=?,
           description=?, desc_hash=?,
           salary_min=COALESCE(?,salary_min), salary_max=COALESCE(?,salary_max),
           salary_period=COALESCE(?,salary_period), salary_source=COALESCE(?,salary_source),
           region=COALESCE(?,region), role_key=?,
           posted_at=COALESCE(posted_at,?), last_seen=?, closed=0 WHERE id=?""",
        (json.dumps(sorted(sources)), best, url,
         keep_title, norm_title(keep_title), keep_company, norm_company(keep_company),
         keep_loc, norm_location(keep_loc), keep_remote,
         keep_desc, text_hash(keep_desc), *pay, job.get("region"),
         role_key(keep_company, keep_title),
         job.get("posted_at"), ts, jid),
    )
    return jid, False, changed


def record_run(kind: str, started: str, stats: dict, error: str | None = None) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO runs (kind,started_at,finished_at,stats,error) VALUES (?,?,?,?,?)",
            (kind, started, now(), json.dumps(stats), error),
        )


def set_disposition(job_id: str, status: str, reason: str = "",
                   note: str | None = None) -> None:
    """Record what the candidate decided about one posting.

    An unknown status is dropped rather than written: a form that submitted an
    empty status once wrote it straight through, which is how two applications
    ended up recorded as dismissals instead.
    """
    if status not in STATUSES:
        return
    with connect() as con:
        if note is None:
            con.execute(
                """INSERT INTO actions (job_id,status,reason,updated) VALUES (?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
                     reason=excluded.reason, updated=excluded.updated""",
                (job_id, status, reason, now()))
        else:
            con.execute(
                """INSERT INTO actions (job_id,status,reason,note,updated)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
                     reason=excluded.reason, note=excluded.note,
                     updated=excluded.updated""",
                (job_id, status, reason, note, now()))


def add_mute(kind: str, value: str, label: str = "", note: str = "") -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO mutes (kind,value,label,note,created) VALUES (?,?,?,?,?)
               ON CONFLICT(kind,value) DO UPDATE SET label=excluded.label,
                 note=excluded.note""",
            (kind, value, label or value, note, now()))


def remove_mute(mute_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM mutes WHERE id=?", (mute_id,))


def muted_companies() -> set[str]:
    with connect() as con:
        return {r["value"] for r in
                con.execute("SELECT value FROM mutes WHERE kind='company'")}


def set_health(source: str, ok: bool, detail: str, n: int = 0) -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO source_health (source,ok,detail,n_jobs,checked) VALUES (?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET ok=excluded.ok, detail=excluded.detail,
               n_jobs=excluded.n_jobs, checked=excluded.checked""",
            (source, int(ok), detail[:400], n, now()),
        )


def snapshot(tag: str = "auto", keep: int = 14) -> str:
    """Consistent copy of the database, using SQLite's own backup API.

    Assessments are the only data here that cannot be re-collected. A copy of
    the file is not safe to take while WAL writes are in flight, so this uses
    the backup API rather than shutil.copy. Kept because a `score --force`
    once destroyed 194 assessments and only a stray scratch file recovered them.
    """
    out_dir = config.DATA_DIR / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = out_dir / f"jobhunt-{stamp}-{tag}.sqlite"
    src = sqlite3.connect(config.db_path(), timeout=30)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # Rotate. Newest `keep` survive; the rest go.
    existing = sorted(out_dir.glob("jobhunt-*.sqlite"))
    for old in existing[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return str(dest)


def export_assessments() -> list[dict]:
    """Every model-written assessment, in the shape /api/scores accepts.

    A plain-JSON escape hatch: it restores judgement into a rebuilt database
    without needing the old SQLite file to be readable.
    """
    with connect() as con:
        rows = con.execute(
            """SELECT job_id, fit_score, verdict, headline, rationale,
                      strengths, gaps, red_flags, model, scored_at
               FROM scores WHERE stage='scored' ORDER BY scored_at""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("strengths", "gaps", "red_flags"):
            d[k] = json.loads(d[k] or "[]")
        out.append(d)
    return out


def close_stale(days: int = 21) -> int:
    """Mark postings that have stopped appearing in their source as closed.

    `closed` was written by the schema and never set by anything, so filled
    roles ranked alongside live ones forever. A posting that an employer feed
    has stopped listing is filled or pulled; that is the whole signal.

    Anything the candidate has acted on is left alone -- an application in
    flight must not vanish from the board because the listing came down.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds")
    with connect() as con:
        cur = con.execute(
            """UPDATE jobs SET closed=1
               WHERE closed=0 AND last_seen < ?
                 AND id NOT IN (SELECT job_id FROM actions
                                WHERE status IN ('applied','interviewing','shortlist'))""",
            (cutoff,))
        return cur.rowcount
