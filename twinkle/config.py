"""Runtime configuration, read from environment with sensible defaults.

Mirrors jiuwenclaw's two-process port layout:
- AgentServer (ws server, heavy execution core): 18000
- Gateway (ws client to agentserver + browser ws server): 19000
"""
import os

AGENTSERVER_HOST = os.getenv("TWINKLE_AGENTSERVER_HOST", "127.0.0.1")
AGENTSERVER_PORT = int(os.getenv("TWINKLE_AGENTSERVER_PORT", "18000"))

GATEWAY_HOST = os.getenv("TWINKLE_GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.getenv("TWINKLE_GATEWAY_PORT", "19000"))
