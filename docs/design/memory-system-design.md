# 长期记忆机制设计与实现

本文讲清一件事：**agent 如何用记忆**——记忆数据怎么存、怎么写进去、怎么检索出来、怎么改、怎么过期。源码在 [`twinkle/agentserver/memory/`](../../twinkle/agentserver/memory)。

## 1. agent 如何使用记忆：一次 ReAct 循环里的记忆流

记忆不是自动塞进上下文的——**模型自己决定调不调**。一个完整往返是这样：

```
每一步 llm.stream 前
  └─ MemoryHook.before_model_call
       ├─ store 非空？否 → no-op，什么都不注入
       └─ 是 → 在 messages 前插一条 system prompt：
              "你有跨会话长期记忆，工具是 memory_search/write_memory/
               read_memory/edit_memory。何时搜、何时写、写哪个文件……"

模型读到这条策略 prompt + 4 个工具的 schema，自己判断：
  ├─ 这一轮不需要历史 → 不调任何记忆工具，正常往下走
  ├─ 回答依赖跨会话事实 → 调 memory_search(query)
  │      └─ 返回召回片段（带 path/score/text/行号）作为 tool_result 回灌
  │         模型据此回答，或再调一次换 query
  ├─ 该记一笔 → 调 write_memory(path, content, append)
  ├─ 要精确改某句 → 调 edit_memory(path, old_text, new_text)
  └─ 要读某文件原文 → 调 read_memory(path, offset, limit)
```

要点：

- **注入的是策略 prompt，不是检索结果。** `MemoryHook` 不替模型搜，只教它「什么时候该搜、搜完该往哪写」。检索只发生在模型主动调 `memory_search` 时。理由：ReAct 循环本质是「模型决定调哪个工具」，自动注入既吃 token 预算，又抢了模型对「这轮到底要不要历史」的判断权。**被动召回（opt-in）**：设 `memory.auto_inject.enabled=true` 时，`before_model_call` 额外把 `USER.md`（用户画像）+ `MEMORY.md`（持久事实）+ 今日 `daily_memory`（episodic）全文（cap，默认 12000 字符）注入 system prompt，对齐 jiuwenswarm `ProjectMemoryRail` / `memory_rail._load_recent_daily_memory`；注入顺序 USER → MEMORY → 今日 daily；默认关，维持只策略。`MEMORY.md` 大到撑爆 cap 时尾部截断 + 提示「更多用 memory_search 查」，退回靠 search。
- **空 store 不注入。** 记忆目录里没文件时 `MemoryHook` 直接 return，不往上下文塞废话。
- **策略 prompt 内容**（[`memory_hook.py`](../../twinkle/agentserver/hooks/builtin/memory_hook.py) 的 `_PROMPT_TEMPLATE`）规定了几条路由规则，见下面 §2。

工具注册在 [`tools/__init__.py`](../../twinkle/agentserver/tools/__init__.py) 的 `tool_manager()`，权限默认 `allow`。`MemoryHook` 在主 agent、子 agent、team agent 三个入口都挂上（priority 80）。

---

## 2. 记忆数据如何存储

记忆分两层：**markdown 是真源，SQLite 是派生检索索引**。

### 2.1 目录结构

默认 `<workspace>/.twinkle_data/memory`（`TWINKLE_MEMORY_DIR` 覆盖）：

```
memory/
├── USER.md                 # 用户画像：姓名/职业/沟通语言/操作系统/常用技术
├── MEMORY.md               # 持久事实：决策/偏好/项目约定/架构/技术选型
├── daily_memory/
│   └── 2026-08-10.md        # 当日笔记：用户说"记住这个"、运行上下文、当日发生的事
└── memory.db               # SQLite 检索库（派生，删了可从 .md 重建）
```

三类文件各有分工，路由由策略 prompt 教模型（代码不强制）：

| 文件 | 写什么 | 何时写（prompt 规定） |
|---|---|---|
| `USER.md` | 用户个人信息 | 用户给了姓名/职业/沟通语言/OS/常用技术 |
| `MEMORY.md` | 决策/偏好/持久事实 | 项目约定、架构、技术选型、已做决定 |
| `daily_memory/YYYY-MM-DD.md` | 当日发生的事 | 用户说"记住这个"、运行上下文、当天事件 |

prompt 还规定**不该写**：临时数据、过程性状态（那是 todo 的活）、寒暄、本轮就过期的事。

### 2.2 SQLite 检索库（`memory.db`）

`MemoryManager.__init__` 连接单库（`check_same_thread=True`，全在 AgentServer 单事件循环线程上），`_ensure_schema` 建表。6 张表分两类：**主链三表**（一个 chunk 的内容/全文/向量三副本，共享 rowid）和**辅助三表**（指纹、缓存、元信息，各自独立）。

#### 各表作用

| 表 | 类别 | 作用 | 关键列 |
|---|---|---|---|
| `chunks` | 主链 | 分块内容主表，一个 chunk 一行，存原文+元数据+向量 BLOB | `id`(`path:start:end`, 主键) / 隐式 `rowid` / `path` / `start_line` / `end_line` / `hash` / `model` / `text` / `embedding`(BLOB) / `updated_at` |
| `chunks_fts` | 主链 | FTS5 全文索引，存 chunk 文本的分词副本（jieba 词级 / 无则逐字空格），供 bm25 搜 | 隐式 `rowid` / `text` |
| `chunks_vec` | 主链 | `sqlite-vec` 向量虚表，存 chunk 向量，供 cosine 搜（可选） | 隐式 `rowid` / `embedding float[1536]` |
| `files` | 辅助 | 文件级指纹，记录每个文件上次索引时的 mtime/size/hash，供增量跳过 | `path`(主键) / `hash` / `mtime` / `size` |
| `embedding_cache` | 辅助 | 嵌入缓存，按 chunk 文本 md5 去重，同文不重复 embed | `hash`(主键) / `embedding` / `dims` / `updated_at` |
| `meta` | 辅助 | 元信息，存当前嵌入模型名，供模型变更时全量重建判据 | `key`(主键) / `value`（如 `embed_model`） |

`chunks` / `chunks_fts` / `embedding_cache` / `files` / `meta` 总会建。`chunks_vec` 只在 `import sqlite_vec` + `load(db)` 都成功时建，置 `self._vec_enabled=True`；任一环节失败只记 warning，降级 FTS-only——**向量是可选增强，不是硬依赖**。

#### 表之间的关联：三条轴

理解这 6 张表的关键是搞清谁和谁关联、用什么字段关联。一共三条独立的关联轴，没有外键约束，全靠代码维护一致性：

```
                 ┌──── rowid 轴（一个 chunk 的三副本，强耦合）────┐
                 │                                                   │
  chunks(rowid) ◄─┼─► chunks_fts(rowid)   ◄─┐                      │
       │          │                          │ 同 rowid = 同 chunk  │
       │          └─► chunks_vec(rowid) ◄────┘                      │
       │                                                              │
       │ path                                                          │
       ▼                                                               │
  files(path)  ← 文件级指纹，驱动增量跳过（path 轴，弱耦合）           │
                                                                       │
  chunks.text ──md5──► embedding_cache(hash)  ← 内容去重缓存（hash 轴，弱耦合）
                                                                       │
  meta(key='embed_model')  ← 全局元信息，独立，驱动模型变更重建 ────────┘
```

**① rowid 轴（主链三表，强耦合）**——`chunks` / `chunks_fts` / `chunks_vec` 三个表共享同一个 `rowid`：往 `chunks` INSERT 拿到 `lastrowid`，再用这个 rowid 往 `chunks_fts` 和 `chunks_vec` INSERT 对应的分词文本和向量。检索时 `_vec_search` 先从 `chunks_vec` 拿到 rowid+distance，再用 rowid 回 `chunks` 取原文/路径/行号；`_fts_search` 同理 FTS 拿 rowid 后 JOIN `chunks` 取详情。**一个 rowid = 一个 chunk 的全部三副本**。删旧块时也是先查 `chunks` 的 rowid 列表，再去三张表 `DELETE WHERE rowid IN (...)`。rowid 是这条轴的承重墙，chunks 是主，FTS/vec 是它的两个侧索引。

**② path 轴（files ↔ chunks，弱耦合）**——`files` 按 `path` 存指纹（mtime/size/hash），`chunks` 每行也带 `path`。`_index_file` 进来先查 `files WHERE path=?` 比指纹决定跳不跳过，变了就 `DELETE FROM chunks WHERE path=?` 删该文件全部旧块。`files` 是「这个文件上次索引成什么样了」的缓存，`chunks` 是「这个文件现在索引出了哪些块」的事实表，两者靠 path 对齐但无外键——`_index_file` 一个事务里同时维护两边。

**③ hash 轴（embedding_cache ↔ chunks.text，弱耦合）**——`embedding_cache` 按 chunk 文本 md5 存向量，和 `chunks` 没有直接字段关联（不存 chunk_id），靠「同文本同 hash」内容寻址。`_embed_chunks` 把要 embed 的文本 md5 逐个查缓存，命中直接取 BLOB，未命中才调 embedding API。好处是**跨文件去重**：两个文件里有完全相同的一段文字，只 embed 一次、缓存一份。坏处是缓存和 chunks 的对应关系是隐式的，删 chunk 不会清对应缓存（缓存只是越积越多，靠 `maxEntries` 或重建清理）。

`meta` 不参与任何关联轴，就是个全局键值对，单独存 `embed_model`，`_clear_if_model_changed` 启动时读它判模型变没变。

#### 建表顺序与一致性

`_ensure_schema` 建 5 张必建表后，`_index_file` 写入时**靠一个事务**保证主链三表 + files 的 rowid/path 一致性：任一插入失败（如 vec0 维度不匹配）整个 `rollback()`，不会出现「`chunks` 有行但 `chunks_fts` 没行」的孤儿。这条事务边界是踩过坑后加的（见 §5.4）。

### 2.3 `memory.db` 如何被构建（从空库到有内容）

`MemoryManager` 是进程级懒单例：`get_memory_manager()`（[`memory/__init__.py`](../../twinkle/agentserver/memory/__init__.py)）所有 `@tool` 函数和 `MemoryHook` 都走它，首次调用才构造，避免 import-time 连库/连 embedding API 的副作用；测试用 `_set_memory_manager(mgr)` 换桩。构造时从 config 读全部参数（分块大小、融合权重、候选放大倍数、单文件 chunk 上限等）。

库不是启动时一次性灌满的，是**懒构造 + 逐文件增量**建起来的。完整时序：

```
进程启动
  └─ AgentServer 起来，MemoryManager 还没构造（懒）

首次需要记忆（某 step 的 MemoryHook.before_model_call，或模型调了某个 memory_* 工具）
  └─ get_memory_manager() 首次调用
       └─ MemoryManager.__init__:
            1. mkdir memory/ + memory/daily_memory/
            2. sqlite3.connect(memory.db)        # 文件不存在则新建
            3. _ensure_schema()                   # 建表
                 ├─ chunks / chunks_fts / embedding_cache / files / meta  # 必建
                 └─ 尝试建 chunks_vec (vec0)       # sqlite_vec 可装则建+置 vec_enabled
                                                      失败 → warning，降级 FTS-only
            4. _clear_if_model_changed()
                 └─ provider 在 且 meta.embed_model 与当前 model 不符 → 清空全表
                 └─ 否则（含空库）no-op

此刻 memory.db 状态：表都建好了，但 chunks 为空——没有任何检索内容。
```

关键：**`__init__` 不扫描已有 markdown 文件**。Twinkle 没有 jiuwenswarm 那种 `sync(reason="initial")` 启动扫盘——schema 是懒构造时建好的空壳，内容什么时候进库、怎么进库，见 §3。

---

## 3. 记忆如何写入

写入入口是 `write_memory` 工具 → `MemoryManager.write`：

```
write(path, content, append)
  1. _resolve_relative_path(path)     # 白名单校验，非法返回错误串
  2. 打开文件 append("a") 或覆盖("w") 写入
     └─ append 且内容不以 \n 结尾 → 补 \n（避免追加时挤到上一行尾）
  3. _index_file(relative_path)       # 落盘后立即索引（见 §5）
  4. 返回 "Stored to {relative_path}."
```

`write_memory(path, content, append=False)` 默认覆盖整文件；`append=True` 追加。整文件覆盖用于重写，追加用于往 `MEMORY.md` 加新条目。

**写入的触发**完全靠模型：它读到策略 prompt，判断「这一轮用户给了该记的信息」，主动调 `write_memory`。代码层没有任何「检测到关键词就自动写」的逻辑——5a 纯 model-driven。

### 3.1 路径白名单（写入前的访问控制）

上面流程第 1 步的 `_resolve_relative_path` 是写入/读取/改写的统一前置校验，决定模型能不能动这个文件。规则（[`store.py: _resolve_relative_path`](../../twinkle/agentserver/memory/store.py)）：

- 只放行 `USER.md` / `MEMORY.md`（根）或 `daily_memory/YYYY-MM-DD.md`（日期须匹配 `^\d{4}-\d{2}-\d{2}\.md$`）。
- `is_relative_to(self._dir)` 防路径穿越（`../../etc/passwd` 之类被挡）。
- 不合法返回错误串（不抛异常）——`write` / `read` / `edit` 都会返回 `Error: invalid memory path ...`，模型拿到 tool_result 自己改。

这是把记忆写权限严格圈在三类文件内的防线：模型无法用记忆工具读写工作区其他文件，也不能越出 `memory/` 目录。`write` / `read` / `edit` 三个工具都先过这道闸，再落盘/读盘。

### 3.2 写入是内容进库的唯一入口

§2.3 说过：`__init__` 只建空壳表，不扫盘。所以 **`memory.db` 的内容是模型驱动写入时由 `_index_file` 逐文件构建的**——`write_memory` / `edit_memory` 落盘 markdown 后立即调 `_index_file(rel)`，这才是把内容写进 `chunks` / `chunks_fts` / `chunks_vec` 的唯一入口（`_index_file` 本身见 §5）。`write` 路径上它是同步调用的，写完文件紧接着索引：

```
write_memory / edit_memory
  └─ store.write / store.edit 落盘 markdown
       └─ _index_file(rel)            # 把内容写进 chunks/FTS/向量 的唯一入口
```

这意味着**第一批 chunks 是模型第一次写入时才进库的**，带来一个实际后果：

- 直接往 `memory/` 目录丢现成的 `USER.md` / `MEMORY.md` 再启动 → `MemoryHook` 仍会注入策略 prompt（因为 `list_files()` 扫的是文件系统，不是 DB，发现有 `.md` 就注入，让模型知道「有记忆可用」）；但 `memory_search` 召回为空——因为这些文件从没经过 `_index_file`，DB 里没有它们的 chunks/向量。
- 要让这些「外部塞进来的」文件进检索库，得等模型对它们调一次 `write_memory`/`edit_memory`（触发 `_index_file`）。Twinkle 无 watchdog，不会自动感知外部编辑重索引——这是 5a 的刻意取舍（见 §9）。

换句话说，markdown 真源和 DB 检索内容在「外部塞文件」这个场景下会暂时不同步：文件在、prompt 注入了，但检索库是空的，要等一次写入把它们对齐。

---

## 4. 记忆如何检索

检索入口是 `memory_search` 工具 → `MemoryManager.search`：

```
search(query, max_results)
  candidates = min(200, max(1, max_results * 2.0))   # 候选放大
  fts  = _fts_search(query, candidates)               # FTS5 bm25
  无 vec 或无 provider?
    是 → 直接返回 fts[:max_results]（FTS-only，见 §4.3）
    否 ↓
  vec  = _vec_search(query, candidates)               # sqlite-vec cosine
  合并候选（FTS 命中 + 向量命中的并集，补齐 chunks 行）
  对每个候选算 fused = 0.7*v + 0.3*_text_sim(bm25)
  按 fused 降序排，取 top-N
  返回 [{path, score, text, start_line, end_line}, ...]
```

### 4.1 向量腿

`_vec_search` 调 `provider.embed([query])` 得查询向量，`chunks_vec MATCH ? ORDER BY distance LIMIT ?` 取最近邻。cosine 距离 ∈ [0,2]，映射成相似度 `1 - distance/2`，clamp 到 [0,1]。查询 embedding 失败时这条腿直接 skip，不抛。

### 4.2 FTS 腿 + bm25 归一化

`_fts_search` 用 `build_fts_query(query)` 构造 MATCH 串（每 token 包引号 + OR，见 §4.3.1）+ `chunks_fts MATCH ?` + `ORDER BY bm25(chunks_fts)`（bm25 越负越相关）。`_text_sim(bm)` 把非正值映射到 [0,1]：`a=abs(bm); a/(1+a)`，最相关→接近 1.0，让 FTS 分数和向量相似度同量纲融合。

### 4.3 CJK 分词：jieba 可选 + 降级逐字空格（FTS 腿的关键）

FTS5 的 `unicode61` 分词器不切 CJK，中文查询会召回失败。分词逻辑在 [`fts.py`](../../twinkle/agentserver/memory/fts.py)（抄 jiuwenswarm `internal.py`），双路径：

- **有 jieba**（装 `[memory]` extra）：索引走 `jieba.cut_for_search`（细粒度提召回），查询走 `jieba.cut`（粗切），再过停用词（`stopwords_zh.txt` 793 词）——词级召回质量高，对齐 jiuwenswarm。
- **无 jieba**：降级 `_space_cjk` 逐字空格（每个汉字前后加空格让 unicode61 切单字 token）+ OR——召回率够用但单字语义弱。

为什么必须做：无 API key 时检索降级为 FTS-only，仍要能查中文。`_embed_chunks` 失败的 chunk 不嵌向量，只进 FTS，这时 CJK 召回全靠分词。

### 4.3.1 FTS phrase query bug（已修）

旧 `_fts_search` 把整句 query 包双引号喂 FTS5 = phrase（所有 token 须按序连续），导致换措辞的自然语言 query（如「我们之前为什么决定用 SQLite」对记忆「决定使用 SQLite」）**0 命中**——FTS 腿实际废掉。现 `build_fts_query` 按 token 切分、每 token 包引号 + **OR 连接**（前 10 token），任一 token 命中即记分，换措辞也能召回。对齐 jiuwenswarm `build_fts_query`。

### 4.4 无分数截断

显式**不做**分数截断，只排序 + top-N。召回质量交给候选放大（2 倍）、融合权重、top-N 控制，不靠一个魔法阈值砍结果。低质结果也会进 top-N，由模型自行判断取舍。

### 4.5 降级矩阵

| 条件 | 行为 |
|---|---|
| `sqlite-vec` 装了 + provider 有 | 混合检索（vector+FTS 融合） |
| `sqlite-vec` 没装 / load 失败 | FTS-only（bm25 排序） |
| 无 `LLM_API_KEY`（provider=None） | FTS-only（写时不嵌向量） |
| 查询 embedding 失败 | 向量腿 skip，退回 FTS-only |
| 无 jieba（未装 `[memory]` extra） | FTS 仍工作，CJK 降级逐字空格 + OR（单字语义弱，召回不丢） |

任何一腿失败都不让整次 search 崩——`search` 总返回一个 list（可能空）。jieba 有无只影响 CJK 召回质量，不影响检索是否工作。

---

## 5. 写入后如何索引：`_index_file` 做了什么

`write` / `edit` 落盘后立即调 `_index_file(rel)`。这是把「markdown 文件」变成「可检索的 chunks/FTS/向量」的核心函数，一次调用完成「判变 → 清旧 → 分块 → 嵌入 → 落库 → 淘汰」全流程。完整步骤（[`store.py: _index_file`](../../twinkle/agentserver/memory/store.py)）：

```
_index_file(rel):
  1. 读文件 stat(mtime/size) + 全文，算 file_hash = md5(content)
  2. 查 files 表旧指纹；mtime + size + hash 三重都一致 → return（未变，跳过）
  3. 开事务（try）：
     a. 删旧块：先查 chunks 表该 path 的所有 rowid，
        DELETE FROM chunks WHERE path=?
        DELETE FROM chunks_fts WHERE rowid IN (旧rowid)
        DELETE FROM chunks_vec WHERE rowid IN (旧rowid)   # 仅 vec_enabled
     b. 分块：_chunk(content) → [Chunk(start, end, text), ...]
     c. 嵌入：want_vec(vec_enabled 且 provider 在) 时 _embed_chunks(texts)
        └─ 内部先查 embedding_cache 命中、未命中批量调 provider.embed 并写回缓存
        └─ 失败：返回 [None,...]，这些块仍进 FTS，不进向量，不重试
     d. 逐块写库（chunks 主表 INSERT，拿 rowid）：
        └─ chunks_fts INSERT(rowid, tokenize_for_fts(text, True))   # jieba 词级(无则逐字空格)
        └─ want_vec 且 blob 非 None：chunks_vec INSERT(rowid, embedding)
     e. UPSERT files(path, hash, mtime, size)  # 更新指纹，下次增量比对的基准
     f. UPSERT meta(embed_model = provider.model)
     g. _evict_excess_chunks(rel)  # 单文件 chunk 超 200 FIFO 删最旧
     h. commit()
  4. 任一步异常 → rollback() + raise（不吞错，让上层 tool 返回错误串）
```

一句话：**`_index_file` = 增量判变 + 全量替换**——文件变了就把该文件在检索库里的一切（chunks/FTS/向量）删干净，用新内容重新分块、重新嵌入、重新写回，指纹更新，超额淘汰。markdown 没变就什么都不做。

下面拆开几个关键子机制。

### 5.1 增量跳过（步骤 2）

三重指纹（mtime + size + content md5）避免「mtime 没变但内容变了」或「size 没变但内容变了」的假阴性。命中跳过是 `_index_file` 最高频的出口——同一个文件被 `write` 反复调用（比如连续追加），内容没变时直接 return，零分块零嵌入零 SQL。

### 5.2 分块：行级滑窗 + 尾部回溯 overlap（步骤 3b）

`_chunk` 按行切，单块预算 `chunk_tokens*3` 字节（默认 256 token ≈ 768 字节），相邻块共享 `chunk_overlap*3` 字节的尾部回溯行。回溯是向回走直到 overlap 预算填满，保证连续块语义衔接——一个事实横跨两块时，overlap 让它至少在两个块里都被检索到。每块带 `start_line` / `end_line`，召回结果回带行号。

### 5.3 嵌入缓存 + 失败兜底（步骤 3c）

`_embed_chunks` 先按 chunk 文本 md5 查 `embedding_cache`，命中直接用；未命中的批量调 `provider.embed([...])`，写回缓存。命中零开销，未命中才打一次 embedding API。

- embed 抛异常时：这些 chunk **仍进 FTS**（不写向量），记 warning，**不重试**——下次该文件内容再变才会重试（5a 无重试调度器）。
- 模型变更：`__init__` 末尾 `_clear_if_model_changed` 对比 `meta.embed_model` 与当前 provider.model，不一致就清空所有表（不重建——重建是惰性的，下次 `write`/`edit` 触发 `_index_file` 才用新模型重灌该文件）。改嵌入模型/维度 → 删 `memory.db` 或靠后续写入逐步回灌。

### 5.4 事务边界（步骤 3 整体）

删旧块插新块整段包在 `try/except`：失败 `rollback()` 后 `raise`。注释点明踩过的坑：不包事务时，中途失败（如 vec0 维度不匹配的 INSERT）会让未提交事务泄漏到单例连接的下一次写，把破损文件的半成品状态一起提交。

---

## 6. 记忆如何更新

更新有两条路：

### 6.1 精确改：`edit_memory`

`edit_memory(path, old_text, new_text)` → `MemoryManager.edit`：

1. 白名单校验 + 文件存在校验
2. 读全文，`old_text not in text` → 返回错误串
3. `text.replace(old_text, new_text, 1)`（只替换**首次**出现）
4. 写回，`_index_file(rel)` 重索引

用途：模型 recall 到与当前信息矛盾的记忆时，按策略 prompt 指引用 `edit_memory` 修正它。比如用户改了操作系统偏好，模型把 `USER.md` 里旧的 OS 那句换掉。

### 6.2 整文件重写：`write_memory(append=False)`

覆盖整文件。用于重写一个杂乱的 `daily_memory/2026-08-10.md`，或整段重写 `MEMORY.md` 的某个区块（配合先 `read_memory` 读出、改完整体写回）。

两条路最后都汇到 `_index_file` 重索引，靠 mtime 增量保证只在该改的时候改。

---

## 7. 记忆如何过期

5a 的过期/淘汰机制很克制，只有一条自动淘汰 + 几条隐式边界：

### 7.1 单文件 chunk 上限（FIFO 淘汰）

`_evict_excess_chunks` 在每次索引后检查该文件 chunk 数是否超 `max_chunks_per_file`（默认 200），超限按 `updated_at ASC` 删最旧的（FIFO）：

```
chunk_count = SELECT COUNT(*) FROM chunks WHERE path=?
if chunk_count > 200:
    num_to_evict = chunk_count - 200
    oldest_rowids = SELECT rowid FROM chunks WHERE path=? ORDER BY updated_at ASC LIMIT num_to_evict
    DELETE FROM chunks / chunks_fts / chunks_vec WHERE rowid IN (oldest_rowids)
```

这是 5a **唯一**的自动淘汰。注意它淘汰的是检索块，**不删 markdown 原文**——markdown 真源还在，只是检索库不再保留最旧块的索引。

### 7.2 没有什么

- **无时间过期**：没有「30 天前的 daily 自动删」。`daily_memory/` 的旧文件会一直留，直到人为删或被 5c 取代。
- **无全局容量上限**：只有单文件 chunk 上限，没有「全库 N 条」的硬顶。
- **无重试调度**：embed 失败的 chunk 不重试，直到文件内容再变才重新索引。

### 7.3 未来：Phase 5c Dreaming

路线图里 Phase 5c「Dreaming」（记忆消化/巩固/遗忘）会取代 FIFO——做语义去重、合并、降权旧记忆。目前 5a 只把这个坑占住，机制极简。

---

## 8. 边界速查

| 边界 | 现状 |
|---|---|
| 嵌入维度 | 硬编码 1536（匹配 `text-embedding-3-small`），换模型/维度须删 `memory.db` 重建 |
| 无 API key | 降级 FTS-only，CJK 靠 jieba 词级（无则逐字空格）仍可召回 |
| embed 失败 | chunk 进 FTS 不进向量，不重试，待文件再变 |
| 自动淘汰 | 单文件 FIFO（>200 chunk 删最旧），不删 markdown |
| 写路由 | 纯 prompt 教模型往哪个文件写，代码不强制 |
| passive 注入 | opt-in（`memory.auto_inject.enabled`）：注入 `USER.md` + `MEMORY.md` + 今日 daily（cap，超限截断）；默认关=维持只策略 |

源文件索引：

| 组件 | 文件 |
|---|---|
| `MemoryManager`（存储+索引+检索+淘汰） | [memory/store.py](../../twinkle/agentserver/memory/store.py) |
| FTS 分词 + query 构造（jieba 可选/降级，抄 jiuwenswarm） | [memory/fts.py](../../twinkle/agentserver/memory/fts.py) |
| 嵌入 Provider（OpenAI 兼容 / Mock 测试） | [memory/embeddings.py](../../twinkle/agentserver/memory/embeddings.py) |
| 进程单例 + 构造配置 | [memory/__init__.py](../../twinkle/agentserver/memory/__init__.py) |
| `MemoryHook`（使用策略注入） | [hooks/builtin/memory_hook.py](../../twinkle/agentserver/hooks/builtin/memory_hook.py) |
| 4 个 `@tool` | [tools/builtin/memory_tools.py](../../twinkle/agentserver/tools/builtin/memory_tools.py) |
| 配置 schema | [config/schema.py](../../twinkle/config/schema.py) `MemoryConfig` |
| 配置默认值 | [resources/config.yaml](../../twinkle/resources/config.yaml) `memory:` |

---

## 9. 与参考实现（jiuwenswarm）的关系

Twinkle 的记忆子系统是对齐 jiuwenswarm `MemoryIndexManager`（[`jiuwenclaw/agentserver/memory/manager.py`](../../)）做的学习向裁剪。**核心架构一致**：markdown 是真源、`memory.db` 是派生检索索引、可从源文件整体重建——文档 §2.3 标注「删了可从 .md 重建」对 jiuwenswarm 同样成立（jiuwenswarm 的 `sync` → `_should_full_reindex` → `_run_reindex` 在 meta 缺失或 provider/model/chunkTokens 变更时全量重扫文件重建索引）。

差异集中在重建的触发方式和降级路径：

| 维度 | jiuwenswarm | Twinkle |
|---|---|---|
| 重建触发 | 文件监听(watchdog) + interval 定时 + onSearch 时 sync + model 变更 | 只在 `write_memory`/`edit_memory` 写入时 + model 变更 |
| 外部编辑 markdown | watchdog 自动重索引 | 不感知，下次工具写入才重索引（mtime 增量跳过未变文件） |
| 重建判据 | meta JSON：provider + model + chunkTokens | 只比对 model 名 |
| 第二数据源 | 还索引 `sessions/*.jsonl`（会话转录） | 只索引 memory markdown |
| sqlite-vec 不可用 | 内存 cosine 兜底（`_search_vector_fallback`） | 降级 FTS-only（CJK jieba 词级 / 无则逐字空格保召回） |

Twinkle 裁掉了 watchdog/interval/onSearch 三条外部触发，只留「工具写入即重索引」——单进程单事件循环、模型驱动写入场景下，外部编辑罕见，mtime 增量跳过已经够用；少一个文件监听器就少一份复杂度和并发风险。这是 5a 的刻意取舍，不是遗漏。
