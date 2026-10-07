"""Pluggable fit-assessment backends.

heuristic  -- deterministic, no network, no cost. Always runs; it is the gate.
ollama     -- a local model on the LAN. Deep-scores whatever clears the gate.
anthropic  -- the hosted API. Only if an ANTHROPIC_API_KEY ever exists.

The gate is what keeps a CPU-only box viable: a few hundred postings a day become
a few dozen that the model actually reads.
"""
from .heuristic import heuristic_score, passes_rules  # noqa: F401
