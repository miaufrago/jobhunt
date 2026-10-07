# jobhunt — orientation for a new session

A consolidated job board. It collects postings from job aggregators, an official
Dice MCP server, and ~43 employers' own ATS feeds; merges duplicates; extracts
stated pay; scores every posting against one candidate's resume with a rubric the
candidate controls; and serves the result as a ranked board.

**Nothing in the code is specific to one candidate.** Everything personal lives in
`config.yaml` and `profile/resume.md`, both mounted from the instance's data
volume and both untracked. The repo ships `config.example.yaml` and
`profile/resume.example.md` as templates. Running a second board for a different
person means a second container with a different volume — not a fork.

## Read these first

| File | Why |
|---|---|
| `config.example.yaml` | The entire profile surface: titles, locations, pay policy, scoring weights, domain phrases, employers, schedule |
| `jobhunt/scoring/heuristic.py` | How a posting is scored without a model. Read `heuristic_score` top to bottom; it is the product. |
| `jobhunt/scoring/llm.py` | The optional model-backed stage 2. `rubric_for(cfg)` derives the prompt from the same `profile` the heuristic scorer reads, so the two cannot disagree about what matters. |
| `jobhunt/pipeline.py` | Collection orchestration, phasing, the run lock |
| `jobhunt/api.py` | The pull queue and the ingest endpoint |
| `jobhunt/db.py` | Schema, `DISMISS_REASONS`, dispositions, company mutes |
| `README.md` | Deployment, endpoints, CLI, and the `profile` scoring knobs |
| `docs/LINKEDIN.md` | The LinkedIn session: login, limits, tool surface, audit |

## Architecture in one pass

```
sources/ ──► pipeline.collect() ──► normalize ──► salary.enrich ──► db.upsert_job
   │              (2 phases)         (dedupe key)   (pay extraction)      │
   │                                                                      ▼
   │                                                          scoring/heuristic.py
   │                                                                      │
   ├── ats.py            Greenhouse / Lever / Ashby / Workday / Amazon
   ├── aggregators.py    JobSpy: Indeed, Google Jobs                ┌──────────┴──────────┐
   ├── dice.py           official Dice MCP server                   │                     │
   ├── linkedin_mcp.py   self-hosted MCP, authenticated session   board          pull queue (api.py)
   └── POST /api/jobs    anything collected elsewhere            (web.py)                 │
                                                                        a session assesses and
                                                                        POSTs back to /api/scores
```

**Dispositions are durable.** `actions` holds status + reason + note per posting;
`mutes` holds companies to hide entirely. The board excludes dismissed postings
and muted companies at query time, so a re-collected posting stays gone. Dismissal
reasons are an enum on purpose -- their distribution is feedback on the search
(see `/mutes`), which free text cannot give.

**Two scoring stages.** Every posting gets a heuristic score — free, instant,
explainable. Whatever clears `deep_score_threshold` is queued for a real
assessment. That gate is what keeps stage 2 affordable.

**Stage 2 has no required backend.** `scoring.backend` can point at Ollama or the
Anthropic API, but the default path is the pull queue: a session runs the
`score-jobs` skill, reads `/api/queue`, assesses, and POSTs to `/api/scores`. No
API key, no local model.

## Standing constraints

These are design decisions, not accidents. Do not undo them without asking.

1. **Be polite to every source.** `sources/http.py` paces requests per *platform*,
   not per host — a dozen Workday tenants are one Workday. `PLATFORM_LIMITS` sets
   Workday's own minimum interval (1.2s) stricter than any other platform's,
   because Workday refuses connections outright under load instead of returning
   a 429 to back off from. `POLITENESS_SCALE` multiplies every interval in that
   table at runtime, so a second instance sharing the public IP slows itself
   down without anyone touching the limits themselves. Never bypass `session()`;
   never raise the limits to go faster.
2. **Collection runs in phases.** Aggregators fetch every LinkedIn description
   individually and will starve the employer feeds if they share a pool. Keep
   `_run_phase` separation.
3. **Comp is a gate, not a tiebreaker.** A stated range below `profile.pay.floor`
   caps the verdict however well the role fits. Do not "fix" this.
4. **Deploys drop the container.** Use `tools/deploy.sh` (~3s). Say so before
   running it — a reverse proxy returns 502 during the restart.
5. **LinkedIn runs on a real authenticated session.** It reaches employers with
   no public feed, and the account matters more than the coverage. Call only
   `search_jobs` / `get_job_details` / `get_saved_jobs`; never the messaging or
   connection tools. Do not raise the limits in `sources.linkedin_mcp` to catch
   up after a missed run. Full detail, including the one-time login and the
   `TIMEOUT=120000` requirement, in `docs/LINKEDIN.md`.
6. **Never start a second collection run.** `pipeline.running()` holds a lock on
   the shared volume. Check it before deploying; a redeploy kills a live run.
7. **A run has a deadline.** `sources.run_budget_seconds` bounds the whole
   collection run and is scaled by `POLITENESS_SCALE` like every rate limit.
   Whatever source is still running when it expires is recorded as timed out
   on the Sources page rather than left to wedge the run indefinitely.
8. **Personal data never enters git.** `config.yaml`, `profile/resume.md`,
   `profiles/`, `backups/` and `instances/*.env` are all gitignored. Only the
   `.example.*` templates are tracked. Check `.gitignore` before adding a file
   that holds a resume, an assessment, a hostname or a token.

## Running a second instance

Every instance-specific value is an environment variable. Nothing is forked.

```sh
cp instances/example.env instances/<name>.env   # edit it
./tools/deploy.sh --env instances/<name>.env --rebuild
```

Then seed that instance's data volume with its own `config.yaml` and
`profile/resume.md`. Both are editable from the Settings page afterwards.

| Variable | Purpose |
|---|---|
| `INSTANCE` | Container name, shown in the UI and returned by the API |
| `JOBHUNT_IP` | Its own address on the container network — one per instance. `deploy.sh` derives the pinned MAC from it. |
| `JOBHUNT_DATA_DIR` | Its own appdata directory; holds the database, config, resume |
| `POLITENESS_SCALE` | Instances behind one public IP share a reputation. Set `2.0` on a second instance to halve its request rate. |
| `APP_PASSWORD` / `API_TOKEN` | Optional; unset means LAN-only with no login |

**Instances behind one public IP share a reputation with every source.** Stagger
their cron schedules in `config.yaml` and raise `POLITENESS_SCALE` rather than
running two full sweeps at once.

## Tailoring the profile to a different career

The scorer is generic; the profile is not. To retarget it, edit only `config.yaml`:

- `profile.domains` — **the biggest lever.** Weighted phrases that evidence the
  candidate's real charters. A security profile and an insurance profile share
  none of these.
- `profile.include_title_patterns` / `exclude_title_patterns` — the coarse gate.
- `profile.target_level_range` — seniority band, 0–9 (6 director, 8 VP, 9 chief).
  Set `profile.target_levels` instead when the target has a hole in it a range
  can't express (e.g. principal IC plus director-and-above, skipping manager) --
  it takes precedence when both are set. Full scale and the rest of the
  scoring-relevant profile keys (`remote_scope`, `industry_preference`,
  `verdict_thresholds`) are in `README.md`.
- `profile.pay` — `target_base`, `floor`, and whether the floor caps the verdict.
- `profile.scoring_weights` — relative importance of level, domain, pay, location,
  listing. Normalized, so they need not total 100.
- `profile.extra_disqualifiers` — appended to the generic built-ins.
- `employers` — verify any new entry with `python -m jobhunt.cli verify-employers`.

For a candidate pursuing **two tracks at once** (say, staying in an industry and
breaking into a new one), prefer one instance with both tracks represented in
`domains` and a union of `include_title_patterns`. Two instances means two
databases and no cross-track dedupe. Split only if the pay policy or target level
genuinely differs between the tracks.

## Finding an employer's ATS

`tools/discover_ats.py` scrapes a careers page for ATS links.
`tools/probe_wide.py` brute-probes Workday tenants and board slugs.
Roughly a third of large employers have no public feed at all (Microsoft, Meta,
Alphabet, Apple, Oracle, Starbucks, Costco …) — those arrive through the
aggregator pass, which is why it exists. `config.yaml` lists the known ones.

## Gotchas that have already bitten

- **JobSpy needs pandas**, which has no wheel for Python 3.13+. The image pins
  3.12. On a newer local interpreter every other source still works and the
  aggregator pass reports itself unavailable.
- **A code deploy copies source into the running container; the image goes
  stale.** Anything that recreates a container starts it from the image and
  silently reverts the code. Compare before recreating, and use
  `deploy.sh --env <file> --rebuild` to bake current source in.
- **Check the container's timezone against the host's** before reading log
  timestamps. They are frequently different. Compare file mtimes, not log text,
  when judging whether a run is alive.
- **A container on both a macvlan and a bridge cannot answer off-subnet
  clients.** Its default route goes via the bridge, so replies leave by the
  wrong interface. Compose `priority` does not change this. The fix is to put
  the client on the same subnet, not to route to it.
- **The LinkedIn login fails with a 30s `Page.goto` timeout unless
  `TIMEOUT=120000` is set.** It looks exactly like a network fault and is not.
  See `docs/LINKEDIN.md`.
- **Only the container writes `jobs.db`. Ever.** The data volume is a Docker bind
  mount, and SQLite's WAL mode needs shared-memory locking that does not survive
  the host/VM boundary. A host-side `python3` writing `data/jobs.db` while the
  container had it open produced `database disk image is malformed` on the next
  access, and the file had to be restored from a snapshot. Host-side tools talk
  HTTP (`/api/scores`, `/api/dispositions`) or run via `docker exec`. Treat
  host-side reads of `jobs.db` as suspect too -- prefer `docker exec`, and note
  that a plain `cp` of a WAL database silently omits the `-wal` file, so a
  snapshot taken that way can be missing recent writes.
- **A published artifact cannot reach this board.** Artifacts run under a CSP that
  blocks every fetch/XHR to every host, silently -- `http://localhost:8377` included.
  The Five Before Noon page therefore keeps its applied/passed marks in its own
  artifact database, and `tools/sync_apply_state.py` pulls them in through
  `/api/dispositions`. Do not try to make the page POST to the API; it fails with
  no error.
- **JobSpy uses its own HTTP session**, so `sources/http.py` pacing does not
  apply to it. Throttle it through its own arguments in `config.yaml` instead.
