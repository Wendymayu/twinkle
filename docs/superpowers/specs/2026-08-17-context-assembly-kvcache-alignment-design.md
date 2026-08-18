# 上下文组装对齐 jiuwenswarm + KV cache 友好（最小对齐）Design

> **For agentic workers:** 本 spec 由 brainstorming 产出。后续用 writing-plans skill 出实现计划（TDD 任务分解）。

## Context

Twinkle 是 jiuwenswarm 的 learning-focused 精简重实现。上下文工程对比调研（见 memory `jiuwenswarm-context-engine-design`）发现：**压缩机制**精简版可用（不追求 jiuwenswarm 9 processor 复杂度，用户已确认保持现状），但**上下文内容和组装方式**与 jiuwenswarm 差距明显，且**完全没有 KV cache 友好设计**——这是本次要补的两块。

### 根因（Twinkle 上下文组装现状）

- `build_system_prompt()`（`agent.py:83`）一个大 f-string 线性拼身份/运行环境/workspace/工具指南，**无 section 概念**。
- hook（SkillHook p90 / MemoryHook p80）用 `_prepend_system_message` **prepend system message** → 多 system 堆叠。
- `_merge_system_messages`（`agent.py:849-902`）靠**字符串前缀**（`# 身份与行为原则` / `## 可用技能` / `## 长期记忆` / `[prior context summary]`）分 5 桶重组——脆弱且堆叠不覆写。
- **易变 env 信息埋在 system 前缀**：`build_system_prompt` 的 `today_date` / `os_type`、MemoryHook 策略 prompt 里的 `today`——每步变却在前缀 → provider 端 prefix cache 失效。
- 无 KV cache 意识/管理器。

### jiuwenswarm 对照

- `SystemPromptBuilder._sections: dict[str, PromptSection]`，`add_section` = `_sections[name] = section`（同名覆写不堆叠），`build()` 按 priority join。
- env（时间/平台）**不进 system**，每轮重建放尾部 `<environment_context>` UserMessage（用 UserMessage 不用 SystemMessage——多数 provider 把额外 SystemMessage 合并进 system 参数破坏前缀 cache 稳定性）。
- KVCacheManager 的 tools diff + 纯追加 release **依赖自研推理后端 vLLM 的 `release()`**；Twinkle 跑 OpenAI 兼容 API，release 是空操作。

## Scope（最小对齐）

**做：**
- **B4** SystemPromptBuilder（dict-by-name section 覆写 + priority join）替代 f-string + merge。
- **B1** env-at-tail（today/os 移尾部 `<environment_context>` UserMessage）。
- section 化注入（SkillHook/MemoryHook 用 `add_section` 而非 prepend）。
- 砍 `_merge_system_messages`。

**不做（YAGNI）：**
- B2/B3（KVCacheManager + tools diff + 纯追加 release）——依赖 vLLM 自研后端，OpenAI API 空操作。
- 压缩子系统 9 processor（DialogueCompressor/RoundLevel/FullCompact/MessageSummaryOffloader/ToolResultDedup 等）——保持现状精简版。
- 多语言 section（jiuwenswarm cn/en 双份）——单语。
- offload 召收 + `reload_original_context_messages` 工具——Twinkle 无 offload。

## 设计

### 1. SystemPromptBuilder（新模块 `agentserver/prompts.py`）

```python
from dataclasses import dataclass

@dataclass
class PromptSection:
    name: str
    content: str
    priority: int

class SystemPromptBuilder:
    """dict-by-name section + priority 排序 + 同名覆写(不堆叠)。
    抄 jiuwenswarm core/single_agent/prompts/builder.py 核心,砍多语言。"""
    def __init__(self) -> None:
        self._sections: dict[str, PromptSection] = {}

    def add_section(self, section: PromptSection) -> None:
        self._sections[section.name] = section  # 同名覆写

    def remove_section(self, name: str) -> None:
        self._sections.pop(name, None)

    def build(self) -> str:
        return "\n\n".join(
            s.content for s in
            sorted(self._sections.values(), key=lambda x: x.priority)
        )
```

每步 per-request 新建实例。`build()` 每次全量重建、幂等。

### 2. section 划分 + env-at-tail

**留 system section（进 `builder.build()`，稳定）：**

| section | priority | 来源 |
|---|---|---|
| identity | 10 | 现 build_system_prompt 的身份行为原则段 |
| runtime_guidance | 20 | 命令语法对照表 + Windows mkdir warning（**去 today/os**） |
| workspace | 30 | 工作区目录表 |
| tools_guidance | 40 | todo/工具使用指南 |
| skills | 90 | SkillHook 注入 |
| memory_strategy | 80 | MemoryHook 注入（**去 today**） |
| memory_recall | 81 | MemoryHook opt-in 被动召回 |

**移尾部 `<environment_context>` UserMessage（每轮重建，不破坏前缀）：**
- `today_date`、`os_type`（当前平台值）。
- MemoryHook 策略 prompt 的 `today` 移出 → 改"当日发生的事 → write_memory('daily_memory/YYYY-MM-DD.md')，今日日期见环境信息"。

对齐后序列：`[SystemMessage(builder.build())] + [历史…] + [UserMessage(<environment_context>)]`。第一条 = 各稳定 section join，字节稳定 → provider 端自动 prefix caching 命中。

### 3. hook 改造 + 砍 merge

- **builder 经 HookContext 共享**：`HookContext` 加 `builder: SystemPromptBuilder | None` 字段。loop 每步新建 builder 赋给 ctx；hook 从 `ctx.builder` 取，调 `add_section`。
- **SkillHook**（p90）：`_prepend_system_message` → `ctx.builder.add_section(PromptSection("skills", ...))`，不再碰 `ctx.inputs.messages`。
- **MemoryHook**（p80）：`_prepend` → `add_section("memory_strategy"...)` + `add_section("memory_recall"...)`，策略 prompt 去 today。
- **ContextCompressionHook**（p95）：summary **保留为 messages 里独立 system msg**（`head + [summary] + tail`），不进 builder——summary 是压缩产物，不是 system prompt section。压缩只改 `ctx.inputs.messages`（messages 那份），不动 `ctx.builder`（builder 仍是 base sections，summary 不进）。
- **RepeatToolCallDetectorHook**（p88）：remediation 仍 append 末尾（不破坏 leading prefix，不动）。
- **砍 `_merge_system_messages`**（`agent.py:516` 调用 + `849-902` 定义）——builder.build() 已是单条 system message，不需要 merge。

### 3a. loop 流程 + session_store 不存 system prompt

**关键架构决策**：session_store **不再存 system prompt**（去掉 `agent.py:485-490` 的 `build_system_prompt()` append）。session 只存 user/assistant/tool 对话历史。system prompt 每步由 builder 重建注入 `messages[0]`——对齐 jiuwenswarm（builder.build() 每步注入 ContextWindow，不持久化 session）。

理由：builder 内容每步可能变（hook add_section 加 skills/memory），持久化会和下一步不一致；每步重建保证 system prompt 永远是当前最新 sections。

**每步 loop 流程**：
1. `builder = SystemPromptBuilder()` + 注入 base sections（identity/runtime_guidance/workspace/tools_guidance，从现 `build_system_prompt` 拆）。
2. `history = session_store.get_messages(session_id)`（纯对话，无 system）。
3. `ctx.builder = builder`；`ctx.inputs.messages = history`。
4. `await hook_manager.execute(BEFORE_MODEL_CALL, ctx)` —— hook 改 `ctx.builder`（SkillHook/MemoryHook `add_section`）和/或 `ctx.inputs.messages`（ContextCompression 压缩 / Repeat remediation append）。
5. 发 LLM 前（旧 `_merge_system_messages` 位置）：`ctx.inputs.messages = [{"role":"system","content":ctx.builder.build()}] + ctx.inputs.messages` + env 消费（见节4）append `<environment_context>` UserMessage。

**压缩的 head 语义变化**：ContextCompressionHook 此刻操作 `ctx.inputs.messages`（纯 history，无 system head）—— `split_messages_head_middle_tail` 的 head 为空，summary 成首条 system msg。loop 末尾再 prepend `builder.build()` 在 summary 前：`[builder.build()] + [summary] + tail + [env]`。符合"summary 不进 builder，是独立 system msg"。

### 4. env-at-tail 消费链

- 新 **RuntimeEnvHook**（`before_model_call`，priority 99，最先跑）往 `ctx.extra["environment_context"]` append `{"content": ..., "source": "runtime_env"}`（today + os）。用 `ctx.extra` 不用 `ctx.builder`——env 不进 system prompt。
- agent loop 发 LLM 前（旧 `_merge_system_messages` 位置）：`if env := ctx.extra.pop("environment_context", None): ctx.inputs.messages.append({"role":"user","content":"<environment_context>\n" + "\n\n".join(e["content"] for e in env) + "\n</environment_context>"})`。`pop()` 防多轮累积。
- UserMessage 不 SystemMessage——多数 provider 把额外 SystemMessage 合并进 system 参数破坏前缀 cache 稳定性（jiuwenswarm 的明示理由）。

## 改动文件

1. **`agentserver/prompts.py`**（新）：`PromptSection` + `SystemPromptBuilder`。
2. **`agentserver/agent.py`**：loop 每步新建 builder + 注入 base sections（identity/runtime_guidance/workspace/tools_guidance，从现 `build_system_prompt` 拆）+ builder 赋 `ctx.builder` + env 消费（`ctx.extra.pop`）+ 砍 `_merge_system_messages` 调用与定义。`build_system_prompt` 拆成各 section 的 content 工厂函数（或内联到 loop）。
3. **`hooks/builtin/skill_hook.py`**：`_prepend_system_message` → `ctx.builder.add_section`。
4. **`hooks/builtin/memory_hook.py`**：`_prepend` → `add_section` x2；策略 prompt 去 today。
5. **`hooks/builtin/runtime_env_hook.py`**（新）：RuntimeEnvHook 注 `ctx.extra["environment_context"]`。
6. **`hooks/base.py`**：`HookContext` 加 `builder` 字段。
7. **`hooks/builtin/__init__.py` + `agent.py` build_agent_loop**：注册 RuntimeEnvHook（priority 99）。

## 验证

- 现有 hook 测试（compression/skill/memory/repeat/overflow）全绿——改 `add_section` 后对外行为不变（system prompt 内容等价）。
- 新测试（TDD，writing-plans 拆）：
  - SystemPromptBuilder dict 覆写（add 同名 section 不堆叠，build 按 priority join）。
  - env-at-tail（`environment_context` 在尾部 UserMessage + `pop()` 防多轮累积）。
  - 第一条 system 字节稳定（skill/memory 内容不变 → `build()` 输出不变）。
  - merge 已删（无 `_merge_system_messages` 调用，多 hook 注入不靠字符串前缀重组）。
- smoke：`python -m twinkle.agentserver` 启服务不崩。

## 成功标准

- `_merge_system_messages` 删除。
- system 第一条 = `builder.build()`，字节稳定（稳定 section 内容不变则输出不变）。
- env（today/os）在尾部 `<environment_context>` UserMessage，不在 system 前缀。
- SkillHook/MemoryHook 用 `ctx.builder.add_section`，不再 prepend system message。
- provider 端 prefix caching 可命中（第一条字节稳定 + env 不破坏前缀）。
