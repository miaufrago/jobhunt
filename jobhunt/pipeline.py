"""Ingest orchestration: pull every enabled source, merge, then score."""
import concurrent.futures as cf
import json
import os
import pathlib
import time
import threading
import traceback

from . import config, db, salary, score
from .normalize import clean
from .scoring.heuristic import passes_rules
from .scoring.locations import region_of
from .sources import (ats, http, fetch_aggregators, fetch_dice, fetch_employer,
                      fetch_linkedin)


LOCK_STALE_SECONDS = 3 * 3600


def _lock_path():
    return config.DATA_DIR / "run.lock"


def _read_lock() -> dict | None:
    """The lock file's contents, without judging or deleting it."""
    p = _lock_path()
    if not p.exists():
        return None
    try:
        info = json.loads(p.read_text(encoding="utf8"))
    except Exception:
        return None
    return info if isinstance(info, dict) else None


def running() -> dict | None:
    """Whoever holds the run lock, or None. A stale lock is ignored and cleared.

    The UI and the CLI both take this lock, so `Run now` can never start a second
    pass on top of a scheduled or command-line run.

    Three ways a lock is stale: unreadable, older than LOCK_STALE_SECONDS, or
    owned by a process that no longer exists. The last one used to be checked
    only at startup, so a run killed mid-flight blocked every sweep for hours.
    """
    info = _read_lock()
    if info is None:
        _lock_path().unlink(missing_ok=True)
        return None
    if time.time() - info.get("started", 0) > LOCK_STALE_SECONDS:
        _lock_path().unlink(missing_ok=True)
        return None
    if not _token_alive(info.get("token") or ""):
        _lock_path().unlink(missing_ok=True)
        return None
    return info


def _proc_boot_token(pid: int | None = None) -> str:
    """Identity of a process that a later process cannot accidentally inherit.

    A bare PID is not enough. The app runs as PID 1 under `exec` in the
    container, so after a restart the *new* process answers `kill(1, 0)` and the
    dead run's lock looked alive forever -- exactly the case this was written to
    clear. Pairing the PID with the kernel's start-tick for that PID makes the
    token unique across restarts: same number, different process, different token.
    """
    pid = os.getpid() if pid is None else int(pid)
    try:
        # /proc/<pid>/stat field 22 is starttime, in clock ticks since boot. The
        # comm field can contain spaces and parentheses, so split after the ')'.
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf8")
        fields = raw[raw.rindex(")") + 1:].split()
        return f"{pid}:{fields[19]}"
    except Exception:
        # No procfs (Windows, macOS, a dev laptop). Fall back to the PID alone;
        # the staleness timeout is the backstop there.
        return f"{pid}:?"


def _token_alive(token: str) -> bool:
    """True only if the process that wrote `token` is still the one running."""
    if not token:
        return False
    pid_s, _, _stamp = token.partition(":")
    try:
        pid = int(pid_s)
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return _proc_boot_token(pid) == token


def clear_stale_lock() -> bool:
    """Drop a lock left behind by a restart.

    A run only ever executes inside this container, so if the owning process is
    gone the lock is meaningless. Called at startup: a restart kills any run, and
    the surviving lock file would otherwise block every later sweep until it aged
    out. Reads the file directly rather than going through `running()`, which
    deletes what it inspects and so cannot report what it found.
    """
    info = _read_lock()
    if not info:
        return False
    if _token_alive(info.get("token") or ""):
        return False
    _lock_path().unlink(missing_ok=True)
    print(f"cleared stale run lock from {info.get('token')} "
          f"(kind={info.get('kind')})", flush=True)
    return True


def _acquire(kind: str) -> bool:
    """Take the run lock, or return False.

    O_CREAT|O_EXCL so two processes racing here cannot both believe they won.
    The previous check-then-write left a window between the two.
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"kind": kind, "pid": os.getpid(),
                          "token": _proc_boot_token(), "started": time.time()})
    for attempt in (1, 2):
        try:
            fd = os.open(_lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # Someone holds it, or nobody does and the file is debris. running()
            # clears it in the second case; retry once, then give up.
            if attempt == 2 or running():
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf8") as fh:
            fh.write(payload)
        return True
    return False


def _release() -> None:
    _lock_path().unlink(missing_ok=True)


def _ingest(jobs: list[dict], cfg: dict | None = None) -> tuple[int, int]:
    """Clean, price, tag and merge a batch of raw postings.

    cfg is a parameter, not a global. It was read from the enclosing scope, which
    does not exist here -- every ingest raised NameError the moment region
    tagging was added, and because _ingest ran outside the per-source try/except
    that killed the whole run rather than one source.
    """
    cfg = cfg if cfg is not None else config.load()
    new = changed = 0
    with db.connect() as con:
        for j in jobs:
            j["company"] = clean(j.get("company"))
            j["title"] = clean(j.get("title"))
            j["location"] = clean(j.get("location"))
            if not (j["company"] and j["title"]):
                continue
            salary.enrich(j)
            j["region"] = region_of(j.get("location"), cfg)
            _, is_new, desc_changed = db.upsert_job(con, j)
            new += int(is_new)
            changed += int(desc_changed and not is_new)
    return new, changed


def ingest_jobs(jobs: list[dict]) -> dict:
    """Public ingest for postings collected outside the scheduled sources.

    Same path as every built-in source: clean, extract pay, dedupe, merge. A
    posting already on the board is merged, not duplicated, so an external
    collector can overlap the aggregators freely.
    """
    before = 0
    with db.connect() as con:
        before = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    new, updated = _ingest(jobs, config.load())
    return {"received": len(jobs), "new": new, "updated": updated,
            "skipped": len(jobs) - new - updated,
            "total_before": before}


def _spread_by_platform(employers: list[dict]) -> list[dict]:
    """Round-robin the fairness order across platforms.

    Sorting purely by least-recently-collected clusters one platform together:
    F5, Nordstrom, Expedia and Premera are all Workday, sat next to each other,
    and all four timed out in a row. The per-platform limiter paces requests but
    cannot help when every worker in the pool is queued behind the same platform
    -- the pool stalls and Workday sees an unbroken run of connections from one
    address, which is what it refuses rather than rate-limits.

    Interleaving keeps the fairness order within each platform while making
    consecutive tasks hit different back ends.
    """
    buckets: dict[str, list[dict]] = {}
    for e in employers:
        buckets.setdefault(e.get("type") or "other", []).append(e)
    out: list[dict] = []
    while buckets:
        for key in list(buckets):
            out.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return out


def collect(kind: str = "full") -> dict:
    """kind: 'full' (everything) or 'poll' (aggregators + dice only).

    Every source runs under an overall deadline. requests' timeout covers a stalled
    connect or read, but not a server that trickles bytes forever -- without a
    deadline one slow employer wedges the whole run.
    """
    cfg = config.load(force=True)
    terms = cfg["search_terms"]
    # Adapters spend their limited description fetches on titles that match.
    ats.set_relevance(lambda title: passes_rules(
        {"title": title, "location": "", "remote": True}, cfg)[0])
    stats = {"kind": kind, "fetched": 0, "new": 0, "updated": 0, "sources_ok": 0,
             "sources_failed": 0, "failures": []}
    tasks: list[tuple[str, callable]] = []

    if cfg["sources"]["aggregators"].get("enabled"):
        tasks.append(("aggregators", lambda: fetch_aggregators(terms, cfg)))
    if cfg["sources"]["dice"].get("enabled"):
        tasks.append(("dice", lambda: fetch_dice(terms, cfg)))
    if (cfg["sources"].get("linkedin_mcp") or {}).get("enabled"):
        tasks.append(("linkedin", lambda: fetch_linkedin(terms, cfg)))
    if kind == "full" and cfg["sources"]["employers"].get("enabled"):
        # Least-recently-collected first. Config order starves whatever sits at
        # the end of the list: a budget that runs out always runs out on the same
        # employers, so they are never collected at all. Rotating by last success
        # means a short run costs everyone a little rather than costing the tail
        # everything.
        with db.connect() as con:
            seen_at = {r["source"]: r["checked"] for r in
                       con.execute("SELECT source, checked FROM source_health "
                                   "WHERE ok=1")}
        employers = sorted(cfg.get("employers", []),
                           key=lambda e: seen_at.get(e["name"], ""))
        for e in _spread_by_platform(employers):
            tasks.append((e["name"], (lambda e=e: fetch_employer(e, terms))))

    # Politeness stretches every request, so the budget has to stretch with it or
    # a slowed-down instance simply never finishes.
    scale = float(os.environ.get("POLITENESS_SCALE", "1.0"))
    budget = cfg["sources"].get("run_budget_seconds", 1800) * max(1.0, scale)
    deadline = time.time() + budget

    # Two phases, not one pool, and the split is by how a source is read rather
    # than by who publishes it.
    #
    #   employers -- an employer's own ATS feed: one or two requests returning a
    #                whole job list. Cheap, and the employer is authoritative.
    #   searches  -- keyword searches across aggregators, Dice and LinkedIn.
    #                Expensive: JobSpy fetches every LinkedIn description
    #                separately, hundreds of connections over several minutes.
    #
    # They are separated because searches starve the employer threads when the
    # two share a pool. Employers finish in about a minute on their own, so the
    # phasing costs no wall clock and removes the contention that was timing
    # them out. Searches are also the only way to reach the third of large
    # employers -- Apple, Meta, Costco -- that publish no feed at all.
    SEARCH_SOURCES = ("aggregators", "dice", "linkedin")
    employer_tasks = [t for t in tasks if t[0] not in SEARCH_SOURCES]
    search_tasks = [t for t in tasks if t[0] in SEARCH_SOURCES]

    # Each phase gets its own guaranteed share. Deriving the allowance from the
    # shared deadline let the employers phase consume the whole budget -- a
    # single wedged employer blocked `as_completed` to the end, and the second
    # phase then ran with the 30-second floor and marked every source failed.
    # Unused time flows forward, so a fast first phase still lends to the second.
    shares = {"employers": float(cfg["sources"].get("employer_budget_share", 0.55)),
              "searches": 1.0}
    started = time.time()
    spent = 0.0
    stop = http.STOP
    stop.clear()

    for phase_name, phase_tasks, workers in (
        ("employers", employer_tasks, 6),
        ("searches", search_tasks, 3),
    ):
        if not phase_tasks:
            continue
        cap = budget * shares[phase_name] - spent
        remaining = max(30.0, min(cap, deadline - time.time()))
        print(f"--- phase: {phase_name} ({len(phase_tasks)} sources, "
              f"{remaining:.0f}s of {budget:.0f}s budget) ---", flush=True)
        stop.clear()
        phase_started = time.time()
        _run_phase(phase_tasks, workers, remaining, stats, cfg, stop)
        spent = time.time() - started
        # Tell any worker still inside a fetch to give up, so phase one is
        # not still hitting employer boards while phase two runs.
        stop.set()
        print(f"--- phase {phase_name} took {time.time() - phase_started:.0f}s ---",
              flush=True)

    stats["elapsed_seconds"] = round(time.time() - started)
    return stats


def _run_phase(tasks: list, workers: int, budget: float, stats: dict,
               cfg: dict, stop: 'threading.Event' = None) -> None:
    pool = cf.ThreadPoolExecutor(max_workers=workers)
    futures = {pool.submit(fn): name for name, fn in tasks}
    done = 0
    try:
        for fut in cf.as_completed(futures, timeout=budget):
            name = futures[fut]
            done += 1
            try:
                jobs = fut.result()
                n, u = _ingest(jobs, cfg)
            except Exception as exc:
                stats["sources_failed"] += 1
                detail = f"{type(exc).__name__}: {exc}"[:300]
                stats["failures"].append(f"{name}: {detail}")
                db.set_health(name, False, detail, 0)
                print(f"[{done}/{len(futures)}] {name}: FAILED {detail}", flush=True)
                continue
            stats["fetched"] += len(jobs)
            stats["new"] += n
            stats["updated"] += u
            stats["sources_ok"] += 1
            db.set_health(name, True, f"{len(jobs)} fetched, {n} new", len(jobs))
            print(f"[{done}/{len(futures)}] {name}: {len(jobs)} fetched, {n} new",
                  flush=True)
    except cf.TimeoutError:
        for fut, name in futures.items():
            if fut.done():
                continue
            stats["sources_failed"] += 1
            detail = f"no response within the {budget:.0f}s phase budget"
            stats["failures"].append(f"{name}: {detail}")
            db.set_health(name, False, detail, 0)
            print(f"TIMED OUT: {name}", flush=True)
    finally:
        # cancel_futures only drops work still queued. Signalling first gives a
        # worker already inside a fetch a chance to stop between requests.
        if stop is not None:
            stop.set()
        # Do not wait: a wedged worker would block shutdown for as long as it hangs.
        pool.shutdown(wait=False, cancel_futures=True)


def run(kind: str = "full", do_score: bool = True, score_limit: int = 10000) -> dict:
    started = db.now()
    db.init()
    if not _acquire(kind):
        held = running() or {}
        return {"skipped": f"a '{held.get('kind', '?')}' run is already in progress"}
    try:
        # A copy before anything writes. Cheap, and the assessments are the one
        # thing in here that cannot be collected again.
        snap = None
        try:
            snap = db.snapshot("prerun")
        except Exception as exc:
            snap = f"failed: {type(exc).__name__}: {exc}"
        stats = collect(kind)
        stats["snapshot"] = snap
        if do_score:
            stats["scoring"] = score.score_pending(limit=score_limit)
        # Postings an employer feed has stopped listing are filled or pulled.
        if kind == "full":
            cfg = config.load()
            stats["closed"] = db.close_stale(
                int(cfg["sources"].get("close_after_days", 21)))
        db.record_run(kind, started, stats)
        return stats
    except Exception:
        db.record_run(kind, started, {}, traceback.format_exc()[-2000:])
        raise
    finally:
        _release()
