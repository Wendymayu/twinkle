# Twinkle 配置 YAML 化设计

- 日期:2026-07-27
- 状态:待用户 review
- 相关:对齐 `jiuwenswarm/resources/config.yaml`;替换现 `twinkle/config.py` 的 env+`.env` 加载方式

## 1. 背景与问题

`twinkle/config.py` 现状:env 变量 + 手写 `.env` 解析 → 一堆模块级常量。痛点有二:

1. **取值域/行为语义没写在 config 里,要读代码**:
   - `TWINKLE_PERMISSIONS` 是一个 JSON env 扛 6 字段,`_load_permissions()` 有三条解析分支(裸布尔→只切 enabled;JSON 对象→**浅合并** `merged.update(user)`,写 `tools` 就整个替换默认;非法→静默回退)。`global_default`/`tools[*]` 能填啥全在 `permissions/` 引擎里。
   - `SKILL_MODE`(`all`/`auto_list`)、`CONTEXT_*`(token 估算 `//3`、"recent pairs" 切法、何时触发)的行为细节藏在 `skill_hook.py`/`context_compression.py`。
2. **职责混在一个文件**:`import json` 在中段(151 行);`ensure_workspace_dir()`/`_seed_example_skills()` 是运行时副作用函数混在常量里;permissions 解析(~50 行 + 5 个派生常量)和路径/LLM/压缩常量混在一条流。

## 2. 决策

**改用 YAML 主配置 + pydantic 类型化校验,对齐 jiuwenswarm。** 参考仓证据:

- `jiuwenswarm/resources/config.yaml` 是主配置(层次化、注释密集);
- `${ENV:-default}` 插值——机密/环境值仍走 env(`api_key: ${API_KEY}`),YAML 可 commit;
- `permissions:` 块(`enabled`/`permission_mode`/`defaults`/`tools{}`/`rules[]`)即 Twinkle `TWINKLE_PERMISSIONS` JSON env 的 YAML 版,取值域写在注释里;
- `react.skill_mode: all` 与 Twinkle `TWINKLE_SKILL_MODE` 同名同值;`telemetry:` 对应 Twinkle `OTEL_*`。

**两个 fork(v1 范围,用户已拍板)**:

- **permissions 的 deny patterns 不搬进 YAML**——只搬 `TWINKLE_PERMISSIONS` JSON env → YAML `permissions` 块;deny patterns 仍留 `builtin_rules.py`(`command_exec` + policy 共读单源),`permissions/` 引擎不动。
- **observability 不并进主 YAML**——`observability/config.py` 仍走 `OTEL_*` env;主 `config.yaml` 只管 agentserver 侧。obs 是可选 `[obs]` extra,边界清晰。

full alignment(deny patterns 进 YAML、obs 进 telemetry)留后续 phase。

## 3. 文件布局

| 文件 | 角色 | 动作 |
|---|---|---|
| `twinkle/resources/config.yaml` | 默认配置:各节 + 注释 + 取值域 + `${ENV:-default}` 占位 | 新增,committed |
| `twinkle/config_schema.py` | pydantic 模型:`Literal` 取值域、默认、启动校验、派生路径 | 新增 |
| `twinkle/config.py` | 加载器:读 YAML → 解析 `${ENV}` → pydantic 校验 → export 同名常量 | 改写 |
| `twinkle/workspace.py` | `ensure_workspace_dir()`/`_seed_example_skills()` | 从 config 挪出 |
| `.env.example` | 只留机密 + 必须走 env 的覆盖 | 瘦身 |
| `docs/architecture.md` §9.2、`CLAUDE.md` 配置表 | 配置说明 | 同步 |

**消费方 API 不变**:`from twinkle.config import X` 照旧;内部从 "env getenv" 换成 "YAML→model→常量",export 的常量名照旧,现有测试 monkeypatch 常量不用改。

## 4. `config.yaml` 结构

```yaml
# Twinkle 运行时配置。${ENV:-default} 仅用于机密 + 部署相关变量(路径/端点/端口);
# 可调参数(max_steps/压缩阈值/skill mode/permissions 策略)直接写值——改它就编辑本文件。
# 本文件可安全 commit:机密走 .env / 环境变量,不落盘于此。
agentserver:
  host: ${TWINKLE_AGENTSERVER_HOST:-127.0.0.1}
  port: ${TWINKLE_AGENTSERVER_PORT:-18000}
gateway:
  host: ${TWINKLE_GATEWAY_HOST:-127.0.0.1}
  port: ${TWINKLE_GATEWAY_PORT:-19000}
workspace:
  dir: ${TWINKLE_WORKSPACE_DIR:-}        # 空 → ~/.twinkle;sandbox 根,command_exec/file_tools 收敛其下
logging:
  dir: ${TWINKLE_LOG_DIR:-}              # 空 → <workspace>/logs
sessions:
  dir: ${TWINKLE_SESSIONS_DIR:-}         # 空 → <workspace>/.twinkle_data/sessions
todos:
  dir: ${TWINKLE_TODOS_DIR:-}            # 空 → <workspace>/.twinkle_data/todos
llm:
  base_url: ${TWINKLE_LLM_BASE_URL:-https://api.openai.com/v1}
  model: ${TWINKLE_LLM_MODEL:-gpt-4o-mini}
  api_key: ${TWINKLE_LLM_API_KEY:-}       # 机密:放 .env,别 commit
agent:
  max_steps: 1000                         # ReAct 最大步数,超限 yield e2a.error(防不收敛硬上限,非目标)
context_compression:
  token_threshold: 60000                  # 估算 token(char//3,不精确)超此即压缩历史
  keep_recent_pairs: 6                    # 保留最近 N 个 user/assistant 对
  summary_prompt: "你是对话上下文压缩器。把给定历史对话压成一段摘要,保留关键事实、用户偏好、已做决策、工具调用结果,丢弃寒暄与冗余。用中文。"
skills:
  dir: ${TWINKLE_SKILLS_DIR:-}            # 空 → <workspace>/skills
  mode: all                               # all = 每步注入 skill 清单;auto_list = 模型按需调 list_skill 拉
  enabled: []                             # 列表;空 = 全开
permissions:
  enabled: false                          # false = 系统关(全 ALLOW,无审计/无 ASK;command_exec 仍走 builtin_rules)
  enabled_channels: [web]
  global_default: allow                   # allow | require-approval | deny
  tools:
    command_exec: require-approval         # allow | require-approval | deny(require-approval 引擎归一为 ASK)
    web_fetch: allow
    web_search: allow
    todo_create: allow
    todo_complete: allow
    todo_list: allow
  rules: []                               # 用户规则(同 jiuwenswarm rules[] 形状;v1 可空)
  approval_overrides: {}
  overrides_file: ${TWINKLE_PERMISSION_OVERRIDES_FILE:-}   # 空 → <workspace>/.twinkle_data/permission_overrides.json
  audit_file: ${TWINKLE_PERMISSION_AUDIT_FILE:-}          # 空 → <workspace>/logs/audit/permission_audit.jsonl
```

### 4.1 环境变量存废(行为变更,需用户确认)

为收敛"配置散在 env"的混乱,YAML 化时按"机密 + 部署变量走 env、可调参数走 YAML 字面值"划分:

- **保留 `${ENV:-default}`**:`TWINKLE_AGENTSERVER_HOST/PORT`、`TWINKLE_GATEWAY_HOST/PORT`、`TWINKLE_WORKSPACE_DIR`、`TWINKLE_LOG_DIR`、`TWINKLE_SESSIONS_DIR`、`TWINKLE_TODOS_DIR`、`TWINKLE_LLM_BASE_URL/API_KEY/MODEL`、`TWINKLE_SKILLS_DIR`、`TWINKLE_PERMISSION_OVERRIDES_FILE`、`TWINKLE_PERMISSION_AUDIT_FILE`。
- **转 YAML 字面值(env 不再生效)**:`TWINKLE_AGENT_MAX_STEPS`、`TWINKLE_CONTEXT_TOKEN_THRESHOLD`、`TWINKLE_CONTEXT_KEEP_RECENT_PAIRS`、`TWINKLE_CONTEXT_SUMMARY_PROMPT`、`TWINKLE_SKILL_MODE`、`TWINKLE_ENABLED_SKILLS`、`TWINKLE_PERMISSIONS`(整个 JSON env 删除)。

这是有意收口:可调参数改编辑 `config.yaml`,不再经 env。`.env.example` 与 `architecture.md §9.2` 同步标注。如用户希望某个被废 env 保留,review 时指出即可。

取值域来源:`permissions.global_default`/`tools[*]` 用 Twinkle 自己的 `allow | require-approval | deny`(见 `permissions/policy.py:103-105`,`require-approval` 归一为 `PermissionLevel.ASK`),**不**照抄 jiuwenswarm 的 `allow/ask/deny`。

## 5. `config_schema.py`(pydantic 模型)

- 每节一个 `BaseModel`(`AgentserverConfig`/`GatewayConfig`/`LLMConfig`/`AgentConfig`/`ContextCompressionConfig`/`SkillsConfig`/`PermissionsConfig`/顶层 `TwinkleConfig`)。
- 取值域用 `Literal`:`SkillMode = Literal["all","auto_list"]`、`PermissionTier = Literal["allow","require-approval","deny"]`。非法值 pydantic 启动即报错——**直接消灭"取值域要读代码"痛点**。
- 派生路径:空 `dir` 字段在 `model_validator(mode="after")` 里从 `workspace.dir` 派生(镜像现 `or str(Path(WORKSPACE_DIR)/...)` 语义);`~` 展开。
- `PermissionsConfig` 字段对应现 `PERMISSIONS` dict 的 6 key + 2 file 路径;`rules`/`approval_overrides` 类型宽松(`list[dict]`/`dict`),v1 不强校验内部结构(与现行为一致)。

## 6. `config.py` 加载器

- 保留 `_load_env_file()`(`.env` 仍要读,供 `${TWINKLE_LLM_API_KEY}` 解析)。
- 新增 `_resolve_env_vars(text)`:正则替换 `${VAR:-default}` / `${VAR}`,从 `os.environ` 取值,缺省用 default,都没则空串。镜像 jiuwenswarm 的插值语义。
- 流程:读 `twinkle/resources/config.yaml`(用 `importlib.resources`,不依赖 cwd)→ `_resolve_env_vars` → `yaml.safe_load`(PyYAML)→ `TwinkleConfig(**data)` 校验 → 把模型字段拍平成同名模块常量(`AGENTSERVER_HOST`、`LLM_API_KEY`、`PERMISSIONS`、`PERMISSIONS_ENABLED`、… 全保留)。
- `PERMISSIONS`/`PERMISSIONS_ENABLED`/`PERMISSIONS_ENABLED_CHANNELS`/`PERMISSIONS_GLOBAL_DEFAULT`/`PERMISSIONS_TOOLS`/`PERMISSIONS_RULES` 派生常量照旧 export(从 `settings.permissions` 派生),消费方不动。
- `ensure_workspace_dir`/`_seed_example_skills` 删掉,改到 `workspace.py`;`agentserver/__main__.py` 与 `server.py:146` 的 `from twinkle.config import ensure_workspace_dir` 改指 `twinkle.workspace`。

## 7. secrets 处理

`llm.api_key: ${TWINKLE_LLM_API_KEY:-}`——YAML 只写占位,真值从 `.env`/env 读,YAML 可 commit。`.env.example` 瘦身后保留 `TWINKLE_LLM_API_KEY=` 与少量必须走 env 的覆盖项;其余配置项的说明挪到 `config.yaml` 注释。

## 8. 不在范围内(v1)

- deny patterns(`COMMAND_DENY_PATTERNS`)搬进 YAML `permissions.rules[]`——要改 `permissions/` 引擎消费,留后续。
- `observability/config.py` 的 `OTEL_*` 并进 `telemetry:` 节——留后续。
- 用户级 workspace 覆盖文件(`<WORKSPACE>/config.yaml` 覆盖包内默认)——jiuwenswarm 有 instance config,Twinkle v1 只读包内 `resources/config.yaml`。
- 热重载:config 现为 import 时读一次,v1 不变。

## 9. 测试影响

- 现有测试 monkeypatch `WORKSPACE_DIR` 等模块常量——保持 export 同名常量后**不用改**。
- 新增 `tests/test_config_loader.py`:YAML→model 解析、`${ENV:-default}` 插值、非法 tier 启动报错、空 dir 派生路径、permissions 派生常量正确。
- permissions 现有测试不动(引擎没改)。

## 10. 风险与开放项

- **新增 PyYAML 依赖**:项目原本"minimal hand-rolled parser"作风,加 PyYAML 是已知取舍(对齐 jiuwenswarm 也用 PyYAML)。需在 `pyproject.toml` `dependencies` 加 `pyyaml`。
- **`require-approval` vs jiuwenswarm `ask`**:YAML 注释要写清 Twinkle 自己的取值域,别误导。
- **派生路径顺序**:`logging.dir` 等依赖 `workspace.dir`,`workspace.dir` 又依赖 `${TWINKLE_WORKSPACE_DIR:-~/.twinkle}`;model_validator 里按 workspace→其余派生,顺序要对。
- **`config.py` import-time 副作用**:加载在 import 时发生(和现在一致);若 YAML 缺失/非法,启动即明确报错(比现在的静默回退好,但需保证 `resources/config.yaml` 随包打包——`pyproject.toml` package-data 要含)。
