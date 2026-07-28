# CLAUDE.md

## Guidelines

- Think before coding — state assumptions, ask when uncertain, don't silently pick one interpretation.
- Simplicity first — only write the minimum code that solves the problem; no unrequested abstractions, flexibility, or error handling.
- Surgical changes — only touch what you must; match existing style; don't "improve" adjacent code; remove orphans your changes create.
- Meaningful names — variables/functions must reflect business intent; no single-letter or generic names like `x`, `data`, `result`.
- One purpose per function — extract responsibly; a function should do one thing, not multitask.
- Goal-driven — define verifiable success criteria before starting; loop until verified.

## What this project is

Twinkle is a **learning-focused reimplementation** of the core agent pipeline of `jiuwenswarm` (reference monorepo at `D:\code\opensource\gitcode\jiuwenswarm`, formerly JiuwenClaw; contains `jiuwenswarm/` swarm framework + `jiuwenclaw/` agent app layer + `jiuwenbox/` deploy). It deliberately mirrors jiuwenswarm's two-process + bidirectional-WebSocket architecture so the two can be compared module-by-module. It is **not** a fork, not a SaaS shell, and not feature-complete — see `roadmap.md` for the current phase and scope (Phase 0–4 landed incl. OTel telemetry, context compression, and tool permissions/approval; Phase 5a long-term memory + Phase 7 skills landed; cron (Phase 6) and sub-agent/later phases planned; multi-channel & enterprise features out of scope).

Check `roadmap.md` for the current phase before making architectural changes. `docs/architecture.md` is the source of truth for the current architecture.

## Architecture

```
Browser ──ws (req/res/event)──> Gateway (:19000) ──ws (E2A envelope)──> AgentServer (:18000)
        <──event broadcast──            <──E2AResponse stream──               AgentLoop (ReAct)
```

- **Gateway = format-translator + stream fanout** — converts browser `req` → `E2AEnvelope`, `E2AResponse` → browser events. Core (`MessageHandler`/`ChannelManager`/`AgentClient`) should not change when adding a new channel.
- **AgentServer is channel-agnostic** — only consumes `E2AEnvelope`, yields `E2AResponse`, never sees the browser.
- **`request_id` threads through both wires** — the load-bearing identifier for demux and frame association.
- **Streaming-only** — all requests implicitly streaming; no `is_stream` field.
- **Frontend**: `LeftNav` switches Chat (`ChatPanel`+`TodoPanel`) vs Sessions (3-pane file browser). `ChatPanel` has a ➕ new-session button.

Per-file details → `docs/architecture.md`.

## Conventions

- **Add a new tool**: async function in `tools/builtin/*_tools.py`, decorate with `@tool`, register in `tool_manager()` inside `tools/__init__.py`. `agent_loop` picks it up via `schemas()`/`execute()`.
- **Add a new skill**: create `<WORKSPACE>/skills/<name>/SKILL.md` (frontmatter `name`/`description`/`trigger` + markdown body; `trigger` parsed-but-discarded). `SkillManager` hot-reloads on next `before_model_call`.
- **Add a new channel**: implement interface (`channel_id`, `on_message`, `send`, `start`), register in `gateway/__main__.py`. Gateway core unchanged.
- **Add a new Hook**: class inheriting `AgentHook` in `hooks/builtin/*_hook.py`, set `priority`, register in `build_agent_loop()` or via `loop.register_hook()`.
- **Add a permission rule**: append `(re.Pattern, reason)` to `COMMAND_DENY_PATTERNS` in `twinkle/agentserver/permissions/builtin_rules.py`. For other tools, set tier in config `permissions.tools` or add a user rule.
- **Tests**: no `pytest-asyncio` — use `asyncio.run()` + `free_port`/`port_factory` fixtures (`tests/conftest.py`).
- **Reference impl**: `jiuwenclaw` at `D:\opensource\gitcode\jiuwenclaw` — consult when behavior unclear; `docs/architecture.md` §11 maps Twinkle→jiuwenclaw files.

## Commands & Config

Run tests:
```bash
python -m pytest tests/ -v
python -m pytest tests/test_agent_loop.py -v                    # single file
python -m pytest tests/test_tool_manager.py::test_name          # single test
```

Start backend (each blocks; use separate terminals or launcher):
```bash
python scripts/start_services.py                                # both
python -m twinkle.agentserver                                   # :18000
python -m twinkle.gateway                                       # :19000
```

Start frontend:
```bash
cd web && npm install && npm run dev                            # http://localhost:5173
```

Set `TWINKLE_LLM_API_KEY` in `.env` (copy `.env.example`) — without it the agent loop fails at model call time. Tunable config → `twinkle/resources/config.yaml` (priority: env var > `.env` file > YAML default).
