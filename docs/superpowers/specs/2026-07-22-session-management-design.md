# Session Management & History Viewing — Design

**Date:** 2026-07-22
**Status:** Approved (brainstorming complete)
**Scope:** New feature — create new chat sessions, list past sessions, view a past
session's conversation history. Persist sessions to disk so they survive restarts.

## Motivation

Twinkle today has no notion of multiple sessions visible to the user. `SessionStore`
is an in-memory `dict[session_id → list[msg]]` that is append-only and lost on
AgentServer restart; `session_id` is minted browser-side on every WebSocket open and
is private to `WebClient`, so a page reload mints a new id and the old conversation
becomes unreachable. The roadmap explicitly deferred persistence, and
`docs/e2a-introduction.md` lists `session.list` / `history.get` as "❌ roadmap 不做".

This feature is a deliberate, net-new extension past the stated roadmap. It mirrors
the session-management behavior of the reference implementation `jiuwenclaw` (the
explorer confirmed there is no separate `jiuwenswarm` package; "jiuwenswarm" is only a
telemetry-instrumentor name in a comment). Goals, in the user's words:

1. After refreshing the page, see the current session's records.
2. Switch a session menu to view historical sessions' conversation records — same
   as jiuwenswarm.

## Reference comparison

`jiuwenclaw` persists sessions as JSON files on disk
(`<workspace>/agent/sessions/<sid>/metadata.json` + `history.json` JSONL), auto-titles
from the first user message (truncated 50 chars), and uses `session.create` /
`session.list` / `session.delete` / `history.get` RPCs. In its **default local storage
mode**, `session.list`/`delete` are handled locally in the gateway and
`session.create`/`history.get` are forwarded to the agent server; in its **remote
storage mode**, all of them are forwarded to the agent server.

Twinkle's architecture forbids business logic in the gateway ("Gateway is a pure
format-translator + stream fanout"). Therefore this design routes **all** session
RPCs to the AgentServer — equivalent to jiuwenclaw's remote storage mode, and a
cleaner version of the local mode. The observable behavior (same disk layout, same
metadata/history fields, same RPC names, same auto-titling) matches jiuwenswarm; the
internal placement is the remote-mode shape.

## Decisions (locked during brainstorming)

- **Persistence format:** JSON files per session (mirror jiuwenclaw). Not SQLite,
  despite the `session_store.py` docstring mentioning SQLite — the JSON mirror better
  serves the learning "compare module-by-module" philosophy.
- **Frontend state management:** a composable (`useSessions.ts`) with module-level
  reactive state. No Pinia, no new runtime dependency (consistent with Twinkle's
  minimal-deps stance).
- **Scope in:** auto-title from first user message ✅, delete session ✅, no
  pagination (history returned in one frame) ✅.
- **Scope out:** do **not** fix the `WebChannel.send` broadcast-to-all leak (single
  tab is unaffected; multi-tab cross-talk is accepted for now).

## Architecture overview

Approach A (approved): AgentServer owns the session RPCs and persistence; a new
`e2a.result` E2A frame kind carries structured RPC responses; the gateway maps it
1:1 to a new browser `result` event. `chat.send` streaming is unchanged.

```
Browser ──ws req {method:session.*|history.get}──> Gateway ──E2AEnvelope──> AgentServer
                                                                                   │
                                                       SessionStore (disk-backed) ◄┘
Browser <──event:result── Gateway <──E2AResponse(e2a.result, is_final)── AgentServer
```

`session_id` remains browser-generated and sticky (persisted in `localStorage`).
The AgentServer gains a small method-dispatch path alongside `AgentLoop.run_stream`.

## §1. Persistence layout + backend

### Persistence layout

New config `TWINKLE_SESSIONS_DIR`, default `<repo_root>/.twinkle_data/sessions`
(i.e. `Path(WORKSPACE_DIR) / ".twinkle_data" / "sessions"`; `WORKSPACE_DIR` defaults
to the repo root). Added to `.gitignore` as `.twinkle_data/`. If the operator wants
strict isolation from the `command_exec` sandbox (whose workdir is confined under
`WORKSPACE_DIR`), they set `TWINKLE_SESSIONS_DIR` outside `WORKSPACE_DIR`.

Per-session directory:

```
.twinkle_data/sessions/<session_id>/
  metadata.json    # {session_id, title, created_at, last_message_at, message_count, channel_id}
  history.json      # JSONL, one record per line
```

`history.json` record shape:
`{id, role, request_id, channel_id, timestamp, content, event_type, session_id,
...openai_fields}` where `...openai_fields` preserves the full OpenAI-native message
(`tool_calls`, `tool_call_id`) so the ReAct context can be reconstructed on cold
start. This differs from jiuwenclaw (which stores display-oriented fields only) and is
necessary because Twinkle resumes ReAct turns on a cold store.

### `SessionStore` rewrite (`twinkle/agentserver/session_store.py`)

Two-layer: in-memory cache + disk persistence (mirrors jiuwenclaw's metadata
in-memory cache).

- In-memory cache `dict[sid → list[OpenAI msg]]` — `AgentLoop.get_messages` hits the
  cache, unchanged speed.
- **Cold-start hydration:** `get_messages(sid)` on cache miss reconstructs the
  OpenAI message list from `history.json` and caches it. This is the linchpin for
  resuming an old session after a restart: `AgentLoop.run_stream` first calls
  `existing = self._store.get_messages(session_id)` and must receive the full history
  (system todo prompt, prior user/assistant/tool messages) rather than an empty list
  that would re-insert the system prompt and lose prior tool context.
- `append(sid, msg, request_id=None, event_type=None)` — append to cache + append a
  `history.json` record + update metadata (`message_count++`, `last_message_at=now`,
  first user message sets the title). Signature adds optional params; existing
  callers pass `request_id` where available.
- New methods:
  - `create_session(sid, channel_id="web")` — idempotent metadata write.
  - `list_sessions(limit=100)` — scan dir, read each `metadata.json`, sort by
    `last_message_at` desc. Metadata corrupt/missing → fall back to dir stat `mtime`
    with minimal stub metadata (mirrors jiuwenclaw `get_all_sessions_metadata` legacy
    fallback).
  - `delete_session(sid)` — remove dir + evict cache entry.
  - `get_history(sid)` — read `history.json` records for frontend display.
- Concurrency: an `asyncio.Lock` serializes the read-modify-write of metadata +
  JSONL append (same pattern as `todo_store.py`). Single-user single-process, so no
  cross-process coherence concern.

### `agent_loop.py` changes

The three `self._store.append(...)` call sites (system todo prompt, user message,
tool result) pass `request_id=envelope.request_id`. No behavioral change to the
ReAct loop.

### RPC dispatch (`twinkle/agentserver/server.py`)

`agent_loop()` constructs `store = SessionStore(SESSIONS_DIR)` and passes it to
`AgentLoop`; `ws_handler(loop, store)` gains a method router:

- `session.create` / `session.list` / `session.delete` / `history.get` →
  `_dispatch_session_rpc(envelope, store)` → single-frame
  `E2AResponse(response_kind="e2a.result", body={...}, is_final=True,
  status="succeeded")`.
- everything else (`chat.send`) → `loop.run_stream(envelope)` (unchanged).

`_dispatch_session_rpc` lives in a new `twinkle/agentserver/session_rpc.py`
(separate from `server.py` so `server.py` stays a thin ws entry point).

### E2A + gateway mapping

- `twinkle/e2a/models.py`: `E2AResponse.response_kind` docstring gains `e2a.result`.
- `twinkle/schema/message.py`: `EventType.RESULT = "result"`.
- `twinkle/gateway/message_handler.py`: `_process_stream` adds a branch —
  `if resp.response_kind == "e2a.result": out = Message(event_type=EventType.RESULT,
  payload=resp.body, content="")`. The existing chat/todo branches are unchanged.
  (Detail discovered during design: today `_process_stream` assumes every response is
  chat-shaped via `body.result.content`; a structured RPC response would otherwise be
  emitted as an empty `chat.final`. The new branch fixes this.)
- `WebChannel.send` already broadcasts arbitrary payloads, so the `result` event
  passes through unchanged (broadcast-to-all leak intentionally not fixed).

## §2. Frontend

### `web/src/services/webClient.ts`

- Expose `getSessionId()` / `setSessionId(id)` (currently `sessionId` is private).
- **Sticky session_id:** `onopen` reads `localStorage.getItem('twinkle.sessionId')`;
  present → reuse, absent → mint `sess_<uuid>` and persist. Page reload reattaches to
  the same session.
- **New `request(method, params): Promise<any>`** — reuses `send()` (returns
  `request_id`), registers in a `private pending: Map<rid, resolve>`; on
  `event === 'result'` in `handle()`, resolves by `request_id`, delivers `payload`,
  deletes the entry. Mirrors jiuwenclaw `webClient.request`. Chat streaming still
  uses `send()` + `onDelta`/`onFinal`, unchanged.

### `web/src/composables/useSessions.ts` (new)

Module-level reactive singleton (no Pinia). State:

```
sessions[]          // [{session_id, title, last_message_at, ...}]
currentSessionId
messages[]          // [{role:'user'|'assistant', content}]
connected, busy, loading
```

Methods:
- `init()` → `client.connect(ready)`; on open, `loadSessions()` and, if
  `localStorage` has a sid, `selectSession(sid)` to surface history immediately.
- `loadSessions()` → `client.request('session.list', {})` → `sessions = payload.sessions`.
- `createSession()` → mint sid → `client.setSessionId` →
  `client.request('session.create', {session_id})` → clear messages → persist
  localStorage → `loadSessions()`.
- `selectSession(id)` → `client.setSessionId(id)` → persist localStorage →
  `messages = (await client.request('history.get', {session_id:id})).messages` →
  `currentSessionId = id`.
- `deleteSession(id)` → `client.request('session.delete', {session_id:id})` → if it
  was the current session, `createSession()` → `loadSessions()`.
- `sendQuery(q)` → push user msg → `client.send('chat.send', {query:q})` (streaming
  deltas/finals update `messages`).

The composable owns `messages` and installs `WebClient`'s `onDelta`/`onFinal`/
`onTodoUpdate` handlers (logic migrated from today's `App.vue`, filtering by
`currentId === client.lastRequestId`).

### Components

- `App.vue` — becomes a layout shell: `<SessionSidebar /> + <ChatPanel /> +
  <TodoPanel />`; `onMounted` calls `useSessions().init()`.
- `SessionSidebar.vue` (new) — "+ 新对话" button (`createSession`); list items show
  `title` + relative time, click `selectSession`, per-item ✕ `deleteSession`;
  current session highlighted.
- `ChatPanel.vue` (extracted from `App.vue`) — consumes composable
  `messages/busy/input`; renders bubbles + input + send.
- `TodoPanel.vue` (extracted from `App.vue`) — existing todo sidebar, unchanged.

### History rendering

`history.get` returns OpenAI-native role/content. `ChatPanel` renders bubbles only
for `role ∈ {user, assistant}`; skips `system` (todo prompt); shows `tool` messages
as one muted monospace line (content summary) — a simplified parallel to jiuwenclaw's
tool_call/tool_result timeline entries.

### Page-reload path

1. load → `init()` → `client.connect`.
2. `onopen` → sid from localStorage (reused) → backend cache hit or cold-start
   hydration from `history.json`.
3. `selectSession(sid)` → `history.get` → messages rendered. The current session is
   immediately visible after refresh.

## §3. Error handling, testing, config

### Error handling

- **Backend RPC:** `_dispatch_session_rpc` try/excepts disk/IO/invalid-sid; returns
  `E2AResponse(response_kind="e2a.result", status="failed",
  body={"type": <method>, "error": str}, is_final=True)` with `request_id` always
  backfilled. Frontend `request()` resolves then rejects on `body.error`.
- **history.json tolerant read:** `get_history` / cold hydration parse JSONL
  line-by-line; a `JSONDecodeError` on one line is skipped (warn-logged), never
  thrown — one bad record cannot brick a session. Mirrors jiuwenclaw
  `read_history_records`.
- **metadata fallback:** covered in `list_sessions` above.
- **concurrency:** `SessionStore` `asyncio.Lock` (covered above).

### Testing (asyncio.run + free_port/tmp_path, no pytest-asyncio)

- `tests/test_session_store.py` extended (disk backend): `create_session` idempotent;
  `append` writes a history line + updates metadata; first user message auto-titles
  (truncate 50 + `...`); `list_sessions` sorts desc + metadata fallback; **cold-start
  recovery** — new `SessionStore(same_dir)` `get_messages` returns the full OpenAI
  message list including `tool_calls`/`tool_call_id`, in order; `get_history` skips
  corrupt lines; `delete_session` removes dir + evicts cache.
- `tests/test_session_rpc.py` (new): `ws_handler(loop, store)` dispatch —
  `session.list/create/delete`, `history.get` each return the right `e2a.result`
  single frame (`is_final`, `status`, `body`); unknown method falls through to
  `loop.run_stream`; RPC exception → `status="failed"` result frame. Uses a fake loop
  (`ws_handler` supports injection) + `tmp_path` sessions dir.
- `tests/test_message_handler.py` extended: feed
  `E2AResponse(response_kind="e2a.result", body={...})` to `_process_stream`, assert
  it emits `Message(event_type=EventType.RESULT, payload=body)`.
- `tests/conftest.py`: add `sessions_dir` (wraps `tmp_path`) and `session_store`
  fixtures.
- Frontend: no test infra (status quo); manual `npm run dev` verification of the
  new/switch/refresh/delete paths.

### Config + gitignore

`twinkle/config.py`:
```python
SESSIONS_DIR = os.getenv("TWINKLE_SESSIONS_DIR") or str(
    Path(WORKSPACE_DIR).parent / ".twinkle_data" / "sessions"
)
```
`.gitignore`: add `.twinkle_data/`.

## File map

**Modified:**
| File | Change |
|---|---|
| `twinkle/agentserver/session_store.py` | rewrite as disk two-layer + create/list/delete/get_history + cold hydration + Lock |
| `twinkle/agentserver/agent_loop.py` | 3 append sites pass `request_id` |
| `twinkle/agentserver/server.py` | `ws_handler(loop, store)` + `_dispatch_session_rpc` router; `agent_loop()` exposes store |
| `twinkle/e2a/models.py` | `response_kind` docstring adds `e2a.result` |
| `twinkle/gateway/message_handler.py` | `_process_stream` adds `e2a.result → result` branch |
| `twinkle/schema/message.py` | `EventType.RESULT = "result"` |
| `twinkle/config.py` | `SESSIONS_DIR` |
| `.gitignore` | `.twinkle_data/` |
| `web/src/services/webClient.ts` | expose sessionId + sticky localStorage + `request(method)` + pending map |
| `web/src/App.vue` | layout shell + `useSessions().init()` |
| `tests/conftest.py` | `sessions_dir`/`session_store` fixtures |
| `tests/test_session_store.py`, `tests/test_message_handler.py` | extended |

**New:**
| File | Purpose |
|---|---|
| `twinkle/agentserver/session_rpc.py` | `_dispatch_session_rpc` (router for session.*/history.get) |
| `tests/test_session_rpc.py` | RPC dispatch tests |
| `web/src/composables/useSessions.ts` | session state composable |
| `web/src/components/SessionSidebar.vue` | session sidebar |
| `web/src/components/ChatPanel.vue` | chat area (extracted) |
| `web/src/components/TodoPanel.vue` | todo sidebar (extracted) |

**Docs:** `docs/architecture.md` adds a "Session persistence & history RPCs" section
flagging this as an intentional extension past the roadmap's "no persistence" stance;
`CLAUDE.md` SessionStore description updated.

## Out of scope (deferred)

- Broadcast-to-all leak in `WebChannel.send` (per-connection session filtering).
- History pagination (single-frame return instead).
- Session rename (manual title edit).
- Long-term memory (`memory.py` stub remains untouched).
- Cross-process / multi-user session coherence (single-user single-process assumed).
