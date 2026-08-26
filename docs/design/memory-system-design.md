# 长期记忆系统设计

> agent 的记忆怎么存、写、检索、改、过期、压缩前兜底、后台整理、防注入。源码在 [`twinkle/agentserver/memory/`](../../twinkle/agentserver/memory) + [`hooks/builtin/memory*.py`](../../twinkle/agentserver/hooks/builtin) + [`tools/builtin/memory_tools.py`](../../twinkle/agentserver/tools/builtin/memory_tools.py)。本子系统对齐 jiuwenswarm 记忆系统做了裁剪;dreaming 为自研(jiuwenswarm 对应是空壳)。

## 0. 全景

四个职责分离,**写入路径零 LLM**:

| 职责 | 组件 | 何时跑 | 做什么 | 不做什么 |
|---|---|---|---|---|
| **写入** | `write_memory` tool + `MemoryManager.write/edit/replace` | 模型主动调 | append/overwrite 落盘 markdown + 标 dirty 排程索引 | 不去重、不整理、不调 LLM |
| **检索** | `memory_search` tool + `MemoryManager.search` | 模型主动调 | hybrid 向量+FTS 召回 + top-N | 不修改记忆 |
| **兜底** | `MemoryFlushHook`（priority 96） | 上下文压缩前 | LLM 查"即将丢弃的 middle"有无未持久化重要信息 → `write_memory` 落盘 | 不做语义抽取（只兜底） |
| **整理** | `Dreaming` 后台 asyncio task | 周期 + busy-backoff | `MEMORY.md` 去冗余/消矛盾/去注入毒 + 从 daily 抽长期事实追加 + 容量 compact | 不动 daily 原料 |

数据流：

```
每步 ReAct:
  ├─ MemoryHook.before_invoke(80)  注 frozen_sections: 策略 prompt + 被动召回(USER.md+MEMORY.md)
  ├─ builder 把 frozen_sections 套进 system prompt
  ├─ MemoryFlushHook.before_model_call(96)  [仅压缩前跑]
  │    └─ should_compress? 否 → no-op
  │    └─ 是 → LLM 查 middle 有无该记未记 → 有则 write_memory 落盘
  └─ ContextCompressionHook(95) 压缩 middle

写入路径:
  write_memory / edit_memory / replace
    └─ store.write/edit/replace 落盘 markdown
         └─ _mark_dirty(标 dirty + 排程 Timer)        ← 不碰 DB 索引
              ├─ 后台 Timer 到期 → _flush_dirty 批量重索引
              └─ search 前 _flush_now → 立即索引保证可见

AgentServer 启动:
  └─ start_dreaming(llm, _get_inflight_count)  后台 task
       └─ await sleep(start_delay) → while True: await dream()
            dream() 门卫: enabled + llm 非空 + inflight==0
              ① scan_claims  ② filter_promotable  ③ append_promotions + save sidecar
              ④ consolidate(单次 LLM: 双类删行 {infectious, redundant})
              ⑤ compact_if_over_budget
```

Flush 在压缩前跑（priority 96 > 95），先把 middle 有价值信息落盘再丢弃。Dreaming 在后台跑，不碰 write/search 路径，不碰 daily。Flush opt-in 默认关；Dreaming 默认开（无 LLM 仍 no-op）。

---

## 1. agent 如何用记忆

记忆不自动塞进上下文——**模型自己决定调不调**。注入端只教它"何时搜、往哪写"，检索只发生在模型主动调 `memory_search` 时。

```
每步 ReAct:
  └─ MemoryHook.before_invoke(80)
       ├─ store 非空？否 → no-op
       └─ 是 → 往 ctx.extra["frozen_sections"] 注两个 PromptSection:
              ├─ memory_strategy(80): 策略 prompt(何时搜/写/不该写,见 §3 §6)
              └─ memory_static(81): USER.md + MEMORY.md 各按字符预算注入(超限 head+tail 截断)

builder 在 before_model_call 把 frozen_sections 套进 system prompt 前段（cache 友好）。

模型读到策略 prompt + 静态召回 + 4 个工具 schema,自己判断:
  ├─ 不需要历史 → 不调记忆工具
  ├─ 回答依赖跨会话事实 → memory_search(query)
  ├─ 需要今日/昨日记录 → memory_search('daily_memory/YYYY-MM-DD')
  ├─ 该记一笔 → write_memory(path, content, append)
  ├─ 要精确改某句 → edit_memory(path, old_text, new_text)
  └─ 要读某文件原文 → read_memory(path, offset, limit)
```

要点（[`memory_hook.py`](../../twinkle/agentserver/hooks/builtin/memory_hook.py)）：

- **注入走 `frozen_sections`**（`ctx.extra["frozen_sections"]` + `PromptSection(name, text, priority)`），由 builder 每步套进 system prompt 前段（cache 友好）。
- **被动召回（默认开）**：`memory.auto_inject.enabled=true` 时，`memory_static` 段把 `USER.md`（用户画像）+ `MEMORY.md`（持久事实）各按字符预算注入（`max_chars_user=4000` / `max_chars_memory=12000`）。超限各自 **head+tail 截断**（保首尾丢中间：首部=画像/核心偏好稳定，尾部=最近事实新，丢中间陈旧段）。关 = 只注策略。**daily 不自动注入**——需要时模型 `memory_search('daily_memory/<日期>')`（daily 走 tool message = 动态区，不占稳定前段）。
- **空 store 不注入。** 记忆目录里没文件时直接 return。
- **工具注册**在 [`tools/__init__.py`](../../twinkle/agentserver/tools/__init__.py) 的 `tool_manager()`，权限默认 `allow`。`MemoryHook` 在主 agent、子 agent、team agent 三个入口都挂上（priority 80）。

---

## 2. 记忆数据如何存储

记忆分两层：**markdown 是真源，SQLite 是派生检索索引**（删了可从 .md 重建）。

### 2.1 目录结构

默认 `<workspace>/.twinkle_data/memory`（`TWINKLE_MEMORY_DIR` 覆盖）：

```
memory/
├── USER.md                 # 用户画像：姓名/职业/沟通语言/操作系统/常用技术
├── MEMORY.md               # 持久事实：决策/偏好/项目约定/架构/技术选型（agent 直写 + dreaming 整理）
├── daily_memory/
│   └── 2026-08-10.md        # 当日笔记（append-only，dreaming 只读）
├── dreaming_state.json     # dreaming sidecar：已晋升集（raw pathlib 自管，不在 write 白名单）
└── memory.db               # SQLite 检索库（派生）
```

三类记忆文件分工，路由由策略 prompt 教模型（代码不强制）：

| 文件 | 写什么 | 何时写（prompt 规定） |
|---|---|---|
| `USER.md` | 用户个人信息 | 用户给了姓名/职业/沟通语言/OS/常用技术 |
| `MEMORY.md` | 决策/偏好/持久事实 | 项目约定、架构、技术选型、已做决定 |
| `daily_memory/YYYY-MM-DD.md` | 当日发生的事 | 用户说"记住这个"、运行上下文、当天事件 |

prompt 还规定**不该写**：临时数据、过程性状态（那是 todo 的活）、寒暄、本轮就过期的事。

`dreaming_state.json` 是 dreaming 的 sidecar，由 `dreaming.py` 用 raw pathlib 原子自管（不走 `mgr.write` 白名单）。

### 2.2 SQLite 检索库（`memory.db`）

`MemoryManager.__init__` 连接单库，`_ensure_schema` 建表。6 张表分两类：**主链三表**（一个 chunk 的内容/全文/向量三副本，共享 rowid）和**辅助三表**（指纹、缓存、元信息，各自独立）。

`check_same_thread=False` + `RLock(_db_lock)` + 独立 `Lock(_dirty_lock)`——去抖后 `_index_file` 跑在 timer 线程（`_flush_dirty`），与主线程 search/`_flush_now` 并发访问 SQLite，需重入锁互斥。

#### 各表作用

| 表 | 类别 | 作用 | 关键列 |
|---|---|---|---|
| `chunks` | 主链 | 分块内容主表，一个 chunk 一行，存原文+元数据+向量 BLOB | `id`(`path:start:end`, 主键) / 隐式 `rowid` / `path` / `source` / `start_line` / `end_line` / `hash` / `model` / `text` / `embedding`(BLOB) / `updated_at` |
| `chunks_fts` | 主链 | FTS5 全文索引，存 chunk 文本分词副本，供 bm25 搜 | 隐式 `rowid` / `text` |
| `chunks_vec` | 主链 | `sqlite-vec` 向量虚表，存 chunk 向量，供 cosine 搜（可选） | 隐式 `rowid` / `embedding float[1536]` |
| `files` | 辅助 | 文件级指纹（mtime/size/hash），供增量跳过 | `path`(主键) / `source` / `hash` / `mtime` / `size` |
| `embedding_cache` | 辅助 | 嵌入缓存，按 chunk 文本 md5 去重，同文不重复 embed | `hash`(主键) / `embedding` / `dims` / `updated_at` |
| `meta` | 辅助 | 元信息，存当前嵌入模型名，供模型变更重建判据 | `key`(主键) / `value` |

`chunks` / `chunks_fts` / `embedding_cache` / `files` / `meta` 总会建。`chunks_vec` 只在 `import sqlite_vec` + `load(db)` 都成功时建，置 `self._vec_enabled=True`；失败只记 warning，降级 FTS-only——**向量是可选增强，不是硬依赖**。

#### 表之间的关联：三条轴

6 张表的关联全靠代码维护一致性，无外键约束，三条独立轴：

```
                 ┌──── rowid 轴（一个 chunk 的三副本，强耦合）────┐
                 │                                                   │
  chunks(rowid) ◄─┼─► chunks_fts(rowid)   ◄─┐                      │
       │          │                          │ 同 rowid = 同 chunk  │
       │          └─► chunks_vec(rowid) ◄────┘                      │
       │ path                                                        │
       ▼                                                             │
  files(path)  ← 文件级指纹,驱动增量跳过(path 轴,弱耦合)               │
                                                                     │
  chunks.text ──md5──► embedding_cache(hash)  ← 内容去重缓存(hash 轴) │
                                                                     │
  meta(key='embed_model')  ← 全局元信息,驱动模型变更重建 ─────────────┘
```

**① rowid 轴（主链三表，强耦合）**——`chunks` / `chunks_fts` / `chunks_vec` 共享同一 `rowid`：往 `chunks` INSERT 拿 `lastrowid`，再用它往 `chunks_fts` 和 `chunks_vec` INSERT 对应的分词文本和向量。检索时 `_vec_search` 从 `chunks_vec` 拿 rowid+distance 回 `chunks` 取原文/路径/行号；`_fts_search` 同理 FTS 拿 rowid 后 JOIN `chunks` 取详情。删旧块先查 `chunks` 的 rowid 列表，再三表 `DELETE WHERE rowid IN (...)`。rowid 是承重墙，chunks 是主，FTS/vec 是侧索引。

**② path 轴（files ↔ chunks，弱耦合）**——`files` 按 `path` 存指纹，`chunks` 每行带 `path`。`_index_file` 先查 `files WHERE path=?` 比指纹决定跳不跳过，变了就 `DELETE FROM chunks WHERE path=?` 删该文件全部旧块。`files` 是"上次索引成什么样"的缓存，`chunks` 是"现在索引出了哪些块"的事实表，靠 path 对齐，`_index_file` 一个事务里同时维护两边。

**③ hash 轴（embedding_cache ↔ chunks.text，弱耦合）**——`embedding_cache` 按 chunk 文本 md5 存向量，和 `chunks` 无直接字段关联（不存 chunk_id），靠"同文本同 hash"内容寻址。`_embed_chunks` 把要 embed 的文本 md5 逐个查缓存，命中直接取 BLOB，未命中才调 embedding API。好处是**跨文件去重**：完全相同的文字只 embed 一次。坏处是删 chunk 不清对应缓存（靠重建清理）。

`meta` 不参与关联轴，独立键值对，存 `embed_model`，`_clear_if_model_changed` 启动时读它判模型变没变。

`_index_file` 写入时靠一个事务保证主链三表 + files 的 rowid/path 一致性：任一插入失败（如 vec0 维度不匹配）整个 `rollback()`，不会出现"`chunks` 有行但 `chunks_fts` 没行"的孤儿。

### 2.3 `memory.db` 如何被构建

`MemoryManager` 是进程级懒单例：`get_memory_manager()`（[`memory/__init__.py`](../../twinkle/agentserver/memory/__init__.py)）所有 `@tool` 函数和 `MemoryHook` 都走它，首次调用才构造，避免 import-time 连库/连 embedding API 的副作用；测试用 `_set_memory_manager(mgr)` 换桩。构造时从 config 读全部参数（分块大小、融合权重、候选放大倍数、单文件 chunk 上限、去抖窗口等）。

库是**懒构造 + 逐文件增量**建起来的：

```
进程启动 → AgentServer 起来,MemoryManager 还没构造(懒)

首次需要记忆(MemoryHook.before_invoke,或模型调了某 memory_* 工具)
  └─ get_memory_manager() 首次调用
       └─ MemoryManager.__init__:
            1. mkdir memory/ + memory/daily_memory/
            2. sqlite3.connect(memory.db, check_same_thread=False)  # 文件不存在则新建
            3. _ensure_schema()                   # 建表
                 ├─ chunks / chunks_fts / embedding_cache / files / meta  # 必建
                 └─ 尝试建 chunks_vec (vec0)       # sqlite_vec 可装则建+置 vec_enabled
                                                      失败 → warning,降级 FTS-only
            4. _clear_if_model_changed()
                 └─ provider 在 且 meta.embed_model 与当前 model 不符 → 清空全表
                 └─ 否则(含空库)no-op

此刻 memory.db:表都建好,但 chunks 为空——没有任何检索内容。
```

**`__init__` 不扫描已有 markdown 文件**。schema 是懒构造时建好的空壳，内容什么时候进库见 §3–§4。

---

## 3. 记忆如何写入

入口是 `write_memory` 工具 → `MemoryManager.write`：

```
write(path, content, append)
  1. _resolve_relative_path(path)     # 白名单校验,非法返回错误串
  2. 打开文件 append("a") 或覆盖("w") 写入
     └─ append 且内容不以 \n 结尾 → 补 \n
  3. _mark_dirty(relative_path)        # 标 dirty + 排程去抖 Timer（不碰 DB 索引）
  4. 返回 "Stored to {relative_path}."
```

`write_memory(path, content, append=False)` 默认覆盖整文件；`append=True` 追加。写入触发完全靠模型读策略 prompt 主动调——代码层没有"检测到关键词就自动写"的逻辑。

### 3.1 路径白名单

`_resolve_relative_path` 是写入/读取/改写的统一前置校验（[`store.py`](../../twinkle/agentserver/memory/store.py)）：

- 只放行 `USER.md` / `MEMORY.md`（根）或 `daily_memory/YYYY-MM-DD.md`（日期须匹配 `^\d{4}-\d{2}-\d{2}\.md$`）。
- `is_relative_to(self._dir)` 防路径穿越。
- 不合法返回错误串（不抛异常）——`write` / `read` / `edit` / `replace` 都返回 `Error: invalid memory path ...`，模型拿到 tool_result 自己改。

### 3.2 写入触发索引，但经去抖（非同步）

`write` / `edit` / `replace` 落盘后只 `_mark_dirty` 标 dirty + 排程后台 Timer，**不碰 DB 索引**。索引由 search 兜底或 Timer 异步做（见 §4）。

实际后果：直接往 `memory/` 目录丢现成的 `USER.md` / `MEMORY.md` 再启动 → `MemoryHook` 仍注入策略 prompt + 静态召回（`list_files()` 扫文件系统发现有 `.md` 就注入）；但 `memory_search` 召回为空——因为这些文件没经过 `_index_file`，DB 里没 chunks/向量。要让"外部塞进来的"文件进检索库，需等模型对它们调一次 `write_memory`/`edit_memory`（触发 `_mark_dirty` → Timer/search 兜底索引）。Twinkle 无 watchdog，不自动感知外部编辑重索引。

---

## 4. 写入后如何索引：去抖 + 兜底

### 4.1 去抖机制（`_mark_dirty` + Timer）

`write` / `edit` / `replace` 落盘后调 `_mark_dirty(relative_path)`（[`store.py`](../../twinkle/agentserver/memory/store.py)）：

- 把 `relative_path` 加入 `_dirty_paths` 集（去重）。
- 取消 pending Timer（若有），重新排程 `threading.Timer(self._debounce, self._flush_dirty)`（默认 `index_debounce_seconds=2.0`）。
- **写入路径不碰 DB**：零 embedding API、零 SQL。

Timer 到期后 `_flush_dirty` 在 timer 线程批量重索引 dirty 文件（逐个 `_index_file`，文件级 hash 跳过未变）。连续写塌成一次重索引。

### 4.2 search 兜底（`_flush_now`）

`search()` 开头：`if self._dirty_paths: self._flush_now()`——主线程同步重索引所有 dirty 文件，立即保证搜到刚写的内容。`_flush_now` 先 `_drain_dirty`（取清 dirty 集 + 取消 pending Timer，原子在 `_dirty_lock` 内防重复索引），再逐个 `_index_file`。

### 4.3 并发与锁

- `check_same_thread=False`：Timer 线程能共享连接。
- `_db_lock`（`RLock`，重入）：`_index_file` 持锁调 `_embed_chunks`/`_evict_excess_chunks`；timer 线程 `_flush_dirty` 与主线程 search/`_flush_now` 并发时互斥。文件 stat/read/hash 在锁外。
- `_dirty_lock`（`Lock`）：保护 `_dirty_paths` + `_sync_timer`，跨线程（主线程 add / timer 线程 drain）。

### 4.4 `_index_file` 内部

`_index_file(relative_path)` 把"markdown 文件"变成"可检索 chunks/FTS/向量"，被 `_flush_dirty`/`_flush_now` 调用：

```
_index_file(rel):
  1. 读文件 stat(mtime/size) + 全文,算 file_hash = md5(content)      [锁外]
  2. 查 files 表旧指纹；mtime + size + hash 三重都一致 → return（未变,跳过）
  3. 持 _db_lock 开事务(try):
     a. 删旧块:查 chunks 表该 path 的所有 rowid,
        DELETE FROM chunks WHERE path=?
        DELETE FROM chunks_fts WHERE rowid IN (旧rowid)
        DELETE FROM chunks_vec WHERE rowid IN (旧rowid)   # 仅 vec_enabled
     b. 分块:_chunk(content) → [Chunk(start, end, text), ...]
     c. 嵌入:want_vec(vec_enabled 且 provider 在)时 _embed_chunks(texts)
        └─ 先查 embedding_cache 命中、未命中批量调 provider.embed 并写回缓存
        └─ 失败:返回 [None,...],这些块仍进 FTS,不进向量,不重试
     d. 逐块写库(chunks 主表 INSERT,拿 rowid):
        └─ chunks_fts INSERT(rowid, tokenize_for_fts(text, True))
        └─ want_vec 且 blob 非 None:chunks_vec INSERT(rowid, embedding)
     e. UPSERT files(path, source, hash, mtime, size)
     f. UPSERT meta(embed_model = provider.model)
     g. _evict_excess_chunks(rel)  # 单文件 chunk 超 200 FIFO 删最旧
     h. commit()
  4. 任一步异常 → rollback() + raise
```

**增量跳过（步骤 2）**：三重指纹（mtime + size + content md5）避免"mtime 没变但内容变了"或"size 没变但内容变了"的假阴性。命中跳过是最高频出口。

**分块（步骤 3b）**：`_chunk` 按行切，单块预算 `chunk_tokens*3` 字节（默认 256 token ≈ 768 字节），相邻块共享 `chunk_overlap*3` 字节的尾部回溯行（向回走直到 overlap 预算填满，保证连续块语义衔接）。每块带 `start_line`/`end_line`，召回回带行号。

**嵌入缓存 + 失败兜底（步骤 3c）**：`_embed_chunks` 先按 chunk 文本 md5 查 `embedding_cache`，命中直接用；未命中的批量调 `provider.embed([...])`，写回缓存。embed 抛异常时这些 chunk **仍进 FTS**（不写向量），记 warning，**不重试**——下次该文件内容再变（经去抖/兜底触发 `_index_file`）才重试。模型变更：`__init__` 末尾 `_clear_if_model_changed` 对比 `meta.embed_model` 与当前 provider.model，不一致就清空所有表（不重建，下次索引触发才用新模型重灌）。

**事务边界（步骤 3 整体）**：删旧块插新块整段包在 `try/except`，失败 `rollback()` 后 `raise`，防中途失败（如 vec0 维度不匹配 INSERT）让未提交事务泄漏到单例连接的下一次写。

---

## 5. 记忆如何检索

入口是 `memory_search` 工具 → `MemoryManager.search`：

```
search(query, max_results)
  if self._dirty_paths: self._flush_now()          # 兜底:保证搜到刚写的内容
  candidates = min(200, max(1, max_results * 2.0))  # 候选放大
  持 _db_lock:
    fts = _fts_search(query, candidates)            # FTS5 bm25
    无 vec 或无 provider?
      是 → 直接返回 fts[:max_results]（FTS-only）
      否 ↓
    vec = _vec_search(query, candidates)            # sqlite-vec cosine
    候选 = 两路并集(fts 行已带正文,vec-only 去主表回填)
    对每个候选算 fused = vector_weight*vec_sim + text_weight*_text_sim(bm25)
      (默认 0.7*v + 0.3*text)
    按 fused 降序,取 top-N
  返回 [{path, score, text, start_line, end_line}, ...]
```

**向量腿**：`_vec_search` 调 `provider.embed([query])` 得查询向量，`chunks_vec MATCH ? ORDER BY distance LIMIT ?` 取最近邻。cosine 距离 ∈ [0,2]，映射成相似度 `1 - distance/2`，clamp [0,1]。查询 embedding 失败时这条腿 skip，不抛。

**FTS 腿 + bm25 归一化**：`_fts_search` 用 `build_fts_query(query)` 构造 MATCH 串（每 token 包引号 + **OR 连接**，前 10 token，任一 token 命中即记分）+ `chunks_fts MATCH ?` + `ORDER BY bm25(chunks_fts)`（bm25 越负越相关）。`_text_sim(bm)` 把非正值映射到 [0,1]：`a=abs(bm); a/(1+a)`，最相关→接近 1.0，与向量相似度同量纲融合。OR 连接保证换措辞的自然语言 query 也能召回（不会因 token 不按序连续而 0 命中）。

**无分数截断**：只排序 + top-N，不靠魔法阈值砍结果。召回质量交给候选放大（2 倍）、融合权重、top-N 控制。低质结果也进 top-N，由模型自行判断取舍。

### CJK 分词：jieba 可选 + 降级逐字空格

FTS5 的 `unicode61` 分词器不切 CJK，中文查询会召回失败。分词逻辑在 [`fts.py`](../../twinkle/agentserver/memory/fts.py)，双路径：

- **有 jieba**（装 `[memory]` extra）：索引走 `jieba.cut_for_search`（细粒度提召回），查询走 `jieba.cut`（粗切），再过停用词（`stopwords_zh.txt` 793 词）——词级召回质量高。
- **无 jieba**：降级 `_space_cjk` 逐字空格（每个汉字前后加空格让 unicode61 切单字 token）+ OR——召回率够用但单字语义弱。

无 API key 时检索降级为 FTS-only，`_embed_chunks` 失败的 chunk 不嵌向量只进 FTS，这时 CJK 召回全靠分词。

### 降级矩阵

| 条件 | 行为 |
|---|---|
| `sqlite-vec` 装了 + provider 有 | 混合检索（vector+FTS 融合） |
| `sqlite-vec` 没装 / load 失败 | FTS-only（bm25 排序） |
| 无 `LLM_API_KEY`（provider=None） | FTS-only（写时不嵌向量） |
| 查询 embedding 失败 | 向量腿 skip，退回 FTS-only |
| 无 jieba（未装 `[memory]` extra） | FTS 仍工作，CJK 降级逐字空格 + OR |

任何一腿失败都不让整次 search 崩——`search` 总返回一个 list（可能空）。jieba 有无只影响 CJK 召回质量，不影响检索是否工作。

---

## 6. 记忆如何更新

更新有三条路：

### 6.1 精确改：`edit_memory`

`edit_memory(path, old_text, new_text)` → `MemoryManager.edit`：

1. 白名单校验 + 文件存在校验
2. 读全文，`old_text not in text` → 返回错误串
3. `text.replace(old_text, new_text, 1)`（只替换**首次**出现）
4. 写回，`_mark_dirty(rel)` 排程重索引

用途：模型 recall 到与当前信息矛盾的记忆时，用 `edit_memory` 修正它。比如用户改了操作系统偏好，模型把 `USER.md` 里旧 OS 那句换掉。

### 6.2 整文件重写：`write_memory(append=False)`

覆盖整文件。用于重写杂乱的 `daily_memory/YYYY-MM-DD.md`，或整段重写 `MEMORY.md` 的某区块（配合先 `read_memory` 读出、改完整体写回）。

### 6.3 原子全量覆写：`MemoryManager.replace`（dreaming 用）

`replace(path, content)` 写 temp 文件再 `os.replace`（同文件系统 rename 原子），防 torn write。dreaming 的 consolidate（删行写回）和 compact（删行写回）共用。agent 不直接调（无对应 `@tool`，是 manager 内部方法）。三条路最后都汇到 `_mark_dirty` → 去抖/兜底索引。

---

## 7. 记忆如何过期

### 7.1 单文件 chunk 上限（FIFO 淘汰）

`_evict_excess_chunks` 在每次索引后检查该文件 chunk 数是否超 `max_chunks_per_file`（默认 200），超限按 `updated_at ASC` 删最旧的（FIFO）：

```
chunk_count = SELECT COUNT(*) FROM chunks WHERE path=?
if chunk_count > 200:
    num_to_evict = chunk_count - 200
    oldest_rowids = SELECT rowid FROM chunks WHERE path=? ORDER BY updated_at ASC LIMIT num_to_evict
    DELETE FROM chunks / chunks_fts / chunks_vec WHERE rowid IN (oldest_rowids)
```

淘汰的是检索块，**不删 markdown 原文**——markdown 真源还在，只是检索库不再保留最旧块的索引。

### 7.2 没有什么

- **无时间过期**：没有"30 天前的 daily 自动删"。`daily_memory/` 旧文件一直留，直到人为删。
- **无全局容量上限**：只有单文件 chunk 上限，没有"全库 N 条"的硬顶。
- **无重试调度**：embed 失败的 chunk 不重试，直到文件内容再变（经去抖/兜底触发 `_index_file`）才重新索引。

### 7.3 Dreaming compact

MEMORY.md 超 `max_memory_chars`（默认 10000）时，dreaming 的 `_compact_if_over_budget` 分阶段丢行（见 §9.5）。这是 MEMORY.md 文件级容量兜底，独立于 §7.1 的检索块淘汰。

---

## 8. Memory Flush（压缩前兜底）

[`memory_flush_hook.py`](../../twinkle/agentserver/hooks/builtin/memory_flush_hook.py)。`MemoryFlushHook` priority 96（> `ContextCompressionHook` 95，先跑），`before_model_call`，`__init__(self, llm)` 构造注入。

### 8.1 触发逻辑

1. `MEMORY_FLUSH_ENABLED` 关 → return（opt-in 默认关）。
2. 无 LLM（`self._llm is None`）→ return。
3. 复用 `compression.should_compress(msgs, token_threshold, keep_recent_pairs)` 判定：**false → return**（无压缩 = 无丢弃 = 无兜底必要，不是每步调 LLM）。
4. true → `split_messages_head_middle_tail(msgs, tail_count)` 取 middle，`_render_messages_text(middle)` 渲染，调 `_flush(middle)`。
5. fail-soft：任何异常 `log.exception` + 不崩（兜底是优化非承重，不阻断压缩/对话）。

### 8.2 兜底 ≠ 抽取

LLM 读 middle 全文（**含其中的 `write_memory` tool_call 历史**），输出"重要且**未被 `write_memory` 覆盖**的信息"——已写的排除，避免二次抽取。输出 JSON 数组，程序 `mgr.write` 落盘（path 白名单由 `store.write` 把）：

```json
[{"path":"MEMORY.md","content":"...","append":true}]
```

无漏 → `[]`，不写。重复写入的善后交给后台 Dreaming。

### 8.3 LLM 调用 + prompt

`llm.stream(messages=[{system:_FLUSH_PROMPT},{user:middle_text}], tools=[])` + 收 `TextDelta`。`_FLUSH_PROMPT` **硬编码进模块常量**（JSON 契约，不进 config——用户改坏 → 解析失败 → 兜底静默失效）。JSON 解析失败 → 不写 + log。`_flush` 返回 `(new_writes, errors)`。

### 8.4 三入口注册

- `server.py` `create_agent` auto-wire `MemoryFlushHook(llm=llm)`。
- `executor.py`（子 agent）`_hook_list` 加 `MemoryFlushHook(llm=self._llm)`。
- `manager.py`（team member）`_build_member` 加 `MemoryFlushHook(llm=self._llm)`。

子/team agent 上下文小不压缩时 `should_compress=false` 自动 no-op。

---

## 9. Dreaming（后台整理）

[`memory/dreaming.py`](../../twinkle/agentserver/memory/dreaming.py)。后台 asyncio task 周期整理 `MEMORY.md`：扫 `daily_memory` 非空行 → claimHash 跨文件聚合 → 确定性门槛晋升进 `MEMORY.md` → 单次 LLM 整合（删冗余/矛盾/注入毒）→ 容量 compact。daily append-only 只读。**晋升零 LLM**，整合 LLM 在后台不进写入关键路径。

### 9.1 触发

`start_dreaming(llm, get_inflight)` 在 AgentServer `main()` 起后台 task（`asyncio.create_task`），随进程生命周期。`enabled + llm` 都满足才起，否则返回 None。`run_loop`：

```
await asyncio.sleep(start_delay_seconds)   # 默认 300s,避免启动即整理
while True:
    try:
        await self.dream()
    except Exception:
        log.exception                        # fail-soft,不崩循环
    await asyncio.sleep(interval_seconds)    # 默认 3600s
```

`get_inflight` = `server.py` 的 `_get_inflight_count`（模块级 `_inflight_count`，`run_task` 进来 +1 / `finally` -1）。**前台忙（inflight > 0）则跳过本轮**，免得和 agent 抢着写 MEMORY.md。

### 9.2 dream() body

门卫（`MEMORY_DREAMING_ENABLED` 开 + `llm` 非空 + `inflight==0`）过后五步：

```
① claims = _scan_claims(mgr)              # 扫 daily 非空行,md5 跨文件聚合
② promotion_state = _load_state(sidecar)  # 读已晋升集
③ candidates = _filter_promotable(claims, promotion_state)  # 门槛筛(纯函数)
④ if candidates:
     _append_promotions(mgr, candidates, promotion_state)  # 批量 append + 记 sidecar
     _save_state(promotion_state, sidecar)                 # 落盘防下 tick 重搬
     await _consolidate(mgr)                                # 单次 LLM 删行整合
⑤ _compact_if_over_budget(mgr, promotion_state)           # 无论晋升与否都跑
```

### 9.3 晋升步（零 LLM，确定性）

**`_scan_claims`** — 扫 `daily_memory/*.md` 所有非空行，`claimHash = md5(line.strip())` 跨文件聚合。返回 `{hash: {"text": stripped_line, "source_files": set, "first_path": str}}`。文件内同行 = 1 claim；跨文件同 claim 累积 `source_files`。daily append-only 只读。

**`_filter_promotable`** — 门槛：`len(source_files) >= MEMORY_DREAMING_MIN_DISTINCT_FILES`（默认 2）**AND** `hash ∉ promotion_state["promoted"]`。

- "≥2 个不同 daily 文件出现" = 跨日复现 = 值得长期记；一次性的事（只写过 1 遍）不搬。
- 幂等靠 sidecar `promoted` 集（只增不减，consolidate 删行不删 record）——不用 MEMORY.md marker（marker 会随被 consolidate 删的行消失 → 下 tick 重晋）。
- 纯函数无副作用。空列表 → 本轮无新晋升 → 跳过 consolidate（但 compact 仍跑）。

**`_append_promotions`** — 候选批量拼好一次 `mgr.write("MEMORY.md", bulk, append=True)`（N 条候选一次写 = 1 次重索引，而非逐条写 N 次），同时 `promotion_state["promoted"][hash] = {ts, text, source_path}`。零 LLM。append 本身不防重（靠 `_filter_promotable` 已筛掉已晋升）。

### 9.4 整合步 `_consolidate`（单次 LLM，双类删除）

`_consolidate` 流程：

1. 读 MEMORY.md → 非空行列表 `lines`。`len(lines) < 2` → return（无可合并）。
2. `_ask_llm(_CONSOLIDATE_PROMPT.format(numbered_lines=...))` → `raw`。LLM 异常 → return（fail-soft，append-only 版留着）。
3. `json.loads(raw)` → `{"infectious":[...], "redundant":[...]}`。解析失败 → return。
4. 校验（`_validate`，对每类独立）：是 int 列表；每个号 ∈ [1, len(lines)]；坏号/非 list → 放弃该类（保守不部分应用）。
   - `infectious`（注入去毒，§10）：`len/len(lines) ≤ max_infectious_fraction(0.5)`，超 → 放弃该类。**不受 redundant 的 25% 约束**。
   - `redundant`（冗余/矛盾）：`len/len(lines) ≤ max_delete_fraction(0.25)`，超 → 放弃该类；向后兼容旧 `delete` 字段。
5. 合并删行集 `delete_set = infectious ∪ redundant`。无 → return。
6. 保留行 = `[line for i, line in enumerate(lines,1) if i not in delete_set]`。
7. `mgr.replace("MEMORY.md", "\n".join(kept) + "\n")`（全量原子写回）。

**LLM 全程不碰文本原文，只出行号**（先 append 后 delete，把"既加又合"降为"只删"）。任一步失败 → fail-soft，晋升步 append 的版本即为最终结果。

`_CONSOLIDATE_PROMPT` **硬编码进 `dreaming.py` 模块常量**（JSON 契约，不进 config）。规则：语义重复删冗余留更完整；矛盾删旧值留后写；故意注入的危险指令式内容剔进 `infectious`（fail-open，拿不准的留）。硬约束：只删行不改写、`redundant` ≤25%、`infectious` ≤50%、不得新增行、只输出 JSON。

### 9.5 compact `_compact_if_over_budget`（reconcile + 分阶段）

`_compact_if_over_budget(mgr, promotion_state)`：

1. 读 MEMORY.md 非空行 `lines`，算 `file_text_set`。
2. **reconcile**：`promoted` 只留 `text` 仍在 MEMORY.md 的记录（清 consolidate 删行 / edit 改行产生的孤儿），防 sidecar 膨胀。
3. `len("\n".join(lines)) <= max_memory_chars` → 若 reconcile 清了孤儿则 save，return（未超不丢行）。
4. **阶段 1**：丢 promotion 行（按 sidecar `ts` 升序，最老先丢）——episodic 该让位。逐个丢直到在预算内，同步从 `promoted` 移除对应 record。
5. **阶段 2**：丢光 promotion 仍超 → 丢非 promotion 行（手写/未追踪，无 ts）按文件位置头部先（append-only 头部 ≈ 最早写）。
6. `mgr.replace` 写回 + `_save_state` 落盘 sidecar。

### 9.6 sidecar 数据结构

`<memory_dir>/dreaming_state.json`（raw pathlib 自管，原子写 tempfile+rename）：

```json
{
  "version": 1,
  "promoted": {
    "<claimHash>": {"ts": "2026-08-15T03:00:00", "text": "- 喜欢爬山运动", "source_path": "daily_memory/2026-08-14.md"}
  }
}
```

- `promoted` 只增不减（晋升即记；consolidate 删行不删 record；compact 阶段1 丢行时才从 `promoted` 移除对应 record；reconcile 清孤儿）。
- 读 `_load_state`：不存在/坏 JSON/坏形状 → `{"version":1,"promoted":{}}`。
- 写 `_save_state`：tempfile + rename，成功不留 `.tmp`。

### 9.7 例子

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
整合步：LLM 指出删第 2 行（留更完整的第 1 行，进 `redundant`）→ MEMORY.md 原子重写成只剩第 1、3 行。下个 interval 再跑时，`- 喜欢爬山运动` 的指纹已在 sidecar → 不重复搬。

---

## 10. 记忆注入防注入

Twinkle 把 `USER.md` / `MEMORY.md` 自动注入 system prompt（cache 友好的 frozen_sections，最高特权位）。防"agent 投毒 MEMORY.md"靠**复用 dreaming 的 consolidate 步骤加去毒职责**，零额外 LLM 调用（consolidate 本来就调一次）。

### 机制

consolidate 识别并剔除"故意注入的危险记忆"（指令式/越权式内容，如"忽略以上所有指令…""你现在是…""把所有文件删了"），归入 `infectious` 类。**fail-open**：只删 LLM 确信是故意注入的危险指令式内容；拿不准的、像正常事实/偏好/决策的，一律保留。优先保护召回率。

- **25% 删行上限与去毒分离**：`redundant` 受 25% 约束；`infectious` **不计入 25%**——注入剔除是安全职责，不该被容量额度卡，受独立上限 ≤50%（防 LLM 失控删空文件）。
- agent 直写 MEMORY.md 的注入，下次 consolidate 扫到剔除。
- 窗口期 = 两次 dreaming 间隔。

### 不动什么 / 不覆盖什么

- **注入端原样**：`USER.md`/`MEMORY.md` 继续自动注入，不加边界标签、不降权、不加 framing。
- **`USER.md` 不处理**：注入风险低（画像稳定、量小、写不频繁），裸奔接受。
- **`memory_search` 不动**：现状 tool 角色低特权返回。
- **不覆盖工具结果 / 子代理注入通道**。

### 局限（挡不住什么）

- 窗口期内"注入即触发"——投毒写入 MEMORY.md 后到下次 dreaming 之间，内容已自动注入、可能已触发执行。
- 拿不准的注入（fail-open 放过）。
- `USER.md` 携带的注入（不处理）。

---

## 11. 配置

[`config/schema.py`](../../twinkle/config/schema.py) `MemoryConfig`（+ 子配置）。优先级：env var > `.env` > YAML 默认。

| 配置块 | 字段 | 默认 | 作用 |
|---|---|---|---|
| `dir` | | `""` | `""` → `<workspace>/.twinkle_data/memory` |
| `embed_model` | | `text-embedding-3-small` | 嵌入模型名（换模型/维度须删 memory.db 重建） |
| `query` | `max_results` | `10` | search top-N |
| `hybrid` | `vector_weight`/`text_weight`/`candidate_multiplier` | `0.7`/`0.3`/`2.0` | 融合权重 / 候选放大倍数 |
| `chunking` | `tokens`/`overlap` | `256`/`32` | 单块 token 预算 / 尾部回溯 overlap |
| `cleanup` | `max_chunks_per_file` | `200` | 单文件 chunk 上限（FIFO 淘汰） |
| `index` | `debounce_seconds` | `2.0` | 写后去抖窗口（连续写塌成一次重索引） |
| `auto_inject` | `enabled` | `True` | 被动召回开关（注 USER.md+MEMORY.md；关=只策略） |
| | `max_chars_user`/`max_chars_memory` | `4000`/`12000` | 分文件注入预算（超限 head+tail 截断） |
| `flush` | `enabled` | `False` | 压缩前兜底开关（opt-in；prompt 硬编码不进 config） |
| `dreaming` | `enabled` | `True` | 后台整理开关（默认开；无 LLM 仍 no-op） |
| | `interval_seconds`/`start_delay_seconds` | `3600`/`300` | 整理周期 / 首跑延迟 |
| | `top_k` | `5` | 聚类相似召回数（备未来 search 用） |
| | `min_distinct_files` | `2` | 晋升门：同一事实须出现在 ≥N 个不同 daily 文件 |
| | `max_memory_chars` | `10000` | MEMORY.md 容量预算，超限 compact |
| | `max_delete_fraction` | `0.25` | consolidate `redundant` 删除行数上限比例 |
| | `max_infectious_fraction` | `0.5` | consolidate `infectious` 注入剔除上限（不受 25% 约束） |

带严格 JSON 输出契约的 prompt（flush `_FLUSH_PROMPT`、dreaming `_CONSOLIDATE_PROMPT`）**硬编码进业务模块常量，不进 config**——用户改坏 → 解析失败 → 静默失效。

---

## 12. 边界速查

| 边界 | 现状 |
|---|---|
| 嵌入维度 | 硬编码 1536（匹配 `text-embedding-3-small`），换模型/维度须删 `memory.db` 重建 |
| 无 API key | 降级 FTS-only，CJK 靠 jieba 词级（无则逐字空格）仍可召回 |
| embed 失败 | chunk 进 FTS 不进向量，不重试，待文件再变（去抖/兜底触发重索引） |
| 写入路径 LLM | **零**（write/edit/replace 只 `_mark_dirty`，不碰 DB 索引/不调 LLM/不调 embedding） |
| 索引触发 | 后台 Timer（去抖 2s 批量）+ search 兜底（`_flush_now` 同步） |
| 自动淘汰 | 单文件 FIFO（>200 chunk 删最旧检索块，不删 markdown）+ dreaming compact（MEMORY.md >10000 字符丢行） |
| 写路由 | 纯 prompt 教模型往哪个文件写，代码不强制 |
| 被动注入 | 默认开：USER.md+MEMORY.md 分预算 head+tail 截断；daily 不自动注入（需 memory_search）；关=只策略 |
| Flush | opt-in 默认关；should_compress=false 自动 no-op；fail-soft |
| Dreaming | 默认开（无 LLM 仍 no-op）；busy-backoff（inflight>0 跳过）；fail-soft |
| 注入防注入 | dreaming consolidate 事后去毒 fail-open，USER.md 裸奔，窗口期内不防 |
| 外部编辑 markdown | 不感知，无 watchdog，下次工具写入（去抖/兜底）才重索引 |

---

## 13. 源文件索引

| 组件 | 文件 |
|---|---|
| `MemoryManager`（存储+索引+检索+淘汰+去抖） | [memory/store.py](../../twinkle/agentserver/memory/store.py) |
| FTS 分词 + query 构造 | [memory/fts.py](../../twinkle/agentserver/memory/fts.py) |
| 嵌入 Provider（OpenAI 兼容 / Mock 测试） | [memory/embeddings.py](../../twinkle/agentserver/memory/embeddings.py) |
| `DreamingOrchestrator`（后台整理 + 注入去毒） | [memory/dreaming.py](../../twinkle/agentserver/memory/dreaming.py) |
| 进程单例 + 构造配置 | [memory/__init__.py](../../twinkle/agentserver/memory/__init__.py) |
| `MemoryHook`（策略注入 + 被动召回） | [hooks/builtin/memory_hook.py](../../twinkle/agentserver/hooks/builtin/memory_hook.py) |
| `MemoryFlushHook`（压缩前兜底） | [hooks/builtin/memory_flush_hook.py](../../twinkle/agentserver/hooks/builtin/memory_flush_hook.py) |
| 4 个 `@tool`（memory_search/write/read/edit） | [tools/builtin/memory_tools.py](../../twinkle/agentserver/tools/builtin/memory_tools.py) |
| Dreaming task 启停 + inflight 计数 | [agentserver/server.py](../../twinkle/agentserver/server.py) |
| 配置 schema（`MemoryConfig` + 子配置） | [config/schema.py](../../twinkle/config/schema.py) |
| 配置默认值 | [resources/config.yaml](../../twinkle/resources/config.yaml) `memory:` |
