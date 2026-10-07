"""Probe every configured source and report which ones actually answer.

Employer ATS slugs and Workday tenants drift. Run this before trusting the list.
"""
import concurrent.futures as cf

from . import config, db
from .sources import ats
from .sources.dice import DiceMCP


def probe_employer(e: dict) -> dict:
    name = e.get("name", "?")
    try:
        jobs = ats.fetch_employer(e, ["security"])
        return {"name": name, "type": e.get("type"), "ok": True,
                "n": len(jobs), "detail": f"{len(jobs)} postings for 'security'"}
    except Exception as exc:
        return {"name": name, "type": e.get("type"), "ok": False, "n": 0,
                "detail": f"{type(exc).__name__}: {exc}"[:300]}


def probe_dice(cfg: dict) -> dict:
    try:
        c = DiceMCP(cfg["sources"]["dice"]["mcp_url"]).connect()
        tools = c.list_tools()
        jobs = c.search("director security", "Seattle, WA", 10)
        return {"name": "Dice (MCP)", "type": "dice", "ok": True, "n": len(jobs),
                "detail": f"tools={tools}; {len(jobs)} results"}
    except Exception as exc:
        return {"name": "Dice (MCP)", "type": "dice", "ok": False, "n": 0,
                "detail": f"{type(exc).__name__}: {exc}"[:300]}


def probe_aggregators() -> dict:
    from .sources import aggregators
    if not aggregators.AVAILABLE:
        return {"name": "Aggregators (JobSpy)", "type": "aggregators", "ok": False,
                "n": 0, "detail": aggregators.REASON}
    return {"name": "Aggregators (JobSpy)", "type": "aggregators", "ok": True,
            "n": 0, "detail": "jobspy importable"}


def run(record: bool = True) -> list[dict]:
    """Probe every configured source once.

    This takes the same run lock as a collection. It touches every employer
    board at once with eight workers, so running it during a collection doubled
    the request rate every politeness limit exists to hold down -- and it looked
    to the source like a single client misbehaving.
    """
    from . import pipeline

    if not pipeline._acquire("verify"):
        held = pipeline.running() or {}
        return [{"name": "verify", "type": "lock", "ok": False, "n": 0,
                 "detail": f"a '{held.get('kind', '?')}' run is already in "
                           f"progress; verification skipped"}]
    try:
        return _probe_all(record)
    finally:
        pipeline._release()


def _probe_all(record: bool) -> list[dict]:
    cfg = config.load(force=True)
    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(probe_employer, e) for e in cfg.get("employers", [])]
        futures.append(pool.submit(probe_dice, cfg))
        futures.append(pool.submit(probe_aggregators))
        for f in cf.as_completed(futures):
            results.append(f.result())
    results.sort(key=lambda r: (not r["ok"], r["name"].lower()))
    if record:
        db.init()
        for r in results:
            db.set_health(r["name"], r["ok"], r["detail"], r["n"])
    return results


if __name__ == "__main__":
    for r in run():
        flag = "OK  " if r["ok"] else "FAIL"
        print(f"{flag} {r['name']:<24} {r['type']:<16} {r['detail']}")
