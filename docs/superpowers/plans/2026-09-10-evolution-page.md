# Skill 进化管理页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> ⚠ **Commit 规矩**:本计划含 commit 步骤;按项目约定 commit 前需用户确认、push 需用户明说。执行时在每个 commit 点先问用户,再执行 `git commit`。切勿自行 `git push`。

**Goal:** 给已落地的 skill 自进化后端 6 RPC 补一个「🧬 进化」前端管理页,并修两个阻断该页的后端 bug(路由放行 + `get_pending` 方法名),使人能在页面上看经验记录/待批、审批/拒绝、蒸馏。

**Architecture:** 后端两处 1 行修复让 `evolve_list`/`evolve_pending` 两个只读 RPC 真正可达 + 调对方法;前端纯加法——`useSessions.ts` 加 refs/动作、新建 `EvolutionView.vue`、`App.vue` 加导航项。页面调 5 个 RPC(`evolve_list`/`evolve_pending`/`evolve_approve`/`evolve_reject`/`evolve_simplify`);`skills.evolve` 不进 v1(见设计 §10)。请求经既有 `webClient.request` → `e2a.result` → 浏览器 `result` 事件。

**Tech Stack:** Python pytest(后端)、Vue 3 `@script setup lang="ts"` + Vite + `vue-tsc`(前端,无前端测试框架,靠类型检查)。

**Spec:** [`docs/design/evolution-page-design.md`](../../docs/design/evolution-page-design.md)

---

## Task 1: 修 `evolve_pending` 方法名(rpc.py)+ 单元测试

`_dispatch_evolve_pending` 调 `orch.get_pending(name)`,但编排器方法是 `get_staged_records`([`orchestrator.py:153`](../../twinkle/agentserver/evolution/orchestrator.py#L153))。先写失败测试,再改一行。

**Files:**
- Modify: `twinkle/agentserver/skills/rpc.py:164`
- Test: `tests/test_skill_rpc.py`(加 2 个测试)

- [ ] **Step 1: 写失败测试** — 在 `tests/test_skill_rpc.py` 末尾追加:

```python
def test_evolve_list_returns_empty_when_no_records(tmp_path):
    """evolve_list 内联 dispatch:无 evolutions.json 的 skill 返空 records。"""
    from twinkle.agentserver.evolution import EvolutionStore, _set_evolution_store
    (tmp_path / "skills" / "foo").mkdir(parents=True)
    _set_evolution_store(EvolutionStore(str(tmp_path / "skills")))
    try:
        frames = _run(_frames(_env("skills.evolve_list", params={"name": "foo"})))
    finally:
        _set_evolution_store(None)
    assert len(frames) == 1
    f = frames[0]
    assert f.response_kind == "e2a.result"
    assert f.body["type"] == "skills.evolve_list"
    assert f.body["skill_name"] == "foo"
    assert f.body["records"] == []


def test_evolve_pending_returns_empty_when_no_staged(tmp_path):
    """evolve_pending 内联 dispatch:无待批返空 pending(调 get_staged_records,非 get_pending)。"""
    from twinkle.agentserver.evolution import (
        EvolutionStore, OnlineEvolutionOrchestrator, ConversationSignalDetector,
        _set_evolution_store, _set_orchestrator,
    )
    evo_store = EvolutionStore(str(tmp_path / "skills"))
    orch = OnlineEvolutionOrchestrator(
        store=evo_store, optimizer=None, scorer=None,
        detector=ConversationSignalDetector())
    _set_evolution_store(evo_store)
    _set_orchestrator(orch)
    try:
        frames = _run(_frames(_env("skills.evolve_pending", params={})))
    finally:
        _set_evolution_store(None)
        _set_orchestrator(None)
    assert len(frames) == 1
    f = frames[0]
    assert f.response_kind == "e2a.result"
    assert f.body["type"] == "skills.evolve_pending"
    assert f.body["pending"] == {}
```

> `OnlineEvolutionOrchestrator.__init__` 仅赋值不调用 optimizer/scorer([orchestrator.py:37-41](../../twinkle/agentserver/evolution/orchestrator.py#L37-L41));`get_staged_records` 只读 `self._staged_records`([:153-157](../../twinkle/agentserver/evolution/orchestrator.py#L153-L157)),故 `optimizer=None` 安全、无 LLM。

- [ ] **Step 2: 跑测试看失败**

Run: `python -m pytest tests/test_skill_rpc.py::test_evolve_pending_returns_empty_when_no_staged -v`
Expected: FAIL — handler 抛 `AttributeError: 'OnlineEvolutionOrchestrator' object has no attribute 'get_pending'`,被 `dispatch_skill_rpc` 的 try/except([rpc.py:56-58](../../twinkle/agentserver/skills/rpc.py#L56-L58))兜成 `status="failed"` 帧,`f.body` 无 `pending` 键 → `assert f.body["pending"] == {}` KeyError。
(`test_evolve_list_returns_empty_when_no_records` 此时本应 PASS——`evolve_list` handler 无 bug;它是 characterization 测试,确认契约。)

- [ ] **Step 3: 改一行实现**

`twinkle/agentserver/skills/rpc.py:164`:

```python
# 前
    pending = orch.get_pending(name)
# 后
    pending = orch.get_staged_records(name)
```

- [ ] **Step 4: 跑测试看通过**

Run: `python -m pytest tests/test_skill_rpc.py::test_evolve_pending_returns_empty_when_no_staged tests/test_skill_rpc.py::test_evolve_list_returns_empty_when_no_records -v`
Expected: 2 PASS。

- [ ] **Step 5: 跑全 skill rpc 测试防回归**

Run: `python -m pytest tests/test_skill_rpc.py -v`
Expected: 全 PASS(原 16 个 + 新 2 个)。

- [ ] **Step 6: 确认后 commit(先问用户)**

```bash
git add twinkle/agentserver/skills/rpc.py tests/test_skill_rpc.py
git commit -m "fix(evolution): evolve_pending 调 get_staged_records(原 get_pending 不存在)"
```

---

## Task 2: 修路由放行(server.py)+ ws 级测试

`server.py:190` 只把 `skills.list_local` 放行内联,其余 skill 方法(含 `evolve_list`/`evolve_pending`)走后台 `run_skill_rpc`,后者无这两个分支 → `"unknown skill method"` 错。先写 ws 级失败测试,再改路由条件。

**Files:**
- Modify: `twinkle/agentserver/server.py:190`
- Test: `tests/test_agentserver_handler.py`(加 2 个 ws 级测试)

- [ ] **Step 1: 写失败测试** — 在 `tests/test_agentserver_handler.py` 末尾追加:

```python
def test_skill_evolve_list_routes_inline(tmp_path) -> None:
    """skills.evolve_list 由 ws_handler 内联路由(改前走后台分支报 unknown skill method)。"""
    from twinkle.agentserver.evolution import EvolutionStore, _set_evolution_store
    port = _free_port()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = _RecordingLoop(store)
    sk_dir = tmp_path / "skills" / "foo"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\n", encoding="utf-8")
    _set_evolution_store(EvolutionStore(str(tmp_path / "skills")))
    try:
        async def run() -> None:
            server = await serve(ws_handler(loop_obj), "127.0.0.1", port)
            try:
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.recv()  # connection.ack
                    env = E2AEnvelope(
                        request_id="r1", session_id="s1", method="skills.evolve_list",
                        params={"name": "foo"},
                    )
                    await ws.send(env.model_dump_json())
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(raw)
                    assert data["response_kind"] == "e2a.result"
                    assert data["body"]["type"] == "skills.evolve_list"
                    assert data["body"]["skill_name"] == "foo"
                    assert data["body"]["records"] == []
                assert loop_obj.seen is None  # 内联短路,不到 ReAct loop
            finally:
                server.close()
                await server.wait_closed()
        asyncio.run(run())
    finally:
        _set_evolution_store(None)


def test_skill_evolve_pending_routes_inline(tmp_path) -> None:
    """skills.evolve_pending 由 ws_handler 内联路由。"""
    from twinkle.agentserver.evolution import (
        EvolutionStore, OnlineEvolutionOrchestrator, ConversationSignalDetector,
        _set_evolution_store, _set_orchestrator,
    )
    port = _free_port()
    store = SessionStore(str(tmp_path / "sessions"))
    loop_obj = _RecordingLoop(store)
    evo_store = EvolutionStore(str(tmp_path / "skills"))
    orch = OnlineEvolutionOrchestrator(
        store=evo_store, optimizer=None, scorer=None,
        detector=ConversationSignalDetector())
    _set_evolution_store(evo_store)
    _set_orchestrator(orch)
    try:
        async def run() -> None:
            server = await serve(ws_handler(loop_obj), "127.0.0.1", port)
            try:
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.recv()  # connection.ack
                    env = E2AEnvelope(
                        request_id="r2", session_id="s2", method="skills.evolve_pending",
                        params={},
                    )
                    await ws.send(env.model_dump_json())
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(raw)
                    assert data["response_kind"] == "e2a.result"
                    assert data["body"]["type"] == "skills.evolve_pending"
                    assert data["body"]["pending"] == {}
                assert loop_obj.seen is None
            finally:
                server.close()
                await server.wait_closed()
        asyncio.run(run())
    finally:
        _set_evolution_store(None)
        _set_orchestrator(None)
```

- [ ] **Step 2: 跑测试看失败**

Run: `python -m pytest tests/test_agentserver_handler.py::test_skill_evolve_list_routes_inline tests/test_agentserver_handler.py::test_skill_evolve_pending_routes_inline -v`
Expected: 2 FAIL — 两方法走 `run_skill_rpc` 的 `else`([rpc.py:127-128](../../twinkle/agentserver/skills/rpc.py#L127-L128))返回 `{type, error: "unknown skill method: skills.evolve_list"}`、`status="failed"`,断言 `data["body"]["type"] == "skills.evolve_list"` 仍成立但 `data["response_kind"]`/`status` 不符(实际 `response_kind` 是 `e2a.result` 但 `status="failed"` 且 body 无 `records`/`pending` 键)→ `assert data["body"]["records"] == []` / `["pending"]` KeyError。

> 注:`E2AResponse.status` 字段;失败帧仍 `response_kind="e2a.result"` + `status="failed"` + `body={"error":...}`。两条断言里对 `body["records"]`/`body["pending"]` 的访问会因缺键而 KeyError,标志失败。

- [ ] **Step 3: 改路由条件**

`twinkle/agentserver/server.py:190`:

```python
# 前
                if handles_skill_rpc(envelope.method):
                    if envelope.method == "skills.list_local":
                        async for frame in dispatch_skill_rpc(envelope):
                            await send(frame)
# 后
                if handles_skill_rpc(envelope.method):
                    if envelope.method in ("skills.list_local", "skills.evolve_list", "skills.evolve_pending"):
                        async for frame in dispatch_skill_rpc(envelope):
                            await send(frame)
```

- [ ] **Step 4: 跑测试看通过**

Run: `python -m pytest tests/test_agentserver_handler.py::test_skill_evolve_list_routes_inline tests/test_agentserver_handler.py::test_skill_evolve_pending_routes_inline -v`
Expected: 2 PASS。

- [ ] **Step 5: 跑全 handler 测试防回归**

Run: `python -m pytest tests/test_agentserver_handler.py -v`
Expected: 全 PASS(原 3 个 + 新 2 个)。

- [ ] **Step 6: 确认后 commit(先问用户)**

```bash
git add twinkle/agentserver/server.py tests/test_agentserver_handler.py
git commit -m "fix(evolution): 放行 evolve_list/evolve_pending 走内联 dispatch(原误入后台分支报 unknown)"
```

---

## Task 3: 前端状态层 useSessions.ts(refs + 动作)

加进化页所需 refs/动作/类型,照搬 `loadInstalled`/`installSkill` 的 `client.request` 写法。无前端测试框架,靠 Task 6 的 `vue-tsc` 类型检查。

**Files:**
- Modify: `web/src/composables/useSessions.ts`

- [ ] **Step 1: 扩 NavKey 类型**

`web/src/composables/useSessions.ts:38`:

```ts
// 前
type NavKey = 'chat' | 'sessions' | 'skills'
// 后
type NavKey = 'chat' | 'sessions' | 'skills' | 'evolution'
```

- [ ] **Step 2: 加类型接口** — 在 `export interface InstalledSkill {...}`(L23)之后加:

```ts
export interface EvolveRecord {
  id: string
  source: string
  score: number
  section: string
  summary: string
  used: number
  positive: number
}
export interface EvolvePendingItem {
  id: string
  source: string
  section: string
  summary: string
}
```

> 字段对齐后端契约:`evolve_list` 返回 `records:[{id,source,score,section,summary,used,positive}]`([rpc.py:148-153](../../twinkle/agentserver/skills/rpc.py#L148-L153));`evolve_pending` 返回 `pending:{skill:[{id,source,section,summary}]}`([rpc.py:167-172](../../twinkle/agentserver/skills/rpc.py#L167-L172))。

- [ ] **Step 3: 加 refs** — 在 `const skillsError = ref<string | null>(null)`(L51)之后加:

```ts
const evolveRecords = ref<EvolveRecord[]>([])
const evolvePending = ref<Record<string, EvolvePendingItem[]>>({})
const evolveSelectedSkill = ref<string>('')
const evolveRecordsLoading = ref(false)
const evolvePendingLoading = ref(false)
const evolveActionLoading = ref(false)
const evolveError = ref<string | null>(null)
```

- [ ] **Step 4: 加动作函数** — 在 `async function uninstallSkill(...)` 整个函数(L189-201)之后、`function sendQuery`(L203)之前加:

```ts
async function loadEvolveRecords(name: string) {
  if (!name) { evolveRecords.value = []; return }
  evolveRecordsLoading.value = true
  evolveError.value = null
  try {
    const payload = await client.request('skills.evolve_list', { name })
    evolveRecords.value = payload?.records ?? []
  } catch (e: any) {
    evolveRecords.value = []
    evolveError.value = e?.message || '加载经验记录失败'
  } finally {
    evolveRecordsLoading.value = false
  }
}

async function loadEvolvePending() {
  evolvePendingLoading.value = true
  evolveError.value = null
  try {
    const payload = await client.request('skills.evolve_pending', {})
    evolvePending.value = payload?.pending ?? {}
  } catch (e: any) {
    evolvePending.value = {}
    evolveError.value = e?.message || '加载待批失败'
  } finally {
    evolvePendingLoading.value = false
  }
}

/** approve/reject/simplify 成功后刷新 inbox(+选中 skill 的 records)。 */
async function refreshEvolve() {
  await Promise.all([loadEvolvePending(), loadEvolveRecords(evolveSelectedSkill.value)])
}

async function approveEvolve(name: string, ids: string[] | null) {
  evolveActionLoading.value = true
  evolveError.value = null
  try {
    await client.request('skills.evolve_approve', { name, record_ids: ids }, 60000)
    await refreshEvolve()
  } catch (e: any) {
    evolveError.value = e?.message || '批准失败'
  } finally {
    evolveActionLoading.value = false
  }
}

async function rejectEvolve(name: string, ids: string[] | null) {
  evolveActionLoading.value = true
  evolveError.value = null
  try {
    await client.request('skills.evolve_reject', { name, record_ids: ids }, 60000)
    await refreshEvolve()
  } catch (e: any) {
    evolveError.value = e?.message || '拒绝失败'
  } finally {
    evolveActionLoading.value = false
  }
}

async function simplifyEvolve(name: string) {
  if (!name) return
  evolveActionLoading.value = true
  evolveError.value = null
  try {
    await client.request('skills.evolve_simplify', { name }, 180000)
    await loadEvolveRecords(name)
  } catch (e: any) {
    evolveError.value = e?.message || '蒸馏失败'
  } finally {
    evolveActionLoading.value = false
  }
}

function selectEvolveSkill(name: string) {
  evolveSelectedSkill.value = name
  loadEvolveRecords(name)
}
```

> 超时:只读 15s(`request` 默认)、approve/reject 60s、simplify 180s(对齐 `installSkill` 的 180000)。`record_ids: null` = 全批/全拒(后端 `get("record_ids") or None` → None;见 [rpc.py:216](../../twinkle/agentserver/skills/rpc.py#L216))。`client.request` 的 `send` 自动带 `session_id`([webClient.ts:115](../../web/src/services/webClient.ts#L115)),进化 RPC 忽略它,无害。

- [ ] **Step 5: 在 `useSessions()` 返回对象加导出** — 在 `searchSkills, loadInstalled, clearSearch, installSkill, uninstallSkill,`(L311)这一行后追加一行:

```ts
    evolveRecords, evolvePending, evolveSelectedSkill,
    evolveRecordsLoading, evolvePendingLoading, evolveActionLoading, evolveError,
    loadEvolveRecords, loadEvolvePending, approveEvolve, rejectEvolve,
    simplifyEvolve, selectEvolveSkill,
```

- [ ] **Step 6: 类型检查**

Run: `cd web && npx vue-tsc --noEmit`
Expected: 无错误退出码 0(若 `vue-tsc` 未装先 `npm install`)。

- [ ] **Step 7: 确认后 commit(先问用户)**

```bash
git add web/src/composables/useSessions.ts
git commit -m "feat(evolution): useSessions 加进化页 refs/动作(5 RPC)"
```

---

## Task 4: 新建 EvolutionView.vue

照 [`SkillsView.vue`](../../web/src/components/SkillsView.vue) 结构 + scoped CSS 同风格。三段:picker / 待批 inbox / 经验记录 + 蒸馏按钮。

**Files:**
- Create: `web/src/components/EvolutionView.vue`

- [ ] **Step 1: 创建组件文件** `web/src/components/EvolutionView.vue`,内容:

```vue
<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useSessions } from '../composables/useSessions'

const {
  installedSkills, evolveRecords, evolvePending, evolveSelectedSkill,
  evolveRecordsLoading, evolvePendingLoading, evolveActionLoading, evolveError,
  loadInstalled, loadEvolvePending, loadEvolveRecords,
  approveEvolve, rejectEvolve, simplifyEvolve, selectEvolveSkill,
} = useSessions()

interface PendingRow {
  skill: string
  id: string
  source: string
  section: string
  summary: string
}
// pending(按 skill 分组)展平成行
const pendingRows = computed<PendingRow[]>(() => {
  const rows: PendingRow[] = []
  for (const [skill, recs] of Object.entries(evolvePending.value)) {
    for (const r of recs) rows.push({ skill, id: r.id, source: r.source, section: r.section, summary: r.summary })
  }
  return rows
})
const pendingCount = computed(() => pendingRows.value.length)
const pendingSkills = computed(() => Object.keys(evolvePending.value))

onMounted(async () => {
  loadEvolvePending()
  if (!installedSkills.value.length) await loadInstalled()
  if (!evolveSelectedSkill.value && installedSkills.value.length) {
    selectEvolveSkill(installedSkills.value[0].name)
  }
})

function onPick(e: Event) {
  const name = (e.target as HTMLSelectElement).value
  if (name) selectEvolveSkill(name)
}
function approveOne(row: PendingRow) { approveEvolve(row.skill, [row.id]) }
function rejectOne(row: PendingRow) { rejectEvolve(row.skill, [row.id]) }
function approveAll(skill: string) { approveEvolve(skill, null) }
function rejectAll(skill: string) { rejectEvolve(skill, null) }
</script>

<template>
  <div class="evolve-view">
    <section class="picker">
      <h3>🧬 进化</h3>
      <select :value="evolveSelectedSkill" @change="onPick" :disabled="!installedSkills.length">
        <option value="" disabled>选择 skill…</option>
        <option v-for="s in installedSkills" :key="s.name" :value="s.name">{{ s.name }}</option>
      </select>
      <button class="ghost" @click="loadEvolveRecords(evolveSelectedSkill)"
              :disabled="!evolveSelectedSkill || evolveRecordsLoading">
        {{ evolveRecordsLoading ? '…' : '刷新记录' }}
      </button>
    </section>

    <section class="pending">
      <h3>📥 待批 ({{ pendingCount }})
        <span class="warn">⚠ 待批存于内存,服务重启丢失</span>
      </h3>
      <ul>
        <li v-for="row in pendingRows" :key="row.skill + row.id">
          <div class="meta">
            <strong>{{ row.skill }}</strong> · <span>{{ row.id }}</span> ·
            <span>{{ row.source }}</span> · <span>{{ row.summary }}</span>
          </div>
          <div class="actions">
            <button class="ok" :disabled="evolveActionLoading" @click="approveOne(row)">✓ 批准</button>
            <button class="ghost" :disabled="evolveActionLoading" @click="rejectOne(row)">✗ 拒绝</button>
          </div>
        </li>
        <li v-if="!pendingCount && !evolvePendingLoading" class="empty">无待批记录</li>
      </ul>
      <div class="bulk" v-for="skill in pendingSkills" :key="'bulk-' + skill">
        <span>{{ skill }} 批量:</span>
        <button class="ok" :disabled="evolveActionLoading" @click="approveAll(skill)">全部批准</button>
        <button class="ghost" :disabled="evolveActionLoading" @click="rejectAll(skill)">全部拒绝</button>
      </div>
    </section>

    <section class="records">
      <h3>📊 经验记录: {{ evolveSelectedSkill || '—' }} ({{ evolveRecords.length }})</h3>
      <ul>
        <li v-for="r in evolveRecords" :key="r.id">
          <div class="meta">
            <strong>{{ r.id }}</strong> · <span class="score">{{ r.score.toFixed(2) }}</span> ·
            <span>{{ r.section }}</span> · <span>used {{ r.used }} / +{{ r.positive }}</span>
          </div>
          <span class="summary">{{ r.summary }}</span>
        </li>
        <li v-if="!evolveRecords.length && !evolveRecordsLoading" class="empty">该 skill 暂无经验记录</li>
      </ul>
    </section>

    <section class="actions-bar">
      <button :disabled="!evolveSelectedSkill || evolveActionLoading"
              @click="simplifyEvolve(evolveSelectedSkill)">
        {{ evolveActionLoading ? '处理中…' : '🧹 蒸馏清理 ' + (evolveSelectedSkill || '') }}
      </button>
    </section>

    <p v-if="evolveError" class="error">{{ evolveError }}</p>
  </div>
</template>

<style scoped>
.evolve-view {
  flex: 1; display: flex; flex-direction: column; gap: 1rem;
  padding: 1rem; min-height: 0; overflow: auto;
}
.picker { display: flex; align-items: center; gap: .6rem; }
.picker h3 { margin: 0; font-size: .95rem; color: #1e293b; }
select {
  padding: .45rem .6rem; border: 1px solid #cbd5e1;
  border-radius: 8px; font-size: .85rem; background: #fff;
}
h3 { margin: 0 0 .5rem; font-size: .9rem; color: #1e293b; }
.warn { font-size: .72rem; color: #b45309; margin-left: .5rem; font-weight: 400; }
ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .4rem; }
li {
  display: flex; align-items: center; justify-content: space-between; gap: .5rem;
  padding: .5rem .6rem; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
}
li .meta { min-width: 0; }
li strong { font-size: .85rem; color: #1e293b; }
li span { font-size: .78rem; color: #64748b; }
li .summary { font-size: .78rem; color: #475569; }
.score { color: #2563eb; font-weight: 600; }
.empty { color: #94a3b8; font-size: .8rem; justify-content: center; }
.actions { display: flex; gap: .3rem; }
.bulk { display: flex; align-items: center; gap: .4rem; margin-top: .4rem; font-size: .8rem; color: #475569; }
button {
  border: 0; background: #4f46d5; color: #fff; border-radius: 8px;
  padding: .45rem .8rem; cursor: pointer; font-size: .8rem;
}
button:disabled { opacity: .5; cursor: not-allowed; }
button.ghost { background: #e2e8f0; color: #475569; }
button.ok { background: #16a34a; }
.actions-bar { margin-top: .2rem; }
.error { color: #dc2626; font-size: .78rem; margin: .3rem 0; }
</style>
```

- [ ] **Step 2: 类型检查**

Run: `cd web && npx vue-tsc --noEmit`
Expected: 退出码 0,无 `EvolutionView` 相关错误。

- [ ] **Step 3: 确认后 commit(先问用户)**

```bash
git add web/src/components/EvolutionView.vue
git commit -m "feat(evolution): 新建进化管理页 EvolutionView(待批/记录/蒸馏)"
```

---

## Task 5: App.vue 挂导航项

**Files:**
- Modify: `web/src/App.vue`

- [ ] **Step 1: 加 import** — 在 `import SkillsView from './components/SkillsView.vue'`(L6)之后加:

```ts
import EvolutionView from './components/EvolutionView.vue'
```

- [ ] **Step 2: 加导航按钮** — 在 `<button ... 🧩 技能</button>`(L23)之后加:

```vue
        <button :class="{ active: activeNav === 'evolution' }" @click="setNav('evolution')">🧬 进化</button>
```

- [ ] **Step 3: 加视图分支** — 在 `<SkillsView v-else-if="activeNav === 'skills'" />`(L27)之后加:

```vue
        <EvolutionView v-else-if="activeNav === 'evolution'" />
```

- [ ] **Step 4: 类型检查 + 启动冒烟**

Run: `cd web && npx vue-tsc --noEmit`
Expected: 退出码 0。

冒烟(可选,需后端起):`cd web && npm run dev` → 打开 http://localhost:5173 → 点侧栏「🧬 进化」→ 见 picker + 空待批 + 空记录 + 蒸馏按钮(无 console 报错)。

- [ ] **Step 5: 确认后 commit(先问用户)**

```bash
git add web/src/App.vue
git commit -m "feat(evolution): App.vue 加 🧬 进化 导航项与视图分支"
```

---

## Task 6: 全量验证

- [ ] **Step 1: 后端全量测试**

Run: `python -m pytest tests/test_skill_rpc.py tests/test_agentserver_handler.py -v`
Expected: 全 PASS。

- [ ] **Step 2: 前端类型检查**

Run: `cd web && npx vue-tsc --noEmit`
Expected: 退出码 0。

- [ ] **Step 3: 端到端冒烟(需后端 + 至少一个 skill)**

启后端:`python scripts/start_services.py`(或两个终端各起 agentserver/gateway)。
前端:`cd web && npm run dev` → http://localhost:5173
验收清单:
1. 点「🧬 进化」→ picker 列出已装 skill、待批/记录区加载无报错。
2. 选某 skill → 经验记录区显示其 records(无则显示空态)。
3. (有 pending 时)逐条 ✓/✗ → inbox 刷新、记录区更新。
4. (有 pending 时)按 skill「全部批准/全部拒绝」→ 刷新。
5. 「蒸馏清理」按钮 → loading → 完成后 records 号新。
6. 任一 RPC 失败(如停后端)→ 行内 `evolveError` 提示。
7. 空态:无 skill / 无 records / 无 pending 各显对应空文案。

- [ ] **Step 4: 更新 roadmap(可选)**

`roadmap.md` Phase 14 补一条「进化管理页落地」。先问用户是否要改 roadmap。

---

## Self-Review(计划写完自查)

1. **Spec 覆盖**:设计 §2.1 路由→Task 2;§2.2 get_pending→Task 1;§3 三处前端→Task 3/4/5;§5 refs/动作→Task 3;§4 布局→Task 4;§7 超时(15/60/180s)→Task 3 动作内;§8 测试→Task 1/2;§10 evolve 不做→未实现(正确);§9 YAGNI→未加新 RPC/未暴露死配置。✅ 全覆盖。
2. **Placeholder 扫描**:每步含完整代码/命令,无 TBD/TODO/"add error handling"。✅
3. **类型一致性**:`EvolveRecord`/`EvolvePendingItem` 字段(Task 3)与 Task 4 模板使用一致;`selectEvolveSkill`/`approveEvolve`/`rejectEvolve`/`simplifyEvolve`/`loadEvolveRecords`/`loadEvolvePending` 在 Task 3 定义、Task 4 解构使用,名称一致。✅
4. **额外发现**:Task 1 修的 `get_pending`→`get_staged_records` 是计划阶段新发现的阻断 bug(设计文档 §2.2 已补);`skills.evolve` 的 `skill_content=` 参数与 `evolve()` 签名不符也是 bug,但 evolve 不进 v1,不在本计划修(留待后续)。
