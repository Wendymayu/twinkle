"""SkillHub 远程客户端 — 从 skillhub.cn (api.skillhub.cn) 公开 API 关键词搜索
可装 skill,从 /api/v1/download?slug= 下载 zip 解压到临时目录。自实现 httpx,
对齐 SkillNetClient 的两方法表面但机制不同(搜索走 /api/skills,下载走 zip 而非
GitHub raw)。一个 "skillhub skill" = {name, description, slug, downloads, score,
version};按查询进程内缓存(TTL)。

数据源:搜索走 api.skillhub.cn/api/skills?keyword=(公开免鉴权,sortBy=score);
下载走 api.skillhub.cn/api/v1/download?slug= → 302 → 腾讯 COS zip(SKILL.md 在根目录)。
"""
from __future__ import annotations

import io
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from twinkle.agentserver.skills.remote import CATALOG_TTL, _locate_skill_dir, safe_child_path
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
