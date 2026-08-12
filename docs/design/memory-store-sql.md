# 记忆库的 SQLite 表设计与 CRUD SQL 解析

这篇拆 `twinkle/agentserver/memory/store.py`(`MemoryManager`)里实际在用的 SQLite schema 和增删改查 SQL。代码引用以方法名为准,SQL 一律原文摘录,不重写。读前可先看 [`sqlite-introduction.md`](../sqlite-introduction.md)(虚表 §8、扩展加载 §9)和 [`sqlite-vector-database.md`](../sqlite-vector-database.md)(vec0/三表关联)。

## 1. 全局:六张表,一张图

```
        ┌──────────────────────┐
        │  chunks              │  主表:存分块正文 + 元数据(行号/路径/hash 等)
        │  (普通表)            │  + 隐式 rowid —— 跨表关联的绳
        └──────────┬───────────┘
                   │ 共享 rowid(插入时手动对齐)
        ┌──────────┴───────────┐
        ▼                      ▼
┌──────────────────┐  ┌──────────────────┐
│  chunks_fts      │  │  chunks_vec      │
│  FTS5 虚表       │  │  vec0 虚表       │
│  关键词检索(倒排)│  │  语义检索(k-NN)  │
└──────────────────┘  └──────────────────┘

┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│  embedding_cache │  │  files           │  │  meta             │
│  文本→向量缓存   │  │  文件指纹(判变用)│  │  键值配置(模型戳)│
└──────────────────┘  └──────────────────┘  └──────────────────┘
```

三类表:

| 类别 | 表 | 作用 |
|---|---|---|
| **三表关联核心** | `chunks` / `chunks_fts` / `chunks_vec` | 主表存正文+元数据;FTS5 虚表做关键词检索;vec0 虚表做语义检索。三者靠**同一个 `rowid`** 关联 |
| **缓存** | `embedding_cache` | 文本 md5 → 向量 blob 的缓存,省去重复嵌入 |
| **配置/指纹** | `files` / `meta` | `files` 存每个文件的修改指纹做增量判变;`meta` 存键值(当前嵌入模型名等) |

关键设计:一张 `.db` 文件同时装下结构化正文、FTS5 倒排索引、vec0 向量索引,不引第二个系统。三表关联靠 `rowid`(见入门篇 §3.4 + 向量篇 §3.1)。

## 2. 建表 DDL 逐表解析(`_ensure_schema`)

### 2.1 `chunks` —— 主表

```sql
CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY, path TEXT, source TEXT, start_line INTEGER,
  end_line INTEGER, hash TEXT, model TEXT, text TEXT, embedding BLOB,
  updated_at TEXT)
```

列含义:

| 列 | 类型 | 含义 |
|---|---|---|
| `rowid` | INTEGER(隐式) | 自增整数,跨表关联键——和 `chunks_fts`/`chunks_vec` 共享(§3.3)。**不声明也存在**(入门篇 §3.4) |
| `id` | TEXT PK | 逻辑主键,值 = `{path}:{start_line}:{end_line}`,人类可读的 chunk 标识 |
| `path` | TEXT | 文件相对路径,如 `MEMORY.md` / `daily_memory/2026-08-11.md` |
| `source` | TEXT | 来源标记,固定 `"memory"` |
| `start_line` / `end_line` | INTEGER | 该 chunk 在原文件里的起止行号(`Chunk.start`/`Chunk.end`) |
| `hash` | TEXT | **整份文件内容**的 md5——同一文件所有 chunk 这列相同。注意不是 chunk 文本的 hash,别和 `embedding_cache.hash` 混 |
| `model` | TEXT | 嵌入这条 chunk 用的模型名(= `embed_model`) |
| `text` | TEXT | chunk 原文 |
| `embedding` | BLOB | 向量 blob。**只写不读**:检索走 `chunks_vec`/`embedding_cache`,不读这列 |
| `updated_at` | TEXT | ISO 时间戳,§4.1 FIFO 淘汰按它排序 |

读法:

- **`id TEXT PRIMARY KEY`** 是**逻辑主键**,值形如 `path:start:end`(`store.py` `_index_file` 里 `chunk_id = f"{relative_path}:{chunk.start}:{chunk.end}"`)。注意它是 **TEXT**,所以**不**像 `INTEGER PRIMARY KEY` 那样成为内置 `rowid` 的别名——这张表同时有一个**隐式 `rowid`**(INTEGER,自增),它才是跨表关联用的键(入门篇 §3.4)。
- **`embedding BLOB`** 列也存了一份向量 blob,但**当前检索路径不读它**——语义检索走 `chunks_vec`,缓存命中走 `embedding_cache`。这列写而不读,留心别误以为是检索的数据源。
- **`text`** 存原文;FTS5 里存的是**CJK 加空格预处理后**的副本(见 §2.2),两者不同。

### 2.2 `chunks_fts` —— FTS5 全文索引

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text)
```

列含义(`fts5(text)` 只定义了一列 + 隐式 rowid):

| 列 | 含义 |
|---|---|
| `rowid` | 对齐主表 `chunks` 的 rowid(插入时显式灌入,§3.3) |
| `text` | **索引文本** = `_space_cjk(chunk.text)`(CJK 逐字加空格预处理版),**不是原文**——和 `chunks.text` 不同 |

- **自存式 `fts5(text)`**,FTS5 自己存一份 `text` 的副本 + 倒排索引。
- **为什么不用外部内容表?** 因为索引的文本要预处理:插入时灌的是 `_space_cjk(chunk.text)`(`store.py` `_index_file`),把 CJK 字符逐字加空格以改善中文分词。这和主表 `chunks.text` 存的原文**不同**;若用外部内容表直接挂 `chunks.text`,就绕过了这道预处理,所以这里刻意用自存式。

### 2.3 `chunks_vec` —— vec0 向量索引(条件建表)

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec
USING vec0(embedding float[1536] distance=cosine)
```

列含义(`vec0(...)` 一列向量 + 隐式 rowid):

| 列 | 含义 |
|---|---|
| `rowid` | 对齐主表 `chunks` 的 rowid |
| `embedding` | `float[1536]` 序列化成的 float32 blob;维度建表钉死,`distance=cosine`(k-NN 返回 `distance` ∈ [0,2]) |

- **维度 `1536` 建表时钉死**(`self._dims`,默认 1536)。换 embedding 模型导致维度变化必须整库重建(见 §4.2)。
- **`distance=cosine`**,k-NN 返回的 `distance` ∈ [0,2],越小越相似。
- **条件建表**:这段包在 `try/except` 里(`store.py` `_ensure_schema`),`sqlite-vec` 扩展加载失败就 `self._vec_enabled = False`,整库**降级到 FTS-only**——不报错、不阻断,只是语义检索那条腿没了。
- 和 `chunks_fts` 一样,只存向量、靠 `rowid` 对齐主表。

### 2.4 `embedding_cache` —— 文本→向量缓存

```sql
CREATE TABLE IF NOT EXISTS embedding_cache(
  hash TEXT PRIMARY KEY, embedding BLOB, dims INTEGER, updated_at TEXT)
```

列含义:

| 列 | 类型 | 含义 |
|---|---|---|
| `hash` | TEXT PK | **chunk 文本**的 md5——缓存键。同一段文本(跨文件/跨重建)只嵌入一次。注意和 `chunks.hash`(=文件 md5)不同 |
| `embedding` | BLOB | 向量 blob |
| `dims` | INTEGER | 向量维度(=1536) |
| `updated_at` | TEXT | 时间戳 |

- `hash` = 文本内容的 md5。同一段文本(哪怕跨文件重复)只嵌入一次,后续直接取 blob。
- `dims` 记录这条向量维度,和 `chunks_vec` 的钉死维度对得上才有意义。

### 2.5 `files` —— 文件指纹(增量判变用)

```sql
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime REAL, size INTEGER)
```

列含义:

| 列 | 类型 | 含义 |
|---|---|---|
| `path` | TEXT PK | 文件相对路径(主键) |
| `source` | TEXT | `"memory"` |
| `hash` | TEXT | 文件内容 md5 |
| `mtime` | REAL | 文件修改时间(`stat.st_mtime`,浮点 unix 时间) |
| `size` | INTEGER | 文件字节数(`stat.st_size`) |

- 一行 = 一个已索引文件的「上次状态指纹」:`mtime`(改时间)+ `size` + `hash`(内容 md5)。三者全等 §3.1 才跳过。

### 2.6 `meta` —— 键值配置

```sql
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)
```

列含义:

| 列 | 类型 | 含义 |
|---|---|---|
| `key` | TEXT PK | 配置键(当前只有 `'embed_model'`) |
| `value` | TEXT | 配置值(`embed_model` → 当前嵌入模型名) |

- 当前只存一个键:`embed_model` = 当前嵌入模型名。`__init__` 末尾 `_clear_if_model_changed` 比对它和 `provider.model`,不一致就整库清空(见 §4.2)。

## 3. 写入:`_index_file` 的「判变 → 删旧 → 重插 → 同步」

写入不走单条 `INSERT`,而是**整文件全量替换**:文件变了就把该文件在三表里的旧数据删干净,用新内容重切、重嵌、重写。整段包在一个事务里,任一步抛异常 `rollback()`。

### 3.1 增量判变(SELECT + 跳过)

```sql
SELECT mtime, size, hash FROM files WHERE path=?
```

拿 `files` 里这个文件上次的指纹,和当前磁盘 `stat.st_mtime` / `stat.st_size` / 内容 `md5` 比对:

```python
if (fingerprint and fingerprint["mtime"] == stat.st_mtime
        and fingerprint["size"] == stat.st_size and fingerprint["hash"] == file_hash):
    return  # 没变,跳过
```

三者全等 → 文件没变 → 直接返回,不切片不嵌入。这是增量索引省算力的关键。

### 3.2 删旧(三表同步 DELETE)

先查出该文件现有 chunk 的所有 rowid,再从三表删:

```sql
SELECT rowid FROM chunks WHERE path=?                    -- 拿要删的 rowid
DELETE FROM chunks     WHERE path=?                      -- 主表按 path 删
DELETE FROM chunks_fts WHERE rowid IN (?,?,?)            -- FTS 索引按 rowid 删
DELETE FROM chunks_vec WHERE rowid IN (?,?,?)            -- 向量索引按 rowid 删
```

要点:

- `chunks` 有 `path` 列,能按文件删;`chunks_fts`/`chunks_vec` **没有 `path`**,只能靠主表查出的 `rowid` 走 `IN (...)`。这就是三表靠 rowid 关联的实操(向量篇 §6)。
- `IN (?,?,?)` 的 `?` 个数 = rowid 个数,运行时用 `placeholders = ",".join("?" * len(stale_rowids))` 拼,再 `execute(sql, stale_rowids)` 逐个绑定(入门篇 §2.2 的参数化)。

### 3.3 重插(三表同步 INSERT,共享 rowid)

```python
cur = self._db.execute(
    "INSERT INTO chunks(id,path,source,start_line,end_line,hash,model,"
    "text,embedding,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
    (chunk_id, relative_path, "memory", chunk.start, chunk.end, file_hash,
     embed_model, chunk.text, blob, now))
rowid = cur.lastrowid                     # 主表自增 rowid
self._db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)",
                 (rowid, _space_cjk(chunk.text)))      # 同一 rowid 灌进 FTS
if want_vec and blob is not None:
    self._db.execute(
        "INSERT INTO chunks_vec(rowid, embedding) VALUES(?, ?)",
        (rowid, blob))                                  # 同一 rowid 灌进 vec0
```

**关联就在这一步建立**:主表 `INSERT` 拿到 `cur.lastrowid`(自增 rowid,入门篇 §3.4),再把这个**同一个数**当 `rowid` 显式灌进 `chunks_fts` 和 `chunks_vec`。三表从此共享一套 rowid,后面检索才能 `JOIN ... ON c.rowid = chunks_fts.rowid`。注意 FTS5 灌的是 `_space_cjk(text)`(预处理版),不是原文。

### 3.4 UPSERT 指纹与模型戳

```sql
INSERT OR REPLACE INTO files(path,source,hash,mtime,size) VALUES(?,?,?,?,?)
INSERT OR REPLACE INTO meta(key,value) VALUES('embed_model', ?)
```

- `INSERT OR REPLACE` 是 SQLite 的 UPSERT(入门篇 §4.2):主键冲突就整行替换。这里 `files.path` / `meta.key` 是主键,所以重写同一文件/同一键就是覆盖。
- `files` 指纹更新成「这次索引后的状态」,作为下次 §3.1 判变的基准。
- `meta('embed_model')` 记下当前嵌入模型名,供 §4.2 用。

## 4. 删除:两类淘汰

### 4.1 单文件 FIFO 淘汰(`_evict_excess_chunks`)

防一个文件反复重切导致 chunk 无限膨胀。超 `max_chunks_per_file`(默认 200)就按最旧优先删多出来的:

```sql
SELECT COUNT(*) FROM chunks WHERE path=?                              -- 数现有几条
SELECT rowid FROM chunks WHERE path=? ORDER BY updated_at ASC LIMIT ? -- 最旧的 N 条
DELETE FROM chunks     WHERE rowid IN (?,?,?)                        -- 三表同步删
DELETE FROM chunks_fts WHERE rowid IN (?,?,?)
DELETE FROM chunks_vec WHERE rowid IN (?,?,?)
```

`ORDER BY updated_at ASC LIMIT ?` = FIFO:最早更新的先删。同样三表按 rowid 同步删。

### 4.2 模型变更整库清空(`_clear_if_model_changed`)

`meta.embed_model` 和当前 `provider.model` 不一致(换了 embedding 模型/维度)→ 旧向量作废,整库清空:

```sql
SELECT value FROM meta WHERE key='embed_model'
DELETE FROM chunks
DELETE FROM chunks_fts
DELETE FROM chunks_vec
DELETE FROM embedding_cache
DELETE FROM files
DELETE FROM meta WHERE key='embed_model'
```

清掉所有 chunk、两套索引、向量缓存、文件指纹、模型戳。注意这里只清不建——真正的重灌是惰性的,发生在下次 `write`/`edit` 调 `_index_file` 时(用新模型重嵌入、重新盖 `embed_model` 戳);没被再写的文件不会被自动回灌。注意 `chunks_vec` 维度建表钉死,换维度不重建会维度不匹配 → 这套机制兜的就是这个。

## 5. 检索:FTS + vec 两路 + 融合(`search`)

`max_results` 决定最终返回条数;`candidates = min(200, max_results * 倍数)` 是两路各自捞的候选数(放大池子再融合截断)。

### 5.1 FTS 关键词腿(`_fts_search`)

```sql
SELECT c.rowid, c.path, c.text, c.start_line, c.end_line, bm25(chunks_fts) AS bm
FROM chunks_fts
JOIN chunks c ON c.rowid = chunks_fts.rowid
WHERE chunks_fts MATCH ?
ORDER BY bm
LIMIT ?
```

- `chunks_fts MATCH ?` 做全文查询(入门篇 §8.2),查询串先包成 `"...""..."` 短语 + CJK 加空格(`_space_cjk`)。
- `JOIN chunks c ON c.rowid = chunks_fts.rowid` 就是把 FTS 命中行按共享 rowid 拼回主表取正文/行号。
- `bm25(chunks_fts)` 返回非正值,越负越相关,`ORDER BY bm` 升序 = 最相关在前(入门篇 §8.2)。

### 5.2 向量语义腿(`_vec_search`)

```sql
SELECT rowid, distance
FROM chunks_vec WHERE embedding MATCH ?
ORDER BY distance
LIMIT ?
```

- `embedding MATCH ?` 是 vec0 注入的 k-NN 操作符,`?` 传查询向量 blob(向量篇 §5)。
- `ORDER BY distance` 升序,越小越相似。cosine `distance` ∈ [0,2],代码里转成相似度 `sim = max(0, 1 - distance/2)` ∈ [0,1](向量篇 §3)。
- 返回 `{rowid: 相似度}`。

### 5.3 融合 + 回填

两路候选按 rowid 求并集:FTS 命中行已经带正文(§5.1 JOIN 拿到了),vec-only 命中(只在向量腿出现)要回主表取正文/行号:

```sql
SELECT rowid, path, text, start_line, end_line FROM chunks WHERE rowid=?
```

然后每条候选算融合分:

```
fused = vector_weight * vec_sim + text_weight * text_sim
```

- `vec_sim` 来自 §5.2(`vec_sims.get(rowid, 0.0)`,vec 腿没命中就 0)。
- `text_sim` 由 §5.1 的 `bm25` 经 `_text_sim(bm) = |bm|/(1+|bm|)` 映射到 [0,1](FTS 腿没命中就 0)。
- 默认 `vector_weight=0.7` / `text_weight=0.3`(语义为主、关键词为辅),按 `ORDER BY fused DESC` 排序,截 `max_results` 条。

**降级**:`sqlite-vec` 没装或无 `provider` 时(`not (self._vec_enabled and self._provider is not None)`),只走 §5.1,按 bm25 顺序取前 N 条返回。两条腿互不阻断。

## 6. embedding 缓存(`_embed_chunks`)

切片后嵌入前,先按文本 md5 查缓存:

```sql
SELECT embedding FROM embedding_cache WHERE hash=?           -- 命中直接用
INSERT OR REPLACE INTO embedding_cache(hash,embedding,dims,updated_at) VALUES(?,?,?,?)  -- 没命中才嵌入并回填
```

- 同一段文本(哪怕跨文件、跨重建)只嵌入一次。模型变更重建时(§4.2)整表清空,强制用新模型重嵌。
- 缓存命中走 `embedding_cache`,没命中走 `provider.embed(...)`,写入 blob 后回填缓存。`chunks_vec` 用的是同一份 blob。

## 7. 速查:六张表 × 增删改查

| 表 | 增(C) | 读(R) | 改(U) | 删(D) |
|---|---|---|---|---|
| `chunks` | `INSERT INTO chunks(...) VALUES(...)`(主表,拿 `lastrowid`) | `SELECT rowid/path/text/... FROM chunks WHERE path=? OR rowid=?` | (无单独 UPDATE,改 = 删旧+重插,§3) | `DELETE FROM chunks WHERE path=?` / `WHERE rowid IN (...)` |
| `chunks_fts` | `INSERT INTO chunks_fts(rowid, text) VALUES(?,?)`(灌预处理文本) | `WHERE chunks_fts MATCH ?` + `bm25(...)`(§5.1) | (无,靠删+重插) | `DELETE FROM chunks_fts WHERE rowid IN (...)` |
| `chunks_vec` | `INSERT INTO chunks_vec(rowid, embedding) VALUES(?,?)` | `WHERE embedding MATCH ? ORDER BY distance`(§5.2) | (无,靠删+重插) | `DELETE FROM chunks_vec WHERE rowid IN (...)` |
| `embedding_cache` | `INSERT OR REPLACE INTO embedding_cache(...) VALUES(...)` | `SELECT embedding FROM embedding_cache WHERE hash=?` | UPSERT 同增 | `DELETE FROM embedding_cache`(整表,模型变更时) |
| `files` | `INSERT OR REPLACE INTO files(...) VALUES(...)`(UPSERT) | `SELECT mtime,size,hash FROM files WHERE path=?`(判变) | UPSERT 同增 | `DELETE FROM files`(整表,模型变更时) |
| `meta` | `INSERT OR REPLACE INTO meta(key,value) VALUES('embed_model',?)` | `SELECT value FROM meta WHERE key='embed_model'` | UPSERT 同增 | `DELETE FROM meta WHERE key='embed_model'`(重建时) |

共性:

- **三表(`chunks`/`chunks_fts`/`chunks_vec`)没有 UPDATE**:文件改了不是改字段,而是删旧 + 重插——因为索引(倒排/向量)跟着内容变,改一行正文等于整条 chunk 失效,重建比局部更新简单可靠。
- **`embedding_cache`/`files`/`meta` 用 UPSERT**:这三张是缓存/配置,主键固定,直接覆盖最自然。
- **删除分两级**:单文件级(`WHERE path=?` / `rowid IN (...)`,§3.2/§4.1)和全库级(模型变更,§4.2)。
