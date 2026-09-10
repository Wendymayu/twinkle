# Skill 进化管理页设计

> 给已落地的 skill 自进化后端(见 [skill-self-evolution-design.md](skill-self-evolution-design.md) §7 的 6 个 RPC)补一个**前端管理页**,承载人机协同核心:看经验记录、看待批、审批/拒绝、蒸馏。后端已建好,本设计只动 1 行后端路由 + 纯加法的前端页。
>
> **挂载点**:侧栏新增「🧬 进化」顶级导航页(与 💬聊天 / 🗂会话 / 🧩技能 并列)。理由:进化是一组独立关注点(6 RPC + 审批工作流),与「技能」页的消费侧(下载安装)职责不同;项目现有约定是「一关注点一页」,独立页扩展空间大。

## 1. 背景与范围

进化后端 Phase 14 已落地:6 个 RPC 经 [`skills/rpc.py`](../../twinkle/agentserver/skills/rpc.py) 暴露,但**无任何前端页面**调用它们——[`SkillsView.vue`](../../web/src/components/SkillsView.vue) 只覆盖 `list_local`/`search`/`install`/`uninstall`。本设计补齐这块。

### 1.1 一个必须先修的后端 bug

`evolve_list`(查经验记录+分数)和 `evolve_pending`(待批列表)是页面最依赖的两条只读路径,实现已写在 [`_dispatch_evolve_list`](../../twinkle/agentserver/skills/rpc.py#L137)/[`_dispatch_evolve_pending`](../../twinkle/agentserver/skills/rpc.py#L159)(内联 dispatch),**但路由不通**:[`server.py:189-199`](../../twinkle/agentserver/server.py#L189-L199) 的 skill RPC 路由里,只有 `skills.list_local` 走内联 `dispatch_skill_rpc`,**其余全部走后台 `run_skill_rpc`**;而 `run_skill_rpc` 的分支链没有这两个方法的 case,落到 `else` 返回 `"unknown skill method"` 错误。

→ 做页面前必须修这条路由(见 §2),否则页面拿不到记录和待批。

### 1.2 范围

| 在范围内 | 不在范围内(YAGNI) |
|---|---|
| 新建「🧬 进化」前端页 | 不加新 RPC(6 个够用,本设计只用 5 个) |
| 修 `evolve_list`/`evolve_pending` 路由(1 行) | 不暴露死配置旋钮(`auto_save`/scoring 权重/`trigger`,见 [skill-self-evolution-design.md](skill-self-evolution-design.md) §8,单例不读这些) |
| 调用 5 个 RPC(list/pending/approve/reject/simplify) | 不做 pending 持久化(v1 内存设计;只加静态提示) |
| approve/reject 逐条 + 批量 | 不做单条经验详情 RPC(`evolve_list` 投影够 v1 看) |
| 后端路由修复的 pytest | `skills.evolve` 手动触发(见 §10,默认不做) |

## 2. 后端改动(2 处,修两个阻断 bug)

### 2.1 路由放行(server.py:190)

[`server.py:190`](../../twinkle/agentserver/server.py#L190) 把内联放行条件从只认 `list_local` 扩到含两个只读进化方法:

```python
# 前
if envelope.method == "skills.list_local":
    async for frame in dispatch_skill_rpc(envelope):
        await send(frame)

# 后
if envelope.method in ("skills.list_local", "skills.evolve_list", "skills.evolve_pending"):
    async for frame in dispatch_skill_rpc(envelope):
        await send(frame)
```

`_dispatch_evolve_list` / `_dispatch_evolve_pending` 已在 [`rpc.py:137/159`](../../twinkle/agentserver/skills/rpc.py#L137) 实现,改完即通。其余 4 个后台 RPC(`evolve`/`approve`/`reject`/`simplify` 走 `run_skill_rpc`)本来就通,不动。Gateway 层无需改动——它已把任意 `e2a.result` 映射为浏览器 `result` 事件。

### 2.2 修 evolve_pending 的方法名(rpc.py:164)

`_dispatch_evolve_pending` 调 `orch.get_pending(name)`,但编排器的方法叫 `get_staged_records(skill_name=None)`([`orchestrator.py:153`](../../twinkle/agentserver/evolution/orchestrator.py#L153))——`get_pending` 不存在,会 `AttributeError`。改为:

```python
# 前(rpc.py:164)
pending = orch.get_pending(name)
# 后
pending = orch.get_staged_records(name)
```

> 调研发现:这是计划阶段新发现的第二处阻断 bug(设计文档 §1.1 原只提路由 bug)。两处都修,`evolve_pending` 才能真正跑通。

## 3. 前端架构(3 处,纯加法)

前端栈:Vue 3(`@script setup lang="ts"`),无路由(靠 `activeNav` ref 切换),状态全在单例 [`useSessions.ts`](../../web/src/composables/useSessions.ts),WS 请求靠 [`webClient.ts`](../../web/src/services/webClient.ts) 的 `request(method, params, timeout)`(收 `result` 事件 resolve、`payload.error` reject)。

| 改动文件 | 做什么 |
|---|---|
| [`useSessions.ts`](../../web/src/composables/useSessions.ts) | 加 refs + 动作函数(照搬 `loadInstalled`/`installSkill` 的 `client.request` 写法);`NavKey` 类型加 `'evolution'` |
| **新建** [`EvolutionView.vue`](../../web/src/components/EvolutionView.vue) | 照 [`SkillsView.vue`](../../web/src/components/SkillsView.vue) 结构 + scoped CSS 同风格 |
| [`App.vue`](../../web/src/App.vue) | 侧栏加 `🧬 进化` 按钮 + `<EvolutionView v-else-if="activeNav === 'evolution'" />` |

## 4. 页面布局与交互

```
┌─────────────────────────────────────────────────────┐
│ 🧬 进化                                              │
│ Skill: [web-scraper ▾]            [刷新记录]          │  ← picker 复用 installedSkills
├─────────────────────────────────────────────────────┤
│ 📥 待批 (跨所有 skill)        ⚠ 待批存于内存,服务重启丢失│
│  web-scraper · ev_9f2 · 故障 · "…"      [✓][✗]       │  ← approve/reject 逐条
│  web-scraper · ev_1b8 · 指令 · "…"      [✓][✗]       │     [全部批准] [全部拒绝]
├─────────────────────────────────────────────────────┤
│ 📊 经验记录: web-scraper (12)                        │  ← 选中 skill 的 records
│  ev_3a1 · 0.82 · Troubleshooting · used 3 / +2       │     (只读:分数/来源/section/摘要/used/positive)
│  ev_7c4 · 0.61 · Scripts          · used 1 / +0       │
├─────────────────────────────────────────────────────┤
│ [🧹 蒸馏清理 web-scraper]                            │  ← simplify,长耗时,loading 态
└─────────────────────────────────────────────────────┘
```

**交互流:**
- 进入页面:自动 `loadEvolvePending()`(全量待批)+ 确保 `installedSkills` 有值(复用 `loadInstalled()`,空则调一次)。
- 选 skill:自动 `loadEvolveRecords(name)`。
- **逐条 approve/reject**:传 `record_ids: [id]`;成功后刷新 inbox + 选中 skill 的 records。
- **批量 approve/reject**:对一个 skill 的全部待批,传 `record_ids: null`(或全选 ids);同样刷新。
- **蒸馏 simplify**:长 timeout + loading 态防双击;成功后刷新 records。
- 待批为空:inbox 显示「无待批」;records 为空:列表显示「该 skill 暂无经验记录」。

**两张提示(静态文案,非动态):**
- 待批区头部:「⚠ 待批存于内存,服务重启丢失」(对齐 [设计文档](skill-self-evolution-design.md) §5 v1 pending 不持久化)。
- 不做「evolution 未启用」banner:RPC 路径与 `evolution.enabled` 无关(`enabled` 只挡后台 Hook),页面在未启用时仍能查看/审批/蒸馏已有记录,空列表本身即传达状态。

## 5. 状态与动作(useSessions.ts)

### 5.1 新增 refs

| ref | 类型 | 作用 |
|---|---|---|
| `evolveRecords` | `EvolveRecord[]` | 选中 skill 的经验记录(来自 `evolve_list`) |
| `evolvePending` | `Record<string, PendingRecord[]>` | 按 skill 分组的待批(来自 `evolve_pending`,全量) |
| `evolveSelectedSkill` | `string` | picker 选中的 skill 名 |
| `evolveRecordsLoading` / `evolvePendingLoading` | `boolean` | 只读加载态 |
| `evolveActionLoading` | `boolean` | approve/reject/simplify 进行中(防双击) |
| `evolveError` | `string \| null` | 行内错误提示 |

> `NavKey` 类型扩展:`'chat' | 'sessions' | 'skills' | 'evolution'`。

### 5.2 新增动作(均 `client.request`)

| 动作 | method | params | timeout | 成功后 |
|---|---|---|---|---|
| `loadEvolveRecords(name)` | `skills.evolve_list` | `{name}` | 15s | set `evolveRecords` |
| `loadEvolvePending()` | `skills.evolve_pending` | `{}`(不传 name=全量) | 15s | set `evolvePending` |
| `approveEvolve(name, ids)` | `skills.evolve_approve` | `{name, record_ids: ids}` | 60s | 刷新 pending + 选中 skill records |
| `rejectEvolve(name, ids)` | `skills.evolve_reject` | `{name, record_ids: ids}` | 60s | 同上 |
| `simplifyEvolve(name)` | `skills.evolve_simplify` | `{name}` | 180s | 刷新 records |

> `ids` 为单元素数组(逐条)或 `null`(全批);`client.request` 的 params 会自动带 `session_id`([`webClient.ts`](../../web/src/services/webClient.ts) 现有行为)。

### 5.3 RPC 响应契约(后端已定,前端按此解析)

```ts
// skills.evolve_list
{ type: "skills.evolve_list", skill_name: string,
  records: [{ id, source, score, section, summary, used, positive }] }

// skills.evolve_pending
{ type: "skills.evolve_pending",
  pending: { [skill_name: string]: [{ id, source, section, summary }] } }

// skills.evolve_approve / reject / simplify
{ type: string, skill_name: string, status: string, message: string }
```

失败帧 body 带 `error`,`client.request` 因 `payload.error` reject(既有约定)。

## 6. 数据流

```
进入「🧬 进化」页
  ├─ loadEvolvePending()  ──> evolvePending(全量待批 inbox)
  └─ (installedSkills 空? loadInstalled())  ──> picker 选项

选 skill X
  └─ loadEvolveRecords(X)  ──> evolveRecords(X 的记录)

逐条/批量 approve/reject(skill X, ids)
  ├─ client.request(...)  ──> status/message
  ├─ loadEvolvePending()  ──> inbox 去掉已处理项
  └─ (X === evolveSelectedSkill? loadEvolveRecords(X))  ──> 记录更新(批准的进 records)

simplify(skill X)
  ├─ client.request(...) [180s, loading]  ──> status/message
  └─ loadEvolveRecords(X)  ──> 低质被删后 records 变短
```

## 7. 超时与错误

| RPC | 模式 | timeout | 理由 |
|---|---|---|---|
| `evolve_list` / `evolve_pending` | 内联只读 | 15s(默认) | 纯本地读盘,快 |
| `evolve_approve` / `evolve_reject` | 后台 | 60s | 落盘 + `render_evolution_markdown` 重渲染,快但给余量 |
| `evolve_simplify` | 后台 | 180s(对齐 install) | LLM 蒸馏判定,慢 |

错误:`client.request` reject → catch → set `evolveError` → 页内行内提示(同 `SkillsView` 的 `skillsError` 模式),操作按钮恢复可点。loading 态用 `evolveActionLoading` 禁用同类按钮防双击。

## 8. 测试

- **后端(新增 pytest)**:验证 `evolve_list`/`evolve_pending` 经 `ws_handler` 路由后能拿到 `e2a.result`(改前会拿到 `"unknown skill method"` 错)。照现有 skill RPC 测试写法;不依赖 LLM(只读 store,可造空 `evolutions.json` 或 mock store)。
- **前端**:项目无前端测试框架,靠 `vue-tsc` 类型检查 + 手动验收(与现有一致)。验收清单:进入页 inbox/records 加载、逐条与批量 approve/reject、simplify、错误态、空态。

## 9. YAGNI 边界

- **不加新 RPC**:6 个够用,本设计只用 5 个。
- **不暴露死配置**:`auto_save`/scoring 权重/`trigger` 是声明但单例不读的死配置(见 [设计文档](skill-self-evolution-design.md) §8),页面不暴露,免误导。
- **不做 pending 持久化**:v1 内存 dict 是既有设计,只加静态提示。
- **不做单条详情 RPC**:`evolve_list` 返回的投影(id/source/score/section/summary/used/positive)够 v1 展示。
- **不做「未启用」banner**:见 §4。

## 10. 子决策:`skills.evolve` 手动触发不做进 v1

6 个 RPC 里,**`skills.evolve`(手动触发进化)默认不放本页**,理由:

1. 它需带 `messages`(原始对话含工具调用结构)才能做信号检测;页面无可靠形状的原始消息,硬传多半得到 `no_signals`——是个困惑按钮。
2. 进化的主驱动是后台 `SkillEvolutionHook`(开启 `evolution.enabled` 后每轮 `after_invoke` 自动跑,有完整工具调用上下文),页面无需重复造入口。
3. 页面聚焦人机协同核心:看记录、看待批、审批、蒸馏(5 个 RPC)。

**如需手动触发按钮**(传当前会话消息、接受弱归因),可后续加:在 `EvolutionView` 加「触发进化」按钮,`client.request('skills.evolve', {name, messages: <当前会话 messages 投影>}, 180s)`,并在 `useSessions` 加 `triggerEvolve(name)` 动作。属未来增强,非 v1 范围。

## 11. 文件清单

| 文件 | 改动 | 类型 |
|---|---|---|
| [`twinkle/agentserver/server.py`](../../twinkle/agentserver/server.py) | L190 路由条件扩 2 方法 | 改 1 行 |
| [`web/src/composables/useSessions.ts`](../../web/src/composables/useSessions.ts) | refs + 动作 + NavKey | 加 |
| `web/src/components/EvolutionView.vue` | 新建页面 | 新建 |
| [`web/src/App.vue`](../../web/src/App.vue) | 导航按钮 + v-else-if | 加 |
| `tests/test_evolution_rpc_routing.py` | 验证 `evolve_list`/`evolve_pending` 路由修复回归 | 新建 |

## 12. 依赖与前置

- 后端进化包 + 6 RPC 已落地(Phase 14),无前置未完成项。
- 前端页不依赖 `evolution.enabled`(RPC 路径不受其影响)。
- 手动验收需后端跑起来(AgentServer :18000 + Gateway :19000)+ 至少一个有 `evolutions.json` 的 skill;可造测试数据。
