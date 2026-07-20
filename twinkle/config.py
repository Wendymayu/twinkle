"""Runtime configuration, read from environment with sensible defaults.

Mirrors jiuwenclaw's two-process port layout:
- AgentServer (ws server, heavy execution core): 18000
- Gateway (ws client to agentserver + browser ws server): 19000

Config sources (first match wins): real environment variables, then a
``.env`` file at the repo root (gitignored — for local secrets like the
LLM API key). No python-dotenv dependency; a minimal hand-rolled parser.
"""
import os
from pathlib import Path


def _load_env_file() -> None:
    """Populate os.environ from a .env file at the repo root.

    Real environment variables always win (os.environ.setdefault), so
    ``.env`` is a convenience default, not an override.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
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


_load_env_file()

AGENTSERVER_HOST = os.getenv("TWINKLE_AGENTSERVER_HOST", "127.0.0.1")
AGENTSERVER_PORT = int(os.getenv("TWINKLE_AGENTSERVER_PORT", "18000"))

GATEWAY_HOST = os.getenv("TWINKLE_GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.getenv("TWINKLE_GATEWAY_PORT", "19000"))

# --- LLM (OpenAI-compatible) ---
# Point at any OpenAI-compatible endpoint by overriding these env vars
# (or by setting them in .env at the repo root).
LLM_BASE_URL = os.getenv("TWINKLE_LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.getenv("TWINKLE_LLM_API_KEY", "")
LLM_MODEL = os.getenv("TWINKLE_LLM_MODEL", "gpt-4o-mini")
