# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Twinkle is a **learning-focused reimplementation** of the core agent pipeline of `jiuwenswarm` (reference monorepo at `D:\code\opensource\gitcode\jiuwenswarm`, formerly JiuwenClaw; contains `jiuwenswarm/` swarm framework + `jiuwenclaw/` agent app layer + `jiuwenbox/` deploy). It deliberately mirrors jiuwenswarm's two-process + bidirectional-WebSocket architecture so the two can be compared module-by-module. It is **not** a fork, not a SaaS shell, and not feature-complete — see `roadmap.md` for the current phase and scope (Phase 0–2 landed incl. OTel telemetry; Phase 3 context-compression pending; skill / long-term memory / tool permissions / cron are planned future phases; multi-channel & enterprise features out of scope).

Check `roadmap.md` for the current phase before making architectural changes. The repository README is stale (describes Phase 0 echo); `docs/architecture.md` is the source of truth for the *current* architecture.

## Commands

All `python` commands assume the project venv. From a fresh checkout on Windows:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Run tests (no `pytest-asyncio` dependency — tests use `asyncio.run()` + a `port_factory`/`free_port` fixture in `tests/conftest.py`):

```bash
python -m pytest tests/ -v
python -m pytest tests/test_agent_loop.py -v          # single file
python -m pytest tests/test_tool_manager.py::test_name  # single test
```

Run the two backend processes (each blocks; use separate terminals or the launcher):

```bash
python scripts/start_services.py        # launches both
# or separately:
python -m twinkle.agentserver          # :18000 — execution core
python -m twinkle.gateway              # :19000 — connection edge
```

Run the frontend (Vite proxies `/ws` → `ws://127.0.0.1:19000`):

```bash
cd web && npm install && npm run dev   # http://localhost:5173
```

The LLM needs `TWINKLE_LLM_API_KEY` set in `.env` (copy `.env.example`). Without it the agent loop will fail at model call time; the ws/gateway/e2a plumbing still works.

## Architecture (the big picture)

Two Python processes + a Vue frontend, with **two distinct message formats**:

```
Browser ──ws (req/res/event)──> Gateway (:19000) ──ws (E2A envelope)──> AgentServer (:18000)
        <──event broadcast──            <──E2AResponse stream──               AgentLoop (ReAct)
```

**Gateway is a pure format-translator + stream fanout** — it converts browser `req` → `E2AEnvelope` inbound and `E2AResponse` frames → browser `chat.delta`/`chat.final` events outbound. **AgentServer never sees the browser**; it only consumes `E2AEnvelope` and yields `E2AResponse`, so it is channel-agnostic by construction.

### Gateway's four components (assembled with one-way dependencies, no cycles)

`ChannelManager ──holds──> MessageHandler ──holds──> AgentClient`

- **`WebChannel`** (`gateway/web_channel.py`) — ws server to the browser. Inbound: parse `req`, build `Message`, **immediately ACK** with `{type:res, ok:true}` (does not wait for the agent), then invoke `on_message`. Outbound: `send(msg)` broadcasts `{type:event}` to **all** connected ws clients. `channel_id="web"` is the routing key.
- **`ChannelManager`** (`gateway/channel_manager.py`) — registers channels by `channel_id`; runs a single asyncio `_dispatch_loop` that pulls from `MessageHandler.dequeue_outbound()` and routes each `Message` to the matching channel's `send()`.
- **`MessageHandler`** (`gateway/message_handler.py`) — inbound: `Message` → `E2AEnvelope` → `AgentClient.send_request_stream`. Outbound: translates each `E2AResponse` to a `Message(chat.delta|chat.final)` and pushes it onto its own `_robot_messages` queue (ChannelManager is the consumer). `_process_stream` is fire-and-forget via `asyncio.create_task`.
- **`AgentClient`** (`gateway/agent_client.py`) — ws client to AgentServer. On `connect()`, it first `recv()`s the `connection.ack` handshake frame (a plain event, **not** E2A-shaped) before starting the demux loop. **Demux** is the key mechanism: one ws connection multiplexes many concurrent requests, demultiplexed by `request_id` into per-request `asyncio.Queue`s. `_send_lock` serializes ws writes.

### AgentServer internals

- **`server.py`** — ws handler: send `connection.ack`, parse `E2AEnvelope`, dispatch to `AgentLoop.run_stream`, send each yielded frame back via `_safe_send` (silently swallows `ConnectionClosed`). `ws_handler(loop)` allows tests to inject a fake loop.
- **`agent_loop.py`** — the ReAct core. `run_stream` is an **async generator** yielding `E2AResponse` with zero ws dependency (so it's unit-testable without sockets). Loop: `store.append(user)` → `llm.stream(msgs, tools)` (yields `TextDelta` | `Finish`) → `TextDelta` yields `e2a.chunk`; a `Finish` with `finish_reason=="tool_calls"` executes tools (result appended as `{role:"tool", tool_call_id, content}` then re-queried) and drains `e2a.todo_update`; `finish_reason=="stop"` yields `e2a.complete`. Guarded by `max_steps` (`TWINKLE_AGENT_MAX_STEPS`, default `1000`) → `e2a.error` if it doesn't converge. **Tool-result re-injection is the linchpin** — the result goes back into `SessionStore` so the next `get_messages` carries it. At entry it also sets the plan-todo ContextVar to the envelope's `session_id` and first-inserts a todo-guidance system message (once per session).
- **`llm_client.py`** — thin OpenAI SDK wrapper; `base_url` is configurable so any OpenAI-compatible endpoint works. `stream()` yields `TextDelta | Finish` (`Finish` carries `finish_reason` + `assistant_message` with accumulated `tool_calls`; `finish_reason=="tool_calls"` signals tool execution; also captures token `usage`).
- **`session_store.py`** — in-memory `dict[session_id, list[msg]]` storing raw OpenAI `messages`. No persistence yet; interface allows swapping in SQLite later without rework.
- **`memory.py`** — **stub** long-term memory (`recall()` returns `[]`, `store()` no-ops). Interface shape is pinned so a real impl can drop in.
- **`tools/`** — the four-layer tool system (Phase 2 rewrite). Split into a framework layer at the top level and concrete tools under `builtin/`; to add a tool, drop a `*_tools.py` in `builtin/` and `register` it inside `tool_manager()` in `__init__.py`:
  - `base.py`: `ToolCard` (pure metadata) + `Tool` (Protocol: `card` + `invoke`)
  - `local_function.py`: `LocalFunction`, the local-Python-function implementation of `Tool`
  - `schema_extractor.py`: hand-written extractor (str/int/float/bool/list/dict/Optional/`X | None` PEP 604 → JSON schema) from a function's signature + docstring
  - `decorator.py`: `@tool` turns a plain async function into a `LocalFunction` (auto-derives name/description/params; override with `@tool(name=..., input_params=...)`)
  - `manager.py`: `ToolManager` — `register`/`unregister`/`list`/`get`/`schemas`/`execute`, stores `dict[str, Tool]`, only knows the `Tool` interface
  - `__init__.py`: re-exports the framework (`Tool`/`ToolCard`/`LocalFunction`/`@tool`/`ToolManager`) + the `tool_manager()` builder that pre-registers the `builtin/` tools. Tool singletons stay module-attribute access (e.g. `builtin.web_fetch.web_fetch`) so tests can monkeypatch internal helpers.
  - **`builtin/`** — concrete tool implementations, grouped out of the framework layer (mirrors openjiuwen's `core/foundation/tool/` vs the app's per-domain tool files, minus jiuwenswarm's catalog/provider indirection):
    - `web_fetch.py`, `web_search.py`: concrete read-only tools (URL→markdown; DuckDuckGo Lite search)
    - `command_exec.py`: shell-command execution tool (slim rewrite of jiuwenclaw's `command_tools.py`). Cross-platform shell detection (PowerShell on Windows, bash/sh on Unix), workspace-confined `workdir`, dangerous-command blocklist, timeout, output clipping, and a non-blocking background mode. **Not read-only** — the only safety rails today are the blocklist + workspace confinement; an approval flow is deferred (roadmap `permissions/`).
    - `todo_tools.py`: the three `@tool` todo functions (create/complete/list) for agent self-planning; reads `plan_todo_context` for session routing, operates the module-level `TodoStore` singleton, returns markdown strings with the current list appended.
  - `agent_loop` calls `self._tools.schemas()` / `self._tools.execute(name, args)` — `ToolManager` is a superset of the old call surface.
- **`plan_todo_context.py`** — a `ContextVar` (`PLAN_TODO_SESSION_ID`) set by `AgentLoop.run_stream` at request entry to the envelope's `session_id`, plus a `get_plan_todo_session_id()` getter with a `"default"` fallback. Lets the parameter-less todo tools resolve the current session without threading it through every tool call.
- **`todo_store.py`** — in-memory `TodoStore` (`dict[session_id, list[TodoTask]]` + per-session `asyncio.Lock` serializing read-modify-write). Methods: `create`/`complete`/`list_tasks`. No persistence (matches SessionStore philosophy).
- **`observability/`** — in-tree OTel telemetry (`Phase: landed`). `setup()` (called from `agentserver/__main__`) monkey-patches `AgentLoop.run_stream` / `LLMClient.stream` / `ToolManager.execute` into `twinkle.agent.invoke` / `gen_ai.chat` / `gen_ai.tool` spans + metrics, exported via OTLP gRPC / console / none. `OTEL_ENABLED` defaults false = zero-cost no-op; opt-in via the `[obs]` extra (`opentelemetry-api`/`-sdk`/`-exporter-otlp-proto-grpc`). Dependency is one-way `observability → agentserver`; agentserver never imports it.

### Message formats (the two wires)

- **Browser ↔ Gateway**: `{type:req|res|event, id, method, event, params|payload, request_id}`. Defined in `web/src/services/webClient.ts` + `twinkle/schema/message.py` (`Message` dataclass + `EventType` of `connection.ack`/`chat.delta`/`chat.final`/`todo.update` — the last carries the structured todo snapshot `{tasks, remaining, total}`).
- **Gateway ↔ AgentServer (E2A)**: Pydantic models in `twinkle/e2a/models.py` — `E2AEnvelope` (request, ~6 fields) and `E2AResponse` (streaming multi-frame: `e2a.chunk` / `e2a.complete` / `e2a.error` / `e2a.todo_update` — the last carries a structured todo snapshot `{tasks, remaining, total}` that the gateway maps to a `todo.update` browser event; the rest map to `chat.delta`/`chat.final`, with `sequence` strictly increasing per `request_id`, `is_final` on the last frame).

The system is **streaming-only** — unary/single-shot mode was removed in Phase 1. There is no `is_stream` field on `E2AEnvelope`; all requests are implicitly streaming.

**`request_id` is the load-bearing identifier** — the browser generates it, it threads through `req.id` → `Message.id` → `E2AEnvelope.request_id` → `E2AResponse.request_id` → outbound `event.request_id`, and the browser uses it to associate interleaved delta/final frames with the originating request.

## Configuration

Read in `twinkle/config.py`, priority: env var > `.env` file > default.

| Variable | Default | Notes |
|---|---|---|
| `TWINKLE_AGENTSERVER_HOST`/`_PORT` | `127.0.0.1` / `18000` | AgentServer listen |
| `TWINKLE_GATEWAY_HOST`/`_PORT` | `127.0.0.1` / `19000` | Gateway browser-ws listen |
| `TWINKLE_LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible |
| `TWINKLE_LLM_API_KEY` | empty | **put in `.env`, never commit** |
| `TWINKLE_LLM_MODEL` | `gpt-4o-mini` | |
| `TWINKLE_AGENT_MAX_STEPS` | `1000` | Max ReAct steps before `e2a.error` (runaway backstop, not a target) |
| `TWINKLE_WORKSPACE_DIR` | `~/.twinkle` | Sandbox root for `command_exec`/`file_tools` — agent file ops confined under this. Defaults to the user home so generated files don't pollute the repo; override to point elsewhere |
| `OTEL_ENABLED` | `false` | Observability master switch (needs `[obs]` extra); false = `setup()` no-op, zero-cost |
| `OTEL_TRACES_EXPORTER`/`OTEL_METRICS_EXPORTER` | `none` | `otlp` / `console` / `none` (read in `twinkle/observability/config.py`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | empty | OTLP gRPC collector endpoint (`http://` = insecure, `https://` = TLS) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | only the gRPC exporter is implemented |
| `OTEL_SERVICE_NAME` | `twinkle-agentserver` | Resource `service.name` |

## Conventions

- **Add a new read-only tool**: write an async function in a `*_tools.py` module under `tools/builtin/`, decorate with `@tool` (the docstring + type hints auto-generate the JSON schema), then `tm.register(it)` inside `tool_manager()` in `tools/__init__.py`. `agent_loop` picks it up via `schemas()`/`execute()` with no loop changes.
- **Add a new channel** (e.g. Feishu): implement the channel interface (`channel_id`, `on_message`, `send`, `start`) and `register_channel` it in `gateway/__main__.py`. Gateway core (`MessageHandler`/`ChannelManager`/`AgentClient`) should not change.
- **Tests must not use `pytest-asyncio`** — use `asyncio.run()` and the `free_port`/`port_factory` fixtures. This is a deliberate choice to avoid pulling the plugin in for free-port fixtures.
- The reference impl `jiuwenswarm` is at `D:\code\opensource\gitcode\jiuwenswarm` (monorepo; `jiuwenclaw/` is the agent app layer; `.py` source is on the `enterprise_dev` branch — main has only `.pyc`) — consult it when a module's behavior is unclear; each module docstring / `docs/architecture.md` §11 maps Twinkle files to jiuwenswarm file ranges.
