#!/usr/bin/env python3
"""Write a daily apply-to shortlist from assessed postings.

The board ranks everything; this answers a narrower question -- what should I do
today. It only ever lists postings a human has actually read and scored (model
`claude-code`), because the free heuristic is a filter, not a ranking: measured
against 262 hand-read assessments its correlation with real fit was 0.30.

Anything dismissed, applied, muted or closed drops out, so marking a posting on
the board is what makes it leave the digest. Nothing here writes to the
database.

    python3 tools/daily_digest.py --db data/jobs.db --out "~/notes/Job Digest"

The real Obsidian vault is the one with a `.obsidian` directory in it. Check that
before pointing --out anywhere: eleven digests were once written to a lookalike
folder that Obsidian never opens, and nobody noticed for two weeks.

Stdlib only, runs on the host, no container needed.
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

STRONG_LIMIT = 25
POSSIBLE_LIMIT = 12
POSSIBLE_FLOOR = 60
STALE_DAYS = 18          # reap closes at 21; warn before that
NEW_WINDOW_HOURS = 30     # "new since the last digest", with slack for a missed run


def _q(con, sql, *args):
    return con.execute(sql, args).fetchall()


def _fmt_pay(row):
    lo, hi = row["salary_min"], row["salary_max"]
    if not lo and not hi:
        return "**no pay posted**"
    if lo and hi and int(lo) != int(hi):
        return f"{int(lo/1000)}-{int(hi/1000)}k"
    return f"{int((lo or hi)/1000)}k"


def _age_days(iso, now):
    """Days since a posting first appeared.

    `first_seen` is stored timezone-aware; `now` is local and naive. Subtracting
    the two raises, so normalise both to naive UTC before comparing.
    """
    if not iso:
        return None
    try:
        seen = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if seen.tzinfo is not None:
        seen = seen.astimezone(dt.timezone.utc).replace(tzinfo=None)
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return (now - seen).days


LIVE = """
  FROM scores s
  JOIN jobs j ON j.id = s.job_id
  LEFT JOIN actions a ON a.job_id = j.id
 WHERE s.model = 'claude-code'
   AND j.closed = 0
   AND COALESCE(a.status, 'new') = 'new'
   AND j.company_norm NOT IN (SELECT value FROM mutes WHERE kind = 'company')
"""


def build(con, now):
    out = []
    add = out.append
    today = now.strftime("%Y-%m-%d")

    add(f"---\ntype: job-digest\ndate: {today}\ntags: [career, jobhunt]\n---\n")
    add(f"# Apply list - {now.strftime('%A %-d %B %Y')}\n")

    strong = _q(con, f"""SELECT j.id, j.company, j.title, j.location, j.url, j.first_seen,
                                j.salary_min, j.salary_max, s.fit_score, s.headline, s.red_flags,
                                (SELECT variant FROM resume_variant v WHERE v.job_id=j.id) AS variant
                         {LIVE} AND s.verdict = 'strong'
                         GROUP BY COALESCE(j.role_key, j.id)
                         ORDER BY s.fit_score DESC LIMIT ?""", STRONG_LIMIT)

    possible = _q(con, f"""SELECT j.id, j.company, j.title, j.location, j.url, j.first_seen,
                                  j.salary_min, j.salary_max, s.fit_score, s.headline, s.red_flags,
                                  (SELECT variant FROM resume_variant v WHERE v.job_id=j.id) AS variant
                           {LIVE} AND s.verdict = 'possible' AND s.fit_score >= ?
                           GROUP BY COALESCE(j.role_key, j.id)
                           ORDER BY s.fit_score DESC LIMIT ?""", POSSIBLE_FLOOR, POSSIBLE_LIMIT)

    fresh = {r["id"] for r in _q(con, f"""SELECT j.id {LIVE}
                AND s.verdict IN ('strong','possible')
                AND s.scored_at > datetime('now', ?)""", f"-{NEW_WINDOW_HOURS} hours")}

    def block(rows, heading, blurb):
        if not rows:
            return
        add(f"## {heading}\n\n{blurb}\n")
        for r in rows:
            age = _age_days(r["first_seen"], now)
            marks = []
            if r["id"] in fresh:
                marks.append("**NEW**")
            if age is not None and age >= STALE_DAYS:
                marks.append(f"**{age}d old - closing soon**")
            flags = json.loads(r["red_flags"] or "[]")
            money = _fmt_pay(r)
            if "no pay posted" in money:
                marks.append("ask comp first")
            tag = ("  -  " + " | ".join(marks)) if marks else ""
            add(f"### {r['company']} - {r['title']}{tag}")
            add(f"*{r['fit_score']} | {money} | {r['location'] or 'location not stated'}*\n")
            if r.keys() and "variant" in r.keys() and r["variant"]:
                add(f"**Send:** {r['variant']}\n")
            add(f"{r['headline']}\n")
            if flags:
                add(f"> Watch: {flags[0]}\n")
            add(f"[Apply]({r['url']})\n")

    block(strong, "Apply to these",
          "Read and scored a strong match. Nothing here has been actioned yet.")
    block(possible, "Worth an hour",
          f"Scored possible at {POSSIBLE_FLOOR}+. Real matches with a stated gap - "
          "read the watch line before committing time.")

    # ---- what still needs a human pass -------------------------------------
    waiting = _q(con, """SELECT COUNT(*) n FROM scores s JOIN jobs j ON j.id = s.job_id
                          WHERE s.stage = 'heuristic' AND j.closed = 0 AND s.fit_score >= 55""")[0]["n"]
    gated = _q(con, """SELECT COUNT(*) n FROM scores
                        WHERE stage = 'heuristic' AND red_flags LIKE '%Hard requirement%'""")[0]["n"]
    add("## Queue\n")
    add(f"- **{waiting}** postings cleared the filter and still need a real read. "
        "Ask for an assessment pass when that number gets uncomfortable.")
    add(f"- {gated} were auto-rejected on a stated bar - years of experience, or an "
        "onsite mandate buried in the body.")
    add(f"- {len(strong)} strong and {len(possible)} possible on this list, "
        "all still unactioned.\n")
    add("Mark anything applied or dismissed on the board and it leaves this digest.\n")
    add(f"<http://localhost:8377>\n")
    add(f"\n---\n*Generated {now.strftime('%Y-%m-%d %H:%M')}. "
        "Only hand-read assessments appear here.*\n")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--out", help="directory for the dated note; omit with --stdout")
    ap.add_argument("--also", action="append",
                    help="extra directory to write into; failures are skipped")
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args()

    db = os.path.expanduser(a.db)
    if not os.path.exists(db):
        sys.exit(f"no database at {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    now = dt.datetime.now()
    text = build(con, now)

    if a.stdout or not a.out:
        print(text)
        return
    name = f"Apply list {now.strftime('%Y-%m-%d')}.md"
    written = []
    # --out first, then any --also targets. On macOS a launchd agent has no TCC
    # grant for ~/Documents, ~/Desktop or ~/Downloads, so writing into an
    # Obsidian vault there fails with EPERM even though the same command works
    # from a terminal. Treat every destination as best-effort and keep going, so
    # one blocked path cannot cost the whole digest.
    for target in [a.out] + (a.also or []):
        d = os.path.expanduser(target)
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, name)
            with open(path, "w") as fh:
                fh.write(text)
            # A stable filename so a bookmark or hotkey always opens the latest.
            with open(os.path.join(d, "Apply list - latest.md"), "w") as fh:
                fh.write(text)
            written.append(path)
        except OSError as exc:
            print(f"skipped {d}: {exc.strerror}", file=sys.stderr)
    if not written:
        sys.exit("could not write the digest anywhere")
    for p_ in written:
        print(p_)


if __name__ == "__main__":
    main()
