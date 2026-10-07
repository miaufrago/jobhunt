"""Print a queue batch for assessment: the parts of a posting that decide fit.

A full description is mostly boilerplate. What matters is the requirements, the
level, the location and the pay, so this trims to the qualification sections and
keeps a little of the opening for context.
"""
import json
import re
import sys

CUES = re.compile(
    r"(what you'?ll bring|what we'?re looking for|qualifications|requirements|"
    r"required skills|you'?ll need|must[- ]have|basic qualifications|"
    r"minimum qualifications|about you|you own this if|we'?re looking for people|"
    r"preferred|experience:|you might thrive)", re.I)


def excerpt(desc: str, head: int = 900, tail: int = 1700) -> str:
    desc = re.sub(r"\n{2,}", "\n", desc or "")
    m = CUES.search(desc)
    out = desc[:head]
    if m and m.start() > head:
        out += "\n  [...]\n" + desc[m.start():m.start() + tail]
    elif len(desc) > head:
        out += desc[head:head + tail]
    return out


def main(path: str, start: int = 0, count: int = 25) -> None:
    d = json.load(open(path, encoding="utf8"))
    jobs = d["jobs"][start:start + count]
    print(f"# batch {start}-{start + len(jobs)} of {d['count']} queued "
          f"on {d['instance']}")
    for j in jobs:
        print("=" * 96)
        print(f"[{j['id']}] {j['title']} | {j['company']} | "
              f"{j.get('location')} | {j.get('pay') or 'no pay stated'} | "
              f"heuristic {j['heuristic']['fit_score']}")
        print(excerpt(j.get("description") or ""))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0,
         int(sys.argv[3]) if len(sys.argv) > 3 else 25)
