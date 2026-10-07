#!/usr/bin/env python3
"""Reconcile the Five Before Noon artifact's ledger into the board.

Why the artifact cannot just post to the board itself: a published artifact runs
under a CSP that blocks every fetch to every host, silently. The page keeps its
own record in its artifact database and the board pulls from it. That pull is
this script.

Why this script talks HTTP instead of opening jobs.db: the data volume is a
Docker bind mount, and SQLite's WAL mode needs shared-memory locking that does
not survive the host/VM boundary. An earlier version of this tool wrote to
data/jobs.db from the host while the container had it open, and the database came
back "malformed" the next time anything touched it. The container is the only
process that writes. Everything else comes through /api/dispositions.

Get the artifact's state with the Artifact tool:

    action: read_db, db_op: get, collection: tracker, doc_id: apply-state

save the document to a file, then:

    python3 tools/sync_apply_state.py --state apply-state.json            # dry run
    python3 tools/sync_apply_state.py --state apply-state.json --commit

Reads JOBHUNT_URL (default http://localhost:8377) and JOBHUNT_TOKEN, falling
back to APP_PASSWORD in .env. Stdlib only.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

# A "Not for me" tap records a decision, not a diagnosis. The board has ten
# dismissal reasons and the artifact has one button, so this is the honest
# mapping; anything more specific belongs on the job page.
SKIP_REASON = "not_interested"


def token_from_env_file(path=".env"):
    try:
        for line in open(path):
            if line.startswith("APP_PASSWORD="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def post(url, token, payload, commit):
    body = json.dumps(payload).encode()
    target = f"{url.rstrip('/')}/api/dispositions"
    if not commit:
        print(f"dry run — would POST {len(payload)} disposition(s) to {target}")
        return None
    req = urllib.request.Request(
        target, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:400]}", file=sys.stderr)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True,
                    help="JSON file: the artifact doc, or just its `data` object")
    ap.add_argument("--url", default=os.environ.get("JOBHUNT_URL",
                                                    "http://localhost:8377"))
    ap.add_argument("--token", default=os.environ.get("JOBHUNT_TOKEN") or
                                      token_from_env_file())
    ap.add_argument("--commit", action="store_true",
                    help="actually post; without this it only reports")
    ap.add_argument("--skip-reason", default=SKIP_REASON)
    args = ap.parse_args()

    raw = json.load(open(args.state))
    state = raw.get("data", raw)
    applied = list(state.get("applied") or [])
    skipped = list(state.get("skipped") or [])
    if not applied and not skipped:
        print("nothing in the artifact ledger")
        return 0

    bad = [p for p in applied + skipped if not re.fullmatch(r"[0-9a-f]{6,40}", p)]
    if bad:
        print(f"ignoring {len(bad)} malformed id(s): {bad[:5]}", file=sys.stderr)

    payload = (
        [{"job_id": p, "status": "applied",
          "note": "applied — recorded in Five Before Noon"}
         for p in applied if p not in bad] +
        [{"job_id": p, "status": "dismissed", "reason": args.skip_reason,
          "note": "passed in Five Before Noon"}
         for p in skipped if p not in bad])

    print(f"artifact ledger: {len(applied)} applied, {len(skipped)} passed"
          f"  (updated {state.get('updated', 'unknown')})")
    for item in payload:
        print(f"  {item['job_id']}  -> {item['status']}")

    result = post(args.url, args.token, payload, args.commit)
    if result is None:
        print("\nre-run with --commit to write.")
        return 0

    print(f"\nwrote {result['written']} row(s)"
          f"; {result['also_applied_to_other_listings']} other listing(s) of the "
          f"same role followed along")
    if result.get("unknown_ids"):
        print(f"  no such posting: {result['unknown_ids']}")
    if result.get("ambiguous_prefixes"):
        print(f"  ambiguous, skipped: {result['ambiguous_prefixes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
