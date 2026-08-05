# SkillHub 下载安装 Skill — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 SkillNet 来源旁新增 SkillHub（api.skillhub.cn）来源，前端来源切换，搜索 + zip 下载安装到 `<workspace>/skills/`，与 SkillNet 共存。

**Architecture:** 方案 A——新增 `skillhub.py` 的 `SkillHubClient`（搜索走 `/api/skills?keyword=`、下载走 `/api/v1/download?slug=`→302→zip 解压），镜像 `SkillNetClient` 两方法表面；`rpc.py` 加 `source` 参数分发；`server.py` 不改（skillhub client 在 `run_skill_rpc` 内部 `get_skillhub_client()` 自取）；两源同写 `skills/` 目录由 `SkillManager` 热重载拾取。顺带把 `RemoteSkill` 改名为 `SkillNetSkill` 与 `SkillHubSkill` 命名对称。

**Tech Stack:** Python 3 + httpx（异步）+ zipfile；Vue 3 + TypeScript 前端；pytest（`asyncio.run` + `httpx.MockTransport`，无 pytest-asyncio）。

**Spec:** `docs/superpowers/specs/2026-08-01-skillhub-download-design.md`（已批准）。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `twinkle/agentserver/skills/remote.py` | SkillNet client + `SkillNetSkill`（改名自 `RemoteSkill`）+ 共享安全函数 | 改（纯改名） |
| `twinkle/agentserver/skills/skillhub.py` | `SkillHubClient` + `SkillHubSkill` + `SkillHubError`（搜索 + zip 下载） | 新建 |
| `twinkle/agentserver/skills/__init__.py` | 进程级单例访问器 + re-export | 改（加 skillhub 单例 + 改名重导出） |
| `twinkle/agentserver/skills/rpc.py` | skill RPC dispatch（`source` 分发） | 改（加 skillhub 分支） |
| `twinkle/agentserver/server.py` | ws_handler 路由 | 不改 |
| `twinkle/config/schema.py` | `SkillsConfig` | 改（加 `skillhub_api_url`） |
| `twinkle/config/__init__.py` | 常量导出 | 改（加 `SKILLS_SKILLHUB_API_URL`） |
| `twinkle/resources/config.yaml` | 默认配置 | 改（加 `skillhub_api_url`） |
| `web/src/composables/useSessions.ts` | 前端 skills 状态/方法 | 改（类型改名 + `source`） |
| `web/src/components/SkillsView.vue` | Skills 页 UI | 改（来源 toggle + 富行） |
| `tests/test_skill_skillhub.py` | SkillHubClient 单元测试 | 新建 |
| `tests/test_skill_rpc.py` | skill RPC 测试 | 改（改名 + skillhub 用例） |
| `tests/test_agentserver_handler.py` / `tests/test_integration.py` | 既有 skillnet 测试 | 改（纯改名） |
| `tests/test_skill_config.py` | skill 配置测试 | 改（加 1 条） |
| `docs/superpowers/specs/2026-07-30-skillnet-download-design.md` | skillnet 设计文档 | 改（1 处改名同步） |

---

## Task 1: 改名 `RemoteSkill` → `SkillNetSkill`（机械，行为不变）

**Files:**
- Modify: `twinkle/agentserver/skills/remote.py`（class 定义 line 32、cache 类型 line 97、返回类型 line 128、构造 line 145）
- Modify: `twinkle/agentserver/skills/__init__.py`（import line 3、`__all__` line 52）
- Modify: `tests/test_skill_rpc.py`（import line 6、构造 line 77/88）
- Modify: `tests/test_agentserver_handler.py`（import line 136、构造 line 138-139）
- Modify: `tests/test_integration.py`（import line 317、构造 line 332）

纯 token 替换 `RemoteSkill` → `SkillNetSkill`（这些文件不含 `RemoteSkillItem`，安全）。skillnet 行为零变化，由既有测试保护。

- [ ] **Step 1: 跑 baseline 确认绿**

Run: `python -m pytest tests/test_skill_remote.py tests/test_skill_rpc.py tests/test_agentserver_handler.py tests/test_integration.py tests/test_skill_config.py -v`
Expected: 全 PASS（确认改名前基线绿；若 pre-existing 失败见记忆 `phase6-cron-tests-environmental-failures` 的环境性失败判定）。

- [ ] **Step 2: 改名 `remote.py`**

把以下 4 处的 `RemoteSkill` 改成 `SkillNetSkill`（其余不动）：

```python
# line 32
class SkillNetSkill:                      # was: class RemoteSkill:
    name: str
    description: str
    skill_url: str        # github.com/{owner}/{repo}/tree|blob/{ref}/{path}
    path: str = ""        # 仓库相对路径;搜索 API 不提供,下载改从 skill_url 解析,留空向后兼容
```
```python
# line 97
        self._query_cache: dict[tuple, tuple[list[SkillNetSkill], float]] = {}
```
```python
# line 128
                                   limit: int = 20, page: int = 1) -> list[SkillNetSkill]:
```
```python
# line 145
            SkillNetSkill(
```

- [ ] **Step 3: 改名 `__init__.py`**

```python
# line 3
from twinkle.agentserver.skills.remote import SkillNetClient, SkillNetError, SkillNetSkill
```
```python
# line 52 (在 __all__ 里)
    "SkillNetClient", "SkillNetError", "SkillNetSkill",
```

- [ ] **Step 4: 改名 3 个测试文件**

`tests/test_skill_rpc.py`:
```python
# line 6
from twinkle.agentserver.skills.remote import SkillNetSkill, SkillNetError
# line 77
    catalog = [SkillNetSkill("foo", "a foo skill", "url_foo", "")]
# line 88
    client = FakeClient(search_result=[SkillNetSkill("foo", "d", "u", "")])
```

`tests/test_agentserver_handler.py`:
```python
# line 136
    from twinkle.agentserver.skills.remote import SkillNetSkill
# line 138-139
        SkillNetSkill("foo", "a foo skill", "url_foo", "skills/foo/SKILL.md"),
        SkillNetSkill("bar", "a bar skill", "url_bar", "skills/bar/SKILL.md"),
```

`tests/test_integration.py`:
```python
# line 317
    from twinkle.agentserver.skills.remote import SkillNetSkill
# line 332
        SkillNetSkill("foo", "a foo skill", "url_foo", "skills/foo/SKILL.md"),
```

- [ ] **Step 5: 跑测试确认仍绿**

Run: `python -m pytest tests/test_skill_remote.py tests/test_skill_rpc.py tests/test_agentserver_handler.py tests/test_integration.py -v`
Expected: 全 PASS（改名不改行为）。

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/skills/remote.py twinkle/agentserver/skills/__init__.py tests/test_skill_rpc.py tests/test_agentserver_handler.py tests/test_integration.py
git commit -m "refactor(skills): rename RemoteSkill to SkillNetSkill for source symmetry"
```

---

## Task 2: 配置 — `skillhub_api_url`

**Files:**
- Modify: `twinkle/config/schema.py:75-82`（`SkillsConfig`）
- Modify: `twinkle/resources/config.yaml:29-36`（`skills:` 段）
- Modify: `twinkle/config/__init__.py:33-39`（常量导出）
- Test: `tests/test_skill_config.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_skill_config.py` 末尾追加：

```python
def test_skills_skillhub_defaults():
    # skillhub_api_url 是 config.yaml 字面量(非 env 驱动),可直接断言 loaded 常量
    assert cfg.settings.skills.skillhub_api_url == "https://api.skillhub.cn"
    assert cfg.SKILLS_SKILLHUB_API_URL == "https://api.skillhub.cn"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_config.py::test_skills_skillhub_defaults -v`
Expected: FAIL with `AttributeError`（`SkillsConfig` 无 `skillhub_api_url` 字段）。

- [ ] **Step 3: 加 schema 字段**

`twinkle/config/schema.py` 的 `SkillsConfig`（line 75-82）加一行（`extra="forbid"`，字段顺序无所谓）：

```python
class SkillsConfig(_StrictModel):
    dir: str = ""  # "" -> <workspace>/skills
    mode: SkillMode = "all"
    enabled: list[str] = []  # [] = all skills open
    skillnet_api_url: str = "http://api-skillnet.openkg.cn"  # SkillNet 公开搜索服务(关键词搜索)
    skillhub_api_url: str = "https://api.skillhub.cn"  # SkillHub 公开列表/下载 API(关键词搜索 + zip 下载)
    github_token: str = ""  # "" = anonymous (60/hour) — GitHub 下载用
    remote_timeout: float = 60.0
    remote_max_retries: int = 3
```

- [ ] **Step 4: 加 config.yaml 默认值**

`twinkle/resources/config.yaml` 的 `skills:` 段（line 33 后）加一行：

```yaml
skills:
  dir: ${TWINKLE_SKILLS_DIR:-}            # 空 → <workspace>/skills
  mode: all                               # all = 每步注入 skill 清单;auto_list = 模型按需调 list_skill 拉
  enabled: []                             # 列表;空 = 全开
  skillnet_api_url: http://api-skillnet.openkg.cn  # SkillNet 公开搜索服务(关键词搜索,503+ skill)
  skillhub_api_url: https://api.skillhub.cn  # SkillHub 公开列表/下载 API(关键词搜索 + zip 下载,免鉴权)
  github_token: ${TWINKLE_GITHUB_TOKEN:-}  # 可选;空=匿名(~60/小时),配 token 提 GitHub 下载额度
  remote_timeout: 60.0                    # 搜索/下载单次请求超时秒
  remote_max_retries: 3                   # 瞬时错误(5xx/网络)重试次数
```

- [ ] **Step 5: 加常量导出**

`twinkle/config/__init__.py`（line 36 后）加一行：

```python
SKILLS_SKILLNET_API_URL = settings.skills.skillnet_api_url
SKILLS_SKILLHUB_API_URL = settings.skills.skillhub_api_url
SKILLS_GITHUB_TOKEN = settings.skills.github_token
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_skill_config.py -v`
Expected: 全 PASS（含新 `test_skills_skillhub_defaults`）。

- [ ] **Step 7: Commit**

```bash
git add twinkle/config/schema.py twinkle/config/__init__.py twinkle/resources/config.yaml tests/test_skill_config.py
git commit -m "feat(skills): add skillhub_api_url config"
```

---

## Task 3: `SkillHubClient` + `SkillHubSkill` + 搜索

**Files:**
- Create: `twinkle/agentserver/skills/skillhub.py`
- Test: `tests/test_skill_skillhub.py`（新建）

- [ ] **Step 1: 写失败测试（搜索 + 缓存 + code!=0）**

创建 `tests/test_skill_skillhub.py`：

```python
import asyncio

import httpx
import pytest

from twinkle.agentserver.skills.skillhub import (
    SKILLHUB_API, SkillHubError, SkillHubClient, SkillHubSkill,
)


def _hub_client(handler, **kw):
    transport = httpx.MockTransport(handler)
    return SkillHubClient(_transport=transport, **kw)


def _list_resp(items):
    return httpx.Response(200, json={"code": 0, "data": {"skills": items}})


def test_search_returns_and_caches():
    calls = {"n": 0}
    items = [{"name": "web-tools-guide", "description_zh": "web tools",
              "slug": "web-tools-guide", "downloads": 200, "score": 100, "version": "1.0.2"}]

    def handler(request):
        calls["n"] += 1
        assert "/api/skills" in str(request.url)
        assert request.url.params.get("keyword") == "web"
        assert request.url.params.get("sortBy") == "score"
        return _list_resp(items)

    c = _hub_client(handler)
    skills = asyncio.run(c.search_remote_skills("web"))
    assert [s.name for s in skills] == ["web-tools-guide"]
    assert skills[0].description == "web tools"  # 取了 description_zh
    assert skills[0].slug == "web-tools-guide"
    assert skills[0].downloads == 200
    assert skills[0].score == 100
    n0 = calls["n"]
    # 缓存命中:二次不发 HTTP
    asyncio.run(c.search_remote_skills("web"))
    assert calls["n"] == n0
    # force_refresh 重拉
    asyncio.run(c.search_remote_skills("web", force_refresh=True))
    assert calls["n"] > n0


def test_search_nonzero_code_returns_empty():
    def handler(request):
        return httpx.Response(200, json={"code": 1, "data": {"skills": []}})
    c = _hub_client(handler)
    assert asyncio.run(c.search_remote_skills("x")) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_skillhub.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'twinkle.agentserver.skills.skillhub'`。

- [ ] **Step 3: 写 `skillhub.py`（模块常量 + 数据类 + 错误类 + client + 搜索）**

创建 `twinkle/agentserver/skills/skillhub.py`：

```python
"""SkillHub 远程客户端 — 从 skillhub.cn (api.skillhub.cn) 公开 API 关键词搜索
可装 skill,从 /api/v1/download?slug= 下载 zip 解压到临时目录。自实现 httpx,
对齐 SkillNetClient 的两方法表面但机制不同(搜索走 /api/skills,下载走 zip 而非
GitHub raw)。一个 "skillhub skill" = {name, description, slug, downloads, score,
version};按查询进程内缓存(TTL)。

数据源:搜索走 api.skillhub.cn/api/skills?keyword=(公开免鉴权,sortBy=score);
下载走 api.skillhub.cn/api/v1/download?slug= → 302 → 腾讯 COS zip(SKILL.md 在根目录)。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from twinkle.agentserver.skills.remote import CATALOG_TTL, safe_child_path
from twinkle.agentserver.skills.store import parse_skill_md


SKILLHUB_API = "https://api.skillhub.cn"


@dataclass
class SkillHubSkill:
    name: str
    description: str
    slug: str
    downloads: int = 0
    score: int = 0
    version: str = ""


class SkillHubError(Exception):
    """用户可见的 SkillHub 错误(网络/坏响应/无 SKILL.md 等)。"""


class SkillHubClient:
    def __init__(self, skillhub_api_url: str = SKILLHUB_API,
                 timeout: float = 60.0, max_retries: int = 3,
                 ttl: float = CATALOG_TTL,
                 _transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_url = skillhub_api_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._ttl = ttl
        self._transport = _transport
        self._query_cache: dict[tuple, tuple[list[SkillHubSkill], float]] = {}

    async def _get(self, client: httpx.AsyncClient, url: str,
                   params: dict | None = None,
                   follow_redirects: bool = False) -> httpx.Response:
        """GET + 重试。5xx/网络错误重试 max_retries 次;skillhub 免鉴权,无 GitHub 限流分支。"""
        for attempt in range(self._max_retries + 1):
            try:
                r = await client.get(url, params=params, timeout=self._timeout,
                                     follow_redirects=follow_redirects)
            except httpx.RequestError as e:
                if attempt == self._max_retries:
                    raise SkillHubError(f"网络错误: {e}")
                continue
            if r.status_code == 404:
                raise SkillHubError(f"SkillHub 404: {url}")
            if 500 <= r.status_code < 600 and attempt < self._max_retries:
                continue
            if 200 <= r.status_code < 300:
                return r
            raise SkillHubError(f"HTTP {r.status_code}")
        raise SkillHubError("请求失败: 超出重试")

    async def search_remote_skills(self, q: str, force_refresh: bool = False,
                                   limit: int = 20, page: int = 1) -> list[SkillHubSkill]:
        """关键词搜索 api.skillhub.cn/api/skills(keyword 服务端过滤,sortBy=score)。
        按查询缓存(TTL)。q 透传给 keyword,非客户端拉全量过滤(与 SkillNet 一致)。"""
        key = (q, limit, page)
        cached = self._query_cache.get(key)
        if cached is not None and not force_refresh and (time.monotonic() - cached[1]) < self._ttl:
            return cached[0]
        async with httpx.AsyncClient(transport=self._transport) as client:
            url = f"{self._api_url}/api/skills"
            params = {"page": str(page), "pageSize": str(limit),
                      "sortBy": "score", "keyword": q}
            resp = await self._get(client, url, params=params)
            data = resp.json()
        if data.get("code") != 0:
            self._query_cache[key] = ([], time.monotonic())
            return []
        items = (data.get("data") or {}).get("skills") or []
        results = [
            SkillHubSkill(
                name=it.get("name", ""),
                description=it.get("description_zh") or it.get("description") or "",
                slug=it.get("slug", ""),
                downloads=it.get("downloads") or 0,
                score=it.get("score") or 0,
                version=it.get("version") or "",
            )
            for it in items
        ]
        self._query_cache[key] = (results, time.monotonic())
        return results
```

注意：`download_skill` 在 Task 4 加；本步先不写它。`_get`/`search_remote_skills` 已可用。`safe_child_path` 本步先 import（Task 4 用），不报未使用（仅 lint warn，不影响）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_skill_skillhub.py::test_search_returns_and_caches tests/test_skill_skillhub.py::test_search_nonzero_code_returns_empty -v`
Expected: 2 PASS。

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/skills/skillhub.py tests/test_skill_skillhub.py
git commit -m "feat(skills): add SkillHubClient search (api.skillhub.cn/api/skills)"
```

---

## Task 4: `SkillHubClient.download_skill`（zip 下载解压）

**Files:**
- Modify: `twinkle/agentserver/skills/skillhub.py`（加 `download_skill` + 顶部补 `io`/`shutil`/`tempfile`/`zipfile`/`Path` import）
- Test: `tests/test_skill_skillhub.py`（追加下载测试）

- [ ] **Step 1: 写失败测试（下载 + 缺 SKILL.md + 缺 slug）**

在 `tests/test_skill_skillhub.py` 顶部 import 区加 `io`、`zipfile`，并在文件末尾追加：

```python
import io
import zipfile


def _make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def test_download_writes_files_and_parses_name(tmp_path):
    zip_bytes = _make_zip({
        "SKILL.md": "---\nname: foo\ndescription: d\n---\nbody",
        "scripts/h.sh": "echo 1",
    })

    def handler(request):
        u = str(request.url)
        if "/api/v1/download" in u and "slug=foo" in u:
            return httpx.Response(302, headers={"Location": "https://cos.example.com/foo.zip"})
        if "cos.example.com/foo.zip" in u:
            return httpx.Response(200, content=zip_bytes,
                                  headers={"Content-Type": "application/zip"})
        return httpx.Response(404)

    c = _hub_client(handler)
    name, skill_dir, temp_root = asyncio.run(c.download_skill("foo"))
    try:
        assert name == "foo"
        assert (skill_dir / "SKILL.md").read_text(encoding="utf-8").startswith("---")
        assert (skill_dir / "scripts" / "h.sh").read_text(encoding="utf-8") == "echo 1"
    finally:
        import shutil as _s
        _s.rmtree(temp_root, ignore_errors=True)


def test_download_missing_skill_md_raises():
    zip_bytes = _make_zip({"helper.py": "print(1)"})

    def handler(request):
        u = str(request.url)
        if "/api/v1/download" in u:
            return httpx.Response(302, headers={"Location": "https://cos.example.com/foo.zip"})
        if "cos.example.com" in u:
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    c = _hub_client(handler)
    with pytest.raises(SkillHubError, match="SKILL.md"):
        asyncio.run(c.download_skill("foo"))


def test_download_missing_slug_raises():
    c = _hub_client(lambda r: httpx.Response(200))
    with pytest.raises(SkillHubError, match="slug"):
        asyncio.run(c.download_skill(""))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_skillhub.py::test_download_writes_files_and_parses_name -v`
Expected: FAIL with `AttributeError: 'SkillHubClient' object has no attribute 'download_skill'`。

- [ ] **Step 3: 补顶部 import**

`twinkle/agentserver/skills/skillhub.py` 顶部 import 区（在 `import time` 那段）补：

```python
from __future__ import annotations

import io
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import httpx

from twinkle.agentserver.skills.remote import CATALOG_TTL, safe_child_path
from twinkle.agentserver.skills.store import parse_skill_md
```

并把 `from twinkle.agentserver.skills.remote import CATALOG_TTL, safe_child_path` 改为同时 import `_locate_skill_dir`：

```python
from twinkle.agentserver.skills.remote import CATALOG_TTL, _locate_skill_dir, safe_child_path
```

- [ ] **Step 4: 写 `download_skill` 方法**

在 `SkillHubClient` 类内（`search_remote_skills` 之后）加：

```python
    async def download_skill(self, slug: str) -> tuple[str, Path, Path]:
        """下载 /api/v1/download?slug=<slug> 的 zip 到临时目录;返回 (skill_name, skill_dir, temp_root)。
        调用方负责 copytree skill_dir→SKILLS_DIR 与清理 temp_root。与 SkillNetClient.download_skill
        同返回契约。"""
        if not slug:
            raise SkillHubError("缺少 slug")
        temp_root = Path(tempfile.mkdtemp(prefix="twinkle_skillhub_"))
        try:
            async with httpx.AsyncClient(transport=self._transport) as client:
                url = f"{self._api_url}/api/v1/download"
                resp = await self._get(client, url, params={"slug": slug},
                                       follow_redirects=True)
            body = resp.content
            if not body:
                raise SkillHubError("下载内容为空")
            try:
                zf = zipfile.ZipFile(io.BytesIO(body))
            except zipfile.BadZipFile as e:
                raise SkillHubError(f"下载内容非 zip: {e}")
            # 逐成员解压,防 zip-slip:每条目路径经 safe_child_path 校验(拒绝对路径/含 ../越界 temp_root)
            for member in zf.infolist():
                if member.is_dir():
                    continue
                target = safe_child_path(temp_root, member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))
            skill_dir = _locate_skill_dir(temp_root)
            if skill_dir is None:
                raise SkillHubError("下载内容未找到 SKILL.md")
            skill = parse_skill_md(skill_dir)
            if skill is None:
                raise SkillHubError("下载的 SKILL.md 缺少 name/description")
            return skill.name, skill_dir, temp_root
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_skill_skillhub.py -v`
Expected: 全 PASS（含 3 个新下载测试 + 2 个搜索测试）。

- [ ] **Step 6: Commit**

```bash
git add twinkle/agentserver/skills/skillhub.py tests/test_skill_skillhub.py
git commit -m "feat(skills): add SkillHubClient.download_skill (zip download + unzip)"
```

---

## Task 5: `get_skillhub_client` 单例

**Files:**
- Modify: `twinkle/agentserver/skills/__init__.py`（加单例 + `__all__` 导出 skillhub 符号）
- Test: `tests/test_skill_skillhub.py`（追加单例测试）

- [ ] **Step 1: 写失败测试**

在 `tests/test_skill_skillhub.py` 末尾追加：

```python
def test_get_skillhub_client_singleton_and_reset():
    from twinkle.agentserver.skills import get_skillhub_client, _set_skillhub_client
    fake = SkillHubClient(skillhub_api_url="http://x")
    _set_skillhub_client(fake)
    try:
        assert get_skillhub_client() is fake
    finally:
        _set_skillhub_client(None)
    c = get_skillhub_client()
    try:
        assert c._api_url == SKILLHUB_API  # 从 config 构造的真实单例
        assert get_skillhub_client() is c
    finally:
        _set_skillhub_client(None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_skillhub.py::test_get_skillhub_client_singleton_and_reset -v`
Expected: FAIL with `ImportError: cannot import name 'get_skillhub_client'`。

- [ ] **Step 3: 加单例到 `__init__.py`**

在 `twinkle/agentserver/skills/__init__.py`：顶部 import 行加 skillhub 符号：

```python
from twinkle.agentserver.skills.store import Skill, SkillManager, parse_skill_md
from twinkle.agentserver.skills.remote import SkillNetClient, SkillNetError, SkillNetSkill
from twinkle.agentserver.skills.skillhub import SkillHubClient, SkillHubSkill, SkillHubError
```

在 `_set_skillnet_client` 之后、`__all__` 之前加：

```python
_SKILLHUB_CLIENT: SkillHubClient | None = None


def get_skillhub_client() -> SkillHubClient:
    """进程级单例(惰性,从 config 构造)。测试用 _set_skillhub_client 替换。
    lazy import config 避免 import-time 副作用。"""
    global _SKILLHUB_CLIENT
    if _SKILLHUB_CLIENT is None:
        from twinkle.config import (
            SKILLS_SKILLHUB_API_URL, SKILLS_REMOTE_TIMEOUT, SKILLS_REMOTE_MAX_RETRIES,
        )
        _SKILLHUB_CLIENT = SkillHubClient(
            skillhub_api_url=SKILLS_SKILLHUB_API_URL,
            timeout=SKILLS_REMOTE_TIMEOUT, max_retries=SKILLS_REMOTE_MAX_RETRIES,
        )
    return _SKILLHUB_CLIENT


def _set_skillhub_client(c: SkillHubClient | None) -> None:
    """测试钩子:替换/重置单例。生产代码不调。"""
    global _SKILLHUB_CLIENT
    _SKILLHUB_CLIENT = c
```

`__all__` 加 skillhub 符号：

```python
__all__ = [
    "Skill", "SkillManager", "parse_skill_md",
    "get_skill_manager", "_set_skill_manager",
    "SkillNetClient", "SkillNetError", "SkillNetSkill",
    "get_skillnet_client", "_set_skillnet_client",
    "SkillHubClient", "SkillHubSkill", "SkillHubError",
    "get_skillhub_client", "_set_skillhub_client",
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_skill_skillhub.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/skills/__init__.py tests/test_skill_skillhub.py
git commit -m "feat(skills): add get_skillhub_client singleton"
```

---

## Task 6: `rpc.py` `source` 分发

**Files:**
- Modify: `twinkle/agentserver/skills/rpc.py`（`run_skill_rpc` 的 search/install 分支）
- Test: `tests/test_skill_rpc.py`（加 `FakeSkillHubClient` + skillhub search/install 用例）

- [ ] **Step 1: 写失败测试（skillhub search + install）**

在 `tests/test_skill_rpc.py`：顶部 import 区把 `SkillNetSkill`（Task 1 已改名）旁边加 skillhub import：

```python
from twinkle.agentserver.skills import _set_skill_manager, _set_skillhub_client, SkillManager
from twinkle.agentserver.skills.remote import SkillNetSkill, SkillNetError
from twinkle.agentserver.skills.skillhub import SkillHubSkill
from twinkle.agentserver.skills.rpc import (
    handles_skill_rpc, dispatch_skill_rpc, run_skill_rpc,
)
```

在 `FakeClient` 之后加 `FakeSkillHubClient`：

```python
class FakeSkillHubClient:
    def __init__(self, search_result=None, download_result=None, download_error=None):
        self._search_result = search_result or []
        self._download_result = download_result
        self._download_error = download_error
        self.last_q = None
        self.last_force = None

    async def search_remote_skills(self, q, force_refresh=False):
        self.last_q = q
        self.last_force = force_refresh
        return list(self._search_result)

    async def download_skill(self, slug):
        if self._download_error:
            raise self._download_error
        return self._download_result
```

在文件末尾追加 skillhub 用例：

```python
def test_search_skillhub_routes_to_hub_client():
    catalog = [SkillHubSkill("web-tools-guide", "d", "web-tools-guide", 200, 100)]
    hub = FakeSkillHubClient(search_result=catalog)
    _set_skillhub_client(hub)
    send = FakeSend()
    try:
        _run(run_skill_rpc(
            _env("skills.search", params={"q": "web", "source": "skillhub"}),
            send, FakeClient()))
    finally:
        _set_skillhub_client(None)
    assert hub.last_q == "web"
    f = send.frames[0]
    assert f.body["type"] == "skills.search"
    s = f.body["skills"][0]
    assert s["slug"] == "web-tools-guide"
    assert s["downloads"] == 200
    assert s["score"] == 100
    assert "skill_url" not in s  # skillhub 载荷不含 skill_url


def test_search_skillhub_empty_q_no_api():
    hub = FakeSkillHubClient(search_result=[SkillHubSkill("x", "d", "x", 1, 1)])
    _set_skillhub_client(hub)
    send = FakeSend()
    try:
        _run(run_skill_rpc(_env("skills.search", params={"q": "", "source": "skillhub"}),
                           send, FakeClient()))
    finally:
        _set_skillhub_client(None)
    assert hub.last_q is None
    assert send.frames[0].body["skills"] == []


def test_install_skillhub_success(monkeypatch, tmp_path):
    skills_dir = tmp_path / "installed"
    skills_dir.mkdir()
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    src = tmp_path / "_src" / "foo"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\n", encoding="utf-8")
    hub = FakeSkillHubClient(download_result=("foo", src, tmp_path / "_src"))
    _set_skillhub_client(hub)
    send = FakeSend()
    try:
        _run(run_skill_rpc(
            _env("skills.install", params={"source": "skillhub", "slug": "foo", "force": False}),
            send, FakeClient()))
    finally:
        _set_skillhub_client(None)
    f = send.frames[0]
    assert f.body["ok"] is True
    assert f.body["skill_name"] == "foo"
    assert (skills_dir / "foo" / "SKILL.md").is_file()


def test_install_skillhub_already_exists(monkeypatch, tmp_path):
    skills_dir = tmp_path / "installed"
    (skills_dir / "foo").mkdir(parents=True)
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    src = tmp_path / "_src" / "foo"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\n", encoding="utf-8")
    hub = FakeSkillHubClient(download_result=("foo", src, tmp_path / "_src"))
    _set_skillhub_client(hub)
    send = FakeSend()
    try:
        _run(run_skill_rpc(
            _env("skills.install", params={"source": "skillhub", "slug": "foo"}),
            send, FakeClient()))
    finally:
        _set_skillhub_client(None)
    f = send.frames[0]
    assert f.body["ok"] is False
    assert "已安装" in f.body["error"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_rpc.py::test_search_skillhub_routes_to_hub_client -v`
Expected: FAIL（`source:"skillhub"` 当前走 skillnet 分支，载荷含 `skill_url` 而非 `slug`）。

- [ ] **Step 3: 改 `rpc.py` 的 `run_skill_rpc`**

把 `twinkle/agentserver/skills/rpc.py` 的 `run_skill_rpc` 整体替换为（search/install 各加 `source` 分发，skillnet 子分支与 install 共用块保持原样）：

```python
async def run_skill_rpc(envelope: E2AEnvelope, send, client) -> None:
    """非内联 skill RPC(search/install):后台任务跑,完成发一个 e2a.result。"""
    method = envelope.method
    try:
        if method == "skills.search":
            q = (envelope.params.get("q") or "").strip()
            force = bool(envelope.params.get("force_refresh"))
            source = envelope.params.get("source", "skillnet")
            if source == "skillhub":
                from twinkle.agentserver.skills import get_skillhub_client
                hub = get_skillhub_client()
                skills = await hub.search_remote_skills(q, force_refresh=force) if q else []
                body = {"type": "skills.search", "skills": [
                    {"name": s.name, "description": s.description, "slug": s.slug,
                     "downloads": s.downloads, "score": s.score}
                    for s in skills]}
            else:
                skills = await client.search_remote_skills(q, force_refresh=force) if q else []
                body = {"type": "skills.search", "skills": [
                    {"name": s.name, "description": s.description, "skill_url": s.skill_url}
                    for s in skills]}
            await send(_result(envelope, body))
        elif method == "skills.install":
            source = envelope.params.get("source", "skillnet")
            force = bool(envelope.params.get("force"))
            if source == "skillhub":
                from twinkle.agentserver.skills import get_skillhub_client
                slug = envelope.params.get("slug")
                skill_name, skill_dir, temp_root = await get_skillhub_client().download_skill(slug)
            else:
                url = envelope.params.get("url")
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
Expected: 全 PASS（含 4 个新 skillhub 用例 + 既有 skillnet 用例，证明 hybrid 分发不破坏 skillnet）。

- [ ] **Step 5: Commit**

```bash
git add twinkle/agentserver/skills/rpc.py tests/test_skill_rpc.py
git commit -m "feat(skills): add source dispatch in skill RPC (skillhub/skillnet)"
```

---

## Task 7: 前端 — 来源 toggle + 富行 + 安装载荷分流

**Files:**
- Modify: `web/src/composables/useSessions.ts`（类型改名 + `source` 参数 + `clearSearch`）
- Modify: `web/src/components/SkillsView.vue`（来源 toggle + 富行 + 安装载荷）
- 无前端测试框架 → 手动验收。

- [ ] **Step 1: 改 `useSessions.ts` 类型与方法**

`web/src/composables/useSessions.ts`：

line 24 改名 + 加新接口：
```typescript
export interface SkillNetSkillItem { name: string; description: string; skill_url: string }
export interface SkillHubSkillItem { name: string; description: string; slug: string; downloads: number; score: number }
```

line 45 `searchResults` 类型改联合：
```typescript
const searchResults = ref<(SkillNetSkillItem | SkillHubSkillItem)[]>([])
```

`searchSkills`（line 153）加 `source` 参数：
```typescript
async function searchSkills(q: string, force = false, source: 'skillnet' | 'skillhub' = 'skillnet') {
  skillsLoading.value = true
  skillsError.value = null
  try {
    const payload = await client.request('skills.search', { q, force_refresh: force, source }, 60000)
    searchResults.value = payload?.skills ?? []
  } catch (e: any) {
    searchResults.value = []
    skillsError.value = e?.message || '搜索失败'
  } finally {
    skillsLoading.value = false
  }
}
```

`installSkill`（line 167）改签名为载荷对象：
```typescript
async function installSkill(args: {
  source: 'skillnet' | 'skillhub'; slug?: string; url?: string
}): Promise<{ ok: boolean; skillName?: string; error?: string }> {
  // 后台任务 + 延迟结果。source=skillhub 走 zip 下载,skillnet 走 GitHub raw。失败帧 → request reject。
  try {
    const payload = await client.request('skills.install', { ...args, force: false }, 180000)
    if (payload?.ok) {
      await loadInstalled() // 刷新已安装列表
      return { ok: true, skillName: payload.skill_name }
    }
    return { ok: false, error: payload?.error || '安装失败' }
  } catch (e: any) {
    return { ok: false, error: e?.message || '安装失败' }
  }
}
```

加 `clearSearch`（在 `loadInstalled` 之后）并加入 `useSessions` 返回对象：
```typescript
function clearSearch() { searchResults.value = [] }
```
在 `useSessions()` 返回里 `searchSkills` 旁加 `clearSearch,`。

- [ ] **Step 2: 重写 `SkillsView.vue`**

`web/src/components/SkillsView.vue` 整体替换为：

```vue
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useSessions } from '../composables/useSessions'

const { installedSkills, searchResults, skillsLoading, skillsError,
        loadInstalled, searchSkills, installSkill, clearSearch } = useSessions()

const query = ref('')
const source = ref<'skillnet' | 'skillhub'>('skillhub')
const installing = ref<string | null>(null)
const toast = ref<string | null>(null)

onMounted(() => { loadInstalled() })

function installKey(s: any): string {
  return source.value === 'skillhub' ? `skillhub:${s.slug}` : `skillnet:${s.skill_url}`
}

function switchSource(s: 'skillnet' | 'skillhub') {
  if (source.value === s) return
  source.value = s
  clearSearch()  // 切来源清掉旧结果(字段不同,避免渲染错位)
}

async function onSearch(force = false) {
  await searchSkills(query.value.trim(), force, source.value)
}

async function onInstall(s: any) {
  const key = installKey(s)
  installing.value = key
  toast.value = null
  try {
    const args = source.value === 'skillhub'
      ? { source: 'skillhub', slug: s.slug }
      : { source: 'skillnet', url: s.skill_url }
    const r = await installSkill(args)
    toast.value = r.ok ? `✓ 已安装：${r.skillName}` : `✗ ${r.error}`
  } finally {
    installing.value = null
  }
}
</script>

<template>
  <div class="skills-view">
    <section class="installed">
      <h3>已安装 ({{ installedSkills.length }})</h3>
      <ul>
        <li v-for="s in installedSkills" :key="s.name">
          <div class="meta"><strong>{{ s.name }}</strong> — <span>{{ s.description }}</span></div>
        </li>
        <li v-if="!installedSkills.length" class="empty">暂无已安装 skill</li>
      </ul>
    </section>

    <section class="search">
      <div class="source-toggle">
        <button :class="{ active: source === 'skillhub' }" @click="switchSource('skillhub')">SkillHub</button>
        <button :class="{ active: source === 'skillnet' }" @click="switchSource('skillnet')">SkillNet</button>
      </div>
      <h3>从 {{ source === 'skillhub' ? 'SkillHub' : 'SkillNet' }} 搜索</h3>
      <div class="search-bar">
        <input v-model="query" placeholder="关键词…" @keyup.enter="onSearch()" />
        <button :disabled="skillsLoading" @click="onSearch()">
          {{ skillsLoading ? '搜索中…' : '搜索' }}
        </button>
        <button :disabled="skillsLoading" class="ghost"
                @click="onSearch(true)" title="强制刷新(跳过缓存)">刷新</button>
      </div>
      <p v-if="skillsError" class="error">{{ skillsError }}</p>
      <ul class="results">
        <li v-for="s in searchResults" :key="installKey(s)">
          <div class="meta">
            <strong>{{ s.name }}</strong> — <span>{{ s.description }}</span>
            <span v-if="source === 'skillhub'" class="stats">↓ {{ (s as any).downloads }} · score {{ (s as any).score }}</span>
          </div>
          <button :disabled="installing === installKey(s)" @click="onInstall(s)">
            {{ installing === installKey(s) ? '安装中…' : '安装' }}
          </button>
        </li>
        <li v-if="!searchResults.length && !skillsLoading && !skillsError" class="empty">
          输入关键词搜索 {{ source === 'skillhub' ? 'SkillHub' : 'SkillNet' }} 中的 skill
        </li>
      </ul>
      <p v-if="toast" class="toast">{{ toast }}</p>
    </section>
  </div>
</template>

<style scoped>
.skills-view {
  flex: 1; display: flex; flex-direction: column; gap: 1rem;
  padding: 1rem; min-height: 0; overflow: auto;
}
h3 { margin: 0 0 .5rem; font-size: .9rem; color: #1e293b; }
ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .4rem; }
li {
  display: flex; align-items: center; justify-content: space-between; gap: .5rem;
  padding: .5rem .6rem; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
}
li .meta { min-width: 0; }
li strong { font-size: .85rem; color: #1e293b; }
li span { font-size: .78rem; color: #64748b; }
.empty { color: #94a3b8; font-size: .8rem; justify-content: center; }
.source-toggle { display: flex; gap: .3rem; margin-bottom: .3rem; }
.source-toggle button {
  border: 1px solid #cbd5e1; background: #fff; color: #475569;
  border-radius: 8px; padding: .35rem .7rem; cursor: pointer; font-size: .8rem;
}
.source-toggle button.active { background: #4f46d5; color: #fff; border-color: #4f46d5; }
.stats { margin-left: .4rem; font-size: .72rem; color: #2563eb; }
.search-bar { display: flex; gap: .4rem; }
input {
  flex: 1; padding: .45rem .6rem; border: 1px solid #cbd5e1;
  border-radius: 8px; font-size: .85rem;
}
button {
  border: 0; background: #4f46d5; color: #fff; border-radius: 8px;
  padding: .45rem .8rem; cursor: pointer; font-size: .8rem;
}
button:disabled { opacity: .5; cursor: not-allowed; }
button.ghost { background: #e2e8f0; color: #475569; }
.error { color: #dc2626; font-size: .78rem; margin: .3rem 0; }
.toast { margin: .4rem 0; font-size: .8rem; color: #475569; }
</style>
```

- [ ] **Step 3: 手动验收**

Run: `cd web && npm run dev`（http://localhost:5173，后端需 `python scripts/start_services.py` 起着）。
验收：
- Skills 页默认来源 = SkillHub；输入 `web` → 搜索 → 列表显示 name + 描述 + `↓ 下载量 · score X`。
- 点「安装」某行 → 转圈 → 成功 toast「✓ 已安装：<name>」→「已安装」列表刷新出现。
- 切到 SkillNet → 旧结果清空 → 输入关键词 → 列表显示 name + 描述（无 stats）→ 安装照常。
- `SkillManager.list_skills()`（或重启后 `list_skill` 工具）能看到刚装的 skill。

- [ ] **Step 4: Commit**

```bash
git add web/src/composables/useSessions.ts web/src/components/SkillsView.vue
git commit -m "feat(web): add SkillHub/SkillNet source toggle in SkillsView"
```

---

## Task 8: 文档同步 — skillnet 设计文档改名

**Files:**
- Modify: `docs/superpowers/specs/2026-07-30-skillnet-download-design.md:76`

- [ ] **Step 1: 改名同步**

`docs/superpowers/specs/2026-07-30-skillnet-download-design.md` line 76：

```markdown
- `fetch_remote_skills(force_refresh: bool = False) -> list[SkillNetSkill]`
```
（原 `list[RemoteSkill]`，与 Task 1 代码改名一致。历史 `plans/2026-07-30-skillnet-download.md` 不改——陈旧计划记录，代码即真相。）

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-07-30-skillnet-download-design.md
git commit -m "docs(skills): sync SkillNet design spec to SkillNetSkill rename"
```

---

## Self-Review

**1. Spec coverage（逐条对照 spec §14 文件清单 + §3-§12）：**
- `skillhub.py`（`SkillHubClient`+`SkillHubSkill`+`SkillHubError`+search+download）→ Task 3+4。✅
- `RemoteSkill`→`SkillNetSkill` 改名（`remote.py`+`__init__.py`+3 测试）→ Task 1。✅
- `get_skillhub_client` 单例（`__init__.py`）→ Task 5。✅
- `rpc.py` `source` 分发（search+install）→ Task 6。✅
- `server.py` 不改 → 计划无 server.py 任务，符合 spec §5.4。✅
- config（schema/yaml/__init__）→ Task 2。✅
- `test_skill_skillhub.py`（search+download+singleton）→ Task 3+4+5。✅
- `test_skill_rpc.py` skillhub 用例 → Task 6。✅
- `test_skill_config.py` 1 条 → Task 2。✅
- 前端 `useSessions.ts`+`SkillsView.vue` → Task 7。✅
- skillnet 设计文档 1 处改名 → Task 8。✅
- §10 安全（zip-slip 防护）→ Task 4 `download_skill` 逐成员 `safe_child_path`。✅
- §9 错误（缺 slug/无 SKILL.md/空 body/非 zip/code!=0）→ Task 3+4 测试覆盖。✅
- §12 YAGNI（不做 namespace/version/skillsets/卸载/base 类）→ 计划均未涉及。✅

**2. Placeholder scan：** 无 TBD/TODO；每步含可执行代码或命令。✅

**3. Type consistency：** `SkillHubSkill` 字段（name/description/slug/downloads/score/version）在 Task 3 定义，Task 4 `download_skill` 用 `parse_skill_md` 取 name（不读 SkillHubSkill 字段，无冲突），Task 6 rpc 读 `s.name/s.description/s.slug/s.downloads/s.score` 与定义一致；`SkillNetSkill`（Task 1 改名）字段 `skill_url` 在 Task 6 skillnet 分支读 `s.skill_url` 一致；`get_skillhub_client`/`_set_skillhub_client`（Task 5）在 Task 6 rpc 局部 import 一致。✅

无遗留问题，计划完整。
