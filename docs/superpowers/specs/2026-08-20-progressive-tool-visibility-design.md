# Progressive Tool Visibility — Design

- Status: Draft for review
- Date: 2026-08-20
- Related: jiuwenswarm `JiuWenProgressiveToolRail` (`deep_agent/rails/jiuwen_progressive_tool_rail.py`)

## 1. 背景

Twinkle `ToolManager.schemas()` 全量返回所有注册工具 schema,`agent.py` `_run_react_loop` 在 L501 一次性冻结并逐步骤塞给 LLM(L562 `llm.stream(tools=...)`),无筛选/检索/分页/token 上限。MCP 接入(Phase 15)后,MCP 工具经 `register_into` 全量灌入同一 ToolManager → 工具线性膨胀无兜底。

参考实现 jiuwenswarm 用 `JiuWenProgressiveToolRail` 解决:eager(常驻 schema)+ deferred(按需)二档 + `tools_search`/`invoke_tool` 两 meta-tool(按名精确间接调用)+ 导航清单(系统提示列 deferred 工具 name+简述)。固定 eager 利 prefix cache。本设计将其核心机制精简引入 Twinkle。

## 2. 目标与成功标准

**目标**:工具数量膨胀时,只把 eager 工具 schema 塞模型,deferred 工具经 meta-tool 按需访问,降每轮 token + 保 prefix cache。

**成功标准**:
- progressive 关闭(`enabled=false`):行为完全等同现状(33 内置全量塞,无 meta-tool,无导航)。现有测试不受影响。
- progressive 开启 + MCP 开启:MCP 工具不在 `tools` 列表、出现在导航清单;`tools_search(tool_name)` 按名精确找到 MCP 工具 schema;`invoke_tool(tool_name, arguments)` 能执行 MCP 工具;33 内置仍在 eager 直达。
- prefix cache 稳定:启用后同一 invoke 内各步 `tools` 列表内容相同。
- 新增测试全绿。

## 3. 范围

**In scope**:主 agent(普通模式)工具注入路径的 eager/deferred 二档 + 两 meta-tool + 导航清单 + 配置开关(默认关)。

**Out of scope(不做)**:
- team-leader/member/subagent 现有静态白名单(正交机制,不动)。
- `enable_for_models`(按模型启停)。
- 阈值自动触发(按工具数自动启用)。
- subagent 独立 profile / 继承父 eager。
- MCP allowlist(按 server 限定暴露子集)。
- 向量/语义检索(`tools_search` 按名精确,对齐 jiuwenswarm)。
- 双语导航(只中文)。

## 4. 架构(叠加层,不侵入核心)

接入点:`server.py` `create_agent` 的 auto-wired hook 列表(L93-101),对齐 `SubagentContextHook` / `ContextCompressionHook` 模式(依赖在 create_agent 可用即 auto-wire)。`tool_manager()`(L74)和 `mcp.register_into`(L76)不动。

```
create_agent(store, hooks, llm):
  tools = tool_manager()                      # 不变:33 内置
  get_mcp_manager().register_into(tools)     # 不变:MCP 灌入
  ...                                         # subagent/workflow executor
  all_hooks = list(hooks or []) + [auto-wired...]
  progressive_hook = apply_progressive_tools(tools, settings.progressive_tool)  # 新
  if progressive_hook:
      all_hooks.append(progressive_hook)      # enabled=false → None → 不加
  return ReActAgent(llm, store, tools, hooks=tuple(all_hooks))
```

`apply_progressive_tools` 在 `mcp.register_into` **之后**调用,确保 meta-tool 持有的 tm 引用已是完整态(含 MCP)。deferred 清单在 meta-tool invoke 时实时算,对注册时机不敏感。

## 5. 组件

### 5.1 配置(`config/schema.py` + `config.yaml`)

新增 `ProgressiveToolConfig`:
```python
class ProgressiveToolConfig:
    enabled: bool = False            # 默认关
    eager_tools: list[str] = []     # 可选;空=默认(33 内置名 + tools_search + invoke_tool)
```
config.yaml:
```yaml
progressive_tool:
  enabled: false
  eager_tools: []   # 空=默认全内置+meta
```

### 5.2 两 meta-tool(新文件 `tools/builtin/progressive_tools.py`)

手写 `Tool` 子类(**非 `@tool`**,因需持有 `tm` 引用;对齐 jiuwenswarm `ToolsSearchTool` 是 `Tool` 子类注入 callable)。常量 `META_NAMES = frozenset({"tools_search", "invoke_tool"})`。

**`ToolsSearchTool(tm, eager_names)`**:
- `card.name="tools_search"`;`description`:按工具注册名查 deferred 工具完整 schema,调用 invoke_tool 前必须先调本工具。
- `input_params`:`{tool_name: str}`(desc:"与导航列表名称一致,精确匹配,忽略大小写")。
- `invoke(inputs)`:实时算 `deferred = [s for s in tm.schemas() if s["function"]["name"] not in eager_names and not in META_NAMES]`;按 `name.lower()==tool_name.lower()` 精确匹配;返回 `{success, matches:[{name, description, input_schema}], count, message}`。未匹配 message:"未找到名为 'X' 的按需可见工具,请检查名称是否与导航列表一致"。整体 try/except 返回结构化 error。

**`InvokeToolTool(tm, eager_names)`**:
- `card.name="invoke_tool"`;`description`:按名间接调用 deferred 工具。
- `input_params`:`{tool_name: str, arguments: object}`。
- `invoke(inputs)`:校验 `tool_name in deferred_names`(对齐 jiuwenswarm rail L473 拒绝调 eager 已暴露的);`await tm.execute(tool_name, arguments)`;返回工具结果(成功/失败 + 输出)。

### 5.3 `ProgressiveToolHook`(新文件 `hooks/builtin/progressive_tool_hook.py`,对齐 `SkillHook` 模式)

- `priority = 70`(功能层 50-99)。
- 构造:`ProgressiveToolHook(eager_names)` —— `eager_names` 构造时传入(来自 builder,见 §5.4)。
- `before_invoke(ctx)`:用 `ctx.agent._tool_manager` 实时读 tm(不缓存 init——`HookManager.register_hook` 调 `init(None)`,缓存会留 `None` → 导航静默失效;实时读 `ctx.agent` 最稳,见 agent.py L423 `agent=self`)。
- `before_invoke(ctx)`(对齐 SkillHook):用 `self._tm.schemas()` 算 deferred 清单(非 eager 非 meta)→ 拼导航 `PromptSection("tool_navigation", header+entries, priority=70)`(header:"按需可见工具导航(不可直接调用),必须先 tools_search 再 invoke_tool";entries = `- {name}: {description[:160]}`)→ `ctx.extra.setdefault("frozen_sections", []).append(...)`。无 deferred → no-op(对齐 SkillHook 无 skill 时 no-op)。
- `before_model_call(ctx)`:`ctx.inputs.tools = [t for t in ctx.inputs.tools if t["function"]["name"] in self.eager_names]`(eager 含两 meta-tool;跨步确定性结果 → cache 友好)。

### 5.4 builder(新文件 `tools/progressive.py`)

`apply_progressive_tools(tm, config) -> ProgressiveToolHook | None`:
1. `config.enabled=False` → 返回 None。
2. `eager_names = config.eager_tools or 默认`(默认 = 33 内置名 + `tools_search` + `invoke_tool`)。
3. 强制 `tools_search`/`invoke_tool` 在 `eager_names`(对齐 jiuwenswarm `_ensure_progressive_meta_tools`)。
4. 构造 `ToolsSearchTool(tm, eager_names)` + `InvokeToolTool(tm, eager_names)`,`tm.register` 两者。
5. 构造 `ProgressiveToolHook(eager_names)` 返回。
6. 权限守卫(#1):凡静态档位非 allow(`require-approval`/`deny`,读 `permissions.tools[name]` 或 `global_default`)的工具强制留 eager,防 `invoke_tool` 绕过 `PermissionHook` 审批门。

## 6. 数据流

启用时一个 invoke 内:
- `before_invoke`(invoke 开始):注导航 frozen_section。
- 每步 loop L524 `add frozen_sections` → 导航进 system prompt。
- L528 `ctx.inputs.tools = 全量`;L529 `before_model_call` 过滤为 eager。
- L562 `llm.stream(tools=eager)`。
- 模型看到 eager schema + 导航。要用 deferred:调 `tools_search(name)` → 返 schema → 调 `invoke_tool(name, args)` → `tm.execute` → 结果。

eager 固定 + 导航 frozen_section 固定 → 跨步稳定 → 命中 prefix cache(对齐 agent.py L499-500 现有注释意图)。

## 7. 错误处理

- `tools_search` 未匹配:`success=false` + 引导回看导航(对齐 jiuwenswarm)。
- `invoke_tool` 名不在 deferred:返回"该工具已在 tools 列表,直接调用"或 not found。
- meta-tool `invoke` 异常:传播到 agent loop 的 `format_tool_error` + RetryHook `ON_TOOL_EXCEPTION`(对齐 catch-specific-then-propagate 约定;meta-tool 不 try/except,工具异常透传,见 `test_invoke_tool_propagates_deferred_tool_exception`)。
- progressive 关闭:builder 返回 None,create_agent 不加 hook、tm 不含 meta-tool → 完全等同现状。

## 8. 测试

对齐 Twinkle:`asyncio.run` + 无 pytest-asyncio。
- 单元:`ToolsSearchTool` 精确匹配(命中/大小写/未命中)、`InvokeToolTool` 转调 `tm.execute` + 拒绝 eager 工具、eager 默认值、强制 meta 在 eager。
- hook:`before_model_call` 过滤前后计数、`before_invoke` 注 frozen_section、无 deferred no-op、enabled=false no-op。
- 集成:启用 progressive + 注册 mock MCP 工具 → tools 列表只含 eager+meta;`tools_search` 找到 deferred MCP;`invoke_tool` 执行 deferred;两步 tools 内容相同(cache 稳定性)。
- 回归:progressive 关闭 → 行为等同现状。

## 9. 参考实现对照(与 jiuwenswarm 差异)

| 维度 | jiuwenswarm | Twinkle(本设计) |
|---|---|---|
| eager/deferred 二档 | ✓ | ✓ |
| 两 meta-tool 按名精确 | ✓ | ✓ |
| 导航清单(name+简述) | ✓ | ✓ |
| 固定 eager + cache | ✓ | ✓ |
| 机制承载 | `JiuWenProgressiveToolRail`(before_model_call rail) | `ProgressiveToolHook`(before_model_call + before_invoke) |
| deferred 工具执行 | `resource_mgr` 间接层 + `invoke_tool` | **直接 `tm.execute`**(省一层,因 Twinkle 工具实例统一在 ToolManager) |
| 默认 eager 名单 | 极小(8 个:读写编辑 grep glob bash + meta) | 内置全 eager(33)+ meta(deferred 只装 MCP) |
| enable_for_models | ✓ | ✗(不做) |
| subagent profile | ✓ | ✗(不做) |
| MCP allowlist | ✓(框架级) | ✗(不做) |
| 触发 | config + 模型白名单 | config 开关 |

关键简化:`invoke_tool` 直接转调 `ToolManager.execute`(因 Twinkle 工具实例统一在 tm),省 jiuwenswarm 的 `resource_mgr` 间接层。

## 10. 不做(YAGNI,显式声明)

`enable_for_models`、阈值自动触发、subagent profile/继承、MCP allowlist、向量检索、双语导航、team/subagent 白名单整合。
