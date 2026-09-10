# Skill 自进化实现 Walkthrough（从零看懂）

> 这是一份**教学文档**，目标是让你读完能"搞懂它是怎么实现的"。和同目录的 [skill-self-evolution-design.md](skill-self-evolution-design.md) 互补——那份是**速查规范**（密表 + 流程图，给已经懂的人查），这份是**逐步讲解**（给第一次读的人学）。读完这份再看那份规范就不费劲了。
>
> 源码在 [`twinkle/agentserver/evolution/`](../../twinkle/agentserver/evolution) + [`hooks/builtin/evolution_hook.py`](../../twinkle/agentserver/hooks/builtin/evolution_hook.py)。下面每一处都标了 `文件:行号`，可点开对照。

## 读完你能搞懂什么

1. 这个模块**为什么存在**——它解决的是什么问题；
2. 一次"失败 → 变成经验 → 下次帮上忙"的**完整闭环**怎么在代码里走一遍；
3. 每个文件（detector / optimizer / store / scorer / orchestrator / hook）**各自干什么、怎么衔接**；
4. 那些"看起来奇怪"的设计**为什么这么做**（零 LLM 检测、审批门、内存 pending、呈现计数落盘点……）；
5. 怎么**自己跑起来**看效果。

---

## Part 0. 它在解决什么问题

先别管代码，想一个场景：

你写了个 skill 叫 `excel-export`，SKILL.md 里写着"用 openpyxl 把 DataFrame 存成 .xlsx"。你发布出去，十个用户用它。**其中七个**第一次都踩同一个坑——没装 openpyxl，`ModuleNotFoundError`，然后自己摸索出"要先 pip install"。这七个的摸索经验，**没有一个写回 SKILL.md**。于是第十一个用户来了，又踩一遍。

**skill 自进化要解决的就是这个**：让 SKILL.md 变成一份**随真实使用而增长的"活文档"**——把每次失败、每次用户纠正、每个可复用脚本，自动沉淀成 skill 自己的增量经验，下次别人用这个 skill 时这些经验会自动出现，避免重复踩坑。

它和旁边两个容易混淆的东西**不是一回事**（这是理解的第一道关）：

| 概念 | 改的对象 | 是 skill 自进化吗 |
|---|---|---|
| 从 SkillHub 下载装 skill | 装别人的 skill 进本地 | ✗ 那是消费侧 |
| dreaming / 长期记忆 | 把会话蒸馏进 `.twinkle_data/memory` 记忆库 | ✗ 不写不改 skill |
| **skill 自进化** | **skill 自己的 SKILL.md + evolutions.json** | **✓ 就是它** |

一句话：**dreaming 记的是"这个 agent 跑过啥"，skill 自进化记的是"这个 skill 该怎么用才不踩坑"**——前者改记忆库，后者改 skill 自身。

---

## Part 1. 一个贯穿全文的例子

为了不空谈，我们用一个具体例子从头走到尾。假设有个 skill `excel-export`，目录：

```
skills/excel-export/
└── SKILL.md          # "用 openpyxl 把 DataFrame 存成 .xlsx"
```

某次对话（这就是后面要喂给检测器的 `messages` 列表）：

| # | role | 内容（简化） |
|---|---|---|
| 1 | user | "帮我把查询结果导出成 excel" |
| 2 | assistant | tool_call: `read_file(path=".../skills/excel-export/SKILL.md")` |
| 3 | tool | （返回 SKILL.md 内容） |
| 4 | assistant | tool_call: `run_code(code="openpyxl.save('out.xlsx')")` ← 忘了装依赖 |
| 5 | tool | `"ModuleNotFoundError: No module named 'openpyxl'"` ← **失败** |
| 6 | assistant | tool_call: `run_code(code="pip install openpyxl; ...save")` |
| 7 | tool | `"success, wrote out.xlsx"` ← **成功的脚本** |
| 8 | user | "不对，应该是 .xls 不是 .xlsx" ← **用户纠正** |

这一段对话里藏着**三种信号**：第 5 条的失败、第 7 条的可复用脚本、第 8 条的用户纠正。自进化的任务就是把它们变成 `excel-export` 的经验。下面五步就是怎么变的。

---

## Part 2. 先认全数据模型（4 个类）

别跳过这节，后面全是这几个类在流转。源码 [`evolution/types.py`](../../twinkle/agentserver/evolution/types.py)。

```python
# 一条"信号"——检测器的产物，优化器的输入
ConversationSignal(type, skill_name, context, msg_index)
  # type: execution_failure / script_artifact / user_intent
  # skill_name: 归因到哪个 skill（关键，见 Part 3.1）

# 一条"改动内容"——LLM 说"这条经验该怎么写进 skill"
EvolutionPatch(section, action, content, target, merge_target, skip_reason,
               script_filename, script_language, script_purpose, keywords, summary)
  # section: Instructions / Examples / Troubleshooting / Scripts
  # action: append / merge / replace / skip
  # target: description / body / script
  # merge_target: 要改写哪条已有记录的 id（ev_xxxxxxxx）

# 一条"经验记录"——最终落盘的东西
EvolutionRecord(id, source, timestamp, context, change, score,
                usage_stats, skill_version, summary)
  # id: ev_<8位hex>，生成时定（secrets.token_hex(4)）
  # source: 哪种信号生的它
  # score: 综合分，刚生时是"种子分"，反馈环后重算

# 使用统计——打分用，反馈环维护
UsageStats(times_presented, times_used, times_positive, times_negative,
           last_presented_at, last_evaluated_at)
```

**`evolutions.json`** 长这样（`EvolutionLog.entries` 是 `EvolutionRecord[]`）：

```json
{
  "entries": [
    {
      "id": "ev_a1b2c3d4",
      "source": "execution_failure",
      "timestamp": "2026-09-08T03:21:00+00:00",
      "context": "ModuleNotFoundError: No module named 'openpyxl'",
      "change": {
        "section": "Troubleshooting",
        "action": "append",
        "target": "body",
        "content": "若报 ModuleNotFoundError: openpyxl，先 `pip install openpyxl` 再导出。",
        "summary": "openpyxl 未安装时先装依赖"
      },
      "score": 0.65,
      "usage_stats": { "times_presented": 0, "times_used": 0, "times_positive": 0, "times_negative": 0 },
      "skill_version": null,
      "summary": "openpyxl 未安装时先装依赖"
    }
  ]
}
```

记住一个**故意去掉的字段**：jiuwenswarm（参考实现）里有个 `applied` 标记"这条经验是否已生效"，但 Twinkle 把它删了——因为那玩意儿在原版里就是摆设（唯一能置 True 的函数没人调用）。Twinkle 用**"渲染进 SKILL.md 索引块"作为生效事实**，不靠这个字段（见 Part 3.3）。这种"抄骨架、删摆设"的裁剪贯穿全模块。

---

## Part 3. 闭环五步，用上面的例子走一遍

整个自进化就是一个**闭环**。先看全貌，再逐段拆：

```
①检测信号 → ②LLM生成经验 → ③存储+固化 → ④打分E/U/F → ⑤反馈环(呈现→跑→判定→回写→重排)
                                                              ↑_next_time_高分索引块靠前、低分被蒸馏淘汰
```

注意 ①③④ 是"机械动作"（规则/IO/公式），②⑤是"调 LLM"。**只有两处花 LLM**，这是它便宜的关键。

### 3.1 ①信号检测——纯正则，零 LLM

[`evolution/signal_detector.py`](../../twinkle/agentserver/evolution/signal_detector.py) 的 `ConversationSignalDetector.detect()`。

**为什么不调 LLM？** 信号检测每次对话都要跑，调 LLM 又贵又会"抽风错归因"。用正则+路径匹配：**便宜、可复现、确定性**。这是个重要的设计取舍（见 Part 7）。

它干两件事：**先扫出"每条消息时 active 的是哪个 skill"，再逐条消息判信号类型**。

**第一步：建立 skill 读取历史**（`_detect_skill_from_tool_calls`，:95）。扫所有 assistant 的 tool_calls，两条路认 skill：

1. 工具参数里出现 `.../<skill_name>/SKILL.md` 路径 → 正则 `(?:^|[\\/])([a-zA-Z0-9_-]+)/SKILL\.md` 抓目录名；
2. 工具名是 `skill_tool` 且参数含 `skill_name` → 直接取。

我们的例子里，第 2 条消息的工具参数有 `.../skills/excel-export/SKILL.md` → 命中路 1 → 历史 = `[(2, "excel-export")]`。

**第二步：逐条消息判信号**（:29 的主循环）。对每条消息先 `_resolve_active_skill(idx)`（:132）——取"最近一次读过、且消息索引 ≤ 当前"的 skill。所以第 5/7/8 条消息的 active_skill 都是 `"excel-export"`（因为第 2 条读过它）。然后：

| 消息 | 判断 | 产出信号 |
|---|---|---|
| 5 (tool) | `_is_failure`：内容含 "error"/"not found" 等失败关键词（:65，对照 `FAILURE_KEYWORDS`） | `execution_failure`, skill=excel-export, context=内容前 500 字 |
| 7 (tool) | `_is_script_success`：工具名 `run_code` 在脚本工具集合里、内容非失败且 >20 字（:70） | `script_artifact`, skill=excel-export, context=前 1000 字 |
| 8 (user) | `_detect_user_intent`：含"不对"/"应该是"等纠正短语（:78） | `user_intent`, skill=excel-export, context=前 300 字 |

**三条信号全部归因到 `excel-export`**——这一步是闭环靠谱的前提：失败得知道算在谁头上。归因不到的信号直接丢（不硬塞）。

> 启用哪些信号由 config `evolution.signals.*` 控制，`orchestrator._get_enabled_signals()`（:219）每次 evolve 读。三个默认全开。

### 3.2 ②LLM 生成经验——唯一"智能"的地方

[`evolution/optimizer.py`](../../twinkle/agentserver/evolution/optimizer.py) 的 `SkillExperienceOptimizer.generate_records()`（:82）。

把「三条信号 + SKILL.md 摘要（截前 1500 字，:186）+ 已有经验（最近 10 条，去重用）」拼进 `SKILL_EXPERIENCE_GENERATE_PROMPT`（:24），调一次 LLM，要求它输出 JSON 草稿数组。

prompt 规定经验来自**三个渠道**：

- **A 预检测信号**——上面规则已归因的 failure/script，默认应产出至少一条 append；
- **B 执行轨迹直接分析**——规则没捕获的：多次重试才成功的 workaround、导致错误的具体调用顺序；
- **C 脚本工件提取**——Agent 生成并成功跑过的脚本，用 `target="script"`。

**数量上限写死在 prompt 里 + 代码强校验**（:131 `_build_records_from_drafts`，文本≤2 / 脚本≤1，独立计数）。超了按"导致失败 > 低效但成功；高频 > 偶发"保留最重要的，其余标 `skip`。**去重决策流**：不相关→`skip=irrelevant`；重复→`skip=duplicate`；相似但有增量→`merge_target` 改写已有记录（**相似但本轮仍出错 → 优先改写不要跳过**）；全新→继续。

我们的例子，LLM 可能产出 3 条草稿：

```json
[
  {"action":"append","target":"body","section":"Troubleshooting",
   "summary":"openpyxl 未安装时先装依赖",
   "content":"若报 ModuleNotFoundError: openpyxl，先 `pip install openpyxl` 再导出。"},
  {"action":"append","target":"body","section":"Instructions",
   "summary":"确认 .xls vs .xlsx 扩展名",
   "content":"用户说 .xls 时先确认是否真要旧格式二进制，默认按 .xlsx。"},
  {"action":"append","target":"script","section":"Scripts",
   "summary":"带依赖检查的安全导出脚本",
   "script_filename":"safe_export.py","script_language":"python",
   "content":"def safe_export(df, path):\n    import subprocess,sys\n    try:\n        import openpyxl\n    except ImportError:\n        subprocess.check_call([sys.executable,'-m','pip','install','openpyxl'])\n    df.to_excel(path)"}
]
```

代码把它们包成 3 个 `EvolutionRecord`。**种子分**（区别于后面重算的综合分）按信号来源给：`failure=0.65 / user_intent=0.70 / script=0.60 / conversation_review=0.50`（`INITIAL_SCORE_BY_SIGNAL`，types.py:91）。

> ⚠️ 一个当前简化：`_build_records_from_drafts` 用 `signals[0].type` 给**本批所有记录**打同一个 source 和种子分（:135）。我们的例子里 `signals[0]` 是第 5 条的 failure，所以三条记录都标 `source=execution_failure`、种子分 0.65——哪怕第三条其实来自脚本。这是 v1 的简化，不影响闭环正确性，但知道一下。

**解析失败有重试**（:108，2 轮，追加固格式提醒），还不成就返回空——不崩。JSON 输出契约的 prompt **硬编码进模块常量、不进 config**：因为用户改坏 prompt → 解析失败 → 静默失效，太危险（见 [JSON 契约 prompt 不进 config](../../../) 的决策）。

### 3.3 ③存储 + 固化——evolutions.json + 索引块 + sidecar

[`evolution/store.py`](../../twinkle/agentserver/evolution/store.py) 的 `EvolutionStore`。但注意：**生成的经验默认不直接落盘**，先进审批门（Part 5）。这里先假设批准了，看落盘长啥样。

落盘不是把经验正文塞进 SKILL.md 正文，而是**注入一个索引块 + 正文写 sidecar 文件**（`render_evolution_markdown`，:145）：

```
skills/excel-export/
├── SKILL.md                  # 末尾多了一个 <!-- evolution-index-* --> 块
├── evolutions.json           # 上面 Part 2 那个结构
└── evolution/
    ├── Troubleshooting.md    # rec1 正文，<a id="ev_a1b2c3d4"> 锚定
    ├── Instructions.md       # rec2 正文
    ├── Scripts.md            # rec3 正文
    └── scripts/
        └── safe_export.py    # 脚本工件实体文件
```

SKILL.md 末尾被注入的索引块长这样（:205）：

```markdown
<!-- evolution-index-start -->
## Evolution Experiences
This skill has accumulated **3** evolution experiences (Instructions(1), Scripts(1), Troubleshooting(1)).
- **[ev_a1b2c3d4]** (execution_failure, score=0.65) — openpyxl 未安装时先装依赖 [→](evolution/Troubleshooting.md#ev_a1b2c3d4)
- **[ev_b2c3d4e5]** (execution_failure, score=0.65) — 确认 .xls vs .xlsx 扩展名 [→](evolution/Instructions.md#ev_b2c3d4e5)
- **[ev_c3d4e5f6]** (execution_failure, score=0.65) — 带依赖检查的安全导出脚本 [→](evolution/Scripts.md#ev_c3d4e5f6)
*Last updated: 2026-09-08T03:21:00+00:00*
<!-- evolution-index-end -->
```

几个细节：
- **索引块存在则替换、不存在则追加**（:221，正则 `_EVOLUTION_INDEX_PATTERN` 匹配 start/end 之间整体替换）。所以经验库更新不会让 SKILL.md 越长越乱，永远是一个干净的块。
- **脚本工件**写实体文件到 `evolution/scripts/`，记录里的 `content` 改成引用 `"See evolution/scripts/safe_export.py"`（:174）——不把一大坨代码塞进 json。
- **原子写**：`save_evolution_log`（:99）用 temp-file + `os.replace` + `fsync`，避免写一半崩了把 json 写坏。
- **跨用户分享前剥索引块**：`read_pristine_skill_content`（:89）把索引块 `sub` 掉，保证上传到 SkillHub 的是作者原文、不带本地经验。

### 3.4 ④打分 E/U/F——三条腿的综合分

[`evolution/scorer.py`](../../twinkle/agentserver/evolution/scorer.py)。综合分 `Score = 0.5·E + 0.3·U + 0.2·F`（权重可 config 覆盖）。

| 维度 | 含义 | 公式 | 无数据兜底 |
|---|---|---|---|
| **E 效能** | 经验正面效果占比 | `(pos+1)/(pos+neg+2)`（:66，贝叶斯平滑 Beta(1,1)） | 0.5 |
| **U 利用率** | 被采纳率 | `used/presented`（:77） | 0.5 |
| **F 新鲜度** | 时间衰减 | `0.5 + 0.5·2^(-days/90)`（:84，90 天从 1.0 衰到 0.5）；版本不匹配再 ×0.7 | 0.5 |

**刚生成的记录，score 是种子分（0.65），不是 calc_score 算出来的**——因为还没有任何使用数据，E/U 都走 0.5 兜底，算出来没区分度。`calc_score` 要等反馈环跑过、有了真实 used/pos/neg 才有意义（在 `update_score` 里调，:182）。

三个"为什么"值得记：
- **E 为什么贝叶斯平滑**？防止"只用过 1 次且成功"就冲到 1.0 满分。加 1 个假阳 + 1 个假阴做先验，少量样本时往 0.5 拉一把，样本多了才信真实比例。
- **F 为什么有版本惩罚**？经验记录带 `skill_version`，skill 改版后旧经验自动 ×0.7 降权，避免过时经验误导。
- **U 为什么有 0.5 兜底**？没被呈现过（presented=0）时不算 0 分——还没机会证明自己，给个中性分。

### 3.5 ⑤反馈环——闭环的命门，也是最硬核的地方

这是整个系统"真闭环"还是"一次写入"的分水岭。在 [`scorer.py`](../../twinkle/agentserver/evolution/scorer.py) 的 `evaluate()`（:137）+ [`orchestrator.py`](../../twinkle/agentserver/evolution/orchestrator.py) 的 `run_feedback_loop()`（:164）。

逻辑：**经验下次被呈现给 agent（模型 `read_skill` 该 SKILL.md、看到索引块）之后，取之后的对话片段（截 ~4000 字），用 LLM 逐条判定"这条经验这轮帮上没帮上"**，再回写统计、重算分、重排——高分在索引块里靠前、低分被蒸馏淘汰。

`evaluate` 让 LLM 对每条经验输出 `{record_id, used, positive, negative, reason}`（:30 的 `EXPERIENCE_EVAL_PROMPT`）。`update_score`（:170）消费三个布尔：

```python
if used:     stats.times_used += 1
if positive: stats.times_positive += 1
if negative: stats.times_negative += 1
record.score = calc_score(...)   # 重算
```

`times_presented`（U 的分母）不在 `update_score` 里加——它在反馈环节点落盘：呈现时（`after_tool_call`）只记内存 id，反馈环从 store 重新读出记录后 `+1` 再 save。

**闭环完整跑通**：生成经验 → 打种子分 → 呈现（模型 read_skill）→ 跑对话 → LLM 判定 → 更新 used/pos/neg → 重算 E/U/F → 重排 → 高分在索引块靠前、低分被蒸馏淘汰。这才叫"随真实使用而增长"，而不是写一次就完事。

---

## Part 4. 怎么接进 agent 主循环（Hook）

上面五步是"核心层"，但它不会自己跑——得有人在每个对话的恰当时机调它。接进主循环的是**两个 Hook**，按 priority 从高到低串成一条"经验从生成到呈现到加载"的链：

| Hook | priority | 时机 | 一句话职责 |
|---|---|---|---|
| [`SkillHook`](../../twinkle/agentserver/hooks/builtin/skill_hook.py) | 90 | `before_invoke` | 注 skill **清单**（name+desc），是 skill 进上下文的入口 |
| [`SkillEvolutionHook`](../../twinkle/agentserver/hooks/builtin/evolution_hook.py) | 80 | `after_tool_call` + `after_invoke` | `read_skill` 呈现时记 presented + 跑反馈环/进化 |

`SkillEvolutionHook` 不再主动往 system message 注入经验——经验随 SKILL.md 索引块**按需呈现**（模型 `read_skill` 时才看到），hook 只在呈现时**记 presented 事件**供反馈环，不每步全量灌摘要进上下文。

### 两个 Hook 各自干什么

**`SkillHook`（清单注入，priority 90）**：`before_invoke` 时把 skill 清单塞进 `ctx.extra["frozen_sections"]`（`skill_hook.py:38`），由主循环每步套用到 `SystemPromptBuilder` 成**跨步稳定的前缀段**。两种模式：
- `all`（默认）：`## 可用技能\n1. {name}: {description}`——只取 frontmatter 的 name/description，**不读 SKILL.md body**（`skill_hook.py:36`）；
- `auto_list`：只塞一句"调 list_skill 看清单，再 read_skill 载入指令"（`skill_hook.py:32`）。

**`SkillEvolutionHook`（呈现记录 + 触发进化，priority 80）**，挂两个时机：

| 时机 | 做什么 | 代码 |
|---|---|---|
| `after_tool_call` | 监听 `read_skill(skill,"SKILL.md")`：模型加载该 skill 主体（含经验索引块）= 经验被呈现 → 记该 skill 全部 non-skip 经验 id 进 `_presented_ids_by_skill`（只记事件，不进上下文、不 +1）。非 read_skill / 读 sidecar 不记。 | :31 |
| `after_invoke` | 先 `_run_feedback_loop`（对本轮 presented 的经验取对话片段做 LLM 判定、回写 times_presented/used、重算——片段从 `ctx.agent._messages` 取，因 AFTER_INVOKE 时 `ctx.inputs` 是 `InvokeInputs`、**无 messages 字段**），再 `_run_evolution`（调 `orchestrator.evolve_all`：detector 只跑一次、按 `skill_name` 分发信号给各 skill）。末尾清空 `_presented_ids_by_skill`。 | :60 |

### 经验的渐进加载（三层按需）

经验不是一次全量灌进上下文，而是**按需逐层加载**——核心是控上下文成本：经验库膨胀到几十条也不撑爆窗口，正文只在模型真要用某条时才加载那一条。

| 层 | 何时触发 | 注入什么 | 上下文归宿 |
|---|---|---|---|
| **L0a·清单** | 每步自动（`SkillHook` `before_invoke`） | skill name+desc | `frozen_sections` → 前缀（跨步稳定，命中 cache） |
| **L0b·呈现** | 模型调 `read_skill(skill,"SKILL.md")` 时（`SkillEvolutionHook` `after_tool_call`） | 记该 skill 全部 non-skip 经验为 presented（**不进上下文**，只记 id 供反馈环） | hook 内部 `_presented_ids_by_skill` |
| **L1·目录** | 同一次 `read_skill(SKILL.md)`（`skill_tools.py:25`） | 整份 SKILL.md **原样**（不剥块、不截断），含索引块——但索引块只有摘要行 + 锚点链接，**无正文** | `tool_result` → history |
| **L2·正文** | 模型再调 `read_skill(skill, "evolution/<section>.md")` | 单条经验正文全文（sidecar 文件，`store.py:198` 写） | `tool_result` → history |
| **L2·脚本** | 模型再调 `read_skill(skill, "evolution/scripts/<file>")` | 脚本工件源码 | `tool_result` → history |

读法是**按需逐层**：

```
L0a 清单（每步自动·前缀，很轻）
  ↓ 模型决定要用某 skill
L1 read_skill(SKILL.md) → 看到索引块（目录页：哪条经验、分多少、锚点 [→]）
    同时 L0b 记该 skill 经验 presented（不进上下文，只供反馈环）
  ↓ 模型点开某条有用的
L2 read_skill(evolution/Troubleshooting.md) → 那一条正文全文（详情页）
```

关键设计点：

- **经验不进 system message**：旧实现的 `before_model_call` 每步遍历所有 skill 把 top-3 摘要 prepend 到 messages——这会让所有 skill 的 times_presented 每步虚涨（哪怕模型这步没碰它们），U=used/presented 分母失真，且 N×3×150 字每步浪费上下文。已改为 `after_tool_call` 监听 `read_skill`：模型真加载某 skill 才记其经验 presented，**presented 计数真实**（不读不涨），与 jiuwenswarm 的 `ExperienceTracker.record_presented`（"a non-rail presentation path displayed" 时才记）对齐。
- **`read_skill` 默认只读 SKILL.md，不跟随链接**（`skill_tools.py:38` 一行 `read_text` 原样返回）。索引块里的 `[→](evolution/Troubleshooting.md#ev_xxx)` 只是 markdown 文本，不会触发自动读 sidecar——要正文，模型得**显式再调一次 `read_skill` 传 sidecar 路径**。这是"目录页/详情页"分离，避免一读 SKILL.md 就把全部经验正文拖进来。
- **L0a 进前缀、L0b 不进上下文**：清单是跨步稳定的 → 进 `frozen_sections` 前缀命中 cache；presented 只是 hook 内部的事件记录（`_presented_ids_by_skill` dict），既不进前缀也不进 history messages——所以经验呈现**完全不占上下文、不扰 cache**。

**触发点**由 config `evolution.trigger` 控制（默认 `after_invoke`，可选 `after_tool_call` / `after_model_call` / `none`）。`evolution.enabled=false` 时 `SkillEvolutionHook` **根本不注册**（在 `server.py` 里条件注册），整条进化链路零开销——但 `SkillHook`（清单注入）和 `read_skill` 工具**始终在**，不依赖进化开关：没开进化时，skill 仍是普通的"清单 → 按需读 body"两段式，只是没有 presented 记录和索引块。这是"opt-in 保守"的体现：进化会改用户的 skill，默认关着，要的人自己开。

进程级单例 `get_orchestrator()`（[`evolution/__init__.py`](../../twinkle/agentserver/evolution/__init__.py) :31）惰性构造，把 store + optimizer + scorer + detector 装配好。**optimizer 和 scorer 共用同一个 `LLMClient`**（和 agent 主循环同模型），不 per-component 各开一个——精简。

---

## Part 5. 怎么手动操作（RPC + 审批门）

### 5.1 审批门——"不静默写改"

生成的经验**默认不直接落盘**，先 stage，按 `auto_save` 决定停不停下等人批（[`orchestrator.py`](../../twinkle/agentserver/evolution/orchestrator.py) `evolve()` :47）：

1. **守卫**：skill 的 SKILL.md 不存在 → `skipped_skill_not_found`；
2. **信号检测**：signals 为 None 自动调 detector，只留归因到当前 skill 的；无信号 → `no_signals`；
3. 读 SKILL.md + 已有经验（去重用）；
4. LLM 生成；无记录 → `no_records`；
5. **分支**：
   - `auto_save=False`（默认）→ 塞进内存 `_pending[skill]`，返回 `staged`，**停下等人批**；
   - `auto_save=True` → `_commit` 直接落盘 + 重渲染索引块，返回 `auto_approved`。

`_pending` 是**进程内 dict，v1 不持久化**（:43，重启丢待批——故意的简化，见 Part 7）。`approve`/`reject` 按 `record_ids` 精细选择（None=全批/全拒）。**"不静默写改"防 LLM 误判直接改坏 skill**——这是第二条保险。

### 5.2 六个 RPC

经 [`skills/rpc.py`](../../twinkle/agentserver/skills/rpc.py) 暴露，分内联（yield 单帧）和非内联（后台 `create_task`，完成发一帧）：

| RPC | 作用 | 模式 |
|---|---|---|
| `skills.evolve` | 手动触发一个 skill 进化（传 messages + skill content） | 后台 |
| `skills.evolve_list` | 查某 skill 经验与分数（按分降序 top-50） | 内联（只读） |
| `skills.evolve_pending` | 查待批列表（可按 skill 过滤） | 内联（只读） |
| `skills.evolve_approve` | 批准 pending（`record_ids` 可选，None=全批） | 后台 |
| `skills.evolve_reject` | 拒绝 pending | 后台 |
| `skills.evolve_simplify` | 蒸馏清理 | 后台 |

### 5.3 蒸馏——淘汰低质

经验库会膨胀，定期蒸馏（`ExperienceScorer.simplify`，scorer.py :186）：

- **规则前置**：分 < `min_score`（默认 0.4）**且**零调用（used+pos+neg 全 0）→ 直接 `DELETE`，**不调 LLM**；
- 其余送 LLM 判 `DELETE/MERGE/REFINE/KEEP`。`orchestrator.simplify`（:199）执行 DELETE（移除+落盘+重渲染），MERGE/REFINE 建议 v1 只返回不自动执行（审批门控）。

> 有意思的闭环细节：一条从没被用过的经验，靠**新鲜度衰减**自己就会掉进蒸馏线——F 在 90 天从 1.0 衰到 0.5，未使用记录的 E=0.5/U=0，分数 `0.5·0.5+0.3·0+0.2·F` 在 90 天时 ≈ 0.35 < 0.4，且零调用 → 被 DELETE。**低质经验会自动过期**，不用人清。

---

## Part 6. 怎么自己跑起来看效果

```yaml
# twinkle/resources/config.yaml
evolution:
  enabled: true              # 总开关（默认 false，opt-in）
  trigger: after_invoke      # 触发点
  auto_save: true            # 想看闭环自动跑就开；想体验审批门就 false
  signals:
    execution_failure: true
    script_artifact: true
    user_intent: true
```

前提：`.env` 里设好 `TWINKLE_LLM_API_KEY`（optimizer/scorer 都要调 LLM，没 key 会在 model call 时失败）。

然后跑一个会触发失败的对话（比如故意让 agent 用一个没装依赖的 skill），对话结束后去看 `skills/<skill>/`：会多出 `evolutions.json` + `evolution/` 目录 + SKILL.md 末尾的索引块。再用 `skills.evolve_list` RPC 看分数。

想看闭环分数变化：同一个 skill 多用几轮，观察 `usage_stats` 的 `times_presented/used` 累积、`score` 从种子分（0.65）变到 calc_score 重算值。

---

## Part 7. 为什么这么设计（那些"看起来奇怪"的点）

理解一个模块，最后要落到"为什么"。这些是抄 jiuwenswarm 骨架时做的取舍，每条都有理由：

1. **信号检测零 LLM**：每次对话都跑，调 LLM 又贵又会错归因。正则+路径匹配便宜可复现。**只在②生成和⑤判定这两处花 LLM**。

2. **审批门默认开（`auto_save=False`）**：LLM 会误判，直接改用户的 skill 太危险。默认 stage 等人批，`auto_save=True` 才自动落。这是"不静默写改"。

3. **pending 存内存不持久化**：v1 简化，重启丢待批。jiuwenswarm 会持久化快照，Twinkle 砍了——待批本来就是临时态，丢了重跑一轮进化就有，不值得为它加存储层。

4. **去掉 `applied` 字段**：原版里它是摆设（唯一能置 True 的函数没人调）。Twinkle 以"渲染进索引块"为生效事实，不靠这个字段。

5. **呈现计数在反馈环节点落盘（不在呈现时）**：呈现时（`after_tool_call`）只记内存 id、不改盘上对象；presented 在反馈环从 store fresh 读后 `+1` 再 save，避免呈现层只改临时对象导致 presented 恒 0 让 U 失效。

6. **数量上限文本≤2/脚本≤1 + 去重**：防经验库一轮膨胀。重复→merge 改写、相似→skip。

7. **版本对齐 ×0.7 惩罚**：经验带 `skill_version`，skill 改版后旧经验自动降权，避免过时经验误导。

8. **JSON 契约 prompt 硬编码不进 config**：生成/评估/蒸馏三个 prompt 都有严格 JSON 输出契约，放 config 用户改坏 → 解析失败 → 静默失效。所以硬编码进模块常量。

9. **可观测走 instrumentor 不 inline**：OTel span 不写进 hook/业务代码，走 [`observability/instrumentors/evolution.py`](../../twinkle/observability/instrumentors/evolution.py) monkey-patch `evolve`。对齐项目可观测约定。

**六道保险**兜住效果：①真闭环（非一次写入）②数量上限+去重 ③蒸馏淘汰 ④不静默 ⑤规则归因 ⑥版本对齐。

---

## 速查：源文件索引

| 你想看 | 去这个文件 |
|---|---|
| 数据模型（Record/Patch/UsageStats/Signal + 种子分 + 失败关键词） | [`evolution/types.py`](../../twinkle/agentserver/evolution/types.py) |
| 信号检测（规则，零 LLM，归因） | [`evolution/signal_detector.py`](../../twinkle/agentserver/evolution/signal_detector.py) |
| LLM 生成经验（prompt + 上限 + 去重） | [`evolution/optimizer.py`](../../twinkle/agentserver/evolution/optimizer.py) |
| 存储 + 固化（json + 索引块 + sidecar + 原子写） | [`evolution/store.py`](../../twinkle/agentserver/evolution/store.py) |
| E/U/F 打分 + 反馈环判定 + 蒸馏 | [`evolution/scorer.py`](../../twinkle/agentserver/evolution/scorer.py) |
| 编排器（evolve/approve/reject/feedback/simplify） | [`evolution/orchestrator.py`](../../twinkle/agentserver/evolution/orchestrator.py) |
| 进程单例 + 组件装配 | [`evolution/__init__.py`](../../twinkle/agentserver/evolution/__init__.py) |
| 接线层·清单注入（skill name+desc → 前缀） | [`hooks/builtin/skill_hook.py`](../../twinkle/agentserver/hooks/builtin/skill_hook.py) |
| 接线层·经验呈现记录 + 触发 | [`hooks/builtin/evolution_hook.py`](../../twinkle/agentserver/hooks/builtin/evolution_hook.py) |
| 按需读 SKILL.md / sidecar / 脚本（渐进加载 L1/L2） | [`tools/builtin/skill_tools.py`](../../twinkle/agentserver/tools/builtin/skill_tools.py) |
| 6 个进化 RPC | [`skills/rpc.py`](../../twinkle/agentserver/skills/rpc.py) |
| 反馈环 times_presented 落盘回归测试 | [`tests/test_evolution_orchestrator.py`](../../tests/test_evolution_orchestrator.py) |
| 配置 schema（`EvolutionConfig`） | [`config/schema.py`](../../twinkle/config/schema.py) |

> 想查精确的字段表/配置默认值/与 jiuwenswarm 的逐点裁剪，去 [skill-self-evolution-design.md](skill-self-evolution-design.md)——这份 walkthrough 是带你进门，那份是案头参考。

---

## 常见问题（Q&A）

读者最容易卡住的问题记在这里，后续有新问题往这补。

### Q1. 怎么判断新增的经验有没有被使用？使用了有没有效果怎么判断？

**核心：两者都是 LLM 语义判定，不是机械追踪。** 系统不追踪"agent 是否真的执行了这条建议"——没有这种硬证据。

**判定在哪**：`scorer.py` 的 `evaluate()`（:137），由 `evolution_hook.py:71` 的 `_run_feedback_loop` 触发。流程：

1. 模型 `read_skill(skill,"SKILL.md")` 时 `after_tool_call` 记该 skill 全部 non-skip 经验 id 进 `_presented_ids_by_skill[skill]`（:31）；
2. agent 跑完这一轮 ReAct（调工具、回话）；
3. `after_invoke` 取"最后 ~10 条消息、~3000 字"作 snippet（hook:90-94）；
4. `evaluate` 把「经验内容 + 这段对话」喂给 LLM，逐条输出 `{record_id, used, positive, negative, reason}`（prompt 见 `EXPERIENCE_EVAL_PROMPT` :30）；
5. `update_score`（:170）消费三个布尔，自增 `times_used/positive/negative`，重算分。

**判据全在 prompt 里**（没有别的机制）：

| 字段 | prompt 怎么问 | 本质 |
|---|---|---|
| `used` | 该经验是否被 Agent 理解和采纳（内容被用于指导后续行为） | 后面行为看起来像不像照经验做 |
| `positive` | 帮助解决了问题或改进了输出 | 用了之后结果变好 |
| `negative` | 导致错误或误导 | 用了之后结果变差/被带偏 |

三者不互斥，各自独立 +1。一条经验可以 used=true 同时 positive=true（采纳且帮上忙），也可以 used=true 而 negative=true（采纳了但反而搞砸）。

**已知局限（按严重度）**：

1. **相关性 ≠ 因果性**：不追踪 agent 是否真执行了建议（比如有没有真跑 `pip install`）。LLM 只看"后面行为像不像照经验做"——agent 本来就会的事也可能被算成 used（假阳）。
2. **snippet 只取 tail**：经验在对话前段被用、后段聊别的 → tail 看不到使用痕迹 → 漏判 used=false。长对话尤其严重。
3. **单次判定无重试**：`evaluate` 解析失败直接 `except → return []`（:166），不像 `optimizer._generate_drafts_with_retries` 有 2 轮重试。一次 LLM 抽风就丢一条反馈。
4. **U 的分母 presented 在反馈环节点落盘**（呈现时只记内存）：落盘点错位会让 presented 恒 0、`calc_utilization` 永走 0.5 兜底 → U 维失效。

**为什么明知不完美还这么设计**：YAGNI——机械追踪需把经验里的具体动作对齐到 agent 的工具调用参数（语义对齐本身就难、易错）。改用便宜的 LLM 判定 × 多轮累积 × 贝叶斯平滑 `(pos+1)/(pos+neg+2)`，靠量纠偏单次噪声；fail-soft：判错或解析失败只是这轮没反馈、不崩。本质是"便宜的语义猜测 × 多轮 × 平滑"逼近真相，承认单次会错、靠趋势纠偏，不是精确计量。

> 最值得加固的点：把 snippet 从"最后 10 条"改成"呈现点之后的全部 trace"——能直接堵住局限 2（长对话漏判）。当前未做，属可识别的已知缺口。
