# SkillNet 一键下载安装 Skill — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** web 端「Skills」页搜索 SkillNet 公开目录（`api-skillnet.openkg.cn`）可装 skill，点「安装」即从 skill_url 指向的 GitHub 仓库下载到 `<WORKSPACE>/skills/` 并被 SkillManager 热重载拾取，无需手动拷贝。

**Architecture:** 后端自实现 搜索（openkg 公开 API `http://api-skillnet.openkg.cn/v1/search`，mode=keyword，服务端搜索）+ 下载（GitHub Contents/raw API，逐文件到临时目录再 copytree）客户端（不引入 skillnet-ai）；search/install 走「后台任务 + 延迟单结果」非阻塞 RPC，list_local 内联；复用现有 `e2a.result`→`result` 事件链路，gateway 零改动。前端加 SkillsView 页 + 侧栏入口。

**Tech Stack:** Python 3.11+ / httpx（GitHub API）/ pydantic / websockets；Vue 3 + TS（无前端测试框架，前端手动验收）；pytest（无 pytest-asyncio，`asyncio.run`）。

**Spec:** `docs/superpowers/specs/2026-07-30-skillnet-download-design.md`

> **⚠ 2026-07-30 数据源修正（实现时核实，覆盖下方任务中与之冲突的代码/描述）**：下方任务的 `fetch_remote_skills`（拉 `zjunlp/SkillNet` tree 全量 + 客户端过滤）已证伪（该仓库仅 1 skill）。实际实现改为 `search_remote_skills(q)` 调 openkg 公开搜索 API（服务端搜索，q 透传）；`download_skill` 不变（GitHub Contents/raw API）。配置 `skills.upstream` 块已移除，改 `skills.skillnet_api_url`；`parse_github_url` 同时接受 `/tree/` 与 `/blob/`（SkillNet API 返回 `/blob/{sha}/...`）。下方任务行文保留供追溯，以本注记 + 实际代码为准。

---

## 提交约定

按项目约定（memory `no-direct-github-push`）：**每次 commit 前与用户确认，不主动 push**。下列 commit 步骤在你确认后执行；执行时可在每个 Task 末尾的检查点批量确认以减少打断。`git add` 可先行（不改历史），`git commit` 待确认。

## 文件结构

后端：
- 新 `twinkle/agentserver/skills/remote.py` — `SkillNetClient`（fetch_remote_skills 缓存目录 + download_skill 下载到 temp）+ `RemoteSkill`/`SkillNetError` + 安全函数 `safe_skill_name`/`safe_child_path` + `parse_github_url`。职责：与 GitHub API 交互，纯传输，不碰 SKILLS_DIR。
- 新 `twinkle/agentserver/skills/rpc.py` — `handles_skill_rpc` / `dispatch_skill_rpc`（仅 list_local，内联）/ `run_skill_rpc`（search/install，非内联）。职责：RPC 编排，调 SkillNetClient + SkillManager，发 e2a.result。
- 改 `twinkle/agentserver/skills/__init__.py` — 加 `get_skillnet_client()`/`_set_skillnet_client()` 单例（照 `get_skill_manager` 形态）。
- 改 `twinkle/agentserver/server.py` — `ws_handler` 加 skill 路由分支 + `skill_tasks` 清理。
- 改 `twinkle/config/schema.py` / `__init__.py` / `resources/config.yaml` / `.env.example` — skills 新增 upstream + token + remote 超时/重试。

前端：
- 改 `web/src/services/webClient.ts` — `request` 加 `timeoutMs` 参数。
- 改 `web/src/composables/useSessions.ts` — `NavKey` 加 `skills` + skills 状态/方法。
- 改 `web/src/App.vue` — 侧栏按钮 + view。
- 新 `web/src/components/SkillsView.vue` — 搜索/结果/安装/已安装面板。

测试：
- 新 `tests/test_skill_config.py` / `tests/test_skill_remote.py` / `tests/test_skill_rpc.py`。

文档：
- 改 `docs/e2a-introduction.md` / `docs/superpowers/specs/2026-07-27-skill-design.md` — 反转「SkillNet 永远不做」表述。

---

## Task 1: 配置 — skills 新增 upstream / github_token / remote 字段

**Files:**
- Modify: `twinkle/config/schema.py`（`SkillsConfig` + 新 `UpstreamConfig`）
- Modify: `twinkle/config/__init__.py`（暴露常量）
- Modify: `twinkle/resources/config.yaml`（`skills:` 段）
- Modify: `.env.example`
- Test: `tests/test_skill_config.py`

- [ ] **Step 1: 写失败测试**

`tests/test_skill_config.py`：
```python
from twinkle.config import settings, SKILLS_UPSTREAM_OWNER, SKILLS_UPSTREAM_REPO, SKILLS_UPSTREAM_BRANCH, SKILLS_UPSTREAM_SKILLS_PATH, SKILLS_GITHUB_TOKEN, SKILLS_REMOTE_TIMEOUT, SKILLS_REMOTE_MAX_RETRIES


def test_skills_upstream_defaults():
    assert settings.skills.upstream.owner == "zjunlp"
    assert settings.skills.upstream.repo == "SkillNet"
    assert settings.skills.upstream.branch == "main"
    assert settings.skills.upstream.skills_path == "skills"
    assert SKILLS_UPSTREAM_OWNER == "zjunlp"
    assert SKILLS_UPSTREAM_REPO == "SkillNet"
    assert SKILLS_GITHUB_TOKEN == ""
    assert SKILLS_REMOTE_TIMEOUT == 60.0
    assert SKILLS_REMOTE_MAX_RETRIES == 3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'SKILLS_UPSTREAM_OWNER'`

- [ ] **Step 3: 实现 schema**

在 `twinkle/config/schema.py` 的 `SkillsConfig` **之前**加 `UpstreamConfig`，并扩展 `SkillsConfig`：
```python
class UpstreamConfig(_StrictModel):
    owner: str = "zjunlp"
    repo: str = "SkillNet"
    branch: str = "main"
    skills_path: str = "skills"


class SkillsConfig(_StrictModel):
    dir: str = ""  # "" -> <workspace>/skills
    mode: SkillMode = "all"
    enabled: list[str] = []  # [] = all skills open
    upstream: UpstreamConfig = UpstreamConfig()
    github_token: str = ""  # "" = anonymous (60/hour)
    remote_timeout: float = 60.0
    remote_max_retries: int = 3
```

- [ ] **Step 4: 暴露常量**

在 `twinkle/config/__init__.py` 的 `# --- skills (Phase 7) ---` 段末尾追加：
```python
SKILLS_UPSTREAM_OWNER = settings.skills.upstream.owner
SKILLS_UPSTREAM_REPO = settings.skills.upstream.repo
SKILLS_UPSTREAM_BRANCH = settings.skills.upstream.branch
SKILLS_UPSTREAM_SKILLS_PATH = settings.skills.upstream.skills_path
SKILLS_GITHUB_TOKEN = settings.skills.github_token
SKILLS_REMOTE_TIMEOUT = settings.skills.remote_timeout
SKILLS_REMOTE_MAX_RETRIES = settings.skills.remote_max_retries
```

- [ ] **Step 5: 更新 config.yaml**

把 `twinkle/resources/config.yaml` 的 `skills:` 段改为：
```yaml
skills:
  dir: ${TWINKLE_SKILLS_DIR:-}            # 空 → <workspace>/skills
  mode: all                               # all = 每步注入 skill 清单;auto_list = 模型按需调 list_skill 拉
  enabled: []                             # 列表;空 = 全开
  upstream:                               # SkillNet 远程来源(github.com/{owner}/{repo}/tree/{branch}/{skills_path})
    owner: zjunlp
    repo: SkillNet
    branch: main
    skills_path: skills
  github_token: ${TWINKLE_GITHUB_TOKEN:-}  # 可选;空=匿名(~60/次),配 token 提额度
  remote_timeout: 60.0                    # GitHub API 单次请求超时秒
  remote_max_retries: 3                   # 瞬时错误(5xx/网络)重试次数
```

- [ ] **Step 6: 更新 .env.example**

在 `.env.example` 末尾追加：
```
# --- skills remote (SkillNet 一键安装) ---
# 可选 GitHub token,提升匿名 60/小时限流额度。留空=匿名。
TWINKLE_GITHUB_TOKEN=
```

- [ ] **Step 7: 跑测试确认通过**

Run: `python -m pytest tests/test_skill_config.py -v`
Expected: PASS。另跑 `python -m pytest tests/ -q` 确认没破坏现有 config 相关测试。

- [ ] **Step 8: 提交（待用户确认）**

```bash
git add twinkle/config/schema.py twinkle/config/__init__.py twinkle/resources/config.yaml .env.example tests/test_skill_config.py
git commit -m "feat(skills): add upstream/github_token/remote config for SkillNet"
```

---

## Task 2: remote.py 纯函数 — RemoteSkill / SkillNetError / safe_skill_name / safe_child_path / parse_github_url

**Files:**
- Create: `twinkle/agentserver/skills/remote.py`
- Test: `tests/test_skill_remote.py`

- [ ] **Step 1: 写失败测试**

`tests/test_skill_remote.py`（先只测纯函数）：
```python
import pytest

from twinkle.agentserver.skills.remote import (
    SkillNetError, safe_skill_name, safe_child_path, parse_github_url,
)


def test_safe_skill_name_accepts_plain():
    assert safe_skill_name("foo") == "foo"


def test_safe_skill_name_rejects_traversal():
    for bad in ["..", ".", "a/b", "a\\b", "/abs"]:
        with pytest.raises(SkillNetError):
            safe_skill_name(bad)


def test_safe_child_path_accepts_inside(tmp_path):
    p = safe_child_path(tmp_path, "foo")
    assert p == tmp_path.resolve() / "foo"


def test_safe_child_path_rejects_escape(tmp_path):
    with pytest.raises(SkillNetError):
        safe_child_path(tmp_path, "..", "x")


def test_parse_github_url_ok():
    assert parse_github_url("https://github.com/zjunlp/SkillNet/tree/main/skills/foo") == (
        "zjunlp", "SkillNet", "main", "skills/foo")


def test_parse_github_url_bad():
    for bad in ["https://example.com/x", "https://github.com/zjunlp/SkillNet",
                "https://github.com/zjunlp/SkillNet/blob/main/skills/foo"]:
        with pytest.raises(SkillNetError):
            parse_github_url(bad)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_remote.py -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.agentserver.skills.remote`

- [ ] **Step 3: 写 remote.py（纯函数部分）**

`twinkle/agentserver/skills/remote.py`：
```python
"""SkillNet 远程客户端 — 从 GitHub 仓库拉取可装 skill 目录、下载单个 skill 到临时目录。
自实现 GitHub Tree/Contents/raw API(不依赖 skillnet-ai),对齐 jiuwenswarm 的 SkillDownloader
但极简。一个 "remote skill" = {name, description, skill_url, path};目录进程内缓存(TTL)。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class RemoteSkill:
    name: str
    description: str
    skill_url: str        # github.com/{owner}/{repo}/tree/{branch}/{skills_path}/{name}
    path: str             # 仓库相对路径 {skills_path}/{name}/SKILL.md


class SkillNetError(Exception):
    """用户可见的 SkillNet 错误(限流/坏 URL/越界等)。"""


def safe_skill_name(name: str) -> str:
    """拒绝可逃逸 skills 目录的名字。对齐 jiuwenswarm _safe_path_name。"""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise SkillNetError(f"非法 skill 名: {name!r}")
    if Path(name).is_absolute():
        raise SkillNetError(f"非法 skill 名: {name!r}")
    return name


def safe_child_path(base: Path, *parts: str) -> Path:
    """解析 base/parts 并确保结果仍在 base 下(防穿越)。"""
    base_resolved = base.resolve()
    candidate = (base_resolved / Path(*parts)).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        raise SkillNetError(f"路径越界: {candidate} 不在 {base_resolved} 下")
    return candidate


def parse_github_url(url: str) -> tuple[str, str, str, str]:
    """解析 github.com/{owner}/{repo}/tree/{ref}/{path} → (owner, repo, ref, path)。"""
    p = urlparse(url)
    if p.netloc != "github.com" or not p.path.startswith("/"):
        raise SkillNetError(f"无法解析 GitHub skill URL: {url}")
    parts = [c for c in p.path.split("/") if c]
    # [owner, repo, "tree", ref, path...]
    if len(parts) < 4 or parts[2] != "tree":
        raise SkillNetError(f"无法解析 GitHub skill URL: {url}")
    owner, repo, ref = parts[0], parts[1], parts[3]
    path = "/".join(parts[4:]) if len(parts) > 4 else ""
    if not owner or not repo or not ref:
        raise SkillNetError(f"无法解析 GitHub skill URL: {url}")
    return owner, repo, ref, path
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_skill_remote.py -v`
Expected: PASS（6 个测试）。

- [ ] **Step 5: 提交（待用户确认）**

```bash
git add twinkle/agentserver/skills/remote.py tests/test_skill_remote.py
git commit -m "feat(skills): add SkillNet remote client scaffolding + path/url safety"
```

---

## Task 3: SkillNetClient._get + fetch_remote_skills（目录拉取 + 缓存）

**Files:**
- Modify: `twinkle/agentserver/skills/remote.py`
- Test: `tests/test_skill_remote.py`（追加）

- [ ] **Step 1: 追加失败测试**

在 `tests/test_skill_remote.py` 顶部加 `import asyncio, httpx`，并追加：
```python
from twinkle.agentserver.skills.store import parse_frontmatter  # 仅为理解格式
from twinkle.agentserver.skills.remote import SkillNetClient


def _client(handler, **kw):
    transport = httpx.MockTransport(handler)
    return SkillNetClient(owner="zjunlp", repo="SkillNet", branch="main",
                           skills_path="skills", _transport=transport, **kw)


def test_fetch_remote_skills_parses_and_caches():
    calls = {"n": 0}
    tree = {"tree": [
        {"type": "blob", "path": "skills/foo/SKILL.md"},
        {"type": "blob", "path": "skills/bar/SKILL.md"},
        {"type": "blob", "path": "README.md"},
    ]}
    foo_md = "---\nname: foo\ndescription: foo skill\n---\nbody"
    bar_md = "---\nname: bar\ndescription: bar skill\n---\nbody"

    def handler(request):
        calls["n"] += 1
        u = str(request.url)
        if u.endswith("git/trees/main?recursive=1"):
            return httpx.Response(200, json=tree)
        if u.endswith("/main/skills/foo/SKILL.md"):
            return httpx.Response(200, text=foo_md)
        if u.endswith("/main/skills/bar/SKILL.md"):
            return httpx.Response(200, text=bar_md)
        return httpx.Response(404)

    c = _client(handler)
    skills = asyncio.run(c.fetch_remote_skills())
    assert [s.name for s in skills] == ["foo", "bar"]
    assert skills[0].description == "foo skill"
    assert skills[0].skill_url == "https://github.com/zjunlp/SkillNet/tree/main/skills/foo"
    n0 = calls["n"]
    # 缓存命中:二次不发 HTTP
    asyncio.run(c.fetch_remote_skills())
    assert calls["n"] == n0
    # force_refresh 重拉
    asyncio.run(c.fetch_remote_skills(force_refresh=True))
    assert calls["n"] > n0


def test_fetch_remote_skills_rate_limit_is_friendly():
    def handler(request):
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})
    c = _client(handler)
    with pytest.raises(SkillNetError, match="限流"):
        asyncio.run(c.fetch_remote_skills())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_remote.py::test_fetch_remote_skills_parses_and_caches -v`
Expected: FAIL — `AttributeError: 'SkillNetClient' object has no attribute 'fetch_remote_skills'`

- [ ] **Step 3: 实现 _get + fetch_remote_skills**

在 `remote.py` 顶部 import 块加：
```python
import logging
import shutil
import tempfile
import time

import httpx

from twinkle.agentserver.skills.store import parse_frontmatter, parse_skill_md

log = logging.getLogger("twinkle.agentserver.skills.remote")

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
CATALOG_TTL = 3600.0
```

在 `parse_github_url` 之后加 `SkillNetClient`：
```python
class SkillNetClient:
    def __init__(self, owner: str, repo: str, branch: str, skills_path: str,
                 github_token: str = "", timeout: float = 60.0,
                 max_retries: int = 3, ttl: float = CATALOG_TTL,
                 _transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._owner = owner
        self._repo = repo
        self._branch = branch
        self._skills_path = skills_path.strip("/")
        self._token = github_token
        self._timeout = timeout
        self._max_retries = max_retries
        self._ttl = ttl
        self._transport = _transport
        self._cache: list[RemoteSkill] | None = None
        self._cache_at: float = 0.0

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """GET + 重试。限流/404 即时致命;5xx/网络错误重试 max_retries 次。"""
        for attempt in range(self._max_retries + 1):
            try:
                r = await client.get(url, headers=self._headers(), timeout=self._timeout)
            except httpx.RequestError as e:
                if attempt == self._max_retries:
                    raise SkillNetError(f"GitHub 网络错误: {e}")
                continue
            if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
                raise SkillNetError("GitHub 匿名限流(60/次)，配置 TWINKLE_GITHUB_TOKEN 或稍后重试")
            if r.status_code == 404:
                raise SkillNetError(f"GitHub 404: {url}")
            if 500 <= r.status_code < 600 and attempt < self._max_retries:
                continue
            if 200 <= r.status_code < 300:
                return r
            raise SkillNetError(f"GitHub HTTP {r.status_code}")
        raise SkillNetError("GitHub 请求失败: 超出重试")

    async def fetch_remote_skills(self, force_refresh: bool = False) -> list[RemoteSkill]:
        if self._cache is not None and not force_refresh and (time.monotonic() - self._cache_at) < self._ttl:
            return self._cache
        async with httpx.AsyncClient(transport=self._transport) as client:
            tree_url = f"{GITHUB_API}/repos/{self._owner}/{self._repo}/git/trees/{self._branch}?recursive=1"
            tree = (await self._get(client, tree_url)).json()
            entries: list[RemoteSkill] = []
            skill_paths = []
            for item in tree.get("tree", []):
                if item.get("type") != "blob":
                    continue
                p = item.get("path", "")
                parts = p.split("/")
                if len(parts) == 3 and parts[0] == self._skills_path and parts[2] == "SKILL.md":
                    skill_paths.append(p)
            for p in skill_paths:
                raw_url = f"{GITHUB_RAW}/{self._owner}/{self._repo}/{self._branch}/{p}"
                text = (await self._get(client, raw_url)).text
                fm = parse_frontmatter(text)
                if fm is None:
                    continue
                name = fm.get("name", "")
                desc = fm.get("description", "")
                if not name or not desc:
                    continue
                skill_name = p.split("/")[1]
                skill_url = f"https://github.com/{self._owner}/{self._repo}/tree/{self._branch}/{self._skills_path}/{skill_name}"
                entries.append(RemoteSkill(name=name, description=desc, skill_url=skill_url, path=p))
        self._cache = entries
        self._cache_at = time.monotonic()
        return entries
```

> 注：catalog 用 `parse_frontmatter`（文本级）解析；download 用 `parse_skill_md`（磁盘级），都复用 `store.py`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_skill_remote.py -v`
Expected: PASS（含缓存 + 限流）。

- [ ] **Step 5: 提交（待用户确认）**

```bash
git add twinkle/agentserver/skills/remote.py tests/test_skill_remote.py
git commit -m "feat(skills): fetch SkillNet catalog via Tree API + cache with TTL"
```

---

## Task 4: SkillNetClient.download_skill（Contents API 递归下载到 temp）

**Files:**
- Modify: `twinkle/agentserver/skills/remote.py`
- Test: `tests/test_skill_remote.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
def test_download_skill_writes_files_and_parses_name(tmp_path):
    contents = {
        "skills/foo/SKILL.md": "---\nname: foo\ndescription: foo\n---\nbody",
        "skills/foo/helper.py": "print(1)",
    }

    def handler(request):
        u = str(request.url)
        if "/contents/skills/foo?ref=main" in u:
            return httpx.Response(200, json=[
                {"type": "file", "path": "skills/foo/SKILL.md",
                 "download_url": "https://raw.githubusercontent.com/zjunlp/SkillNet/main/skills/foo/SKILL.md"},
                {"type": "file", "path": "skills/foo/helper.py",
                 "download_url": "https://raw.githubusercontent.com/zjunlp/SkillNet/main/skills/foo/helper.py"},
            ])
        if u.endswith("/main/skills/foo/SKILL.md"):
            return httpx.Response(200, text=contents["skills/foo/SKILL.md"])
        if u.endswith("/main/skills/foo/helper.py"):
            return httpx.Response(200, text=contents["skills/foo/helper.py"])
        return httpx.Response(404)

    c = _client(handler)
    name, skill_dir, temp_root = asyncio.run(c.download_skill(
        "https://github.com/zjunlp/SkillNet/tree/main/skills/foo"))
    try:
        assert name == "foo"
        assert (skill_dir / "SKILL.md").read_text(encoding="utf-8").startswith("---")
        assert (skill_dir / "helper.py").read_text(encoding="utf-8") == "print(1)"
    finally:
        import shutil as _s
        _s.rmtree(temp_root, ignore_errors=True)


def test_download_skill_missing_skill_md_raises(tmp_path):
    def handler(request):
        u = str(request.url)
        if "/contents/skills/foo?ref=main" in u:
            return httpx.Response(200, json=[
                {"type": "file", "path": "skills/foo/helper.py",
                 "download_url": "https://raw.githubusercontent.com/zjunlp/SkillNet/main/skills/foo/helper.py"},
            ])
        if u.endswith("/main/skills/foo/helper.py"):
            return httpx.Response(200, text="print(1)")
        return httpx.Response(404)

    c = _client(handler)
    with pytest.raises(SkillNetError, match="SKILL.md"):
        asyncio.run(c.download_skill("https://github.com/zjunlp/SkillNet/tree/main/skills/foo"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_remote.py::test_download_skill_writes_files_and_parses_name -v`
Expected: FAIL — `AttributeError: 'SkillNetClient' object has no attribute 'download_skill'`

- [ ] **Step 3: 实现 download_skill + _download_tree + _locate_skill_dir**

在 `remote.py` 的 `SkillNetClient` 内 `fetch_remote_skills` 之后加：
```python
    async def download_skill(self, skill_url: str) -> tuple[str, Path, Path]:
        """下载 skill_url 指向的 skill 到临时目录;返回 (skill_name, skill_dir, temp_root)。
        调用方负责 copytree skill_dir→SKILLS_DIR 与清理 temp_root。"""
        owner, repo, ref, path = parse_github_url(skill_url)
        temp_root = Path(tempfile.mkdtemp(prefix="twinkle_skillnet_"))
        try:
            async with httpx.AsyncClient(transport=self._transport) as client:
                await self._download_tree(client, owner, repo, ref, path, temp_root)
            skill_dir = _locate_skill_dir(temp_root)
            if skill_dir is None:
                raise SkillNetError("下载内容未找到 SKILL.md")
            skill = parse_skill_md(skill_dir)
            if skill is None:
                raise SkillNetError("下载的 SKILL.md 缺少 name/description")
            return skill.name, skill_dir, temp_root
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise

    async def _download_tree(self, client: httpx.AsyncClient, owner: str, repo: str,
                              ref: str, path: str, dest: Path) -> None:
        if not path:
            return
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
        data = (await self._get(client, url)).json()
        if isinstance(data, dict):
            data = [data]
        for entry in data:
            etype = entry.get("type")
            ep = entry.get("path", "")
            if not ep:
                continue
            if etype == "file":
                dl = entry.get("download_url") or f"{GITHUB_RAW}/{owner}/{repo}/{ref}/{ep}"
                content = (await self._get(client, dl)).content
                target = dest / ep
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            elif etype == "dir":
                await self._download_tree(client, owner, repo, ref, ep, dest)
```

在模块级（`SkillNetClient` 之后）加：
```python
def _locate_skill_dir(root: Path) -> Path | None:
    """递归找第一个 SKILL.md,返回其所在目录。"""
    for sk in root.rglob("SKILL.md"):
        return sk.parent
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_skill_remote.py -v`
Expected: PASS（全部）。

- [ ] **Step 5: 提交（待用户确认）**

```bash
git add twinkle/agentserver/skills/remote.py tests/test_skill_remote.py
git commit -m "feat(skills): download a single SkillNet skill via Contents API to temp dir"
```

---

## Task 5: get_skillnet_client() 单例

**Files:**
- Modify: `twinkle/agentserver/skills/__init__.py`
- Test: `tests/test_skill_remote.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
def test_get_skillnet_client_singleton_and_reset():
    from twinkle.agentserver.skills import get_skillnet_client, _set_skillnet_client
    fake = SkillNetClient(owner="x", repo="y", branch="main", skills_path="s")
    _set_skillnet_client(fake)
    try:
        assert get_skillnet_client() is fake
    finally:
        _set_skillnet_client(None)
    # 重置后构造真实单例(读 config)
    c = get_skillnet_client()
    assert c._owner == "zjunlp" and c._repo == "SkillNet"
    assert get_skillnet_client() is c
    _set_skillnet_client(None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_remote.py::test_get_skillnet_client_singleton_and_reset -v`
Expected: FAIL — `ImportError: cannot import name 'get_skillnet_client'`

- [ ] **Step 3: 实现**

改 `twinkle/agentserver/skills/__init__.py`，在 `get_skill_manager` 之前加 import，在 `_set_skill_manager` 之后加单例：
```python
from twinkle.agentserver.skills.remote import SkillNetClient, SkillNetError, RemoteSkill

_SKILLNET_CLIENT: SkillNetClient | None = None


def get_skillnet_client() -> SkillNetClient:
    """进程级单例(惰性,从 config 构造)。测试用 _set_skillnet_client 替换。
    lazy import config 避免 import-time 副作用。"""
    global _SKILLNET_CLIENT
    if _SKILLNET_CLIENT is None:
        from twinkle.config import (
            SKILLS_UPSTREAM_OWNER, SKILLS_UPSTREAM_REPO, SKILLS_UPSTREAM_BRANCH,
            SKILLS_UPSTREAM_SKILLS_PATH, SKILLS_GITHUB_TOKEN,
            SKILLS_REMOTE_TIMEOUT, SKILLS_REMOTE_MAX_RETRIES,
        )
        _SKILLNET_CLIENT = SkillNetClient(
            owner=SKILLS_UPSTREAM_OWNER, repo=SKILLS_UPSTREAM_REPO,
            branch=SKILLS_UPSTREAM_BRANCH, skills_path=SKILLS_UPSTREAM_SKILLS_PATH,
            github_token=SKILLS_GITHUB_TOKEN, timeout=SKILLS_REMOTE_TIMEOUT,
            max_retries=SKILLS_REMOTE_MAX_RETRIES,
        )
    return _SKILLNET_CLIENT


def _set_skillnet_client(c: SkillNetClient | None) -> None:
    """测试钩子:替换/重置单例。生产代码不调。"""
    global _SKILLNET_CLIENT
    _SKILLNET_CLIENT = c
```

并把 `__all__` 改为：
```python
__all__ = [
    "Skill", "SkillManager", "parse_skill_md",
    "get_skill_manager", "_set_skill_manager",
    "SkillNetClient", "SkillNetError", "RemoteSkill",
    "get_skillnet_client", "_set_skillnet_client",
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_skill_remote.py -v`
Expected: PASS（全部）。

- [ ] **Step 5: 提交（待用户确认）**

```bash
git add twinkle/agentserver/skills/__init__.py tests/test_skill_remote.py
git commit -m "feat(skills): process-level SkillNetClient singleton accessor"
```

---

## Task 6: skill RPC dispatch — handles_skill_rpc / dispatch_skill_rpc(list_local) / run_skill_rpc(search/install)

**Files:**
- Create: `twinkle/agentserver/skills/rpc.py`
- Test: `tests/test_skill_rpc.py`

- [ ] **Step 1: 写失败测试**

`tests/test_skill_rpc.py`：
```python
import asyncio

import pytest

from twinkle.agentserver.skills import _set_skill_manager, SkillManager
from twinkle.agentserver.skills.remote import RemoteSkill, SkillNetError
from twinkle.agentserver.skills.rpc import (
    handles_skill_rpc, dispatch_skill_rpc, run_skill_rpc,
)
from twinkle.e2a.models import E2AEnvelope


def _env(method, rid="r1", params=None):
    return E2AEnvelope(request_id=rid, session_id="s1", method=method, params=params or {})


def _run(coro):
    return asyncio.run(coro)


async def _frames(envelope):
    return [f async for f in dispatch_skill_rpc(envelope)]


class FakeSend:
    def __init__(self):
        self.frames = []

    async def __call__(self, resp):
        self.frames.append(resp)


class FakeClient:
    def __init__(self, catalog=None, download_result=None, download_error=None):
        self._catalog = catalog or []
        self._download_result = download_result
        self._download_error = download_error

    async def fetch_remote_skills(self, force_refresh=False):
        return list(self._catalog)

    async def download_skill(self, url):
        if self._download_error:
            raise self._download_error
        return self._download_result


def test_handles_skill_rpc():
    assert handles_skill_rpc("skills.list_local")
    assert handles_skill_rpc("skills.search")
    assert handles_skill_rpc("skills.install")
    assert not handles_skill_rpc("chat.send")


def test_list_local_returns_installed(tmp_path):
    sk = tmp_path / "foo"
    sk.mkdir()
    (sk / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\nbody", encoding="utf-8")
    _set_skill_manager(SkillManager(str(tmp_path)))
    try:
        frames = _run(_frames(_env("skills.list_local")))
    finally:
        _set_skill_manager(None)
    assert len(frames) == 1
    f = frames[0]
    assert f.response_kind == "e2a.result"
    assert f.body["type"] == "skills.list_local"
    assert [s["name"] for s in f.body["skills"]] == ["foo"]


def test_search_filters_by_keyword():
    catalog = [
        RemoteSkill("foo", "a foo skill", "url_foo", "skills/foo/SKILL.md"),
        RemoteSkill("bar", "a bar skill", "url_bar", "skills/bar/SKILL.md"),
    ]
    send = FakeSend()
    _run(run_skill_rpc(_env("skills.search", params={"q": "foo"}), send, FakeClient(catalog=catalog)))
    assert len(send.frames) == 1
    f = send.frames[0]
    assert f.body["type"] == "skills.search"
    assert [s["name"] for s in f.body["skills"]] == ["foo"]


def test_search_force_refresh_passes_through():
    seen = {}

    class C(FakeClient):
        async def fetch_remote_skills(self, force_refresh=False):
            seen["force"] = force_refresh
            return []
    send = FakeSend()
    _run(run_skill_rpc(_env("skills.search", params={"q": "", "force_refresh": True}), send, C()))
    assert seen["force"] is True


def test_install_success_copies_and_reports(monkeypatch, tmp_path):
    skills_dir = tmp_path / "installed"
    skills_dir.mkdir()
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    src = tmp_path / "_src" / "foo"
    src.parent.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\n", encoding="utf-8")
    send = FakeSend()
    _run(run_skill_rpc(
        _env("skills.install", params={"url": "https://github.com/zjunlp/SkillNet/tree/main/skills/foo", "force": False}),
        send, FakeClient(download_result=("foo", src, tmp_path / "_src"))))
    assert len(send.frames) == 1
    f = send.frames[0]
    assert f.body["ok"] is True
    assert f.body["skill_name"] == "foo"
    assert (skills_dir / "foo" / "SKILL.md").is_file()
    # temp_root 被清理
    assert not (tmp_path / "_src" / "foo").exists()


def test_install_already_exists_reports_error(monkeypatch, tmp_path):
    skills_dir = tmp_path / "installed"
    (skills_dir / "foo").mkdir(parents=True)
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    src = tmp_path / "_src" / "foo"
    src.parent.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\n", encoding="utf-8")
    send = FakeSend()
    _run(run_skill_rpc(
        _env("skills.install", params={"url": "u", "force": False}),
        send, FakeClient(download_result=("foo", src, tmp_path / "_src"))))
    f = send.frames[0]
    assert f.body["ok"] is False
    assert "已安装" in f.body["error"]


def test_install_download_error_reports_failed(monkeypatch, tmp_path):
    skills_dir = tmp_path / "installed"
    skills_dir.mkdir()
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    send = FakeSend()
    _run(run_skill_rpc(
        _env("skills.install", params={"url": "u"}),
        send, FakeClient(download_error=SkillNetError("boom"))))
    f = send.frames[0]
    assert f.status == "failed"
    assert f.body["error"] == "boom"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_rpc.py -v`
Expected: FAIL — `ModuleNotFoundError: twinkle.agentserver.skills.rpc`

- [ ] **Step 3: 实现 rpc.py**

`twinkle/agentserver/skills/rpc.py`：
```python
"""skill RPC dispatch。

- skills.list_local:内联(纯本地 SkillManager 扫描),yield 单个 e2a.result。
- skills.search / skills.install:非内联 —— server.py 用 asyncio.create_task 起
  run_skill_rpc,完成后用连接 send() 发一个 e2a.result。不阻塞读循环。
失败帧 body 带 error,前端 request() 因 payload.error reject(同 dispatch_session_rpc)。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import AsyncIterator

from twinkle.e2a.models import E2AEnvelope, E2AResponse
from twinkle.agentserver.skills.remote import SkillNetError, safe_child_path, safe_skill_name

log = logging.getLogger("twinkle.agentserver.skills.rpc")

_SKILL_METHODS = {"skills.list_local", "skills.search", "skills.install"}


def handles_skill_rpc(method: str) -> bool:
    return method in _SKILL_METHODS


def _result(envelope: E2AEnvelope, body: dict, succeeded: bool = True) -> E2AResponse:
    return E2AResponse(
        request_id=envelope.request_id,
        sequence=0,
        is_final=True,
        status="succeeded" if succeeded else "failed",
        response_kind="e2a.result",
        body=body,
    )


async def dispatch_skill_rpc(envelope: E2AEnvelope) -> AsyncIterator[E2AResponse]:
    """内联 skill RPC(仅 skills.list_local)。search/install 由 run_skill_rpc 处理。"""
    method = envelope.method
    try:
        if method == "skills.list_local":
            from twinkle.agentserver.skills import get_skill_manager
            skills = get_skill_manager().list_skills()
            body = {"type": "skills.list_local", "skills": [
                {"name": s.name, "description": s.description} for s in skills]}
            yield _result(envelope, body)
        else:
            return  # search/install 非内联 —— server.py 走 run_skill_rpc
    except Exception as exc:
        log.exception("skill rpc %s failed: %s", method, exc)
        yield _result(envelope, {"type": method, "error": str(exc)}, succeeded=False)


async def run_skill_rpc(envelope: E2AEnvelope, send, client) -> None:
    """非内联 skill RPC(search/install):后台任务跑,完成发一个 e2a.result。"""
    method = envelope.method
    try:
        if method == "skills.search":
            q = (envelope.params.get("q") or "").lower()
            force = bool(envelope.params.get("force_refresh"))
            skills = await client.fetch_remote_skills(force_refresh=force)
            if q:
                skills = [s for s in skills
                          if q in s.name.lower() or q in s.description.lower()]
            body = {"type": "skills.search", "skills": [
                {"name": s.name, "description": s.description, "skill_url": s.skill_url}
                for s in skills]}
            await send(_result(envelope, body))
        elif method == "skills.install":
            url = envelope.params.get("url")
            force = bool(envelope.params.get("force"))
            skill_name, skill_dir, temp_root = await client.download_skill(url)
            try:
                from twinkle.config import SKILLS_DIR
                safe_skill_name(skill_name)
                dest = safe_child_path(Path(SKILLS_DIR), skill_name)
                if dest.exists() and not force:
                    await send(_result(envelope,
                        {"type": "skills.install", "ok": False,
                         "error": f"skill '{skill_name}' 已安装"}, succeeded=False))
                    return
                if dest.exists() and force:
                    shutil.rmtree(dest)
                shutil.copytree(skill_dir, dest)
                await send(_result(envelope,
                    {"type": "skills.install", "ok": True, "skill_name": skill_name}))
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)
        else:
            await send(_result(envelope,
                {"type": method, "error": f"unknown skill method: {method}"}, succeeded=False))
    except Exception as exc:
        log.exception("skill rpc %s failed: %s", method, exc)
        await send(_result(envelope, {"type": method, "error": str(exc)}, succeeded=False))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_skill_rpc.py -v`
Expected: PASS（7 个测试）。

- [ ] **Step 5: 提交（待用户确认）**

```bash
git add twinkle/agentserver/skills/rpc.py tests/test_skill_rpc.py
git commit -m "feat(skills): skill RPC dispatch (list_local inline + search/install background)"
```

---

## Task 7: server.py 路由 — skill 分支 + skill_tasks 清理

**Files:**
- Modify: `twinkle/agentserver/server.py`
- Test: `tests/test_skill_rpc.py`（追加一个路由判定测试；ws_handler 级非阻塞为结构性保证，附手动验收见 Task 11）

- [ ] **Step 1: 追加测试（handles_skill_rpc 已在 Task 6 覆盖；此处确认 import 通）**

在 `tests/test_skill_rpc.py` 末尾追加：
```python
def test_server_imports_skill_routing():
    # 静态确认 server.py 能 import 到 skill 路由符号(防回归)
    from twinkle.agentserver.server import ws_handler  # noqa: F401
    from twinkle.agentserver.skills import get_skillnet_client  # noqa: F401
    from twinkle.agentserver.skills.rpc import dispatch_skill_rpc, run_skill_rpc  # noqa: F401
```

- [ ] **Step 2: 跑测试确认失败/通过**

Run: `python -m pytest tests/test_skill_rpc.py::test_server_imports_skill_routing -v`
Expected: 现状可能 PASS（import 已成立）—— 主要是防后续改动破坏 import。

- [ ] **Step 3: 改 server.py 路由**

在 `twinkle/agentserver/server.py` 顶部 import 区，`from twinkle.agentserver.sessions import (...)` 之后加：
```python
from twinkle.agentserver.skills import get_skillnet_client
from twinkle.agentserver.skills.rpc import dispatch_skill_rpc, handles_skill_rpc, run_skill_rpc
```

在 `ws_handler` 的 `handler` 闭包里，`active: dict[str, asyncio.Task] = {}` 之后加：
```python
        skill_tasks: set[asyncio.Task] = set()
```

在 `if handles_session_rpc(envelope.method):` 分支之后、`sid = envelope.session_id ...` 之前，插入 skill 路由分支：
```python
                if handles_skill_rpc(envelope.method):
                    if envelope.method == "skills.list_local":
                        async for frame in dispatch_skill_rpc(envelope):
                            await send(frame)
                    else:  # skills.search / skills.install — 非阻塞后台任务
                        t = asyncio.create_task(
                            run_skill_rpc(envelope, send, get_skillnet_client()))
                        skill_tasks.add(t)
                        t.add_done_callback(skill_tasks.discard)
                    continue
```

把 `finally` 块改为同时清理 skill_tasks：
```python
        finally:
            for t in list(active.values()):
                t.cancel()
            for t in skill_tasks:
                t.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)
            await asyncio.gather(*skill_tasks, return_exceptions=True)
            active.clear()
            skill_tasks.clear()
            APPROVAL_REGISTRY.cancel_all()
```

- [ ] **Step 4: 跑全量测试确认无回归**

Run: `python -m pytest tests/ -q`
Expected: 全 PASS。

- [ ] **Step 5: 提交（待用户确认）**

```bash
git add twinkle/agentserver/server.py tests/test_skill_rpc.py
git commit -m "feat(skills): route skills.* RPCs in ws_handler (non-blocking for search/install)"
```

---

## Task 8: 前端 — webClient.request 加 timeoutMs

**Files:**
- Modify: `web/src/services/webClient.ts`
- Test: 无前端测试框架，手动验收

- [ ] **Step 1: 改 request 签名**

把 `web/src/services/webClient.ts` 的 `request` 方法改为接受可选 `timeoutMs`：
```typescript
  /** Fire an RPC (session.* / history.get / skills.*) and resolve with the `result` payload. */
  request(method: string, params: Record<string, any> = {}, timeoutMs = 15000): Promise<any> {
    return new Promise((resolve, reject) => {
      const id = this.send(method, params)
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`timeout waiting for result: ${method}`))
      }, timeoutMs)
      this.pending.set(id, (payload: any) => {
        clearTimeout(timer)
        if (payload?.error) reject(new Error(payload.error))
        else resolve(payload)
      })
    })
  }
```

- [ ] **Step 2: 验收**

`webClient.ts` 无类型报错（`npm run build` 或 `npm run dev` 启动无 TS error）。现有 `request('session.list', {})` 仍用默认 15s，行为不变。

- [ ] **Step 3: 提交（待用户确认）**

```bash
git add web/src/services/webClient.ts
git commit -m "feat(web): request() accepts per-call timeoutMs (for slow skill install)"
```

---

## Task 9: 前端 — useSessions 加 skills 状态/方法 + NavKey

**Files:**
- Modify: `web/src/composables/useSessions.ts`
- Test: 无前端测试框架，手动验收

- [ ] **Step 1: 加类型与状态**

在 `web/src/composables/useSessions.ts` 顶部 `interface TodoState {...}` 之后加：
```typescript
interface RemoteSkillItem { name: string; description: string; skill_url: string }
interface InstalledSkillItem { name: string; description: string }
```

把 `type NavKey = 'chat' | 'sessions'` 改为：
```typescript
type NavKey = 'chat' | 'sessions' | 'skills'
```

在 `const previewLoading = ref(false)` 之后加：
```typescript
const searchResults = ref<RemoteSkillItem[]>([])
const installedSkills = ref<InstalledSkillItem[]>([])
const skillsLoading = ref(false)
```

- [ ] **Step 2: 加方法**

在 `function sendQuery(q: string) {...}` 之后加：
```typescript
async function searchSkills(q: string, forceRefresh = false) {
  skillsLoading.value = true
  try {
    const payload = await client.request('skills.search', { q, force_refresh: forceRefresh }, 30000)
    searchResults.value = payload?.skills ?? []
  } finally {
    skillsLoading.value = false
  }
}

async function loadInstalled() {
  const payload = await client.request('skills.list_local', {})
  installedSkills.value = payload?.skills ?? []
}

async function installSkill(url: string) {
  await client.request('skills.install', { url, force: false }, 120000)
}
```

- [ ] **Step 3: 导出**

把 `export function useSessions() { return { ... } }` 的返回对象补充 skills 相关项：
```typescript
    searchResults, installedSkills, skillsLoading,
    searchSkills, loadInstalled, installSkill,
```
（加在 `loadSessionFiles, readSessionFile, restoreSession,` 之后即可）

- [ ] **Step 4: 验收**

`npm run dev` 启动无 TS error；`useSessions()` 解构出 `searchResults/installedSkills/skillsLoading/searchSkills/loadInstalled/installSkill`。

- [ ] **Step 5: 提交（待用户确认）**

```bash
git add web/src/composables/useSessions.ts
git commit -m "feat(web): skills nav + search/install/list_local state in useSessions"
```

---

## Task 10: 前端 — SkillsView.vue + App.vue 接线

**Files:**
- Create: `web/src/components/SkillsView.vue`
- Modify: `web/src/App.vue`
- Test: 无前端测试框架，手动验收（见 Task 11）

- [ ] **Step 1: 建 SkillsView.vue**

`web/src/components/SkillsView.vue`：
```vue
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useSessions } from '../composables/useSessions'

const { searchResults, installedSkills, skillsLoading, searchSkills, installSkill, loadInstalled } = useSessions()

const query = ref('')
const installingUrl = ref<string | null>(null)
const toast = ref<{ kind: 'ok' | 'err'; text: string } | null>(null)

const installedNames = computed(() => new Set(installedSkills.value.map((s) => s.name)))

async function onSearch() {
  await searchSkills(query.value)
}
async function onRefresh() {
  await searchSkills(query.value, true)
}
async function onInstall(skillUrl: string, name: string) {
  installingUrl.value = skillUrl
  toast.value = null
  try {
    await installSkill(skillUrl)
    toast.value = { kind: 'ok', text: `已安装：${name}` }
    await loadInstalled()
  } catch (e: any) {
    toast.value = { kind: 'err', text: String(e?.message ?? e) }
  } finally {
    installingUrl.value = null
  }
}
onMounted(() => { loadInstalled().catch(() => { /* 已安装列表加载失败不阻塞搜索 */ }) })
</script>

<template>
  <div class="skills-view">
    <div class="toolbar">
      <input v-model="query" placeholder="关键词（名称 / 描述）" @keyup.enter="onSearch" />
      <button @click="onSearch" :disabled="skillsLoading">搜索</button>
      <button @click="onRefresh" :disabled="skillsLoading">刷新目录</button>
    </div>

    <div v-if="toast" :class="['toast', toast.kind]">{{ toast.text }}</div>
    <div v-if="skillsLoading" class="muted">加载中…（首次拉取 SkillNet 目录较慢）</div>

    <ul class="results">
      <li v-for="s in searchResults" :key="s.skill_url">
        <div class="row">
          <div class="meta">
            <div class="name">{{ s.name }}</div>
            <div class="desc">{{ s.description }}</div>
          </div>
          <button v-if="installedNames.has(s.name)" disabled class="installed">已安装</button>
          <button v-else @click="onInstall(s.skill_url, s.name)" :disabled="installingUrl === s.skill_url">
            {{ installingUrl === s.skill_url ? '安装中…' : '安装' }}
          </button>
        </div>
      </li>
    </ul>

    <details class="installed-box">
      <summary>已安装（{{ installedSkills.length }}）</summary>
      <ul>
        <li v-for="s in installedSkills" :key="s.name"><b>{{ s.name }}</b> — {{ s.description }}</li>
      </ul>
    </details>
  </div>
</template>

<style scoped>
.skills-view { padding: 1rem; overflow: auto; height: 100%; }
.toolbar { display: flex; gap: .5rem; margin-bottom: .75rem; }
.toolbar input { flex: 1; padding: .45rem .6rem; border: 1px solid #e2e8f0; border-radius: 8px; }
.toolbar button { padding: .45rem .8rem; border: 0; border-radius: 8px; background: #4f46d5; color: #fff; cursor: pointer; }
.toolbar button:disabled { opacity: .5; cursor: not-allowed; }
.toast { padding: .5rem .75rem; border-radius: 8px; margin-bottom: .5rem; font-size: .85rem; }
.toast.ok { background: #ecfdf5; color: #047857; }
.toast.err { background: #fef2f2; color: #b91c1c; }
.muted { color: #94a3b8; font-size: .85rem; margin-bottom: .5rem; }
.results { list-style: none; padding: 0; margin: 0 0 1rem; }
.results li { border: 1px solid #e2e8f0; border-radius: 10px; margin-bottom: .5rem; background: #fff; }
.row { display: flex; align-items: center; padding: .6rem .75rem; gap: .75rem; }
.meta { flex: 1; min-width: 0; }
.name { font-weight: 600; color: #1e293b; }
.desc { font-size: .82rem; color: #64748b; margin-top: .15rem; }
.row button { border: 0; border-radius: 8px; padding: .4rem .7rem; background: #eef2ff; color: #4f46d5; cursor: pointer; flex: 0 0 auto; }
.row button:disabled { cursor: not-allowed; }
.row button.installed { background: #f1f5f9; color: #94a3b8; }
.installed-box summary { cursor: pointer; color: #475569; font-size: .9rem; }
.installed-box ul { list-style: none; padding: 0; }
.installed-box li { padding: .2rem 0; font-size: .85rem; color: #475569; }
</style>
```

- [ ] **Step 2: 接线 App.vue**

在 `web/src/App.vue` 的 `<script setup>` import 区加：
```typescript
import SkillsView from './components/SkillsView.vue'
```

把 `<nav class="sidebar">` 改为：
```html
      <nav class="sidebar">
        <button :class="{ active: activeNav === 'chat' }" @click="setNav('chat')">💬 聊天</button>
        <button :class="{ active: activeNav === 'sessions' }" @click="setNav('sessions')">🗂 会话</button>
        <button :class="{ active: activeNav === 'skills' }" @click="setNav('skills')">🧩 Skills</button>
      </nav>
```

把 `<main class="content">` 内部改为：
```html
      <main class="content">
        <ChatView v-if="activeNav === 'chat'" />
        <SessionsView v-else-if="activeNav === 'sessions'" />
        <SkillsView v-else-if="activeNav === 'skills'" />
        <SessionsView v-else />
      </main>
```

- [ ] **Step 3: 验收（构建）**

Run: `cd web && npm run build`
Expected: 构建成功，无 TS / 模板错误。

- [ ] **Step 4: 提交（待用户确认）**

```bash
git add web/src/components/SkillsView.vue web/src/App.vue
git commit -m "feat(web): SkillsView page + sidebar entry"
```

---

## Task 11: 文档反转 + 端到端手动验收

**Files:**
- Modify: `docs/e2a-introduction.md`
- Modify: `docs/superpowers/specs/2026-07-27-skill-design.md`
- 验收：启动服务做一次真实安装

- [ ] **Step 1: 更新 e2a-introduction.md**

把 `docs/e2a-introduction.md` 第 423-432 行的整个「Skill 系统（roadmap 全砍）」段替换为：
```markdown
#### Skill 系统（部分已实现 + 部分砍）

| method | 说明 | 状态 |
|---|---|---|
| `skills.list_local` | 本地已安装 skill 清单 | ✅ 已实现（Phase 7+） |
| `skills.search` | 搜索 SkillNet 可装 skill | ✅ 已实现（2026-07-30 重纳，见 `2026-07-30-skillnet-download-design.md`） |
| `skills.install` | 从 SkillNet（GitHub）下载安装 | ✅ 已实现 |
| `skills.uninstall` / `skills.import_local` | 卸载 / 导入本地 | ❌ 暂不做 |
| `skills.marketplace.*` | Skill 市场操作 | ❌ 不做 |
| `skills.clawhub.*` | ClawHub 下载 | ❌ 不做 |
| `skills.evolution.*` | Skill 自进化 | ❌ Phase 9 |
```

> 标题从「roadmap 全砍」改为「部分已实现 + 部分砍」，把 search/install/list_local 标 ✅ 并指向新 spec。

- [ ] **Step 2: 更新 2026-07-27-skill-design.md**

把 `docs/superpowers/specs/2026-07-27-skill-design.md` 第 368-371 行的「永远不做」段中 marketplace/SkillNet 那行替换，并在段首加反转说明。改为：
```markdown
**永远不做(roadmap §明确超出范围)**:
- ~~marketplace / SkillNet / symphony 树检索 / `agentic` 模式——企业级,依赖 openjiuwen 生态。~~
  > **2026-07-30 反转**:SkillNet 一键下载安装已重新纳入(参考 jiuwenswarm,自实现 GitHub API,不做语义搜索/marketplace/clawhub)。见 `2026-07-30-skillnet-download-design.md`。marketplace / symphony / `agentic` 模式仍不做。
- per-skill 绑定工具的 scoped 注册——jiuwenswarm 也用共享 toolkit,不需要 per-skill scoping。
- `trigger` 关键词自动匹配——模型驱动,见上。
```

- [ ] **Step 3: 端到端手动验收**

后端：
```bash
python scripts/start_services.py   # 或分两个终端跑 agentserver + gateway
```
前端：
```bash
cd web && npm run dev              # http://localhost:5173
```
操作：
1. 打开 web，点侧栏「🧩 Skills」。
2. 「已安装」展开应显示 seed 的 example skills（doc-audit 等）。
3. 关键词框输入一个词（如 `doc`），点「搜索」。首次较慢（拉 `zjunlp/SkillNet` 目录）。
4. 结果列表出现匹配 skill；已装的显示「已安装」，其余显示「安装」。
5. 点某未装 skill 的「安装」→ 按钮转「安装中…」→ 弹绿色「已安装：xxx」toast + 「已安装」列表刷新出现它。
6. 在终端确认 `<WORKSPACE>/skills/<name>/SKILL.md` 已生成（`~/.twinkle/skills/`）。
7. 回聊天页发一句触发该 skill 的话，确认 agent 下一步能用到（SkillHook 注入）。

> 若遇 `GitHub 匿名限流`，在 `.env` 设 `TWINKLE_GITHUB_TOKEN=<你的 token>` 重启后端。

- [ ] **Step 4: 提交（待用户确认）**

```bash
git add docs/e2a-introduction.md docs/superpowers/specs/2026-07-27-skill-design.md
git commit -m "docs(skills): reverse 'SkillNet 永远不做' — search/install now implemented"
```

---

## 自检（写完计划后 fresh-eyes 复核）

**1. Spec 覆盖**：
- §6.1 SkillNetClient（fetch_remote_skills + download_skill + 安全）→ Task 2/3/4 ✅
- §6.2 rpc.py（handles/dispatch/run）→ Task 6 ✅
- §6.3 server.py 路由 + skill_tasks 清理 → Task 7 ✅
- §6.4 config → Task 1 ✅
- §7 前端（webClient/useSessions/App/SkillsView）→ Task 8/9/10 ✅
- §9 RPC 契约（search/list_local/install）→ Task 6 + 前端调用 Task 9 ✅
- §10 错误处理（限流/已装/越界/无 SKILL.md/异常帧）→ Task 3/4/6 测试覆盖 ✅
- §12 测试（test_skill_remote / test_skill_rpc）→ Task 2/3/4/5/6 ✅
- §14 文档反转 → Task 11 ✅

**2. 占位扫描**：无 TBD/TODO；每个代码步骤含完整代码。✅

**3. 类型/签名一致性**：
- `SkillNetClient(owner, repo, branch, skills_path, github_token="", timeout=60.0, max_retries=3, ttl=CATALOG_TTL, _transport=None)` — Task 3 定义、Task 5 调用、Task 6 FakeClient 实现其方法签名（fetch_remote_skills(force_refresh=) / download_skill(url)）一致 ✅
- `download_skill(url) -> (name, skill_dir, temp_root)` — Task 4 定义、Task 6 install 用法一致 ✅
- `run_skill_rpc(envelope, send, client)` — Task 6 定义、Task 7 调用一致 ✅
- `dispatch_skill_rpc(envelope)` — Task 6 定义、Task 7 调用一致 ✅
- 前端 `request(method, params, timeoutMs=15000)`、`searchSkills(q, forceRefresh=false)`、`installSkill(url)` — Task 8/9/10 一致 ✅

无遗漏，无矛盾。
