---
name: score-jobs
description: Assess queued job postings from the jobhunt board against the profile resume and write the results back. Use when asked to "score the job queue", "assess new jobs", "rate the postings", or "run the job assessment".
---

# Score the jobhunt queue

The jobhunt container collects and heuristically ranks postings continuously. It
cannot start a session, so it publishes a queue instead. You pull the queue,
assess each posting, and post the results back.

Base URL: `$JOBHUNT_URL`. There is no default -- the address is specific to your
deployment. Set it in the environment, or ask which board to score if it is unset
and more than one is running.
If `APP_PASSWORD` or `API_TOKEN` is set on the container, pass it as `?token=`.

## Steps

1. **Pull the queue.**

   ```bash
   curl -s "$JOBHUNT_URL/api/queue?limit=25" > /tmp/queue.json
   python -c "import json;d=json.load(open('/tmp/queue.json'));print(d['count'],'jobs')"
   ```

   The response carries the resume, the rubric, and for each job: id, title,
   company, location, url, sources, the full description, and what the heuristic
   pass already concluded.

   If `count` is 0, say so and stop. Do not lower the threshold without being asked.

2. **Assess each posting yourself.** Read the resume from the response — do not
   assume you remember it. For each job produce:

   | Field | Meaning |
   |---|---|
   | `fit_score` | 0-100 against the rubric in the response |
   | `verdict` | `strong` (>=72), `possible` (>=55), `weak` (>=35), `no` |
   | `headline` | Under 12 words. The one reason it is or is not worth the time. |
   | `rationale` | Two or three sentences. How the score breaks down. |
   | `strengths` | Quote the resume. What actually evidences this role. |
   | `gaps` | Quote the posting. Stated must-haves the resume cannot evidence. |
   | `red_flags` | Reasons to decline: downlevel, staffing-agency listing, contract, clearance, on-site mandate. |

   Be strict. Most postings are not a good fit. Judge the posting as written and
   do not assume unstated seniority. Carry forward any red flag the heuristic pass
   already found unless the full text disproves it.

3. **Post the results back.**

   ```bash
   curl -s -X POST "$JOBHUNT_URL/api/scores" \
     -H 'Content-Type: application/json' -d @/tmp/scores.json
   ```

   The body is a JSON array of objects with `job_id` plus the fields above. The
   response reports how many were written.

4. **Report to the user**: how many you assessed, and the `strong` ones by name
   with their headline and board link (`$JOBHUNT_URL/job/<id>`). Do not paste the
   full assessments — they are on the board.

## Notes

- Assessments overwrite the heuristic score for that posting and are marked
  `model: claude-code` on the job page.
- A posting is re-queued only when its description changes on the source site.
- If the container is unreachable, say so plainly. Do not score from memory.
