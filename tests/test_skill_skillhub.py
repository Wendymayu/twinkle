import asyncio
import io
import zipfile

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
