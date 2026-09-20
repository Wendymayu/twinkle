# Memory 召回锚点恢复：chunk 按节切 + 返回带日期锚点设计

> 日期：2026-09-20
> 状态：设计已与用户确认，待写实现计划
> 评测批次：`data-sets/locomo_twinkle/output/2026-09-20_125942/conv-26`（mean F1 0.578，9/37 全对）
> 关联记忆：[[memory-store-index-debounce]] [[memory-dedup-gap]] [[locomo-077-missing-deps-not-capability]] [[dashscope-embed-broken-silent-degrade]]

## 1. 背景与诊断

### 1.1 评测事实

conv-26 cat2（单跳事实）37 题，mean F1 **0.578**，9 题全对、28 题错。逐题读 raw 证据后，**28 道错题里约 20 题 twinkle 实际召回了正确事实**，部分甚至几乎逐字答对：

| 题号 | F1 | raw 里 twinkle 实际答的 | gold | 真正丢分原因 |
|---|---|---|---|---|
| #74 | 0.18 | "road trip the weekend before October 20, 2023" | The weekend before 20 October 2023 | 几乎逐字答对，被 harness fallback 整段抽答稀释 |
| #68 | 0.15 | "into art for seven years...started around 2016" | Since 2016 | 答对了，被加粗全收稀释 |
| #53 | 0.27 | raw 开头"applied...during the week of...August 23" | The week of 23 August 2023 | 开头答对，列了 8 个加粗 timeline 日期被 harness 全收 |
| #33/#36/#54/#57 | 0.25~0.35 | "late June"/"mid-July"/"mid-August" | The week/X before Y | 语义对、**形式不贴 gold 相对表述** |

低分三层归因（按题数）：
1. **gold 相对表述天花板**（~15 题，不可修）：locomo cat2 gold 用 `"the X before Y"` 相对日期，任何绝对日期答案 F1 天花板 0.3–0.5。数据集特性，非 twinkle/harness 问题。
2. **harness `extract_answer` 抽答稀释**（~6 题，可修但**非记忆模块**）：加粗全收 + 无加粗 fallback 取末行整段，把答对的 #74 从 ~0.9 毁到 0.18。
3. **记忆模块侧**（少数题，本 spec 重点）：召回多日期不收敛(#53/#57)、**chunk 切分切断日期锚点导致相对日期丢精度**(#33/#36/#54/#57)、专有名词未保真(#26)、语言不一致(#73)。

**本 spec 聚焦第 3 层**（用户选定范围"只深挖记忆模块召回"）。诚实结论：记忆召回在工作，硬优化召回质量受 gold 天花板约束，记忆模块侧改进现实收益预期 +0.05~0.15。

### 1.2 实证根因：chunk 切分切断日期锚点

直接查 `memory.db`（conv-26/memory/memory.db）验证了真根因——**chunk 粒度其实不粗，但切断"对话日期标题 + 事实"关联**：

- MEMORY.md 被切成 **55 个 chunk**，每个仅 ~5 行 / 800–1100 字符（`store.py:462-484` `_chunk` 按 `chunk_tokens=256`×3 字符 budget 行切）。
- #53 答案事实"applied to adoption agencies this week"在 `L122-127` chunk，但对话日期标题 `## 2023-08-23 — Conversation with Melanie` 在 `L118-122` chunk（**前一个**）：

```
L118-122 chunk 末尾: ...## 2023-08-23 — Conversation with Melanie
                    - Caroline applied to adoption agencies this week...
L122-127 chunk 开头: - Caroline applied to adoption agencies this week... (无标题!)
```

- `chunk_overlap=32` token（~96 字符）不够保证标题随事实行进入下一 chunk。memory_search 返回 `### {path} (score) \n {text}`（`memory_tools.py:19-22`）只给单 chunk text，**无文件级锚点** → agent 召回到正确事实但丢失"这是 8 月 23 日对话里说的"定位 → 把"this week"组织成粗略绝对日期或回溯 timeline，不贴 gold 的"the week of Aug 23"。

**注意**：MEMORY.md 原文（205 行）每个 `## YYYY-MM-DD` 节都**有**标题 + 相对时间表述——**ingest 保真其实 OK**，问题在召回侧 chunk 切分丢锚点。dreaming 只删行不重写（`dreaming.py:31` 硬约束），不背锅。

## 2. 核心决策（与用户确认）

1. **方案 A + B 组合**：A（返回带日期锚点，快赢）+ B（chunk 按节切，治本）。用户选定。
2. **不动 chunk 切分以外的索引逻辑**：FTS/vec 融合权重（`vector_weight=0.7`/`text_weight=0.3`）不变，`_hit` 形状只加字段不改语义。
3. **不做 rerank / ingest 改 hook / harness 抽答**（见 §7 YAGNI；语言自适应见 §4.4 方案 E）。
4. **重索引走手动删 `memory.db`**（评测场景），不加 `_CHUNK_LOGIC_VERSION` 自动迁移（YAGNI）。
5. **方案 E：记忆模块 prompt/返回语言自适应**（用户纠正：记忆模块要适应不同语言，不应硬编码中文）。`memory_search` 返回头跟随 query 语言；`memory_hook` 策略 prompt 加"作答跟随提问语言"约束。治 #73。

## 3. 方案选择

| 方案 | 机制 | 评价 |
|---|---|---|
| **A（选）** | memory_search 返回 chunk 时附"所属对话日期锚点"（解析行号→最近 `## YYYY-MM-DD` 标题） | 最小改动、治实证根因、不重索引、可快赢验证 |
| **B（选）** | `_chunk` 按 `## ` 节边界切（不跨节，标题行是某 chunk 首行） | 治本，但改核心切分 + 重索引；A 的升级版 |
| C（弃） | 召回后 LLM/cross-encoder rerank 选 top-1 | #53 实证召回已对（开头答对），rerank 收益存疑 + 增延迟/LLM 调用 |
| D（弃） | ingest 改 hook 双写绝对日期 | 原文已有锚点，问题在召回侧，改 hook 只影响新写 |
| **E（选）** | `memory_search` 返回头跟随 query 语言（CJK 检测）+ `memory_hook` 策略 prompt 加作答语言约束 | 治 #73 中文作答英文 gold；记忆模块硬编码中文 prompt/返回头混入英文数据 context 诱导中文作答 |

**为何 A+B+E**：A 立刻给所有 chunk（含节内后续 chunk）补锚点；B 减少跨节、让节首 chunk 自带标题；E 让记忆模块不诱导作答语言。三者正交互补。simplicity first：A 是返回层不动索引，B 是索引层治本，E 是 prompt/返回模板语言自适应。

## 4. 组件改动

### 4.1 `store.py` — chunk 按节切分（B）

**`_chunk`（`store.py:462-484`）改为按 `## ` 节边界切**：

现状：按行累积到 `budget` 切 + overlap 回退。

改后逻辑：
- 扫描行累积时，**遇到 `^## ` 行且非当前 chunk 首行 → 截断 flush**（不跨节），新 chunk 从 `## ` 行起。
- 节内仍按 `budget` 切；**节边界不做 overlap**（避免跨节，节内 chunk 间仍 overlap 但不越过下一个 `## `）。
- 无 `## ` 节的文件（USER.md 是 `# User Profile` + `-` 列表；单日 daily 若无 `## `）走**原逻辑**（含 overlap），noop。

伪码：
```python
def _chunk(self, content):
    lines = content.splitlines()
    if not lines: return []
    budget = max(1, self._chunk_tokens) * 3
    out = []
    i, n = 0, len(lines)
    while i < n:
        size, j = 0, i
        while j < n and size < budget:
            # 节边界:遇到 ## 且非 chunk 首行 → 截断
            if j > i and lines[j].lstrip().startswith("## "):
                break
            size += len(lines[j]) + 1
            j += 1
        out.append(Chunk(i+1, j, "\n".join(lines[i:j])))
        if j >= n: break
        # overlap 回退,但不越过下一个 ## 节(回退终点 > 当前 chunk 末尾的下一个 ##)
        i = self._next_start(lines, j, overlap_bytes)  # 回退到 j 之前,不跨 ##
    return out
```

**边界**：节标题行强制成为某 chunk 首行，不会落在 chunk 中间被切断。

### 4.2 `store.py` — 返回带日期锚点（A）

**`_hit`（`store.py:668-674`）增加 `section_title` 字段**：
```python
return {"path": row["path"], "score": round(score, 4),
        "text": row["text"], "start_line": row["start_line"],
        "end_line": row["end_line"],
        "section_title": section_title}  # 新增
```

**新增 `_section_title_for(path, start_line) -> str`**：
- 读文件全文 → `splitlines`
- 从 `start_line-1`（0-indexed）向上扫，返回首个 `lstrip().startswith("## ")` 的 `strip()` 行
- 无 `## ` 行的 fallback：
  - `path.startswith("daily_memory/")` → 文件名日期（`daily_memory/2023-08-23.md` → `"2023-08-23"`）—— daily 文件名即日期锚点
  - `path == "USER.md"` → `"User Profile"`
  - 其他 → `""`

**`search`（`store.py:599-643`）按 path 分组缓存**：本次 search 的所有 hit 按 `path` 分组，每 `path` 读一次文件文本缓存 `{path: lines}`，所有 hit 共用调 `_section_title_for`。避免每 hit 一次读盘。

### 4.3 `memory_tools.py` — 返回格式加锚点

**`memory_search`（`memory_tools.py:12-22`）返回头加 `[§ {section_title}]`**：
```python
lines = [f"## 记忆召回 ({len(hits)} 条)"]
for h in hits:
    st = h.get("section_title", "")
    anchor = f" [§ {st}]" if st else ""
    lines.append(f"### {h['path']} L{h['start_line']}-{h['end_line']}{anchor} (score {h['score']})\n{h['text']}")
return "\n\n".join(lines)
```

效果：agent 看到的是 `### MEMORY.md L122-127 [§ 2023-08-23 — Conversation with Melanie] (score 0.82)` → 能把"this week"锚定到 8/23 → 作答"the week of Aug 23"贴 gold。

### 4.4 `memory_tools.py` / `memory_hook.py` — 语言自适应（E）

**根因**：`memory_tools.py:19` 返回头 `## 记忆召回 (N 条)` 硬编码中文；`memory_hook.py:13-27` `_PROMPT_TEMPLATE` 硬编码中文策略。英文 locomo 评测时，这些中文混入英文数据 context，与英文 quiz question 并存，诱导 agent 中文作答（#73 pred=`2023 年 9 月` vs gold=`September 2023`）。

**`memory_search` 返回头跟随 query 语言**（`memory_tools.py:19`）：
- query 含 CJK 字符（`re.search(r'[一-鿿]', query)`）→ 中文头 `## 记忆召回 (N 条)`
- 否则 → 英文头 `## Memory Recall (N hits)`
- `### {path} ...` 行本身无语言词（path/score/§/L 符号），不区分。
- 抽 `_query_header(query, n)` helper 复用。

**`memory_hook _PROMPT_TEMPLATE`（`memory_hook.py:13-27`）加作答语言约束**：
- 策略 prompt 末尾加一句："**作答语言**：用**用户提问的语言**作答（英文问→英文答，中文问→中文答），不要固定用某种语言。"
- 保留中文策略本体（Twinkle 主用户中文，中文策略让中文用户可读；agent 能执行中文指令）。不把策略 prompt 改英文——那会牺牲中文用户可读性，且约束作答语言已足够治 #73。

**`dreaming _CONSOLIDATE_PROMPT`（`dreaming.py:21-39`）**：不改。它只输出 JSON 行号（`{"injectious":[...],"redundant":[...]}`），不产出进 agent context 的正文记忆，语言影响可忽略。

**双语支持（同时服务中文与英文用户）**：
- 返回头跟随 query 语言自适应——中文用户问→`## 记忆召回 (N 条)`，英文用户问→`## Memory Recall (N hits)`。
- 策略 prompt 保留中文：agent 能执行中文指令（多语言模型），且策略 prompt 注入 system prefix、**用户不直接看**，英文用户不会"看到"中文策略；加"作答跟随提问语言"约束后，英文用户提问即得英文作答、中文用户提问即得中文作答。
- 故无需把策略 prompt 改英文（E2）——那只在 E1 验证后 #73 类问题仍存时才升级。E1 是最小双语自适应。

## 5. 重索引与迁移

- 改 `_chunk` 后旧 `memory.db` 的 chunks 是旧切分（55 chunk 跨节版）→ **删 `memory.db` 重建**：
  - 评测场景：直接 `rm memory.db`，下次 MemoryManager 初始化重建 + watchdog 标 dirty + `_flush_dirty` 重索引。
  - 生产侧：同上；后续可加 `_CHUNK_LOGIC_VERSION` meta 戳触发自动清（**本期不做**，YAGNI）。
- 重索引后 MEMORY.md chunk 数预期变化（节首对齐，可能略增），USER.md 不变（无 `## `）。

## 6. 测试（对齐 CLAUDE.md `asyncio.run()` + `free_port` 惯例）

### 6.1 单元测试
- **`_chunk` 按节切**：构造含多个 `## ` 标题的 content，断言每个 chunk 要么首行是 `## `、要么不跨 `## `（无 chunk 的 `[start_line, end_line]` 跨越 `## ` 标题行）。
- **`_chunk` 无 `## ` 文件走原逻辑**：USER.md 风格 content 切分结果与改动前等价（含 overlap）。
- **`_section_title_for`**：MEMORY.md 中间行返回最近 `## ` 标题；daily 路径返回文件名日期；USER.md 返回 `"User Profile"`；首行无前置 `## ` 返回 fallback。
- **`search` 返回含 `section_title`**：建小 memory，search 后断言 hit dict 有非空 `section_title`（对 MEMORY.md）。
- **按 path 缓存**：mock `_section_title_for` 读盘次数 ≤ path 数（非 hit 数）。
- **返回头语言自适应**：英文 query → `## Memory Recall`；中文 query → `## 记忆召回`；`_query_header` CJK 检测正确。
- **memory_hook prompt 含作答语言约束句**：断言 `_build_prompt()` 输出含"作答语言"约束文本。

### 6.2 评测回归
- 删 conv-26 `memory.db` 重建 → 重跑 conv-26 cat2 评测 → 断言"相对日期丢精度"类题（#33/#36/#54/#57/#8/#21/#31/#35/#41/#45/#67/#5/#28/#29）mean F1 提升（目标 +0.05~0.15）。
- 现有 memory 测试套件全绿（FTS/vec 无回归）。

## 7. 不做（YAGNI / 范围外）

- **rerank**（方案 C）：#53 实证召回已对，收益存疑 + 增延迟。
- **ingest 改 hook 双写绝对日期**（方案 D）：MEMORY.md 原文已有 `## ` 标题 + 相对时间，问题在召回侧 chunk；改 `memory_hook.py:24` 引导只影响新写，不治已存。
- **harness `extract_answer` 抽答稀释**（#74 等 ~6 题）：非记忆模块，用户明确范围外。
- **`_CHUNK_LOGIC_VERSION` 自动迁移**：手动删 db 即可，加版本号是过度设计。
- **专有名词保真**（#26 书名 "Nothing is Impossible" 未存）：ingest 侧 write_memory 时 agent 未写，属 hook 引导另题，本 spec 不覆盖。

## 8. 风险与边界

- **`section_title` 解析读盘**：每次 search 每 path 读一次（按 path 分组缓存）。memory_search 典型 10 hits 跨 1-2 文件 → 1-2 次读盘，可接受。若后续 daily 增多可加 mtime 缓存（本期不做）。
- **chunk 切分逻辑变 → rowid/FTS/vec 索引内容变**：删 db 重建，已覆盖（§5）。
- **USER.md 无 `## ` 节**：B noop、A 返回 `"User Profile"`（无害锚点，不丢信息）。
- **daily 文件无 `## ` 标题但有文件名日期**：A 用文件名作锚点（额外收益，daily 召回也能锚定日期）。
- **gold 天花板仍在**：本 spec 提升"召回可用性"（agent 拿到锚点能贴 gold 形式），但 gold 相对表述的 ~15 题天花板不会消失，mean F1 到不了 0.9。
