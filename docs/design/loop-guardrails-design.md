# Agent 循环护栏设计:去掉步数上限 + 死循环硬停 + 流级超时

> 日期:2026-08-24
> 状态:已实现(改动 1 + 改动 2 + 改动 1-扩展);改动 3a/3b 经实现期核实后取消(详见 §4)
> 对齐参考:openclaw(死循环检测硬停 + 流级 idle 超时 + idle-timeout-breaker);jiuwenswarm(护栏"插槽"默认全关,作反面参照)
> 关联调研:`docs/design/context-window-budget-comparison.md`、记忆 `agent-loop-guardrails-cross-project`
>
> **后续扩展(2026-08-24,同日)**:改动 1 扩展到子 agent —— subagent + team member 步数上限一并去掉(`max_steps` 参数从 `ReActAgent` 彻底移除,executor/manager 零传参,主/子全无界)。兜底分工:subagent 保留现有 `hard_timeout=300s` 整执行硬上限;team member 无 hard 上限,故补注册 `RepeatToolCallDetectorHook`(对齐主 agent CRITICAL force_finish)防"活跃但无效"空转。至此全部 agent 无步数上限,对齐 openclaw(非 jiuwenswarm 子 agent 有界路线)。详见 §2/§6/§8。
>
> **实现期核实(2026-08-24)**:改动 3a 的前提("`AsyncOpenAI(timeout=120)` 是整请求 120s 超时、会误杀长流")经读 openai SDK 源码核实为**误**——`timeout` 透传给 httpx 的 `read` 超时,语义本就是"相邻两次读最多 N 秒"(per-chunk idle),稳态长流每 chunk 重置计时器、不会误杀。故现有 `timeout=120` 已达成"单次卡死→杀这次调用→RetryHook 重试"的目标,改动 3a 的 helper 与之重复,按 CLAUDE.md 简单优先 revert,沿用现有 timeout(不降到 60,避免 reasoning 模型思考期无 chunk 的误杀)。改动 3b 的 breaker 因 `RetryHook`(max_retries=1)+ 外层 except 再 raise 使 run 在 2 次超时后即以 error 死掉,breaker(threshold=5)永不可达=死代码,经用户定夺去掉。最终落地仅改动 1 + 改动 2。

## 1. 背景与动机

Twinkle 是通用智能体。原先主 agent 有一个 `max_steps=1000` 的步数硬上限——不管 agent 怎么跑,跑满 1000 步就停。这对长任务是个问题:一个真正复杂的任务可能合法地需要远超 1000 步。因此决定**去掉这个步数上限**。

但去掉之后,必须有别的机制让 agent 停下来,否则真停不下来。

调研 jiuwenswarm 和 openclaw 后的核心结论(详见记忆 `agent-loop-guardrails-cross-project`):

- **两参考实现都没有"固定整循环 wall-clock 超时"**,也不把 token 累计花费用作硬停止。它们的哲学是"信任 LLM 自己停下来 + 用户能中断 + 检测异常模式硬停"。
- **jiuwenswarm**:护栏(`MaxRoundsEvaluator`/`TimeoutEvaluator`/`TokenBudgetEvaluator`/`RepeatToolCallDetector`)都有类,但默认一个都不挂载;默认 task-loop 模式甚至把内层 `max_iterations` 覆盖成 `sys.maxsize`。等于"默认基本不防",反面教材。
- **openclaw**:核心 `runLoop` 是 `while(true)` 无步数上限;靠 ① tool-loop 检测(critical 阻断执行、二次 critical 终止 run)② post-compaction 循环守卫 ③ 流级 LLM idle 超时 ④ idle-timeout-breaker(成本失控熔断)组合兜底。无 token 预算硬约束。

本设计按用户要求"参考 openclaw",去掉步数上限,用**死循环硬停 + 流级超时**两个新刹车替代,且**不加整循环 wall-clock 超时**(长任务本身耗时,整循环硬时限会误杀)。

### agent 停不下来,只有两类原因

| agent 停不下来的原因 | 谁来抓 |
|---|---|
| 模型在**重复调同一个工具**(死循环) | 改动 2 |
| 模型**卡住不出字**(卡死) | 改动 3 |

---

## 2. 改动 1 — 去掉主 agent 的 1000 步上限

**做什么**:

- [agent.py:505](../../twinkle/agentserver/agent.py) `for _step in range(self._max_steps)` → `for _step in itertools.count()`(无界)。
- 删除 max_steps 超限的 error 分支([agent.py:730-737](../../twinkle/agentserver/agent.py))——`count()` 不会耗尽,`for...else` 块变死代码,按 CLAUDE.md "remove orphans" 删掉。
- [config.yaml:24](../../twinkle/resources/config.yaml) `agent.max_steps` 标记 deprecated。
- **子 agent(subagent + team member)步数上限一并去掉**(2026-08-24 扩展):`max_steps` 参数从 `ReActAgent.__init__` 彻底移除,executor.py / manager.py 零传参,主/子全无界(`for _step in itertools.count()`)。兜底分工:subagent 保留现有 `hard_timeout=300s` 整执行硬上限(executor.py `asyncio.wait_for(child_task, hard_timeout)`);team member 无 hard 整执行上限,故补注册 `RepeatToolCallDetectorHook`(对齐主 agent 的 CRITICAL force_finish)防"活跃但无效"死循环空转。两参考查证(2026-08-24):jiuwenswarm 子 agent 有步数上限(默认 15,硬停)+ 软硬超时(600/3000s);openclaw 子 agent **无**步数上限(主/子共用 while-true 无界,仅结构性 cap 深度1/子数5/并发8 + 可选墙钟超时 `runTimeoutSeconds` 默认关)。Twinkle 子 agent 现走 **openclaw 路线**(无步数),用 hard_timeout / RepeatDetector 兜底,非 jiuwenswarm 的步数硬停。[旧版本文档误称"对齐 openclaw 子 agent hard timeout",已纠正——openclaw 子 agent 既无步数上限也无 hard/soft/abort 三联超时;Twinkle 的 300/120/30 是自身 `subagent` config,非 openclaw来源]

**兜底全靠改动 2 + 改动 3**。

---

## 3. 改动 2 — CRITICAL 死循环 → force_finish 硬停

### 问题(现状的毛病)

agent 为了完成某任务,反复调同一个工具、同样参数,每次拿到一样结果。比如反复 `read_file("a.txt")`,读 30 次,内容都一样。

Twinkle 其实**已经能检测到**这种"连续 30 次完全相同调用+结果"——`RepeatToolCallDetectorHook` 有 4 档严重度(LOW/MEDIUM/HIGH/CRITICAL),滑动窗口 30 + 稳定哈希,默认注册,能力齐全。

但检测到之后它只做一件事:往对话里塞一句系统消息:"你重复了,换个策略吧",然后**继续循环**。模型经常无视这句警告,照样重复。于是 agent 一直重复到撞 1000 步才停——而改动 1 还要把 1000 去掉,那就真停不下来了。

**一句话:检测到了却不停,等于没检测。** 配置键 `loop_block`/`global_stop` 的命名也误导,暗示会 block/stop,实则只升档位。

### 怎么解决

检测到 CRITICAL 的那一刻,直接调 `ctx.request_force_finish()`——这个信号会让主循环立刻 break,给用户返回一个"检测到死循环,已停止"的结果。不再给模型继续重复的机会。

模型在到达 CRITICAL(30 次连续相同)之前,中低档(MEDIUM/HIGH)已经收到过多次"换个策略"的 remediation 提醒,有充分机会改过;到 30 次还停不下来,就判定为真死循环,强制停。

### 技术细节

- 文件:[repeat_tool_call_detector_hook.py:111](../../twinkle/agentserver/hooks/builtin/repeat_tool_call_detector_hook.py) `before_model_call`。
- 新增 CRITICAL 分支:`ctx.request_force_finish(result={"error": "agent stopped: repeated tool-call loop detected (CRITICAL)"})`。
- MEDIUM/HIGH 保留现有 remediation 注入(不变);LOW 不动。
- CRITICAL 阈值 = `global_stop`(默认 30,trailing identical call+outcome),见 [repeat_tool_call_detector_hook.py:166-167](../../twinkle/agentserver/hooks/builtin/repeat_tool_call_detector_hook.py)。
- 模型路径已消费 force_finish([agent.py:544-554](../../twinkle/agentserver/agent.py) / `:707-717`,`ContextOverflowRecoveryHook` 熔断同款机制),**无需改循环**。
- ~6 行改动。
- config 注释补一句:`global_stop` 现在真会硬停,消除命名误导。

### 与 openclaw 的差异(为何不"全对齐")

openclaw 的 tool-loop 检测:1st critical 阻断**单个**工具执行(注入假 error result、循环继续给模型一次机会),2nd critical 才终止 run。Twinkle 的 hook API 只有 `force_finish`(终止整个循环),没有"阻断单个工具、循环继续"的信号。全对齐需新增 `request_skip_tool` 信号(改 base.py + agent.py + 检测器状态,约 40 行,新增 API 面)。

经评估选简化方案:**到 CRITICAL 直接 force_finish**。理由:Twinkle 已在 MEDIUM+ 注入 remediation 警告,模型到 CRITICAL 前已有多次改策略机会,直接终止语义上等价于 openclaw "2nd critical 终止",省去中间阻断步。符合 CLAUDE.md "Simplicity first"。

---

## 4. 改动 3 — 流级 LLM idle 超时 + idle-timeout-breaker

这管的是另一类——**不是重复调工具,是模型自己吐不出字**。分两层。

### 4a — 单次调用卡死:per-chunk idle 超时(实现期核实:已由现有机制覆盖,取消)

**原问题假设**:模型流式吐字中途 hang 住(吐了几个 token 后卡死,既不继续也不报错);原以为 `AsyncOpenAI(timeout=120)` 是"整请求 120s 超时",会误杀长流、分不清"正常慢"和"卡死"。

**实现期核实(读 openai SDK 源码)**:前提为误。`AsyncOpenAI(timeout=120)` 把 `timeout` 透传给 httpx(`_base_client.py` 里 `timeout=cast(Timeout, timeout)`),httpx 的 `read` 超时语义是"**相邻两次读之间最多 N 秒**"——即 per-chunk idle,不是整请求。流式时 `_streaming.py` 的 `response.aiter_lines()` 每次底层 socket read 都受它约束:模型停止吐 chunk 达 120s → `ReadTimeout` → SDK 包成 `APITimeoutError`(瞬时,`RetryHook` 重试);稳态长流每 chunk 重置计时器,**不会误杀**。`schema.py` 的注释本就写着"per-chunk read timeout"。

**结论**:现有 `timeout=120` 已达成改动 3a 的全部目标(单次卡死→杀这次调用→重试),helper(`asyncio.wait_for` per-chunk 包裹,原设计 §4a option ②)与之重复。按 CLAUDE.md "Simplicity first" + 本设计 §4a 自有的"二选一,不写死"权限,选 option ①(httpx 原生 per-read),**revert helper、沿用现有 `timeout=120`**:

- 不降到 60s:60 是原设计任取值,120 对 reasoning 模型思考期(可能短暂无 chunk)更安全;若要更快检测可一行调 `llm.timeout`。
- 无需新增 `llm.idle_timeout_seconds` config、无需改 `llm_client.py`、无需新文件。
- TDD 期间为 helper 写的 `tests/test_llm_client.py` 随 revert 删除(测的是被移除的代码)。

### 4b — 反复卡死:idle-timeout-breaker(实现期核实:不可达,取消)

**原设计**:加计数器,每次卡死超时 +1、每次模型成功吐完清零,连续 5 次→`force_finish`。对齐 openclaw 的 idle-timeout-breaker(防 60s 内 761-1384 次付费调用的成本失控)。

**实现期核实(读 agent.py retry 循环 + RetryHook)**:在 Twinkle 不可达=死代码,故取消。原因链:

- `RetryHook` 默认 `max_retries=1`([retry_hook.py:48](../../twinkle/agentserver/hooks/builtin/retry_hook.py)),且只在 `retry_attempt < 1` 时请求重试。
- agent.py retry 循环([agent.py:558-725](../../twinkle/agentserver/agent.py)):attempt 0 超时→`on_model_exception`+RetryHook 请求重试→continue;attempt 1 超时→`on_model_exception`+RetryHook 不再请求(1<1 假)→`raise`。
- 该 `raise` 冒到 [agent.py:449-452](../../twinkle/agentserver/agent.py) 外层 `except Exception`→**又**触发一次 `on_model_exception`→再 `raise`→run 以 error 结束。

即:模型卡死时,`on_model_exception` 至多触发 3 次(attempt0 + attempt1 + 外层),然后 run 死掉。breaker threshold=5 **永远到不了 5**(run 早在第 2 次超时就死了)。openclaw 那"761 次/60s"的失控空转在 Twinkle 根本不可能发生——retry 循环会先死。

**用户定夺(2026-08-24)**:去掉 3b。理由:① retry-die-fast 已使"反复卡死"在 2 次超时后自然停止,outcome 与 breaker 一致(都停),只是机制不同;② 强行让 breaker 可达(threshold 改 2,或引入 openclaw 的"survive 卡死"机制让 run 不死)要么仅是美化报错、要么改 agent.py 错误语义超出已批范围,且真卡死时反而比现在更慢(stop 前最多 5×2≈10 次调用 ≈ 10 分钟)。符合 CLAUDE.md 简单优先。无新文件、无 config、无注册。

### 不加整循环 wall-clock(明确决策)

用户要求:**不加整请求 wall-clock 超时**,因为长任务本身耗时,整循环硬时限会误杀合法长任务。这与 openclaw 一致——openclaw 也没有固定整循环超时,其 run-level deadline 由调用方(cron/heartbeat)传入。

---

## 5. 不抓的一类(已知缺口,接受)

第三类:模型在**不停吐字、不停调工具,但调用略变**(不是完全相同)。这类:

- 不触发改动 2(不是连续相同 → 到不了 CRITICAL);
- 不触发改动 3(模型一直在动 → 不卡死、不到 5);

故无硬兜底。

这是 **openclaw 也有**的缺口——openclaw 靠调用方(cron/heartbeat)传入整任务超时来补。Twinkle 是交互式的、没有调用方给超时,且用户明确不要整循环超时,故接受此缺口。比"有 1000 步兜底"弱,但与 openclaw 一致,符合"参考 openclaw"。

---

## 6. 涉及文件

| 文件 | 改动 | 状态 |
|---|---|---|
| [agent.py](../../twinkle/agentserver/agent.py) | 循环改 `itertools.count()` 无界;`max_steps` 参数+_max_steps+三元+超限 error 分支全删 | ✅ 已落地(+扩展) |
| [repeat_tool_call_detector_hook.py](../../twinkle/agentserver/hooks/builtin/repeat_tool_call_detector_hook.py) | `before_model_call` 新增 CRITICAL → force_finish 硬停分支 | ✅ 已落地 |
| [config.yaml](../../twinkle/resources/config.yaml) | `agent.max_steps` + `subagent.max_steps` 注释均标 deprecated(值保留,代码忽略) | ✅ 已落地(+扩展) |
| [executor.py](../../twinkle/agentserver/tools/builtin/subagent/executor.py) | subagent 去掉 `max_steps=` 传参(保留 `hard_timeout=300s` 硬上限) | ✅ 已落地(扩展) |
| [manager.py](../../twinkle/agentserver/team/manager.py) | team member 去掉 `max_steps=` 传参 + 补注册 `RepeatToolCallDetectorHook` | ✅ 已落地(扩展) |
| [config/__init__.py](../../twinkle/config/__init__.py) | 删 `SUBAGENT_MAX_STEPS` 死导出(manager 不再导入);`AGENT_MAX_STEPS` 留(测试断言) | ✅ 已落地(扩展) |
| [llm_client.py](../../twinkle/agentserver/llm_client.py) | (原计划包 `asyncio.wait_for` per-chunk idle) | ❌ 取消:现有 httpx read 超时已是 per-chunk idle(§4a) |
| 新增 `hooks/builtin/idle_timeout_breaker_hook.py` + server.py 注册 | (原计划 idle-timeout-breaker) | ❌ 取消:retry-die-fast 使其不可达(§4b) |
| config `llm.idle_timeout_seconds` / `agent.idle_timeout_breaker_threshold` | (原计划新增) | ❌ 取消:随 3a/3b 一并取消 |
| 测试 | 见 §7 |

## 7. 测试计划(TDD,先红后绿)

1. **改动 2**(✅ 已落地):连续 30 次相同调用+相同结果 → 断言 CRITICAL 触发 `force_finish`、循环终止(不到无界);断言 MEDIUM/HIGH 仍走 remediation 注入、不停;断言 CRITICAL 绕过 remediation 限频。见 `tests/test_repeat_tool_call_detector_hook.py` + `tests/test_agent_loop.py::test_unbounded_loop_stops_via_critical_not_step_cap`。
2. **改动 1 + 扩展**(✅ 已落地):主/子 agent 全无界(`max_steps` 参数已从 `ReActAgent` 移除);子 agent 兜底 = subagent `hard_timeout=300s` / team member `RepeatToolCallDetectorHook` CRITICAL force_finish。见 `tests/test_agent_loop.py::test_react_agent_has_no_step_cap_parameter` / `test_unbounded_loop_stops_via_critical_not_step_cap`、`tests/test_team.py::test_build_member_registers_repeat_detector_hook`。
3. ~~改动 3a / 3b~~(❌ 取消):见 §4a / §4b。3a 的 helper 测试随 revert 删除。
4. **回归**:max_steps 参数移除后,正常短任务仍正常完成(yield complete + return);context_assembly / progressive 等测试去掉 `max_steps=N` safety kwarg(其脚本 LLM 本就自然完成,从未触顶)。

## 8. 验收标准

- [x] 主 agent 无 1000 步上限;长任务可超 1000 步继续跑。
- [x] 连续 30 次相同工具调用+结果 → agent 强制停止并返回死循环错误(不再跑到无界)。
- [x] 子 agent(subagent + team member)步数上限一并去掉;subagent 靠 `hard_timeout=300s`、team member 靠 `RepeatToolCallDetectorHook` CRITICAL force_finish 兜底。
- [x] 既有测试全绿(扩展期把 context_assembly / progressive 测试里 `max_steps=N` safety kwarg 去掉;`max_steps` 参数移除的契约测试 = `test_react_agent_has_no_step_cap_parameter`)。
- 单次 LLM 卡死:由现有 `AsyncOpenAI(timeout=120)` httpx read 超时覆盖(§4a,无需新增),`RetryHook` 重试 1 次。
- 反复卡死:由 retry-die-fast 覆盖(§4b,2 次超时即停 run),无需 breaker。

## 9. 不做范围(YAGNI)

- 不加 token 累计花费预算(两参考都没有硬约束;usage 继续只喂 OTel)。
- 不加整循环 wall-clock 超时(用户明确拒绝)。
- 不加 `request_skip_tool` 全 openclaw 对齐(简化方案已满足)。
- 不加 post-compaction 循环守卫(Twinkle 的 RepeatToolCallDetector 在压缩后仍生效,功能重叠)。
- 子 agent 的 hard/soft/abort 超时(300/120/30)保留不动(subagent 整执行硬上限仍 300s);仅去掉步数上限维度。
