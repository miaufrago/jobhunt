"""CLI. The web UI calls the same functions; this is for cron, debugging, and first run."""
import argparse
import json
import pathlib
import sys

from . import config, db, pipeline, score, verify


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="jobhunt")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database")
    r = sub.add_parser("run", help="collect from all sources, then score")
    r.add_argument("--kind", choices=["full", "poll"], default="full")
    r.add_argument("--no-score", action="store_true")
    r.add_argument("--score-limit", type=int, default=10000)

    s = sub.add_parser("score", help="score jobs that have no current score")
    s.add_argument("--limit", type=int, default=10000)
    s.add_argument("--force", action="store_true",
                   help="rescore, skipping rows that already hold a real assessment")
    s.add_argument("--discard-assessments", action="store_true",
                   help="with --force, also overwrite model-written assessments "
                        "(destructive; a snapshot is taken first)")

    sub.add_parser("verify-employers", help="probe every source and report health")
    sub.add_parser("stats", help="print database counters")

    b = sub.add_parser("backup", help="snapshot the database")
    b.add_argument("--tag", default="manual")
    b.add_argument("--keep", type=int, default=14)

    x = sub.add_parser("export-scores", help="dump assessments as JSON")
    x.add_argument("--out", default="-")

    r = sub.add_parser("reap", help="close postings their source has stopped listing")
    r.add_argument("--days", type=int, default=21)

    sub.add_parser("calibrate",
                    help="how well the heuristic gate predicts assessments")

    v = sub.add_parser("serve", help="run the web UI")
    v.add_argument("--host", default="0.0.0.0")
    v.add_argument("--port", type=int, default=8080)

    a = p.parse_args(argv)

    if a.cmd == "init":
        db.init()
        print(f"database ready at {config.db_path()}")
        return 0

    if a.cmd == "run":
        out = pipeline.run(a.kind, do_score=not a.no_score, score_limit=a.score_limit)
        print(json.dumps(out, indent=2))
        return 0

    if a.cmd == "score":
        db.init()
        print(json.dumps(score.score_pending(limit=a.limit, force=a.force,
                                        discard_assessments=a.discard_assessments), indent=2))
        return 0

    if a.cmd == "calibrate":
        print(json.dumps(score.calibration(), indent=2))
        return 0

    if a.cmd == "backup":
        print(db.snapshot(a.tag, keep=a.keep))
        return 0

    if a.cmd == "export-scores":
        payload = json.dumps(db.export_assessments(), indent=2)
        if a.out == "-":
            print(payload)
        else:
            pathlib.Path(a.out).write_text(payload, encoding="utf8")
            print(f"wrote {a.out}")
        return 0

    if a.cmd == "reap":
        n = db.close_stale(a.days)
        print(json.dumps({"closed": n, "older_than_days": a.days}, indent=2))
        return 0

    if a.cmd == "verify-employers":
        bad = 0
        for r in verify.run():
            flag = "OK  " if r["ok"] else "FAIL"
            bad += 0 if r["ok"] else 1
            print(f"{flag} {r['name']:<24} {r['type']:<16} {r['detail']}")
        print(f"\n{bad} source(s) failing")
        return 0

    if a.cmd == "stats":
        db.init()
        with db.connect() as con:
            for label, sql in (
                ("jobs", "SELECT COUNT(*) FROM jobs WHERE closed=0"),
                ("scored", "SELECT COUNT(*) FROM scores WHERE stage='scored'"),
                ("filtered", "SELECT COUNT(*) FROM scores WHERE stage!='scored'"),
                ("fit>=70", "SELECT COUNT(*) FROM scores WHERE fit_score>=70"),
            ):
                print(f"{label:<10} {con.execute(sql).fetchone()[0]}")
        return 0

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("jobhunt.web:app", host=a.host, port=a.port)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
