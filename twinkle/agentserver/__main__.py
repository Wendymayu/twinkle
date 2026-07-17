"""Entry point: `python -m twinkle.agentserver`."""
import asyncio
import logging

from twinkle.agentserver.server import main

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(main())
