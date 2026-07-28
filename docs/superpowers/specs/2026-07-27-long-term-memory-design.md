# Phase 5a — 长期记忆（核心 4 工具 + prompt 注入）设计

- 状态：设计稿，待评审
- 日期：2026-07-27
- 对应 roadmap：Phase 5（长期记忆），拆为 5a / 5b / 5c；本 spec 范围 = **5a**
- 参考实现：jiuwenswarm `enterprise_dev` 分支（源码锚点见 §9）

---

## 1. 范围

**5a（本 spec，做）**
- 存储：markdown 权威源 + SQLite（`memory.db`，6 表）+ sqlite-vec 向量 + FTS5 全文 + embedding 缓存
- 4 个 `@tool`：`memory_search` / `write_memory` / `read_memory` / `edit_memory`
- `MemoryHook`（before_model_call 注入使用策略 prompt，5a 只 proactive 一档）
- embedding provider（OpenAI 兼容；无 key 降级 FTS-only，Mock 仅测试 helper）
- **纯 model-driven 写入 / 查询**（召回不自动注入 messages）

**5b（deferred）**：auto-extraction 子 agent——对话结束后 hook 派 cache-sharing 子 agent 自动从对话提取事实写入（+ 与模型显式写的互斥）。roadmap 把"wiki LLM 子 agent 索引"列在 Phase 5 不做项，auto-extraction 算这类。

**5c（deferred）**：Dreaming sweeper——定时（默认 4h）扫会话转录 → 压缩 → LLM 提炼 → 晋升 `DREAMING.md`（FIFO cap 50）+ sessions 入池索引。

> 5a 记忆池**只含显式写入的 `memory/*.md`**，不含会话转录（sessions 不入池）：会话原文提炼是 5c Dreaming 的活，5a 没 Dreaming、直接入池既噪又与 5c 撞车。会话历史仍在 `SessionStore`（Phase 1）+ 上下文压缩（Phase 3）里。

> **已知 tradeoff**：5a 记忆能否被用起来取决于模型主动调 `write_memory`——模型不调则记忆空、feature 形同虚设。auto-extraction 子 agent（5b）补这个口。`write_memory` / `memory_search` 各打一条 INFO 日志，便于上线后观测"记忆到底有没有被用"。

---

## 2. 架构总览

三个新模块，全部照现有 `skills` / `skill_tools` / `SkillHook` 同构，`agent_loop` **零结构改动**：

```
twinkle/agentserver/
├─ memory/                       ← 新包（照 skills/ 形态）
│   ├─ __init__.py               ← get_memory_manager() 单例 + _set_memory_manager() 测试钩子
│   ├─ store.py                  ← MemoryManager：SQLite schema + search/write/read/edit + mtime 增量索引
│   └─ embeddings.py             ← OpenAICompatibleEmbeddingProvider + MockEmbeddingProvider
├─ tools/builtin/memory_tools.py ← 新：4 个 @tool（memory_search/write_memory/read_memory/edit_memory）
└─ hooks/builtin/memory_hook.py  ← 新：MemoryHook(AgentHook) priority 80, before_model_call 注入使用策略 prompt
```

**召回模型**：模型主动调 `memory_search` → `MemoryManager.search` → SQLite 混合检索 → markdown 片段作 `{role:"tool"}` 回灌。`MemoryHook` 只注入"你有长期记忆，涉及偏好/历史时先调 `memory_search`"这类 prompt（5a 只 proactive 一档），**不替模型搜**——和 `SkillHook` 注入 skill 清单、模型自己调 `read_skill` 完全同构。

### 2.1 存储文件布局

记忆落 `<MEMORY_DIR>`（默认 `<WORKSPACE>/.twinkle_data/memory`）：

```
<MEMORY_DIR>/
├─ USER.md                    # 用户档案
├─ MEMORY.md                  # 长期记忆（决策/偏好/持久事实）
├─ daily_memory/
│   └─ YYYY-MM-DD.md          # 每日笔记 + 运行上下文
└─ memory.db                  # SQLite 检索索引（向量+全文+缓存）
```

**markdown 是权威源**（人 / 模型直接读写 `.md`），`memory.db` 是衍生的检索索引（切块 + embedding + BM25），丢了可从 `.md` 全量重建。

### 2.2 per-file 路由语义

对标 jiuwenswarm `docs/zh/记忆.md` L53–56 + `memory/internal.py:237/248`。三个文件各装什么：

| 文件（相对 MEMORY_DIR） | 装什么 | 例子 |
|---|---|---|
| `USER.md`（根） | 用户档案——"关于这个人"的稳定事实 | 姓名、职业、爱好、用 Windows 11、常用 Python |
| `MEMORY.md`（根） | 长期记忆——决策 / 偏好 / 持久事实（跨会话有用的项目领域事实，非用户个人） | "项目用 Python 3.12"、"偏好 pytest"、"架构是两进程 ws" |
| `daily_memory/YYYY-MM-DD.md`（子目录） | 每日笔记 + 运行上下文 + **用户说"记住这个"的默认落点** | "今天修了登录 bug"、"部署了 v2.1"、"记住我把项目存 D 盘" |

**白名单**（代码强制，`MemoryManager._validate_path`）：`USER.md` / `MEMORY.md` / `daily_memory/YYYY-MM-DD.md` 三个路径模式；resolved ∈ MEMORY_DIR；越界返回错误串不抛。

**路由规则**（写进 `MemoryHook` usage-strategy prompt，教模型往哪个文件写）：
- 用户个人信息（姓名 / 职业 / 沟通语言 / OS）→ `USER.md`
- 决策、偏好、持久事实（项目约定 / 架构 / 技术选型）→ `MEMORY.md`
- 用户说"记住这个" / 当日发生的事 / 运行上下文 → `daily_memory/<今天日期>.md`

**frontmatter 4 类 type（user/feedback/project/reference）是 5b auto-extraction 的约定**，5a 的 `write_memory` 写裸 content、不强制 frontmatter——5a 的分类靠"写哪个文件"这粒度足够。

### 2.3 MemoryHook 使用策略 prompt（草稿）

`MemoryHook` 注入的 system prompt 草稿（5a 只此一档；`<今天日期>` 由 `MemoryHook` 运行时用 `datetime.date.today().isoformat()` 替换，`<MEMORY_DIR>` 替换为实际路径）。注入方式照 `SkillHook._prepend_system_message`：赋新 list 前插 system msg，不原地 mutate。

**proactive**：

```
## 长期记忆
你有跨会话长期记忆,通过工具读写:memory_search(搜)/write_memory(写,append=True 追加)/read_memory(读)/edit_memory(改)。记忆文件在 <MEMORY_DIR>。

何时搜:用户提及偏好/历史/之前说过/继续上次,或回答依赖跨会话事实时,先调 memory_search(query)。

何时写:
- 用户个人信息(姓名/职业/沟通语言/操作系统/常用技术) → write_memory("USER.md", ...)
- 决策/偏好/持久事实(项目约定/架构/技术选型/已做决定) → write_memory("MEMORY.md", ...)
- 用户说"记住这个"/当日发生的事/运行上下文 → write_memory("daily_memory/<今天日期>.md", ...)

不该写:临时数据、当前任务过程性状态(那是 todo 的活)、寒暄、本轮就过期的事。
recall 到与当前信息矛盾的记忆时,用 edit_memory 修正它。
```

> "记住这个" 默认落当日 daily 文件而非 `MEMORY.md`——对齐 jiuwenswarm `docs/zh/记忆.md` L52 的路由。

> passive 模式（jiuwenswarm `is_proactive` 对应项）是后续 easy-add：加 `memory.mode` config + 第二段 prompt，代码逻辑不动。5a 不做（Twinkle 单 agent 模式，无 plan/fast 切场景，YAGNI）。

### 2.4 SQLite schema（6 表）

`MemoryManager` 持 `<MEMORY_DIR>/memory.db`，6 张表（对齐 jiuwenswarm，不精简）：

| 表 | 字段 | 作用 |
|---|---|---|
| `chunks` | `id, path, source, start_line, end_line, hash, model, text, embedding, updated_at` | 主体（`source` 恒为 `memory`，5a 不含 sessions） |
| `chunks_fts` | FTS5 虚拟表（`contentless_delete=1`） | BM25 关键词检索 |
| `chunks_vec` | sqlite-vec `vec0`（**可选**，未装 `[memory]` extra 不建） | 向量余弦 |
| `embedding_cache` | `hash, embedding, dims, updated_at` | 跳重复 embedding API |
| `files` | `path, source, hash, mtime, size` | **增量索引**：跳未变文件 |
| `meta` | `key, value` | 存 embed provider / model，**换模型触发全量重建** |

### 2.5 检索参数与降级

- **chunking** `tokens:256, overlap:32`；**候选放大** `candidate_multiplier: 2.0`（取 `min(200, max_results*2)` 再融合过滤）
- **混合打分** `score = 0.7*vec_sim + 0.3*text_sim`，按 chunk id 去重，`min_score` 过滤（默认 0.3），降序取 `max_results`（默认 10）
- **embed 模型** `text-embedding-3-small`（OpenAI 系，对齐 Twinkle 默认 base_url）；复用 `llm.api_key` + `llm.base_url`
- **降级矩阵**（生产）：有 key + 装 sqlite-vec → 全混合（向量 + FTS）；否则（无 key 或未装 sqlite-vec）→ FTS-only（不实例化 embedding provider、不 embed、不存向量，markdown 权威源在，后续装好补齐后从 `.md` 全量重建）。`MockEmbeddingProvider` 仅作测试 helper（确定性向量，离线验证 hybrid 融合逻辑），不进生产降级路径——假向量混进 hybrid（`0.7*假+0.3*真`）反而比纯 FTS 差。

---

## 3. 行为流程

### 3.1 Flow 1 — 何时写记忆 + 存储过程

**决策者**：模型（5a 纯 model-driven；auto-extraction 子 agent 是 5b）。模型在 ReAct 中判断"这条值得跨会话保留"时主动调 `write_memory`。

**何时该写**（`MemoryHook` 注入的 usage-strategy prompt 引导）：
- 用户明确陈述偏好 / 约定 / 事实（"我喜欢…" / "项目用 X 框架" / "记住我的…"）
- 用户纠正模型（"不对，应该是…"）→ 写纠正后的事实
- 工具结果里冒出稳定的、跨会话有用的事实（非临时数据）
- 用户明说"记住这个"
- **不该写**（prompt 也明示）：临时数据、当前任务过程性状态（那是 todo 的活）、寒暄、本轮就过期的事

**存储过程**（`write_memory(path, content, append)`）：
1. **路径校验**：path 命中白名单（§2.2，`_validate_path` 正则 + resolved ∈ MEMORY_DIR）；越界 → 错误串，不抛
2. **写 markdown**：`append=True` 追加；`append=False` 覆盖（纠正时常用）。frontmatter 由模型写在 content 里（prompt 教过），代码不强校验
3. **增量索引**（同步，5a 小库够用；sqlite3 在 async @tool 里同步调用）：
   a. 读文件 mtime + size → 查 `files` 表：未变 → 跳过重索引
   b. 切块 `tokens=256, overlap=32`，每块算 hash
   c. 查 `embedding_cache`：命中跳 API；未命中 → `embed_batch`（无 key → Mock；API 失败 → 标 pending 下轮重试）
   d. upsert：删该 path 旧 chunks → 插 `chunks` + `chunks_fts` + `chunks_vec`(若装)，更新 `files` / `meta`
4. 返回确认串（如 `Stored 3 chunks to MEMORY.md`）

`edit_memory(path, old_text, new_text)`：字符串替换 → 重写文件 → mtime 变 → 同步骤 3 重索引。

### 3.2 Flow 2 — 记忆清理策略

**5a（无 Dreaming，Dreaming 是 5c）**：
- **无时间衰减、无重要性评分**（对齐 jiuwenswarm：靠检索相关度，不靠时间）
- **纠正型清理**：模型发现记忆过期 / 矛盾时调 `edit_memory` 改、或 `write_memory(append=False)` 覆盖。prompt 引导"recall 到与当前矛盾的记忆时 edit 它"
- **容量兜底**（防 DB 无界膨胀）：每个 memory 文件 chunk 数硬上限（`memory.cleanup.max_chunks_per_file`，默认 200），超限丢最旧 chunk（FIFO，按 `chunks.updated_at`）——5a 唯一的自动"淘汰"，5c Dreaming 用更智能的压缩晋升取代它
- **用户手动清理**：直接编辑 / 删除 `<WORKSPACE>/.twinkle_data/memory/*.md`（mtime 变 → 下次索引自动同步；文件删 → 该 path chunks 清空）
- **换 embed 模型**：`meta` 表检测 model 变 → 清空 `chunks` / `chunks_vec` / `embedding_cache` 全量重建（防维度错乱）

**5c Dreaming（deferred）**：定时扫 sessions → 压缩 → LLM 提炼 → 晋升 `DREAMING.md`（FIFO cap 50）——真正的"会话→长期记忆"提炼 + 淘汰管线。5a 不做。

### 3.3 Flow 3 — 何时查询记忆 + 查询过程

**决策者**：模型（5a 纯 model-driven；`MemoryHook` **不自动检索注入**，只注入使用策略 prompt）。

**何时该查**（usage-strategy prompt 引导）：
- 用户提及偏好 / 历史 / 之前说过 / 继续上次 → 先 `memory_search`
- 回答确实依赖跨会话事实时
- **不该查**：纯当前会话能答的、不依赖历史的事实性问题

**注入时机**：`MemoryHook.before_model_call` **每步**注入使用策略 prompt（赋新 list 前插 system msg，照 `SkillHook`）——但这是**指导不是检索**。检索只发生在模型主动调 `memory_search` 时。

**查询过程**（`memory_search(query, max_results, min_score)`）：
1. **embed query**：`embed_provider.embed(query)`（无 key → Mock；未装 sqlite-vec → 跳过向量支）
2. **并行检索**：
   - 向量支（若装 sqlite-vec）：`chunks_vec` 余弦距离取 `min(200, max_results*2.0)` 候选
   - 全文支：`chunks_fts` BM25 取同量候选
3. **融合**：`score = 0.7*vec_sim + 0.3*text_sim`，按 chunk id 去重
4. **过滤**：`score >= min_score`（默认 0.3）→ 降序取 `max_results`（默认 10）
5. **返回**：`[{path, score, text}]` 拼成 markdown 片段串作 tool_result 回灌 → 下一轮 LLM 在 `{role:"tool"}` 看见，决定再查 / 直接答

**降级**：未装 sqlite-vec → 只走步骤 2 全文支，`score = text_sim`，仍可用。

---

## 4. 组件

### 4.1 `memory/store.py` — `MemoryManager`

对标 jiuwenswarm `manager.py`，精简。持 SQLite DB `<MEMORY_DIR>/memory.db`（6 表 schema 见 §2.4，路径校验见 §2.2，检索参数 / 降级见 §2.5）。

**方法**：
- `search(query, max_results=10, min_score=0.3) -> list[dict]`（返回 `[{path, score, text}]`）
- `write(path, content, append=False) -> str`（确认串 / 错误串）
- `read(path, offset=None, limit=None) -> str`
- `edit(path, old_text, new_text) -> str`
- `list_files() -> list[str]`（供 `MemoryHook` 判有无记忆）

### 4.2 `memory/embeddings.py` — 两个 provider

- `OpenAICompatibleEmbeddingProvider`：复用 `LLM_BASE_URL` + `LLM_API_KEY` + `embed_model`（默认 `text-embedding-3-small`，1536 维，对齐 Twinkle 默认 OpenAI 体系）；POST `/embeddings`
- `MockEmbeddingProvider`：**测试 helper**（确定性哈希伪向量，离线验证 hybrid 融合逻辑，不进生产路径）。生产无 key 时走 FTS-only（不实例化 provider）

### 4.3 `memory/__init__.py`

照 `skills/__init__.py`：`get_memory_manager()` 进程级单例（惰性构造，lazy import config），`_set_memory_manager()` 测试钩子（配 tmp_path 盘）。

### 4.4 `tools/builtin/memory_tools.py` — 4 个 `@tool`

照 `skill_tools.py`，薄包装 `get_memory_manager()`。全部返回字符串（成功带片段 / 确认，失败带错误串，**不抛、不炸 ReAct**）：

```python
@tool
async def memory_search(query: str, max_results: int | None = None, min_score: float | None = None) -> str: ...

@tool
async def write_memory(path: str, content: str, append: bool = False) -> str: ...

@tool
async def read_memory(path: str, offset: int | None = None, limit: int | None = None) -> str: ...

@tool
async def edit_memory(path: str, old_text: str, new_text: str) -> str: ...
```

### 4.5 `hooks/builtin/memory_hook.py` — `MemoryHook`

- `priority = 80`（功能层 50–99，低于 `SkillHook` 90）
- `before_model_call`：调 `get_memory_manager().list_files()` 判有无记忆 → 无则 no-op；有则注入使用策略 prompt（草稿见 §2.3，5a 只 proactive 一档）
- 注册进 `hooks/builtin/__init__.py` + `server.py main()` 的 hook 列表

---

## 5. 配置

新增 `memory:` 块（照 `skills` 形状，`_derive_paths` 派生 `MEMORY_DIR`）：

```yaml
memory:
  dir: ${TWINKLE_MEMORY_DIR:-}        # 空 → <workspace>/.twinkle_data/memory
  embed_model: text-embedding-3-small # 复用 llm.api_key + llm.base_url,不另开 embed.* env
  query:
    max_results: 10
    min_score: 0.3
  hybrid:
    vector_weight: 0.7
    text_weight: 0.3
    candidate_multiplier: 2.0
  chunking:
    tokens: 256
    overlap: 32
  cleanup:
    max_chunks_per_file: 200     # 单文件 chunk 上限,超限 FIFO 丢最旧(5a 唯一自动淘汰;5c Dreaming 取代)
```

`config/__init__.py` 摊平出 `MEMORY_DIR` / `MEMORY_EMBED_MODEL` / `MEMORY_QUERY_*` / `MEMORY_HYBRID_*` / `MEMORY_CHUNKING_*` / `MEMORY_CLEANUP_*`。embedding 不另开 env（复用 `LLM_API_KEY` / `LLM_BASE_URL`，对齐 roadmap"复用 `TWINKLE_LLM_BASE_URL` 体系"）。

`ensure_workspace_dir()` 加 `os.makedirs(_cfg.MEMORY_DIR, exist_ok=True)` + seed `daily_memory/` 子目录。

---

## 6. 错误处理

- **DB / schema 初始化失败**：`MemoryManager` 构造 try/except → 降级 no-op（`search` 返空、`write` 只写文件不索引），不炸 agent_loop（照 stub "零成本"哲学）
- **embedding API 失败**：search 降级 FTS-only + 告警；write 时 embedding 失败 → 文件已写、该 chunk 标 pending，下次 mtime 扫描重试
- **sqlite-vec 未装但配了向量**：启动告警 + 自动 FTS-only（不报错）
- **路径越界 / 非白名单**：write / read / edit 返回错误字符串，**不抛、不炸 ReAct**（照 `skill_tools`）
- **换 embed 模型**：`meta` 表检测 model 变 → 清空 `chunks` / `chunks_vec` / `embedding_cache` 全量重建

---

## 7. 测试

不依赖 `pytest-asyncio`，用 `asyncio.run()` + `free_port` / `tmp_path` fixture（项目约定）。

- `test_memory_store.py`：tmp_path DB — write→search 往返；edit 替换重索引；read offset / limit；路径白名单越界拒；mtime 增量（二次写 embedding 调用计数 = 1）；换 embed_model 触发重建；FTS-only 降级（monkeypatch sqlite-vec import 失败仍 search）
- `test_memory_embeddings.py`：Mock 确定性；OpenAI-compatible monkeypatch httpx 解析向量
- `test_memory_hook.py`：before_model_call 有 memory 注入使用策略 prompt / 无 memory no-op
- `test_memory_tools.py`：4 个 @tool 经 `ToolManager.schemas` / `execute`（monkeypatch `get_memory_manager`）
- `test_agent_loop_memory.py`：`build_agent_loop` + `MemoryHook` + monkeypatch embed，跑一轮 ReAct 验证 `memory_search` tool_result 回灌

---

## 8. 文件清单

**新增**
- `twinkle/agentserver/memory/__init__.py`（`get_memory_manager` + `_set_memory_manager`）
- `twinkle/agentserver/memory/store.py`（`MemoryManager`：6 表 schema + search/write/read/edit + mtime 增量 + path 白名单）
- `twinkle/agentserver/memory/embeddings.py`（`OpenAICompatibleEmbeddingProvider` + `MockEmbeddingProvider`）
- `twinkle/agentserver/tools/builtin/memory_tools.py`（4 个 @tool）
- `twinkle/agentserver/hooks/builtin/memory_hook.py`（`MemoryHook`）
- 5 个测试文件（见 §7）

**改**
- `twinkle/config/schema.py`（+`MemoryConfig` + `TwinkleConfig.memory` + `_derive_paths` 派生 `MEMORY_DIR`；`PermissionsConfig.tools` 默认加 4 个 memory 工具 = `allow`）
- `twinkle/config/__init__.py`（摊平 `MEMORY_*` 常量）
- `twinkle/resources/config.yaml`（+ `memory:` 块；`permissions.tools` 加 `memory_search`/`write_memory`/`read_memory`/`edit_memory` = `allow`，对齐 `file_tools`）
- `twinkle/workspace.py`（`ensure_workspace_dir` + `makedirs(MEMORY_DIR)` + seed `daily_memory/` 子目录）
- `twinkle/agentserver/tools/__init__.py`（`tool_manager()` register 4 个 memory 工具）
- `twinkle/agentserver/hooks/builtin/__init__.py`（+ `MemoryHook` 导出）
- `twinkle/agentserver/server.py`（`main()` hook 列表加 `MemoryHook()`；删 `memory = LongTermMemory()` + `AgentLoop(..., memory)`）
- `twinkle/agentserver/agent_loop.py`（删 `memory` 构造参数 + 第 156 行 `self._memory.recall(query)`）
- `pyproject.toml`（`[memory]` extra = `sqlite-vec`）

**删**
- `twinkle/agentserver/memory.py`（stub → 改为 `memory/` 包）

**roadmap 修订**（落实时改 `roadmap.md` Phase 5 + 里程碑 M6，两条）：
- "每轮 `llm.stream` 前注入召回记忆" → `MemoryHook`(before_model_call) 只注入使用策略 prompt，不自动注入检索结果
- "接口形状不变" → 退役 `LongTermMemory` stub + `AgentLoop` 的 `memory` 参数；改为 `memory/` 包 + `MemoryManager` 单例 + 4 个 `@tool`
- "Mock fallback（无 key 时降级）" → 无 key 走 FTS-only 降级（不实例化 embedding provider、不 embed）；`MockEmbeddingProvider` 保留为测试 helper，不进生产降级
- 标注 5a 落地、5b / 5c deferred（5b auto-extraction 子 agent；5c Dreaming sweeper + sessions 入池）

---

## 9. jiuwenswarm 源码锚点（`enterprise_dev` 分支，`git show enterprise_dev:<path>` 读）

- `jiuwenswarm/agents/harness/common/memory/manager.py` — `MemoryIndexManager`：schema / search / 同步
- `.../memory/embeddings.py` — `OpenAICompatibleEmbeddingProvider` + `Mock`
- `.../tools/memory_tools.py` — `@tool memory_search` / `memory_get` / `write_memory` / `edit_memory` / `read_memory`
- `.../memory/internal.py:237/248` — daily memory 文件在 `memory/daily_memory/` 子目录
- `docs/zh/记忆.md` L53–56 — per-file 路由语义表（USER/MEMORY/daily）
- `.../auto_memory/extraction_runner.py` + `extract_memories.py` — 对话后子 agent 提取（5b 参考）
- `.../memory/dreaming/sweeper.py` — Dreaming 压缩晋升（5c 参考）
- `jiuwenswarm/resources/config.yaml` — memory 段 L84–182、embed 段 L442–445

---

## 10. 验收标准

- `write_memory` 写入 `USER.md` / `MEMORY.md` / `daily_memory/YYYY-MM-DD.md` 后，`memory_search` 能按语义召回相关片段（hybrid 命中）
- 路径越界（`../escape.md`、非白名单文件名）一律返回错误串、不抛、不炸 ReAct
- `edit_memory` 替换后，旧文本不再被召回、新文本可召回（mtime 重索引生效）
- 未装 sqlite-vec（`[memory]` extra）时，`memory_search` 降级 FTS-only 仍可用、不报错
- 无 `LLM_API_KEY` 时，降级 FTS-only，链路不挂
- `MemoryHook` 有 memory 注入使用策略 prompt、无 memory no-op
- `agent_loop` 零结构改动（无新构造参数、无新分支，除删 stub 调用外）
- 跨会话事实召回：A 会话 `write_memory("用户偏好中文")` → B 会话 `memory_search("用户语言偏好")` 命中
