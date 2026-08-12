# SQLite 入门(给 MySQL 用户)

这篇假设你会 MySQL,但从没碰过 SQLite。目标:读完能直接上手用。全程拿 MySQL 做对照——差异讲清楚,相同的只点一句不展开。

## 1. 心智模型:SQLite 和 MySQL 的根本差别

这是最该先建立的认知,后面所有差异都从这里来。

| 维度 | MySQL | SQLite |
|---|---|---|
| 形态 | **服务进程**,客户端通过网络连 | **嵌入进程的库**,没有服务进程 |
| 「数据库」是什么 | 服务里的一个逻辑库 | **一个文件**(如 `test.db`) |
| 用户/权限 | 有用户、密码、GRANT 权限 | **没有**。能读文件就能读写,没有认证 |
| 连接方式 | `mysql -h host -u user -p` | 打开一个文件路径,进程内 API |
| 部署 | 装服务、起服务、建库建用户 | **零配置**,文件不存在自动建 |
| 并发 | 多连接并发读写,行锁 | **单写多读**,写锁是整库级别 |
| 适用 | 多用户、高并发、网络访问 | 嵌入式、单机、单写、跟随文件走 |

一句话:**MySQL 是个服务,SQLite 是个会读写文件的库**。你「打开数据库」就是「打开一个文件」,`sqlite3.connect("test.db")` 文件不存在就直接创建。没有 host/port/user/password,没有 `CREATE DATABASE`、`GRANT`——这些概念在 SQLite 里都不存在。

## 2. 三分钟上手

### 2.1 命令行 `sqlite3`

**装**:Windows 用 `winget install sqlite.org.sqlite` 或下 [sqlite.org](https://sqlite.org/download.html) 的工具包;macOS 自带;Linux 一般自带或 `apt install sqlite3`。

**创建/打开一个库**:

```bash
sqlite3 test.db          # 文件不存在就创建;存在就打开
sqlite3 :memory:        # 内存库,进程退出就没,适合试东西
```

进了交互界面后,提示符是 `sqlite>`。两种命令要分清:

- **点命令**(以 `.` 开头,不是 SQL,是 CLI 工具的元命令)
- **SQL 语句**(以 `;` 结尾)

```sql
sqlite> CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
sqlite> INSERT INTO users(name, age) VALUES('alice', 30), ('bob', 25);
sqlite> SELECT * FROM users;
1|alice|30
2|bob|25
```

常用点命令:

| 命令 | 作用 | MySQL 里对应 |
|---|---|---|
| `.tables` | 列出所有表 | `SHOW TABLES;` |
| `.schema users` | 看建表语句 | `SHOW CREATE TABLE users;` |
| `.help` | 帮助 | `help;` |
| `.quit` / `.exit` | 退出 | `exit;` |
| `.read file.sql` | 执行一个 SQL 文件 | `source file.sql;` |
| `.headers on` | 查询结果显示列名 | 默认就显示 |
| `.mode column` | 列对齐显示(好看) | — |

**查表结构的 SQL 写法**(记不住点命令时也能用):

```sql
SELECT name, sql FROM sqlite_master WHERE type='table';   -- 等价 SHOW TABLES + SHOW CREATE
PRAGMA table_info(users);                                  -- 等价看列定义
```

`sqlite_master` 是 SQLite 存所有表/索引元信息的内置表,作用类似 MySQL 的 `information_schema`。

### 2.2 Python(Python 用户最常见用法)

SQLite 最大的卖点就是**Python 标准库自带**(`import sqlite3`),不用装任何东西:

```python
import sqlite3

conn = sqlite3.connect("test.db")   # 文件不存在自动建;":memory:" 是内存库
conn.row_factory = sqlite3.Row       # 让结果行能按列名取(row["name"])
cur = conn.cursor()

cur.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
cur.execute("INSERT INTO users(name, age) VALUES(?, ?)", ("alice", 30))   # ? 防注入
cur.executemany("INSERT INTO users(name, age) VALUES(?, ?)",
                [("bob", 25), ("carol", 40)])
conn.commit()                        # 改完必须 commit 才落盘

cur.execute("SELECT * FROM users WHERE age > ?", (20,))
for row in cur.fetchall():
    print(row["id"], row["name"], row["age"])

conn.close()
```

要点:

- **`?` 占位符**,不是 MySQL/Python 里常见的 `%s`。**永远用参数化,别字符串拼接拼 SQL**——SQL 注入就这来的。
- **改完必须 `commit()`**。Python sqlite3 默认开隐式事务,DML 不 commit 不落盘。`executemany` 批量插。
- **`conn.execute(...)` 是简写**(等价于拿 cursor 再 execute),简单场景不用显式 cursor。
- 连接用完 `close()`,或用 `with sqlite3.connect(...) as conn:`(退出自动 commit,但不自动 close)。

## 3. 数据类型:和 MySQL 差别最大的地方

SQLite 的类型系统是它最容易让 MySQL 用户栽跟头的地方。MySQL 是**严格类型**(声明 `INT` 插字符串会报错或强转报 warning),SQLite 是**动态类型**。

### 3.1 五大存储类

SQLite 只有 **5 个存储类**(比 MySQL 的类型少很多):

| 存储类 | 存什么 | MySQL 大致对应 |
|---|---|---|
| `INTEGER` | 整数(按值大小 1/2/4/8 字节) | `INT` / `BIGINT` |
| `TEXT` | 文本,变长 | `VARCHAR` / `TEXT` |
| `REAL` | 浮点(8 字节 IEEE) | `DOUBLE` / `FLOAT` |
| `BLOB` | 二进制,按字节原样存 | `BLOB` / `BINARY` |
| `NULL` | 没有值 | `NULL` |

注意没有 `DECIMAL`(用 `REAL` 或 TEXT 存)、没有 `CHAR(n)`/`VARCHAR(n)` 长度语义(声明了也忽略,TEXT 变长)、没有 `ENUM`/`SET`。

### 3.2 类型亲和(type affinity)——动态类型

SQLite 建表时声明的列类型只是个**「亲和」建议**,不是强制约束。你往 `INTEGER` 列插字符串 `'30'` 它会转成数字 `30`;往 `TEXT` 列插什么都当文本;甚至往任何列插任何类型都不报错:

```sql
CREATE TABLE t(a INTEGER);
INSERT INTO t VALUES('hello');     -- 不报错!存成 TEXT
SELECT typeof(a) FROM t;           -- 返回 'text'
```

这很灵活,但**bug 也藏在这**——你以为存了数字,其实存了字符串,比较和聚合时结果就诡异。实战纪律:声明贴近期望类型,靠参数化传对类型,别依赖 SQLite 替你纠错。

MySQL 里声明 `INT` 插 `'hello'` 会报错或强转 + warning,SQLite 不会——这是最要记住的差异。

### 3.3 那些 SQLite 没有的类型

| 你在 MySQL 用的 | SQLite 怎么办 |
|---|---|
| `BOOLEAN` / `TINYINT(1)` | **没有独立布尔类型**,存 `0`/`1`(`INTEGER`)。`TRUE`/`FALSE` 关键字是 `1`/`1` 的别名 |
| `DATE` / `DATETIME` / `TIMESTAMP` | **没有原生日期类型**。约定存 `TEXT`(ISO 字符串 `'2026-08-10 14:30:00'`)或 `INTEGER`(unix 秒)。用日期函数处理 |
| `DECIMAL(10,2)` | 用 `REAL`(注意浮点精度)或 TEXT 存,自己处理 |
| `AUTO_INCREMENT` | 见下面 §3.4 |

### 3.4 自增主键:`INTEGER PRIMARY KEY` vs `AUTO_INCREMENT`

这是和 MySQL 差别明显的点:

```sql
-- MySQL
CREATE TABLE t(id INT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(50));

-- SQLite
CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT);
```

SQLite 里 `INTEGER PRIMARY KEY` 这列**自动就是自增的**(它是内置 `rowid` 的别名),**不需要**写 `AUTO_INCREMENT`。

那 SQLite 也有 `AUTOINCREMENT` 关键字,它和 MySQL 的 `AUTO_INCREMENT` 语义不同:

- 默认(`INTEGER PRIMARY KEY`):删掉一行后,新插入的行可能**复用**被删的最大 rowid。
- 加 `AUTOINCREMENT`(`id INTEGER PRIMARY KEY AUTOINCREMENT`):rowid **只增不减、永不复用**,即使删光也记着上次的最高值。代价是多一张 `sqlite_sequence` 表。

一般用默认就够;只有在你不希望 ID 复用(比如对外暴露的 ID)时才加 `AUTOINCREMENT`。

## 4. SQL 语法:你会 MySQL 就会大半

SELECT / INSERT / UPDATE / DELETE / JOIN / GROUP BY / 子查询这些**和 MySQL 几乎一样**,不展开。下面只讲**差异和易错点**。

### 4.1 建表

```sql
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT UNIQUE,
  age INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);
```

`IF NOT EXISTS`、`NOT NULL`、`UNIQUE`、`DEFAULT` 都和 MySQL 一样。`DEFAULT (datetime('now'))` 用函数表达式做默认值(SQLite 3.31+ 支持表达式默认值)。

### 4.2 插入与冲突处理(SQLite 特有)

```sql
-- 普通
INSERT INTO users(name, email) VALUES('alice', 'a@x.com');

-- 冲突时忽略(MySQL 也有 INSERT IGNORE)
INSERT OR IGNORE INTO users(name, email) VALUES('alice', 'a@x.com');

-- 冲突时整行替换(MySQL 的 REPLACE INTO)
INSERT OR REPLACE INTO users(name, email) VALUES('alice', 'a@x.com');

-- 真正的 UPSERT(MySQL 的 INSERT ... ON DUPLICATE KEY UPDATE;SQLite 3.24+)
INSERT INTO users(name, email) VALUES('alice', 'a@x.com')
  ON CONFLICT(email) DO UPDATE SET name = excluded.name;
```

`excluded` 是 SQLite 在 `ON CONFLICT` 里指代「将要插入但冲突的那行」的别名,对应 MySQL 的 `VALUES(name)`。

### 4.3 字符串拼接:`||` 不是 `CONCAT()`

```sql
-- MySQL
SELECT CONCAT(first, ' ', last) FROM users;
-- SQLite
SELECT first || ' ' || last FROM users;
```

SQLite 用 `||` 拼字符串(这是 SQL 标准),**没有 `CONCAT()` 函数**。

### 4.4 LIMIT 两种写法都支持

```sql
SELECT * FROM users LIMIT 10;             -- 取 10 条
SELECT * FROM users LIMIT 20, 10;         -- 跳过 20 取 10(逗号语法,和 MySQL 一样)
SELECT * FROM users LIMIT 10 OFFSET 20;   -- 同上,更清晰的写法
```

### 4.5 日期处理(因为没有原生日期类型)

日期存成 TEXT(ISO)后,用 SQLite 的日期函数操作,这些函数能识别多种格式:

```sql
SELECT date('now');                       -- 今天 '2026-08-10'
SELECT datetime('now');                   -- 现在 '2026-08-10 14:30:00'
SELECT date('now', '+7 days');            -- 一周后
SELECT strftime('%Y-%m', created_at) FROM users;   -- 从 TEXT 列里取年月
SELECT * FROM users WHERE created_at >= date('now', '-30 days');  -- 最近 30 天
```

记住:**日期是 TEXT/INTEGER,靠这些函数解释**,不是原生类型。存的时候约定用 ISO 格式(`'2026-08-10 14:30:00'`),字符串排序天然就是时间排序。

### 4.6 常用函数对照

| 想做的事 | MySQL | SQLite |
|---|---|---|
| 字符串长度 | `CHAR_LENGTH(s)` / `LENGTH(s)` | `LENGTH(s)` |
| 取子串 | `SUBSTRING(s, 1, 3)` | `SUBSTR(s, 1, 3)` |
| 大小写 | `UPPER` / `LOWER` | `UPPER` / `LOWER` |
| 拼接 | `CONCAT(a, b)` | `a || b` |
| 当前时间 | `NOW()` / `CURDATE()` | `datetime('now')` / `date('now')` |
| 取年份 | `YEAR(d)` | `strftime('%Y', d)` |
| 聚合 | `COUNT`/`SUM`/`MAX`/`MIN`/`AVG` | 一样 |

## 5. 并发与性能

MySQL 是多连接并发,SQLite 不是。理解它的并发模型避免 `database is locked`:

- **默认模式(rollback journal)**:写的时候整库加锁,写期间别人连读都不行(读也要等写完)。适合「单写者」。
- **WAL 模式**(`PRAGMA journal_mode=WAL`):读写分离,写时不阻塞读、多读可并发,但仍**只有一个写者**。适合「一写多读」。代价是多两个文件(`-wal`、`-shm`)。

```sql
PRAGMA journal_mode=WAL;        -- 切到 WAL,多读一写场景必开
PRAGMA busy_timeout=5000;       -- 遇到锁时等 5 秒再报错(默认 0,立即报 locked)
```

Python 多线程访问同一个 connection 默认会报错(`check_same_thread=True`),要么每线程一个 connection,要么 `connect(..., check_same_thread=False)`(但要自己保证线程安全,并配 WAL)。

**什么时候 SQLite 顶不住**:多进程/多机高并发写同一份数据、写 QPS 高、要网络访问——这些场景回到 MySQL/PostgreSQL。SQLite 的甜区是单机、单写或低并发写、数据量中小、库跟随应用文件走。

## 6. 常用 PRAGMA 速查

`PRAGMA` 是 SQLite 的配置开关语法(MySQL 用 `SET`):

| PRAGMA | 作用 |
|---|---|
| `PRAGMA journal_mode=WAL;` | 切 WAL 日志模式(一写多读并发) |
| `PRAGMA busy_timeout=5000;` | 锁等待超时毫秒 |
| `PRAGMA foreign_keys=ON;` | 开外键约束(**默认关**,和 MySQL 默认开相反) |
| `PRAGMA table_info(表名);` | 看列定义 |
| `PRAGMA user_version=1;` | 给库打版本号(做迁移用) |
| `PRAGMA foreign_key_list(表名);` | 看外键 |

注意:**外键约束 SQLite 默认是关的**——你建表声明了 `FOREIGN KEY` 也不生效,除非每次连接 `PRAGMA foreign_keys=ON`。MySQL 用户特别容易在这栽:以为声明了就有约束,其实没有。

## 7. MySQL → SQLite 速查对照表

| 想做的事 | MySQL | SQLite |
|---|---|---|
| 连库 | `mysql -h host -u user -p db` | `sqlite3 file.db` |
| 列出表 | `SHOW TABLES;` | `.tables` / `SELECT name FROM sqlite_master WHERE type='table';` |
| 看建表语句 | `SHOW CREATE TABLE t;` | `.schema t` |
| 看列 | `DESC t;` / `SHOW COLUMNS FROM t;` | `PRAGMA table_info(t);` |
| 自增主键 | `INT PRIMARY KEY AUTO_INCREMENT` | `INTEGER PRIMARY KEY`(自带) |
| 插入忽略冲突 | `INSERT IGNORE` | `INSERT OR IGNORE` |
| 替换插入 | `REPLACE INTO` | `INSERT OR REPLACE` |
| Upsert | `ON DUPLICATE KEY UPDATE` | `ON CONFLICT(col) DO UPDATE SET` |
| 字符串拼接 | `CONCAT(a,b)` | `a \|\| b` |
| 当前时间 | `NOW()` | `datetime('now')` |
| 布尔 | `TRUE`/`FALSE`(TINYINT) | `1`/`1`(INTEGER) |
| 日期列 | `DATE`/`DATETIME` | TEXT(ISO) 或 INTEGER |
| 外键约束 | 默认开 | **默认关**,要 `PRAGMA foreign_keys=ON` |
| 标识符引用 | 反引号 `` ` `` | 双引号 `"` |
| 切库 | `USE db;` | 连另一个文件 |
| 建用户 | `CREATE USER` | 不存在 |

## 8. 虚拟表(virtual table)

前面所有表都是 SQLite 用内置 B-tree 存的普通表。**虚拟表**不同:它的存储和查询由一个**模块(module)**实现,数据可以不在 B-tree 里——可以存在自定义格式里、当场算出来、甚至来自外部数据源。对调用方来说它长得像普通表,`SELECT`/`INSERT`/`JOIN` 都能用,但底下是模块在拦截。

### 8.1 建法:`CREATE VIRTUAL TABLE ... USING`

```sql
CREATE VIRTUAL TABLE docs_fts USING fts5(title, body);
```

读法:`USING fts5(title, body)` 里 `fts5` 是模块名,**括号里的 `title, body` 就是这张表的列定义**——等价于普通表 `CREATE TABLE docs_fts(title TEXT, body TEXT)` 的列清单,只是挪进了模块调用的括号里。FTS5 的列隐式都是 TEXT(全文检索只索引文本),所以不写类型;普通表那种「列名在表名后的括号里 + 声明类型」的写法,在虚表里合并成了 `USING 模块(列1, 列2, ...)`。模块决定这张虚表怎么存数据、支持哪些操作。

和普通表的关键差异:

| 维度 | 普通表 | 虚拟表 |
|---|---|---|
| 存储 | B-tree,SQLite 自己管 | 模块自己定(可以是另一种文件格式,甚至现算) |
| 建表 | `CREATE TABLE` | `CREATE VIRTUAL TABLE ... USING 模块(参数)` |
| 支持的操作 | 增删改查全支持 | **模块声明哪些就支持哪些**——比如 `vec0` 只支持特定 `INSERT`/`MATCH` 查询,不能随便 `UPDATE` |
| 看定义 | `sqlite_master.sql` | 同样能查,但底层结构由模块管 |

**模块来源**:SQLite 自带 `fts5`(全文检索)、`rtree`(空间索引)等;第三方能注册更多模块,比如 `sqlite-vec` 的 `vec0` 向量检索模块(见向量篇)。第三方模块要靠下一节「扩展加载」挂进来,`USING vec0(...)` 才认得。

### 8.2 内置模块 FTS5:把 SQLite 当全文搜索引擎

`FTS5` 是 SQLite 自带的全文检索模块,建一张虚表存倒排索引,用 `MATCH` 做关键词查询、`bm25()` 按相关性排序。这是实战里最常用的虚表。

```sql
-- 建全文索引表(列就是要检索的文本列)
CREATE VIRTUAL TABLE docs_fts USING fts5(title, body);

INSERT INTO docs_fts(title, body) VALUES('SQLite 入门', 'SQLite 是个嵌入式的...');
INSERT INTO docs_fts(title, body) VALUES('MySQL 调优', 'MySQL 是个服务...');

-- MATCH 做全文查询
SELECT rowid, title FROM docs_fts WHERE docs_fts MATCH 'SQLite';
-- bm25() 是函数不是表:bm25(docs_fts) 给每行算相关性(非正值,越负越相关),
-- AS score 把它起成一列,ORDER BY score 按相关性排序
SELECT rowid, bm25(docs_fts) AS score
FROM docs_fts WHERE docs_fts MATCH 'SQLite OR 嵌入'
ORDER BY score;
```

要点:

- **`MATCH`** 是 FTS5 注入的查询操作符,支持布尔(`AND`/`OR`/`NOT`)、前缀(`word*`)、短语(`"a b"`)等语法。
- **`bm25()` 是个函数,不是表**。`bm25(docs_fts)` 拿 FTS5 表名当参数,为当前匹配到的每行算一个 BM25 相关性得分(非正值,越负越相关);上面 `AS score` 把这个值起成一列,结果集里就多出 `score` 列,`ORDER BY score` 就是按相关性排——全程没有单独的「bm25 表」,它只是查询时现算的一列。
- **上面这版 FTS5 表自己存正文**:`title`/`body` 的文本本身就存在 `docs_fts` 里(连同倒排索引一起),所以这例子里只有 `docs_fts` 一张表,没有别的表。但如果你除了要检索的文本,还要存别的列(状态、来源、外键……)或要 JOIN 别的表,把文本在 FTS5 表里再存一份就重复了——这时用下面的「外部内容表」:FTS5 只维护索引,正文从你那张普通表里取。

外部内容表(contentless / external content)写法——和向量篇里用的就是这种:

```sql
CREATE TABLE docs(id INTEGER PRIMARY KEY, title TEXT, body TEXT);   -- 普通表,存正文和其他列
CREATE VIRTUAL TABLE docs_fts USING fts5(
  title, body,
  content='docs',            -- FTS5 只维护倒排索引，数据实际存在 docs 表
  content_rowid='id'         -- 靠这一列和业务表关联
);
-- 插业务表后,同步往 FTS5 插(或建触发器自动同步)
INSERT INTO docs(id, title, body) VALUES(1, 'SQLite', '...');
INSERT INTO docs_fts(rowid, title, body) VALUES(1, 'SQLite', '...');
```

### SQLite全文检索机制

SQLite 的全文检索（FTS5）与传统的 `LIKE '%keyword%'` 精确子串匹配不同，它本质上是一种**基于分词（Tokenization）的匹配机制**，在这一点上与 ElasticSearch 的核心原理类似，但在功能复杂度和生态上相对轻量。

**1. 基于分词器（Tokenizer）的匹配**
FTS5 在建立索引时，会使用分词器将文本拆解为独立的词元（Tokens）。默认情况下，它会根据空格和标点符号进行分词，并转换为小写。因此，当你执行 `MATCH 'SQLite'` 时，它是在查找包含该词元的文档，而不是做纯粹的字符串子串匹配。

**2. 支持前缀匹配与短语匹配**
除了基础的词元匹配，FTS5 还支持：

- **前缀匹配**：使用 `*` 通配符，例如 `MATCH 'lin*'` 可以匹配包含 "linux" 或 "link" 等以 "lin" 开头的词。
- **短语匹配**：使用双引号，例如 `MATCH '"full-text search"'` 会精确匹配这个连续的短语。

**3. 局限性：不支持词内子串匹配（Trigram）**
这是 SQLite FTS5 与 ElasticSearch 等高级搜索引擎的一个显著区别。FTS5 默认**不支持**匹配嵌入在单词内部的子串。例如，查询 `MATCH 'cot'` 无法匹配到 "Scott" 或 "cottage" 中的 "cot"。
*注：虽然 PostgreSQL 等数据库有内置的 Trigram（三元组）索引来解决这个问题，SQLite 官方并未直接内置此功能。如果要在 SQLite 中实现类似词内子串的搜索，通常需要借助自定义的 FTS5 Tokenizer（如社区实现的 trigram 分词器）或通过特定的表结构（如后缀表）来变通实现。*

**4. 支持布尔逻辑与相关性排序**
FTS5 支持 `AND`、`OR`、`NOT` 等布尔查询语法，并且内置了 `bm25()` 等辅助函数，能够像 ElasticSearch 一样根据词频、文档长度等维度计算相关性得分，从而对搜索结果进行排序。

**总结**
SQLite 的 FTS5 是**分词匹配**，而非简单的精确子串匹配。它具备倒排索引、分词、相关性打分等现代全文检索引擎的核心特征，非常适合中小型应用和嵌入式场景。但如果你的业务强依赖于“词内任意子串搜索”或复杂的自然语言处理（如中文深度分词、同义词扩展等），则可能需要引入 ElasticSearch 或在 SQLite 上进行深度的自定义扩展。

## 9. 扩展加载(extension loading)

SQLite 核心能**在运行时加载扩展**(`.dll` / `.so` / `.dylib`),扩展可以注册新函数、新排序规则、新虚表模块。`sqlite-vec` 就是这样一个扩展——它注册了 `vec0` 模块和 `vec_f32()` 等函数。扩展没装/没加载,`USING vec0(...)` 就报错找不到模块。

### 9.1 在 Python 里加载

```python
import sqlite3

conn = sqlite3.connect(":memory:")
conn.enable_load_extension(True)          # ① 先开"允许加载扩展"开关(默认关)
conn.load_extension("C:/path/to/sqlite_vec")  # ② 按路径加载,不带后缀,自动找 .dll/.so/.dylib
conn.enable_load_extension(False)         # ③ 加载完关上(安全起见)

# 验证:扩展注册的函数能用了
print(conn.execute("select vec_version()").fetchone()[0])
```

三步缺一不可,顺序固定:**开开关 → 加载 → 关开关**。

带 Python 绑定的扩展(如 `sqlite-vec`)通常提供加载助手,省得自己拼路径:

```python
import sqlite3, sqlite_vec
conn = sqlite3.connect("vectors.db")
sqlite_vec.load(conn)        # 内部走 C API 注册,等价于上面 ①②③
```

要点和坑:

- **`enable_load_extension(True)` 是必须的前置开关**。SQLite 默认禁止加载扩展(防止一个 `.db` 文件触发任意代码执行),必须显式打开。
- **扩展是连接级的**。挂在 A 连接上的扩展,B 连接用不了——每个新连接都要 `load` 一次。用连接池时记得在「建连接」的钩子里加载。
- **有些 Python 构建彻底禁用了扩展加载**,`enable_load_extension` 会抛 `AttributeError` 或报错。官方 python.org 的 Windows/macOS 构建允许;部分 Linux 发行版为安全关掉了。报错时换一个允许扩展的构建,或用 `sqlite-vec` 这类自带可加载扩展文件的包。
- **CLI 也能加载**:`sqlite3 vectors.db ".load sqlite_vec"`(`.load` 后跟扩展文件路径,可省后缀)。

### 9.2 安全注意

加载扩展等于让数据库进程执行外部代码——**只加载你信任的来源的扩展**。默认关闭开关就是这个意思:你不能从一个不信任的 `.db` 里被动触发代码执行,必须自己主动 `enable_load_extension(True)` 并指定路径。生产里加载完立刻关上开关是好习惯。
