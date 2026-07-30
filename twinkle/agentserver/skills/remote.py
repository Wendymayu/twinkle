"""SkillNet 远程客户端 — 从公开 SkillNet 搜索 API (api-skillnet.openkg.cn) 关键词
搜索可装 skill,从 GitHub (skill_url 指向的仓库) 下载单个 skill 到临时目录。
自实现(不依赖 skillnet-ai),对齐 jiuwenswarm 的 SkillNetSearcher + SkillDownloader 但极简。
一个 "remote skill" = {name, description, skill_url, path};按查询进程内缓存(TTL)。

数据源:搜索走 openkg 公网服务(503+ skill,含诗词/古风等);下载走 GitHub Contents/raw
(skill_url 指向 github.com/{owner}/{repo}/tree|blob/{ref}/{path})。故搜索是服务端关键词匹配,
非客户端拉全量目录过滤。
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from twinkle.agentserver.skills.store import parse_skill_md


GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
SKILLNET_API = "http://api-skillnet.openkg.cn"
CATALOG_TTL = 3600.0


@dataclass
class RemoteSkill:
    name: str
    description: str
    skill_url: str        # github.com/{owner}/{repo}/tree|blob/{ref}/{path}
    path: str = ""        # 仓库相对路径;搜索 API 不提供,下载改从 skill_url 解析,留空向后兼容


class SkillNetError(Exception):
    """用户可见的 SkillNet 错误(限流/坏 URL/越界等)。"""


def safe_skill_name(name: str) -> str:
    """拒绝可逃逸 skills 目录、或含 OS 非法字符的名字。对齐 jiuwenswarm _safe_path_name。
    含 Windows 非法字符(" < > | : * ?)会致 makedirs WinError 123,必须拦(引号未剥时兜底)。"""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise SkillNetError(f"非法 skill 名: {name!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise SkillNetError(f"非法 skill 名: 含控制字符: {name!r}")
    if any(c in '"<>|:*?' for c in name):
        raise SkillNetError(f"非法 skill 名: 含 OS 非法字符: {name!r}")
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
    """解析 github.com/{owner}/{repo}/tree|blob/{ref}/{path} → (owner, repo, ref, path)。
    SkillNet 搜索 API 返回的 skill_url 用 /blob/{sha}/... 形式(GitHub 目录也允许 blob),
    故 tree 与 blob 都接受。"""
    p = urlparse(url)
    if p.netloc != "github.com" or not p.path.startswith("/"):
        raise SkillNetError(f"无法解析 GitHub skill URL: {url}")
    parts = [c for c in p.path.split("/") if c]
    # [owner, repo, "tree"|"blob", ref, path...]
    if len(parts) < 4 or parts[2] not in ("tree", "blob"):
        raise SkillNetError(f"无法解析 GitHub skill URL: {url}")
    owner, repo, ref = parts[0], parts[1], parts[3]
    path = "/".join(parts[4:]) if len(parts) > 4 else ""
    if not owner or not repo or not ref:
        raise SkillNetError(f"无法解析 GitHub skill URL: {url}")
    return owner, repo, ref, path


class SkillNetClient:
    def __init__(self, skillnet_api_url: str = SKILLNET_API,
                 github_token: str = "", timeout: float = 60.0,
                 max_retries: int = 3, ttl: float = CATALOG_TTL,
                 _transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_url = skillnet_api_url.rstrip("/")
        self._token = github_token
        self._timeout = timeout
        self._max_retries = max_retries
        self._ttl = ttl
        self._transport = _transport
        self._query_cache: dict[tuple, tuple[list[RemoteSkill], float]] = {}

    def _headers(self) -> dict[str, str]:
        # 搜索 API 无需 auth;GitHub 下载用 token 提额度。
        h = {"Accept": "application/vnd.github+json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _get(self, client: httpx.AsyncClient, url: str,
                   params: dict | None = None) -> httpx.Response:
        """GET + 重试。限流/404 即时致命;5xx/网络错误重试 max_retries 次。"""
        for attempt in range(self._max_retries + 1):
            try:
                r = await client.get(url, headers=self._headers(), params=params, timeout=self._timeout)
            except httpx.RequestError as e:
                if attempt == self._max_retries:
                    raise SkillNetError(f"网络错误: {e}")
                continue
            if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
                raise SkillNetError("GitHub 匿名限流(60/小时)，配置 TWINKLE_GITHUB_TOKEN 或稍后重试")
            if r.status_code == 404:
                raise SkillNetError(f"GitHub 404: {url}")
            if 500 <= r.status_code < 600 and attempt < self._max_retries:
                continue
            if 200 <= r.status_code < 300:
                return r
            raise SkillNetError(f"HTTP {r.status_code}")
        raise SkillNetError("请求失败: 超出重试")

    async def search_remote_skills(self, q: str, force_refresh: bool = False,
                                   limit: int = 20, page: int = 1) -> list[RemoteSkill]:
        """关键词搜索 SkillNet 公开 API({api}/v1/search,mode=keyword)。按查询缓存(TTL)。
        服务端搜索——q 传给 API,非客户端拉全量过滤。"""
        key = (q, limit, page)
        cached = self._query_cache.get(key)
        if cached is not None and not force_refresh and (time.monotonic() - cached[1]) < self._ttl:
            return cached[0]
        async with httpx.AsyncClient(transport=self._transport) as client:
            url = f"{self._api_url}/v1/search"
            params = {"q": q, "mode": "keyword", "limit": limit, "page": page}
            resp = await self._get(client, url, params=params)
            data = resp.json()
        if not data.get("success"):
            self._query_cache[key] = ([], time.monotonic())
            return []
        items = data.get("data") or []
        results = [
            RemoteSkill(
                name=it.get("skill_name", ""),
                description=it.get("skill_description") or "",
                skill_url=it.get("skill_url") or "",
                path="",
            )
            for it in items
        ]
        self._query_cache[key] = (results, time.monotonic())
        return results

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
        # 先递归收集所有文件 (path, download_url)(Contents API 逐目录,受 rate-limit),
        # 再并发拉取文件内容(raw.githubusercontent.com 无 rate-limit,慢网络下 N×延迟→~1×)。
        files = await self._collect_files(client, owner, repo, ref, path)
        await self._download_files(client, files, dest)

    async def _collect_files(self, client: httpx.AsyncClient, owner: str, repo: str,
                             ref: str, path: str) -> list[tuple[str, str]]:
        if not path:
            return []
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
        data = (await self._get(client, url)).json()
        if isinstance(data, dict):
            data = [data]
        out: list[tuple[str, str]] = []
        for entry in data:
            etype = entry.get("type")
            ep = entry.get("path", "")
            if not ep:
                continue
            if etype == "file":
                dl = entry.get("download_url") or f"{GITHUB_RAW}/{owner}/{repo}/{ref}/{ep}"
                out.append((ep, dl))
            elif etype == "dir":
                out.extend(await self._collect_files(client, owner, repo, ref, ep))
        return out

    async def _download_files(self, client: httpx.AsyncClient,
                              files: list[tuple[str, str]], dest: Path) -> None:
        async def _one(ep: str, dl: str) -> None:
            content = (await self._get(client, dl)).content
            target = safe_child_path(dest, ep)  # 防穿越:恶意 ep 也写不到 temp 外
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if files:
            await asyncio.gather(*(_one(ep, dl) for ep, dl in files))


def _locate_skill_dir(root: Path) -> Path | None:
    """递归找第一个 SKILL.md,返回其所在目录。"""
    for sk in root.rglob("SKILL.md"):
        return sk.parent
    return None
