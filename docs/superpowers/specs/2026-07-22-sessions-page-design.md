# Sessions Page & File Browser — Design

**Date:** 2026-07-22
**Status:** Approved (brainstorming complete)
**Scope:** Restructure the Twinkle frontend into a jiuwen-style nav shell with a dedicated Sessions page (3-pane file browser), move the new-session button beside the chat input, and add two backend RPCs (`session.files`, `file.read`) to browse a session's files.

## Motivation

The current Twinkle frontend (built in the prior session-management feature) is a 3-column always-visible layout: `SessionSidebar` (session list + "+ 新对话" at top) | `ChatPanel` | `TodoPanel`. It has no notion of separate views/pages and no file browser. The user wants parity with the reference impl `jiuwenclaw` (which they call "jiuwenswarm"): sessions as an independent nav entry opening a dedicated sessions page that shows the history session list **and** the session's files, plus a new-session button beside the command input.

## Reference comparison

`jiuwenclaw` (`D:\opensource\gitcode\jiuwenclaw\jiuwenclaw\web\src`):
- **Shell**: CSS-grid `topbar + left-nav + content`. View switching via a local `activeNav` state (`'chat' | 'sessions' | ...`), no react-router. `SessionSidebar` is the left nav (grouped entries).
- **Sessions page** (`components/SessionsPanel/index.tsx`): 3-pane — session list (1fr) | file tree (1fr) | file preview (3fr). Files fetched via backend `agent/sessions/{sid}` (recursive) + `/file-api/file-content?path=…`. Preview special-cases `history.json` (chat-bubble toggle vs raw JSON), `todo.json` (grouped), `.md/.mdx` (rendered), other `.json` (formatted). Auto-selects the first previewable file.
- **Chat page**: `InputArea` toolbar = left mode-switch | right `[New Session (+), Voice, Model, Send]` — the new-session button is already beside the input.
- **New session** (`handleNewSession`): clears state → mints `sess_…` → `session.create` → stays on chat view.
- **Restore** (`handleRestoreSession`): from Sessions page "Restore" button → `setActiveNav('chat')` + reload history.

Twinkle reuses the spirit but scopes down: left nav has only **2 entries** (Chat + Sessions); sessions have only 2 files (`metadata.json` + `history.json`); the file tree is flat (no recursion needed yet).

## Decisions (locked during brainstorming)

- **Left nav**: 2 entries only — "聊天" (Chat) + "会话" (Sessions). No jiuwen full nav (Agents/Cron/Skills/…).
- **Sessions page**: full 3-pane file browser (list | file tree | preview), mirroring jiuwen.
- **New backend RPCs**: `session.files` (list a session's files) + `file.read` (read one file's content).
- **New-session button**: beside the chat input (remove the old sidebar "+ 新对话").
- **Chat view has no session list** — sessions live only in the Sessions page (independent menu, jiuwen-style).

## Architecture overview

Backend: two new `SessionStore` methods (`list_files`, `read_file`) + two new `session_rpc` dispatch entries. No gateway changes (`e2a.result → result` already carries arbitrary bodies).

Frontend: `App.vue` becomes a nav shell (LeftNav + content switched by `activeNav`). `ChatView` wraps ChatPanel (with a new ➕ button beside the input) + TodoPanel. `SessionsView` is the 3-pane file browser. The old `SessionSidebar.vue` is removed.

## §1. Backend

### `SessionStore` two new methods (`twinkle/agentserver/session_store.py`)

- `list_files(session_id) -> list[dict]` — scan `<sessions_dir>/<sid>/` top-level entries; return `[{name, is_dir, size}]`. Flat (no recursion) — Twinkle session dirs are flat; extend later if subdirs appear. Unknown session → empty list.
- `read_file(session_id, name) -> str` — read `<sid>/<name>` text. **Path-safety** (load-bearing — `name` comes from the browser and content is echoed back):
  - Reject bare-filename violations: empty, contains `/` or `\`, or in `(".", "..")`.
  - Resolve `base = session_dir.resolve()`, `target = (base/name).resolve()`; require `target == base` or `base in target.parents`.
  - `FileNotFoundError` if not a regular file.

Both are sync (like the other read methods — single-threaded asyncio, no interleaving race; consistent with `get_messages`/`list_sessions`/`get_history`).

### `session_rpc.py` two new dispatch entries

Add `"session.files"` and `"file.read"` to `_SESSION_METHODS`:
- `session.files` → `store.list_files(sid)` → `body={"type":"session.files","files":[...]}`.
- `file.read` → `store.read_file(sid, name)` (`name = params.get("name")`) → `body={"type":"file.read","name":name,"content":<str>}`.

Exceptions → `status="failed"` `e2a.result` frame with `body={"type":<method>,"error":str(exc)}` (existing pattern).

### Gateway

No changes — `e2a.result → result` event mapping (Task 5 of the prior feature) already transparently carries arbitrary `body`.

## §2. Frontend

### Shell (`web/src/App.vue`)

Replace the 3-column always-visible layout with a nav shell:

```
┌────┬───────────────────────────────────────┐
│Left│  content (v-if activeNav: ChatView | SessionsView) │
│Nav │                                       │
└────┴───────────────────────────────────────┘
```

`onMounted` still calls `useSessions().init()`. content is `v-if="activeNav==='chat'" <ChatView/> <SessionsView v-else/>`.

### `LeftNav.vue` (new)

Narrow left column. Two buttons: "💬 聊天" / "🗂 会话". Click → `setNav(key)`. Active button highlighted. Matches jiuwen's SessionSidebar (2-entry subset).

### `ChatView.vue` (new)

Wraps `<ChatPanel/>` (center, flex:1) + `<TodoPanel/>` (right, 280px). Same chat layout as today minus the session sidebar.

### `ChatPanel.vue` (modify)

Input footer becomes a toolbar: `[➕ 新对话] [input] [发送]`. The ➕ button calls `createSession()` (existing: mint id → `setSessionId` → clear messages → `session.create` → `loadSessions`). The old `SessionSidebar`'s "+ 新对话" is removed (sidebar deleted).

### `SessionsView.vue` (new, 3-pane)

```
| SessionListPane | FileTreePane | FilePreviewPane |
```

Grid `1fr : 1fr : 3fr` (jiuwen proportions).

- **SessionListPane.vue** (new): `session.list` → items (title, relative time, message_count); click → `selectedSessionId` + `loadSessionFiles(sid)`; per-item ✕ → `deleteSession(id)`; header "↩ 恢复" button → `restoreSession(sid)` (disabled unless a `sess_…` is selected).
- **FileTreePane.vue** (new): `sessionFiles` → list; click a file → `readSessionFile(sid, name)`; highlight selected; "select a session first" empty state.
- **FilePreviewPane.vue** (new): render `previewContent` by `previewFile`:
  - `history.json`: a toggle "聊天气泡 / 原始 JSON". Bubbles mode reuses `fromHistory` to render user/assistant bubbles (tool = muted line). Raw mode = formatted JSON `<pre>`.
  - `metadata.json`: formatted JSON `<pre>`.
  - other: plain-text `<pre>`.
  - Header shows filename.

### `useSessions.ts` additions

- `activeNav: Ref<'chat'|'sessions'>` (default `'chat'`) + `setNav(key)`.
- Sessions-page-only state: `selectedSessionId: Ref<string>` (≠ chat's `currentSessionId`), `sessionFiles: Ref<list>`, `previewFile: Ref<string|null>`, `previewContent: Ref<string>`, `previewLoading: Ref<bool>`, `historyAsBubbles: Ref<bool>` (default true).
- Methods:
  - `loadSessionFiles(sid)` → `client.request('session.files',{session_id:sid})` → `sessionFiles`; auto-select first file → `readSessionFile`.
  - `readSessionFile(sid, name)` → `client.request('file.read',{session_id:sid,name})` → `previewContent` (set `previewFile=name`).
  - `restoreSession(sid)` → `selectSession(sid)` (existing: loads chat history) + `setNav('chat')`.

## §3. Testing, path-safety, files

### Path-safety (re-stated, load-bearing)

`read_file` rejects any `name` that is not a bare filename in the session dir (no `/`, no `\`, no `.`/`..`, resolved path must stay within the session dir). Covered by explicit tests.

### Testing (TDD, asyncio.run + tmp_path, no pytest-asyncio)

- `tests/test_session_store.py` extended: `list_files` returns `metadata.json` + `history.json` after create+append; empty session → only `metadata.json`; `is_dir=False`, `size>0`. `read_file` reads `metadata.json` (valid JSON) + `history.json` (JSONL). Path-safety: `read_file(sid,"../etc/passwd")` → `ValueError`; `read_file(sid,"a/b")` → `ValueError`; `read_file(sid,"nope")` → `FileNotFoundError`.
- `tests/test_session_rpc.py` extended: `session.files` → `e2a.result` single frame, `body.type=="session.files"`, `body.files` has 2 entries; `file.read` of `metadata.json` → `body.content` is valid JSON; `file.read` with unsafe `name` → `status="failed"` result frame.
- `tests/test_integration.py` extended: `test_session_files_ws_round_trip` — real ws path: `session.files` + `file.read` → assert `result` events return the file list and content (closes the browser↔gateway framing gap for the new RPCs).
- Frontend: no test infra; manual `npm run dev` verification (chat ➕ new; sessions page list/tree/preview; history bubble/JSON toggle; restore → chat).

### File map

**Backend (modify):** `twinkle/agentserver/session_store.py` (list_files, read_file), `twinkle/agentserver/session_rpc.py` (2 dispatch entries).

**Frontend (modify):** `web/src/App.vue` (shell), `web/src/components/ChatPanel.vue` (➕ button), `web/src/composables/useSessions.ts` (activeNav + sessions-page state + 3 methods).

**Frontend (new):** `web/src/components/LeftNav.vue`, `ChatView.vue`, `SessionsView.vue`, `SessionListPane.vue`, `FileTreePane.vue`, `FilePreviewPane.vue`.

**Frontend (delete):** `web/src/components/SessionSidebar.vue`.

**Docs:** `docs/architecture.md` §4.7 (file-browser RPCs + nav shell), `CLAUDE.md` session_rpc bullet (+ `session.files`/`file.read`).

## Out of scope (deferred)

- Recursive file trees (subdirs in session dirs) — flat listing is enough for 2-file sessions.
- Markdown rendering / edit-save in the preview pane (twinkle sessions have no `.md` files).
- `todo.json` grouped rendering (twinkle todos aren't persisted to a session file yet).
- Full jiuwen nav (Agents/Cron/Skills/…) — only Chat + Sessions.
- Broadcast-to-all leak in `WebChannel.send` (still intentionally unfixed).
