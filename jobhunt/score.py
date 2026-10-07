"""Scoring driver.

Every posting gets a heuristic score -- free, instant, explainable. Whatever clears
the deep_score_threshold then goes to a model backend, if one is reachable.

That ordering is what makes a CPU-only box workable: a few hundred postings a day
become a few dozen a model actually reads.
"""
import json

from . import config, db
from .scoring import heuristic_score, passes_rules
from .scoring import llm


def backend_status(cfg=None) -> tuple[str, str]:
    return llm.backend_status(cfg or config.load())


def score_pending(limit: int = 400, force: bool = False, deep: bool = True,
                  discard_assessments: bool = False) -> dict:
    cfg = config.load()
    resume = config.resume_text()
    sc = cfg["scoring"]
    threshold = sc.get("deep_score_threshold", 55)
    deep_cap = sc.get("deep_score_max_per_run", 40)
    backend, why = llm.backend_status(cfg)

    stats = {"seen": 0, "filtered": 0, "heuristic": 0, "deep": 0, "deep_errors": 0,
             "backend": backend, "backend_detail": why}

    with db.connect() as con:
        if force:
            # `force` re-runs the gate over every posting, assessed ones
            # included. That is deliberate: the gate's prediction for an
            # assessed posting is exactly what calibration measures, so
            # excluding those rows would freeze the measurement.
            #
            # The assessment itself is protected at the write path instead --
            # _save refuses to let a heuristic pass overwrite a `scored` row,
            # and only refreshes its heuristic_score. Excluding them from the
            # query, as this once did, silently made `calibrate` report stale
            # numbers that never moved when the scorer changed.
            path = db.snapshot("preforce")
            stats["snapshot"] = path
            print(f"snapshot before forced rescore: {path}", flush=True)
            rows = con.execute(
                "SELECT * FROM jobs WHERE closed=0 "
                "ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = con.execute(
                """SELECT j.* FROM jobs j LEFT JOIN scores s ON s.job_id = j.id
                   WHERE j.closed=0 AND (s.job_id IS NULL OR s.desc_hash != j.desc_hash)
                   ORDER BY j.first_seen DESC LIMIT ?""", (limit,)).fetchall()

    survivors: list[tuple[dict, dict]] = []
    for row in rows:
        job = dict(row)
        stats["seen"] += 1
        ok, why_not = passes_rules(job, cfg)
        if not ok:
            _save(job["id"], "filtered",
                  {"fit_score": 0, "verdict": "no", "headline": why_not,
                   "rationale": why_not}, "rules", job["desc_hash"],
                  discard=discard_assessments)
            stats["filtered"] += 1
            continue
        result = heuristic_score(job, cfg)
        _save(job["id"], "heuristic", result, "heuristic", job["desc_hash"],
              discard=discard_assessments)
        stats["heuristic"] += 1
        if result["fit_score"] >= threshold:
            survivors.append((job, result))

    if not (deep and backend != "heuristic" and survivors):
        return stats

    survivors.sort(key=lambda pair: pair[1]["fit_score"], reverse=True)
    for job, base in survivors[:deep_cap]:
        try:
            result, model = llm.deep_score(cfg, resume, job)
            result.setdefault("red_flags", [])
            result["red_flags"] = list(dict.fromkeys(
                result["red_flags"] + base.get("red_flags", [])))
            _save(job["id"], "scored", result, model, job["desc_hash"])
            stats["deep"] += 1
        except Exception as exc:
            stats["deep_errors"] += 1
            stats.setdefault("last_error", f"{type(exc).__name__}: {exc}"[:300])
    if len(survivors) > deep_cap:
        stats["deep_deferred"] = len(survivors) - deep_cap
    return stats


def _save(job_id: str, stage: str, r: dict, model: str, desc_hash: str,
          discard: bool = False) -> None:
    """Write one score.

    heuristic_score is kept in its own column and never overwritten by an
    assessment. The gate decides which postings are worth a real read, so its
    accuracy is the thing most worth measuring -- and the assessment used to
    overwrite the only record of what the gate had predicted, which made that
    measurement impossible after the fact.
    """
    is_heur = stage in ("heuristic", "filtered")
    with db.connect() as con:
        if is_heur:
            prior = con.execute("SELECT stage FROM scores WHERE job_id=?",
                                (job_id,)).fetchone()
            if prior is not None and prior["stage"] == "scored" and not discard:
                # Never let an automated pass overwrite a real assessment. A
                # changed description or a newly added exclusion pattern used to
                # replace the assessment with a keyword score, silently -- seven
                # went that way in a single run. Record what the gate now thinks
                # and leave the judgement alone; the stale desc_hash is what
                # re-queues it for a fresh read.
                con.execute(
                    "UPDATE scores SET heuristic_score=?, heuristic_at=? "
                    "WHERE job_id=?",
                    (r.get("fit_score", 0), db.now(), job_id))
                return
        con.execute(
            """INSERT INTO scores (job_id,stage,fit_score,verdict,headline,rationale,
                 strengths,gaps,red_flags,model,desc_hash,scored_at,
                 heuristic_score,heuristic_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET stage=excluded.stage,
                 fit_score=excluded.fit_score, verdict=excluded.verdict,
                 headline=excluded.headline, rationale=excluded.rationale,
                 strengths=excluded.strengths, gaps=excluded.gaps,
                 red_flags=excluded.red_flags, model=excluded.model,
                 desc_hash=excluded.desc_hash, scored_at=excluded.scored_at,
                 heuristic_score=COALESCE(excluded.heuristic_score,
                                          scores.heuristic_score),
                 heuristic_at=COALESCE(excluded.heuristic_at, scores.heuristic_at)""",
            (job_id, stage, r.get("fit_score", 0), r.get("verdict", "no"),
             r.get("headline", ""), r.get("rationale", ""),
             json.dumps(r.get("strengths", [])), json.dumps(r.get("gaps", [])),
             json.dumps(r.get("red_flags", [])), model, desc_hash, db.now(),
             r.get("fit_score", 0) if is_heur else None,
             db.now() if is_heur else None),
        )
        con.execute(
            "INSERT OR IGNORE INTO actions (job_id,status,updated) VALUES (?,'new',?)",
            (job_id, db.now()))


def calibration() -> dict:
    """How well the heuristic gate predicts what a real assessment concludes.

    The gate chooses which postings are worth a session's attention, so its
    agreement with the assessments is the one number that says whether that
    attention is being spent well. Rows scored before heuristic_score existed
    have their heuristic recomputed rather than being dropped.
    """
    cfg = config.load()
    with db.connect() as con:
        rows = con.execute(
            """SELECT j.*, s.fit_score AS assessed, s.verdict AS assessed_verdict,
                      s.heuristic_score AS predicted
               FROM jobs j JOIN scores s ON s.job_id=j.id
               WHERE s.stage='scored'""").fetchall()
    pairs, by_verdict = [], {}
    for r in rows:
        job = dict(r)
        pred = job["predicted"]
        if pred is None:
            ok, _ = passes_rules(job, cfg)
            pred = heuristic_score(job, cfg)["fit_score"] if ok else 0
        pairs.append((pred, job["assessed"]))
        by_verdict.setdefault(job["assessed_verdict"], []).append(pred)
    n = len(pairs)
    if n < 2:
        return {"n": n, "detail": "not enough assessments to calibrate"}
    mp = sum(p for p, _ in pairs) / n
    ma = sum(a for _, a in pairs) / n
    sp = (sum((p - mp) ** 2 for p, _ in pairs) / n) ** 0.5
    sa = (sum((a - ma) ** 2 for _, a in pairs) / n) ** 0.5
    cov = sum((p - mp) * (a - ma) for p, a in pairs) / n
    thr = cfg["scoring"].get("deep_score_threshold", 55)
    good = [(p, a) for (p, a), r in zip(pairs, rows)
            if r["assessed_verdict"] in ("strong", "possible")]
    return {
        "n": n,
        "correlation": round(cov / (sp * sa), 3) if sp and sa else None,
        "heuristic_mean": round(mp, 1),
        "assessed_mean": round(ma, 1),
        "optimism": round(mp - ma, 1),
        "threshold": thr,
        "good_roles": len(good),
        "good_roles_below_threshold": sum(1 for p, _ in good if p < thr),
        "heuristic_mean_by_verdict": {
            v: round(sum(x) / len(x), 1) for v, x in sorted(by_verdict.items()) if x},
    }
