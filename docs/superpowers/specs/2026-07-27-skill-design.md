# Skill 系统设计

## 一句话概括

Agent 从"调原子工具"升级到"调用打包的指令束(skill)"——每个 skill 是一个目录里的 `SKILL.md`(frontmatter + 指令体),`SkillManager` 扫描 + 热重载,模型在 `all` / `auto_list` 两种发现模式(配置切换、默认 `all`)下按需把 `SKILL.md` 读进上下文指导多步任务。挂在 `before_model_call` Hook 上,核心 ReAct 循环零结构改动。

---

## 为什么需要 Skill

工具是原子能力(web_fetch / command_exec / todo …),但一类多步任务(刷文档、做代码审查、跑调研)需要**一整套流程 + 多个工具的协同编排**。把这套流程硬塞进 system prompt 不现实(随任务类型增长),硬编码进 agent_loop 又把循环搞乱。Skill 是**比 tool 高一层的抽象**——一个 skill 打包一份指令(`SKILL.md`)+ 隐含的工具用法,agent 按需载入,载入后用现有 builtin tool 执行。

类比:tool 是单个函数,skill 是一份"操作手册"——agent 遇到一类任务时先翻对应手册,再照手册调工具。

---

## 设计来源

对照 jiuwenswarm(`openjiuwen` 框架层 + `jiuwenswarm/` 应用层),Twinkle 做**同构但裁剪**的实现:

| jiuwenswarm | Twinkle | 说明 |
|---|---|---|
| `Skill(name, description, directory)` 极小模型 | 同 | 运行时只这三字段 |
| `SkillManager` 扫 `skills/` 目录 | 同 | + mtime 热重载(照 Twinkle permission_overrides 模式) |
| `SkillUseRail`(DeepAgentRail, priority=100) | `SkillHook`(AgentHook, priority=90) | jiuwenswarm 的 Rail = Twinkle 的 Hook(同构,见 hook-design.md §设计来源) |
| `SkillTool` / `ListSkillTool` 工具 | `read_skill` / `list_skill` @tool | 同:模型调工具懒载入 SKILL.md,body 作 tool_result |
| `all` / `auto_list` / `agentic` 三模式 | `all` / `auto_list` 两模式 | `agentic`(树索引)企业级,不做 |
| `all` 用 `PromptAttachmentManager` + window-mutator 注入 | hook prepend 到 `ctx.inputs.messages` | 粗版等价,不引入新抽象 |
| `skill_turbo/` planner→executor→fallback 子 agent | 不做 | Phase 8 subagent 后置 |
| marketplace / SkillNet / symphony 树检索 | 不做 | 企业级,roadmap §明确超出范围 |
| `trigger` frontmatter 解析后丢弃 | 同 | 模型靠 description 自己选,不做关键词自动匹配 |

---

## 数据模型

运行时模型极小,对位 jiuwenswarm:

```python
@dataclass
class Skill:
    name: str                 # skill 名,作唯一 key
    description: str          # 给模型看的一句话描述(模型据此决定要不要用)
    directory: Path           # skill 目录绝对路径(读 SKILL.md / 附带文件用)
```

**只有这三字段**。无 priority(那是 prompt section 的概念,skill 本身没有)、无 version、无 bundled-tools 列表、无 trigger。

### SKILL.md 盘上格式

一个 skill = `<SKILLS_DIR>/<skill_name>/SKILL.md`(目录名即 skill 名),YAML frontmatter + markdown 指令体:

```markdown
---
name: doc-audit
description: 以源码为唯一事实来源,系统性核对并更新项目文档,确保内容与实现一致。
trigger: 用户提到"刷文档"、"更新文档"、"check 文档"等关键词时加载。
---

## 核心原则
- 源码是唯一事实来源...

## 操作流程
1. 确定范围...
2. 逐文件审计...
3. 修复 + 验收...
```

- `name` / `description` 进运行时 `Skill` 对象。
- **`trigger` 解析后丢弃**(对齐 jiuwenswarm):loader 读 YAML 时认得它,但不存进 `Skill`。模型看不到 `trigger`,只看 `description` 自己决定。**不做关键词自动匹配**——理由见 §设计决策回顾。
- markdown 体是给模型读的指令(操作流程、注意事项、用哪些 builtin tool),载入后作为 tool_result 进 ReAct 上下文。
- skill 目录可附带其他文件(脚本/示例),`read_skill` 的 `relative_file_path` 参数可读它们(默认读 `SKILL.md`)。

---

## SkillManager

`twinkle/agentserver/skills/store.py`,职责:扫目录、解析、缓存、热重载、白名单。

```python
class SkillManager:
    def __init__(self, skills_dir: str, enabled: list[str] | None = None):
        self._dir = skills_dir
        self._enabled = set(enabled) if enabled else None  # None = 全开
        self._mtime = -1.0
        self._skills: list[Skill] = []

    def list_skills(self) -> list[Skill]:
        self._refresh_if_changed()
        return self._skills

    def get_skill(self, name: str) -> Skill | None: ...

    def _refresh_if_changed(self) -> None:
        # stat skills_dir mtime;变了才 rescan(照 permission_overrides._load_overrides 模式)
        ...
```

### 扫描 + 热重载

- `_refresh_if_changed`:stat `<SKILLS_DIR>` mtime,变了才重扫(照 `permissions/policy.py` 的 `_load_overrides` mtime 模式)。目录不存在 / 空 → `[]`。
- 扫描:遍历 `<SKILLS_DIR>` 子目录,找 `SKILL.md`,YAML 解析 frontmatter 取 `name`+`description`,构造 `Skill(name, description, directory=<绝对路径>)`。
- **坏 skill 不崩**:frontmatter 缺 `name`/`description`、YAML 解析失败、目录没 `SKILL.md` → log + skip(对齐 SessionStore 坏行跳过、todo 文件损坏返 `[]`)。
- **白名单**:`TWINKLE_ENABLED_SKILLS` 非空时,只留名单内的 skill;空 = 全开。

### 单例

`twinkle/agentserver/skills/__init__.py`,进程级单例(照 `todo/__init__.py::get_todo_store()`):

```python
_SKILL_MANAGER: SkillManager | None = None

def get_skill_manager() -> SkillManager:
    global _SKILL_MANAGER
    if _SKILL_MANAGER is None:
        from twinkle.config import SKILLS_DIR, ENABLED_SKILLS
        _SKILL_MANAGER = SkillManager(SKILLS_DIR, ENABLED_SKILLS)
    return _SKILL_MANAGER

def _set_skill_manager(mgr: SkillManager | None) -> None: ...  # 测试钩子
```

`@tool` 函数和 `SkillHook` 都调 `get_skill_manager()`(call-time,便于测试 monkeypatch,对齐 todo 工具的模式)。

---

## 两种发现模式 + 配置切换

**模式是单值配置,一个 agent 跑一种,不并存**(对齐 jiuwenswarm `skill_mode`)。模型怎么"看到"skill 清单,由模式决定:

| 模式 | 模型怎么看到清单 | 何时耗 token | 实现 |
|---|---|---|---|
| `all`(默认) | 框架**每步主动塞**全量 name+desc 进上下文(SkillHook prepend system msg 到 `ctx.inputs.messages`) | 每步都带 N 条描述 | SkillHook.before_model_call 拼 + prepend |
| `auto_list` | 模型**要时调 `list_skill`** 拉清单 | 只用时花 | 注册 `list_skill` 工具 + SkillHook 注入一句提示 |

**选 skill 永远是模型的事**——两种模式都不替模型选,只决定"清单怎么到模型眼前"。载入都是模型调 `read_skill(name)` 读 SKILL.md → body 作 tool_result(详见 §完整数据流)。

### 为什么默认 `all`

skill 少(学习项目典型 1-5 个)时,`all` 让模型始终看见 skill 清单、主动想到用,体验顺;token 开销可忽略。skill 多了再切 `auto_list`。

---

## 工具

`twinkle/agentserver/tools/builtin/skill_tools.py`,两个 `@tool` 函数,注册进 `tool_manager()`(对齐 todo_tools):

| 工具 | 输入 | 返回(tool_result) | 何时用 |
|---|---|---|---|
| `list_skill` | _(无参)_ | markdown 清单 `## 可用技能\n0. name: desc\n1. ...`(空则 `No skills available.`) | `auto_list` 模式模型主动调;`all` 模式可不调(清单已注入)但注册无害 |
| `read_skill` | `skill_name: str, relative_file_path: str = "SKILL.md"` | SKILL.md(或指定文件)正文 | 模型选定 skill 后调,载入指令体 |

```python
@tool
async def list_skill() -> str:
    """List available skills (name + description). Call before read_skill to see the catalog."""
    mgr = get_skill_manager()
    skills = mgr.list_skills()
    if not skills:
        return "No skills available."
    lines = ["## 可用技能"] + [f"{i}. {s.name}: {s.description}" for i, s in enumerate(skills)]
    return "\n".join(lines)

@tool
async def read_skill(skill_name: str, relative_file_path: str = "SKILL.md") -> str:
    """Load a skill's instructions (SKILL.md by default). Pass the skill_name from list_skill."""
    mgr = get_skill_manager()
    skill = mgr.get_skill(skill_name)
    if skill is None:
        return f"Skill '{skill_name}' not found."
    path = Path(skill.directory) / relative_file_path
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Error reading skill '{skill_name}/{relative_file_path}': {exc}"
```

错误走字符串 tool_result(不抛、不炸 ReAct,对齐 todo_tools 的 `TodoError` 模式)。`read_skill` 复用普通文件读(skill 是本地文件,不需要 jiuwenswarm 的 VFS 抽象)。

---

## SkillHook

`twinkle/agentserver/hooks/builtin/skill_hook.py`,`AgentHook` priority **90**(功能层,见 hook-design.md §优先级设计:安全拦截 100+ / 功能 50-99 / 观察 0-49)。

```python
class SkillHook(AgentHook):
    priority = 90

    async def before_model_call(self, ctx: HookContext) -> None:
        mgr = get_skill_manager()
        skills = mgr.list_skills()
        if not skills:
            return  # 无 skill → no-op,不注入
        mode = SKILL_MODE  # "all" | "auto_list",from config
        if mode == "all":
            content = self._render_catalog(skills)
            self._prepend_system(ctx, content)
        else:  # auto_list
            self._prepend_system(ctx, "你有 skills 可用。需要时先调 list_skill 看清单,再调 read_skill(name) 载入指令。")

    def _prepend_system(self, ctx, content: str) -> None:
        # 用新 list 替换 ctx.inputs.messages(不 in-place mutate——msgs 可能是 store 的
        # 内部 list,in-place insert 会污染历史)。每步从 store 重读(agent_loop:161)
        # → 每步重设,正好"每轮带、不累积",store 不被改。
        ctx.inputs.messages = [{"role": "system", "content": content}] + ctx.inputs.messages
```

- 注入点 `before_model_call` + `ctx.inputs.messages` 可变——这是 Phase 3 给 Phase 5 记忆注入铺的同一钩子点(agent_loop.py:173 设 `ctx.inputs.messages`、:198 用、注释 :195-197 明说 hook 改它生效;context_compression 已用同款不写回 SessionStore)。
- **每步重注入,不累积**:`ctx.inputs.messages` 每步从 `store.get_messages` 重读(`agent_loop:161`)+ 压缩(`:164`),所以 SkillHook 每步 prepend 一次,正好 `all` 模式"每轮带清单"语义,且清单随 mtime 热重载更新。
- `list_skill` / `read_skill` 注册在 `tool_manager()`(全局),不分模式都注册——`all` 模式下模型也可调(清单已注入,通常不调,无害)。

### 注册

`build_agent_loop()`(server.py)始终注册 SkillHook(always-on,无 enabled flag):

```python
loop = build_agent_loop(store, hooks=[PermissionHook(engine), SkillHook(), LoggingHook()])
```

`main()` 里和 PermissionHook/LoggingHook 一起传入。无 skills 目录 = 无 skills = SkillHook no-op,无害(不像 permissions 需 opt-in 关掉,因为 skill 不挂起、不审计,默认开无副作用)。

### 与 PermissionHook 共存

- SkillHook priority 90,`before_model_call`;PermissionHook priority 100,`before_tool_call`。**不同事件,无 tie**。SkillHook 是当前唯一 `before_model_call` hook,其 priority 实际不与谁冲突,放 90 纯按"功能层"约定。

---

## 完整数据流

### `all` 模式

```
AgentLoop._inner_run_stream
  for step in range(MAX_STEPS):
    msgs = store.get_messages(sid)          # 每步重读历史
    msgs = compress_messages(msgs, ...)     # 压缩(不写回 store)
    ctx.inputs = ModelCallInputs(messages=msgs, tools=tool_manager.schemas())
    ├─ SkillHook.before_model_call(ctx)     # ★ 注入 skill 清单
    │     skills = get_skill_manager().list_skills()    # mtime 热重载
    │     ctx.inputs.messages = [{role:system, content:"# 可用技能\n0. ..."}] + ctx.inputs.messages
    ├─ llm.stream(ctx.inputs.messages, tools)          # LLM 看见清单
    │     ↓ 模型读清单,决定用某个 skill
    │     ↓ (模型发 tool_call: read_skill("doc-audit"))
    ├─ _hooked_tool_call → read_skill       # @tool 读 SKILL.md
    │     → tool_result = "<SKILL.md 正文>"   # body 作 {role:tool} 回灌
    ├─ store.append({role:tool, tool_call_id, content: tool_result})
    └─ 下一步 ReAct:模型带着 skill 指令 + 历史再查 LLM → 按 SKILL.md 流程调 builtin tool(read_file/command_exec/...)
```

### `auto_list` 模式

```
...同上到 before_model_call...
    ├─ SkillHook.before_model_call(ctx)     # ★ 只注入一句提示
    │     ctx.inputs.messages = [{role:system, content:"你有 skills,需要时调 list_skill..."}] + ctx.inputs.messages
    ├─ llm.stream(...)
    │     ↓ 模型觉得可能要用 skill
    │     ↓ (tool_call: list_skill)
    ├─ _hooked_tool_call → list_skill       # 返回 name+desc 清单
    │     → tool_result = "## 可用技能\n0. ..."
    ├─ store.append(...)
    └─ 下一步:模型挑 skill → tool_call: read_skill("doc-audit") → body 回灌 → 照流程执行
```

关键不变式:**skill body 永远不预载入**,只有模型调 `read_skill` 才进上下文(作 tool_result)。这贴 Twinkle 现有 ReAct(tool_result → store.append → 再查模型),agent_loop 零结构改动。

---

## 配置

`config.py` 加(对齐现有 `os.getenv("TWINKLE_X") or default` 风格):

```python
# --- skills (Phase 7) ---
SKILLS_DIR = os.getenv("TWINKLE_SKILLS_DIR") or str(Path(WORKSPACE_DIR) / "skills")
SKILL_MODE = os.getenv("TWINKLE_SKILL_MODE", "all")  # "all" | "auto_list"
_enabled_raw = (os.getenv("TWINKLE_ENABLED_SKILLS") or "").strip()
ENABLED_SKILLS = [s.strip() for s in _enabled_raw.split(",") if s.strip()]  # [] = 全开
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `TWINKLE_SKILLS_DIR` | `<WORKSPACE>/skills` | skill 目录(用户选 B,更显眼) |
| `TWINKLE_SKILL_MODE` | `all` | `all` / `auto_list` |
| `TWINKLE_ENABLED_SKILLS` | _(空=全开)_ | 逗号分隔白名单 |

`ensure_workspace_dir()`(server 启动调)顺带 `mkdir <WORKSPACE>/skills`,首次启动建空目录(用户往里放 SKILL.md 即生效,mtime 热重载自动扫到)。

---

## 示例 skill

仓库带 1 个最小示例(开箱可 demo + E2E 测试有真 skill 可调)。放 `twinkle/resources/skills/<name>/SKILL.md`,`ensure_workspace_dir` 首次启动时拷到 `<WORKSPACE>/skills/`(若不存在)。示例选一个简单、自包含的(如 `doc-audit`——刷文档核对,流程清晰、用的都是已有 builtin tool)。

---

## 文件地图

| 文件 | 角色 |
|---|---|
| `agentserver/skills/store.py` | `Skill` + `SkillManager`(扫描/mtime 热重载/白名单) |
| `agentserver/skills/__init__.py` | re-export + `get_skill_manager()` 单例 + `_set_skill_manager()` 测试钩子 |
| `agentserver/tools/builtin/skill_tools.py` | `list_skill` / `read_skill` 两个 @tool |
| `agentserver/hooks/builtin/skill_hook.py` | `SkillHook`(priority=90,before_model_call 注入) |
| `agentserver/server.py` | `build_agent_loop()` 注册 SkillHook;`ensure_workspace_dir` 建 + 拷贝示例 skill |
| `agentserver/tools/__init__.py` | `tool_manager()` 注册 `list_skill` / `read_skill` |
| `config.py` | `SKILLS_DIR` / `SKILL_MODE` / `ENABLED_SKILLS` |
| `resources/skills/<name>/SKILL.md` | 示例 skill |
| `e2a/models.py` | 无改动(skill 走 tool_result,不引入新 response_kind) |

---

## 与 jiuwenswarm 的差异

| | jiuwenswarm | Twinkle |
|---|---|---|
| `all` 注入 | `PromptAttachmentManager` + `PromptSection`(priority 段)+ ContextEngine window-mutator | hook prepend system msg 到 `ctx.inputs.messages`(粗版等价,无新抽象) |
| `auto_list` 过滤 | `ListSkillTool` 带 `query` 时跑独立 LLM call 过滤 | `list_skill` 全量返回,无 LLM 过滤(第一切片不做) |
| 发现模式 | `all` / `auto_list` / `agentic` 三种 | `all` / `auto_list` 两种(`agentic` 不做) |
| 工具注册 | 两 tier(`resource_mgr` 全局 + `ability_manager` per-agent) | 单 `ToolManager`(全局),第一切片够用 |
| `trigger` | 解析后丢弃 | 同 |
| 编排 | `skill_turbo/` planner→executor→fallback DeepAgent | 不做(Phase 8 subagent 后置) |
| 生态 | marketplace / SkillNet / symphony 树检索 | 不做(企业级,roadmap §明确超出范围) |
| Rail 优先级 | `SkillUseRail` priority 100 | `SkillHook` priority 90(功能层;PermissionHook 100 在不同事件不冲突) |

---

## 设计决策回顾

### 为什么 hook prepend 而不是 jiuwenswarm 的 window-mutator

Twinkle 已有 `before_model_call` + `ctx.inputs.messages` 注入点(context_compression 已用同款不写回 store),**不需要引入 `PromptAttachmentManager` + window-mutator 这套新抽象**。hook prepend 一条 system msg 行为等价(模型每步看见清单),粗一点(没有 priority 段拼装、没有 per-session attachment store),但第一切片够用。skill 多了 / 要多段 priority 拼装再加 `PromptSection` 层。

### 为什么 always-on 无 enabled flag

空 skills 目录 = 无 skills = SkillHook no-op(不注入、不报错),**不需要 opt-in 开关**。比 permissions 的 opt-in 更简:permissions 需要关掉因为 ASK 会挂起 run_stream、审计要写盘,默认开有副作用;skill 不挂起、不审计、不写盘,默认开无副作用。无 skills 时零成本。

### 为什么 priority 90

功能层(hook-design.md §优先级设计 50-99)。PermissionHook 100 是 `before_tool_call` 不同事件,不 tie。SkillHook 是当前唯一 `before_model_call` hook,priority 实际不冲突,放 90 纯按约定(未来若有别的功能 hook 加进来,按层排)。

### 为什么 trigger 丢弃

对齐 jiuwenswarm。**关键词自动匹配 brittle 且难维护**(用户说"刷文档"还是"更新文档"还是"doc audit"?枚举不全就漏触发)。模型读 description 自己决定更灵活、跟 ReAct 一致(jiuwenswarm 也这么干)。`trigger` 留在 frontmatter 是给**作者自己看 + 未来可能用**,第一切片不消费。

### 为什么模型驱动选择而非自动触发

同上。模型读清单 + description 自己选,调 `read_skill` 载入——这跟 Twinkle 现有 ReAct(tool_call → tool_result → 再查模型)完全一致,agent_loop 零结构改动。自动触发要额外写匹配逻辑 + 处理"触发错了"的回退,得不偿失。

### 为什么 skills 目录放 `<WORKSPACE>/skills` 而非 `.twinkle_data/skills`

用户可见性优先——skill 是用户高频往里塞 `SKILL.md` 的目录,放 `<WORKSPACE>/skills`(而非 `.twinkle_data/skills`)让用户直接看得见、好操作。代价是跟 sessions/todos(在 `.twinkle_data/` 下)不平齐,但 skill 是用户内容、sessions/todos 是运行时状态,分开放合理。

---

## 明确不做(第一切片 / 永不做)

**第一切片不做(后续可加)**:
- `all` 模式的结构化 `PromptAttachmentManager` + window-mutator(hook prepend 粗代)。
- `list_skill` 的 LLM 过滤路由(带 query 时跑独立 LLM call)。
- skill 目录附带文件的索引/检索(只支持 `read_skill` 按路径读)。

**依赖未落地的 Phase,后置**:
- `skill_turbo/` planner→executor→fallback 子 agent 编排 → Phase 8 subagent。
- skill 自进化(失败/纠正信号 → 经验固化回 SKILL.md) → Phase 9,且依赖 Phase 5 长期记忆。

**永远不做(roadmap §明确超出范围)**:
- marketplace / SkillNet / symphony 树检索 / `agentic` 模式——企业级,依赖 openjiuwen 生态。
- per-skill 绑定工具的 scoped 注册——jiuwenswarm 也用共享 toolkit,不需要 per-skill scoping。
- `trigger` 关键词自动匹配——模型驱动,见上。
