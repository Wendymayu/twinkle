# 并行工具调用 (Parallel Tool Calls)

## 概述

当 LLM 在一次响应中返回多个 tool calls 时，Twinkle 并发执行它们（`asyncio.gather`），而非逐个顺序执行。单 tc 时走顺序路径（无 gather 开销）。遇 `HookInterrupt`（权限审批 ASK）自动降级为顺序执行。

## 为什么同一批 tool calls 不需要考虑依赖？

**LLM tool calling 协议约定：同一批 tool calls 天然独立。**

这是 ReAct 范式的基本约束——模型在**看到上一轮 tool result 之后**才决定下一步。如果 tool B 依赖 tool A 的结果，模型会：

```
Turn 1: 调用 tool A
Turn 2: 看到 A 的结果 → 调用 tool B（参数依赖 A 的输出）
```

模型**不可能**在同一批返回有依赖的 A 和 B，因为它在发出 B 的调用时还没看到 A 的结果。因此，同一批 tool calls 之间不存在依赖关系，可以安全地并发执行。

### 行业先例

- **OpenAI API**：`parallel_tool_calls` 参数，默认 `true`，文档明确说明同一批 tool calls 是独立的
- **jiuwenswarm (openjiuwen)**：`AbilityManager.execute()` 用 `asyncio.gather` 并发执行所有 tool calls，`ReActAgentConfig.parallel_tool_calls` 默认 `true`
- **Anthropic API**：tool use 的设计也遵循同一批调用独立的约定

## 降级策略

当 `permissions.enabled=true` 且某个工具需要用户审批时，`PermissionHook` 会抛出 `HookInterrupt`，需要 yield `e2a.ask` 帧给浏览器并 `await` 用户回复。这在 `asyncio.gather` 中无法完成（不是 async generator 上下文）。

因此，当并行执行中检测到 `HookInterrupt` 时，**整个 batch 降级为顺序执行**，恢复原有的 ASK yield 逻辑。这是极少见的降级路径——仅在权限系统启用且工具需要审批时触发。

## 实现细节

### 并发隔离

每个 tool call 使用独立的资源，避免并发竞态：

| 共享状态 | 问题 | 解决方案 |
|----------|------|----------|
| `HookContext.inputs` | 并发覆盖 | 每个 tc 创建独立 `HookContext` |
| `HookContext.extra` | 审批状态竞态 | 每个 tc 使用独立 `extra={}` |
| `TODO_EVENTS` ContextVar | 并发 append/flush 竞态 | 每个 tc 用 `asyncio.create_task` 隔离 ContextVar copy |
| `SessionStore.append` | 结果顺序 | gather 后按 tcs 原顺序 append |
| `seq` 序号 | 并发递增乱序 | gather 后统一分配序号 |

### ContextVar 隔离

`_try_parallel_tool_calls` 中每个 tool call 通过 `asyncio.create_task` 包装，而非直接传入 `asyncio.gather`。`create_task` 会创建 ContextVar 的 copy，确保每个 tc 的 `TODO_EVENTS` buffer 互不干扰。

### 结果顺序

`asyncio.gather(return_exceptions=True)` 保证结果顺序与输入一致。gather 完成后，按 `tcs` 原顺序将 tool_result append 到 session store，确保 LLM 下一轮看到的对话历史与 tool_calls 顺序匹配。
