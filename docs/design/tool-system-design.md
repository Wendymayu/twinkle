# 工具系统设计与实现

## 一句话概括

Twinkle 的工具系统是「四层模型 + 单一入口 + 横切安全」：`ToolCard`（纯元数据）/ `Tool`（接口）/ `LocalFunction`（本地函数实现）/ `ToolManager`（容器）四层；`ToolManager.execute` 是唯一执行入口，经 `@hook` 装饰的 `_hooked_tool_call` 调用，把权限、重试、失败兜底、可观测性全挂在横切层；安全靠工作区路径 confinement + 命令 deny 黑名单 + 文件写上限 + 子 agent 能力守卫四道防线纵深布防。并行、失败、权限的细节分别见 [`parallel-tool-calls.md`](./parallel-tool-calls.md) / [`failure-handling-design.md`](./failure-handling-design.md) / [`permission-approval-design.md`](./permission-approval-design.md)——本文是把它们串起来的总览。

---

## 四层工具模型

对齐 jiuwenswarm `foundation/tool/base.py`，裁到最小子集。四层各司其职，互不越界：

| 层 | 角色 | 文件 |
|---|---|---|
| `ToolCard` | 纯描述数据：`name` / `description` / `parameters`（OpenAI function-calling JSON schema） | [tools/base.py](../../twinkle/agentserver/tools/base.py) |
| `Tool` | 接口协议（`@runtime_checkable` Protocol）：`card` + `async invoke(args) -> str` | [tools/base.py](../../twinkle/agentserver/tools/base.py) |
| `LocalFunction` | `Tool` 的一种实现：捆 `ToolCard` + 一个 `Callable`，`invoke` 就是 `await self.func(**args)` | [tools/local_function.py](../../twinkle/agentserver/tools/local_function.py) |
| `ToolManager` | `Tool` 的容器：`register` / `unregister` / `get` / `list` / `schemas` / `execute`，**只知道 `Tool` 接口** | [tools/manager.py](../../twinkle/agentserver/tools/manager.py) |

关键设计：`ToolManager` 只依赖 `Tool` 接口（`card` + `invoke`），不关心是本地函数还是（未来的）MCP 工具。`LocalFunction` 是当前唯一一种实现；加 MCP 时它是 `Tool` 的兄弟实现，`ToolManager` 不动。

### `@tool` 装饰器：函数 → LocalFunction

[tools/decorator.py](../../twinkle/agentserver/tools/decorator.py) 把一个普通 async 函数包成 `LocalFunction`：调 `extract(f)` 从签名 + docstring 抽 `name` / `description` / `parameters`，构 `ToolCard`，与原函数绑成 `LocalFunction`。三种用法：`@tool`（bare）/ `@tool()`（空调用）/ `@tool(name=..., input_params=...)`（手动覆盖 schema）。还支持非装饰器形式 `tool(fn)`，`tool_manager()` 用它把已构建的工具再注册。

### schema 自动抽取

[tools/schema_extractor.py](../../twinkle/agentserver/tools/schema_extractor.py) 是 ~50 行纯 stdlib 的手写抽取器，把 Python 函数签名 + docstring 翻译成 OpenAI function-calling `parameters`：

- 类型映射：`str→string`、`int→integer`、`float→number`、`bool→boolean`、`list→array`、`dict→object`；`Optional[X]` / `X | None` 解包并标非必填；未知类型回退 `{"type":"string"}`。
- 描述取 docstring 第一段（内部换行折空格）。不解析 per-param 描述（YAGNI），要覆盖就 `@tool(input_params=...)`。

这避免了在函数签名和 schema 两处维护同一份参数定义——签名即真源，schema 是它的投影。

---

## 工具注册与默认目录

[tools/__init__.py](../../twinkle/agentserver/tools/__init__.py) 的 `tool_manager()` 构建默认 `ToolManager`，注册当前全部内置工具：

| 类别 | 工具 | 文件 |
|---|---|---|
| HTTP | `web_fetch`、`web_search` | [builtin/web_fetch.py](../../twinkle/agentserver/tools/builtin/web_fetch.py) / [web_search.py](../../twinkle/agentserver/tools/builtin/web_search.py) |
| Shell | `command_exec` | [builtin/command_exec.py](../../twinkle/agentserver/tools/builtin/command_exec.py) |
| 文件 | `read_file`、`write_file`、`edit_file`、`list_files`、`glob` | [builtin/file_tools.py](../../twinkle/agentserver/tools/builtin/file_tools.py) |
| Todo | `todo_create`、`todo_update`、`todo_list`、`todo_get` | [builtin/todo_tools.py](../../twinkle/agentserver/tools/builtin/todo_tools.py) |
| Skill | `list_skill`、`read_skill` | [builtin/skill_tools.py](../../twinkle/agentserver/tools/builtin/skill_tools.py) |
| Memory | `memory_search`、`write_memory`、`read_memory`、`edit_memory` | [builtin/memory_tools.py](../../twinkle/agentserver/tools/builtin/memory_tools.py) |
| Cron | `cron_list_jobs`、`cron_create_job`、`cron_update_job`、`cron_delete_job`、`cron_run_now` | [builtin/cron_tools.py](../../twinkle/agentserver/tools/builtin/cron_tools.py) |
| 子 agent | `spawn_subagent` | [builtin/subagent/tools.py](../../twinkle/agentserver/tools/builtin/subagent/tools.py) |
| 工作流 | `execute_workflow`（描述动态构建，列可用 workflow） | [workflow/tools.py](../../twinkle/agentserver/workflow/tools.py) |

加新工具的约定见 CLAUDE.md「Conventions」：在 `tools/builtin/*_tools.py` 写 async 函数、`@tool` 装饰、在 `tool_manager()` 里 `tm.register(...)`。`agent_loop` 通过 `schemas()`/`execute()` 自动拾取，无需改循环。

---

## 执行流：从模型 tool_calls 到 tool_result

```
LLM Finish(tool_calls)  ──agent_loop._inner_run_stream──┐
                                                       │
   schemas() = ToolManager.schemas()  ◄────────────────┤  把工具清单喂给模型（每轮调模型前）
                                                       │
   单 tc ─► _hooked_tool_call(ctx)  ─► @hook(before/after/exception)  ─► ToolManager.execute(name, args)
                                                                       │
                                                          unknown?  ┄►  return "[error] unknown tool: {name}"
                                                                       │  否则
                                                                       └► LocalFunction.invoke(args) = await func(**args)
                                                                       │
                                                  异常上抛（不吞）─► @hook 触发 ON_TOOL_EXCEPTION ─► RetryHook 重试瞬时一次
                                                                       │  仍失败/非瞬时
                                                  agent_loop except Exception ─► result = "[tool error] {type}: {exc}"
                                                                       │
   tool_result = {"role":"tool","tool_call_id":tc["id"],"content":result}
        └─► SessionStore.append ─► _reask=True ─► 重调模型（ReAct 续循环）
```

- **schemas → 模型**：每轮调模型前，`ctx.inputs.tools = self._tool_manager.schemas()`（[agent_loop.py](../../twinkle/agentserver/agent_loop.py) 模型段），模型据此决定调哪些工具。
- **单一执行入口**：`ToolManager.execute` 是所有工具调用的唯一入口（[manager.py:44-52](../../twinkle/agentserver/tools/manager.py#L44-L52)）。未知工具返回 `[error] unknown tool: {name}` 串（是结果不是异常）；已知工具 `await t.invoke(args)`——异常**上抛不吞**，让 `@hook` 的 `ON_TOOL_EXCEPTION` 触发、`RetryHook` 能重试。
- **横切挂在 `_hooked_tool_call`**：[agent_loop.py](../../twinkle/agentserver/agent_loop.py) 的 `_hooked_tool_call` 用 `@hook(..., on_exception=HookEvent.ON_TOOL_EXCEPTION)` 装饰。`before_tool_call`（权限拦截）、`after_tool_call`、`on_tool_exception`（重试）全在这层，`ToolManager` 和 `LocalFunction` 对此一无所知。
- **结果恒为字符串**：`invoke` 契约是 `-> str`。无论成功（工具自己返回的串）还是失败（`[tool error]` 兜底串 / 权限 deny 串 / 用户拒绝串），都作为 `tool_result.content` 回灌模型。OpenAI tool 协议的 `tool` 消息 `content` 本就是字符串——不用结构化对象逼模型学读 `is_error` 字段。

---

## 并行工具调用

LLM 一次返回多个 tool_calls 时并发执行。要点（详见 [`parallel-tool-calls.md`](./parallel-tool-calls.md)）：

- 同一批 tool_calls 天然独立（LLM 协议约定：模型看到上一轮 result 才决定下一步，不可能同批返回有依赖的调用），可安全 `asyncio.gather`。
- 单 tc 走顺序路径（无 gather 开销）；多 tc 经 `_try_parallel_tool_calls` 用 `asyncio.create_task` 包装每个 tc（ContextVar copy 隔离 TODO buffer）+ `asyncio.gather(return_exceptions=True)`，结果按原顺序回灌。
- 并发隔离：每个 tc 独立 `HookContext`（`inputs`/`extra` 不共享）、独立 TODO_EVENTS buffer、独立 `create_task`。
- **降级**：任一 tc 触发权限 ASK（`HookInterrupt`）→ 整个 batch 降级为顺序执行，恢复 `e2a.ask` yield 逻辑（gather 不是 async generator 上下文，无法 yield）。

---

## 失败处理（软 / 硬二分）

一句话：**工具侧一切失败都是软的——错误变 `tool_result`，循环继续；模型侧失败是硬的——异常上抛，循环终止**。瞬时异常两侧都先重试一次。完整链路见 [`failure-handling-design.md`](./failure-handling-design.md)，这里只列对工具系统相关的要点：

| 工具失败场景 | 处理 | 循环 |
|---|---|---|
| 工具抛瞬时异常（`httpx`/超时） | `@hook` `ON_TOOL_EXCEPTION` → `RetryHook` 重试 1 次 | 重试 |
| 工具抛非瞬时异常 | `RetryHook` 不重试 → `agent_loop` `except Exception` 兜底 `[tool error] {type}: {exc}` | 继续 |
| 工具自处理错误（`command_exec`/`file_tools`） | 内部包 `[ERROR]:` 串返回，不抛 | 继续 |
| 未知工具 | `execute` 返回 `[error] unknown tool: {name}` | 继续 |
| 权限 DENY / 用户拒绝 | deny 串 / `[tool denied by user: …]` 当 `tool_result` | 继续 |
| 子 agent 失败/超时 | `SubagentResult(success=False)` → `_wrap` 转串 + stop hint | 继续 |

要点：`ToolManager.execute` **不再吞异常**（曾经内部 catch-all 使 `ON_TOOL_EXCEPTION` 成死代码，现已反转）——异常上抛让重试和兜底各归其位，兜底成串的职责上移到 `agent_loop` 调用处的 `except Exception`。`command_exec` 非零退出码不算失败（返回 JSON，模型自判），因 `grep`/`test` 非零是常态。

---

## 权限（ALLOW / DENY / ASK）

权限是挂在 `before_tool_call` 上的横切 Hook（`PermissionHook`，priority=100），核心 ReAct 循环零改动。详见 [`permission-approval-design.md`](./permission-approval-design.md)，要点：

- 三决策：`ALLOW`（放行）/ `DENY`（`force_finish`，deny 串当 `tool_result` 回灌）/ `ASK`（`HookInterrupt` 挂起、`yield e2a.ask`、等前端人类决策回灌 `approval.respond` 后恢复）。
- `PermissionPolicy.check` 四层合并：allow_always 覆盖 > builtin deny 规则 > 用户 deny 规则 > per-tool 档位（`require-approval` 归一为 ASK）。优先级固定：覆盖 > 拒绝 > 档位。
- `approval_id`（非 `request_id`）是 Future key，跨被挂起的 `chat.send`(R) 与审批响应(R2) 两侧关联；`ws_handler` 并发 per-request task 保证挂起时读循环仍转、审批响应能进。
- `allow_always` 覆盖写磁盘 JSON + mtime 热重载，跨重启存活且即时生效。

---

## 安全：纵深四道防线

工具能改文件系统、起进程、连外网，风险不对等。Twinkle 不靠单一闸门，而是多层 defense-in-depth——任一层漏了，下一层兜：

### ① 工作区路径 confinement

`command_exec` 的 `_resolve_workdir` 与 `file_tools` 的 `_resolve_file_path` 同构：相对路径 join 到 `WORKSPACE_DIR`，`resolve()` 后 `relative_to(root)`——逃出工作区抛 `ValueError`，工具层包成 `[ERROR]: workdir is outside the project workspace.` / `[ERROR]: path is outside the project workspace.`。这是「命令/文件操作只能在工作区内」的硬约束，与权限系统开关无关。

### ② 命令 deny 黑名单（单一真源）

[permissions/builtin_rules.py](../../twinkle/agentserver/permissions/builtin_rules.py) 的 `COMMAND_DENY_PATTERNS`（17 条正则）是 `command_exec` deny 的**唯一真源**——`command_exec._check_command_safety` 与 `PermissionPolicy.check` 都引同一张表，杜绝双份维护。覆盖：

- 磁盘/分区破坏：`rm -rf`、`del /f /s /q`、`rd /s /q`、`format`、`mkfs`、`dd of=/dev/`、`diskpart`
- 下载执行：`curl|bash`、`iwr|iex`、`bash <(curl)`
- 混淆/动态执行：`base64 -d|bash`、`eval`、`iex`、`python -c "...socket/subprocess..."`
- 反弹/bind shell：`nc -e`、`socat EXEC:`、`bash -i /dev/tcp/`
- fork bomb / 资源滥用：`:(){ :|:& };:`、`kill -9 -1`、`ulimit -u unlimited`
- 凭证访问/解密、证书私钥读取

**关键**：这张表在 `command_exec` 工具内部也读（`_check_command_safety`），所以**即使权限系统关闭（`permissions.enabled=false`）或 hook 被绕过，危险命令仍被拒**——这是 defense-in-depth 的核心：工具自保不依赖外层开关。

### ③ 文件写上限 + read-before-write

[file_tools.py](../../twinkle/agentserver/tools/builtin/file_tools.py)：

- `_WRITE_MAX_BYTES = 5 MiB`——单次写上限，防爆大文件。
- 强制 read-before-write：`FileReadRegistry`（per-session）记录已读路径，`write_file`/`edit_file` 要求该路径读过才放行，防盲覆盖。
- 二进制检测：已知扩展名表 + 首 8 KiB 探 NUL 字节，避免把二进制当文本塞进模型上下文。
- `edit_file` 用精确字符串替换（非正则），要求 old_string 唯一，避免误改。

### ④ 子 agent 递归 / 能力守卫

[subagent/models.py](../../twinkle/agentserver/tools/builtin/subagent/models.py) 的 `EXCLUDED_TOOLS`：

```python
EXCLUDED_TOOLS = {"spawn_subagent", "write_memory", "edit_memory"}
```

子的 `ToolManager` 复制父的工具**减去这个集合**。两重守卫：

- `spawn_subagent` 被排除 → 子不能再委派 → **单层递归**，防无限递归烧资源。
- `write_memory` / `edit_memory` 被排除 → 子的记忆只读。子 agent 无直连用户通道、上下文被父控制，让它写长期记忆不安全。

配合 [subagent/executor.py](../../twinkle/agentserver/tools/builtin/subagent/executor.py) 的软/硬/abort 三级超时（`soft_timeout` 无活动复位、`hard_timeout` 包整个 child run、`abort_timeout` 取消卡死子的等待窗口）+ 结果截断 `max_result_chars=8000`（防父上下文爆炸），子 agent 的失败/超时全包成 `SubagentResult(success=False)` 不抛，转串回灌——和普通工具软失败语义一致。

### allow_always 的 shell 元字符护栏

`allow_always` 持久化把命令头 + ` *` glob 写进覆盖表（前两个空白分隔 token，故意不用 `shlex` 免得把 Windows 路径 `C:\Users` 嚼成 `C:Users`）。但**拒绝 bless 含 shell 元字符的命令**：

```python
_SHELL_METACHARS = frozenset(";&|<>`$\n")
```

否则一个持久化的 `"npm run *"` 会 bless `"npm run build && rm -rf /"`——glob 匹配到但后半段是危险链，绕过 deny 规则。含元字符的命令 fall through 给 deny 规则/档位重新判定，不进覆盖表。这是「永久放行」便利性与安全性的闸门。

### 审计

[permissions/audit.py](../../twinkle/agentserver/permissions/audit.py) 的 `ToolPermissionLog` 往 `permission_audit.jsonl` 追加 JSONL（`tool`/`decision`/`source`/`rule_id`/`reason`/`user_decision`/`channel`/`session_id`/`request_id`/`ts`，**不含 `args`/`approval_id`**）。每次 `engine.check` 写一行；`allow_always` 持久化再写一行。fail-soft：写盘出错只 `log.warning` 不阻断判定。审计只追加、无查询面——没有 RPC/UI，只能直接看文件（裁掉 jiuwenswarm DB 审计的取舍）。

---

## 可观测性：OTel gen_ai.tool span

[observability/instrumentors/tool.py](../../twinkle/observability/instrumentors/tool.py) 用 `patch_method` 包 `ToolManager.execute`，每次工具调用产一个 `gen_ai.tool` span：

- `start_as_current_span`（非 `start_span`）——工具内部产的 span（如 `spawn_subagent` 触发的 `twinkle.agent.invoke`）父挂到这个 tool span，而非外层 agent invoke。
- 属性：`gen_ai.tool.name`、`gen_ai.tool.arguments`（截断）、`gen_ai.tool.result`（截断）、`gen_ai.tool.error`。
- 结果串以 `tool.error` 前缀（`[error]`/`[tool error]`）判定 error 布尔；抛异常则 `record_exception` + `status=ERROR`。
- `metrics.record_tool_call(name, error, duration)` 同步记指标。

由于 `execute` 现在上抛异常（不再吞），失败工具的 span 现在是 ERROR + `record_exception`——比吞异常时恒 OK 更正确。

---

## 文件地图

| 文件 | 角色 |
|---|---|
| [tools/base.py](../../twinkle/agentserver/tools/base.py) | `ToolCard` + `Tool` Protocol |
| [tools/local_function.py](../../twinkle/agentserver/tools/local_function.py) | `LocalFunction`——`Tool` 的本地函数实现 |
| [tools/decorator.py](../../twinkle/agentserver/tools/decorator.py) | `@tool` 装饰器：函数 → `LocalFunction` |
| [tools/schema_extractor.py](../../twinkle/agentserver/tools/schema_extractor.py) | 签名 + docstring → OpenAI parameters JSON schema |
| [tools/manager.py](../../twinkle/agentserver/tools/manager.py) | `ToolManager`：register/get/list/schemas/execute（唯一执行入口，抛异常不吞） |
| [tools/__init__.py](../../twinkle/agentserver/tools/__init__.py) | `tool_manager()`——默认工具目录注册 |
| [tools/builtin/](../../twinkle/agentserver/tools/builtin/) | 内置工具实现（command_exec / file_tools / web_* / todo / skill / memory / cron / subagent） |
| [agent_loop.py](../../twinkle/agentserver/agent_loop.py) | `_hooked_tool_call`（`@hook` 装饰，权限/重试/兜底横切）+ `_try_parallel_tool_calls`（并发）+ 调用处 `except Exception`→`[tool error]` 兜底 |
| [permissions/builtin_rules.py](../../twinkle/agentserver/permissions/builtin_rules.py) | `COMMAND_DENY_PATTERNS`（17 条正则）——command_exec deny 单一真源 |
| [permissions/policy.py](../../twinkle/agentserver/permissions/policy.py) | `PermissionPolicy`：四层合并 + allow_always 持久化 + mtime 热重载 |
| [permissions/engine.py](../../twinkle/agentserver/permissions/engine.py) | `PermissionEngine`：通道门 + 审计 + 委派 |
| [permissions/audit.py](../../twinkle/agentserver/permissions/audit.py) | `ToolPermissionLog`：JSONL 审计，fail-soft |
| [hooks/builtin/permission_hook.py](../../twinkle/agentserver/hooks/builtin/permission_hook.py) | `PermissionHook`（priority=100）：bypass / check / 三决策分派 |
| [hooks/builtin/retry_hook.py](../../twinkle/agentserver/hooks/builtin/retry_hook.py) | `RetryHook`：瞬时异常重试一次（工具 + 模型） |
| [tools/builtin/subagent/models.py](../../twinkle/agentserver/tools/builtin/subagent/models.py) | `EXCLUDED_TOOLS`（递归 + 能力守卫）+ `SubagentResult` + `SoftTimeoutError` |
| [tools/builtin/subagent/executor.py](../../twinkle/agentserver/tools/builtin/subagent/executor.py) | `SubagentExecutor`：隔离子 loop + 软/硬/abort 超时 + 异常全包不抛 |
| [observability/instrumentors/tool.py](../../twinkle/observability/instrumentors/tool.py) | `gen_ai.tool` span 插桩 |

---

## 设计决策回顾

### 为什么四层而非一层

`ToolCard`（数据）/ `Tool`（接口）/ `LocalFunction`（实现）/ `ToolManager`（容器）分开，让「描述」「实现方式」「容器」正交。代价是四个小文件，换来：加 MCP 工具是 `Tool` 的新兄弟实现，`ToolManager` 不动；schema 从签名抽取而非手维护，函数定义即真源；`ToolManager` 只信接口，不关心实现细节。对学习型重实现，这层抽象是「未来 MCP」的预留位，当前只本地函数一种实现。

### 为什么 `execute` 抛异常而非吞成串

曾经 `execute` 内部 catch-all 兜底，保证「工具异常不击穿循环」不依赖调用方，代价是 `ON_TOOL_EXCEPTION` 成死代码——异常在 manager 层就被吃成串，永不冒泡到 `@hook`，`RetryHook` 无从介入。**现已反转**：`execute` 抛，`ON_TOOL_EXCEPTION` 真正触发，瞬时异常重试一次，兜底成 `[tool error]` 串的职责上移到 `agent_loop` 调用处。不变式「工具异常不击穿循环」由调用方 `except Exception` 保证，`execute` 契约从「返回错误串」变成「抛异常」，唯一调用方已同步。

### 为什么安全靠多层而非单一权限闸门

权限系统可关、hook 可被绕过、配置可错——单一闸门任一环漏了就全漏。纵深布防让每层自保：`command_exec` 内部读同一张 deny 表（关权限也拒危险命令）、`_resolve_workdir` 工具层 confinement（与权限无关）、`file_tools` 写上限 + read-before-write、子 agent `EXCLUDED_TOOLS` 防递归。权限层（`PermissionHook` + `PermissionPolicy`）是「人类 in-the-loop」的额外切面，不是唯一闸门。代价是 deny 逻辑分散在工具层和权限层两处——但用「单一真源表 + 两处引用」化解（`builtin_rules.matches` 一张表，两边读）。

### 为什么 `command_exec` 非零退出码不算失败

`grep` 没匹配返回 1、`test` 失败返回非零，是命令正常表达「没找到 / 不成立」。当错误会让模型误以为命令「坏了」而放弃。退出码、stdout、stderr 全给模型，让它按语义解读——把「失败」的定义权交给语义而非进程退出码。

### 为什么子 agent 永不抛异常

`SubagentExecutor.execute_subagent` 把软/硬超时和所有异常全包成 `SubagentResult(success=False)`，再经 `_wrap` 转串回灌父。子 agent 是个工具，它的失败就该走和普通工具一样的软失败路径——父看到错误串能换路，而非被子的异常击穿。子的 `e2a.error` 帧、内部异常、活动超时都在 `execute_subagent` 这层被吃掉，对父循环完全透明。

### 为什么 `allow_always` 拒绝 shell 元字符

bless 是把「命令头 + ` *`」glob 写进覆盖表，下次同头命令直接放行。若允许 bless 含 `;&|` 的命令，`"npm run *"` 会 bless `"npm run build && rm -rf /"`——glob 匹配到，后半段危险链却绕过 deny 规则。元字符命令 fall through 重新判定，是这个「永久放行」便利与安全冲突的唯一闸门。
