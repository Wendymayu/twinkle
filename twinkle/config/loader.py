"""Config loader: read YAML -> parse -> resolve ${ENV:-default} on values -> validate.

Parse-first (not text-first): yaml.safe_load the raw text, then walk the parsed
data resolving ${ENV:-default} in string values. This mirrors jiuwenswarm's
common/config.py and avoids a text-first footgun where an unquoted empty-default
`dir: ${VAR:-}` resolving to `dir: ` is parsed by YAML as null (failing pydantic
str fields). Under parse-first, an unquoted `dir: ${VAR:-}` parses as the plain
scalar string "${VAR:-}" and resolves to "" — no null, no quoting required.

${VAR:-default} semantics: a non-empty real env var wins; an empty/unset env var
falls back to the default; ${VAR} with no default and no env yields "".

_load_env_file() populates os.environ from a .env at the repo root FIRST (real env
still wins via setdefault), so ${TWINKLE_LLM_API_KEY} resolves from .env.

Path notes: this module lives at twinkle/config/loader.py, so
  Path(__file__).parent.parent       -> twinkle/   (where resources/config.yaml lives)
  Path(__file__).parent.parent.parent -> repo root  (where .env lives)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .schema import TwinkleConfig

# Config YAML ships as a package data file under twinkle/resources/ (one level
# above this package).
CONFIG_YAML_PATH = Path(__file__).resolve().parent.parent / "resources" / "config.yaml"

_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _load_env_file() -> None:
    """Populate os.environ from a .env at the repo root.

    Real env wins (setdefault), so .env is a convenience default, not an override.
    Mirrors the original twinkle/config.py parser verbatim.
    """
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _resolve_env_vars(text: str) -> str:
    """Replace ${VAR:-default} / ${VAR} using os.environ (empty env -> default)."""

    def _replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        val = os.environ.get(name)
        if val:  # non-empty real env wins; empty falls through
            return val
        return default if default is not None else ""

    return _ENV_RE.sub(_replace, text)


def _resolve_env_vars_in_data(obj):
    """Recursively resolve ${ENV:-default} in every string value of a parsed YAML
    structure (dict/list/scalar). Non-string leaves are returned unchanged. Dict
    keys are not resolved (env vars in keys is not a use case here)."""
    if isinstance(obj, str):
        return _resolve_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars_in_data(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars_in_data(v) for v in obj]
    return obj


def load_config(config_path: str | Path | None = None) -> TwinkleConfig:
    """Read the YAML at config_path (default: packaged resources/config.yaml),
    parse it, resolve ${ENV:-default} in string values, and validate into
    TwinkleConfig. Raises on missing file or invalid config (bad tier/mode ->
    pydantic ValidationError)."""
    _load_env_file()  # so ${TWINKLE_LLM_API_KEY} etc. resolve from .env
    path = Path(config_path) if config_path else CONFIG_YAML_PATH
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    data = _resolve_env_vars_in_data(data)
    return TwinkleConfig(**data)
