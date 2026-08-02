# jiuwenswarm 的 Skill 自进化：让提示词成为会成长的"活文档"

> 日期：2026-08-02
> 性质：参考实现拆解（基于 jiuwenswarm 真实源码）
> 源码基路径：`D:/code/opensource/gitcode/jiuwenswarm/.venv_develop/Lib/site-packages/openjiuwen/`（下文以 `openjiuwen/...` 简写）
> 本文目标：把 skill 自进化的"是什么 / 为什么 / 怎么设计 / 怎么实现"讲清楚，所有结论标注到文件与行号，方便后续在 Twinkle 落地时对照。

---

## 一、痛点与定义：把 skill 从"冻结"变成"活文档"

设计稿 `docs/en/SkillSelfEvolution.md` 开篇点睛：

> Most agents **freeze** skills after deployment: tool errors become log lines; user corrections do not change behavior.

一个 skill 本质是一段 `SKILL.md`（frontmatter + 正文）。传统模式下：

- 工具调用超时 / 报错 → 只进日志，下次还犯同样的错；
- 用户说"不对，我要的是上海不是北京" → 这一次被纠正，下次 skill 仍按默认北京走；
- skill 里缺的 Troubleshooting、缺的 Examples，永远缺下去。

**skill 自进化要解决的就是：把"用了之后学到的"固化回 skill 本身，让 SKILL.md 成为随真实使用而增长的活文档。** 设计稿一句话："Skills stay **living documents**: risks, examples, and fixes accumulate from real use."

综合源码行为给定义：

> **skill 自进化 = 一个闭环反馈系统**：从 agent 运行时的工具调用 / 对话中检测"信号"（失败、纠正、可复用脚本），用 LLM 把信号转成结构化的"经验记录"存进每个 skill 的 `evolutions.json`，按 E/U/F 打分排序；下次加载该 skill 时把经验注入，再用 LLM 判定"这条经验这次帮上没帮上"回写使用统计——从而让 skill 内容随真实使用持续修正、去重、重组。

三个容易混淆的边界，先划清：

| 概念 | 改的是 | 是不是 skill 自进化 |
|---|---|---|
| 从 SkillHub/SkillNet 下载安装 skill | 装别人的 skill 进本地 | ✗ 那是"消费侧" |
| dreaming / 长期记忆 | 把会话蒸馏进 `.dreams` 记忆库 | ✗ 不写不改 skill（见 §四.1） |
| **skill 自进化** | **skill 自身的 SKILL.md + evolutions.json** | **✓** |

---

## 速答五问：架构 / 触发 / 实现 / 数据 / 效果

讲完「为什么、是什么」，下面五个问题把「怎么设计、何时触发、怎么实现、数据存哪、效果怎么保证」一次性答清。每问末尾标了对应小节，细节在后。

### ① 怎么设计的(两层架构)?

jiuwenswarm 把进化切成两层（**不是行业通用术语，只是给它两层起个直白名字**）：

- **适配层** = jiuwenswarm 仓库（应用侧，薄）：只做接线——slash 命令、往前端推审批/状态、注册 rail、Web UI 编辑入口；
- **核心层** = openjiuwen SDK（独立包，厚）：承载全部进化逻辑——信号检测、LLM 生成经验、存储固化、E/U/F 打分、反馈环判定。

> jiuwenswarm 自己的文档把这俩叫 host / engine，本文换直白说法。所有"要不要改、改成什么、怎么打分、怎么合并"都在核心层。→ 详见 §2.1

### ② 何时触发 skill 进化?

不是每次对话都进化，而是「skill 被调用后」或「手动 / 复盘」时才扫信号：

- **自动(默认主路径)**：`SkillEvolutionRail`(priority=80)挂在 agent 步骤末，默认触发点 `AFTER_INVOKE`(skill 调用之后)；受 `auto_scan` 开关控制(代码默认 `True`，设计稿示例写 `false`，见 §四.6)。可选 `AFTER_MODEL_CALL` / `AFTER_TOOL_CALL` / `AFTER_TASK_ITERATION` / `NONE`。
- **手动**：`/evolve <skill> [user_query]` 显式对一个 skill 跑一次进化扫描。
- **主动复盘**：每 `fuzzy_review_interval`(默认 5)轮非 follow-up 迭代后，往 agent 队列塞一个自我检查 follow-up prompt，主动触发一轮复盘进化。

写改前默认要人批(见 ⑤)。→ 详见 §2.4、§3.7

### ③ 怎么实现(闭环 5 步)?

```
①信号检测 → ②LLM 生成经验 → ③存储+固化 → ④打分 E/U/F → ⑤反馈环判定 → 回写 → 重排 ↺
```

1. **信号检测**(`ConversationSignalDetector`，规则为主、可不开 LLM)：扫工具结果里的失败关键词(`execution_failure`)、成功脚本(`script_artifact`)、用户纠正(`user_intent`，默认关)；并把信号**归因到具体 skill**(正则扫 read/file 类工具参数路径，或 `skill_name` 参数，取最近读过的 skill)。
2. **LLM 生成经验**(`SkillExperienceOptimizer.generate_records`)：「信号 + SKILL.md 摘要 + 对话片段 + 已有经验(去重)」拼 prompt，LLM 出 JSON draft → 解析成 `EvolutionRecord`。**数量上限：文本 ≤2 条、脚本 ≤1 条**，超的按优先级标 skip。
3. **存储 + 固化**(`EvolutionStore`)：经验记进 `evolutions.json`(按 `merge_target` append/merge)，原子 temp-rename 写盘；同时往 SKILL.md 注入一个 delimited 索引块，正文写 sidecar `evolution/<section>.md`(不内联进 SKILL.md 正文，跨用户分享前剥掉索引块)。**审批门控**：默认 stage 等人批，`/evolve*` 不静默写改；`auto_save=True` 才自动批。
4. **打分 E/U/F**(`ExperienceScorer`)：E 效能(贝叶斯平滑 `(pos+1)/(pos+neg+2)`)、U 利用率(`used/presented`)、F 新鲜度(90 天半衰期 × 版本匹配惩罚)；综合 `0.5E + 0.3U + 0.2F`。
5. **反馈环**(`ExperienceScorer.evaluate`，最硬核)：经验下次注入 agent 后，取之后的对话片段，**LLM 逐条判定 used/positive/negative**，回写 `UsageStats`，重算 E/U/F，重排——高分优先注入、低分被蒸馏淘汰。

→ 逐步带代码拆解见 §3.1–§3.6

### ④ 经验存哪、长啥样?

每个 skill 一个 `evolutions.json`（常量 `_EVOLUTION_FILENAME`），存 `EvolutionLog.entries: List[EvolutionRecord]`：

- **`EvolutionRecord`**：`id`(ev_8位hex)、`source`(execution_failure/user_intent/script_artifact/conversation_review)、`context`、`change: EvolutionPatch`、`score`、`usage_stats: UsageStats(times_presented/used/positive/negative)`、`skill_version`。
- **`EvolutionPatch`**：`section`(Instructions/Examples/Troubleshooting/Scripts/...)、`action`(append/merge/replace/skip)、`content`、`target`(description/body/script)、`merge_target`(改写哪条已有)。

**经验正文不内联进 SKILL.md 正文**——往 SKILL.md 注入一个 `<!-- evolution-index-start/end -->` 索引块（指向 sidecar），正文写到 `evolution/<section>.md`，每条用 `<a id="{record.id}">` 锚定；脚本工件写 `evolution/scripts/<file>`。跨用户分享前 `read_pristine_skill_content` 会**剥掉索引块**，保证 hub 上存的是作者原文。

```
~/.jiuwenswarm/workspace/agent/skills/<skill_name>/
├── SKILL.md                      # 含 <!-- evolution-index-* --> 索引块
├── evolutions.json               # entries: EvolutionRecord[]
└── evolution/
    ├── <section>.md              # 经验正文，按 record.id 锚定
    └── scripts/<file>            # script-target 记录的脚本工件
```

→ 详见 §2.3、附录

### ⑤ 效果怎么保证?

六道保险，让进化「真能越用越好」而不是「LLM 乱写一气」：

- **真闭环(非一次写入)**：经验写进去后，还要被注入 → 跑对话 → LLM 判定帮没帮上 → 回写统计 → 重算分 → 重排。**得分随真实效果升降**，低效经验自动降权，不是写一次就永久占位。
- **数量上限 + 去重**：生成时强制文本≤2 / 脚本≤1，重复→`merge_target` 改写、相似→skip，防止经验库膨胀淹没信号。
- **蒸馏淘汰**：`simplify` 逐条提 DELETE/MERGE/REFINE/KEEP(分 < 0.4 且零调用 → 删)，审批门控，定期清掉低质经验。
- **不静默(防误判污染)**：默认审批门，`/evolve*` 写改前必须人批；只有 `auto_save=True` 才自动落盘——避免 LLM 误判直接改坏 skill。
- **规则归因(防 LLM 抽风)**：信号检测和「失败算哪个 skill」靠正则 + 路径匹配，不用 LLM，便宜、可复现，不会因 LLM 抽风错归因。
- **版本对齐**：新鲜度 F 在 skill 版本不匹配时 × 0.7 惩罚，旧经验自动降权，避免过时经验误导。

→ 闭环关键见 §3.5，蒸馏见 §3.7，出入/坑见 §四

---

## 二、整体设计

### 2.1 两层切分：适配层接线，核心层决策

> 说明:「适配层 / 核心层」是 jiuwenswarm 的工程切分,**不是行业通用术语**,这里只是给它的两层起个直白名字。
> **适配层** = jiuwenswarm 仓库(应用侧,薄),把外部入口(slash 命令、前端审批推送、Web UI)接到 SDK;
> **核心层** = openjiuwen SDK(独立包,厚),承载全部进化逻辑。jiuwenswarm 自己的文档把这俩叫 host / engine,本文换成更直白的说法,后文「适配层」即 host、「核心层」即 engine。

```
jiuwenswarm 仓库 (适配层, 薄)               openjiuwen SDK (核心层, 真正的进化逻辑)
─────────────────────────────────────      ────────────────────────────────────────────
evolution_slash.py     (/evolve 命令)      agent_evolving.signal.ConversationSignalDetector
evolution_helpers.py   (往前端推状态/审批)  agent_evolving.optimizer.skill_call.SkillExperienceOptimizer
evolution_rails.py     (往 team-mgr 注册)   agent_evolving.checkpointing.EvolutionStore
skill/skill_manager.py (Web UI 编辑经验)    agent_evolving.experience.{ExperienceScorer, ExperienceManager, OnlineEvolutionOrchestrator}
                                          harness.rails.evolution.SkillEvolutionRail
```

适配层只做接线：slash 命令、往前端推审批/状态、注册 rail、Web UI 编辑入口。**所有"要不要改、改成什么、怎么打分、怎么合并"都在 `openjiuwen`(核心层)里**。

> ⚠️ 设计稿 `docs/en/SkillSelfEvolution.md` 里的概念类名 `SkillCallOperator`/`SkillOptimizer`/`SkillEvolutionManager`/`SignalDetector` **在代码里不存在**，真实类名是 `SkillExperienceOperator`/`SkillExperienceOptimizer`/`ExperienceManager`/`ConversationSignalDetector`。照文档找代码会扑空（见 §四.4）。

### 2.2 闭环：5 步 + 审批门 + 蒸馏

```
①信号检测            ②LLM 生成经验         ③存储 + 固化              ④打分              ⑤反馈环
ConversationSignal → SkillExperience    → EvolutionStore          → ExperienceScorer → 下次注入 → 对话 →
Detector             Optimizer            evolutions.json           (E/U/F)            LLM 判定 → 更新分 → 重排
(失败/纠正/脚本工件)  .generate_records    + render_evolution          update_score
                                         _markdown (写回 SKILL.md)
                                                                   + 审批门(ExperienceManager / OnlineEvolutionOrchestrator)
                                                                   + 蒸馏(simplify: DELETE/MERGE/REFINE/KEEP)
```

### 2.3 数据模型

每个 skill 一个 `evolutions.json`（常量 `_EVOLUTION_FILENAME`），存 `EvolutionLog`，里面是 `entries: List[EvolutionRecord]`。

**`EvolutionRecord`**（`openjiuwen/agent_evolving/checkpointing/types.py:114`）：

```python
@dataclass
class EvolutionRecord:
    id: str                            # make() 生成 ev_<8位hex>
    source: str                        # execution_failure / user_intent / script_artifact / conversation_review
    timestamp: str                     # ISO UTC
    context: str                       # 信号上下文
    change: EvolutionPatch             # 一条改动
    applied: bool = False               # 是否已"固化"（见 §四.2 的诚实提醒）
    score: float = 0.6                  # E/U/F 综合分
    usage_stats: Optional[UsageStats] = None
    skill_version: Optional[str] = None
    summary: Optional[str] = None
```

**`EvolutionPatch`**（`types.py:53`，即上面的 `change` 字段）：

```python
@dataclass
class EvolutionPatch:
    section: str        # Instructions/Examples/Troubleshooting/Scripts/Collaboration/...
    action: str         # append/merge/replace/skip（protocols.py:30 VALID_PATCH_ACTIONS）
    content: str        # 要写回的 Markdown / 脚本源码
    target: EvolutionTarget = EvolutionTarget.BODY   # description / body / script
    skip_reason: Optional[str] = None
    merge_target: Optional[str] = None              # 改写哪条已有记录
    script_filename / script_language / script_purpose: Optional[str] = None
    keywords: Optional[List[str]] = None
    summary: Optional[str] = None
```

**`UsageStats`**（`types.py:17`，打分用的使用统计）：

```python
@dataclass
class UsageStats:
    times_presented: int = 0     # 被注入给 agent 的次数（由呈现层写，非 scorer）
    times_used: int = 0          # 被采纳的次数
    times_positive: int = 0      # 产生正面效果的次数
    times_negative: int = 0      # 产生负面效果的次数
    last_presented_at: Optional[str] = None
    last_evaluated_at: Optional[str] = None
```

### 2.4 触发点（速答②的细节）

`SkillEvolutionRail`（`harness/rails/evolution/skill_evolution_rail.py:119`，`priority=80`）在 `__init__` 里持有一整套组件：

```python
self._evolution_store   = ...  # EvolutionStore
self._evolver            = ...  # SkillExperienceOptimizer
self._scorer             = ...  # ExperienceScorer
self._manager            = ...  # ExperienceManager
self._online_orchestrator = ... # OnlineEvolutionOrchestrator
self._experience_tracker  = ... # ExperienceTracker
```

触发由两个开关共同决定——**一个是"何时"，一个是"是否"**：

- **何时**：`evolution_trigger = EvolutionTriggerPoint.AFTER_INVOKE`（默认，`skill_evolution_rail.py:161`）。可选 `AFTER_MODEL_CALL` / `AFTER_TOOL_CALL` / `AFTER_TASK_ITERATION` / `NONE`（`evolution_rail.py:143`）。
- **是否**：`auto_scan: bool`（代码默认 `True`，`:147`）。`_allow_evolution_trigger` 在 `not auto_scan` 时直接返回 False。

对应配置（设计稿示例）：

```yaml
evolution:
  auto_scan: false      # 设计稿示例写「默认关」；env EVOLUTION_AUTO_SCAN 覆盖
  skill_create: false   # 自动创建新 skill，默认关；env SKILL_CREATE 覆盖
```

> ⚠️ 注意 `auto_scan` 的默认值在「代码」与「设计稿示例」之间不一致——代码里默认 `True`(见 §四.6)。落地 Twinkle 时要明确选一个默认。

---

## 三、实现：逐步拆解（带真实代码）

### 3.1 信号检测：`ConversationSignalDetector`

`agent_evolving/signal/from_conv.py:155`。**规则为主、可选 LLM**（不开 LLM 也能跑）。默认只开两类信号（`:197`）：

```python
enabled_signal_types = signal_types or {"execution_failure", "script_artifact"}
```

三类信号：

| 信号类型 | 触发 | 归入 section |
|---|---|---|
| `execution_failure`（`:408`） | tool 结果匹配 `_FAILURE_KEYWORDS`（error/exception/failed/timeout/econnrefused/enoent/permission denied…） | Troubleshooting |
| `script_artifact`（`:390`） | 代码执行工具调用**成功**（结果里无失败关键词），抽取脚本评估复用价值 | Scripts |
| `user_intent`（`:486`） | 用户纠正短语（wrong/should be/not that/actually…），默认关，需显式 `detect_user_intent()` | Instructions |

**关键——把信号归因到具体 skill**（不然失败不知道算谁头上）。`_detect_skill_from_tool_calls`（`:436`）两条路：

1. 正则扫 read/file 类工具的参数路径 `.../<skill>/SKILL.md` → 取目录名；
2. 工具名是 `skill_tool` 且参数含 `skill_name` → 直接取。

再用 `_resolve_active_skill`（`:425`）取"**最近一次读过的 skill**"作为当前消息的归因：

```python
@staticmethod
def _resolve_active_skill(msg_idx, skill_read_history):
    """Return the most recently read skill at or before *msg_idx*."""
    for idx, name in reversed(skill_read_history):
        if idx <= msg_idx:
            return name
    return None
```

### 3.2 LLM 生成经验：`SkillExperienceOptimizer.generate_records`

`agent_evolving/optimizer/skill_call/experience_optimizer.py:392`。把"信号 + SKILL.md 摘要 + 对话片段 + 已有经验（去重用）"拼进 prompt，调 LLM，解析 JSON drafts 为 `EvolutionRecord`。

prompt（`optimizer/skill_call/templates.py` 的 `SKILL_EXPERIENCE_GENERATE_PROMPT`）最硬核的几段：

**经验来自三个渠道（设计精髓）**：

- **渠道 A 预检测信号**：规则引擎已归因到当前 skill 的 execution_failure / script_artifact，默认应产出至少一条 append；
- **渠道 B 执行轨迹直接分析**：规则没完整捕获的——Agent 多次重试才成功的 workaround、导致错误的具体调用顺序/参数/前置检查缺失/恢复步骤；
- **渠道 C 脚本工件提取**：Agent 生成并成功执行的脚本（图表/数据处理/自动化），用 `target="script"`。

**数量限制（prompt 写死，代码也强校验）**：

```
文本经验不超过 2 条，脚本经验不超过 1 条，独立计数互不影响。
超过则按优先级保留最重要的，其余标 skip：
1. 导致失败/错误 > 导致低效但最终成功
2. 高频/可复现 > 单次偶发
```

代码侧强校验（`_build_records_from_drafts`，`:513`）：

```python
if is_script and len(script_records) >= 1:
    continue          # 脚本上限 1
if not is_script and len(text_records) >= 2:
    continue          # 文本上限 2
record = EvolutionRecord.make(
    source=source, context=merged_context, change=patch,
    score=INITIAL_SCORE_BY_SIGNAL.get(source, 0.6),   # 种子分
    summary=draft.summary,
)
```

**种子分**（`experience_optimizer.py:41`，生成时的初值，区别于后续 E/U/F 重算）：

```python
INITIAL_SCORE_BY_SIGNAL = {
    "execution_failure": 0.65,
    "user_intent": 0.70,
    "script_artifact": 0.60,
    "conversation_review": 0.50,
}
```

**决策流（prompt）**：相关性判断（不相关 → skip `irrelevant`）→ 去重判断（重复 → skip `duplicate`；相似但有增量 → `merge_target` 改写；**相似但本轮仍出错 → 优先改写不要跳过**；全新 → 继续）→ 优先级筛选（top2 文本 + top1 脚本，其余 `low_priority`）→ 定 target（description/body/script）+ section。

**LLM 要产出的 JSON schema**：

```json
[
  {
    "action": "append | skip",
    "skip_reason": "irrelevant | duplicate | low_priority | null",
    "target": "description | body | script",
    "section": "Instructions | Examples | Troubleshooting | Scripts | Collaboration",
    "summary": "一句话摘要 | null",
    "keywords": ["6-12 个关键词"],
    "content": "Markdown 或脚本源码 | null",
    "merge_target": "ev_xxxxxxxx 或 null",
    "script_filename": "...", "script_language": "...", "script_purpose": "..."
  }
]
```

解析有重试：`_generate_drafts_with_retries` 对截断输出走全量重生、对 JSON 损坏走 `JSON_FIX_PROMPT` 修复，若干轮内不成就返回空。

### 3.3 存储 + 固化（写回 SKILL.md）：`EvolutionStore`

`agent_evolving/checkpointing/evolution_store.py`。核心原语：

- `append_record` — 追加/合并到 `evolutions.json`（按 `merge_target` 决定 append 还是 merge）；
- `save_evolution_log` — 原子 temp-file rename 写盘；
- `write_skill_content(name, content)` — 覆写 SKILL.md（`:360`）；
- `create_skill(name, description, body, frontmatter)` — 建新 skill：校验 `^[a-zA-Z0-9_-]+$`、拒路径穿越、建 SKILL.md + 空 `evolutions.json` + `evolution/` 目录（实现 `store_archive.py:34`）。

**"固化"（solidify）不是把经验正文内联进 SKILL.md 正文，而是注入一个 delimited 索引块 + 把正文写到 sidecar 文件**。`store_projection.py:172` 的 `update_skill_md_index`：

```python
index_block = "\n".join([
    "<!-- evolution-index-start -->",
    "## Evolution Experiences",
    f"This skill has accumulated **{total}** evolution experiences ({parts}).",
    *experience_index_lines,      # 指向 evolution/<section>.md#<id>
    *script_table_lines,
    f"*Last updated: {now}*",
    "<!-- evolution-index-end -->",
])
content = await self._store.read_file_text(skill_md_path)
if _EVOLUTION_INDEX_PATTERN.search(content):
    content = _EVOLUTION_INDEX_PATTERN.sub(index_block, content)   # 替换旧块
else:
    content = content.rstrip() + "\n\n" + index_block + "\n"        # 追加新块
await self._store.write_file_text(skill_md_path, content)
```

经验的实际正文写到 `evolution/<section>.md`（`render_section_file`），每条用 `<a id="{record.id}"></a>` + `### [{record.id}] ...` 锚定。`read_pristine_skill_content`（`:149`）在跨用户分享前会**剥掉这个块**，保证 hub 上存的是作者原文。

### 3.4 打分：E/U/F

`agent_evolving/experience/scorer.py`。常量（`:22`）：

```python
W_E = 0.5; W_U = 0.3; W_F = 0.2
FRESHNESS_HALF_LIFE_DAYS = 90
STALE_VERSION_PENALTY = 0.7
```

三项：

- **E 效能**（贝叶斯平滑，Beta(1,1) 先验，`:198`）：`(times_positive + 1) / (times_positive + times_negative + 2)`，无数据返回 0.5 中性；
- **U 利用率**（`:211`）：`times_used / times_presented`，无数据 0.5；
- **F 新鲜度**（`:219`）：`0.5 + 0.5 * 2^(-days_old/90)`，90 天半衰期，从 1.0 衰减到 0.5；**版本不匹配再 ×0.7**。

综合（`:249`）：

```python
def calc_score(record, current_skill_version=None):
    stats = record.usage_stats or UsageStats()
    e = calc_effectiveness(stats)
    u = calc_utilization(stats)
    f = calc_freshness(record, current_skill_version)
    return W_E * e + W_U * u + W_F * f        # Score = 0.5E + 0.3U + 0.2F
```

排序：`EvolutionStore.get_records_by_score(name, min_score=...)`，`/evolve_list <skill> --sort score` 可查。

### 3.5 反馈环（速答⑤的核心 / 最硬核）：`ExperienceScorer.evaluate`

`scorer.py:325`。**闭环的关键**：经验下次被注入给 agent 后，取之后的对话片段，**用 LLM 逐条判定这条经验有没有被用到 / 正面 / 负面**，再 `update_score` 回写 `UsageStats`。

判定 prompt（`EXPERIENCE_EVAL_PROMPT`，CN 版，`scorer.py:43`）：

```
你是一个经验评估专家。根据对话片段，评估之前展示给 Agent 的经验是否被有效使用。

## 展示给 Agent 的经验
{presented_experiences}

## 对话片段（展示经验之后的部分）
{conversation_snippet}

## 评估任务
对于每条展示的经验，判断：
1. 该经验是否被 Agent 理解和采纳（内容被用于指导后续行为）
2. 该经验是否产生了积极效果（帮助解决了问题或改进了输出）
3. 该经验是否产生了消极效果（导致错误或误导）

## 输出格式
输出 JSON 数组，每条经验一个对象：
[{"record_id":"...","used":true/false,"positive":true/false,"negative":true/false,"reason":"简短说明"}]
只输出 JSON，不要其他内容。
```

（`presented_experiences` 的格式化见 `_format_presented_experiences`，`:452`：每条渲染成 `[<id>] <content 前 200 字>`；对话片段截断到 4000 字。）

`update_score`（`:260`）消费这三个布尔：

```python
if eval_result.get("used"):     stats.times_used += 1
if eval_result.get("positive"): stats.times_positive += 1
if eval_result.get("negative"): stats.times_negative += 1
stats.last_evaluated_at = datetime.now(tz=timezone.utc).isoformat()
record.score = calc_score(record, current_skill_version)
```

> 注意：`update_score` **不**自增 `times_presented`——分母 U 由"呈现层"（注入经验给 agent 的组件）维护；`UsageStats.last_presented_at` 也由呈现层写，scorer 不碰。

所以闭环完整跑通是：**生成经验 → 打种子分 → 注入 → 跑对话 → LLM 判定 → 更新 used/positive/negative → 重算 E/U/F → 重排 → 下次优先注入高分经验、淘汰低分**。

### 3.6 审批门控：`OnlineEvolutionOrchestrator.evolve` + `ExperienceManager`

生成的经验不是直接落盘，先 stage，按 `requires_approval` 决定停下等人批还是自动批。`agent_evolving/experience/online_orchestrator.py:53` 的 `evolve()` 流程：

1. **守卫**：`skill_name`/`signals` 空 → `skipped_no_input`；skill 不存在 → `skipped_skill_not_found`；
2. 取/建 per-skill operator（`SkillExperienceOperator`）；
3. `_build_context`：从 store 读 `skill_content` + 三类已有 pending 记录（desc/body/script）；
4. `_generate_local_apply_preview`：调 updater 生成 updates → 执行 → `manager.build_local_apply_preview`；
5. 空结果 → `no_evolution_no_records`；
6. `manager.stage_apply_results(...)`：把批次塞进 pending-approval 快照；
7. **分支**（`:128`）：

```python
if requires_approval:
    return OnlineEvolutionResult(status="staged", request=request, ...)    # 停下，等人批
apply_result = await self._manager.approve_request(request.request_id or "")  # 自动批
if not getattr(apply_result, "ok", False):
    return OnlineEvolutionResult(status="persistence_failed", ...)
return OnlineEvolutionResult(status="auto_approved", ...)
```

rail 侧：`run_evolution` 传 `requires_approval = not self._auto_save`（`:875`），所以 `auto_save=True` ⇒ 自动批。`/evolve`、`/evolve_simplify` 默认都要审批——"不静默写改"。

**真正的落盘**在 `ExperienceManager.approve_request → _commit_pending_change → common.commit_pending_change`（`agent_evolving/experience/common.py:50`）：

```python
for index, record in enumerate(records):
    try:
        await store.append_record(pending.skill_name, record, subject_kind=...)
    except Exception as exc:
        # 部分失败：保留未写的尾部，已写的不回滚
        pending.payload[:] = list(records[index:])
        return PendingCommitResult(applied_count=applied_count,
                                   pending_count=len(remaining), errors=errors)
    applied_count += 1
```

`append_record`（`store_records.py:138`）干四件事：(a) script-target 记录写 `evolution/scripts/<file>` 并把 `content` 改成引用；(b) 追加/合并进 `entries`（按 `merge_target`）；(c) `save_evolution_log` 原子写盘；(d) **`render_evolution_markdown` 重写 SKILL.md 索引块 + sidecar**。失败回滚 projection 文件和 `evolutions.json`。

### 3.7 蒸馏与重建（速答②的主动复盘 / 速答⑤的淘汰）

经验库会膨胀，所以要蒸馏。`ExperienceScorer.simplify`（`scorer.py:367`，`SIMPLIFY_PROMPT`）逐条提 `DELETE / MERGE / REFINE / KEEP`（如分 < 0.4 且零调用 → 删），对应 `/evolve_simplify`，**审批门控，不静默**。`ExperienceRebuildService`（`experience/rebuild.py`）把累积经验重组成 SKILL.md，对应 `/evolve_rebuild`——设计稿明说"它不是直接覆写按钮，会生成一个后续任务正常跑"。

还有 **fuzzy review**（`_on_after_task_iteration`，`skill_evolution_rail.py:532`）：每 `fuzzy_review_interval`（默认 5）轮非 follow-up 迭代后，往 agent 队列塞一个自我检查 follow-up prompt，主动触发一轮"复盘"进化。

---

## 四、几个必须知道的诚实提醒

写这篇的依据是已安装的 `openjiuwen` 快照（0.1.10 / 0.1.15），它 pinned 到 `gitcode.com/openJiuwen/agent-core.git@develop`（独立仓库、develop 分支、未锁版本）。所以下面这些"出入"是针对快照说的，live develop 分支可能已修。

1. **dreaming ≠ skill 自进化**。`agents/harness/common/memory/dreaming/sweeper.py` 是扫会话 transcript → 压缩 → 灌进 `.dreams` 长期记忆库，**不写不改 skill**。整文件 grep `skill` 只 1 命中且是 prompt 里的例子词。别拿来当自进化参考。
2. **`mark_records_applied` 在已装包里没有调用方**。`EvolutionStore.mark_records_applied`（`:509`）是唯一能把 `applied` 置 `True` 的方法，但 grep 全 `openjiuwen/` 包零调用。审批落盘走的是 `append_record`，approved 记录以 `applied=False` 进 `evolutions.json`，靠渲染进索引块生效——"applied" 这个字段在当前实现里基本是摆设。设计稿"`applied:true`=已固化"的描述与实现有出入。
3. **自动创建新 skill 还在路上**。`TeamSkillCreateRail`/`SkillCreateRail` 被适配层（jiuwenswarm 仓库）的 `evolution_rails.py` import，但已装 openjiuwen 快照里**不存在**该类（在 `develop-skill-creator`/`enterprise_vibeskill_dev` 分支开发，刚落的 `4c42b744 feat(skills): add skill-omni-creation skill` 也在动这块）。存储原语 `create_skill` 有，自动触发 rail 没。设计稿把它写成可用功能，快照里跑不起来。
4. **文档类名 ≠ 真实类名**。设计稿的 `SkillCallOperator`/`SkillOptimizer`/`SkillEvolutionManager`/`SignalDetector` 是概念命名，代码里真实是 `SkillExperienceOperator`/`SkillExperienceOptimizer`/`ExperienceManager`/`ConversationSignalDetector`。
5. **离线 RL 是另一条轴**。`agent_evolving` 下还有 `trainer/`、`agent_rl/`、`dataset/`、`evaluator/`、`trajectory/`——离线训练/轨迹学习，**不接在运行时 skill 自进化环上**，别混。
6. **`auto_scan` 默认值:代码 ≠ 设计稿示例**。§2.4 里代码默认 `auto_scan=True`(`skill_evolution_rail.py:147`)，但设计稿的 yaml 示例写 `auto_scan: false` 注「默认关」。两者矛盾——快照里跑起来是「代码默认开」。Twinkle 落地时要显式定一个默认值，别被设计稿的示例误导。

---

## 五、小结

jiuwenswarm 的 skill 自进化是一个**真闭环、不静默、LLM 驱动、规则归因**的系统。回扣速答五问:

- **怎么设计的**(①):两层切分——适配层(jiuwenswarm 仓库)接线,核心层(openjiuwen SDK)决策。
- **何时触发**(②):不是每轮都跑,而是 skill 调用后(`AFTER_INVOKE`)、手动 `/evolve`、或每 5 轮主动复盘时才扫信号;受 `auto_scan` 开关控制。
- **怎么实现**(③):闭环 5 步——信号检测 → LLM 生成经验 → 存储+固化 → 打分 E/U/F → 反馈环判定 → 回写 → 重排。
- **数据存哪**(④):每个 skill 一个 `evolutions.json`(EvolutionRecord[]),正文走 SKILL.md 索引块 + sidecar `evolution/<section>.md`,脚本走 `evolution/scripts/`。
- **效果怎么保证**(⑤):六道保险——真闭环让得分随真实效果升降、数量上限+去重防膨胀、蒸馏淘汰低质、审批门防误判、规则归因防 LLM 抽风、版本对齐降权旧经验。

四个性格标签:

- **真闭环**：生成 → 注入 → 判定 → 打分 → 重排，经验会因"帮没帮上"而升降；
- **不静默**：默认审批门，`/evolve*` 写改前都要人批；`auto_save` 才自动落；
- **LLM 驱动**：生成经验、判定效果、蒸馏清理都靠 LLM prompt；
- **规则归因**：信号检测和"哪条失败算哪个 skill"靠正则 + 路径匹配，不用 LLM，便宜可靠。

它的价值不在"自动改 skill"这个动作本身，而在**把每次失败和纠正都变成 skill 的增量**——skill 不再是部署即冻结的提示词，而是随使用持续修正的活文档。这也是它区别于"下载安装别人的 skill"（消费侧）和"长期记忆 dreaming"（记忆侧）的根本点：自进化改的是 **skill 自身**。

---

## 附录：slash 命令速查

| 命令 | 作用 | 是否审批门控 |
|---|---|---|
| `/evolve <skill> [user_query]` | 手动触发一个 skill 的进化（Planning 扫描失败/纠正；Cluster 需带 intent） | 是 |
| `/evolve_list <skill> [--sort score]` | 查某 skill 的经验记录与分数 | — 只读 |
| `/evolve_simplify <skill> [intent]` | 蒸馏清理（DELETE/MERGE/REFINE/KEEP） | 是 |
| `/evolve_rebuild <skill> [intent]` | 把累积经验重组成 SKILL.md（生成后续任务，非直接覆写） | 是 |
| `/evolve_rollback` | 回滚 | — |
| Web UI "View skill experience" | 编辑 `change.content`、删条目、保存 | 编辑即生效，下次对话自动加载 |
