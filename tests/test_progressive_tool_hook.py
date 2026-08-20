# tests/test_progressive_tool_hook.py
"""ProgressiveToolHook: before_model_call 过滤 tools 为 eager;before_invoke 注导航;
无 deferred 时 no-op。对齐 SkillHook 模式。tm 经 ctx.agent._tool_manager 取。"""
import asyncio

from twinkle.agentserver.hooks.base import (
    HookContext, HookEvent, ModelCallInputs, InvokeInputs,
)
from twinkle.agentserver.hooks.builtin.progressive_tool_hook import ProgressiveToolHook
from twinkle.agentserver.tools.builtin.progressive_tools import (
    ToolsSearchTool, InvokeToolTool, META_NAMES,
)
from twinkle.agentserver.tools.decorator import tool
from twinkle.agentserver.tools.manager import ToolManager


def _setup(eager):
    @tool
    async def read_file(path: str) -> str:
        """read a file"""
        return f"content:{path}"

    @tool
    async def mcp_query(sql: str) -> str:
        """query db"""
        return f"rows:{sql}"

    m = ToolManager()
    m.register(read_file)
    m.register(mcp_query)
    m.register(ToolsSearchTool(m, eager))
    m.register(InvokeToolTool(m, eager))

    class _Agent:
        _tool_manager = m

    return m, ProgressiveToolHook(eager), _Agent


_EAGER = ["read_file", "tools_search", "invoke_tool"]


def test_before_model_call_filters_to_eager():
    m, hook, AgentCls = _setup(_EAGER)
    ctx = HookContext(
        agent=AgentCls(), event=HookEvent.BEFORE_MODEL_CALL,
        inputs=ModelCallInputs(messages=[], tools=m.schemas()),
        session_id="s", request_id="r",
    )
    asyncio.run(hook.before_model_call(ctx))
    names = [s["function"]["name"] for s in ctx.inputs.tools]
    assert "mcp_query" not in names        # deferred 被过滤
    assert "read_file" in names
    assert "tools_search" in names and "invoke_tool" in names


def test_before_invoke_injects_navigation():
    m, hook, AgentCls = _setup(_EAGER)
    ctx = HookContext(
        agent=AgentCls(), event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="q", mode=""),
        session_id="s", request_id="r",
    )
    asyncio.run(hook.before_invoke(ctx))
    navs = [s for s in ctx.extra.get("frozen_sections", [])
            if s.name == "tool_navigation"]
    assert len(navs) == 1
    content = navs[0].content
    assert "mcp_query" in content          # deferred 进导航
    assert "read_file" not in content      # eager 不进导航
    assert "- tools_search:" not in content   # meta 不作为导航条目(header 用法会提及)
    assert "- invoke_tool:" not in content    # meta 不作为导航条目
    assert "不可直接调用" in content
    assert "tools_search" in content and "invoke_tool" in content  # header 用法说明提及两 meta


def test_no_deferred_is_noop():
    # 只有 eager 工具 + meta,无 deferred → before_invoke 不注导航
    @tool
    async def read_file(path: str) -> str:
        """read"""
        return path

    m = ToolManager()
    m.register(read_file)
    m.register(ToolsSearchTool(m, _EAGER))
    m.register(InvokeToolTool(m, _EAGER))

    class _Agent:
        _tool_manager = m

    hook = ProgressiveToolHook(_EAGER)
    ctx = HookContext(
        agent=_Agent(), event=HookEvent.BEFORE_INVOKE,
        inputs=InvokeInputs(query="q", mode=""),
        session_id="s", request_id="r",
    )
    asyncio.run(hook.before_invoke(ctx))
    navs = [s for s in ctx.extra.get("frozen_sections", [])
            if s.name == "tool_navigation"]
    assert navs == []


# --- 端到端集成:ReActAgent + ProgressiveToolHook + mock deferred 工具 --- #

from twinkle.agentserver.agent import ReActAgent, AgentRequest, normal_base_sections
from twinkle.agentserver.llm_client import Finish
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.tools.progressive import apply_progressive_tools
from twinkle.config.schema import ProgressiveToolConfig, PermissionsConfig


class _CapturingLLM:
    """捕获每步 system + tools + 全量 messages;按 scripts 返回事件。"""
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0
        self.seen_systems: list[str] = []
        self.seen_tools: list[list[str]] = []
        self.seen_messages: list[list] = []

    async def stream(self, messages, tools):
        self.seen_systems.append(messages[0]["content"])
        self.seen_tools.append([s["function"]["name"] for s in (tools or [])])
        self.seen_messages.append(messages)
        events = self._scripts[self.calls]
        self.calls += 1
        for ev in events:
            yield ev


def _builtin_tm_with_deferred_mcp(tmp_path):
    """模拟 create_agent 的 tm:33 内置 + 1 mock MCP 工具(deferred)。"""
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()                       # 纯 33 内置
    # 模拟 MCP 灌入一个 deferred 工具
    @tool
    async def mcp_fake_query(sql: str) -> str:
        """fake mcp db query"""
        return f"rows:{sql}"
    tm.register(mcp_fake_query)
    return tm


def test_apply_disabled_returns_none_and_no_meta(tmp_path):
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    hook = apply_progressive_tools(tm, ProgressiveToolConfig(enabled=False), PermissionsConfig())
    assert hook is None
    names = [t.card.name for t in tm.list()]
    assert "tools_search" not in names
    assert "invoke_tool" not in names


def test_apply_enabled_registers_meta_and_eager_has_all_builtin(tmp_path):
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    builtin_names = {t.card.name for t in tm.list()}
    hook = apply_progressive_tools(tm, ProgressiveToolConfig(enabled=True), PermissionsConfig())
    assert hook is not None
    names = [t.card.name for t in tm.list()]
    assert "tools_search" in names and "invoke_tool" in names
    # 默认 eager = 全内置 + meta(MCP 若有则不在)
    for name in builtin_names:
        assert name in hook.eager_names
    assert "tools_search" in hook.eager_names


def test_end_to_end_eager_filter_plus_navigation_and_invoke(tmp_path):
    tm = _builtin_tm_with_deferred_mcp(tmp_path)
    hook = apply_progressive_tools(tm, ProgressiveToolConfig(enabled=True), PermissionsConfig())
    assert hook is not None
    # 模型编排:step1 调 tools_search 找 mcp_fake_query;step2 调 invoke_tool 执行;step3 完成
    llm = _CapturingLLM([
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "tools_search",
                                           "arguments": '{"tool_name": "mcp_fake_query"}'}}]})],
        [Finish("tool_calls", {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c2", "type": "function",
                              "function": {"name": "invoke_tool",
                                           "arguments": '{"tool_name": "mcp_fake_query", "arguments": {"sql": "SELECT 1"}}'}}]})],
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    agent = ReActAgent(llm, store, tm, hooks=(hook,),
                      base_sections=normal_base_sections(), max_steps=5)
    req = AgentRequest(session_id="s1", request_id="r1", query="query db")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())
    # 每步 tools 只含 eager + meta,deferred(mcp_fake_query)不暴露
    for step_tools in llm.seen_tools:
        assert "mcp_fake_query" not in step_tools
        assert "tools_search" in step_tools
        assert "invoke_tool" in step_tools
    # §8 cache 稳定性:各步 tools 列表必须逐字节相同(prefix cache 友好)
    assert llm.seen_tools[0] == llm.seen_tools[1] == llm.seen_tools[2]
    # 导航在每步 system
    for sys_text in llm.seen_systems:
        assert "按需可见工具导航" in sys_text
        assert "mcp_fake_query" in sys_text
    # 跑了 3 步
    assert llm.calls == 3
    # invoke_tool 实际执行了 deferred 工具(mcp_fake_query sql=SELECT 1),
    # 结果 "rows:SELECT 1" 回灌对话(step3 输入 messages 含 step2 的 tool result)。
    # 防 false-positive:broken invoke 不会产生此结果。
    assert "rows:SELECT 1" in str(llm.seen_messages[-1])


def test_end_to_end_progressive_off_is_status_quo(tmp_path):
    """progressive 关闭 → 无 hook、无 meta-tool,行为等同现状(回归守卫)。"""
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    hook = apply_progressive_tools(tm, ProgressiveToolConfig(enabled=False), PermissionsConfig())
    assert hook is None
    llm = _CapturingLLM([
        [Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    store = SessionStore(str(tmp_path / "sessions"))
    asyncio.run(store.create_session("s1"))
    agent = ReActAgent(llm, store, tm, hooks=(),
                      base_sections=normal_base_sections(), max_steps=2)
    req = AgentRequest(session_id="s1", request_id="r1", query="hi")

    async def _run():
        async for _frame in agent.run(req):
            pass

    asyncio.run(_run())
    step_tools = llm.seen_tools[0]
    assert "tools_search" not in step_tools      # 无 meta-tool
    # 无导航段
    assert "按需可见工具导航" not in llm.seen_systems[0]


def test_apply_refuses_to_defer_approval_tier_tools():
    """#1 权限守卫:invoke_tool 绕过 PermissionHook,故 builder 强制非 allow 档工具留 eager。"""
    from twinkle.agentserver.tools import tool_manager
    tm = tool_manager()
    # 用户自定义 eager 只列 read_file,企图把 command_exec(require-approval)defer
    permissions = PermissionsConfig(
        tools={"command_exec": "require-approval"}, global_default="allow")
    hook = apply_progressive_tools(
        tm, ProgressiveToolConfig(enabled=True, eager_tools=["read_file"]), permissions)
    assert hook is not None
    # command_exec 强制回 eager(不走 invoke_tool 绕过审批门)
    assert "command_exec" in hook.eager_names
    assert "read_file" in hook.eager_names
    assert "tools_search" in hook.eager_names
    # allow 档的内置工具可正常 defer(不在 eager)——对照证明只挡审批级
    # (任取一个不在 eager_tools 列表内的 allow 档内置;若 glob 非内置,改用 tm.list() 里另一 allow 档内置)
    allow_deferred = [t.card.name for t in tm.list()
                     if t.card.name not in hook.eager_names]
    assert allow_deferred, "至少一个 allow 档内置应被 defer"
    assert "command_exec" not in allow_deferred  # 审批级的不在 deferred 里
