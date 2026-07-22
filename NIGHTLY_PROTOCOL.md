# Nightly Autonomous Loop Protocol

> 这是夜间自治 loop 的操作手册。每次 cron 触发（或本 turn 继续推进时），先读本文件，再读 PROGRESS.md，按状态机推进。

## 启动元信息

- 启动：2026-07-23 夜
- 分支：`nightly/autonomous-2026-07-23`（**本地 only，绝不 push、绝不开 PR**）
- 主分支：`main`（**绝不在 main 上提交任何东西**）
- 用户状态：**在睡觉，不可交互提问**，所有决策自治

## 用户授权（睡前 AskUserQuestion 确认）

1. **范围**：尽量多做，顺序 Phase 3 → Phase 6 → Phase 4
2. **提交**：只本地 feature 分支，不 push、不开 PR
3. **失败策略**：自修复有限次（最多 2 次），仍不过则在 PROGRESS.md 标记 `BLOCKED` 并停止该任务，**不跳过同 Phase 的子步骤、不留半成品坑**

## 每次 fire 的执行步骤（幂等 + 状态驱动）

1. **确认分支**：`git branch --show-current` 必须是 `nightly/autonomous-2026-07-23`。不是则 `git checkout nightly/autonomous-2026-07-23`。若该分支不存在（会话重启后本地分支还在，一般不会不存在）→ 停止并标记。
2. **读 PROGRESS.md**，找第一个 `[ ]` 且非 `BLOCKED` 的任务作为 `current`。
3. 若所有 Phase 的所有子任务都 `[x]` 或全部 `BLOCKED` → loop 结束，在 PROGRESS.md 顶部写 `STATUS: DONE` 或 `STATUS: ALL_BLOCKED`，不再推进。
4. 执行 `current` 任务。每个 Phase 的标准流程：
   - **a. brainstorming**（自驱动：把设计选项 + tradeoffs 想清楚，写进 spec）→ 调用 `superpowers:brainstorming` 技能，自治走流程，结论写进 spec
   - **b. spec**：写 `docs/superpowers/specs/2026-07-23-phaseN-<slug>-design.md`，参考已有 spec 格式
   - **c. plan**：调用 `superpowers:writing-plans`，写 `docs/superpowers/plans/2026-07-23-phaseN-<slug>.md`
   - **d. 实现**：调用 `superpowers:executing-plans`（可用 subagent / Agent 工具执行 plan 步骤）
   - **e. 验证**：`python -m pytest tests/ -v` 相关用例 + 端到端（`.env` 有 key，模型 glm-5.1 via dashscope）
   - **f. self code-review**：用 `/code-review` 技能或派 subagent review 本 Phase diff，修 finding
   - **g. 提交**：`git add <新文件+改动> && git commit -m "[nightly] Phase N: <步骤>"`
   - **h. 更新 PROGRESS.md**：勾选 `[x]`，追加运行日志一行
5. **失败处理**：测试不过 → 自修复最多 2 次（改代码→重测）→ 仍不过 → PROGRESS.md 该任务标 `BLOCKED` + 记原因 + 保存失败测试输出到 `tests/nightly_failures/phaseN_<step>.log` → 进下一个任务（若同 Phase 后续子步骤依赖此步则整 Phase 标 BLOCKED，不硬推进）
6. **连续推进**：一个任务做完不主动停，继续下一个，直到 turn 的 context/budget 用尽或全做完或全 BLOCKED。turn 结束后 cron 保底接力。

## 硬约束（不可违反）

- **绝不 push、绝不开 PR、绝不在 main 提交**。所有 commit 只进 `nightly/autonomous-2026-07-23`。
- **绝不修改 baseline 未提交文件**（用户之前的工作，留在工作区）。每次开工先 `git status`，凡是会话启动快照里就 modified/untracked 的文件 **不碰**，已知清单：
  - `CLAUDE.md`, `roadmap.md`, `docs/architecture.md`, `docs/e2a-introduction.md`
  - `docs/superpowers/specs/2026-07-1{7,}*` / `2026-07-2{0,1,2}-*` 已有 spec/plan（含 file-tools/task-planning/observability/todo-progress-ui/session-management/sessions-page）
  - `tests/test_agentserver_handler.py`, `tests/test_integration.py`, `tests/test_message_handler.py`
  - `twinkle/agentserver/server.py`, `twinkle/gateway/message_handler.py`
  - `web/package-lock.json`, `package-lock.json`, `web/tsconfig.tsbuildinfo`
  - **判断规则**：夜间新建文件 + 改干净的源码（如 `session_store.py`、新增 `cron/` 包等）允许；动上面清单里的文件 **禁止**。roadmap 状态更新（Phase 3 `[待启动]`→`[已完成]`）留白天用户做。
- **绝不删文件**（除非是本夜间 loop 自己刚创建的、且写错了要重来的）。
- **不碰 `.env` / API key**。
- **遵守 `CLAUDE.md` 约定**：测试用 `asyncio.run()` + `free_port`/`port_factory`，不用 `pytest-asyncio`；新工具走 `@tool` + `tool_manager()`；两进程架构不动；E2A 消息格式不动。
- **参考实现**：jiuwenswarm 在 `D:\code\opensource\gitcode\jiuwenswarm`，`.py` 源码在 `enterprise_dev` 分支，读取用 `git -C <path> show enterprise_dev:<path>` 或 `git -C <path> log enterprise_dev`。行为不清时查对应文件。

## Phase 验收标准（来自 roadmap.md）

- **Phase 3（上下文压缩）**：单会话 100 轮不爆 token、关键事实不丢。
  - 自动化验证：构造 100 轮对话 → 断言压缩触发后 message token 数下降 + 关键事实（注入的特定字符串）仍可在压缩后上下文里找到。端到端可跑真模型验证 token 数。
- **Phase 6（cron）**：注册 cron 任务→到点唤醒 agent 执行→结果推送到通道；支持单次任务 + 立即触发。
  - 自动化验证：注册一个短间隔（如 3s）cron → 断言 wake 阶段构造了 E2AEnvelope → 断言 push 阶段推到了目标 channel。`trigger_run_now` 单测覆盖。
- **Phase 4（权限/审批）**：危险工具过策略；`require-approval` 触发审批卡；拒绝带 `[PERMISSION_DENIED]` 回灌；审计日志可查。
  - 夜间只能做：钩子点 + 档位策略 + 审计日志 + 拒绝回灌 + command_exec 安全增强，**全部可单测**。
  - 夜间 **做不了**：审批卡的端到端（需用户在场 allow/deny）。该子任务标 `PARTIAL`，留白天。

## 醒来交接

PROGRESS.md 顶部维护「醒来先看这里」区块：分支名、当前状态、BLOCKED 清单、commit 列表指针、下一步建议、如何 merge / 如何丢弃。
