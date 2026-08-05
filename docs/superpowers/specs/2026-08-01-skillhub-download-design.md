# SkillHub 下载安装 Skill — 设计

- 日期：2026-08-01
- 状态：已批准，待实现
- 关联：在 [`2026-07-30-skillnet-download-design.md`](./2026-07-30-skillnet-download-design.md) 落地的 SkillNet 通路（openkg 搜索 + GitHub raw 下载）旁**新增第二条来源** SkillHub，不替换、不合并 SkillNet。两来源共存于同一前端页面（来源切换），共用同一本地存储与热重载。

## 1. 背景与目标

用户希望在现有 SkillNet 来源之外，新增从 [skillhub.cn](https://www.skillhub.cn)（面向中国用户的 Skills 社区，腾讯 EdgeOne 托管，"Top 50 高质量 AI Skills"）搜索 + 下载安装 skill 的能力，且**与 SkillNet 同时保留**。SkillHub 与 SkillNet 是两套独立后端、两种不同下载机制（SkillNet 走 GitHub raw 逐文件，SkillHub 走自有 `/api/v1/download` 返回 zip）。

**成功标准**：
- 前端切到 SkillHub 来源 → 输入关键词搜索 → 结果列表点「安装」→ skill 落到 `<WORKSPACE>/skills/<name>/` → `SkillManager` mtime 热重载拾取 → 下一步 `before_model_call` 注入。
- 切回 SkillNet 来源 → 照常搜索/安装，行为与现状一致。
- 两来源安装的 skill 共存于同一 `skills/` 目录，`list_skill`/`read_skill` 工具统一可见。

## 2. 关键参考事实（已实测核实 2026-08-01）

- **SkillHub 真实 API 主机 = `api.skillhub.cn`**（不是 `www.skillhub.cn`；www 只托管 SPA 外壳，`/api/*` 在 www 上会回 `index.html`）。
- **列表/搜索（公开免鉴权）**：`GET https://api.skillhub.cn/api/skills?page=&pageSize=&sortBy=&order=&keyword=&category=&source=&labels=` → `{"code":0,"data":{"skills":[...]}}`。
  - 路径是 `/api/skills`（**无 v1**）。`sortBy=score` 生效（按 `score` 排，顶部 `score=100000`）。`keyword` 是真过滤（实测 `keyword=zzzznotexist`→0 条，`keyword=web`→5 条相关）。
  - 每个 skill 字段：`slug`（下载 key）、`name`、`namespace`（`handle`/`publicSlug`/`canonicalName` 如 `@user_ec205dbb/web-tools-guide`）、`version`（如 `1.0.2`）、`description_zh`/`description`、`score`、`downloads`、`stars`、`installs`、`source`（如 `community`）、`category`、`tags`、`labels`。
- **下载（公开免鉴权）**：`GET https://api.skillhub.cn/api/v1/download?slug=<slug>`（可选 `&namespace=&version=&tag=`） → **302** → 跳转腾讯 COS `skillhub-*.cos.accelerate.myqcloud.com/skills/<slug>/<version>.zip` → `Content-Type: application/zip`，magic `PK\x03\x04`。
  - 路径是 `/api/v1/download`（**有 v1**）。
  - 实测 `web-tools-guide@1.0.2`（13.8KB）zip 内含 **`SKILL.md` 在根目录**（+ `scripts/`、`references/` 子目录）→ 与 Twinkle 现有 `_locate_skill_dir`（`rglob("SKILL.md")`）+ `parse_skill_md` + `copytree` 安装流程**直接兼容**，无需改存储层。
- SkillHub 自带 `INSTALL_SKILL` postMessage 协议（`{slug, name, downloadUrl}`），**我们不沿用**——走后端 RPC，与 SkillNet 一致。
- 数据源：逆向前端 bundle `skill-hub.cjfvoani.js`（API base 常量 `zc`/`Hc` = `https://api.skillhub.cn`）。详见记忆 `skillhub-api-spec`。

## 3. 已锁定的关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 来源共存形态 | 页面**来源切换**（单选 toggle，一次搜一个源） | 用户选择；与现有单源 UI 一致，前端只加 toggle |
| 外部依赖 | 自实现 `httpx`，不引第三方 skillhub SDK | 符合 Twinkle 轻依赖/学习重写定位 |
| 安装执行模型 | 复用现有「后台任务 + 延迟单结果」 | server.py 路由不变；读循环不阻塞 |
| 文件组织 | 新 `skillhub.py`，**复用** `remote.py` 的 `_locate_skill_dir`/`safe_skill_name`/`safe_child_path`/`parse_skill_md`，不抽象 base 类 | 两源下载机制本就不同（GitHub raw vs zip），分文件清晰；base 类属未要求抽象（YAGNI） |
| 搜索结果类型 | **两个独立 dataclass**：`SkillNetSkill`（原 `RemoteSkill` **改名**，skillnet：`name/description/skill_url/path`）+ `SkillHubSkill`（skillhub：`name/description/slug/downloads/score/version`，放 `skillhub.py`）。rpc 按 `source` 分支各自序列化 | rpc 是 `source` 字符串分支、无多态消费者，共享类型/基类是死抽象；两独立类自描述、无空字段。改名 `RemoteSkill`→`SkillNetSkill` 为与 `SkillHubSkill` 命名对称（用户选择） |
| SkillNet 通路逻辑 | **行为不动**（注入 client、既有 skillnet 分支逻辑均不改）；`rpc.py` / `test_skill_rpc.py` 仅**增量**加 skillhub 分支与用例；`remote.py` 唯一触碰是把 `RemoteSkill` **改名**为 `SkillNetSkill`（纯重命名，字段/行为不变，机械波及 `__init__.py` 重导出 + 3 个测试文件引用） | 用户「和 skillNet 同时保留」原话；hybrid 分发让 skillhub 纯增量。改名仅替换标识符、不改行为 |
| 安装键 | `slug`-only，不传 `version`/`namespace` | 实测 `?slug=` 单参即下到 zip；version pin 为升级路径 |
| skillhub 结果行元数据 | 富：name + 描述 + downloads + score | 用户从 `sortBy=score` 入口，排名信息值得留 |
| 默认来源 | SkillHub | 新源优先；可一句话改回 SkillNet |
| 鉴权 | 无 token（skillhub 公开免鉴权） | 实测确认 |

## 4. 架构

复用现有 skill RPC 链路（gateway 零改动），新增 `SkillHubClient` 并行通路，通过 RPC 的 `source` 参数与 `SkillNetClient` 分发。两通路最终都落到同一 `<WORKSPACE>/skills/<name>/` 目录，由 `SkillManager` 的 mtime 热重载统一拾取——这是「共存」的物理基础。

```
前端 SkillsView (来源 toggle) ──request('skills.search/install', {source, ...}, timeoutMs)──> webClient
      │ ws
      ▼
Gateway message_handler ─create_task(_process_stream)─> AgentClient.send_request_stream
      │ (非阻塞)
      ▼
AgentServer ws_handler
      ├─ skills.list_local  → 内联 dispatch_skill_rpc (不变)
      └─ skills.search/install → create_task(run_skill_rpc)  (路由不变)
                                    │ 按 params.source 分发:
                                    ├─ skillnet → 注入的 client (SkillNetClient, 原样不动)
                                    └─ skillhub → get_skillhub_client() (新, 局部 import)
                                    │   搜索: api.skillhub.cn/api/skills?keyword=
                                    │   下载: api.skillhub.cn/api/v1/download?slug= → 302 → zip → 解压
                                    ▼
                              send(e2a.result) ──> 回流前端 resolver
                                    │
SkillManager.list_skills() mtime 变 → 重扫 → SkillHook 下一步注入
```

## 5. 后端组件

### 5.1 `twinkle/agentserver/skills/skillhub.py`（新）— `SkillHubClient`

config 驱动，`get_skillhub_client()` 进程级惰性单例（放 `skills/__init__.py`，仿 `get_skillnet_client()`）。镜像 `SkillNetClient` 的两方法表面，便于 `rpc.py` 统一调度。

- 数据类 `SkillHubSkill`（放 `skillhub.py`）：`name: str`、`description: str`（取 `description_zh` 优先、回退 `description`）、`slug: str`、`downloads: int = 0`、`score: int = 0`、`version: str = ""`。与 `SkillNetSkill`（原 `RemoteSkill`，见 §3 改名）并列，两源各用各的，无空字段、无共享基类。
- `search_remote_skills(q: str, force_refresh: bool = False, limit: int = 20, page: int = 1) -> list[SkillHubSkill]`
  - `GET {api}/api/skills?page=&pageSize=<limit>&sortBy=score&keyword=<q>`。
  - 解析 `{"code":0,"data":{"skills":[...]}}`；`code != 0` → 当空列表。
  - 按查询进程内缓存（独立 `_query_cache`，复用 `CATALOG_TTL` 同款 TTL 逻辑）；`force_refresh` 重拉。
  - 服务端搜索——`q` 透传给 `keyword`，非客户端拉全量过滤（与 SkillNet 一致）。
- `download_skill(slug: str) -> tuple[str, Path, Path]`（与 `SkillNetClient.download_skill` **同返回契约**：`(skill_name, skill_dir, temp_root)`）
  - `GET {api}/api/v1/download?slug=<slug>`，`follow_redirects=True`（httpx 自动跟 302 到腾讯 COS）→ `.content` 即 zip 字节。
  - `zipfile.ZipFile(io.BytesIO(body))` **逐成员**解压到 `temp_root`，每个成员路径先经 `safe_child_path(temp_root, member_path)` 校验再写（防 zip-slip：拒绝绝对路径/含 `..`/越界 `temp_root` 的条目），对齐 SkillNet 逐文件 `safe_child_path` 的安全姿态（见 §10）。
  - 复用 `remote.py` 的 `_locate_skill_dir(temp_root)`（`rglob("SKILL.md")`）+ `parse_skill_md` 取 `name`；缺 SKILL.md → `SkillHubError("下载内容未找到 SKILL.md")`；缺 name/description → `SkillHubError`。
  - 返回 `(name, skill_dir, temp_root)`；调用方负责 `copytree` 与清理（与 SkillNet 同）。
- 安全：**复用** `remote.py` 的 `safe_skill_name` / `safe_child_path`（import，不重复实现）。
- HTTP：`httpx.AsyncClient`（已是依赖）；`SkillHubError(Exception)`（对齐 `SkillNetError`）；`_get` 重试逻辑照搬 `SkillNetClient._get` 的 5xx/网络重试，但**去掉 GitHub 限流分支**（skillhub 无 token、无 403 rate-limit）。
- 复用 config 的 `remote_timeout` / `remote_max_retries`（字段语义通用，不另开）。

### 5.2 `twinkle/agentserver/skills/__init__.py`（改）

仿 `get_skillnet_client()` / `_set_skillnet_client()` 加 `get_skillhub_client()` / `_set_skillhub_client()` 进程级惰性单例，从 config 读 `SKILLS_SKILLHUB_API_URL` + `SKILLS_REMOTE_TIMEOUT` + `SKILLS_REMOTE_MAX_RETRIES`。`__all__` 加 `SkillHubClient`、`SkillHubSkill`、`SkillHubError`、两个访问器；`RemoteSkill` 随改名（在 `remote.py` 重命名为 `SkillNetSkill`），`__init__.py` 的重导出与 `__all__` 同步更新。

### 5.3 `twinkle/agentserver/skills/rpc.py`（改，最小）

`run_skill_rpc` 在 `skills.search` / `skills.install` 两分支各加 `source = envelope.params.get("source", "skillnet")` 读取 + skillhub 子分支。**SkillNet 子分支与现有 install 共用块保持原样。**

- `skills.search`：
  - skillhub → `get_skillhub_client().search_remote_skills(q, force_refresh=force)`（局部 import，仿 rpc.py:43 的 `get_skill_manager`），body `{type:"skills.search", skills:[{name, description, slug, downloads, score}]}`。
  - skillnet → 原样 `{type:"skills.search", skills:[{name, description, skill_url}]}`。
- `skills.install`：先按 source 取 `(name, skill_dir, temp_root)`——
  - skillhub → `get_skillhub_client().download_skill(params.get("slug"))`；
  - skillnet → 注入 `client.download_skill(params.get("url"))`；
  - **之后的安全校验 + 已装判断 + `copytree` + 清理块（rpc.py:72-87）两源共用，不动**。
- `handles_skill_rpc` / `dispatch_skill_rpc` 不变（仍 3 个 method）。

### 5.4 `twinkle/agentserver/server.py`（不改）

`server.py:149` `run_skill_rpc(envelope, send, get_skillnet_client())` 原样——skillhub 分支在 `run_skill_rpc` 内部局部 `get_skillhub_client()` 自取。`skill_tasks` 清理不变。

### 5.5 配置

- `twinkle/config/schema.py` `SkillsConfig` 加 `skillhub_api_url: str = "https://api.skillhub.cn"`。
- `twinkle/resources/config.yaml` `skills:` 下加 `skillhub_api_url: https://api.skillhub.cn  # SkillHub 公开列表/下载 API`。
- `twinkle/config/__init__.py` 加 `SKILLS_SKILLHUB_API_URL = settings.skills.skillhub_api_url`。
- `.env.example` 不动（无 token）。`remote_timeout` / `remote_max_retries` 复用。

## 6. 前端组件

### 6.1 `web/src/components/SkillsView.vue`（改）

- 搜索区顶部加**来源 toggle**（两个按钮 `SkillHub` / `SkillNet`），绑 `source` ref，默认 `"skillhub"`。标题随来源变（「从 SkillHub 搜索」/「从 SkillNet 搜索」）。
- `onInstall` 按来源传不同载荷：skillhub → `installSkill({source:'skillhub', slug: s.slug})`；skillnet → `installSkill({source:'skillnet', url: s.skill_url})`。
- 结果行按来源渲染：skillhub 行显示 `name + 描述 + ↓{downloads} + score 角标`；skillnet 行维持原样。
- `installing` 跟踪键改用复合键 `${source}:${slug|url}`，避免两源同名碰撞。

### 6.2 `web/src/composables/useSessions.ts`（改）

- `searchSkills(q, force, source)` → `client.request('skills.search', {q, force_refresh: force, source}, 30000)`，结果原样填 `searchResults`（字段按来源不同，模板按 `source` 分支读）。
- `installSkill({source, slug?, url?})` → `client.request('skills.install', {source, slug, url, force:false}, 120000)`。

## 7. 数据流

### 7.1 搜索

1. 前端 `request('skills.search', {q, source:'skillhub', force_refresh}, 30000)`。
2. Gateway → AgentServer → `create_task(run_skill_rpc)`（非内联，不阻塞）。
3. `run_skill_rpc`：`source=='skillhub'` → `get_skillhub_client().search_remote_skills(q, force)`（缓存命中快，冷则 1 次 `/api/skills`）→ `send(e2a.result {skills:[{name,description,slug,downloads,score}]})`。
4. 回流前端 `searchResults`，按 `source` 渲染富行。

### 7.2 安装

1. 前端 `request('skills.install', {source:'skillhub', slug, force:false}, 120000)`。
2. Gateway → AgentServer → `create_task(run_skill_rpc)`。
3. `run_skill_rpc`：`source=='skillhub'` → `get_skillhub_client().download_skill(slug)` → GET `/api/v1/download?slug=` 跟 302 → zip 字节 → `extractall(temp)` → `_locate_skill_dir` + `parse_skill_md` 取 name → `(name, skill_dir, temp_root)` → 安全校验 → 已装且 `!force` → `send({error})`；否则 `copytree(skill_dir→SKILLS_DIR/<name>)` → 清 temp → `send({ok, skill_name})`。
4. `e2a.result` → AgentClient 队列 → gateway `_process_stream` → `result` 事件 → 前端 resolver。
5. `SkillManager` 下次 `list_skills()` mtime 变 → 重扫 → 新 skill 生效。

## 8. RPC 契约

| method | params | 返回 body | 后端执行 |
|---|---|---|---|
| `skills.list_local` | `{}` | `{type:"skills.list_local", skills:[{name,description}]}` | 内联（不变） |
| `skills.search` | `{q, force_refresh?, source?="skillnet"}` | skillnet: `{type, skills:[{name,description,skill_url}]}`；skillhub: `{type, skills:[{name,description,slug,downloads,score}]}` | 后台任务 |
| `skills.install` | skillnet: `{url, force?, source?="skillnet"}`；skillhub: `{slug, force?, source:"skillhub"}` | `{type:"skills.install", ok:bool, skill_name?, error?}` | 后台任务 |
| `skills.uninstall` | `{name}` | `{type:"skills.uninstall", ok:bool, skill_name?, error?}` | 后台任务（本地瞬时，复用 install 通路；SkillManager mtime 热重载摘除） |

> 注：RPC 的 `source` 是本系统的**来源路由参数**（`skillnet` / `skillhub`），与 SkillHub 列表 API 自身的 `source` 字段/查询参（如 `community`）无关——后者不传（见 §12）。

## 9. 错误处理

- skillhub 列表 `code != 0` 或网络/超时 → `SkillHubError` → `send({error})`。
- 下载：非 zip / 空 body → `SkillHubError("下载内容为空")`；zip 内无 SKILL.md → `SkillHubError("下载内容未找到 SKILL.md")`；缺 name/description → `SkillHubError`。措辞对齐 SkillNet 友好错误风格。
- 已安装且 `!force` → 复用 rpc.py:76 的 `skill 'X' 已安装` 路径（两源共用 install 块）。
- COS 偶发 5xx → 重试 `remote_max_retries` 次后 `send({error})`。
- `slug` 缺失（前端没传）→ `send({error:"缺少 slug"})`。
- 后台任务异常 → `send({error: str(exc)})`，前端 reject（同 `dispatch_session_rpc` 失败帧模式）。
- 前端断连：任务服务端仍跑完落盘 + 热重载，仅丢成功 toast（与 SkillNet 一致，可接受）。

## 10. 安全

- name 经 `safe_skill_name` + dest 经 `safe_child_path`（防穿越），**复用 SkillNet 已有的同名函数**，对齐 jiuwenswarm。
- zip 解压防 zip-slip：逐成员经 `safe_child_path(temp_root, member)` 校验，拒绝逃逸 `temp_root` 的条目（恶意 zip 不能写到 temp 外），与 SkillNet 逐文件 raw 下载的 `safe_child_path` 姿态一致。
- 下载内容不做签名/hash 校验（来源即 SkillHub 官方 API，与 SkillNet 走 GitHub raw 一致的信任模型）。
- 临时目录下载完即清理。
- skillhub 公开免鉴权，无凭证泄露面。

## 11. 测试（`asyncio.run` + `httpx.MockTransport`，无 pytest-asyncio）

- `tests/test_skill_skillhub.py`（新，镜像 `test_skill_remote.py`）：
  - search：mock `/api/skills?keyword=` 返回 → 解析 `slug/name/description/downloads/score` 正确 + 缓存二次不发 HTTP + `force_refresh` 重拉 + `code != 0` 返空。
  - download：`MockTransport` 模拟 `/api/v1/download?slug=` → 302 + Location → COS url 返回真实 zip 字节（用 `zipfile` 现造一个含根 `SKILL.md` 的 zip）→ 解压命中 `_locate_skill_dir` + name 解析；造一个无 SKILL.md 的 zip → 报错。
  - `get_skillhub_client` 单例 + reset。
- `tests/test_skill_rpc.py`（改，加 skillhub 用例）：`skills.search` source=skillhub 透传 keyword + 不客户端过滤 + 路由到 FakeSkillHubClient；`skills.install` source=skillhub + slug 成功 copytree / 已装 / 下载错三场景。**SkillNet 既有用例不动**（hybrid 设计保证）。
- `tests/test_skill_config.py`（加 1 条）：`skillhub_api_url` 默认值 + YAML override。
- 前端：手动验收（Twinkle 无前端测试框架）。

## 12. 不做（YAGNI / 显式边界）

- 不做 SkillHub `skillsets`（skill 包）——只单个 skill。
- 不做 `namespace` / `version` / `tag` 透传——slug-only 即可下到 zip；version pin 为升级路径。
- 不做 skillhub 的 `category` / `source` / `labels` 过滤 UI——只关键词搜索（对齐 SkillNet 能力边界）。
- 不做 install_status 轮询——后台任务 + 延迟单结果（同 SkillNet）。
- 不做 boot 预热——首次 search 冷加载慢（转圈），后续缓存命中。
- 不抽象 `RemoteSkillSource` base 类——两源机制不同，分文件清晰即可（CLAUDE.md「不造抽象」）。
- 不做 zip 签名/校验——信任模型同 SkillNet（GitHub raw）。

## 13. 文档同步

- 本 spec：`docs/superpowers/specs/2026-08-01-skillhub-download-design.md`。
- 记忆 `skillhub-api-spec`（已存于 `~/.claude/.../memory/`）记录 API 实测细节，本 spec 引用之。
- `docs/superpowers/specs/2026-07-30-skillnet-download-design.md`：line 76 `list[RemoteSkill]` 同步改名为 `SkillNetSkill`（1 处，文档级，与代码改名一致）。历史 `plans/2026-07-30-skillnet-download.md` 留旧（陈旧计划记录，代码即真相，不改）。
- `docs/e2a-introduction.md`：若 §skill RPC 列了来源，补注 SkillHub 为第二来源（实现时核实该段是否存在）。
- CLAUDE.md「Conventions」的 skill 段如需提双来源，实现时一并（非必须）。

## 14. 文件清单（实现触点）

后端：
- 新 `twinkle/agentserver/skills/skillhub.py`（`SkillHubClient` + `SkillHubSkill` + `SkillHubError`）
- 改 `twinkle/agentserver/skills/remote.py`（`RemoteSkill` **改名** `SkillNetSkill`，纯重命名，字段/行为不变）
- 改 `twinkle/agentserver/skills/__init__.py`（`get_skillhub_client` / `_set_skillhub_client` + 导出；含 `RemoteSkill`→`SkillNetSkill` 重导出）
- 改 `twinkle/agentserver/skills/rpc.py`（`run_skill_rpc` search/install 加 `source` 分发）
- 改 `twinkle/config/schema.py`（`SkillsConfig.skillhub_api_url`）
- 改 `twinkle/config/__init__.py`（`SKILLS_SKILLHUB_API_URL`）
- 改 `twinkle/resources/config.yaml`（`skills.skillhub_api_url`）
- 新 `tests/test_skill_skillhub.py`
- 改 `tests/test_skill_rpc.py`（`RemoteSkill`→`SkillNetSkill` 改名 + 加 skillhub 用例）
- 改 `tests/test_agentserver_handler.py`（`RemoteSkill`→`SkillNetSkill` 改名，机械）
- 改 `tests/test_integration.py`（同上）
- 改 `tests/test_skill_config.py`（加 1 条）

前端：
- 改 `web/src/components/SkillsView.vue`（来源 toggle + 富行渲染 + 安装载荷分流）
- 改 `web/src/composables/useSessions.ts`（`searchSkills`/`installSkill` 加 `source`；TS 类型 `RemoteSkillItem`→`SkillNetSkillItem` 改名 + 新增 `SkillHubSkillItem`）
