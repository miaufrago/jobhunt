# jobhunt

One consolidated job board with one fit assessment, run from your own container.

Pulls postings from LinkedIn (authenticated, via a self-hosted MCP server),
Indeed, Google Jobs, Dice, and
43 employers' own ATS feeds. Merges duplicates, scores each survivor against your
resume with a rubric you control, and shows the result as a ranked board.

## Why the ATS feeds matter

Aggregators truncate job descriptions and drop or delay leadership postings. An
employer's own board carries the full text and the real apply link. The pipeline
prefers the ATS copy whenever both exist -- see `SOURCE_RANK` in `jobhunt/normalize.py`.

Employers whose ATS has no public API (Microsoft, Starbucks, Costco, Alaska Airlines,
Zillow, and others -- Eightfold, Phenom, iCIMS, NEOGOV) are covered by the aggregator
pass instead. The bottom of `config.yaml` lists them.

## Make it yours first

**Nothing in the code is specific to any one person.** Everything personal lives
in two files, and neither is tracked in git:

```bash
cp config.example.yaml config.yaml        # your titles, metros, pay, employers
cp profile/resume.example.md profile/resume.md
```

Then edit both. `config.yaml` is heavily commented; the knobs that matter most
are `profile.domains`, `profile.include_title_patterns`, `profile.target_levels`
and `profile.pay`. See **Tuning the profile** below.

If you skip this step the app seeds both files from the examples on first run,
so it starts — it will just be scoring against a fictional resume.

Running a board for a second person means a second container with its own data
volume. It does not mean a fork.

## Run it

```bash
cp .env.example .env          # optional: ANTHROPIC_API_KEY, APP_PASSWORD
docker compose up -d --build
```

That publishes port 8377 on the host. Open `http://<host>:8377`.

### Running it on a macvlan address instead

If a reverse proxy on the same host runs on a macvlan network, it cannot reach
the host's own IP, so give the container its own address rather than publishing
a host port:

```bash
docker run -d --name jobhunt --restart unless-stopped \
  --network <your-macvlan-net> --ip 192.168.1.10 \
  -v /srv/appdata/jobhunt:/data -e TZ=Etc/UTC jobhunt:latest
```

Then point the proxy at `http://192.168.1.10:8080` — the container's internal port,
not a published one. `tools/deploy.sh` automates this and pins a MAC derived
from the IP, so a DHCP server's reservations keep matching across recreates.

### State

State lives in the data volume, not in the repo:

```
config.yaml           search terms, target titles, locations, employers, schedule
profile/resume.md     what every assessment scores against
jobs.db               postings, scores, your shortlist/applied marks
```

Both `config.yaml` and the resume are editable from the Settings page. On a
fresh volume the container seeds them from `config.example.yaml` and
`profile/resume.example.md`.

## LinkedIn

LinkedIn carries the employers with no public ATS feed — Microsoft, Meta,
Alphabet, Apple, Starbucks, Costco. It runs through a self-hosted MCP server
holding an authenticated session.

**There is no cookie to copy.** The container runs the login browser and you drive
it from your own browser through a one-time viewer URL. Setup, the re-login
procedure, the required `TIMEOUT=120000`, the rate limits and the audit findings
are all in **[docs/LINKEDIN.md](docs/LINKEDIN.md)**. Read that before touching the
container.

## Pages

| Page | What it does |
|---|---|
| Board | Every posting ranked by fit. Filter by score, status, company, age. |
| Job | Score breakdown: resume evidence, gaps, red flags, pay, full JD. Set status, reason and notes; mute the company. |
| Sources | Per-source health and recent runs. **Verify all sources** re-probes every feed. |
| Dismissed | Why postings were set aside, muted companies, and recent dismissals |
| Settings | Edit `config.yaml` and the resume. Saving reloads the schedule. |

## Tracking what you have decided

Every row carries the date it was first seen and its age in days, plus three
controls: shortlist, applied, and a dismiss dropdown.

Dismissing takes a **reason** rather than a bare flag -- `pay_too_low`,
`wrong_level`, `wrong_domain`, `not_a_fit`, `company`, `location`, `contract`,
`stale`, `applied_elsewhere`, `not_interested`. A dismissed posting never returns
to the board, even when a later sweep re-collects it.

The reasons are the point. The **Dismissed** page aggregates them, and the
aggregate is feedback on the search itself: twelve dismissals for "pay too low"
means the search terms are aimed below the target, not that twelve separate roles
were wrong. Notes are free text and render on the board under the title.

**Muting a company** hides every posting it files, past and future -- useful for
the consultancies that flood a security search. Unmute from the Dismissed page.

## Schedule

Set in `config.yaml`, run by an in-container scheduler:

- `full_sweep_cron` -- every source including all employer ATS feeds (default 6am)
- `aggregator_poll_cron` -- aggregators and Dice only (default every 3h, 8am-6pm)

LinkedIn rate-limits around the tenth page from one IP, so the poll stays narrow.

## Scoring

No hosted API is required. Two stages:

1. **Heuristic** (always, free, instant) -- `jobhunt/scoring/heuristic.py` scores
   five components separately and reports each: seniority band against
   `target_level_range`, domain overlap against weighted charter phrases,
   **compensation**, location, and listing quality. Staffing-agency listings,
   contract roles, clearance requirements, and downlevels come back as red flags.

   Relative importance is set by `profile.scoring_weights` (normalized, so the
   values need not total 100). **Compensation is a gate, not a tiebreaker:** a
   stated range topping out below `profile.pay.floor` scores zero for pay *and*
   caps the verdict, however well the role fits otherwise. A posting that states
   no range is neutral, not penalized -- roughly half omit it.
2. **Deep score** (optional) -- everything above `deep_score_threshold`, capped at
   `deep_score_max_per_run`, goes to a model that reads the full posting and
   returns rationale, resume evidence, and gaps.

The gate is what makes stage 2 cheap: a few hundred postings a day become a few
dozen worth reading in full. A posting is rescored only when its description changes.

### Knobs beyond the five weights

A few `profile` settings shape what reaches the board at all, or how the five
components above are read, and are easy to miss because they live outside
`scoring_weights`.

**Location scope** is decided before scoring runs, not as part of it -- a
posting outside scope is dropped entirely rather than marked down. With
`profile.remote_scope: us_only` (the default), `jobhunt/scoring/locations.py`
keeps a posting when it is remote and reachable from the US, or on-site in one
of the metros listed in `profile.locations`; a posting whose only stated
location is a foreign city or region is dropped before it is ever scored. A
location that names only the country -- "United States", with no city or
state -- is kept rather than dropped, because that string means a national
listing in practice: multi-site or remote in all but name, and excluding it
would lose real roles. `profile.exclude_location_patterns` is a list of
regexes checked first, for anything the built-in country markers get wrong.
Set `remote_scope: anywhere` to skip the country check entirely.

**Seniority** is judged against `profile.target_level_range` -- a `[low, high]`
band on this 0-9 scale -- unless `profile.target_levels` is set, which takes
precedence:

| 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|
| entry | IC | senior IC | principal IC | manager | senior manager | director | senior director | VP | chief |

Use the explicit list when the target has a hole in it that a range cannot
express -- "principal IC, or director and above, but not the manager rungs in
between" is `target_levels: [3, 6, 7, 8, 9]`, and no `[low, high]` pair can say
that.

**`profile.industry_preference`** (`bonus`, `prefer`, `deprioritize`) nudges the
score a few points for or against postings whose company, title, or opening
description matches the listed terms. It is deliberately not a filter: a
candidate leaving an industry may still want to see what it is offering, so a
deprioritized posting stays on the board, still gets scored on its merits, and
says in its gaps why it was marked down -- it does not simply disappear.

**`profile.verdict_thresholds`** (`strong`, `possible`, `weak`; default
`72`/`55`/`35`) sets the `fit_score` cutoffs that turn the number into a
verdict. The pay floor still overrides these cutoffs when it caps the verdict
-- see "Compensation is a gate" above.

### Stage 2 without an API key -- the pull queue

The container cannot start an assistant session, so it does not try. It publishes
a queue instead, and you pull it when you are already in one:

> score the job queue

That runs `.claude/skills/score-jobs`, which fetches `/api/queue`, assesses each
posting against the resume, and posts the results back to `/api/scores`. Results
land on the board marked `model: claude-code`. No API key, no local model, no cost
beyond the session you were already in.

| Endpoint | Purpose |
|---|---|
| `GET /api/queue?limit=25` | Postings above the threshold that still need assessment, plus the resume and rubric |
| `POST /api/scores` | Write assessments back |
| `GET /api/stats` | Counters |

Auth is `?token=` or an `X-API-Token` header, matching `API_TOKEN` (or `APP_PASSWORD`).

### Stage 2 unattended

Set `scoring.backend` if you want stage 2 to run on the schedule without you:

| Value | Behaviour |
|---|---|
| `auto` | Anthropic if `ANTHROPIC_API_KEY` exists, else Ollama if reachable, else heuristic only |
| `ollama` | A local model on the LAN: `docker run -d --name ollama -p 11434:11434 -v /srv/appdata/ollama:/root/.ollama ollama/ollama` then `ollama pull qwen3:8b`. On a GPU-less Xeon budget about a minute per posting. |
| `anthropic` | The hosted API. |
| `heuristic` | Stage 1 only. The pull queue still works. |

Both backends read the same rubric. `rubric_for(cfg)` in `jobhunt/scoring/llm.py`
builds it straight from `profile` -- the same weights, target level band, domain
list, and pay floor the heuristic scorer reads -- so a model assessment and the
deterministic one are working from the same policy and cannot disagree about
what matters, only about how well a given posting meets it. Both backends also
return the same JSON shape `heuristic_score` does (`fit_score`, `verdict`,
`headline`, `rationale`, `strengths`, `gaps`, `red_flags`), which is why the
board and the Job page render either result identically.

## CLI

```bash
python -m jobhunt.cli run --kind full     # collect everything, then score
python -m jobhunt.cli run --kind poll     # aggregators + Dice only
python -m jobhunt.cli score --force       # rescore everything against a new resume
python -m jobhunt.cli verify-employers    # probe every source, report health
python -m jobhunt.cli stats
```

## Adding an employer

Add an entry to `employers:` in `config.yaml`, then run `verify-employers`.

```yaml
- {name: Acme,   type: greenhouse, slug: acme}
- {name: Globex, type: workday, host: globex.wd1.myworkdayjobs.com, tenant: globex, site: External}
```

Types: `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `workday`, `amazon`.

To find an employer's endpoint, `tools/discover_ats.py` scrapes a careers page for
ATS links and `tools/probe_wide.py` brute-probes Workday tenants and board slugs.

## Local development

JobSpy needs pandas, which has no wheel for Python 3.13+. The image pins 3.12. On a
newer local interpreter every other source still works; the aggregator pass reports
itself unavailable on the Sources page.

### Tests

```bash
pytest tests
```

**Run them with `APP_PASSWORD` unset.** The page tests drive the board through a
test client and assume LAN-only mode, so a password in the environment redirects
every request to the sign-in form, and six of them fail on markup that is really
just the login page. Unset it, or run in a shell that never had it:

```bash
env -u APP_PASSWORD -u API_TOKEN pytest
```

The suite needs the same interpreter as the image — on Python 3.13+ the imports
fail on pandas before any test runs. The simplest way to get a matching
environment is the container itself:

```bash
docker compose exec jobhunt sh -c 'pip install -q pytest && env -u APP_PASSWORD python -m pytest'
```

## Attribution and licensing

Derived from jobhunt by Dominic Williams, used with permission. Published
without a license — all rights reserved. See [NOTICE.md](NOTICE.md).
