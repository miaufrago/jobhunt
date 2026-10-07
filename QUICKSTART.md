# Quickstart

A personal job board: it pulls postings from aggregators, Dice, LinkedIn and
employers' own job feeds, merges the duplicates, extracts the pay, and scores
everything against your resume using a rubric you control.

Runs entirely on your machine. Nothing is uploaded anywhere.

---

## The short version

You need **Docker Desktop**, running.

### With Claude Code (recommended)

```sh
cd jobhunt
claude
```

then:

```
/setup-jobhunt
```

It reads your resume, interviews you about what you actually want, writes your
profile, starts the board, and reads the first results with you. Ten to fifteen
minutes, most of it talking.

### Without Claude Code

```sh
cd jobhunt
cp .env.example .env
cp config.example.yaml data/config.yaml
cp profile/resume.example.md data/profile/resume.md
```

Now edit those two files — **this is the whole job**, and the board is worthless
until you do. `data/config.yaml` is heavily commented; `profile` is the part that
matters. Then:

```sh
docker compose up -d
docker exec jobhunt python -m jobhunt.cli run --kind poll
open http://localhost:8377
```

---

## What you actually have to decide

Everything personal lives in `data/config.yaml`. The code is generic.

| Setting | What it does |
|---|---|
| `domains` | **The biggest lever.** Weighted phrases evidencing the work you want. Generic words ("leadership", "stakeholder") score noise — be specific to your field. |
| `include_title_patterns` | Coarse gate. A posting is scored only if the title matches. |
| `target_levels` / `target_level_range` | Seniority, 0–9. Set **both**, to the same intent. |
| `pay.floor` / `target_base` | The floor is a gate: under it can never be a strong match. |
| `locations` + `hard_requirements.home_markers` | Where you would actually work. |
| `search_terms` | What gets searched. **Six to fourteen** — run time scales with the count. |
| `employers` | Employers whose own job feed gets polled. Start with two or three. |

After any edit, re-score without re-collecting:

```sh
docker exec jobhunt python -m jobhunt.cli score --force
```

---

## Expect to tune it two or three times

The first board is always somewhat wrong. That is normal, not failure. Look at
the top ten results and work backwards:

- Too junior or senior → `target_levels` **and** `target_level_range`
- Wrong kind of company → `domains` phrases
- Pays nothing → `pay.floor`
- Too few results → loosen `include_title_patterns`, add `search_terms`
- Too much noise → tighten `domains`, raise `min_fit_to_show`

---

## Two honest warnings

**The score is a filter, not a ranking.** Measured against 212 hand-read
assessments on a real board, the heuristic's correlation with actual fit was
**0.096** — postings read as *weak* averaged a *higher* score than postings read
as *possible*. It is genuinely good at discarding the obviously wrong, which is
most of what it sees, and close to useless at ordering what is left. Read across
categories rather than straight down the list.

**A "remote" tag is not evidence.** On one week of real collection, eight of
twenty hand-read postings were location-disqualified and six of those were tagged
remote — "Remote, US" that meant California-only, "(remote)" that meant three
days in an office, an explicit eleven-state allowlist. `hard_requirements` in
`config.yaml` catches many of these before they reach you. It does not catch all
of them. Read the body before you get attached.

---

## Commands

```sh
docker compose up -d                                   # start
docker compose down                                    # stop
docker compose --profile linkedin up -d                # start with LinkedIn

docker exec jobhunt python -m jobhunt.cli run --kind poll   # quick collection
docker exec jobhunt python -m jobhunt.cli run --kind full   # everything, slow
docker exec jobhunt python -m jobhunt.cli score --force     # re-score in place
docker exec jobhunt python -m jobhunt.cli stats             # counts
docker exec jobhunt python -m jobhunt.cli verify-employers  # source health
docker exec jobhunt python -m jobhunt.cli reap              # close dead postings
docker exec jobhunt python -m jobhunt.cli backup            # snapshot the db
```

Board at `http://localhost:8377`. `/sources` for source health, `/mutes` for
what you have dismissed and why.

---

## LinkedIn (optional)

The single best source — but it means an authenticated session living in a
container, stored unencrypted on disk. Your call.

```sh
docker compose --profile linkedin up -d
docker exec -it linkedin-mcp /app/.venv/bin/python -m linkedin_mcp_server --login
```

Then set `sources.linkedin_mcp.enabled: true` in `data/config.yaml` and restart.
Full detail, including what the integration deliberately never calls, is in
`docs/LINKEDIN.md`.

No port is published for that container on purpose: it has no authentication,
and publishing one would expose a logged-in LinkedIn session to your network.

---

## If something breaks

| Symptom | Cause |
|---|---|
| Board loads, zero jobs | Config didn't parse, or nothing collected yet. Run `stats`, then a poll. |
| `AttributeError: 'list' object has no attribute 'items'` | `domains` or `secondary_regions` written as a list. They are mappings — see `config.example.yaml`. |
| Aggregators unavailable | JobSpy needs pandas, which needs Python 3.12. Inside Docker this is already handled. |
| LinkedIn login times out at 30s | `TIMEOUT: "120000"` missing from the linkedin-mcp service. It ships set — don't remove it. |
| LinkedIn returns `421 Misdirected Request` | The MCP server rejects non-loopback Host headers. Handled in `jobhunt/sources/mcp.py`. |
| Scheduled runs fire at odd hours | Container `TZ` in `docker-compose.yml` doesn't match yours. |
