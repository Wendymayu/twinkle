# 上下文 per-invoke 冻结前缀 + memory 静态化 Design

> **For agentic workers:** 本 spec 由 brainstorming 产出。后续用 writing-plans skill 出实现计划（TDD 任务分解）。
> **前置:** 上一轮"上下文组装对齐 jiuwenswarm + KV cache 友好"（见 `2026-08-17-context-assembly-kvcache-alignment-design.md` + `2026-08-18-...-plan.md`）已落地：`SystemPromptBuilder`（dict 覆写 + priority join）、env-at-tail（RuntimeEnvHook → 尾部 `<environment_context>` UserMessage）、SkillHook/MemoryHook 已改 `ctx.builder.add_section`、`_merge_system_messages` 已删、session_store 不存 system。本 spec 是它的下一阶段。

## Context

上一轮把 system prompt 结构对齐了 jiuwenswarm，但 **`builder.build()` 跨步并未真正字节稳定**——仍有 per-step 变动内容混在前缀里，provider 端自动 prefix cache 命中打折。

三方对比（twinkle vs jiuwenswarm vs openclaw）结论：

- twinkle 走 **OpenAI Chat Completions API**（`llm_client.py:16,84` 用 `AsyncOpenAI` + `chat.completions.create`；`config.yaml` 默认 `https://api.openai.com/v1` + `gpt-4o-mini`，可切 DeepSeek/Qwen/GLM/dashscope）。**这些 provider 无 `cache_control` 字段**（那是 Anthropic Messages API 的），只做服务端自动 prefix cache。所以 twinkle 唯一能动 cache 的杠杆 = **让开头字节（system message = `builder.build()`）跨步字节稳定**。
- jiuwenswarm 母本在 invoke-prep 一次注入 skills/tools/memory，跨步稳定（jiuwenswarm 另有 Anthropic `cache_control` 断点 + vLLM release，twinkle 架构用不了，已证伪适用，砍）。
- openclaw 用 cache boundary sentinel + `cache_control`——twinkle 架构不支持，砍。

故本 spec 目标：**把 skills / USER.md·MEMORY.md / tools 从 per-step 重算改为 per-invoke 一次冻结，使 `builder.build()` 跨步字节稳定，自动 prefix cache 真正命中。**

### 根因（当前 per-step 变动源）

1. **tools 每步重建**：`agent.py:507` 每步 `tool_schemas = self._tool_manager.schemas()`（`manager.py:31-42`，纯内存每次新建 list，无 I/O 但每步重算）。
2. **skills 每步重算**：`skill_hook.py` `before_model_call` 每步重算 skills section（内容稳但每步重算；SkillHub 中途装 skill 会让前缀变 → cache break）。
3. **memory_recall 每步重读可变文件**：`memory_hook.py` `before_model_call` 每步重加 `memory_strategy`（稳）+ `memory_recall`（读 USER.md/MEMORY.md/**daily_memory**——daily 每步重读、USER.md/MEMORY.md 在 `write_memory` 后变 → 前缀变 → cache break）。这是上一轮 spec 成功标准"第一条字节稳定"未达成的根因。
4. **SkillEvolutionHook 每步 prepend experience system 消息**（`evolution_hook.py:65`，进 messages 不进 builder）：不破坏 `builder.build()` 前缀 cache（前缀字节仍命中），但阻止 cache 延伸进 history。**out of scope，仅 flag。**

## Scope

### 做

- **点2 tools 冻结**：`_run_react_loop` 顶部算一次 `schemas()` + team 过滤，`for _step` 内复用 frozen `tool_schemas`，不每步重建。
- **点3 skills per-invoke**：SkillHook `before_model_call` → `before_invoke`，一次注入，invoke 内跨步稳定。
- **点4 memory 静态化**：MemoryHook `before_invoke` 注 `memory_strategy`（稳定策略）+ `memory_static`（USER.md + MEMORY.md，读一次/invoke）；daily 不注；`memory_recall` section 删；召回走 `memory_search` = tool message = 动态区。`MEMORY_AUTO_INJECT_ENABLED` 默认翻开（USER.md/MEMORY.md 总是注入，只要文件在）。
- **注入机制 A**：`ctx.extra["frozen_sections"]`——before_invoke hooks 把稳定 section 追加到 list，loop 每步套用到 builder。

### 不做（YAGNI）

- **点5 cached_tokens 观测**（用户决定先不做）：后果——无法在 twinkle 内验证 cache 命中，得靠 provider 的 usage 响应或后续补。**已知限制，写入成功标准旁。**
- **`cache_control` / sentinel boundary**：twinkle 走 OpenAI Chat Completions API，无此字段；要用得整个换 Anthropic SDK + 消息格式，大改不值。
- **per-session 冻结**：选 per-invoke（更简单 + jiuwenswarm 对齐；模型下一轮能看到自己 `write_memory` 的更新；cache 跨步命中是主要收益，invoke 边界偶发 break 可接受）。
- **SkillEvolutionHook 改造**：它独立 `list_skills()`（`evolution_hook.py:31-33`），不消费 SkillHook section，不冲突；其 experience prepend out of scope。
- **SkillManager.list_skills memoize**：可选优化（省 SkillEvolutionHook 每步的 iterdir+stat FS I/O，`store.py:110-123`），不影响 cache（不进 `builder.build()`），先不做，留 flag。

## 设计

### 1. 注入机制：`frozen_sections` on `ctx.extra`

问题：`BEFORE_INVOKE` 在 `run()` 触发（`agent.py:432`），此时 builder 还不存在（每步在 loop 里才 `SystemPromptBuilder()`）。所以 SkillHook/MemoryHook 的 `before_invoke` 不能直接 `ctx.builder.add_section`（builder 为 None），得 stash、loop 每步套用。

- `HookContext.extra` 已存在（`base.py:188`），无需新字段。
- `BEFORE_INVOKE` hooks（SkillHook.before_invoke / MemoryHook.before_invoke）算好稳定 `PromptSection`，`ctx.extra.setdefault("frozen_sections", []).append(sec)`。
- loop 每步在 `for sec in base: builder.add_section(sec)` 之后追加：
  ```python
  for sec in ctx.extra.get("frozen_sections", []):
      builder.add_section(sec)
  ```
- 分层：`base_sections`（per-agent/per-mode 稳定：身份/工作区）vs `frozen_sections`（per-invoke 稳定：skills/memory）。职责清。

### 2. tools 冻结（`agent.py`）

`_run_react_loop` 的 `is_team_mode = request.mode == "team"`（`agent.py:489`）之后、`for _step`（`agent.py:~499`）之前：

```python
is_team_mode = request.mode == "team"
# 一次冻结 tool schemas（invoke 内不变；team 过滤只依赖 request.mode，before_invoke 时已知）
tool_schemas = self._tool_manager.schemas()
if is_team_mode:
    tool_schemas = [t for t in tool_schemas
                    if t["function"]["name"] in _TEAM_LEADER_TOOL_WHITELIST]
```

`for _step` 内删掉原 `tool_schemas = self._tool_manager.schemas()`（`agent.py:507`）+ team 过滤（508-510），`ctx.inputs = ModelCallInputs(messages=msgs, tools=tool_schemas)` 每步用同一个 frozen list。

**前提（待验证）**：无 hook 在 invoke 内 `register`/`unregister` 工具。Explore 在 loop 区（`agent.py:470-549`）未发现；plan 阶段需全 grep `register`/`unregister` 调用确认。

### 3. skills per-invoke（`skill_hook.py`）

- `before_model_call` → `before_invoke`：
  ```python
  async def before_invoke(self, ctx: HookContext) -> None:
      from twinkle.agentserver.skills import get_skill_manager
      skills = get_skill_manager().list_skills()
      if not skills:
          return
      mode = self._mode or _get_skill_mode()
      if mode == "auto_list":
          content = "你有 skills 可用。需要时先调 list_skill 看清单,再调 read_skill(name) 载入指令。"
      else:
          if mode != "all":
              log.warning("unknown SKILL_MODE %r, falling back to 'all'", mode)
          lines = ["## 可用技能"] + [f"{i}. {s.name}: {s.description}" for i, s in enumerate(skills)]
          content = "\n".join(lines)
      ctx.extra.setdefault("frozen_sections", []).append(
          PromptSection("skills", content, priority=90))
  ```
- 删 `before_model_call` 实现。
- SkillEvolutionHook **不动**（独立读 `list_skills()`，不消费 SkillHook section）。

### 4. memory 静态化（`memory_hook.py`）

`before_model_call` → `before_invoke`，注两个 section 到 `frozen_sections`：

**空 store 守卫保留**：`if not mgr.list_files(): return`（无记忆文件 → 不注 strategy 也不注 static，与现行为一致，避免无记忆时前缀多一段）。

- `memory_strategy`（priority 80）：稳定策略 prompt。**改 prompt 加一行**——daily 不再自动注入，得告诉模型去搜：
  > 需要今日/昨日记录时，先 `memory_search('daily_memory/<日期>')`。
- `memory_static`（priority 81）：USER.md + MEMORY.md 内容，读一次/invoke。格式沿用现 recall 的 `### 用户画像（USER.md）` / `### 持久事实（MEMORY.md）`，**去掉 daily 段**。受 `MEMORY_AUTO_INJECT_ENABLED` 门控。
- `before_model_call` → no-op（删）。
- 删 `memory_recall` section（USER.md/MEMORY.md 挪 `memory_static`，daily 移除，召回走 `memory_search`=tool message 自然进动态区）。
- `MEMORY_AUTO_INJECT_ENABLED` 默认翻为 **True**：`schema.py:128` `MemoryAutoInjectConfig.enabled: bool = False`→`True` + `config.yaml:66` `enabled: false`→`true`（注释同步：去 daily 描述 + 改 before_invoke）。`config/__init__.py:53` 读 `settings.memory.auto_inject.enabled` 无需改。门控语义：flag on → 注 strategy + memory_static（USER.md+MEMORY.md，无 daily）；flag off → 只 strategy。

### 5. loop 落地（`agent.py` step 段，A 机制）

每步（现有 `for sec in base: builder.add_section(sec)` 之后）：

```python
for sec in base:
    builder.add_section(sec)
for sec in ctx.extra.get("frozen_sections", []):   # NEW: per-invoke 冻结段
    builder.add_section(sec)
ctx.builder = builder
ctx.inputs = ModelCallInputs(messages=msgs, tools=tool_schemas)  # tool_schemas 已 frozen
await self._hook_manager.execute(HookEvent.BEFORE_MODEL_CALL, ctx)
# BEFORE_MODEL_CALL 现只剩 SkillEvolutionHook(experience)/ContextCompression/Repeat 等
ctx.inputs.messages = (
    [{"role": "system", "content": ctx.builder.build()}]
    + ctx.inputs.messages
)
env_entries = ctx.extra.pop("environment_context", None)   # env-tail 不变
if env_entries:
    ...  # append <environment_context> UserMessage（不变）
```

`run()` 的 `execute(BEFORE_INVOKE)`（`agent.py:432`）触发 SkillHook/MemoryHook.before_invoke 填 `frozen_sections`；ctx 贯穿 `run → _run_react_loop`，可用。member/subagent 路径同样过（SkillHook/MemoryHook 是全局 hook，loop 共享，frozen_sections 对所有 mode 生效）。

## 改动文件

| 文件 | 动作 |
|---|---|
| `twinkle/agentserver/agent.py` | 改：tools 冻结移出 for-loop + frozen_sections 套用 |
| `twinkle/agentserver/hooks/builtin/skill_hook.py` | 改：`before_model_call`→`before_invoke` + stash 到 `ctx.extra` |
| `twinkle/agentserver/hooks/builtin/memory_hook.py` | 改：`before_invoke` 注 strategy+memory_static + 删 recall + prompt 加 daily-search 提示 + 去 daily |
| `twinkle/config/schema.py` | 改：`MemoryAutoInjectConfig.enabled` 默认 `False`→`True`（line 128） |
| `twinkle/resources/config.yaml` | 改：`memory.auto_inject.enabled` `false`→`true`（line 66）+ 注释更新（去 daily、改 before_invoke） |
| `tests/test_skill_hook.py` | 改：断言到 before_invoke + `ctx.extra["frozen_sections"]` |
| `tests/test_memory_hook.py` | 改：断言 memory_static（USER.md/MEMORY.md, 无 daily）+ before_invoke；daily 不在前缀 |
| `tests/test_agent_loop_context_assembly.py` | 新增/改：跨步 `builder.build()` 字节稳定 + tools frozen + frozen_sections 注入 |
| `tests/test_base_sections.py` | 可能改：若 frozen_sections 影响断言（预期不影响 base_sections 工厂本身） |
| `tests/test_team.py` | 可能改：member 路径 frozen_sections 注入断言 |

## 验证（TDD，writing-plans 拆）

- 现有 hook 测试（skill/memory）改 `before_invoke` + `frozen_sections` 断言后全绿，对外行为等价（system prompt 内容等价，只是注入时机变了）。
- 新测试：
  - tools 冻结：`_tool_manager.schemas()` 一 invoke 只调一次（mock 计数）。
  - frozen_sections：before_invoke 注入 → loop 每步 builder 含该 section。
  - 跨步字节稳定：同一 invoke 多步 `builder.build()` 输出完全一致（无 per-step 变动 section）。
  - memory_static：USER.md/MEMORY.md 注入一次，daily 不在 `builder.build()`。
  - `MEMORY_AUTO_INJECT_ENABLED` 默认 True（config 层）。
- smoke：`python -m twinkle.agentserver` 启服务不崩；member/subagent/leader 三路径跑通。

## 成功标准

- `builder.build()` 跨步字节稳定（skills / memory_static 都 per-invoke 注一次，无 per-step 变动 section）。
- tools 一 invoke 只 `schemas()` 一次，for-loop 复用。
- skills 一 invoke 注一次；daily 不在前缀；`memory_search` 结果走动态（tool message）。
- `memory_static` 一 invoke 注一次（USER.md+MEMORY.md，读一次）。
- `MEMORY_AUTO_INJECT_ENABLED` 默认 True。
- **已知限制**：无 cached_tokens 观测 → 无法在 twinkle 内验证 cache 命中（靠 provider usage 响应或后续补观测）；SkillEvolutionHook experience prepend 阻止 cache 延伸进 history（out of scope）。

## 非目标（明确不做）

- `cache_control` / sentinel boundary（OpenAI API 架构不支持）。
- per-session 冻结（选 per-invoke）。
- cached_tokens 观测（点5，先不做）。
- SkillEvolutionHook 改造 / SkillManager memoize。
- 压缩子系统、i18n、KVCacheManager（上一轮已砍，继续不做）。
