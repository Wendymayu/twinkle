# Phase 19 Team 协作核心 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 Phase 18 Team 子系统加任务队列编排 + leader→member steer 注入 + member_name 寻址,实现单进程内任务驱动型多 agent 协作。

**Architecture:** 复用 Phase 18 Team 容器 + delegate 通路。新增 `TeamTaskStore`(复用 TodoStore 单例,`team:{sid}` key,加 claim 独占/依赖/环检测编排)、`MessageBox`(member 私有信箱)、`ReActAgent` inbox drain(每步排空注入内存 `msgs`,不进 session store)、`member_name` 寻址(替 persona hash)。member→leader 求助走 `metadata.help_reason` + 结束 run;leader→member 走 `send_member` steer。

**Tech Stack:** Python 3.11 asyncio;pytest 用 `asyncio.run()`(无 pytest-asyncio);Twinkle ReActAgent/ToolManager/TodoStore/SessionStore/hook 体系。

**参考 spec:** `docs/superpowers/specs/2026-08-07-phase19-team-collaboration-core-design.md`

---

## File Structure

- **Create** `twinkle/agentserver/team/message_box.py` — `MessageBox`:包 `asyncio.Queue` 的 `put`/`drain`,member 私有信箱
- **Create** `twinkle/agentserver/team/task_store.py` — `TeamTaskStore`:复用 TodoStore 单例,team-level key,加 claim 独占/依赖解除/环检测/release_claims
- **Modify** `twinkle/agentserver/agent.py` — `ReActAgent.__init__` 加 `inbox` 参数 + `_Inbox` Protocol;`_run_react_loop` 循环头 drain;`build_member_system_prompt` 加 `member_name`;`_TEAM_LEADER_TOOL_WHITELIST` 加 task 工具
- **Modify** `twinkle/agentserver/team/manager.py` — `Team`:`_members: dict[member_name, ReActAgent]`、`_inboxes`、`task_store`、`_personas`;`_member_key`/`_member_session_id`/`_ensure_member`/`_build_member`/`delegate` 改 member_name 签名;`send_member` 方法;`_drive_member` 加 member_name + release;`MEMBER_TOOL_WHITELIST` 加 task 工具
- **Modify** `twinkle/agentserver/tools/builtin/team_tools.py` — `delegate_to_member` 加 `member_name`;新增 `create_task`/`claim_task`/`complete_task`/`cancel_task`/`list_tasks`/`get_task`/`send_member`
- **Modify** `twinkle/agentserver/tools/__init__.py` — 注册新 team task 工具
- **Modify** `twinkle/agentserver/team/context.py` — 加 `CURRENT_MEMBER_NAME` ContextVar(member run 时 `_drive_member` set,member 工具自动读)
- **Modify** `twinkle/agentserver/team/__init__.py` — 导出 `MessageBox`、`TeamTaskStore`
- **Modify** `tests/test_team.py` — 修签名相关测试 + 加 member_name/send_member/inbox/task_store 测试

---

## Task 1: MessageBox

**Files:**
- Create: `twinkle/agentserver/team/message_box.py`
- Test: `tests/test_message_box.py`

- [ ] **Step 1: Write failing test**

`tests/test_message_box.py`:
```python
from twinkle.agentserver.team.message_box import MessageBox


def test_put_drain_returns_messages_in_order():
    box = MessageBox()
    box.put("hello")
    box.put("world")
    assert box.drain() == ["hello", "world"]


def test_drain_empty_returns_empty_list():
    box = MessageBox()
    assert box.drain() == []


def test_drain_clears_queue():
    box = MessageBox()
    box.put("a")
    assert box.drain() == ["a"]
    assert box.drain() == []


def test_empty_reflects_state():
    box = MessageBox()
    assert box.empty() is True
    box.put("x")
    assert box.empty() is False
    box.drain()
    assert box.empty() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_message_box.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'twinkle.agentserver.team.message_box'`

- [ ] **Step 3: Implement MessageBox**

`twinkle/agentserver/team/message_box.py`:
```python
"""MessageBox — member 私有信箱,包 asyncio.Queue 提供 drain 便捷方法。

纯 FIFO(put/drain),不带持久化/优先级/审计(那些是 TeamMessageStore 编排语义,YAGNI 不做)。
对齐 spec §1.3 / §5.4:leader send_member → box.put;member run 循环每步 box.drain。
"""
from __future__ import annotations

import asyncio


class MessageBox:
    """Member 私有信箱:包 asyncio.Queue 提供 drain 便捷方法。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def put(self, content: str) -> None:
        """非阻塞投递一条消息(member run 时 drain)。"""
        self._queue.put_nowait(content)

    def drain(self) -> list[str]:
        """非阻塞排空,返回所有未读消息(无则空 list)。"""
        out: list[str] = []
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out

    def empty(self) -> bool:
        return self._queue.empty()
```

- [ ] **Step 4: Export from team package**

`twinkle/agentserver/team/__init__.py` — 在 import 区加 `from twinkle.agentserver.team.message_box import MessageBox`,在 `__all__` 加 `"MessageBox"`。

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_message_box.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/team/message_box.py twinkle/agentserver/team/__init__.py tests/test_message_box.py
git commit -m "feat(team): add MessageBox for member inbox (put/drain)"
```

---

## Task 2: ReActAgent inbox drain

`ReActAgent` 加可选 inbox,`_run_react_loop` 每步开头 drain,消息加到内存 `msgs`(不 append session store,对齐 spec §5.3)。

**Files:**
- Modify: `twinkle/agentserver/agent.py`(`__init__` 加 inbox + `_Inbox` Protocol;`_run_react_loop` 循环头 drain)
- Test: `tests/test_agent_inbox.py`

- [ ] **Step 1: Write failing test**

`tests/test_agent_inbox.py`:
```python
import asyncio

from twinkle.agentserver.agent import AgentRequest, ReActAgent
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.sessions import SessionStore
from twinkle.agentserver.team.message_box import MessageBox
from twinkle.agentserver.tools.manager import ToolManager


class _RecordingLLM:
    """Scripted LLM that records the messages it received per call."""

    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0
        self.received_messages: list = []

    async def stream(self, messages, tools):
        self.received_messages = list(messages)
        for ev in self._scripts[self.calls]:
            yield ev
        self.calls += 1


def _run_agent(store, llm, inbox, query="do task", sid="s1"):
    agent = ReActAgent(llm, store, ToolManager(), inbox=inbox)

    async def _go():
        await store.create_session(sid)
        async for _ in agent.run(AgentRequest(session_id=sid, request_id="r1", query=query)):
            pass

    asyncio.run(_go())


def test_inbox_message_reaches_llm_but_not_session_store(session_store):
    box = MessageBox()
    box.put("steer: add risk section")
    llm = _RecordingLLM([
        [TextDelta("ok"), Finish("stop", {"role": "assistant", "content": "ok", "tool_calls": None})],
    ])
    _run_agent(session_store, llm, box)

    # LLM 收到的 messages 含 steer
    assert any("steer: add risk section" in (m.get("content") or "")
               for m in llm.received_messages)
    # session store 不含 steer(不进历史)
    history = session_store.get_history("s1")
    assert not any("steer: add risk section" in (m.get("content") or "")
                   for m in history)


def test_no_inbox_does_not_break_run(session_store):
    llm = _RecordingLLM([
        [TextDelta("done"), Finish("stop", {"role": "assistant", "content": "done", "tool_calls": None})],
    ])
    _run_agent(session_store, llm, inbox=None)
    assert llm.calls == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_inbox.py -v`
Expected: FAIL — `ReActAgent.__init__() got an unexpected keyword argument 'inbox'`

- [ ] **Step 3: Add `_Inbox` Protocol + `inbox` param to `ReActAgent.__init__`**

In `twinkle/agentserver/agent.py`:

(a) 顶部 import 区加(若未有)`from typing import Protocol`,并加 Protocol 定义(放在 `AgentRequest` dataclass 附近):
```python
class _Inbox(Protocol):
    """信箱协议:member 信箱有 drain()→list[str]。MessageBox 实现此协议。

    agent.py 不 import team 包(避免循环);leader 传 None,member 传 MessageBox。
    """
    def drain(self) -> list[str]: ...
```

(b) `ReActAgent.__init__` 签名加 `inbox` keyword-only 参数(当前签名: `(self, llm, store, tools, *, hooks=(), max_steps=None)`):
```python
def __init__(
    self,
    llm: LLMClient,
    store: SessionStore,
    tools: ToolManager,
    *,
    hooks: tuple[AgentHook, ...] = (),
    max_steps: int | None = None,
    inbox: _Inbox | None = None,
) -> None:
    self._llm = llm
    self._session_store = store
    self._tool_manager = tools
    self._hook_manager = HookManager()
    for h in hooks:
        self._hook_manager.register_hook(h)
    self._max_steps = max_steps if max_steps is not None else MAX_STEPS
    self._inbox = inbox
```

- [ ] **Step 4: Add drain to `_run_react_loop` cycle head**

In `_run_react_loop`,循环头当前是(agent.py 约 L477-478):
```python
for _step in range(self._max_steps):
    msgs = self._session_store.get_messages(session_id)
```
改成(在 `msgs = ...` 之后插 drain,加到内存 `msgs`,**不** append session store):
```python
for _step in range(self._max_steps):
    msgs = self._session_store.get_messages(session_id)
    if self._inbox is not None:
        _new = self._inbox.drain()
        if _new:
            msgs = list(msgs) + [{"role": "user", "content": m} for m in _new]
```
其余(`BEFORE_MODEL_CALL`、`ctx.inputs = ModelCallInputs(messages=msgs, ...)`、`self._llm.stream(messages=ctx.inputs.messages, ...)`)不变——`msgs` 已含 drain 内容,且未写回 store。

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_inbox.py -v`
Expected: 2 PASS

- [ ] **Step 6: Run existing agent tests to ensure no regression**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: All PASS(leader/normal 模式 inbox=None,drain 分支不进)

- [ ] **Step 7: Commit**

```bash
git add twinkle/agentserver/agent.py tests/test_agent_inbox.py
git commit -m "feat(agent): add optional inbox drain in ReActAgent run loop (steer injection)"
```

---

## Task 3: member_name 寻址 + Team inboxes/send_member + member prompt

`Team` 改用 `member_name` 作 key(替 persona hash),持 `_inboxes: dict[member_name, MessageBox]`,加 `send_member` 方法;`build_member_system_prompt` 注入 `member_name`;`delegate_to_member` 兼建+启(member_name + persona),不需独立 create_member 工具(spec §2.3 工具列表无 create_member)。

**Files:**
- Modify: `twinkle/agentserver/agent.py`(`build_member_system_prompt` 加 `member_name`)
- Modify: `twinkle/agentserver/team/manager.py`(`Team`:`_members`/`_inboxes`/`_personas`/`task_store`;`_member_key`/`_member_session_id`/`_ensure_member`/`_build_member`/`delegate`/`send_member` 改 member_name)
- Test: `tests/test_team.py`(加 member_name/send_member 测试)

- [ ] **Step 1: Write failing test**

`tests/test_team.py` 加(沿用 helpers `_team_with_scripted_llm`、`session_store`):
```python
def test_member_session_id_uses_member_name(session_store):
    team = _team_with_scripted_llm(session_store, [])
    sid = team._member_session_id("researcher")
    assert "researcher" in sid
    assert sid.startswith("s1__team_")  # _session_id="s1"


def test_ensure_member_keys_by_member_name(session_store):
    team = _team_with_scripted_llm(session_store, [])
    asyncio.run(team._ensure_member("researcher", "金融分析师"))
    assert "researcher" in team._members
    assert "researcher" in team._inboxes


def test_ensure_member_rejects_same_name_different_persona(session_store):
    team = _team_with_scripted_llm(session_store, [])
    asyncio.run(team._ensure_member("researcher", "金融分析师"))
    import pytest
    with pytest.raises(Exception):
        asyncio.run(team._ensure_member("researcher", "different persona"))


def test_send_member_puts_into_inbox(session_store):
    team = _team_with_scripted_llm(session_store, [])
    asyncio.run(team._ensure_member("researcher", "金融分析师"))
    asyncio.run(team.send_member("researcher", "add risk section"))
    assert team._inboxes["researcher"].drain() == ["add risk section"]


def test_send_member_unknown_name_errors(session_store):
    team = _team_with_scripted_llm(session_store, [])
    import pytest
    with pytest.raises(KeyError):
        asyncio.run(team.send_member("nobody", "msg"))


def test_member_prompt_contains_member_name(session_store):
    team = _team_with_scripted_llm(session_store, [])
    asyncio.run(team._ensure_member("researcher", "金融分析师"))
    member_sid = team._member_session_id("researcher")
    history = session_store.get_history(member_sid)
    assert "researcher" in history[0]["content"]
```

顶部加 `import asyncio` 和 `import pytest`(若未 import)。

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_team.py -k "member_session_id_uses_member_name or ensure_member_keys or send_member or member_prompt_contains_member_name" -v`
Expected: FAIL — `Team._member_session_id` 还用 persona;`_inboxes` 不存在;`send_member` 不存在

- [ ] **Step 3: Add `member_name` to `build_member_system_prompt`**

`twinkle/agentserver/agent.py` 的 `build_member_system_prompt`(约 L275)改签名 + 在 team_role 段注入 member_name:
```python
def build_member_system_prompt(*, persona: str, workspace: str,
                               member_name: str = "") -> str:
    """Build a member's system prompt with team identity front and center. ..."""
    name_line = f"（成员名: `{member_name}`）" if member_name else ""
    return f"""# 团队角色

你是 Teammate{name_line}，{persona}

作为团队成员:
- Leader 定义"做什么",你来决定"怎么做"
- 聚焦任务目标,自主搜索、执行、产出
- 认领 team task 时写 owner=<你的成员名>;任务完成调 complete_task 回报
- 任务完成后给出清晰总结,让 Leader 能直接整合

# 当前人设

{persona}

# 团队共享工作区

路径: `{workspace}`
所有文件读写操作在此目录内进行。

---

{build_agent_runtime_prompt()}"""
```

- [ ] **Step 3b: Add `CURRENT_MEMBER_NAME` ContextVar**

`twinkle/agentserver/team/context.py`(当前含 `CURRENT_TEAM` + `MEMBER_WORKSPACE`,L11/L16)末尾加 member 身份 ContextVar:
```python
CURRENT_MEMBER_NAME: ContextVar[str | None] = ContextVar(
    "current_member_name", default=None)
```
(整文件已有 `from contextvars import ContextVar`,无需新 import。)member run 时 `_drive_member` 的 `_run()` set `CURRENT_MEMBER_NAME(member_name)`(Task 7 Step 3),member 调 `claim_task`/`complete_task` 时 `_current_member_name()` 读它(team_tools.py),LLM 无需显式传 member_name。

- [ ] **Step 4: Refactor `Team` to key by `member_name` + add inboxes/personas/task_store**

`twinkle/agentserver/team/manager.py` 的 `Team.__init__`(约 L65-79)加字段:
```python
def __init__(self, llm, store, parent_tools, session_id, config) -> None:
    self._llm = llm
    self._store = store
    self._parent_tools = parent_tools
    self._session_id = session_id
    self._config = config
    self._members: dict[str, "ReActAgent"] = {}
    self._inboxes: dict[str, "MessageBox"] = {}          # NEW: member_name → MessageBox
    self._personas: dict[str, str] = {}                   # NEW: member_name → persona(§3.3 同名冲突校验)
    self.workspace = ensure_team_workspace(session_id)
```
顶部 import 加 `from twinkle.agentserver.team.message_box import MessageBox`。

- [ ] **Step 5: Rewrite member-key/session-id helpers to use member_name**

替换 `_member_key` + `_member_session_id`(约 L83-89):
```python
@staticmethod
def _member_key(member_name: str) -> str:
    """member_name 即 key(spec §3.1:稳定可读,替代 persona hash)。"""
    return member_name

def _member_session_id(self, member_name: str) -> str:
    return f"{self._session_id}__team_{member_name}"
```

- [ ] **Step 6: Rewrite `_ensure_member` / `_build_member` to take member_name + persona**

替换约 L93-133:
```python
async def _ensure_member(self, member_name: str, persona: str) -> "ReActAgent":
    if member_name in self._members:
        if self._personas[member_name] != persona:
            raise ValueError(
                f"member_name '{member_name}' already used for a different persona")
        return self._members[member_name]
    member = await self._build_member(member_name, persona)
    self._members[member_name] = member
    self._personas[member_name] = persona
    self._inboxes[member_name] = MessageBox()
    return member

async def _build_member(self, member_name: str, persona: str) -> "ReActAgent":
    from twinkle.agentserver.agent import ReActAgent, build_member_system_prompt
    tm = ToolManager()
    for t in self._parent_tools.list():
        if t.card.name in MEMBER_TOOL_WHITELIST:
            tm.register(t)
    member_sid = self._member_session_id(member_name)
    await self._store.create_session(member_sid)
    system_prompt = build_member_system_prompt(
        persona=persona,
        workspace=str(self.workspace),
        member_name=member_name,
    )
    await self._store.append(member_sid, {"role": "system", "content": system_prompt})
    hooks = [SkillHook(), MemoryHook(), LoggingHook(), RetryHook()]
    member = ReActAgent(
        self._llm, self._store, tm,
        hooks=tuple(hooks),
        max_steps=SUBAGENT_MAX_STEPS,
    )
    # 注入 member 自己的信箱(steer drain)
    member._inbox = self._inboxes[member_name]
    return member
```
注意:这里直接设 `member._inbox`(ReActAgent.__init__ 已支持 inbox 参数,但 _build_member 为复用现有构造,在此赋值;若想走构造传参,改成 `ReActAgent(..., inbox=self._inboxes[member_name])`)。推荐后者——把 `member = ReActAgent(...)` 那行改成:
```python
    return ReActAgent(
        self._llm, self._store, tm,
        hooks=tuple(hooks),
        max_steps=SUBAGENT_MAX_STEPS,
        inbox=self._inboxes[member_name],
    )
```
(删掉 `member._inbox = ...` 那行。)

- [ ] **Step 7: Rewrite `delegate` signature + add `send_member`**

替换 `delegate`(约 L137-150)+ 加 `send_member`:
```python
async def delegate(self, member_name: str, persona: str,
                   objective: str, prompt: str = "") -> str:
    """Delegate to a member by name; builds+starts if first time. Run to convergence."""
    member = await self._ensure_member(member_name, persona)
    member_sid = self._member_session_id(member_name)
    query = f"{objective}\n\n{prompt}" if prompt else objective
    from twinkle.agentserver.agent import AgentRequest
    request = AgentRequest(
        session_id=member_sid,
        request_id=f"{self._session_id}__team_{uuid.uuid4().hex[:8]}",
        query=query,
    )
    return await self._drive_member(member, request, member_name)

async def send_member(self, member_name: str, content: str) -> str:
    """Leader → member 单向 steer:投递到 member 信箱,不阻塞。

    member 跑时 run 循环每步 drain;idle 时滞留信箱,下次 delegate 启动时 drain(无害)。
    """
    if member_name not in self._inboxes:
        raise KeyError(f"unknown member: {member_name}")
    self._inboxes[member_name].put(content)
    return f"sent to {member_name}"
```
注意:`_drive_member` 签名加 `member_name`(Task 7 用;此处先加预留参数保持签名稳定,Task 7 在 finally 填 release 逻辑)。临时:
```python
async def _drive_member(self, member, request, member_name: str = "") -> str:
    # ... 现有逻辑不变(member_name 在 Task 7 用于 release)...
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/test_team.py -k "member_session_id or ensure_member or send_member or member_prompt_contains_member_name" -v`
Expected: 新 6 个 PASS

- [ ] **Step 9: Fix broken existing tests (signature changes)**

`delegate(persona, objective, prompt)` → `delegate(member_name, persona, objective, prompt)`;`_build_member(persona)` → `_build_member(member_name, persona)`;`_member_key(persona)` 现返 `member_name`。逐个修 `tests/test_team.py`:

- `test_member_key_stable`(L70-75):删 `assert len(k1) == 16`(不再 blake2b)。改成 `assert Team._member_key("researcher") == "researcher"`(member_name 即 key)。
- `test_member_key_different_persona`(L78-81):`_member_key("researcher")` vs `_member_key("writer")` 断言 `!=` 仍通过(不同 name 不同 key),无需改。
- `test_build_member_filtered_tools`(L115-132):`await team._build_member("tester")` → `await team._build_member("tester", "tester persona")`。断言(tool_names 含 web_search/read_file/write_file/command_exec,不含 spawn_subagent/delegate_to_member/write_memory)不变。
- `test_build_member_persona_in_system_prompt`(L134-148):`await team._build_member("researcher")` → `await team._build_member("researcher", "金融分析师")`。断言 `"researcher" in history[0]["content"]` 仍成立(member_name="researcher" 进成员名行)。
- `test_build_member_workspace_in_prompt`(L150-162):`await team._build_member("tester")` → `await team._build_member("tester", "tester persona")`。断言 workspace 不变。
- `test_delegate_runs_member_to_completion`(L167-173):`asyncio.run(team.delegate("researcher", "analyze data"))` → `asyncio.run(team.delegate("researcher", "researcher persona", "analyze data"))`。断言 `"analysis done" in result` 不变。
- `test_delegate_reuses_member`(L176-186):两次 `team.delegate("researcher", "task1")` / `"task2"` → `team.delegate("researcher", "researcher persona", "task1")` / `"task2")`。断言 `len(team._members) == 1` 仍通过(key=member_name="researcher")。
- `test_delegate_to_member_no_contextvar`(L193-202):`delegate_to_member.func("researcher", "task")` → `delegate_to_member.func("researcher", "researcher persona", "task")`。断言 `"team unavailable"` 不变。
- `test_member_prompt_*`(L320-369):调 `build_member_system_prompt(persona="tester", workspace="/tmp/ws")` 不传 member_name → `member_name` 默认 `""`,`name_line=""`,prompt 仍是 `你是 Teammate，{persona}`。断言含 Teammate/团队角色/当前人设/团队共享工作区/运行环境/工具使用指南/todo_create 仍成立,**无需改**。

跑全部:
```bash
python -m pytest tests/test_team.py -v
```
Expected: All PASS(新 6 个 + 修好的现有 7 处;`test_member_key_different_persona` / `test_member_prompt_*` 无需改仍通过)

- [ ] **Step 10: Commit**

```bash
git add twinkle/agentserver/agent.py twinkle/agentserver/team/manager.py tests/test_team.py
git commit -m "feat(team): member_name addressing + inboxes + send_member steer"
```

---

## Task 4: TeamTaskStore

复用 `TodoStore` 单例,team-level key `f"team:{leader_sid}"`,加 claim 独占 / 依赖解除 / 环检测 / release_claims。claim 在 `TodoStore._lock` 内 load→校验 owner 空→save(spec §4.3 复用 _lock)。

**Files:**
- Create: `twinkle/agentserver/team/task_store.py`
- Modify: `twinkle/agentserver/team/manager.py`(`Team.__init__` 建 `self.task_store = TeamTaskStore(f"team:{self._session_id}")`)
- Test: `tests/test_task_store.py`

- [ ] **Step 1: Write failing test**

`tests/test_task_store.py`:
```python
import asyncio
import pytest

from twinkle.agentserver.team.task_store import TeamTaskStore
from twinkle.agentserver.todo import TodoError, _set_todo_store


@pytest.fixture
def store(tmp_path):
    from twinkle.agentserver.todo.store import TodoStore
    s = TodoStore(str(tmp_path / "todos"))
    _set_todo_store(s)
    yield s
    _set_todo_store(None)


def _new(store):
    return TeamTaskStore("team:s1")


def test_create_task_pending(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("调研 X"))
    assert t.status == "pending"
    assert t.subject == "调研 X"
    assert t.owner == ""


def test_claim_sets_owner_and_in_progress(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    claimed = asyncio.run(ts.claim_task(t.id, "researcher"))
    assert claimed.owner == "researcher"
    assert claimed.status == "in_progress"


def test_claim_rejects_already_claimed(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    with pytest.raises(TodoError):
        asyncio.run(ts.claim_task(t.id, "writer"))


def test_claim_rejects_blocked_by_uncompleted(store):
    ts = _new(store)
    t1 = asyncio.run(ts.create_task("T1"))
    t2 = asyncio.run(ts.create_task("T2", blocked_by=[t1.id]))
    with pytest.raises(TodoError):  # T1 未完成,T2 不能 claim
        asyncio.run(ts.claim_task(t2.id, "writer"))


def test_claim_allows_after_dependency_completed(store):
    ts = _new(store)
    t1 = asyncio.run(ts.create_task("T1"))
    t2 = asyncio.run(ts.create_task("T2", blocked_by=[t1.id]))
    asyncio.run(ts.claim_task(t1.id, "researcher"))
    asyncio.run(ts.complete_task(t1.id, "result", "researcher"))
    # T1 completed → T2 解除 blocked,可 claim
    claimed = asyncio.run(ts.claim_task(t2.id, "writer"))
    assert claimed.owner == "writer"


def test_complete_rejects_wrong_owner(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    with pytest.raises(TodoError):
        asyncio.run(ts.complete_task(t.id, "r", "writer"))


def test_complete_sets_result_and_completed(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    done = asyncio.run(ts.complete_task(t.id, "调研结果", "researcher"))
    assert done.status == "completed"
    assert done.result == "调研结果"


def test_no_false_cycle_on_linear_chain(store):
    """线性依赖链 T3→T2→T1 不应误报环。"""
    ts = _new(store)
    t1 = asyncio.run(ts.create_task("T1"))
    t2 = asyncio.run(ts.create_task("T2", blocked_by=[t1.id]))
    t3 = asyncio.run(ts.create_task("T3", blocked_by=[t2.id]))
    assert t3.status == "pending"


def test_has_cycle_detects_mutual_dependency(store):
    """直接构造 A↔B 互依(A.blocked_by=[B], B.blocked_by=[A]),create C blocked_by=[A]
    时从 A 出发 DFS 命中 visited 的 B→A,应拒绝。"""
    import time as _time
    from twinkle.agentserver.todo import TodoTask
    ts = _new(store)
    now = _time.time()
    a = TodoTask(id="A", subject="A", status="pending", blocked_by=["B"],
                 created_at=now, updated_at=now)
    b = TodoTask(id="B", subject="B", status="pending", blocked_by=["A"],
                 created_at=now, updated_at=now)
    # 绕过 create_task 的顺序校验,直接 seed 一对互依 task
    async def _seed():
        async with ts._store._lock("team:s1"):
            ts._store._save("team:s1", [a, b])
    asyncio.run(_seed())
    # create C blocked_by=[A]:从 A 走 → B → A(visited 命中)→ 环
    with pytest.raises(TodoError):
        asyncio.run(ts.create_task("C", blocked_by=["A"]))
```
(注:`create_task` 时新 task 尚无 id,不会出现在任何现有 task 的 blocked_by 链里,故 create 时的环检测只防"dep 之间已成环"的种子数据;`_has_cycle` 本身用直接构造的互依 tasks 单测更稳。)

```python
def test_request_help_sets_metadata(store):
    """member 求助:标 metadata.help_reason,不改 status(spec §1.4)。"""
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    helped = asyncio.run(ts.request_help(t.id, "need X data", "researcher"))
    assert helped.metadata.get("help_reason") == "need X data"
    assert helped.status == "in_progress"  # 不改 status,留 release 回 pending
    assert helped.owner == "researcher"     # owner 保留(释放时才清)


def test_request_help_rejects_wrong_owner(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    with pytest.raises(TodoError):
        asyncio.run(ts.request_help(t.id, "reason", "writer"))


def test_release_claims_preserves_help_reason(store):
    """release 把 in_progress 回 pending + owner 清空,但 metadata.help_reason 保留(spec §7)。"""
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    asyncio.run(ts.request_help(t.id, "stuck on X", "researcher"))
    count = asyncio.run(ts.release_claims("researcher"))
    assert count == 1
    after = asyncio.run(ts.get_task(t.id))
    assert after.status == "pending"
    assert after.owner == ""
    assert after.metadata.get("help_reason") == "stuck on X"  # 保留
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_task_store.py -v`
Expected: FAIL — `No module named 'twinkle.agentserver.team.task_store'`

- [ ] **Step 3: Implement TeamTaskStore**

`twinkle/agentserver/team/task_store.py`:
```python
"""TeamTaskStore — team 共享任务队列,复用 TodoStore 单例,加编排层。

spec §4:claim 独占(校验 owner 空) + 依赖解除(派生) + 环检测(DFS) + 4 态。
复用 TodoStore 单例(按 team:{sid} 存,leader/member 共享同一队列);
claim 在 TodoStore._lock 内 load→校验→save(spec §4.3 复用 _lock)。
member→leader 求助走 metadata.help_reason(不混 blocked)。
"""
from __future__ import annotations

import time

from twinkle.agentserver.todo import TodoError, TodoTask, get_todo_store


class TeamTaskStore:
    """team session 级编排层,复用 TodoStore 单例。"""

    def __init__(self, team_session_id: str) -> None:
        # team_session_id 形如 "team:{leader_sid}";leader/member 用同一 key
        self._sid = team_session_id

    @property
    def _store(self):
        # 惰性取单例——Team.__init__ 建 TeamTaskStore 时不触发 get_todo_store(),
        # 避免非 team-task 测试(如 test_team.py 的 member 测试,conftest 的 todo
        # fixture 非 autouse)写脏默认 todo 目录。TeamTaskStore 方法调时才取。
        return get_todo_store()

    # ── 内部 helper(复用 TodoStore 的 _lock/_load/_save/_find_by_id,spec §4.3)──

    def _find(self, tasks: list[TodoTask], task_id: str) -> TodoTask | None:
        # _find_by_id 是 TodoStore staticmethod(store.py:107-112),实例可调
        return self._store._find_by_id(tasks, task_id)

    def _met_blocked_by(self, tasks: list[TodoTask], task_id: str) -> TodoTask | None:
        """return the task if it's completed, else None(用于依赖校验)。"""
        t = self._find(tasks, task_id)
        return t if (t is not None and t.status == "completed") else None

    def _has_cycle(self, tasks: list[TodoTask], start_id: str, visited: set[str]) -> bool:
        """DFS 环检测:start_id 沿 blocked_by 走,回到自己则成环。"""
        t = self._find(tasks, start_id)
        if t is None:
            return False
        for dep in t.blocked_by:
            if dep in visited:
                return True
            visited.add(dep)
            if self._has_cycle(tasks, dep, visited):
                return True
            visited.discard(dep)
        return False

    # ── public API ──

    async def create_task(self, subject: str,
                          blocked_by: list[str] | None = None) -> TodoTask:
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            deps = list(blocked_by or [])
            # 环检测:每个新依赖链不能回到自己
            for dep in deps:
                visited = {dep}
                if self._has_cycle(tasks, dep, visited):
                    raise TodoError(f"cycle detected: {dep} dependency chain loops")
            now = time.time()
            task = TodoTask(
                id=f"t-{now}-{len(tasks)}",
                subject=subject,
                status="pending",
                blocked_by=deps,
                created_at=now,
                updated_at=now,
            )
            tasks.append(task)
            self._store._save(self._sid, tasks)
            return task

    async def claim_task(self, task_id: str, member_name: str) -> TodoTask:
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            t = self._find(tasks, task_id)
            if t is None:
                raise TodoError(f"Task {task_id} not found.")
            if t.status != "pending":
                raise TodoError(f"Task {task_id} not pending (status={t.status}).")
            if t.owner:
                raise TodoError(f"Task {task_id} already claimed by {t.owner}.")
            unmet = [d for d in t.blocked_by if self._met_blocked_by(tasks, d) is None]
            if unmet:
                raise TodoError(f"Task {task_id} blocked by uncompleted: {unmet}")
            t.owner = member_name
            t.status = "in_progress"
            t.updated_at = time.time()
            self._store._save(self._sid, tasks)
            return t

    async def complete_task(self, task_id: str, result: str,
                            member_name: str) -> TodoTask:
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            t = self._find(tasks, task_id)
            if t is None:
                raise TodoError(f"Task {task_id} not found.")
            if t.owner != member_name:
                raise TodoError(
                    f"Task {task_id} not owned by {member_name} (owner={t.owner}).")
            t.status = "completed"
            t.result = (result or "").strip() or "done"
            t.updated_at = time.time()
            self._store._save(self._sid, tasks)
            return t
            # 依赖解除是派生:其他 task 的 blocked 在它们 claim 时靠
            # _met_blocked_by 检查(前置 completed 即解除),无需主动改别的 task。

    async def request_help(self, task_id: str, reason: str,
                            member_name: str) -> TodoTask:
        """member 执行中遇困难,在 task 上标 metadata.help_reason(spec §1.4)。

        不改 status(留 in_progress);member run 结束后 release_claims 把它回
        pending、owner 清空,但 metadata 保留——leader 通过 list_tasks/get_task
        看 help_reason 决定是否 steer 或重派。blocked 专属依赖派生,求助不混 blocked。
        """
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            t = self._find(tasks, task_id)
            if t is None:
                raise TodoError(f"Task {task_id} not found.")
            if t.owner != member_name:
                raise TodoError(
                    f"Task {task_id} not owned by {member_name} (owner={t.owner}).")
            t.metadata["help_reason"] = (reason or "").strip() or "unspecified"
            t.updated_at = time.time()
            self._store._save(self._sid, tasks)
            return t

    async def cancel_task(self, task_id: str) -> TodoTask:
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            t = self._find(tasks, task_id)
            if t is None:
                raise TodoError(f"Task {task_id} not found.")
            t.status = "cancelled"
            t.owner = ""
            t.updated_at = time.time()
            self._store._save(self._sid, tasks)
            return t

    async def list_tasks(self, status: str | None = None) -> list[TodoTask]:
        return await self._store.list(self._sid, status=status)

    async def get_task(self, task_id: str) -> TodoTask | None:
        return await self._store.get(self._sid, task_id)

    async def release_claims(self, member_name: str) -> int:
        """member 退出时释放其 claim 但未 complete 的 task(spec §7)。

        owner 清空、status 回 pending;metadata 保留(help_reason 不丢)。
        """
        async with self._store._lock(self._sid):
            tasks = self._store._load(self._sid)
            now = time.time()
            count = 0
            for t in tasks:
                if t.owner == member_name and t.status == "in_progress":
                    t.owner = ""
                    t.status = "pending"
                    t.updated_at = now
                    count += 1
            if count:
                self._store._save(self._sid, tasks)
            return count
```
**注:** `_store` 是 `@property` 惰性取 `get_todo_store()` 单例(非 `__init__` 时取),故 `Team.__init__` 建 `TeamTaskStore` 无副作用。`_find` 调 `TodoStore._find_by_id`(staticmethod,store.py:107-112)。`list_tasks`/`get_task` 复用 `TodoStore.list(session_id, status)`/`get(session_id, task_id)`(store.py:203-220,均含 session_id 参数)。

- [ ] **Step 4: Wire `TeamTaskStore` into `Team`**

`twinkle/agentserver/team/manager.py` 顶部 import 加 `from twinkle.agentserver.team.task_store import TeamTaskStore`。`Team.__init__` 末尾加:
```python
    self.task_store = TeamTaskStore(f"team:{session_id}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_task_store.py -v`
Expected: 12 PASS(状态机/claim 独占/依赖阻塞与解除/环检测/complete owner 校验/request_help metadata/release 保留 help_reason)

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/team/task_store.py twinkle/agentserver/team/manager.py tests/test_task_store.py
git commit -m "feat(team): TeamTaskStore (claim/dependency/cycle/release over TodoStore)"
```

---

## Task 5: team task/message 工具

`team_tools.py` 加 `create_task`/`claim_task`/`complete_task`/`cancel_task`/`list_tasks`/`get_task`/`send_member`;`delegate_to_member` 加 `member_name`;注册到 `tool_manager()`。

**Files:**
- Modify: `twinkle/agentserver/tools/builtin/team_tools.py`
- Modify: `twinkle/agentserver/tools/__init__.py`
- Test: `tests/test_team_tools.py`

- [ ] **Step 1: Write failing test**

`tests/test_team_tools.py`:
```python
import asyncio

from twinkle.agentserver.team.context import CURRENT_TEAM
from twinkle.agentserver.team.manager import Team, TeamManager
from twinkle.agentserver.tools.builtin import team_tools


def _team(session_store):
    mgr = TeamManager(llm=None, store=session_store, parent_tools=None,
                     config=None)
    team = mgr.ensure_team("s1")
    CURRENT_TEAM.set(team)
    return team


def test_send_member_no_contextvar():
    CURRENT_TEAM.set(None)
    out = asyncio.run(team_tools.send_member.func("researcher", "hi"))
    assert "team unavailable" in out


def test_create_task_then_list(session_store, isolated_todo_store):
    _team(session_store)
    out = asyncio.run(team_tools.create_task.func("调研 X"))
    assert "Created" in out or "调研" in out
    listed = asyncio.run(team_tools.list_tasks.func())
    assert "调研 X" in listed


def test_claim_complete_flow(session_store, isolated_todo_store):
    team = _team(session_store)
    t = asyncio.run(team.task_store.create_task("T1"))  # 直接拿真实 task id
    claimed = asyncio.run(team_tools.claim_task.func(t.id, "researcher"))
    assert "researcher" in claimed or "Claimed" in claimed
    done = asyncio.run(team_tools.complete_task.func(t.id, "结果"))
    assert "Completed" in done or "completed" in done.lower()
```
(注:`isolated_todo_store` fixture(conftest.py)自动 `_set_todo_store(tmp)` + reset,防写脏默认 todo 目录。`test_send_member_no_contextvar` 不触 task_store,无需该 fixture。`test_claim_complete_flow` 用 `team.task_store.create_task` 直接拿 `t.id`,不依赖 list_tasks 字符串解析。)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_team_tools.py -v`
Expected: FAIL — `team_tools.create_task` 不存在

- [ ] **Step 3: Implement tools in `team_tools.py`**

`twinkle/agentserver/tools/builtin/team_tools.py` — 在 `delegate_to_member` 旁加(顶部 import 加 `from twinkle.agentserver.todo import TodoError`):
```python
@tool
async def delegate_to_member(member_name: str, persona: str, objective: str,
                              prompt: str = "") -> str:
    """委派任务给团队成员。成员是独立 agent,看不到你的对话历史。第一次委派某 member_name 会创建该成员。

    member_name: 成员名(简短英文标识,如 researcher)。稳定可读,用于 task owner/消息寻址。
    persona: 成员角色描述。如 "金融分析师,专长美股财报分析"。
    objective: 任务目标。自包含——成员所需一切应在此描述。主路径用"认领并执行 queue 中你能做的 task"。
    prompt: 可选,额外上下文或具体指令。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    return await team.delegate(member_name, persona, objective, prompt)


@tool
async def create_task(subject: str, blocked_by: list[str] | None = None) -> str:
    """创建一个 team 共享任务入队。blocked_by 指定前置依赖(它们的 id)。

    subject: 任务主题/目标。
    blocked_by: 可选,前置 task id 列表;这些 task completed 后本 task 才能被认领。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    try:
        t = await team.task_store.create_task(subject, blocked_by=blocked_by)
        return f"Created task {t.id}: {t.subject}" + (
            f" (blocked_by {t.blocked_by})" if t.blocked_by else "")
    except TodoError as exc:
        return f"Error: {exc}"


@tool
async def claim_task(task_id: str, member_name: str = "") -> str:
    """认领一个 team task(独占)。需 pending 且无 owner 且前置全完成。

    task_id: 要认领的 task id。
    member_name: 你的成员名(作 owner)。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    name = member_name or _current_member_name()
    try:
        t = await team.task_store.claim_task(task_id, name)
        return f"Claimed task {t.id}: {t.subject} (owner={t.owner})"
    except TodoError as exc:
        return f"Error: {exc}"


@tool
async def complete_task(task_id: str, result: str = "",
                        help_reason: str = "",
                        member_name: str = "") -> str:
    """完成你认领的 task(写结果),或在遇困难时请求 leader 帮助(标 help_reason)。

    task_id: 你的 task id。
    result: 任务结果/产出(完成时)。
    help_reason: 遇困难求助时写明原因。非空时标 metadata.help_reason + 不标 completed;
                 member run 结束后 task 回 pending,leader 通过 list_tasks 看 help_reason
                 决定 steer/重派(spec §1.4 求助,不混 blocked)。
    member_name: 你的成员名(可省,自动取)。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    name = member_name or _current_member_name()
    try:
        if help_reason:
            t = await team.task_store.request_help(task_id, help_reason, name)
            return f"Help requested on task {t.id}: {help_reason}"
        t = await team.task_store.complete_task(task_id, result, name)
        return f"Completed task {t.id}."
    except TodoError as exc:
        return f"Error: {exc}"


@tool
async def cancel_task(task_id: str) -> str:
    """取消一个 team task(leader 用)。"""
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    try:
        t = await team.task_store.cancel_task(task_id)
        return f"Cancelled task {t.id}."
    except TodoError as exc:
        return f"Error: {exc}"


@tool
async def list_tasks(status: str = "") -> str:
    """列出所有 team task。可按 status 过滤(pending/in_progress/completed/cancelled)。"""
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    tasks = await team.task_store.list_tasks(status=status or None)
    return _format_team_tasks(tasks)


@tool
async def get_task(task_id: str) -> str:
    """查看单个 team task 详情(含 result/owner/blocked_by/help_reason)。"""
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    t = await team.task_store.get_task(task_id)
    if t is None:
        return f"Task {task_id} not found."
    return _format_team_tasks([t])


@tool
async def send_member(member_name: str, message: str) -> str:
    """向指定成员发送异步消息(steer,非阻塞)。消息进入成员信箱,成员下次运行时读取。只在 member 跑时有效调整方向;idle 时滞留。

    member_name: 目标成员名。
    message: 消息内容(运行中调整方向用,不派发任务——任务走 create_task)。
    """
    team = CURRENT_TEAM.get()
    if team is None:
        return "[team unavailable]"
    try:
        return await team.send_member(member_name, message)
    except KeyError:
        return f"Error: unknown member '{member_name}'"


def _current_member_name() -> str:
    """member 工具调用时取自己的 member_name。

    member run 时 `_drive_member` 的 `_run()` set `CURRENT_MEMBER_NAME` ContextVar
    (Task 7 实现),member 调工具时自动取,无需 LLM 显式传参。Task 7 落地前
    返回空——此时 `claim_task(task_id, member_name)` 走显式传参 fallback。
    """
    from twinkle.agentserver.team.context import CURRENT_MEMBER_NAME
    return CURRENT_MEMBER_NAME.get() or ""


def _format_team_tasks(tasks) -> str:
    if not tasks:
        return "No team tasks."
    icon = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]",
            "cancelled": "[-]"}
    lines = []
    for t in tasks:
        ic = icon.get(t.status, "[ ]")
        deps = (f" (blocked by: {', '.join(t.blocked_by)})" if t.blocked_by else "")
        owner = f" [@{t.owner}]" if t.owner else ""
        res = f" | {t.result}" if t.result else ""
        help_r = t.metadata.get("help_reason")
        help_line = f" ⚠help: {help_r}" if help_r else ""
        lines.append(f"- {ic} {t.id}: {t.subject}{deps}{owner}{res}{help_line}")
    return "\n".join(lines)
```
注意:`claim_task`/`complete_task` 的 `member_name` 默认空走 `_current_member_name()`(读 `CURRENT_MEMBER_NAME` ContextVar)。member run 时 `_drive_member` 的 `_run()` set 此 ContextVar(Task 7 Step 3 实现),故 member 调工具时自动取自己的 name,LLM 无需显式传。Task 7 落地前 ContextVar 未 set,返回空——`claim_task(task_id, "researcher")` 走显式传参(Task 5 测试即如此)。`complete_task` 加 `help_reason` 参数:非空 → 调 `request_help`(标 metadata、不标 completed);空 → 正常 complete(spec §1.4 求助分流)。

- [ ] **Step 4: Register tools in `tool_manager()`**

`twinkle/agentserver/tools/__init__.py` 的 `tool_manager()`(约 L49 `tm.register(team_tools.delegate_to_member)` 旁)加:
```python
    tm.register(team_tools.delegate_to_member)
    tm.register(team_tools.create_task)       # NEW
    tm.register(team_tools.claim_task)        # NEW
    tm.register(team_tools.complete_task)     # NEW
    tm.register(team_tools.cancel_task)       # NEW
    tm.register(team_tools.list_tasks)        # NEW
    tm.register(team_tools.get_task)          # NEW
    tm.register(team_tools.send_member)       # NEW
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_team_tools.py -v`
Expected: PASS

- [ ] **Step 6: Verify all team tools registered**

```python
# 临时 sanity:
python -c "from twinkle.agentserver.tools import tool_manager; names=[t.card.name for t in tool_manager().list()]; print([n for n in names if n in ('create_task','claim_task','complete_task','cancel_task','list_tasks','get_task','send_member','delegate_to_member')])"
```
Expected: 8 个名字都在。

- [ ] **Step 7: Commit**

```bash
git add twinkle/agentserver/tools/builtin/team_tools.py twinkle/agentserver/tools/__init__.py tests/test_team_tools.py
git commit -m "feat(team): team task/message tools (create/claim/complete/cancel/list/get/send_member)"
```

---

## Task 6: 白名单重配

`_TEAM_LEADER_TOOL_WHITELIST` 加 task 工具 + `send_member`;`MEMBER_TOOL_WHITELIST` 加 `claim_task`/`complete_task`/`list_tasks`/`get_task`(member 执行,不 create/cancel)。

**Files:**
- Modify: `twinkle/agentserver/agent.py`(`_TEAM_LEADER_TOOL_WHITELIST`)
- Modify: `twinkle/agentserver/team/manager.py`(`MEMBER_TOOL_WHITELIST`)
- Test: `tests/test_team.py`(白名单断言)

- [ ] **Step 1: Write failing test**

`tests/test_team.py` 加:
```python
def test_leader_whitelist_has_team_task_tools():
    from twinkle.agentserver.agent import _TEAM_LEADER_TOOL_WHITELIST
    for name in ("create_task", "cancel_task", "list_tasks", "get_task",
                 "send_member", "delegate_to_member"):
        assert name in _TEAM_LEADER_TOOL_WHITELIST, f"missing {name}"


def test_member_whitelist_has_claim_complete():
    for name in ("claim_task", "complete_task", "list_tasks", "get_task"):
        assert name in MEMBER_TOOL_WHITELIST, f"missing {name}"
    # member 不应能 create/cancel/send_member(协调权归 leader)
    for name in ("create_task", "cancel_task", "send_member"):
        assert name not in MEMBER_TOOL_WHITELIST, f"{name} should not be in member whitelist"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_team.py -k "leader_whitelist_has_team_task or member_whitelist_has_claim" -v`
Expected: FAIL — 新工具不在白名单

- [ ] **Step 3: Update `_TEAM_LEADER_TOOL_WHITELIST`**

`twinkle/agentserver/agent.py`(约 L317)frozenset 加:
```python
_TEAM_LEADER_TOOL_WHITELIST: frozenset[str] = frozenset({
    # Coordination
    "delegate_to_member",
    "create_task", "cancel_task", "list_tasks", "get_task",   # NEW: team task 编排
    "send_member",                                            # NEW: leader→member steer
    # Planning & tracking
    "todo_create", "todo_update", "todo_list", "todo_get",
    # Read-only inspection
    "read_file", "list_files", "glob",
    "web_search", "web_fetch",
    "memory_search", "read_memory",
    "list_skill", "read_skill",
    "cron_list_jobs",
})
```

- [ ] **Step 4: Update `MEMBER_TOOL_WHITELIST`**

`twinkle/agentserver/team/manager.py`(约 L45)frozenset 加:
```python
MEMBER_TOOL_WHITELIST: frozenset[str] = frozenset({
    "web_search", "web_fetch",
    "read_file", "write_file", "edit_file", "list_files", "glob",
    "command_exec",
    "memory_search", "read_memory",
    "todo_create", "todo_update", "todo_list", "todo_get",
    "claim_task", "complete_task", "list_tasks", "get_task",   # NEW: member 执行 team task
    "list_skill", "read_skill",
    "cron_list_jobs", "cron_create_job", "cron_update_job",
    "cron_delete_job", "cron_run_now",
})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_team.py -k "whitelist" -v`
Expected: PASS(含新 2 个 + 现有 whitelist 测试——注意现有 `test_leader_whitelist_excludes_execution_tools` 断言不含 command_exec/write_file 等,仍成立;`test_leader_whitelist_has_coordination_tools` 含 delegate_to_member/read_file/web_search/todo_create,仍成立)

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/agent.py twinkle/agentserver/team/manager.py tests/test_team.py
git commit -m "feat(team): wire task tools into leader/member whitelists"
```

---

## Task 7: member 退出释放认领

`_drive_member` 在 member run 结束(正常/超时/错误)时,调 `task_store.release_claims(member_name)` 释放该 member claim 但未 complete 的 task(spec §7 关键边界)。

**Files:**
- Modify: `twinkle/agentserver/team/manager.py`(`_drive_member` 加 `member_name` + finally release)
- Test: `tests/test_team.py`

- [ ] **Step 1: Write failing test**

`tests/test_team.py` 加(需 member claim 一个 task 后 run 结束未 complete → task 释放):
```python
def test_member_run_end_releases_uncompleted_claim(session_store, isolated_todo_store):
    team = _team_with_scripted_llm(session_store, [
        # member run 一轮就 stop(claim 了但没 complete)
        [TextDelta("claimed"), Finish("stop", {"role": "assistant",
          "content": "claimed", "tool_calls": None})],
    ])
    # 建一个 task 并手动让 member claim(member 没真调工具,模拟)
    t = asyncio.run(team.task_store.create_task("T1"))
    asyncio.run(team.task_store.claim_task(t.id, "researcher"))
    # member run 结束(delegate 跑完)
    asyncio.run(team.delegate("researcher", "researcher persona", "claim T1"))
    # member run 结束 → T1 应被释放(未 complete)
    after = asyncio.run(team.task_store.get_task(t.id))
    assert after.status == "pending"
    assert after.owner == ""
```
(注:这个测试模拟 member claim 但没 complete;实际 member 若调 claim_task 工具会更真实,但 scripted LLM 不调工具,故手动 claim 模拟 member 已 claim 的状态。核心断言:run 结束后未 complete 的 claim 被释放。)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_team.py -k "member_run_end_releases" -v`
Expected: FAIL — `_drive_member` 还没 release,task 卡在 in_progress

- [ ] **Step 3: Add release to `_drive_member` finally**

`twinkle/agentserver/team/manager.py` 的 `_drive_member`(Task 3 已加 `member_name` 预留参数)。现在在 `finally` 块加 release。当前 finally(约 L193-199)是 cancel runner + abort。改成:
```python
async def _drive_member(self, member, request, member_name: str = "") -> str:
    queue: asyncio.Queue = asyncio.Queue()

    async def _run():
        from twinkle.agentserver.team.context import MEMBER_WORKSPACE, CURRENT_MEMBER_NAME
        MEMBER_WORKSPACE.set(self.workspace)
        if member_name:
            CURRENT_MEMBER_NAME.set(member_name)
        try:
            async for frame in member.run(request):
                await queue.put(frame)
        except Exception as exc:
            await queue.put(exc)
        finally:
            await queue.put(None)

    runner = asyncio.create_task(_run())
    final = ""
    try:
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=SUBAGENT_SOFT_TIMEOUT)
            except asyncio.TimeoutError:
                return "[member timeout]"
            if frame is None:
                break
            if isinstance(frame, Exception):
                log.warning("member error: %s", frame)
                return f"[member error: {type(frame).__name__}]"
            if frame.response_kind == "e2a.complete":
                final = frame.body.get("result", {}).get("content", "") or ""
            elif frame.response_kind == "e2a.error":
                return f"[member error: {frame.body.get('error', 'unknown')}]"
        if len(final) > SUBAGENT_MAX_RESULT_CHARS:
            final = final[:SUBAGENT_MAX_RESULT_CHARS] + "\n…[truncated]"
        return final
    finally:
        if not runner.done():
            runner.cancel()
        try:
            await asyncio.wait_for(runner, timeout=SUBAGENT_ABORT_TIMEOUT)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        # NEW: member run 结束,释放其 claim 但未 complete 的 task(spec §7)
        if member_name:
            try:
                released = await self.task_store.release_claims(member_name)
                if released:
                    log.info("released %d claimed task(s) of member %s",
                             released, member_name)
            except Exception as exc:
                log.warning("release_claims failed for %s: %s", member_name, exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_team.py -k "member_run_end_releases or delegate" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/team/manager.py tests/test_team.py
git commit -m "feat(team): release member's uncompleted claims on run end (spec §7)"
```

---

## Task 8: 端到端数据流 + spec §6 验证

验证完整链路:leader create_task(带依赖)→ delegate 启动 member → member claim/complete → 依赖解除 → 第二个 member claim → leader 综合。含 steer 注入演示。

**Files:**
- Test: `tests/test_team_flow.py`

- [ ] **Step 1: Write end-to-end test**

`tests/test_team_flow.py`:
```python
import asyncio

from twinkle.agentserver.agent import AgentRequest
from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.team.context import CURRENT_TEAM
from twinkle.agentserver.tools.builtin import team_tools


def test_full_flow_create_claim_complete_dependency(session_store, isolated_todo_store):
    """leader 拆 T1/T2(blocked_by T1)→ researcher claim+complete T1 →
    writer claim T2(依赖已解除)→ 全完成。"""
    from tests.test_team import _team_with_scripted_llm
    # researcher 跑一轮:claim T1 + complete T1
    team = _team_with_scripted_llm(session_store, [
        [TextDelta("done"), Finish("stop", {"role": "assistant",
          "content": "T1 done", "tool_calls": None})],
    ])
    CURRENT_TEAM.set(team)

    # leader 建 T1, T2(blocked_by T1)
    t1 = asyncio.run(team.task_store.create_task("调研 X"))
    t2 = asyncio.run(team.task_store.create_task("写报告", blocked_by=[t1.id]))

    # researcher 认领+完成 T1(直接走 task_store 模拟 member 工具调用)
    asyncio.run(team.task_store.claim_task(t1.id, "researcher"))
    asyncio.run(team.task_store.complete_task(t1.id, "调研结果", "researcher"))

    # T1 completed → T2 依赖解除,writer 可 claim
    claimed_t2 = asyncio.run(team.task_store.claim_task(t2.id, "writer"))
    assert claimed_t2.owner == "writer"

    # writer 读 T1 result(get_task)
    t1_after = asyncio.run(team.task_store.get_task(t1.id))
    assert t1_after.result == "调研结果"

    # writer 完成 T2
    asyncio.run(team.task_store.complete_task(t2.id, "报告写好", "writer"))
    tasks = asyncio.run(team.task_store.list_tasks())
    assert all("completed" in line or "[x]" in line for line in tasks.split("\n") if line.strip())


def test_steer_injection_into_member_run(session_store):
    """leader send_member → member run 下一轮 drain 注入(spec §6 可选 steer 演示)。"""
    from tests.test_team import _team_with_scripted_llm
    team = _team_with_scripted_llm(session_store, [
        [TextDelta("got it"), Finish("stop", {"role": "assistant",
          "content": "got it", "tool_calls": None})],
    ])
    asyncio.run(team._ensure_member("writer", "写手"))
    # leader 在 member 跑前/中发 steer(member inbox)
    asyncio.run(team.send_member("writer", "加风险提示节"))
    # member run → drain 注入(member 的 _inbox)
    asyncio.run(team.delegate("writer", "写手", "写报告"))
    # member 的 inbox 应已 drain(send_member 投的已取走)
    assert team._inboxes["writer"].drain() == []
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/test_team_flow.py -v`
Expected: 2 PASS

- [ ] **Step 3: Run full test suite to verify no regression**

Run: `python -m pytest tests/ -v`
Expected: All PASS(含 test_message_box / test_agent_inbox / test_team / test_task_store / test_team_tools / test_team_flow + 现有 test_agent_loop / test_todo_* 等)

- [ ] **Step 4: Commit**

```bash
git add tests/test_team_flow.py
git commit -m "test(team): end-to-end flow (create/claim/complete/dependency + steer)"
```

---

## Self-Review

**1. Spec coverage:**
- §1.2 TeamTaskStore → Task 4 ✓
- §1.2 leader→member steer → Task 2(drain)+ Task 1(Box)+ Task 3(send_member)✓
- §1.2 member_name → Task 3 ✓
- §1.3 mailbox + steer 注入 → Task 1 + 2 + 3 ✓
- §1.3 状态机/依赖图/环检测 → Task 4 ✓
- §1.4 leader 不收消息 + member 求助 → 设计(member→leader 走 task `metadata.help_reason`,无 leader inbox);Task 4 `request_help` 方法 + Task 5 `complete_task(help_reason=...)` 分流 + Task 5 `list_tasks`/`get_task` 显示 help_reason + Task 7 release 保留 metadata ✓
- §1.5 member 间不直接协作 → defer(spec 明说 defer,不实现)✓
- §2.2 四组件(Box/TaskStore/inbox drain/member_name)→ Task 1/4/2/3 ✓
- §2.3 工具 → Task 5 ✓
- §3 member 身份 → Task 3 ✓
- §4 task queue → Task 4 ✓
- §5 通信+steer → Task 1+2+3+5 ✓
- §6 数据流 → Task 8 ✓
- §7 错误处理(环检测/claim 独占/超时/释放/steer 滞留)→ Task 4(环/claim)+ Task 7(释放)+ Task 2(steer drain, idle 滞留无害)✓
- §8 测试 → 各 Task TDD + Task 8 ✓

**2. Placeholder scan:** 无 TBD/TODO/占位。`_current_member_name()` 读 `CURRENT_MEMBER_NAME` ContextVar(非硬编码返空);`TeamTaskStore._find` 用真实 `self._store._find_by_id(...)`(TodoStore staticmethod);`_store` 是 `@property` 惰性取单例(非 `__init__` 时取)。`complete_task(help_reason=...)` 分流到 `request_help`(spec §1.4 求助有工具支持)。

**3. Type consistency:**
- `MessageBox.put(content: str)` / `drain() -> list[str]` — 全文一致(Task 1 定义,Task 2/3 用)
- `TeamTaskStore` 方法签名:`create_task(subject, blocked_by=None)→TodoTask`;`claim_task(task_id, member_name)→TodoTask`;`complete_task(task_id, result, member_name)→TodoTask`;`request_help(task_id, reason, member_name)→TodoTask`(Task 4);`cancel_task(task_id)→TodoTask`;`list_tasks(status=None)→list[TodoTask]`;`get_task(task_id)→TodoTask|None`;`release_claims(member_name)→int` — Task 4/5/7 一致
- `complete_task` 工具(`task_id, result="", help_reason="", member_name=""`)非空 help_reason → `task_store.request_help`,空 → `task_store.complete_task` — Task 4/5 一致
- `Team.delegate(member_name, persona, objective, prompt)` / `send_member(member_name, content)` — Task 3 定义,Task 5/7/8 用,一致
- `ReActAgent.__init__(..., inbox=_Inbox | None)` — Task 2 定义,Task 3 `_build_member` 传 `inbox=self._inboxes[member_name]`,一致
- `build_member_system_prompt(*, persona, workspace, member_name="")` — Task 3 改签名,Task 3 `_build_member` 调用传 member_name,现有 `test_member_prompt_*` 不传(默认 "")不破坏,一致
- `_drive_member(member, request, member_name="")` + `_run()` set `CURRENT_MEMBER_NAME(member_name)` — Task 3 加预留参数,Task 7 实现 release + ContextVar set,一致

**注意事项(执行时):**
- Task 3 Step 6 的 `_build_member` 用构造传 inbox(`ReActAgent(..., inbox=self._inboxes[member_name])`),删原 `member._inbox = ...` 行(避免绕构造)
- Task 5 的 `claim_task`/`complete_task` 测试(Task 5 Step 1)显式传 member_name(Task 7 落地前 ContextVar 未 set);Task 7 落地后 member 跑时自动 set
- Task 7 测试用手动 claim 模拟(scripted LLM 不调工具,故手动 `team.task_store.claim_task` 模拟 member 已 claim);核心验证 release 逻辑——run 结束后未 complete 的 claim 回 pending + owner 空
- Task 4 `TeamTaskStore._store` 是 `@property`,`Team.__init__` 建 `TeamTaskStore(f"team:{sid}")` 不触发 `get_todo_store()`,故 `test_team.py` 的 member 测试(只用 `session_store` fixture,conftest 的 todo fixture 非 autouse)不会写脏默认 todo 目录
