"""Model-backed deep scoring.

Two backends, one prompt:
  ollama     -- any OpenAI-compatible or native Ollama endpoint on the LAN.
  anthropic  -- the hosted API, used only if a key is present.

Both are optional. When neither is configured the heuristic score stands on its own.
"""
import json
import os

import requests

def rubric_for(cfg: dict) -> str:
    """The rubric follows the same profile the heuristic scorer uses, so a model
    assessment and the deterministic one cannot disagree about what matters."""
    from .heuristic import _pay_policy, _weights

    prof = cfg.get("profile") or {}
    w = _weights(cfg)
    pay = _pay_policy(cfg)
    domains = ", ".join((prof.get("domains") or {}).keys()) or "the candidate's charters"
    lo, hi = prof.get("target_level_range", [6, 9])
    locations = ", ".join(prof.get("locations") or []) or "anywhere"

    # Role shape is scored by the heuristic as a nudge; state it here too so the
    # two stages cannot disagree about it. Seniority alone does not distinguish
    # a principal who sets direction from one who writes the policy engine.
    shape = prof.get("role_shape") or {}
    shape_rule = ""
    if shape.get("depth_markers"):
        shape_rule = (
            "\n- Role shape matters as much as level. An individual-contributor "
            "seat is acceptable; a deep specialist seat usually is not. If the "
            "posting's requirements centre on sustained hands-on depth in a "
            "named technology or discipline the resume cannot evidence, treat "
            "that as a significant gap and say so plainly, whatever the title "
            "says. A role that carries people leadership is preferred, and "
            "worth a few points, but its absence alone is not a mark against "
            "the posting.")

    def money(v):
        return f"${v:,.0f}" if v else "unset"

    return f"""You assess fit between one candidate and one job posting.

Score 0-100 against these weights. Be strict; most postings are not a good fit.
  {w['level']:.0f}  Level and scope. Target band is roughly {lo}-{hi} on a 0-9 scale
      (6 director, 7 senior director, 8 VP, 9 chief). Over-levelled loses some
      points; under-levelled loses more.
  {w['domain']:.0f}  Domain match against the candidate's real charters, not adjacent
      buzzwords. The domains that matter: {domains}.
  {w['pay']:.0f}  Compensation. Target base {money(pay.get('target_base'))} earns full
      marks. A stated range topping out below {money(pay.get('floor'))} scores zero
      here and CANNOT be a strong match, however well the role fits otherwise --
      say so in red_flags. A posting that states no range is neutral, not penalized.
  {w['location']:.0f}  Location and practicalities. Target areas: {locations}.
  {w['listing']:.0f}  A clean listing: a direct employer posting, not a staffing agency
      or a contract.

Rules:
- Judge the posting as written. Do not assume unstated seniority or unstated pay.
- A stated must-have the resume cannot evidence is a gap, even if learnable.{shape_rule}
- Quote the resume in strengths. Quote the posting in gaps.
- headline: under 12 words, the one reason this is or is not worth the time.
Reply with JSON only, no prose around it."""


SCHEMA = {
    "type": "object",
    "properties": {
        "fit_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": ["strong", "possible", "weak", "no"]},
        "headline": {"type": "string"},
        "rationale": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["fit_score", "verdict", "headline", "rationale",
                 "strengths", "gaps", "red_flags"],
    "additionalProperties": False,
}


def posting_block(job: dict, limit: int = 12000) -> str:
    return (f"COMPANY: {job.get('company')}\n"
            f"TITLE: {job.get('title')}\n"
            f"LOCATION: {job.get('location') or 'unstated'}\n"
            f"SOURCE: {job.get('best_source') or job.get('source')}\n\n"
            f"--- POSTING ---\n{(job.get('description') or '')[:limit]}")


def _system(resume: str, cfg: dict) -> str:
    """Rubric plus resume, both derived from the profile.

    This previously referenced a module-level RUBRIC constant that no longer
    exists, and took one argument while both call sites passed two. Either
    fault alone made every deep-score call raise, and score_pending swallows
    the exception into a counter -- so both model backends failed silently.
    """
    return (f"{rubric_for(cfg)}\n\n--- CANDIDATE RESUME ---\n"
            f"{resume}\n--- END RESUME ---")


# ---------------------------------------------------------------- ollama

def ollama_available(cfg: dict) -> tuple[bool, str]:
    url = cfg["scoring"].get("ollama", {}).get("base_url", "")
    if not url:
        return False, "no scoring.ollama.base_url configured"
    try:
        r = requests.get(f"{url.rstrip('/')}/api/tags", timeout=8)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        want = cfg["scoring"]["ollama"].get("model", "")
        if want and not any(m.split(":")[0] == want.split(":")[0] for m in models):
            return False, f"model '{want}' not pulled; host has {models}"
        return True, f"ok, models: {models}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]


def score_ollama(cfg: dict, resume: str, job: dict) -> dict:
    o = cfg["scoring"]["ollama"]
    body = {
        "model": o["model"],
        "messages": [
            {"role": "system", "content": _system(resume, cfg)},
            {"role": "user", "content": posting_block(job, o.get("jd_char_limit", 9000))},
        ],
        "stream": False,
        "format": SCHEMA,
        "keep_alive": o.get("keep_alive", "30m"),
        "options": {
            "temperature": 0,
            "num_ctx": o.get("num_ctx", 8192),
            "num_predict": 900,
        },
    }
    r = requests.post(f"{o['base_url'].rstrip('/')}/api/chat", json=body,
                      timeout=o.get("timeout", 900))
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


# ---------------------------------------------------------------- anthropic

def anthropic_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def score_anthropic(cfg: dict, resume: str, job: dict) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=cfg["scoring"].get("anthropic_model", "claude-opus-5"),
        max_tokens=4000,
        system=[{"type": "text", "text": _system(resume, cfg),
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": posting_block(job)}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA},
                       "effort": cfg["scoring"].get("effort", "high")},
        thinking={"type": "adaptive"},
    )
    return json.loads(next(b.text for b in msg.content if b.type == "text"))


# ---------------------------------------------------------------- dispatch

def backend_status(cfg: dict) -> tuple[str, str]:
    """Which deep-score backend will actually be used, and why."""
    want = cfg["scoring"].get("backend", "auto")
    if want in ("heuristic", "none"):
        return "heuristic", "deep scoring disabled in config"
    if want == "queue":
        # Deliberate: assessments come from a session pulling /api/queue. Probing
        # a model backend here only produced a connection error in every log line.
        return "heuristic", "stage 2 served by the pull queue (/api/queue)"
    if want in ("anthropic", "auto") and anthropic_available():
        return "anthropic", "ANTHROPIC_API_KEY present"
    ok, detail = ollama_available(cfg)
    if want in ("ollama", "auto") and ok:
        return "ollama", detail
    return "heuristic", f"no model backend reachable ({detail})"


def deep_score(cfg: dict, resume: str, job: dict) -> tuple[dict, str]:
    backend, _ = backend_status(cfg)
    if backend == "anthropic":
        return score_anthropic(cfg, resume, job), cfg["scoring"].get(
            "anthropic_model", "claude-opus-5")
    if backend == "ollama":
        return score_ollama(cfg, resume, job), cfg["scoring"]["ollama"]["model"]
    raise RuntimeError("no deep-score backend available")
