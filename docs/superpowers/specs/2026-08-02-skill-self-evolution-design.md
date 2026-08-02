# Twinkle 的 Skill 自进化设计

> 日期：2026-08-02
> 性质：设计稿（借鉴一套被验证有效的闭环机制，落地到 Twinkle 架构）
> 关联：`docs/superpowers/research/` 下的参考实现拆解是依据，本文是 Twinkle 的落地设计
> 目标：把 skill 从"部署即冻结"变成"随使用持续修正的活文档"——在 Twinkle 现有 `SkillManager` + `AgentHook` + approval 架构上，落地一个**真闭环、不静默、规则归因**的进化系统。

---

## 一、目标与边界

**目标**：工具报错、用户纠正、可复用脚本这些"用了之后学到的"，固化回 skill 本身（`SKILL.md` + 经验库），让 skill 随真实使用持续修正、去重、重组。

### 借鉴（参考实现里被验证有效的设计）

- **闭环 5 步**：信号检测 → LLM 生成经验 → 存储+固化 → E/U/F 打分 → 反馈环判定。
- **规则归因**（正则/路径匹配，不用 LLM）——便宜、可复现，不会因 LLM 抽风错归因。
- **数量上限 + 去重**——单轮文本≤2 / 脚本≤1，重复走 `merge_target`、相似走 skip，防经验库膨胀淹没信号。
- **E/U/F 打分**——贝叶斯效能 + 利用率 + 新鲜度衰减（90 天半衰期）+ 版本不匹配惩罚。
- **经验不内联 SKILL.md 正文**——走 `<!-- evolution-index -->` 索引块 + sidecar 文件，不污染作者原文。
- **审批门控**——默认不静默写改，`auto_save` 才自动落盘。
- **蒸馏淘汰**——低分经验定期 DELETE/MERGE/REFINE/KEEP。

### 不借鉴（参考实现里的坑/摆设）

| 参考实现的问题 | Twinkle 的处理 |
|---|---|
| `applied` 字段是摆设（唯一能置 True 的方法无人调用，approved 记录永远 `applied=False`） | **不加这个字段**。"是否生效"以"渲染进索引块"为唯一事实，省一个永远 False 的死字段。 |
| `auto_scan` 默认值矛盾（代码默认 True、设计稿示例写 false） | **定一个、不再矛盾**：`evolution.enabled: false`（总开关默认关，进化会改用户 skill，保守 opt-in），代码与配置一致。 |
| 自动创建新 skill 的 rail 未就绪 | **v1 不做**。新 skill 走 SkillHub/SkillNet 下载或手建，进化只改已存在 skill。 |
| 离线 RL / trainer 是另一条轴 | **不纳入**。不接运行时进化环。 |
| 概念类名 ≠ 真实类名（文档一套、代码一套） | **设计稿即代码名**，从一开始一致。 |

### 与长期记忆（LTM）的边界

LTM（Phase 5a）把会话蒸馏进 memory store；skill 自进化改的是 **skill 文件自身**。两者不交叉，别混。一句话判别：改的是 `skills/<name>/` 下文件 → 自进化；改的是 memory store → LTM。

---

## 二、速答五问

### ① 怎么设计（两层切分）？

- **接线层** = `SkillEvolutionHook`（`hooks/builtin/evolution_hook.py`，`AgentHook` 子类，priority≈80）：挂在 agent loop 的 `AFTER_INVOKE`，把"进化"事件路由到核心层；外加 `skills/rpc.py` 里几个 evolve RPC 给前端。
- **核心层** = `twinkle/agentserver/evolution/` 新包：`signal_detector` / `optimizer` / `store` / `scorer`，纯逻辑、可单测，不依赖 agent loop。
- **复用现成件**：`LLMClient`（生成经验/判定效果）、`SkillManager`（读 skill 内容）、approval 机制（审批门）。

> "接线层 / 核心层"不是行业通用术语，只是给两层起个直白名字：接线层把外部入口挂上，核心层装全部进化逻辑。

### ② 何时触发？

不是每轮都跑，而是 invoke 结束后或手动时才扫信号：

- **自动**：`AFTER_INVOKE`（一次 invoke 结束后扫信号），受 `evolution.enabled` 总开关控制（默认关）。
- **手动**：RPC `skills.evolve <name>` 显式对一个 skill 跑一次。
- **（v1.2）主动复盘**：每 N 轮 `AFTER_TASK_ITERATION` 塞一个自我检查 follow-up。
- 写改前默认人批（`auto_save: false`）。

### ③ 怎么实现（闭环 5 步）？

```
①信号检测 → ②LLM 生成经验 → ③存储+固化 → ④打分 E/U/F → ⑤反馈环判定 → 回写 → 重排 ↺
```

逐步落到 Twinkle 文件见 §四。

### ④ 数据存哪？

```
<workspace>/skills/<name>/
├── SKILL.md                      # 注入 <!-- evolution-index-start/end --> 索引块
├── evolutions.json               # entries: EvolutionRecord[]
└── evolution/
    ├── <section>.md              # 经验正文，按 record.id 锚定
    └── scripts/<file>            # script-target 记录的脚本工件
```

`SkillManager` 的 mtime 热重载天然能感知 `SKILL.md` 变化（索引块更新 → 下次 `list_skills` 重扫），无需额外通知机制。

### ⑤ 效果怎么保证？

六道保险，让进化「真能越用越好」而非「LLM 乱写一气」：

- **真闭环（非一次写入）**：经验写进去后还要被注入 → 跑对话 → LLM 判定帮没帮上 → 回写统计 → 重算分 → 重排。得分随真实效果升降。
- **数量上限 + 去重**：文本≤2 / 脚本≤1，防膨胀。
- **蒸馏淘汰**：分<0.4 且零调用 → 删，审批门控。
- **不静默**：默认审批门，复用 Twinkle approval 机制；`auto_save` 才自动落。
- **规则归因**：信号检测和"失败算哪个 skill"靠正则+路径，不用 LLM。
- **版本对齐**：新鲜度 F 在 skill 版本不匹配时 ×0.7，旧经验自动降权。

---

## 三、整体设计

### 3.1 两层切分：接线层接线，核心层决策

```
接线层 (Twinkle agentserver, 薄)               核心层 (evolution/ 包, 进化逻辑)
─────────────────────────────────────          ────────────────────────────────────────────
hooks/builtin/evolution_hook.py                evolution/signal_detector.py    (ConversationSignalDetector)
  (AFTER_INVOKE → 跑进化)                       evolution/optimizer.py         (SkillExperienceOptimizer)
skills/rpc.py                                   evolution/store.py             (EvolutionStore)
  (evolve/evolve_list/evolve_simplify RPC)      evolution/scorer.py            (ExperienceScorer)
                                                evolution/types.py             (EvolutionRecord/Patch/UsageStats)
```

接线层只做接线：挂 hook、路由事件、暴露 RPC。**所有"要不要改、改成什么、怎么打分、怎么合并"都在 `evolution/`（核心层）里**。

### 3.2 闭环：5 步 + 审批门 + 蒸馏

```
①信号检测            ②LLM 生成经验         ③存储 + 固化              ④打分              ⑤反馈环
ConversationSignal → SkillExperience    → EvolutionStore          → ExperienceScorer → 下次注入 → 对话 →
Detector             Optimizer            evolutions.json           (E/U/F)            LLM 判定 → 更新分 → 重排
(失败/纠正/脚本工件)  .generate_records    + render_evolution          update_score
                                         _markdown (写回 SKILL.md)
                                                                   + 审批门(复用 Twinkle approval)
                                                                   + 蒸馏(simplify: DELETE/MERGE/REFINE/KEEP)
```

### 3.3 数据模型

每个 skill 一个 `evolutions.json`，存 `EvolutionLog.entries: List[EvolutionRecord]`。

**`EvolutionRecord`**（`evolution/types.py`）：

```python
@dataclass
class EvolutionRecord:
    id: str                  # ev_<8位hex>
    source: str              # execution_failure / user_intent / script_artifact / conversation_review
    timestamp: str           # ISO UTC
    context: str             # 信号上下文
    change: EvolutionPatch   # 一条改动
    score: float = 0.6       # E/U/F 综合分
    usage_stats: UsageStats | None = None
    skill_version: str | None = None
    summary: str | None = None
    # 注意:无 applied 字段——参考实现里它是摆设,Twinkle 以"渲染进索引块"为生效事实
```

**`EvolutionPatch`**（即上面的 `change`）：

```python
@dataclass
class EvolutionPatch:
    section: str        # Instructions/Examples/Troubleshooting/Scripts/...
    action: str         # append/merge/replace/skip
    content: str        # 要写回的 Markdown / 脚本源码
    target: str = "body"   # description / body / script
    skip_reason: str | None = None
    merge_target: str | None = None   # 改写哪条已有记录
    script_filename: str | None = None
    script_language: str | None = None
    script_purpose: str | None = None
    keywords: list[str] | None = None
    summary: str | None = None
```

**`UsageStats`**（打分用）：

```python
@dataclass
class UsageStats:
    times_presented: int = 0   # 被注入给 agent 的次数（由注入层写，非 scorer）
    times_used: int = 0
    times_positive: int = 0
    times_negative: int = 0
    last_presented_at: str | None = None
    last_evaluated_at: str | None = None
```

### 3.4 触发点（用 Twinkle 的 HookEvent）

`SkillEvolutionHook`（priority≈80）override `after_invoke`（默认触发点）。可选切到 `after_tool_call` / `after_model_call` / `after_task_iteration`（后两个 reserved，需 agent_loop 启用）。

| HookEvent | 含义 | 适合作触发点吗 |
|---|---|---|
| `AFTER_INVOKE` | 一次完整 invoke 结束 | ✓ 默认——一轮对话后扫信号最自然 |
| `AFTER_TOOL_CALL` | 每次工具调用后 | 细粒度，信号捕获更及时但开销大 |
| `AFTER_TASK_ITERATION` | 一个任务迭代后 | （v1.2）主动复盘用 |

两个开关：**何时**（trigger HookEvent）+ **是否**（`evolution.enabled` 总开关）。

---

## 四、实现：逐步落到 Twinkle 文件

### 4.1 信号检测：`evolution/signal_detector.py`

`ConversationSignalDetector`，**规则为主、可选 LLM**（不开 LLM 也能跑）。扫 `AFTER_INVOKE` 时对话里的工具调用结果：

| 信号类型 | 触发 | 归入 section |
|---|---|---|
| `execution_failure` | 工具结果匹配失败关键词（error/exception/failed/timeout/econnrefused/enoent/permission denied…） | Troubleshooting |
| `script_artifact` | 代码执行工具调用成功（结果无失败关键词），抽脚本评估复用价值 | Scripts |
| `user_intent` | 用户纠正短语（wrong/should be/not that/actually…），**默认关** | Instructions |

**关键——把信号归因到具体 skill**（不然失败不知道算谁头上）。复用 `SkillManager` 已有的 skill 名单，两条路：

1. 正则扫 read/file 类工具参数路径 `.../<skill>/SKILL.md` → 取目录名；
2. 工具名是 `skill_tool` 且参数含 `skill_name` → 直接取。

取"最近一次读过的 skill"作为当前消息归因（`_resolve_active_skill`）。**纯规则、不用 LLM**，便宜可复现。

### 4.2 LLM 生成经验：`evolution/optimizer.py`

`SkillExperienceOptimizer.generate_records`：把"信号 + SKILL.md 摘要 + 对话片段 + 已有经验（去重用）"拼 prompt，调 `LLMClient`，解析 JSON drafts 为 `EvolutionRecord`。

- **三渠道**：A 预检测信号（规则已归因的 failure/script）、B 执行轨迹直接分析（多次重试才成功的 workaround）、C 脚本工件提取。
- **数量上限**（prompt 写死 + 代码强校验）：文本≤2、脚本≤1，超的按优先级标 skip。
- **种子分**（生成时初值，区别于后续 E/U/F 重算）：failure 0.65 / user_intent 0.70 / script 0.60 / review 0.50。
- **决策流**：相关性（不相关→skip）→ 去重（重复→skip；相似有增量→`merge_target` 改写；相似但本轮仍出错→优先改写不跳过；全新→继续）→ 优先级筛选 → 定 target+section。
- **解析重试**：截断→全量重生、JSON 损坏→修复 prompt，若干轮不成就返回空。

### 4.3 存储 + 固化：`evolution/store.py`

`EvolutionStore`，**扩展现有 `skills/` 目录布局**（不另起炉灶）：

- `append_record` — 追加/合并到 `evolutions.json`（按 `merge_target` 决定 append/merge）；
- `save_evolution_log` — 原子 temp-file rename 写盘；
- `render_evolution_markdown` — 往 `SKILL.md` 注入/替换 `<!-- evolution-index-start/end -->` 索引块，正文写 sidecar `evolution/<section>.md`（每条用 `<a id="{record.id}">` 锚定）。

**经验正文不内联进 SKILL.md 正文**——只注入一个 delimited 索引块指向 sidecar。跨用户分享前（如上传 SkillHub）剥掉索引块，保证 hub 上存的是作者原文。

`SkillManager` 的 mtime 签名里已经包含每个子目录 `SKILL.md` 的 mtime——索引块写回后 mtime 变 → 下次 `list_skills` 自动重扫，无需手动通知。

### 4.4 打分 E/U/F：`evolution/scorer.py`

`ExperienceScorer`，常量走 config（见 §六）：

- **E 效能**（贝叶斯平滑，Beta(1,1) 先验）：`(times_positive + 1) / (times_positive + times_negative + 2)`，无数据返回 0.5；
- **U 利用率**：`times_used / times_presented`，无数据 0.5；
- **F 新鲜度**：`0.5 + 0.5 * 2^(-days_old/半衰期)`，版本不匹配再 ×`stale_version_penalty`；
- 综合：`w_E·E + w_U·U + w_F·F`（默认 0.5/0.3/0.2）。

排序：`EvolutionStore.get_records_by_score(name, min_score=...)`，RPC `skills.evolve_list <name> --sort score` 可查。

### 4.5 反馈环（核心 / 最硬核）：`ExperienceScorer.evaluate`

经验下次被注入给 agent 后，取之后的对话片段，**用 LLM 逐条判定 used/positive/negative**，`update_score` 回写 `UsageStats`，重算 E/U/F，重排。

- **注入**：扩展 `SkillHook.before_model_call`——注入 skill 清单时，同时把该 skill 的 top-N 高分经验摘要拼进去；`times_presented` 由这里 +1（scorer 不碰）。
- **判定**：`AFTER_INVOKE` 时，取本轮注入过的经验 + 注入之后的对话片段，调 `LLMClient` 出 `{record_id, used, positive, negative, reason}[]`。
- **回写**：`update_score` 消费三个布尔 → 更新 `UsageStats` → 重算 `score`。

闭环跑通：**生成经验 → 种子分 → 注入 → 跑对话 → LLM 判定 → 更新 used/positive/negative → 重算 E/U/F → 重排 → 下次优先注入高分、淘汰低分**。

### 4.6 审批门控：复用 Twinkle approval

生成的经验不直接落盘，先 stage 成 pending 批次。**复用 Phase 4 的 approval 机制**（不另造一套 staging）：

- `requires_approval = not auto_save`（默认 `auto_save: false` → 要人批）；
- 待批批次通过 RPC `skills.evolve_pending` 暴露给前端，前端 `skills.evolve_approve` / `skills.evolve_reject`；
- approve → `EvolutionStore.append_record` 落盘 + 重渲染索引块；reject → 丢弃。

> 也可走 `approval.ask`（mid-chat HITL）同款通路，但 v1 用独立 RPC 更解耦（evolve 审批不在 chat 流里阻塞）。

### 4.7 蒸馏与重建

- `ExperienceScorer.simplify`：逐条提 DELETE/MERGE/REFINE/KEEP（分<`min_score` 且零调用 → 删），对应 RPC `skills.evolve_simplify`，审批门控。
- `ExperienceRebuildService`（v1.2）：把累积经验重组成 SKILL.md，对应 `skills.evolve_rebuild`——生成后续任务跑，不直接覆写。

---

## 五、取舍清单

| 维度 | 参考实现 | Twinkle 取舍 |
|---|---|---|
| `applied` 字段 | 摆设（无人调用） | **不加**，以渲染进索引块为生效事实 |
| 总开关默认 | 代码 True / 文档 false 矛盾 | **`enabled: false`**（opt-in，代码配置一致） |
| 自动创建 skill | rail 未就绪 | **v1 不做** |
| 离线 RL / trainer | 另一条轴 | **不纳入** |
| 类名 | 文档≠代码 | **设计稿即代码名** |
| 审批机制 | 自研 staging | **复用 Twinkle approval 思路**（v1 用独立 RPC 解耦） |
| 反馈环 | LLM 判定 used/pos/neg | **借鉴**（核心，不能省） |
| 规则归因 | 正则/路径 | **借鉴**（便宜可靠） |
| 数量上限 | 文本≤2/脚本≤1 | **借鉴** |
| E/U/F | 贝叶斯+利用率+新鲜度 | **借鉴**，权重走 config |
| 经验存储 | 索引块+sidecar | **借鉴**，扩展现有 `skills/` 目录 |
| fuzzy review | 每 5 轮主动复盘 | **v1.2 再加** |

---

## 六、配置与文件清单

### config.yaml 新增（schema.py 加 `EvolutionConfig`）

```yaml
evolution:
  enabled: false                # 总开关,默认关(进化改用户 skill,保守 opt-in)
  trigger: after_invoke         # after_invoke | after_tool_call | after_model_call | none
  auto_save: false              # 默认审批门(不静默写改);true 才自动落盘
  max_text_records: 2           # 单轮文本经验上限
  max_script_records: 1         # 单轮脚本经验上限
  scoring:
    w_effectiveness: 0.5
    w_utilization: 0.3
    w_freshness: 0.2
    freshness_half_life_days: 90
    stale_version_penalty: 0.7
  distill:
    min_score: 0.4              # 分低于此 + 零调用 → 删
  signals:
    execution_failure: true
    script_artifact: true
    user_intent: false          # 默认关
```

### 新文件

- `twinkle/agentserver/evolution/__init__.py` — re-exports + 进程级单例访问器（照 `skills/__init__.py` 形态）。
- `twinkle/agentserver/evolution/types.py` — `EvolutionRecord` / `EvolutionPatch` / `UsageStats`（无 `applied`）。
- `twinkle/agentserver/evolution/signal_detector.py` — `ConversationSignalDetector`。
- `twinkle/agentserver/evolution/optimizer.py` — `SkillExperienceOptimizer`。
- `twinkle/agentserver/evolution/store.py` — `EvolutionStore`（扩展现有 `skills/` 目录布局）。
- `twinkle/agentserver/evolution/scorer.py` — `ExperienceScorer`（E/U/F + `evaluate` + `simplify`）。
- `twinkle/agentserver/hooks/builtin/evolution_hook.py` — `SkillEvolutionHook`（接线层，`AFTER_INVOKE`）。

### 修改

- `twinkle/agentserver/hooks/builtin/__init__.py` — 导出 `SkillEvolutionHook`。
- `twinkle/agentserver/server.py:build_agent_loop` — 注册 `SkillEvolutionHook()`（条件：`evolution.enabled`）。
- `twinkle/agentserver/hooks/builtin/skill_hook.py` — `before_model_call` 注入 skill 清单时，顺带注入 top-N 高分经验 + `times_presented += 1`。
- `twinkle/agentserver/skills/rpc.py` — 加 `skills.evolve` / `skills.evolve_list` / `skills.evolve_simplify` / `skills.evolve_pending` / `skills.evolve_approve` / `skills.evolve_reject`。
- `twinkle/config/schema.py` + `twinkle/resources/config.yaml` — 加 `EvolutionConfig`。
- `web/src/components/SkillsView.vue` + `useSessions.ts` — 经验面板（evolve_list 查看、pending 审批）。

### 测试（`asyncio.run()` + `free_port`/`port_factory`，无 pytest-asyncio）

- `tests/test_evolution_signal.py` — 信号检测 + skill 归因（规则，可单测）。
- `tests/test_evolution_optimizer.py` — LLM 生成（mock LLMClient，验数量上限 + 去重）。
- `tests/test_evolution_store.py` — append/merge、原子写、索引块渲染、sidecar、pristine 剥离。
- `tests/test_evolution_scorer.py` — E/U/F 计算、`evaluate` 回写、`simplify`。
- `tests/test_evolution_rpc.py` — 全栈 RPC（照 `test_skill_rpc_round_trip` 形态）。

---

## 七、落地分阶段

- **v1（闭环骨架）**：信号检测（failure/script）→ LLM 生成（上限+去重）→ store（evolutions.json + 索引块 + sidecar）→ E/U/F 打分 → 审批门（独立 RPC）→ 蒸馏。**反馈环 `evaluate` 也含在内**（闭环不能省：注入 + 判定 + 回写），否则"效果保证"是空话。
- **v1.1**：经验注入策略优化（top-N 选择、按 section 分配）、前端经验面板。
- **v1.2**：fuzzy review（每 N 轮主动复盘）、`evolve_rebuild`（经验重组 SKILL.md）。

---

## 八、小结

Twinkle 的 skill 自进化目标是一个**真闭环、不静默、LLM 驱动、规则归因**的系统，回扣速答五问：

- **怎么设计的**(①)：两层切分——接线层 `SkillEvolutionHook` 挂 `AFTER_INVOKE`，核心层 `evolution/` 包装全部逻辑；复用 `LLMClient`/`SkillManager`/approval。
- **何时触发**(②)：invoke 结束后（`AFTER_INVOKE`）或手动 `skills.evolve`；受 `evolution.enabled` 总开关控制，默认关。
- **怎么实现**(③)：闭环 5 步——信号检测 → LLM 生成经验 → 存储+固化 → 打分 E/U/F → 反馈环判定 → 回写 → 重排。
- **数据存哪**(④)：`<workspace>/skills/<name>/` 下 `evolutions.json` + `SKILL.md` 索引块 + sidecar `evolution/<section>.md` + `evolution/scripts/`。
- **效果怎么保证**(⑤)：六道保险——真闭环、数量上限+去重、蒸馏淘汰、不静默审批、规则归因、版本对齐。

借鉴的是被验证有效的闭环机制（5 步 + E/U/F + 规则归因 + 索引块/sidecar 存储 + 审批门 + 蒸馏），丢掉的是摆设（`applied` 字段）、矛盾（`auto_scan` 默认值）、未就绪（自动建 skill）、不相关（离线 RL）。skill 不再是部署即冻结的提示词，而是随使用持续修正的活文档——这也是它区别于"下载安装别人的 skill"（消费侧）和"长期记忆 LTM"（记忆侧）的根本点：自进化改的是 **skill 自身**。
