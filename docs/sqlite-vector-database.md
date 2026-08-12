# SQLite 当向量数据库用(sqlite-vec)

这篇讲怎么用 `sqlite-vec` 扩展把 SQLite 变成一个能做语义检索的向量库。假设你读过 [`sqlite-introduction.md`](./sqlite-introduction.md),知道 SQLite 基本用法和「虚拟表」「扩展加载」的概念。

## 1. 先搞清:什么是向量检索,SQLite 在哪一档

**向量检索**的做法:把文本(或图片等)用 embedding 模型转成一个固定维度的浮点向量(如 768 维、1536 维),检索时把查询也转成向量,在库里找「方向最接近」的几条。它解决的是**语义相似**——「用户喜欢 Python」和「偏好用 Python 写代码」没有共同关键词,但向量很近,关键词检索(FULLTEXT)召不回,向量能。

数据库按规模分三档:

| 档 | 规模 | 选什么 |
|---|---|---|
| 玩具/原型 | 几百~几千向量 | SQLite + sqlite-vec,**单文件零依赖** |
| 中小生产 | 几千~几十万向量 | SQLite + sqlite-vec,或 PostgreSQL + pgvector |
| 大规模 | 百万~亿级向量 | 专用向量库(Qdrant / Milvus / Weaviate),带 ANN 索引 |

**SQLite 当向量库的甜区**:中小规模、单机、不想引外部服务、向量要和结构化数据放一起(JOIN)、库跟着应用文件走能整体打包。一个 `.db` 文件同时存你的业务表、FTS5 全文索引、vec0 向量索引,不引第二个系统——这是它最大的卖点。

**不适用**:百万级以上向量(下面会讲为什么)、要多机并发写、要 GPU 加速、要近似最近邻(ANN)索引。

## 2. 装与加载 sqlite-vec

`sqlite-vec` 是第三方 C 扩展,不是 SQLite 自带的。装 Python 绑定:

```bash
pip install sqlite-vec
```

加载到连接(每个连接要单独加载一次):

```python
import sqlite_vec
import sqlite3

conn = sqlite3.connect("vectors.db")
conn.enable_load_extension(True)     # 先打开"允许加载扩展"开关
sqlite_vec.load(conn)                 # 把 sqlite-vec 挂进这个连接
conn.enable_load_extension(False)     # 关上开关(安全起见)

# 验证挂上了:调用 sqlite-vec 提供的函数
print(conn.execute("select vec_version()").fetchone()[0])
```

要点:

- **扩展是连接级**。同一进程的两个 connection,挂在 A 上的扩展 B 用不了。每个连接都要 `load` 一次。
- **`enable_load_extension(True)` 是必须的前置开关**——SQLite 默认禁止加载扩展(安全考虑),加载完关上。
- 命令行 CLI 也能加载:`sqlite3 vectors.db ".load sqlite_vec"`(扩展文件路径按平台找)。

## 3. 建向量表:`vec0` 虚表

`sqlite-vec` 提供一个叫 `vec0` 的虚拟表模块。建表语法:

```sql
CREATE VIRTUAL TABLE docs_vec USING vec0(
  embedding float[768] distance=cosine
);
```

读法:

- `vec0(...)` 是模块名 + 参数。
- `embedding float[768]` 声明向量列叫 `embedding`,**维度 768 在建表时钉死**。换 embedding 模型导致维度变化,必须删表重建(数据也丢,要重新嵌入)。
- `distance=cosine` 指定距离度量。可选 `cosine` / `L2`(欧氏) / `L1`(曼哈顿),**默认 `L2`**。文本语义检索一般用 `cosine`。

各度量的 distance 含义(查询时返回的 `distance` 列):

| 度量 | distance 范围 | 含义 | 用途 |
|---|---|---|---|
| `cosine` | [0, 2] | 0=方向完全一致,2=反向 | 文本/语义(只关心方向,不关心长度) |
| `L2` | [0, ∞) | 欧氏距离,0=完全相同 | 通用,受向量长度影响 |
| `L1` | [0, ∞) | 曼哈顿距离,0=完全相同 | 偶用 |

**越小越相似**是统一的排序方向,查询时 `ORDER BY distance` 升序。

cosine 转「相似度」(0~1,越大越像)的常用映射:`similarity = 1 - distance / 2`,clamp 到 [0,1]。这是 cosine distance ∈ [0,2] 的直接推论。

### 3.1 `rowid` 哪来的?

上面 `CREATE` 没声明 `rowid`,但下面插入/查询都用了它。它是 SQLite 给每行默认就有的**内置隐式列**——建表不写也存在,用 `rowid` 这个名字就能在 `INSERT`/`SELECT` 里访问;不显式给值就自增,给值就用你的。和入门篇 §3.4「`INTEGER PRIMARY KEY` 是内置 rowid 的别名」是同一套机制,只是 vec0 让你直接用裸 `rowid`。本篇 §6 正是靠把主表 rowid 灌进 `chunks_vec` 让三表共享 rowid 才能 JOIN。

## 4. 插入向量:序列化成 float32 blob

向量必须先序列化成 **float32 的二进制 blob** 再插,不能直接插 Python list。三种序列化方式:

```python
# 方式 A:sqlite-vec 的 Python 辅助函数(最直接)
from sqlite_vec import serialize_floats
blob = serialize_floats([0.1, 0.2, 0.3, ...])   # list[float] → bytes

# 方式 B:标准库 struct(不依赖 sqlite-vec 的 Python 端)
import struct
blob = struct.pack(f"{len(vec)}f", *vec)         # 1536 维 → 6144 字节

# 方式 C:SQL 内用 vec_f32() 函数(把 blob/text 在 SQL 里转成 float32 blob)
conn.execute("INSERT INTO docs_vec(rowid, embedding) VALUES (?, vec_f32(?))",
             (rowid, blob_or_text))
```

插:

```python
conn.execute("INSERT INTO docs_vec(rowid, embedding) VALUES (?, ?)",
             (rowid, serialize_floats(vec)))
```

`rowid` 是这条向量的 id,后面用它和元数据表关联(见 §5)。`vec0` 表本身只存向量,不存别的——文本、来源、元数据请放另一张普通表。

## 5. 查询:k-NN 近邻检索

语义检索的核心 SQL——「给一个查询向量,找库里方向最近的 k 条」:

```python
query_vec = embed_query("怎么用 Python 写爬虫")   # 你的 embedding 模型
qblob = serialize_floats(query_vec)

rows = conn.execute(
    "SELECT rowid, distance "
    "FROM docs_vec "
    "WHERE embedding MATCH ? "        # MATCH 是 vec0 注入的操作符
    "ORDER BY distance "              # 升序:越小越相似
    "LIMIT 10",
    (qblob,)
).fetchall()
# rows: [(3, 0.08), (17, 0.12), ...]  → rowid=3 的向量最接近查询
```

要点:

- **`WHERE embedding MATCH ?`** 是 vec0 注入的语法,`?` 传查询向量 blob。
- **`ORDER BY distance`** 升序,`LIMIT k` 取前 k 个最近邻。
- **暴力扫描**。sqlite-vec 的 k-NN 是**精确(exact)**的——对表里每条向量算一次距离再排序,没有 ANN 近似索引(HNSW/IVF 那种)。所以规模上限就来自这:十万级以内很快,百万级开始慢,千万级就别用了。
- 查询向量必须和建表时的维度、序列化方式一致(都是 float32 blob)。

## 6. 把元数据和向量放一起(rowid 关联)

`vec0` 表只存向量,你的原文/标题/来源等放普通表,靠 `rowid` 关联:

```sql
-- 普通表存元数据
CREATE TABLE docs(
  id INTEGER PRIMARY KEY,    -- 这个 id 就是 vec0 表里的 rowid
  title TEXT,
  body TEXT,
  source TEXT
);

-- 向量表只存向量
CREATE VIRTUAL TABLE docs_vec USING vec0(
  embedding float[768] distance=cosine
);
```

两张表的 id 相等是在**插入时**手动建立的,不是查询时自动匹配:先插 `docs` 拿到自增 id,再把这个值当 `rowid` 插进 `docs_vec`——同一个数,两边就绑上了。

```python
cur = conn.execute("INSERT INTO docs(title, body, source) VALUES(?, ?, ?)",
                   (title, body, source))
rowid = cur.lastrowid          # docs 的自增 id(= 它的 rowid,见入门篇 §3.4)
conn.execute("INSERT INTO docs_vec(rowid, embedding) VALUES(?, ?)",
             (rowid, to_blob(embed(body))))   # 同一个数灌进 vec0 的 rowid
```

查询时先 vec0 近邻拿 rowid,再 JOIN 回 `docs` 取详情:

```python
rows = conn.execute("""
    SELECT d.id, d.title, d.body, v.distance
    FROM docs_vec v
    JOIN docs d ON d.id = v.rowid      -- 按 rowid 拼回元数据
    WHERE v.embedding MATCH ?
    ORDER BY v.distance
    LIMIT 10
""", (qblob,)).fetchall()
```

这是 SQLite 当向量库最舒服的模式:**向量和结构化数据在同一库、能 JOIN、能加 WHERE 过滤**(比如「只在 source='blog' 的范围里语义搜」)。专用向量库要 JOIN 业务表就得跨系统,SQLite 这点占便宜。

## 7. 混合检索:关键词(FTS5)+ 语义(vec0)

只用向量检索会漏掉精确关键词匹配(产品名、人名、代码标识符);只用关键词检索漏掉语义近似。两者混合是生产里最常见的做法。

```sql
-- FTS5 全文表(建法和 sqlite-introduction.md §8 一致)
CREATE VIRTUAL TABLE docs_fts USING fts5(body, content='docs', content_rowid='id');
```

混合策略:两条腿各取候选,归一化后加权融合。

```python
q = "Python 爬虫"
qblob = serialize_floats(embed_query(q))

# 关键词腿:FTS5 bm25(返回非正值,越负越相关)
kw = conn.execute("""
    SELECT rowid, bm25(docs_fts) AS bm
    FROM docs_fts WHERE docs_fts MATCH ?
    ORDER BY bm LIMIT 20
""", (q,)).fetchall()

# 语义腿:vec0 cosine distance(越小越相关)
vec = conn.execute("""
    SELECT rowid, distance
    FROM docs_vec WHERE embedding MATCH ?
    ORDER BY distance LIMIT 20
""", (qblob,)).fetchall()

# 归一化 + 融合(示意):bm25 映射到 [0,1],cosine distance 映射到 [0,1]
def bm25_to_sim(bm):
    a = abs(bm); return a / (1 + a)           # 越相关越接近 1
def dist_to_sim(d):
    return max(0.0, 1.0 - d / 2)              # cosine distance ∈ [0,2]

scores = {}
for rowid, bm in kw:    scores[rowid] = scores.get(rowid, 0) + 0.3 * bm25_to_sim(bm)
for rowid, d in vec:    scores[rowid] = scores.get(rowid, 0) + 0.7 * dist_to_sim(d)

top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:10]
```

权重(0.3 文本 / 0.7 向量)是经验值,按数据调。两条腿都降级友好:没装 sqlite-vec 就退 FTS-only,没装 FTS 就退向量-only。

## 8. 完整可跑示例(Python)

下面这段不依赖任何 embedding API——用一个确定性的 mock 向量(按文本 hash 生成),保证你复制就能跑,看到效果。真实场景把 `embed()` 换成你的 embedding 模型调用即可。

```python
import sqlite3, hashlib, struct
import sqlite_vec

# --- mock embedding:文本 → 768 维伪向量(确定性,仅用于跑通) ---
def embed(text, dims=768):
    h = hashlib.md5(text.encode("utf-8")).digest()
    return [(h[i % len(h)] / 255.0) for i in range(dims)]

def to_blob(vec):
    return struct.pack(f"{len(vec)}f", *vec)

# --- 建库 + 加载扩展 ---
conn = sqlite3.connect(":memory:")
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
conn.row_factory = sqlite3.Row

conn.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, body TEXT)")
conn.execute("CREATE VIRTUAL TABLE docs_vec USING vec0(embedding float[768] distance=cosine)")

# --- 插几条文档 ---
docs = [
    "用户喜欢用 Python 写自动化脚本",
    "偏好用 Python 写爬虫抓数据",
    "团队用 Go 做后端服务",
    "前端用 React 和 TypeScript",
    "数据库选了 PostgreSQL",
]
for i, body in enumerate(docs, start=1):
    conn.execute("INSERT INTO docs(id, body) VALUES (?, ?)", (i, body))
    conn.execute("INSERT INTO docs_vec(rowid, embedding) VALUES (?, ?)",
                 (i, to_blob(embed(body))))
conn.commit()

# --- 语义检索 ---
query = "用什么语言写爬虫"
qblob = to_blob(embed(query))
rows = conn.execute("""
    SELECT d.id, d.body, v.distance
    FROM docs_vec v
    JOIN docs d ON d.id = v.rowid
    WHERE v.embedding MATCH ?
    ORDER BY v.distance
    LIMIT 3
""", (qblob,)).fetchall()

for r in rows:
    print(f"{r['distance']:.4f}  {r['body']}")
```

mock 向量没有真实语义,但能验证整条「插向量 → MATCH → JOIN」管道是通的。接真实 embedding 时,只要保证**建表维度、插入维度、查询维度三者一致**,这段代码原样能用。

## 9. 局限与替代方案

| 局限 | 说明 | 对策 |
|---|---|---|
| 维度建表时钉死 | `float[768]` 写死,换模型/维度须删表重建,数据要重新嵌入 | 上线前定好 embedding 模型;迁移时重嵌 |
| 暴力扫描,无 ANN 索引 | k-NN 对全表算距离排序,百万级开始吃力 | 十万级以内够用;更大上 pgvector(HNSW)/Qdrant |
| 单写者 | SQLite 写锁是整库级别,并发写会 `database is locked` | 一写多读开 WAL(见入门文档 §5);高并发写换专用库 |
| 扩展连接级 | 每个 connection 都要 `load` 一次 | 用连接池时记得每个新连接加载 |
| 无 GPU 加速 | 纯 CPU | 大批量嵌入/检索要 GPU 的话不在 SQLite 这一档 |

**何时该换**:向量数量到百万级、检索延迟扛不住、要多机水平扩展、要 HNSW 这类 ANN 索引——换 PostgreSQL + pgvector(还能继续用 SQL,平滑),或专用向量库(Qdrant/Milvus,有 ANN + 分布式)。

SQLite + sqlite-vec 的定位是**「向量检索的最小可行起点」**:数据量不大、不想引外部服务、要和结构化数据 JOIN 时,一个 `.db` 文件全搞定。规模撑不住再换,数据迁移成本不高(导出向量 + 重建)。

---

## 速查:sqlite-vec 关键 API

| 做什么 | 写法 |
|---|---|
| 加载扩展 | `conn.enable_load_extension(True); sqlite_vec.load(conn)` |
| 建向量表 | `CREATE VIRTUAL TABLE t USING vec0(embedding float[768] distance=cosine)` |
| 序列化向量(Python) | `sqlite_vec.serialize_floats(list)` 或 `struct.pack(f"{n}f", *vec)` |
| 序列化向量(SQL) | `vec_f32(blob_or_text)` 函数 |
| 插入 | `INSERT INTO t(rowid, embedding) VALUES(?, ?)` |
| k-NN 查询 | `SELECT rowid, distance FROM t WHERE embedding MATCH ? ORDER BY distance LIMIT k` |
| 手算距离(SQL) | `vec_distance_cosine(a, b)` / `vec_distance_L2(a, b)` |
| cosine → 相似度 | `1 - distance / 2`(distance ∈ [0,2]) |

参考:SQLite 基础见 [`sqlite-introduction.md`](./sqlite-introduction.md)(虚表与 FTS5 §8、扩展加载 §9)。
