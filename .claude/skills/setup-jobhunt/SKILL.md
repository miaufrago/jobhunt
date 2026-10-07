---
name: setup-jobhunt
description: Interview a new user about their job search and stand up their jobhunt board — reads their resume, writes data/config.yaml and data/profile/resume.md, starts Docker, and opens the board. Use on a fresh clone, or to retarget an existing board at a different career.
---

# Stand up a jobhunt board for the person in front of you

You are setting up a personal job board. The code is generic; **everything that
makes it useful is the profile you are about to write.** A default profile
produces a board full of noise, so the interview is the product, not paperwork
before it.

Work through the phases in order. Do not skip the read in phase 1 — asking
someone to recite their own resume is the fastest way to lose them.

---

## Phase 0 — check the ground

```sh
docker --version && docker compose version
```

If Docker is missing or not running, say so plainly and stop: they need Docker
Desktop running before anything else works. Everything after this assumes the
repo root is the working directory.

Check whether `data/config.yaml` already exists. If it does, this is a retarget,
not a first run: read it, tell them what it currently targets in one sentence,
and ask whether to adjust it or start fresh. Never overwrite it silently.

---

## Phase 1 — read the resume first, ask second

Ask for the resume and **read it before asking anything else**:

> Point me at your resume and I'll read it first — a file path is easiest, and
> PDF, Word, Markdown or plain text are all fine. Or paste it here.

Use `Read` for pdf/md/txt. For `.docx`, try
`python3 -c "import docx" 2>/dev/null` and if unavailable just ask them to paste
the text — do not send them off to install things.

Then **play back what you learned in three or four sentences** and ask them to
correct it. Something like: *"So — about nine years in supply chain, last four at
a 3PL, most recently running carrier integrations for a team of six. Strongest
evidence is in vendor negotiation and network design. Right so far?"*

This does three jobs at once: it proves you read it, it surfaces anything the
resume undersells, and it makes the questions that follow specific.

---

## Phase 2 — interview

Ask in **small batches, two or three at a time**, not as a form. Follow what
they say. Everything below needs an answer eventually, but the order should
follow the conversation.

**Titles and level.** What titles would they actually take? Which ones look
right but they would turn down? Are they targeting sideways, up, or a deliberate
step down? Map their answer onto the 0–9 scale:

| | | | |
|---|---|---|---|
| 0 entry | 1 associate | 2 mid | 3 principal IC |
| 4 manager | 5 senior manager | 6 director | 7 senior director |
| 8 VP | 9 chief | | |

**Geography.** Where would they physically work? Is remote required, preferred,
or irrelevant? Any metro they would relocate to for the right role — that becomes
`secondary_regions`, shown as a separate tab rather than mixed in. Any place they
would explicitly refuse — worth knowing, it is a common source of wasted reading.

**Money.** Two numbers: the floor (below which they would decline) and the
target (full marks). Ask whether the floor is hard. If it is, `floor_caps_verdict`
stays true and an under-floor posting can never be a strong match.

**Domain phrases — the biggest lever, so spend real time here.** You want the
vocabulary that appears in postings they would want and is *absent* from postings
they would not. Draw candidates from the resume, propose them, and let the person
cut and correct:

> From your resume I'd weight "carrier integration", "network design", "freight
> procurement" and "warehouse operations" heavily. Which of those actually
> describe work you want more of, and what am I missing?

Push back on anything generic. "Leadership", "communication" and "stakeholder
management" appear in every posting ever written and score nothing but noise.

**Disqualifiers.** What makes them close the tab immediately? Commission-only,
on-site five days, a named employer, an industry they are leaving.

**Lagniappe — ask this one, it does real work.** *"Anything else that matters?
Company size, mission, culture, a non-negotiable, something you're trying to get
away from?"* Answers here become `industry_preference`, extra disqualifiers, or
simply context. It is also the question that most often changes the shape of the
whole profile.

**Years, for the hard-requirements gate.** How many years in the target
discipline? This drives `hard_requirements.years_experience`. Be careful and
honest: it is years in the discipline they are targeting, not total working
years. When a career change makes that ambiguous, say so and use the lower
number — the gate is lenient by design and a low value is the safe direction.

---

## Phase 3 — write the files

Write two files. Both live on the mounted data volume and neither is tracked by
git.

**`data/profile/resume.md`** — their resume as Markdown. Convert if needed.
Keep it complete; the scorer reads the whole thing.

**`data/config.yaml`** — start from `config.example.yaml`, which documents every
key and has the correct shape for each. Watch these four, since each has a shape
that is easy to get wrong and fails loudly only at scoring time:

```yaml
domains:                      # MAPPING label -> {weight, phrases}, not a list
  carrier & transport:
    weight: 9
    phrases: ["carrier integration", "freight procurement", "tms"]

secondary_regions:            # MAPPING name -> {markers}. Plain strings, NOT regexes
  Bay Area:
    markers: ["san francisco", "palo alto", "bay area"]

extra_disqualifiers:          # LIST of {cost, label, phrases}
  - cost: 25
    label: "commission-only compensation"
    phrases: ["commission only", "100% commission"]

target_levels: [2, 3, 4]      # heuristic scorer reads this
target_level_range: [2, 4]    # model rubric reads this -- set BOTH, same intent
```

Set `search_terms` from the titles they gave — **six to fourteen**. Each term is
a separate paged search against every source, and run time scales with the count;
One board went from 10 to 14 terms and a "quick" poll became 21 minutes.

Set `hard_requirements.home_markers` to their metros, lowercase. Leave `remote`
out of that list deliberately — the remote case is handled in code.

Leave `employers` short. Two or three they actually care about beats forty they
do not. Adding more later is one line each, verified with
`docker exec jobhunt python -m jobhunt.cli verify-employers`.

Then **show them the profile in plain language** — not the YAML. "Here's what
I'd search for, where, at what level, with what floor." Get a correction before
starting anything.

---

## Phase 4 — start it

```sh
cp .env.example .env          # only if .env does not exist
docker compose up -d          # first run builds the image, a few minutes
```

Wait for health, then confirm the config actually parses — a malformed profile
produces a board that loads and scores nothing, which is a confusing failure:

```sh
docker exec jobhunt python -m jobhunt.cli stats
```

Then collect. Use a poll first, not a full sweep: it is faster and proves the
whole path end to end before anyone waits an hour.

```sh
docker exec jobhunt python -m jobhunt.cli run --kind poll
```

Open `http://localhost:8377`.

**Then read the first results together.** This is the most valuable part of the
whole setup and the easiest to skip. Pull up the top ten and ask which ones are
wrong. Every wrong one is a fixable profile bug:

| What they say | What to change |
|---|---|
| "Too junior" / "too senior" | `target_levels` **and** `target_level_range` |
| "Wrong kind of company" | `domains` phrases, or `industry_preference` |
| "I'd never work there" | `extra_disqualifiers`, or mute the company on `/mutes` |
| "This pays nothing" | `pay.floor` |
| "Not actually remote" | Expected — `hard_requirements` catches many, not all |
| Too few results | Loosen `include_title_patterns`, add `search_terms` |
| Too much noise | Tighten `domains`, raise `min_fit_to_show` |

Re-score in place after any profile edit — no need to re-collect:

```sh
docker exec jobhunt python -m jobhunt.cli score --force
```

Expect to go round this loop two or three times. Say so up front, so the first
imperfect board reads as normal rather than as failure.

---

## Phase 5 — LinkedIn, only if they want it

**Optional, and worth being straight about the trade.** LinkedIn is the single
best source — on one real board it produced 9 of 10 strong matches while 18
employer ATS feeds produced 1. It also means a real authenticated session in a
container, stored unencrypted on disk, and automation against an account that
can be restricted.

If they want it, walk `docs/LINKEDIN.md` with them. The short version:

```sh
docker compose --profile linkedin up -d
docker exec -it linkedin-mcp /app/.venv/bin/python -m linkedin_mcp_server --login
```

Then set `sources.linkedin_mcp.enabled: true` in `data/config.yaml` and restart
the board container.

Four things that have already bitten someone:

- **`TIMEOUT: "120000"` is not optional.** Without it the login dies on a 30s
  `Page.goto` timeout that looks exactly like a network fault. It is already set
  in `docker-compose.yml`; do not remove it.
- **No port is published for this container, on purpose.** It has no
  authentication. Publishing one would expose a logged-in LinkedIn session to
  the whole LAN.
- **Never raise the rate limits** in `sources.linkedin_mcp` to catch up after a
  missed run. Cadence is what distinguishes automation. The account matters more
  than the coverage.
- **Only `search_jobs`, `get_job_details` and `get_saved_jobs` are ever called.**
  Never the messaging, connection or people-search tools.

Its Chromium grows to well over a gigabyte and holds it. A daily
`docker restart linkedin-mcp` returns it to about 90 MB.

---

## Phase 6 — hand over

Leave them with:

- `http://localhost:8377` — the board. `/sources` shows source health, `/mutes`
  shows dismissals.
- `docker compose up -d` / `down` — start and stop.
- Where the schedule lives (`schedule` in `data/config.yaml`) and that the
  container's `TZ` in `docker-compose.yml` has to match their timezone for the
  cron to mean what they think.
- That their resume and profile are editable from the Settings page.
- That assessments beyond the free heuristic come from running the `score-jobs`
  skill against `/api/queue` — no API key needed.

**Tell them the heuristic is a filter, not a ranking.** Measured against 212
hand-read assessments on one board, its correlation with real fit was **0.096**
— roles read as weak scored *higher* on average than roles read as possible. It
is good at throwing out the obviously wrong, which is most of the corpus, and
close to useless at ordering what survives. Read by lane, not from the top down.
