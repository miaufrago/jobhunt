"""Guard against the escape-corruption bug class.

Four times in this project a regex was written through a shell heredoc and the
escape collapsed: `\\b` became a literal backspace (0x08) inside the compiled
pattern. Nothing raised. `US_MARKERS` silently matched "la" inside "Poland" and
passed every foreign posting; `SUBDIVISION_CODE` silently matched nothing.

Both failure modes are invisible in review and in normal use, so they are
checked mechanically instead.

    python -m pytest tests/ -q
"""
import pathlib
import re

PKG = pathlib.Path(__file__).resolve().parent.parent / "jobhunt"

# Control characters that should never appear in source. Tab, newline and
# carriage return are legitimate.
FORBIDDEN = {c for c in range(0x00, 0x20)} - {0x09, 0x0A, 0x0D}


def test_no_control_characters_in_source():
    offenders = []
    for path in sorted(PKG.rglob("*.py")):
        data = path.read_bytes()
        for i, byte in enumerate(data):
            if byte in FORBIDDEN:
                line = data[:i].count(b"\n") + 1
                offenders.append(f"{path.name}:{line} contains 0x{byte:02x}")
    assert not offenders, (
        "Control characters in source -- almost certainly a collapsed regex "
        "escape from a shell heredoc:\n  " + "\n  ".join(offenders))


def test_compiled_patterns_are_clean():
    """Import every module and inspect the patterns it actually compiled.

    Source can look correct while the compiled object is wrong, so this checks
    the runtime objects rather than the text.
    """
    import importlib

    offenders = []
    for path in sorted(PKG.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(PKG.parent).with_suffix("")
        mod = importlib.import_module(str(rel).replace("\\", ".").replace("/", "."))
        for name, obj in vars(mod).items():
            if isinstance(obj, re.Pattern):
                bad = [hex(ord(c)) for c in obj.pattern if ord(c) in FORBIDDEN]
                if bad:
                    offenders.append(f"{mod.__name__}.{name}: {bad}")
    assert not offenders, (
        "Compiled regex contains control characters:\n  " + "\n  ".join(offenders))


def test_us_markers_reject_foreign_subdivisions():
    """The concrete regression: foreign codes must not read as US."""
    from jobhunt.scoring.locations import SUBDIVISION_CODE, US_MARKERS

    for loc in ("Remote, ON", "Remote, BC", "Remote, SG", "Remote, KA"):
        assert SUBDIVISION_CODE.findall(loc), f"no code found in {loc!r}"
        assert not US_MARKERS.search(loc), f"{loc!r} wrongly reads as US"
    for loc in ("Seattle, WA", "Boston, MA", "Remote - USA", "United States"):
        assert US_MARKERS.search(loc), f"{loc!r} should read as US"
