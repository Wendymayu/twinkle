# Dreaming 重做设计 — 参考 openclaw 的 daily→MEMORY.md 整合模型

> 状态：**已落地**（2026-08-17，T1–T10 全 GREEN，`tests/test_dreaming.py` 29 passed / 全量 877 passed / 16 pre-existing fail）。§4 的旧「方案 C LLM 合并」描述已由 `docs/design/memory-b-scheme-design.md` §4 更新为本文 B 模型（promote + consolidate-delete）。
>
> **先读 §0**——用大白话 + 例子回答"怎么工作"。§1–§19 是给实现的详细规范，想看细节再翻。

## 0. 怎么工作（先读这节）

这节只回答四个问题：**何时触发、搬哪些文件、怎么提取、怎么重构 MEMORY.md**。不涉及实现细节。

### ① 何时触发 dreaming

定时任务，后台跑，agent 不阻塞：

- 服务启动后等 `start_delay_seconds`（默认 5 分钟），然后每 `interval_seconds`（默认 1 小时）跑一次 `dream()`。
- 跑之前过两道门，**同时过才真跑**：
  1. config 里 `memory.dreaming.enabled` 开着（默认**关**，要手动开）。
  2. 当前没有 agent 在跑（`inflight == 0`）——agent 空闲时才整理，免得和 agent 抢着写 MEMORY.md。
- 任一道不过 → 这次跳过，下个小时再来。
- 整个过程在一个后台 asyncio task 里，挂在 `server.py` 启动时。

### ② dreaming 时把哪些文件的内容写到 MEMORY.md

- **扫描源**：`daily_memory/` 下所有 `YYYY-MM-DD.md`（每天的日常记忆文件）。
- **写入目标**：`MEMORY.md`（长期记忆）。
- **不碰** `USER.md`（用户档案，稳定，不参与整理）。
- 一句话：把 daily 里**够格**的内容搬到 MEMORY.md。daily 文件本身只读不动（它是 append-only 日志，不做修改）。

### ③ 如何提取文件内容

零 LLM，纯程序判断：

1. 读每个 daily 文件，取**所有非空行**。每行 = 一个"事实候选"（claim）。
2. 给每行算指纹：`md5(去掉首尾空白的行)`。同一行文字（哪怕在不同文件里）= 同一个指纹。
3. 跨文件聚合：同一个指纹出现在几个**不同的 daily 文件**里，记下这个集合 `source_files`。
4. 门槛：只留"在 ≥ `min_distinct_files`（默认 2）个不同 daily 文件里出现过"的候选——意思是一条事实在不同天被写了 ≥2 遍 = 反复出现 = 值得长期记；一次性的事（只写过 1 遍）不搬。并且这个指纹不能已经在 sidecar 的"已晋升"集合里（没搬过）。
5. 通过门槛的候选，逐条 append 到 MEMORY.md 末尾。同时在 sidecar `dreaming_state.json` 记下这个指纹，下次不再重复搬。

### ④ 如何重构 MEMORY.md

搬完之后，对整个 MEMORY.md 做一次 LLM 整合（去重 + 消矛盾）。**这步只在有新候选被搬进来之后才跑；这次没搬新内容就跳过，等下个 interval。**

1. 程序把 MEMORY.md 当前所有非空行**编号**（1、2、3…），整包发给 LLM。
2. LLM 只做一件事：**指出哪些行该删**。规则——语义重复的（同一事实不同措辞）删冗余、留更完整那条；矛盾的（同一属性不同值，如"用 Windows" vs "用 Mac"）删旧值、留更后写那条。输出 `{"delete":[行号]}`。LLM 不改写任何原文、不新增内容。
3. 程序校验：删的行数 ≤ 总行数的 25%（防 LLM 误删太多）。**过了才执行**；没过、或 LLM 报错、或 JSON 解析失败 → 跳过这次整合，保留刚才 append 后的版本（fail-soft，不丢数据）。
4. 通过则原子重写 MEMORY.md：去掉被删的行，其余**逐字保留**。
5. 另外，如果 MEMORY.md 太长（超 `max_memory_chars` 默认 10000 字符），按时间丢最早的提升行（compact）。

> 为什么用"删行号"而不是让 LLM 重写？因为晋升步（③）已经把新内容 append 进去了，整合步只需要**删冗余/矛盾的旧行**，LLM 任务从"既加又合"降成"只删"——更安全（LLM 全程不碰文本原文），也更便宜。

### 一个具体例子

`daily_memory/` 里有：
```
2026-08-14.md:  - 喜欢爬山运动
                - 今天吃了火锅
2026-08-15.md:  - 喜欢爬山运动
```

跑一次 `dream()`：

- `- 喜欢爬山运动` 的指纹出现在 2 个文件 ≥2 → 够格 → 搬进 MEMORY.md，sidecar 记下指纹。
- `- 今天吃了火锅` 只在 1 个文件 <2 → 不够格 → 不搬（还在 daily 里，没丢）。

搬完后，假设 MEMORY.md 里现在是：
```
1: - 用 Windows 系统
2: - 用 Windows          ← 和第 1 行语义重复
3: - 喜欢爬山运动        ← 刚搬进来的
```
整合这步：LLM 指出删第 2 行（留更完整的第 1 行）→ MEMORY.md 原子重写成只剩第 1、3 行。下个 interval 再跑时，`- 喜欢爬山运动` 的指纹已在 sidecar 里 → 不重复搬。

---

## 1. 背景与目标

用户判定当前 dreaming「真的很烂」，要求参考 `D:\code\opensource\github\openclaw` 的 dreaming 机制重做，目标明确：

- **「从日常记忆整理内容到 MEMORY.md」** —— daily → MEMORY.md 的 promotion + 整合路径。
- **「不走写入时合并」** —— 守 B-scheme：写入路径零 LLM，去重/整合在后台 dreaming 传递里做。
- **完整 openclaw Deep 模型（裁剪版）** —— 含 consolidation LLM（语义合并）+ 验证 + append-only 回退。

## 2. 现状诊断（当前 `memory/dreaming.py`）

| 病 | 现状 |
|---|---|
| N² pairwise LLM | `_dedupe_and_resolve` 对 MEMORY.md 每条 `mgr.search` 召回 + LLM 逐对判 redundant/merge/conflicting/independent。昂贵且短视（局部判不了 merge）。 |
| 每段落 LLM 抽取 | `_harvest_daily` 对每个 daily 非空行 LLM `_EXTRACT_PROMPT` 抽事实。LLM 成本焊进每段。 |
| 无工作追踪 | 每 tick 全量重跑，N² LLM 重复烧。 |
| 子串匹配脆弱 | `_already_in_memory` 用 `entry in chunk` 判已存在。 |
| merge 死分支 | `merge` 首版当 independent（测试不覆盖，等于不做）。 |

## 3. openclaw 机制参考（`docs/concepts/dreaming.md` + 源码追踪）

- **3 阶段**（Light/REM/Deep），Deep 写 MEMORY.md。
- **中间短期 recall store**（JSON）攒证据：claim `{key, snippet, claimHash, recallCount, recallDays, sourcePath, promotedAt}`。
- **文件级摄入指纹**（mtime+size+lastDreamingDayIngested）跳未改文件（防重跑）。
- **claimHash 精确去重** + MEMORY.md 内 `<!-- openclaw-memory-promotion:{key} -->` marker。
- **确定性 6 信号加权评分**（relevance/frequency/query-diversity/recency/consolidation/conceptual）+ 阈值门 —— 够格候选才到 LLM。
- **单次 LLM consolidation**（按 projectKey 分组）：LLM 收 [当前 MEMORY.md + 带预计算 resultEntry 的候选] → 重写 + operations[{add/merge/supersede, resultEntry, priorEntries}]。**LLM 不创作文本**（resultEntry 代码预计算，逐字搬）。
- **严格验证**（旧条目丢失 ≤25%、resultEntry 字节相等、集合相等）→ 失败回退 append-only。
- **原子写**（tempfile+rename+fsync + expectedHash 乐观锁）+ **容量预算**（10000 字符，compact 丢最旧晋升段）+ **预镜像备份**。

## 4. 范围

本设计 = **完整 openclaw Deep 模型，裁到 Twinkle 单进程单用户 idle-only 尺寸**。一次性实现（不分阶段交付），dream() body 含两步：

- **晋升步（promote）**：daily → claimHash 去重 → 确定性门槛 → append + sidecar 记录。**零 LLM**。
- **整合步（consolidate）**：单次 LLM 出"删行号列表" → 验证 → 删冗余/矛盾行。**一次 LLM**。

**不在范围**（openclaw 生产级包袱，Twinkle 不需要）：
- expectedHash 乐观锁（Twinkle 单进程 idle-only 无并发外改）。
- 预镜像备份（单进程无并发，append-only + sidecar 已够安全）。
- 多项目 projectKey 分组（Twinkle 单 memory dir）。
- recall store 的 recallCount/recallDays/query-diversity（需 query log，Twinkle 无；用"跨 distinct daily 文件数"诚实代理 frequency）。
- Light/REM 阶段（Twinkle daily 是 append-only 自由行无标题，分块过度设计；直接非空行 = 候选）。

## 5. 适配取舍

| openclaw | Twinkle | 理由 |
|---|---|---|
| 标题分块 + 祖先上下文 | 非空行 = 候选（复用现有 `_nonempty_lines`） | daily 是 append-only 自由行无标题 |
| 文件级 mtime+size 摄入指纹 | 不做（每 tick 全量重扫 daily） | interval=3600s、daily 小文件少，重扫廉价 |
| recall store JSON | sidecar `dreaming_state.json`（只记 promoted 集） | 只需 idempotency + compact 年龄，不需攒证据 |
| 6 信号评分 | 1 信号：跨 distinct daily 文件数 | 无 query log，算不出 recall/diversity |
| add/merge/supersede structured ops + 150 行验证 | **删行号列表** + 删除比例验证 | 先 append 再 consolidate-delete，LLM 任务降维到"只删" |
| MEMORY.md marker | **sidecar `promoted` 集**（MEMORY.md 不挂 marker） | consolidation 会删行（含已晋升冗余行）；marker 会被删 → 下 tick 重晋。sidecar 只增不减，删了也不重晋 |
| 原子写 + 乐观锁 | `MemoryManager.replace`（tempfile+os.replace，无乐观锁） | 单进程无并发；原子防 crash 撕裂即可 |
| compact 丢最旧晋升段 | 同（按 sidecar ts 丢最老仍存在的 promotion 行） | append-only 保证 ts 序 |

## 6. 架构（wiring 全不动，只换 dream() body）

**保留不动**（守"不要大动"）：
- `DreamingOrchestrator` 类壳 + `__init__(llm, get_inflight)`。
- `run_loop`（start_delay + interval + fail-soft try-except）。
- `start_dreaming(llm, get_inflight)` 模块函数。
- `server.py:244` 调用点 `start_dreaming(llm, _get_inflight_count)`。
- `dream()` 门卫：`if not MEMORY_DREAMING_ENABLED or self.llm is None: return` + `if self._get_inflight() > 0: return`。
- A/B 组测试（门卫 + start_dreaming）不动。

> 注：B 的 consolidate 用 `self.llm`，故门卫的 `llm is None` 检查**有道理**（不是无意义 gate）。

**dream() body（门卫过后）**：

```
dream()  [门卫不动]
  1. claims = _scan_claims(mgr)
       # 扫 daily_memory/*.md 非空行 → md5(line.strip()) 精确去重
       # → {hash: {text, source_files:set, first_path}}
  2. state = _load_state()                    # sidecar
  3. candidates = [c for c in claims.values()
                  if len(c.source_files) >= MEMORY_DREAMING_MIN_DISTINCT_FILES
                     and c.hash not in state["promoted"]]   # 确定性门
  4. if not candidates: return
  5. _promote_append(mgr, candidates)         # mgr.write(append=True) 每条 "text\n"
       for c in candidates: state["promoted"][c.hash] = {ts, text, first_path}
  6. _save_state(state)                        # 原子写 sidecar
  7. await _consolidate(mgr)                   # 单次 LLM(见 §8)；失败/验证不过 → 跳过(append-only 版留着)
  8. _compact_if_over_budget(mgr, state)       # len>max → 按 ts 丢最老仍存在的 promotion 行
```

## 7. 核心设计决策（非显然，请重点审）

### 7.1 consolidate = LLM 出"删行号列表"，不是 openclaw 的 add/merge/supersede

openclaw 一次 consolidation 既"加"又"合并"（add/merge/supersede + resultEntry）。本设计**先 append（晋升步已加）再 consolidate-delete**：consolidation 只需删冗余/矛盾的**旧行**。

- LLM 输入：MEMORY.md 非空行**编号列表**。
- LLM 输出：`{"delete":[行号]}`。
- 程序逆向 filter 掉这些行 → `mgr.replace` 全量写回。

**LLM 全程不碰文本**（只出行号），比 openclaw 的 structured ops 更安全（连 resultEntry 字节校验都免了），效果等价：
- add = 晋升步 append 已做。
- merge = 删冗余旧条目留新的。
- supersede = 删矛盾旧值留后写值。

验证降维到"删除比例 ≤ 25%"。这是对 openclaw 的**裁剪**（Twinkle 先 append 后 delete，把 LLM 任务从"既加又合"降为"只删"），不是偷懒。

### 7.2 sidecar `dreaming_state.json` 替 MEMORY.md marker

idempotency + compact 年龄用 sidecar，**不在 MEMORY.md 挂 marker**。原因：consolidation 会删行（含已晋升的冗余行）；若靠 MEMORY.md marker 判 idempotency，marker 随行被删 → 下 tick 重晋。sidecar 的 `promoted` 集**只增不减**（一旦晋升不再重晋，哪怕被 consolidation 删了），跟 openclaw recall store 的 `promotedAt` 同理。

sidecar 由 dreaming.py 用 raw pathlib 自管（不走 `mgr.write` 白名单——那是给用户记忆内容的），原子写（tempfile + os.replace）。

### 7.3 砍掉 search-score 预筛（`already_known_score`）

Phase 1 草案曾用 `mgr.search` score 预筛"已在 MEMORY.md"。B 里语义去重归 consolidate LLM（能判"不同措辞同事实"），idempotency 靠 sidecar hash 精确判。不需 write-time search-score 预筛。少一个 config 字段 + 少一次 search。

## 8. 数据结构

### 8.1 sidecar `<memory_dir>/dreaming_state.json`

```json
{
  "version": 1,
  "promoted": {
    "<claimHash>": {"ts": "2026-08-15T03:00:00", "text": "- 喜欢爬山运动", "source_path": "daily_memory/2026-08-14.md"}
  }
}
```

- `promoted` 只增不减（晋升即记；consolidate 删行不删 record）。
- 读：`_load_state()` → 不存在/坏 JSON → `{"version":1,"promoted":{}}`。
- 写：`_save_state(state)` → tempfile + os.replace（原子）。

### 8.2 claim（`_scan_claims` 返回）

```python
{ "<claimHash>": {"text": "<strip 后的行>", "source_files": {"daily_memory/2026-08-14.md", ...}, "first_path": "..."} }
```

- `claimHash = hashlib.md5(line.strip().encode()).hexdigest()`（strip 去空白，保留大小写）。
- 文件内同行去重为 1 claim；跨文件同 claim 累积 `source_files`。

## 9. LLM 调用（consolidate）

**prompt 硬编码进 `dreaming.py` 模块常量**（JSON 契约，不进 config，守 [[json-contract-prompts-not-in-config]]）：

```
_CONSOLIDATE_PROMPT = """你是记忆去重整合器。下面是【MEMORY.md 当前的非空行，已编号】。

找出其中的：
- 语义重复行（同一事实、不同措辞）→ 保留更完整/更明确的那条，删冗余的。
- 矛盾行（同一实体的单一取值属性、不同取值，如"用 Windows" vs "用 Mac"）→ 保留更后写入（编号更大）的那条，删旧值。

硬约束：
1. 只删行，绝不改写任何行的原文（保留的行逐字不动）。
2. 删除行数不得超过总行数的 25%。
3. 不得新增行、不得新增内容。
4. 只输出 JSON，禁止非 JSON 文本（不要代码块、不要解释）：
{"delete":[行号, 行号, ...]}

【MEMORY.md 编号行】
{numbered_lines}"""
```

- `numbered_lines` = `"1: - 用 Windows 系统\n2: - 用 Windows\n3: - 喜欢爬山运动\n..."`（非空行，1-indexed）。
- MEMORY.md 非空行 < 2 → 跳过 consolidate（无可合并）。
- 复用现有 `_ask_llm(prompt)` 收 TextDelta（保留该方法，原 `_CONSOLIDATE_PROMPT`/`_EXTRACT_PROMPT` 二选一改写）。

## 10. 验证（consolidate 输出）

`_consolidate(mgr)` 流程：

1. 读 MEMORY.md → 非空行列表 `lines`。`len(lines) < 2` → return（跳过）。
2. `_ask_llm(_CONSOLIDATE_PROMPT.format(numbered_lines=...))` → `raw`。LLM 异常 → `log.exception` + return（fail-soft，append-only 版留着）。
3. `json.loads(raw)` → `{"delete":[ints]}`。解析失败 → `log.warning` + return。
4. 校验：
   - `delete` 是 int 列表；
   - 每个号 ∈ [1, len(lines)]；
   - `len(set(delete)) / len(lines) ≤ MEMORY_DREAMING_MAX_DELETE_FRACTION(0.25)`。
   - 任一不过 → `log.warning` + return。
5. 保留行 = `[line for i, line in enumerate(lines, 1) if i not in set(delete)]`。
6. `mgr.replace("MEMORY.md", "\n".join(kept) + "\n")`（全量原子写回）。

**回退**：步骤 2-4 任一失败 → 不写，晋升步 append 的版本即为最终结果（等价 openclaw append-only fallback）。

## 11. compact（容量预算）

`_compact_if_over_budget(mgr, state)`：

1. `text = mgr.read("MEMORY.md")`；`len(text) ≤ MEMORY_DREAMING_MAX_MEMORY_CHARS` → return。
2. 当前行列表 `lines`。
3. `promoted_texts = {rec["text"] for rec in state["promoted"].values()}`。
4. 找 `lines` 中文本 ∈ `promoted_texts` 的行 = 当前 promotion 行；按其 sidecar `ts` 升序（最老先丢）。
5. 逐个从 `kept` 移除（只移 promotion 行，非 promotion 用户行不动），直到 `len(joined) ≤ max`。
6. `mgr.replace("MEMORY.md", "\n".join(kept) + "\n")`。

## 12. config 新增（`MemoryDreamingConfig`）

```python
class MemoryDreamingConfig(_StrictModel):
    enabled: bool = False
    interval_seconds: int = 3600
    start_delay_seconds: int = 300
    top_k: int = 5                       # 保留(暂无消费者，备未来 search 用)；如确认无用可删
    min_distinct_files: int = 2          # 晋升门：跨 distinct daily 文件数
    max_memory_chars: int = 10000        # 容量预算
    max_delete_fraction: float = 0.25    # consolidate 删除比例上限(安全阀)
```

`config/__init__.py` 加 `MEMORY_DREAMING_MIN_DISTINCT_FILES` / `MEMORY_DREAMING_MAX_MEMORY_CHARS` / `MEMORY_DREAMING_MAX_DELETE_FRACTION`。`resources/config.yaml` `memory.dreaming` 块加 3 键 + 注释更新。

## 13. store.py 新增（小，surgical）

`MemoryManager.replace(path, content) -> str`：原子全量覆写。

```python
def replace(self, path: str, content: str) -> str:
    relative_path = self._resolve_relative_path(path)
    if relative_path is None:
        return f"Error: invalid memory path '{path}'."
    fpath = self._dir / relative_path
    fpath.parent.mkdir(parents=True, exist_ok=True)
    tmp = fpath.with_suffix(fpath.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(fpath))   # 原子
    except OSError as exc:
        return f"Error replacing '{path}': {exc}"
    self._index_file(relative_path)
    return f"Replaced {relative_path}."
```

- consolidate（删行写回）+ compact（删行写回）共用。
- `write(append=True)` 仍用于晋升步 append（无需原子——逐行 append 幂等，crash 安全）。
- `edit` 现有保留（其他消费者）。

## 14. dreaming.py 方法增删

**新增**：`_scan_claims` / `_load_state` / `_save_state` / `_promote_append` / `_consolidate` / `_compact_if_over_budget`。
**改写**：`_CONSOLIDATE_PROMPT`（旧 pairwise judge prompt → 新删行号 prompt）；`_ask_llm` 保留（consolidate 用）。
**删 orphan**：`_dedupe_and_resolve` / `_find_earlier_entry` / `_judge_relation` / `_harvest_daily` / `_extract_facts` / `_EXTRACT_PROMPT` / `_already_in_memory` / `_memory_entries`（如新 body 不再用）/ `json` import（consolidate 仍用 json，留）。
**留**：`_nonempty_lines`（`_scan_claims` 复用）；`_ask_llm`（consolidate 用）；`TextDelta` import（`_ask_llm` 用）。

## 15. B-scheme 合规（§7 边界）

| 边界 | 本设计 |
|---|---|
| 写入路径 LLM | **零**（晋升步零 LLM；consolidate 在后台 dreaming，不在 write 路径） |
| daily append-only | 不动只读（`_scan_claims` 只 read） |
| dreaming ≠ write/search | 独立后台 task（wiring 不动） |
| opt-in 默认关 | `enabled: false`（不变） |
| LLM 在 dreaming | consolidate 一次 LLM（spec §4 原允许；B-scheme §7 写"LLM 只在 flush/dreaming"，守） |

> 不声称"dreaming 零 LLM 强化"——那是 Phase 1 草案的说法；B 的 consolidate 用 LLM，回归 spec §4 原意（dreaming 可用 LLM，在后台）。

## 16. 改动文件清单

| 文件 | 改动 |
|---|---|
| `twinkle/agentserver/memory/dreaming.py` | 重写 dream() body + 增删方法（§14） |
| `twinkle/agentserver/memory/store.py` | 新增 `replace(path, content)`（§13） |
| `twinkle/config/schema.py` | `MemoryDreamingConfig` 加 3 字段（§12） |
| `twinkle/config/__init__.py` | 加 3 常量 |
| `twinkle/resources/config.yaml` | `memory.dreaming` 块加 3 键 |
| `tests/test_dreaming.py` | 重写 C/D/E 组（§17） |
| `docs/design/memory-b-scheme-design.md` §4 | 批准后折入（T10） |
| `roadmap.md` Phase 5-5c | 机制描述更新（T10） |

**不动**：`server.py`、`DreamingOrchestrator` 类壳/`__init__`/`run_loop`/`start_dreaming`、A/B 组测试。

## 17. TDD 任务分解（RED→GREEN）

- **T1 config**：RED 断 3 新常量 → schema+__init__+yaml。
- **T2 `MemoryManager.replace`**：RED 原子覆写（tempfile+os.replace，索引更新）。
- **T3 `_scan_claims`**：RED dedup_same_line（2 daily 同行→1 claim, source_files={2}）+ single_file。
- **T4 sidecar load/save**：RED 不存在→空 dict / save→load 往返 / 原子（.tmp 不留）。
- **T5 门 + `_promote_append`**：RED blocks_low_frequency（1 文件不晋）+ blocks_already_promoted（sidecar 有 hash 不晋）+ passes_2_files（晋 + sidecar 记录 + MEMORY.md append）。
- **T6 `_consolidate`**：RED deletes_redundant（2 同义行→LLM 出 delete→剩 1）+ resolves_conflict（留后写值）+ loss_budget_fallback（LLM 出 >25%→验证拦→不动）+ llm_fail_soft（异常→跳过 append 留着）+ json_parse_fail_soft。
- **T7 `_compact_if_over_budget`**：RED 超 max→丢最老 promotion 行（非 promotion 行不动）。
- **T8 dream 端到端 + 门卫**：RED no_daily_files_noop + disabled_noop + busy_skips + runs_when_idle + promotes_across_two_daily_then_consolidates（端到端）+ sidecar_idempotent_across_ticks。
- **T9 A/B 组测试调整**：A 组 4 个删 `llm.calls` 断言（consolidate 才调 llm，门卫不调）；B 组不变。`runs_when_idle` 1 条目无 daily→无 candidate→不 consolidate→不调 llm→条目留着。
- **T10 文档**：折入 §4 + roadmap。

> 旧 4 测试（`dedup_redundant`/`resolve_conflict`/`extract_new_fact_from_daily`/`extract_when_memory_empty`）测删掉的 pairwise/harvest LLM → 换成 T6/T8 新模型测试。`skip_existing_fact`/`llm_failure_fails_soft` 碰巧过但意图错 → 换成真实原因断言。

## 18. 验证

```bash
python -m pytest tests/test_dreaming.py -v               # ~16 GREEN（新模型）
python -m pytest tests/test_observability_memory.py -v  # 3 flush 不变
python -m pytest tests/ -q                                # ~848 passed / 16 pre-existing（15 cron+1 pptx）
python -m twinkle.agentserver                            # smoke：启服务不崩
```

成功标准：N² pairwise 消失、claimHash 精确去重替子串、sidecar 幂等防重跑、单次 consolidate LLM（删行号，不碰文本）+ 比例验证 + fail-soft、容量预算 compact、B-scheme §7 全守。

## 19. 与 openclaw / jiuwenswarm 关系

- **借 openclaw 的模型**：claimHash 精确去重、确定性门槛筛够格候选、sidecar 跟 promotedAt（只增不减）、单次 consolidation LLM + 验证 + append-only 回退、容量预算 compact。
- **裁剪**：删行号列表替 add/merge/supersede structured ops（先 append 后 delete 降维）；无乐观锁/预镜像/多项目/6 信号（Twinkle 无 query log + 单进程 idle-only）。
- **保留 Twinkle 占优处**：busy-backoff 触发（对话中跳过，比 openclaw 固定凌晨 3 点合单用户）、opt-in 默认关。
- **jiuwenswarm**：local 腿空壳定时器无整理逻辑（gaps 已确认），本设计仍是自研落地，对齐 openclaw 而非 jiuwenswarm。
