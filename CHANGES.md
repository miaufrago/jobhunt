# Changes from the original

Every change came out of running this board daily for one real job search, and
each one is here because something went wrong first.

## Scoring

**Hard-requirements gate** (`jobhunt/scoring/hard_requirements.py`, new)
Reads the two things a keyword scorer cannot see: a years-of-experience bar, and
an onsite, hybrid, residency or region mandate stated in the body rather than the
metadata. A posting behind either is capped below the queue threshold instead of
being ranked on keyword overlap.

Both rules are deliberately lenient, because a false positive here is silent -- it
drops a posting out of the queue and nobody ever sees it. The years rule takes the
*lowest* stated bar, since postings list alternative entry paths and only one must
be met. The location rule fires only when no wanted location appears alongside the
mandate and the same sentence offers no remote alternative.

Measured against 262 hand-read assessments on a live corpus, the heuristic's
correlation with actual fit rose from 0.096 to 0.30, and its optimism fell from
16.4 points to 10.4.

Configured under `profile.hard_requirements`; disabled unless enabled.

**Annual pay periods** (`jobhunt/salary.py`)
"per year", "annually", "/yr" and friends now resolve the period, so an annual
range is no longer read as something else.

## Reliability

**Scheduler misfire grace** (`jobhunt/web.py`)
APScheduler's default grace is one second, so a cron firing while the machine is
asleep was discarded outright -- silently, with only a "was missed by" line in the
log. On a laptop that meant losing days of collection. Each job now gets a grace
just under its own interval (23h for a daily sweep, 5h for a thrice-daily poll)
with `coalesce`, so a missed run starts on wake and a weekend asleep produces one
sweep rather than three.

**MCP Host header** (`jobhunt/sources/mcp.py`)
MCP servers built on FastMCP validate the Host header as DNS-rebinding protection
and answer `421 Misdirected Request` to anything that is not loopback -- including
a Docker Compose service name. Reaching a sidecar at `http://linkedin-mcp:8080/mcp`
failed outright. The client now sends `Host: localhost` for plain-HTTP container
hostnames only; hosted HTTPS servers keep their real Host, so TLS and virtual-host
routing are untouched. Removes the need to pin container IPs.

## Onboarding

**config.example.yaml**
The example shipped `domains`, `secondary_regions` and `extra_disqualifiers` as
lists where the code expects mappings, so copying it as the README instructed
raised `AttributeError: 'list' object has no attribute 'items'` on every posting.
Shapes corrected, each one commented, and the profile neutralised so it belongs to
nobody.

**README / docs**
Corrected the config path (the live file is `data/config.yaml`; a `config.yaml` at
the repo root is read by nothing). Rewrote the deployment and LinkedIn login paths
for a laptop; the remote-host tooling is kept and marked advanced.

**QUICKSTART.md** and **`/setup-jobhunt`** (new)
A ten-minute path, and a Claude Code skill that reads a new user's resume,
interviews them, writes their profile, starts the board and reads the first
results with them.

## Tools

**tools/daily_digest.py** (new)
Writes a dated apply-list from assessed postings only -- never raw heuristic
output, because the heuristic is a filter and not a ranking. Anything dismissed,
applied, muted or closed drops out. Read-only against the database.

## Tests

**tests/test_hard_requirements.py** (new)
21 cases, every one drawn from a real posting. The false-positive cases matter
most: each was found by replaying the gate against assessments that had already
been read by hand.
