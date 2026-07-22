"""Concrete tool implementations, grouped separately from the framework layer.

Holds the leaf tools (web / shell / todo). The framework — ``Tool`` /
``ToolCard`` / ``LocalFunction`` / ``@tool`` / ``ToolManager`` — stays at
the parent :mod:`twinkle.agentserver.tools` level. Add a new tool as a
``*_tools.py`` module in here, then register it in
:func:`twinkle.agentserver.tools.tool_manager`.

Mirrors openjiuwen's split of ``core/foundation/tool/`` (the engine) from
the app's per-domain tool files, but deliberately does NOT adopt
jiuwenswarm's ``@harness_element`` catalog + provider indirection —
registration stays a single ``ToolManager.register()`` hop.

This package re-exports nothing on purpose: tool singletons stay
module-attribute access (``web_fetch.web_fetch``) so tests can monkeypatch
internal helpers — same convention as before the move; only the import
path changed.
"""
