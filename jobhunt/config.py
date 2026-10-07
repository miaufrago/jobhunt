"""Config loading. Path is overridable so the container can mount /data/config.yaml."""
import os
import pathlib
import shutil
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = pathlib.Path(os.environ.get("JOBHUNT_DATA", ROOT / "data"))
CONFIG_PATH = pathlib.Path(os.environ.get("JOBHUNT_CONFIG", ROOT / "config.yaml"))

# A real profile and a real resume are runtime inputs, never tracked in git.
# On a fresh checkout neither exists, so seed both from the shipped templates
# rather than failing. The container entrypoint does the same for /data.
CONFIG_EXAMPLE = ROOT / "config.example.yaml"
RESUME_EXAMPLE = ROOT / "profile" / "resume.example.md"

_cache = {"mtime": None, "value": None}


def _seed(target: pathlib.Path, template: pathlib.Path) -> pathlib.Path:
    """Copy `template` to `target` once, if `target` is missing."""
    if not target.exists() and template.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, target)
    return target


def load(force: bool = False) -> dict:
    p = _seed(CONFIG_PATH, CONFIG_EXAMPLE)
    mtime = p.stat().st_mtime
    if force or _cache["mtime"] != mtime:
        _cache["value"] = yaml.safe_load(p.read_text(encoding="utf8"))
        _cache["mtime"] = mtime
    return _cache["value"]


def save(cfg_text: str) -> dict:
    """Validate then persist raw YAML text (used by the Settings editor)."""
    parsed = yaml.safe_load(cfg_text)
    if not isinstance(parsed, dict) or "profile" not in parsed:
        raise ValueError("config must be a mapping with a 'profile' key")
    CONFIG_PATH.write_text(cfg_text, encoding="utf8")
    return load(force=True)


def resume_text() -> str:
    cfg = load()
    p = pathlib.Path(cfg["profile"]["resume_path"])
    if not p.is_absolute():
        p = ROOT / p
    return _seed(p, RESUME_EXAMPLE).read_text(encoding="utf8")


def db_path() -> pathlib.Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "jobs.db"
