"""Entry point: `python -m twinkle.agentserver`."""
import asyncio
import logging

from twinkle.agentserver.server import main
from twinkle.config import ensure_workspace_dir

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    import twinkle.observability
    twinkle.observability.setup()
    ensure_workspace_dir()
    asyncio.run(main())
