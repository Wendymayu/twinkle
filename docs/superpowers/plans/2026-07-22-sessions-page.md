# Sessions Page & File Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the frontend into a jiuwen-style nav shell with a dedicated 3-pane Sessions page (session list | file tree | file preview), move the new-session button beside the chat input, and add `session.files` + `file.read` backend RPCs to browse a session's files.

**Architecture:** Backend adds two `SessionStore` read methods + two `session_rpc` dispatch entries (no gateway change — `e2a.result` mapping already carries arbitrary bodies). Frontend: `App.vue` becomes a nav shell (`LeftNav` + content switched by `activeNav`); `ChatView` wraps `ChatPanel` (now with a ➕ button beside the input) + `TodoPanel`; `SessionsView` is the 3-pane file browser. The old `SessionSidebar.vue` is deleted.

**Tech Stack:** Python 3 (stdlib `json`/`pathlib`/`asyncio`, `pydantic`), `websockets`, Vue 3 + Vite (no Pinia/Router).

**Spec:** `docs/superpowers/specs/2026-07-22-sessions-page-design.md`

## Global Constraints

- Tests use plain `def test_*` + `asyncio.run()`. **No `pytest-asyncio`** (deliberate).
- `SessionStore` mutating methods are async + hold `asyncio.Lock`; reads are sync. The two new methods (`list_files`, `read_file`) are **sync** reads.
- `read_file` must be path-traversal-safe: `name` is a bare filename only (no `/`, no `\`, no `.`/`..`), and the resolved path must stay within the session dir. This is load-bearing — `name` comes from the browser and content is echoed back.
- New RPCs yield a single `E2AResponse(response_kind="e2a.result", is_final=True)`; failures yield `status="failed"` with `body={"type":<method>,"error":str}`.
- Gateway stays a pure format-translator (no business logic).
- Left nav has exactly 2 entries: 聊天 (Chat) + 会话 (Sessions).
- No new runtime frontend dependency (no Pinia/Router) — view switching via a reactive `activeNav` in the `useSessions` composable.
- Branch: `feat/session-management` (already checked out; the prior session-management feature + the origin/main merge are already committed).

## File Structure

**Backend:** `twinkle/agentserver/session_store.py` (+`list_files`/`read_file`), `twinkle/agentserver/session_rpc.py` (+2 dispatch entries).

**Frontend:** `web/src/composables/useSessions.ts` (+`activeNav` + sessions-page state + 3 methods); `web/src/App.vue` (shell); `web/src/components/ChatPanel.vue` (➕ button); new `LeftNav.vue`/`ChatView.vue`/`SessionsView.vue`/`SessionListPane.vue`/`FileTreePane.vue`/`FilePreviewPane.vue`; **delete** `web/src/components/SessionSidebar.vue`.

**Tests/docs:** `tests/test_session_store.py`, `tests/test_session_rpc.py`, `tests/test_integration.py`, `docs/architecture.md`, `CLAUDE.md`.

---

### Task 1: SessionStore `list_files` + `read_file` (path-safe)

**Files:**
- Modify: `twinkle/agentserver/session_store.py`
- Test: `tests/test_session_store.py`

**Interfaces:**
- Produces: sync `list_files(session_id) -> list[dict]` returning `[{name, is_dir, size}]` for the top-level entries of `<sessions_dir>/<sid>/` (empty list if absent); sync `read_file(session_id, name) -> str` with path-traversal guards (`ValueError` for non-bare names or escaping paths, `FileNotFoundError` if missing).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_store.py`:

```python
def test_list_files_lists_session_files(session_store):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hi"}, request_id="r1"))
    files = session_store.list_files("s1")
    names = {f["name"] for f in files}
    assert "metadata.json" in names
    assert "history.json" in names
    for f in files:
        assert f["is_dir"] is False
        assert f["size"] >= 0


def test_list_files_unknown_session_returns_empty(session_store):
    assert session_store.list_files("never") == []


def test_read_file_returns_metadata_json(session_store):
    _run(session_store.create_session("s1"))
    import json as _json
    content = session_store.read_file("s1", "metadata.json")
    meta = _json.loads(content)
    assert meta["session_id"] == "s1"
    assert meta["message_count"] == 0


def test_read_file_returns_history_jsonl(session_store):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hi"}, request_id="r1"))
    content = session_store.read_file("s1", "history.json")
    import json as _json
    lines = [_json.loads(l) for l in content.splitlines() if l.strip()]
    assert lines[0]["role"] == "user"
    assert lines[0]["content"] == "hi"


def test_read_file_rejects_path_traversal(session_store):
    _run(session_store.create_session("s1"))
    for bad in ["../etc/passwd", "a/b", "..", ".", "a\\b", ""]:
        try:
            session_store.read_file("s1", bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_read_file_missing_raises_filenotfound(session_store):
    _run(session_store.create_session("s1"))
    try:
        session_store.read_file("s1", "nope.json")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_session_store.py -k "list_files or read_file" -v`
Expected: FAIL — `AttributeError: 'SessionStore' object has no attribute 'list_files'`.

- [ ] **Step 3: Implement `list_files` + `read_file`**

Add to `twinkle/agentserver/session_store.py` (inside `SessionStore`, after `get_history`):

```python
    def list_files(self, session_id: str) -> list[dict]:
        """List top-level files in a session dir. Flat (no recursion) — Twinkle
        session dirs are flat. Unknown session -> []."""
        sdir = self._session_dir(session_id)
        if not sdir.is_dir():
            return []
        out: list[dict] = []
        for p in sdir.iterdir():
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "is_dir": p.is_dir(), "size": st.st_size})
        out.sort(key=lambda f: f["name"])
        return out

    def read_file(self, session_id: str, name: str) -> str:
        """Read a file's text content from a session dir. ``name`` MUST be a
        bare filename — path traversal is rejected (name comes from the browser
        and content is echoed back to the preview pane)."""
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError(f"unsafe file name: {name!r}")
        base = self._session_dir(session_id).resolve()
        target = (base / name).resolve()
        if target != base and base not in target.parents:
            raise ValueError(f"path escapes session dir: {name!r}")
        p = self._session_dir(session_id) / name
        if not p.is_file():
            raise FileNotFoundError(f"no such file: {name}")
        return p.read_text(encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session_store.py -v`
Expected: all PASS (prior tests + the 6 new ones).

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/session_store.py tests/test_session_store.py
git commit -m "session: list_files + path-safe read_file on SessionStore"
```

---

### Task 2: session_rpc dispatch for `session.files` + `file.read`

**Files:**
- Modify: `twinkle/agentserver/session_rpc.py`
- Test: `tests/test_session_rpc.py`

**Interfaces:**
- Produces: `_SESSION_METHODS` now includes `session.files`/`file.read`; `dispatch_session_rpc` handles them: `session.files` → `body={"type":"session.files","files":store.list_files(sid)}`; `file.read` → `body={"type":"file.read","name":name,"content":store.read_file(sid,name)}` (`name = envelope.params.get("name")`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_rpc.py`:

```python
def test_session_files_returns_result_frame(session_store):
    _run(session_store.create_session("s1"))
    _run(session_store.append("s1", {"role": "user", "content": "hi"}, request_id="r1"))
    frames = _run(_frames(_env("session.files", session_id="s1"), session_store))
    f = frames[0]
    assert f.response_kind == "e2a.result"
    assert f.is_final is True
    assert f.body["type"] == "session.files"
    names = {x["name"] for x in f.body["files"]}
    assert "metadata.json" in names
    assert "history.json" in names


def test_file_read_returns_content(session_store):
    _run(session_store.create_session("s1"))
    frames = _run(_frames(
        _env("file.read", session_id="s1", params={"name": "metadata.json"}),
        session_store,
    ))
    f = frames[0]
    assert f.body["type"] == "file.read"
    assert f.body["name"] == "metadata.json"
    import json as _json
    meta = _json.loads(f.body["content"])
    assert meta["session_id"] == "s1"


def test_file_read_unsafe_name_returns_failed_frame(session_store):
    _run(session_store.create_session("s1"))
    frames = _run(_frames(
        _env("file.read", session_id="s1", params={"name": "../etc/passwd"}),
        session_store,
    ))
    f = frames[0]
    assert f.status == "failed"
    assert f.body["type"] == "file.read"
    assert "error" in f.body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_session_rpc.py -k "session_files or file_read" -v`
Expected: FAIL — `session.files`/`file.read` not in `_SESSION_METHODS`, so `dispatch_session_rpc` yields nothing (the `else: return`).

- [ ] **Step 3: Wire the two dispatch entries**

In `twinkle/agentserver/session_rpc.py`:

Change the `_SESSION_METHODS` set:
```python
_SESSION_METHODS = {
    "session.create", "session.list", "session.delete", "history.get",
    "session.files", "file.read",
}
```

In `dispatch_session_rpc`, add two branches (before the `else: return`):
```python
        elif method == "session.files":
            files = store.list_files(sid)
            body = {"type": "session.files", "files": files}
        elif method == "file.read":
            name = envelope.params.get("name")
            content = store.read_file(sid, name)
            body = {"type": "file.read", "name": name, "content": content}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session_rpc.py -v`
Expected: all PASS (prior 6 + 3 new).

- [ ] **Step 5: Run the full backend suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/session_rpc.py tests/test_session_rpc.py
git commit -m "session_rpc: dispatch session.files + file.read"
```

---

### Task 3: ws-path integration test for `session.files` + `file.read`

**Files:**
- Modify: `tests/test_integration.py`

**Interfaces:**
- Produces: `test_session_files_ws_round_trip` exercising the full browser→gateway→AgentServer ws path for the two new RPCs, asserting the `result` events carry the file list + content (closes the framing gap for the new RPCs).

- [ ] **Step 1: Write the test**

Append to `tests/test_integration.py` (reusing the existing `_ScriptedLLM`, imports, and the `serve(ws_handler(loop_obj, store), ...)` + gateway stand-up pattern from `test_end_to_end_tool_round_trip`):

```python
async def _collect_result(browser) -> dict:
    """Read frames until a `result` event; return its payload."""
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(browser.recv(), timeout=5)
        frame = json.loads(raw)
        if frame.get("type") == "event" and frame.get("event") == "result":
            return frame["payload"]
    raise AssertionError("no result event received")


def test_session_files_ws_round_trip(tmp_path, port_factory) -> None:
    agentserver_port = port_factory()
    gateway_port = port_factory()
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s-files"))
    asyncio.run(store.append("s-files", {"role": "user", "content": "hello"},
                              request_id="r0"))
    loop_obj = AgentLoop(_ScriptedLLM([]), store, _reg_with_echo(), LongTermMemory())

    async def run() -> None:
        server = await serve(ws_handler(loop_obj, store), "127.0.0.1", agentserver_port)
        try:
            agent_client = AgentClient(f"ws://127.0.0.1:{agentserver_port}")
            await agent_client.connect()
            message_handler = MessageHandler(agent_client)
            channel_manager = ChannelManager(message_handler)
            web_channel = WebChannel("127.0.0.1", gateway_port)
            channel_manager.register_channel(web_channel)
            await channel_manager.start()
            web_server = await serve(web_channel.handler, "127.0.0.1", gateway_port)
            try:
                async with connect(f"ws://127.0.0.1:{gateway_port}") as browser:
                    await browser.recv()  # connection.ack

                    # session.files
                    await browser.send(json.dumps({
                        "type": "req", "id": "rf1", "method": "session.files",
                        "params": {"session_id": "s-files"},
                    }))
                    await asyncio.wait_for(browser.recv(), timeout=5)  # ack
                    payload = await _collect_result(browser)
                    assert payload["type"] == "session.files"
                    names = {f["name"] for f in payload["files"]}
                    assert "metadata.json" in names
                    assert "history.json" in names

                    # file.read
                    await browser.send(json.dumps({
                        "type": "req", "id": "rf2", "method": "file.read",
                        "params": {"session_id": "s-files", "name": "metadata.json"},
                    }))
                    await asyncio.wait_for(browser.recv(), timeout=5)  # ack
                    payload = await _collect_result(browser)
                    assert payload["type"] == "file.read"
                    assert payload["name"] == "metadata.json"
                    meta = json.loads(payload["content"])
                    assert meta["session_id"] == "s-files"
            finally:
                web_server.close()
                await web_server.wait_closed()
                await channel_manager.stop()
                await agent_client.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
```

(If `_reg_with_echo` import is needed it's already at the top of `test_integration.py`. `_ScriptedLLM([])` is fine — the RPCs don't run the loop.)

- [ ] **Step 2: Run the test**

Run: `python -m pytest tests/test_integration.py::test_session_files_ws_round_trip -v`
Expected: PASS. If the `result` event doesn't arrive, that's a real framing gap — debug it (the existing `test_session_rpc_round_trip` already proved `session.list`/`history.get` framing works, so this should too).

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: ws-path round-trip for session.files + file.read"
```

---

### Task 4: `useSessions` — `activeNav` + sessions-page state + 3 methods

**Files:**
- Modify: `web/src/composables/useSessions.ts`

**Interfaces:**
- Produces: `activeNav: Ref<'chat'|'sessions'>` + `setNav(key)`; sessions-page state `selectedSessionId`/`sessionFiles`/`previewFile`/`previewContent`/`previewLoading`/`historyAsBubbles`; methods `loadSessionFiles(sid)`/`readSessionFile(sid,name)`/`restoreSession(sid)`. `fromHistory` is exported (reused by FilePreviewPane). `createSession` unchanged.

- [ ] **Step 1: Add the new state + methods to `useSessions.ts`**

Edit `web/src/composables/useSessions.ts`:

(a) Add to the imports line:
```typescript
import { ref, computed } from 'vue'
```
(no change — `ref`/`computed` already imported.)

(b) After the existing module-level state block (after `const todo = ref<TodoState | null>(null)`), add:
```typescript
type NavKey = 'chat' | 'sessions'
const activeNav = ref<NavKey>('chat')
const selectedSessionId = ref<string>('')
const sessionFiles = ref<{ name: string; is_dir: boolean; size: number }[]>([])
const previewFile = ref<string | null>(null)
const previewContent = ref<string>('')
const previewLoading = ref(false)
const historyAsBubbles = ref(true)

function setNav(key: NavKey) {
  activeNav.value = key
}
```

(c) After `deleteSession`, add the three sessions-page methods:
```typescript
async function loadSessionFiles(sid: string) {
  if (!sid) {
    sessionFiles.value = []
    previewFile.value = null
    previewContent.value = ''
    return
  }
  selectedSessionId.value = sid
  const payload = await client.request('session.files', { session_id: sid })
  sessionFiles.value = payload?.files ?? []
  // auto-select the first file
  const first = sessionFiles.value.find((f) => !f.is_dir)
  if (first) {
    await readSessionFile(sid, first.name)
  } else {
    previewFile.value = null
    previewContent.value = ''
  }
}

async function readSessionFile(sid: string, name: string) {
  if (!sid || !name) return
  previewLoading.value = true
  previewFile.value = name
  try {
    const payload = await client.request('file.read', { session_id: sid, name })
    previewContent.value = payload?.content ?? ''
  } finally {
    previewLoading.value = false
  }
}

async function restoreSession(sid: string) {
  await selectSession(sid) // loads chat history + sets currentSessionId
  setNav('chat')
}
```

(d) Export everything in the `useSessions()` return:
```typescript
export function useSessions() {
  return {
    sessions, currentSessionId, messages, connected, busy, loading, todo,
    completedCount, box, fromHistory,
    activeNav, setNav,
    selectedSessionId, sessionFiles, previewFile, previewContent,
    previewLoading, historyAsBubbles,
    init, loadSessions, createSession, selectSession, deleteSession, sendQuery,
    loadSessionFiles, readSessionFile, restoreSession,
  }
}
```

Also add `fromHistory` to the returned object (it's currently module-private; export it so `FilePreviewPane` can reuse it). Add this line to the return if not present: `fromHistory,`.

- [ ] **Step 2: Build to verify no type errors**

Run: `cd web && npm run build`
Expected: clean (nothing imports the new fields yet — that's fine; the composable just exposes them).

- [ ] **Step 3: Commit**

```bash
git add web/src/composables/useSessions.ts
git commit -m "web: useSessions activeNav + sessions-page state + file RPC methods"
```

---

### Task 5: `LeftNav.vue` + `App.vue` shell + `ChatView.vue` placeholder

**Files:**
- Create: `web/src/components/LeftNav.vue`
- Create: `web/src/components/ChatView.vue`
- Create: `web/src/components/SessionsView.vue` (placeholder; full impl in Task 7)
- Modify: `web/src/App.vue`

**Interfaces:**
- Produces: `LeftNav` (2 buttons → `setNav`), `ChatView` (wraps `ChatPanel` + `TodoPanel`), `SessionsView` (placeholder div for now), `App.vue` shell switching on `activeNav`.

- [ ] **Step 1: Create `LeftNav.vue`**

```vue
<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
const { activeNav, setNav } = useSessions()
</script>

<template>
  <nav class="left-nav">
    <button :class="{ active: activeNav === 'chat' }" @click="setNav('chat')">💬 聊天</button>
    <button :class="{ active: activeNav === 'sessions' }" @click="setNav('sessions')">🗂 会话</button>
  </nav>
</template>

<style scoped>
.left-nav {
  width: 72px; flex: 0 0 72px; border-right: 1px solid #e2e8f0; background: #fff;
  display: flex; flex-direction: column; gap: .25rem; padding: .5rem .35rem;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
button {
  border: 0; background: transparent; border-radius: 8px; padding: .5rem .25rem;
  cursor: pointer; font-size: .72rem; color: #475569; display: flex;
  flex-direction: column; align-items: center; gap: .15rem;
}
button:hover { background: #f1f5f9; }
button.active { background: #eef2ff; color: #4f46d5; font-weight: 600; }
</style>
```

- [ ] **Step 2: Create `ChatView.vue`**

```vue
<script setup lang="ts">
import ChatPanel from './ChatPanel.vue'
import TodoPanel from './TodoPanel.vue'
</script>

<template>
  <div class="chat-view">
    <ChatPanel />
    <TodoPanel />
  </div>
</template>

<style scoped>
.chat-view { display: flex; flex: 1; min-width: 0; }
@media (max-width: 640px) { .chat-view { flex-direction: column; } }
</style>
```

- [ ] **Step 3: Create `SessionsView.vue` (placeholder)**

```vue
<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
const { setNav } = useSessions()
</script>

<template>
  <div class="sessions-view">
    <p>Sessions page (TODO in Task 7)</p>
    <button @click="setNav('chat')">← 返回聊天</button>
  </div>
</template>

<style scoped>
.sessions-view { flex: 1; padding: 2rem; color: #64748b;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
</style>
```

- [ ] **Step 4: Rewrite `App.vue` as the shell**

Replace `web/src/App.vue`:

```vue
<script setup lang="ts">
import { onMounted } from 'vue'
import { useSessions } from './composables/useSessions'
import LeftNav from './components/LeftNav.vue'
import ChatView from './components/ChatView.vue'
import SessionsView from './components/SessionsView.vue'

const { init, activeNav } = useSessions()

onMounted(() => { init() })
</script>

<template>
  <div class="app">
    <LeftNav />
    <main class="content">
      <ChatView v-if="activeNav === 'chat'" />
      <SessionsView v-else />
    </main>
  </div>
</template>

<style>
* { box-sizing: border-box; }
html, body, #app { height: 100%; margin: 0; }
body { background: #f8fafc; }
.app { display: flex; height: 100%; }
.content { flex: 1; min-width: 0; display: flex; }
</style>
```

- [ ] **Step 5: Build to verify**

Run: `cd web && npm run build`
Expected: clean. (The old `SessionSidebar` is now unused but still on disk — deleting it is Task 7. `ChatPanel` still imports from `useSessions` only what it used before; the ➕ button comes in Task 6. Build should pass.)

- [ ] **Step 6: Commit**

```bash
git add web/src/App.vue web/src/components/LeftNav.vue web/src/components/ChatView.vue web/src/components/SessionsView.vue
git commit -m "web: nav shell (LeftNav + ChatView/SessionsView v-if)"
```

---

### Task 6: `ChatPanel` ➕ new-session button beside the input

**Files:**
- Modify: `web/src/components/ChatPanel.vue`

**Interfaces:**
- Produces: ChatPanel's input footer now has a ➕ button (calls `createSession`) to the left of the input + send.

- [ ] **Step 1: Add the ➕ button**

In `web/src/components/ChatPanel.vue`, change the destructure to include `createSession`:
```typescript
const { messages, connected, busy, loading, sendQuery, createSession } = useSessions()
```

Replace the `<footer>` block with:
```html
    <footer>
      <button class="new-btn" @click="createSession" :disabled="!connected" title="新对话">➕</button>
      <input v-model="input" @keyup.enter="send" :disabled="!connected" placeholder="说点什么…" />
      <button @click="send" :disabled="!connected">发送</button>
    </footer>
```

Add to the `<style scoped>` block:
```css
.new-btn {
  padding: .6rem 1rem; border: 0; border-radius: 12px; background: #fff;
  border: 1px solid #cbd5e1; color: #4f46e5; font-size: 1rem; cursor: pointer;
}
.new-btn:hover:not(:disabled) { background: #f1f5f9; }
.new-btn:disabled { opacity: .5; cursor: not-allowed; }
```

- [ ] **Step 2: Build to verify**

Run: `cd web && npm run build`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add web/src/components/ChatPanel.vue
git commit -m "web: ➕ new-session button beside the chat input"
```

---

### Task 7: `SessionsView` 3-pane (list | file tree | preview) + delete `SessionSidebar`

**Files:**
- Create: `web/src/components/SessionListPane.vue`
- Create: `web/src/components/FileTreePane.vue`
- Create: `web/src/components/FilePreviewPane.vue`
- Modify: `web/src/components/SessionsView.vue`
- Delete: `web/src/components/SessionSidebar.vue`

**Interfaces:**
- Consumes: `useSessions` `sessions`/`selectedSessionId`/`sessionFiles`/`previewFile`/`previewContent`/`previewLoading`/`historyAsBubbles`/`loadSessionFiles`/`readSessionFile`/`deleteSession`/`restoreSession`/`fromHistory`.
- Produces: a 3-pane Sessions page; the old `SessionSidebar.vue` is removed (its session-list role is now `SessionListPane`).

- [ ] **Step 1: Create `SessionListPane.vue`**

```vue
<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
const { sessions, selectedSessionId, loadSessionFiles, deleteSession, restoreSession, connected } = useSessions()

function relTime(ts: number): string {
  if (!ts) return ''
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return '刚刚'
  if (s < 3600) return Math.floor(s / 60) + '分钟前'
  if (s < 86400) return Math.floor(s / 3600) + '小时前'
  return Math.floor(s / 86400) + '天前'
}
</script>

<template>
  <section class="pane list-pane">
    <div class="pane-head">
      <span>历史会话</span>
      <button class="restore-btn"
        :disabled="!selectedSessionId || !connected"
        @click="selectedSessionId && restoreSession(selectedSessionId)">↩ 恢复</button>
    </div>
    <ul class="sess-list">
      <li v-for="s in sessions" :key="s.session_id"
          :class="['sess-item', { active: s.session_id === selectedSessionId }]"
          @click="loadSessionFiles(s.session_id)">
        <div class="sess-main">
          <div class="sess-title">{{ s.title || '(无标题)' }}</div>
          <div class="sess-meta">{{ relTime(s.last_message_at) }} · {{ s.message_count }}条</div>
        </div>
        <span class="sess-del" @click.stop="deleteSession(s.session_id)">✕</span>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; min-height: 0; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.list-pane { flex: 1 1 0; min-width: 0; }
.pane-head { display: flex; justify-content: space-between; align-items: center; padding: .7rem .85rem; border-bottom: 1px solid #e2e8f0; font-weight: 600; font-size: .9rem; color: #1e293b; }
.restore-btn { border: 0; border-radius: 8px; background: #4f46e5; color: #fff; padding: .3rem .6rem; font-size: .78rem; cursor: pointer; }
.restore-btn:disabled { background: #cbd5e1; cursor: not-allowed; }
.sess-list { list-style: none; margin: 0; padding: .35rem; overflow-y: auto; flex: 1; }
.sess-item { display: flex; align-items: center; gap: .4rem; padding: .5rem; border-radius: 8px; cursor: pointer; }
.sess-item:hover { background: #f1f5f9; }
.sess-item.active { background: #eef2ff; }
.sess-main { flex: 1; min-width: 0; }
.sess-title { font-size: .85rem; color: #1e293b; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sess-meta { font-size: .72rem; color: #94a3b8; margin-top: .1rem; }
.sess-del { color: #cbd5e1; flex-shrink: 0; }
.sess-del:hover { color: #ef4444; }
</style>
```

- [ ] **Step 2: Create `FileTreePane.vue`**

```vue
<script setup lang="ts">
import { useSessions } from '../composables/useSessions'
const { sessionFiles, previewFile, readSessionFile, selectedSessionId, previewLoading } = useSessions()
</script>

<template>
  <section class="pane tree-pane">
    <div class="pane-head"><span>文件</span></div>
    <ul class="file-list">
      <li v-if="!selectedSessionId" class="empty">先选一个会话</li>
      <li v-else v-for="f in sessionFiles" :key="f.name"
          :class="['file-item', { active: f.name === previewFile, dir: f.is_dir }]"
          @click="!f.is_dir && readSessionFile(selectedSessionId, f.name)">
        <span class="icon">{{ f.is_dir ? '📁' : '📄' }}</span>
        <span class="name">{{ f.name }}</span>
        <span v-if="f.name === previewFile && previewLoading" class="load">…</span>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; min-height: 0; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.tree-pane { flex: 1 1 0; min-width: 0; }
.pane-head { padding: .7rem .85rem; border-bottom: 1px solid #e2e8f0; font-weight: 600; font-size: .9rem; }
.file-list { list-style: none; margin: 0; padding: .35rem; overflow-y: auto; flex: 1; }
.file-item { display: flex; align-items: center; gap: .4rem; padding: .4rem .5rem; border-radius: 8px; cursor: pointer; font-size: .82rem; color: #334155; }
.file-item:hover { background: #f1f5f9; }
.file-item.active { background: #eef2ff; color: #4f46d5; }
.file-item.dir { color: #94a3b8; cursor: default; }
.icon { width: 1.1em; }
.name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.load { color: #94a3b8; }
.empty { padding: 1rem; color: #94a3b8; font-size: .82rem; }
</style>
```

- [ ] **Step 3: Create `FilePreviewPane.vue`**

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useSessions } from '../composables/useSessions'

const { previewFile, previewContent, previewLoading, historyAsBubbles, fromHistory } = useSessions()

const isHistory = computed(() => previewFile.value === 'history.json')
const isJson = computed(() => previewFile.value?.endsWith('.json'))
const formattedJson = computed(() => {
  try { return JSON.stringify(JSON.parse(previewContent.value), null, 2) } catch { return previewContent.value }
})
const bubbles = computed(() => isHistory.value ? fromHistory(
  previewContent.value.split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l))
) : [])
</script>

<template>
  <section class="pane preview-pane">
    <div class="pane-head">
      <span>{{ previewFile || '预览' }}</span>
      <label v-if="isHistory" class="toggle">
        <input type="checkbox" v-model="historyAsBubbles" />
        聊天气泡
      </label>
    </div>
    <div class="preview-body">
      <div v-if="previewLoading" class="state">加载中…</div>
      <div v-else-if="!previewFile" class="state">选一个文件查看</div>
      <div v-else-if="isHistory && historyAsBubbles" class="bubbles">
        <div v-for="(m, i) in bubbles" :key="i" :class="['row', m.role]">
          <div v-if="m.role === 'tool'" class="tool-line">{{ m.content }}</div>
          <div v-else class="bubble">{{ m.content }}</div>
        </div>
      </div>
      <pre v-else class="json">{{ formattedJson }}</pre>
    </div>
  </section>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; min-height: 0; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.preview-pane { flex: 3 1 0; min-width: 0; }
.pane-head { display: flex; justify-content: space-between; align-items: center; padding: .7rem .85rem; border-bottom: 1px solid #e2e8f0; font-weight: 600; font-size: .9rem; }
.toggle { font-size: .78rem; font-weight: 400; color: #64748b; display: flex; align-items: center; gap: .3rem; }
.preview-body { flex: 1; overflow: auto; padding: 1rem; }
.state { color: #94a3b8; }
.json { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, monospace; font-size: .8rem; color: #1e293b; }
.bubbles { display: flex; flex-direction: column; gap: .55rem; }
.row { display: flex; }
.row.user { justify-content: flex-end; }
.row.assistant { justify-content: flex-start; }
.bubble { max-width: 80%; padding: .5rem .8rem; border-radius: 14px; white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
.row.user .bubble { background: #4f46d5; color: #fff; border-bottom-right-radius: 4px; }
.row.assistant .bubble { background: #f1f5f9; color: #1e293b; border-bottom-left-radius: 4px; }
.tool-line { font-family: ui-monospace, monospace; font-size: .78rem; color: #94a3b8; padding: .15rem .4rem; }
</style>
```

- [ ] **Step 4: Rewrite `SessionsView.vue` (replace the placeholder)**

```vue
<script setup lang="ts">
import SessionListPane from './SessionListPane.vue'
import FileTreePane from './FileTreePane.vue'
import FilePreviewPane from './FilePreviewPane.vue'
import { useSessions } from '../composables/useSessions'
const { sessions, loadSessions } = useSessions()
// ensure the list is fresh when entering the page
loadSessions()
</script>

<template>
  <div class="sessions-view">
    <SessionListPane />
    <FileTreePane />
    <FilePreviewPane />
  </div>
</template>

<style scoped>
.sessions-view {
  flex: 1; display: grid; grid-template-columns: 1fr 1fr 3fr; gap: .75rem;
  padding: .75rem; min-height: 0; min-width: 0;
}
@media (max-width: 900px) { .sessions-view { grid-template-columns: 1fr; grid-auto-rows: auto; } }
</style>
```

- [ ] **Step 5: Delete the old `SessionSidebar.vue`**

```bash
git rm web/src/components/SessionSidebar.vue
```

(Confirm nothing imports it — `App.vue` no longer does after Task 5.)

- [ ] **Step 6: Build to verify**

Run: `cd web && npm run build`
Expected: clean. If `SessionSidebar` is still referenced anywhere, remove that import (it shouldn't be — App.vue was rewritten in Task 5).

- [ ] **Step 7: Manual e2e**

Start backends + `npm run dev`; exercise: 聊天页 ➕ 新建 + 发消息 → 切到 🗂 会话页 → 看到会话列表 → 点会话 → 文件树出 metadata.json/history.json → 点 history.json → 气泡预览 + 切原始 JSON → 点 metadata.json → 格式化 JSON → 「↩ 恢复」→ 回聊天页看到历史。If the env can't run the stack, list the exact commands for the operator.

- [ ] **Step 8: Commit**

```bash
git add web/src/components/SessionsView.vue web/src/components/SessionListPane.vue web/src/components/FileTreePane.vue web/src/components/FilePreviewPane.vue
git rm web/src/components/SessionSidebar.vue  # if not already staged
git commit -m "web: 3-pane Sessions page (list | files | preview); drop SessionSidebar"
```

---

### Task 8: Docs

**Files:**
- Modify: `docs/architecture.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `docs/architecture.md` §4.7**

Add to the session-persistence section: the two file-browser RPCs (`session.files` → flat file list of a session dir; `file.read` → path-traversal-safe single-file read) and the nav-shell frontend (LeftNav with Chat/Sessions entries switching an `activeNav` composable field; SessionsView = 3-pane list/tree/preview with `history.json` chat-bubble toggle and `metadata.json` formatted JSON). Cross-link the spec + plan.

- [ ] **Step 2: Update `CLAUDE.md`**

In the `session_rpc.py` bullet, extend the RPC list to include `session.files`/`file.read`. In the frontend conventions (or a new line), note the nav-shell layout (`LeftNav` + `ChatView`/`SessionsView` switched by `useSessions.activeNav`) replacing the old always-visible `SessionSidebar`.

- [ ] **Step 3: Commit**

```bash
git add docs/architecture.md CLAUDE.md
git commit -m "docs: sessions-page file-browser RPCs + nav-shell frontend"
```

---

## Self-Review (run after writing)

**1. Spec coverage:**
- `list_files` + path-safe `read_file` — Task 1. ✓
- `session.files` + `file.read` dispatch — Task 2. ✓
- ws-path framing test for new RPCs — Task 3. ✓
- `activeNav`/`setNav` + sessions-page state + 3 methods — Task 4. ✓
- nav shell + LeftNav + ChatView/SessionsView v-if — Task 5. ✓
- ➕ new-session button beside input — Task 6. ✓
- 3-pane SessionsView + panes + delete SessionSidebar — Task 7. ✓
- history.json bubble/JSON toggle + metadata.json formatted — Task 7 Step 3. ✓
- docs — Task 8. ✓

**2. Placeholder scan:** no TBD/TODO except the Task-7 placeholder `SessionsView` which Task 5 Step 3 explicitly creates then Task 7 replaces — that's a staged build, not a placeholder. Manual-e2e steps give exact commands. No vague steps.

**3. Type/signature consistency:**
- `list_files(sid) -> list[dict]` (`{name,is_dir,size}`) matches test assertions, rpc `body["files"]`, and `useSessions.sessionFiles` type `{name,is_dir,size}[]`. ✓
- `read_file(sid, name) -> str` matches rpc `body["content"]` and `useSessions.previewContent: Ref<string>`. ✓
- `loadSessionFiles(sid)` / `readSessionFile(sid,name)` / `restoreSession(sid)` defined in Task 4, consumed in Task 7. ✓
- `activeNav`/`setNav` defined in Task 4, consumed in Task 5 (LeftNav/App) and Task 7 (SessionsView back-button removed in final). ✓
- `fromHistory` exported in Task 4, consumed in Task 7 FilePreviewPane. ✓
- `createSession` (existing) consumed in Task 6. ✓

No issues found.
