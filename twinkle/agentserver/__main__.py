"""Entry point: `python -m twinkle.agentserver`."""
import asyncio

from twinkle.agentserver.server import main
from twinkle.config import ensure_workspace_dir
from twinkle.logging_config import setup_logging

if __name__ == "__main__":
    setup_logging("agentserver")
    import twinkle.observability
    twinkle.observability.setup()
    ensure_workspace_dir()
    asyncio.run(main())
