"""sessions 包入口 — re-exports + session_store() builder。"""
from twinkle.agentserver.sessions.store import SessionStore
from twinkle.agentserver.sessions.handlers import dispatch_session_rpc, handles as handles_session_rpc


def session_store() -> SessionStore:
    """从 config 构造一个 SessionStore(生产装配用)。"""
    from twinkle.config import SESSIONS_DIR
    return SessionStore(SESSIONS_DIR)


__all__ = [
    "SessionStore", "session_store",
    "dispatch_session_rpc", "handles_session_rpc",
]
