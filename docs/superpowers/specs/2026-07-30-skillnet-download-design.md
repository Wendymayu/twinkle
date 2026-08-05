# SkillNet 一键下载安装 Skill — 设计

- 日期：2026-07-30
- 状态：已批准，待实现
- 关联：反转 `2026-07-27-skill-design.md` 中「marketplace/SkillNet 永远不做」的决定；参考 `D:\code\opensource\gitcode\jiuwenswarm` 的 `jiuwenclaw` SkillNet 安装方案。

> **⚠ 2026-07-30 数据源修正（实现时核实，覆盖下方 §2/§架构中与之冲突的描述）**：经核实 jiuwenswarm 的 SkillNet **搜索**走公开搜索 API `http://api-skillnet.openkg.cn/v1/search`（`skillnet-ai` 的 `SkillNetSearcher`，`mode=keyword`，503+ skill，含「诗词」等），**下载**才走 GitHub Contents/raw API（skill_url 指向的仓库，常为 `/blob/{sha}/...`）。故 Twinkle 的 `SkillNetClient.search_remote_skills(q)` 把 q 透传给该 API 做服务端搜索（**非**下方早期描述的「拉 `github.com/zjunlp/SkillNet` tree 全量目录 + 客户端过滤」——该仓库只有 1 个 skill，已证伪）；`download_skill(skill_url)` 走 GitHub。配置 `skills.upstream`(owner/repo/branch/skills_path) 已移除，改 `skills.skillnet_api_url`；`parse_github_url` 同时接受 `/tree/` 与 `/blob/`。下方保留原设计行文供追溯，以此注记为准。

## 1. 背景与决策反转

Twinkle 当前 skill 系统完全由本地文件系统驱动：`<WORKSPACE>/skills/<name>/SKILL.md` → `SkillManager`（`twinkle/agentserver/skills/store.py`）基于 mtime 签名惰性热重载 → `SkillHook` 在 `before_model_call` 注入清单 + `list_skill`/`read_skill` 两个 @tool。**没有任何远程 install/download 通路，也没有任何面向前端的 skill RPC。**

旧设计 `docs/superpowers/specs/2026-07-27-skill-design.md` 与 `docs/e2a-introduction.md`（第 423-432 行）曾把 `skills.skillnet.*` / marketplace / install / download 列为「永久砍掉」。

**决策反转（2026-07-30，用户拍板）**：用户希望「页面点击就下载 skill」，参考 jiuwenswarm 的 SkillNet 方案。用户意图优先于旧文档。本功能重新纳入，并同步更新旧文档避免后人困惑。

## 2. 关键参考事实（已核实）

- **jiuwenswarm SkillNet 本质**（实现时核实）：**搜索**走 SkillNet 公开搜索 API `http://api-skillnet.openkg.cn/v1/search`（`skillnet-ai` 的 `SkillNetSearcher`，`mode=keyword`，503+ skill）；**下载**走 GitHub Contents/raw API（`skillnet-ai` 的 `SkillDownloader`，逐文件下载到临时目录再 `copytree` 进 skills 目录）。安装走「异步后台任务 + 前端轮询 install_status」。三套来源（SkillNet / ClawHub / OpenJiuwen），本功能只做 SkillNet。Twinkle 自实现搜索（openkg API）+ 下载（GitHub API）两条通路，不引入 skillnet-ai 依赖。
- **twinkle 现有 RPC 链路开箱即用**：后端仿 `sessions/handlers.py` 加 dispatch，yield `E2AResponse(response_kind="e2a.result", is_final=True, body={...})`；gateway `message_handler._process_stream` 自动映射成浏览器 `result` 事件；前端 `webClient.request()` 的 pending resolver 收。**gateway 层零改动**。
- **并发模型（决定安装执行模型）**：
  - Gateway 侧 `handle_message` 对每个请求 `asyncio.create_task(self._process_stream(...))` —— 不阻塞。
  - AgentServer 侧 `ws_handler` 读循环 `async for raw in ws:` 中，session RPC 是**内联 await**（`async for frame in dispatch_session_rpc(...): await send(frame)`），只有 chat 才 `create_task`。慢操作若走内联 RPC，会堵住整条 Gateway↔AgentServer 连接的读循环（正是 jiuwenswarm 当初改异步的原因）。
  - `AgentClient.send_request_stream` **无读超时**，会一直等到 final 帧 or 连接断开 —— 延迟结果在 AgentServer↔Gateway 链路不会丢。
  - 前端 `webClient.request()` 超时**硬编码 15s 且无入参覆盖** —— 需加可选 `timeoutMs` 参数。

## 3. 目标与成功标准

用户在 web 端新增的「Skills」页输入关键词搜索 SkillNet 仓库的 skill，结果列表点「安装」→ skill 自动落到 `<WORKSPACE>/skills/<name>/`，下次模型调用即被 `SkillManager` mtime 热重载拾取，无需手动下载/拷贝。

验收：
- 安装后 `SkillManager.list_skills()` 能看到该 skill。
- `SkillHook` 下一步 `before_model_call` 注入它。
- 前端「已安装」列表刷新出现。
- search 关键词能从缓存目录里过滤出对应 skill。

## 4. 已锁定的关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 页面交互形态 | 搜索 + 安装（仿 jiuwenswarm） | 用户选择 |
| 搜索质量 | 关键词过滤即可（name/description 子串匹配） | 用户选择；无需语义/向量搜索 |
| 外部依赖 | **不引入 skillnet-ai，自实现** GitHub Tree/Contents API 枚举 + raw 下载 | 符合 Twinkle 轻依赖/学习重写定位 |
| 安装执行模型 | **后台任务 + 延迟单结果**（非轮询） | 用户选择；读循环立即释放不阻塞；服务端完成 + 热重载让「断连丢 toast」后果可接受；异步+轮询为升级路径 |

## 5. 架构

复用 twinkle 现有 RPC 链路，gateway 零改动。仿 jiuwenswarm「后端从 GitHub 下载」但自实现。**所有网络 I/O（search / install）统一走后台任务 + 延迟单结果，不阻塞 AgentServer 读循环**；只有纯本地的 `skills.list_local` 内联。

```
前端 SkillsView ──request('skills.search/install', …, timeoutMs)──> webClient
      │ ws req
      ▼
Gateway message_handler.handle_message ──create_task(_process_stream)──> AgentClient.send_request_stream
      │ (非阻塞, 各请求独立任务)
      ▼
AgentServer ws_handler
      ├─ skills.list_local  → 内联 async for dispatch_skill_rpc (快, 纯本地)
      └─ skills.search/install → create_task(run_skill_rpc)  (读循环立即继续)
                                          │ 搜索: openkg API / 下载: GitHub Contents/raw API
                                          ▼
                                   SkillNetClient (自实现, 查询缓存)
                                          │ copytree 到 <WORKSPACE>/skills/<name>
                                          ▼
                              send(e2a.result) ──> 回流到前端 resolver
                                          │
SkillManager.list_skills() mtime 变 → 重扫 → SkillHook 下一步注入
```

## 6. 后端组件（新增）

### 6.1 `twinkle/agentserver/skills/remote.py` — `SkillNetClient`

config 驱动，`get_skillnet_client()` 进程级惰性单例（放 `skills/__init__.py`，仿 `get_skill_manager()`）。

- `fetch_remote_skills(force_refresh: bool = False) -> list[SkillNetSkill]`
  - Tree API：`GET https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1`（1 次，拿全仓库路径）。
  - 筛出 `{skills_path}/<name>/SKILL.md` 路径。
  - 批量 raw 拉 SKILL.md（`https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}`），**复用 `skills/store.py::parse_skill_md`** 解析 `name`/`description`。
  - **内存缓存 + TTL（1h）**；`force_refresh=True` 或 TTL 过期则重拉。返回 `[{name, description, skill_url, path}]`。
  - `skill_url` 构造为 `https://github.com/{owner}/{repo}/tree/{branch}/{skills_path}/{name}`（前端展示 + 作为 install 入参）。
- `download_skill(skill_url: str, dest_root: Path) -> tuple[str, Path]`
  - 解析 GitHub URL → `(owner, repo, branch, path)`。
  - Contents API（`GET /repos/{o}/{r}/contents/{path}?ref={branch}`）枚举该 path 下文件（子目录递归）。
  - raw 下载每个文件到 `tempfile.TemporaryDirectory`。
  - 解析临时目录里的 SKILL.md 取 `name`（复用 `parse_skill_md`）。
  - 返回 `(skill_name, temp_dir)`。调用方负责 `copytree` + 清理。
- 安全（对齐 jiuwenswarm `_safe_path_name`/`_safe_child_path`）：
  - `safe_skill_name(name)`：拒绝 `.`/`..`/含斜杠/绝对路径。
  - `safe_child_path(base, *parts)`：解析后须 `relative_to(base_resolved)`，防穿越。
- HTTP 客户端：优先 `httpx.AsyncClient`（若已是依赖，LLM 栈通常已带）；否则 stdlib `urllib` + `asyncio.to_thread`。实现时先确认依赖现状。
- 鉴权：可选 `GITHUB_TOKEN`（HTTP `Authorization: Bearer <token>`）；空 = 匿名（~60/次）。无 token 时命中 403 限流返回带限流上下文的友好错误。

### 6.2 `twinkle/agentserver/skills/rpc.py` — skill RPC dispatch

仿 `sessions/handlers.py`。

- `handles_skill_rpc(method) -> bool`：`method in {"skills.search", "skills.list_local", "skills.install"}`。
- `dispatch_skill_rpc(envelope, store) -> AsyncIterator[E2AResponse]`：**仅处理内联的 `skills.list_local`**，yield 单个 `e2a.result`（失败 yield `status="failed"` 的 result 帧，body 带 `error`，前端 `request()` 因 `payload.error` reject —— 同 `dispatch_session_rpc` 模式）。
- `async def run_skill_rpc(envelope, send, client, store) -> None`：处理 **`skills.search` / `skills.install`**（非内联，由 `ws_handler` 起 `create_task`）。
  - `skills.search`：读 `params.force_refresh` 传给 `client.fetch_remote_skills(force_refresh=...)` → 关键词过滤 name/description（大小写不敏感子串）→ `send(e2a.result {type, skills:[...]})`。
  - `skills.install`：`client.download_skill(url, SKILLS_DIR)` → 安全校验 → 已存在且 `!force` → `send({error:"skill 'X' 已安装"})`；否则 `copytree(temp→dest)` → 清 temp → `send({ok, skill_name})`；异常 → `send({error: str(exc)})`。

### 6.3 `server.py::ws_handler` 路由新增

在现有 `approval.respond` / session RPC / chat 分支之间插入：

```python
if envelope.method == "skills.list_local":
    async for frame in dispatch_skill_rpc(envelope, store):
        await send(frame)
    continue
if envelope.method in ("skills.search", "skills.install"):
    t = asyncio.create_task(run_skill_rpc(envelope, send, get_skillnet_client(), store))
    skill_tasks.add(t)
    t.add_done_callback(skill_tasks.discard)
    continue
```

并在 handler 闭包加 `skill_tasks: set[asyncio.Task] = set()`；`finally` 里 `for t in skill_tasks: t.cancel()` + `gather`。

### 6.4 配置 — `twinkle/resources/config.yaml` + `twinkle/config/schema.py`

`skills:` 下新增（不破坏现有 `dir`/`mode`/`enabled`）：

```yaml
skills:
  upstream:                 # 新增
    owner: zjunlp
    repo: SkillNet
    branch: main
    skills_path: skills
  github_token: ${TWINKLE_GITHUB_TOKEN:-}   # 可选；空=匿名(60/次)
  remote_timeout: 60          # 新增
  remote_max_retries: 3       # 新增
```

`SkillsConfig` schema 加 `upstream: UpstreamConfig`、`github_token: str=""`、`remote_timeout: int=60`、`remote_max_retries: int=3`；在 `config/__init__.py` 暴露常量。`.env.example` 加 `TWINKLE_GITHUB_TOKEN=` 一行 + 注释。

## 7. 前端组件（新增）

### 7.1 `web/src/services/webClient.ts`
`request(method, params, timeoutMs = 15000)`：把硬编码 15000 改为参数默认值。install 用 120000，search 用 30000。

### 7.2 `web/src/composables/useSessions.ts`
- `NavKey` 加 `'skills'`。
- 新增 `searchResults` ref、`installedSkills` ref、`skillsLoading` ref。
- `searchSkills(q, forceRefresh=false)`：`client.request('skills.search', {q, force_refresh: forceRefresh}, 30000)` → 填 `searchResults`。
- `loadInstalled()`：`client.request('skills.list_local', {})` → 填 `installedSkills`。
- `installSkill(url)`：`client.request('skills.install', {url, force:false}, 120000)`。

### 7.3 `web/src/App.vue`
侧栏加 `<button @click="setNav('skills')" ...>Skills</button>`；`<main>` 加 `<SkillsView v-if="activeNav==='skills'"/>`。

### 7.4 `web/src/components/SkillsView.vue`（新）
- 顶部：搜索框 + 「搜索」按钮 + 「刷新目录」按钮（调 `searchSkills(q, forceRefresh=true)` 重拉目录并过滤）。
- 结果列表：每行 name / description / 「安装」按钮；命中 `installedSkills` 则置灰显示「已安装」。
- 下方：「已安装」只读列表（name / description）。
- 交互：点「安装」→ 该行按钮转圈 → 成功 toast + `loadInstalled()` 刷新；失败 toast。
- 进入页面时 `loadInstalled()`。

## 8. 数据流

### 8.1 安装
1. 前端 `request('skills.install', {url, force:false}, 120000)`。
2. Gateway 包 `E2AEnvelope` → `AgentClient.send_request_stream`（无读超时）。
3. AgentServer `ws_handler`：`method=='skills.install'` → `create_task(run_skill_rpc(...))`，读循环立即继续。
4. `run_skill_rpc`：解析 url → Contents API 枚举 → raw 下载 temp → `parse_skill_md` 取 name → 安全校验 → 已存在且 `!force` → `send({error})`；否则 `copytree(temp→dest)` → 清 temp → `send({ok, skill_name})`；异常 → `send({error})`。
5. `e2a.result` → AgentClient 队列 → gateway `_process_stream` → `result` 事件 → 前端 resolver。
6. `SkillManager` 下次 `list_skills()` mtime 变 → 重扫 → 新 skill 生效。

### 8.2 搜索
1. 前端 `request('skills.search', {q}, 30000)`。
2. Gateway → AgentServer → `create_task(run_skill_rpc)`（非内联，冷缓存慢也不阻塞）。
3. `run_skill_rpc`：`client.fetch_remote_skills()`（缓存命中快，冷则 1 Tree + N raw）→ 关键词过滤 → `send(e2a.result {skills:[...]})`。
4. 回流到前端 `searchResults`，渲染。
5. 首次冷缓存较慢（前端转圈）；后续命中缓存快。「刷新目录」按钮调 `searchSkills(q, forceRefresh=true)` 强制重拉目录并过滤。

## 9. RPC 契约

| method | params | 返回 body | 后端执行 |
|---|---|---|---|
| `skills.search` | `{q, force_refresh?}` | `{type:"skills.search", skills:[{name,description,skill_url}]}` | 后台任务 |
| `skills.list_local` | `{}` | `{type:"skills.list_local", skills:[{name,description}]}` | 内联 |
| `skills.install` | `{url, force?}` | `{type:"skills.install", ok:bool, skill_name?, error?}` | 后台任务 |

## 10. 错误处理

- GitHub 限流（403 + `X-RateLimit-Remaining:0`）→ `error: "GitHub 匿名限流(60/次)，配置 TWINKLE_GITHUB_TOKEN 或稍后重试"`。
- 网络/超时 → 重试 `remote_max_retries` 后 `error`。
- 已安装且 `!force` → `error: "skill 'X' 已安装"`（前端先标已装，主要兜底）。
- 下载内容无 SKILL.md / 缺 name/description → `error`。
- 路径穿越（name 含 `..`/斜杠）→ 拒绝。
- 后台任务异常 → `send({error: str(exc)})`，前端 reject（同 `dispatch_session_rpc` 失败帧模式）。
- 前端断连：任务服务端仍跑完落盘 + 热重载，仅丢成功 toast（可接受）。
- GitHub URL 解析失败（非 `github.com/.../tree/...`）→ `error: "无法解析 GitHub skill URL"`。

## 11. 安全

- name 经 `safe_skill_name` + dest 经 `safe_child_path`（防穿越），对齐 jiuwenswarm。
- 下载内容不做签名/hash 校验（来源即 GitHub raw，与 jiuwenswarm SkillNet 一致）。
- `github_token` 不回传前端（只读服务端）。
- 临时目录下载完即清理。

## 12. 测试（`asyncio.run` + free_port，无 pytest-asyncio）

- `tests/test_skill_remote.py`：
  - mock GitHub Tree/raw 响应 → `fetch_remote_skills` 解析正确 + 缓存二次不发 HTTP + `force_refresh` 重拉。
  - `download_skill` 文件落盘 + name 解析 + 路径穿越 name 拒绝 + force 覆盖/已存在拒绝。
  - 限流 403 → 友好错误。
- `tests/test_skill_rpc.py`：
  - 注入 fake `SkillNetClient` → `skills.search` 关键词过滤（含/不包含/空查询）。
  - `skills.list_local` 内联返回本地 skill 清单。
  - `skills.install` 经 `ws_handler` 断言**起后台任务不阻塞**（紧随其后的请求不被堵）+ 最终 `e2a.result` 帧到达（成功 / 失败 / 已安装 三场景）。
- 前端：手动验收（twinkle 无前端测试框架）。

## 13. 不做（YAGNI / 显式边界）

- 不做卸载（uninstall）—— 只读「已安装」列表。需要时再加。
- 不做 ClawHub / OpenJiuwen 来源 —— 只 SkillNet。
- 不做语义/向量搜索 —— 只关键词过滤。
- 不做 skill 版本号 / 依赖管理（requirements.txt 安装）—— skill = 含 SKILL.md 的目录。
- 不做 install_status 轮询 —— 后台任务 + 延迟单结果即可。**成功/失败仍由那一个延迟返回的 `e2a.result` 帧触发 toast 告知用户**（`request()` resolve/reject）；轮询只是用来报进度和抗断连，非告知结果的必要手段。（断连恢复、进度反馈为升级路径，暂不做。）
- 不做 boot 预热目录缓存 —— 首次 search 冷加载慢（转圈），后续缓存命中。
- 不做 zipball 整仓下载优化 —— Tree + raw 逐文件，规避仓库体积风险。

## 14. 文档同步

- `docs/superpowers/specs/2026-07-27-skill-design.md`：删改「marketplace/SkillNet 永远不做」段落，注明 2026-07-30 按「参考 jiuwenswarm」重新纳入，指向本 spec。
- `docs/e2a-introduction.md` 第 423-432 行：把 `skills.search`/`skills.install`/`skills.list_local` 从「砍掉」移到「已实现」。
- 本 spec：`docs/superpowers/specs/2026-07-30-skillnet-download-design.md`。

## 15. 文件清单（实现触点）

后端：
- 新 `twinkle/agentserver/skills/remote.py`（`SkillNetClient` + 安全函数）
- 新 `twinkle/agentserver/skills/rpc.py`（`handles_skill_rpc` / `dispatch_skill_rpc` / `run_skill_rpc`）
- 改 `twinkle/agentserver/skills/__init__.py`（`get_skillnet_client()` 单例 + 导出）
- 改 `twinkle/agentserver/server.py`（`ws_handler` 路由分支 + `skill_tasks` 清理）
- 改 `twinkle/config/schema.py`（`SkillsConfig` 新字段 + `UpstreamConfig`）
- 改 `twinkle/config/__init__.py`（暴露常量）
- 改 `twinkle/resources/config.yaml`（`skills.upstream` 等）
- 改 `.env.example`（`TWINKLE_GITHUB_TOKEN`）
- 新 `tests/test_skill_remote.py` / `tests/test_skill_rpc.py`

前端：
- 改 `web/src/services/webClient.ts`（`request` 加 `timeoutMs`）
- 改 `web/src/composables/useSessions.ts`（`NavKey` + skills 状态/方法）
- 改 `web/src/App.vue`（侧栏按钮 + view）
- 新 `web/src/components/SkillsView.vue`

文档：
- 改 `docs/superpowers/specs/2026-07-27-skill-design.md`
- 改 `docs/e2a-introduction.md`
