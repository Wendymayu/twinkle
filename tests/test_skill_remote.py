import asyncio

import httpx
import pytest

from twinkle.agentserver.skills.remote import (
    SKILLNET_API, SkillNetError, SkillNetClient, safe_skill_name, safe_child_path, parse_github_url,
)


def test_safe_skill_name_accepts_plain():
    assert safe_skill_name("foo") == "foo"


def test_safe_skill_name_rejects_traversal():
    for bad in ["", "..", ".", "a/b", "a\\b", "/abs", "foo\x00bar"]:
        with pytest.raises(SkillNetError):
            safe_skill_name(bad)


def test_safe_skill_name_rejects_windows_invalid():
    # 含 " 等的 skill 名(引号未剥时)会让 Windows makedirs 报 WinError 123,必须拦
    for bad in ['foo"bar', "a<b", "a>b", "a|b", "a:b", "a*b", "a?b"]:
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


def test_parse_github_url_accepts_blob_and_sha():
    # SkillNet 搜索 API 返回的 skill_url 用 /blob/{sha}/... 形式
    url = ("https://github.com/openclaw/skills/blob/"
           "7f3c0cab77599719670efa8d97365df45d89a23f/skills/enchograph/daily-gushiwen")
    assert parse_github_url(url) == (
        "openclaw", "skills",
        "7f3c0cab77599719670efa8d97365df45d89a23f", "skills/enchograph/daily-gushiwen")


def test_parse_github_url_bad():
    for bad in ["https://example.com/x", "https://github.com/zjunlp/SkillNet",
                "https://github.com/zjunlp/SkillNet/commit/sha/skills/foo"]:
        with pytest.raises(SkillNetError):
            parse_github_url(bad)


def _client(handler, **kw):
    transport = httpx.MockTransport(handler)
    return SkillNetClient(_transport=transport, **kw)


def _search_resp(items):
    return httpx.Response(200, json={"success": True, "data": items})


def test_search_remote_skills_returns_and_caches():
    calls = {"n": 0}
    items = [{"skill_name": "foo", "skill_description": "foo skill",
              "skill_url": "url_foo", "category": "X"}]

    def handler(request):
        calls["n"] += 1
        assert "/v1/search" in str(request.url)
        assert request.url.params.get("mode") == "keyword"
        assert request.url.params.get("q") == "诗词"
        return _search_resp(items)

    c = _client(handler)
    skills = asyncio.run(c.search_remote_skills("诗词"))
    assert [s.name for s in skills] == ["foo"]
    assert skills[0].description == "foo skill"
    assert skills[0].skill_url == "url_foo"
    n0 = calls["n"]
    # 缓存命中:二次不发 HTTP
    asyncio.run(c.search_remote_skills("诗词"))
    assert calls["n"] == n0
    # force_refresh 重拉
    asyncio.run(c.search_remote_skills("诗词", force_refresh=True))
    assert calls["n"] > n0


def test_search_remote_skills_not_success_returns_empty():
    def handler(request):
        return httpx.Response(200, json={"success": False, "data": []})
    c = _client(handler)
    assert asyncio.run(c.search_remote_skills("x")) == []


def test_search_remote_skills_rate_limit_is_friendly():
    def handler(request):
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})
    c = _client(handler)
    with pytest.raises(SkillNetError, match="限流"):
        asyncio.run(c.search_remote_skills("x"))


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


def test_get_skillnet_client_singleton_and_reset():
    from twinkle.agentserver.skills import get_skillnet_client, _set_skillnet_client
    fake = SkillNetClient(skillnet_api_url="http://x")
    _set_skillnet_client(fake)
    try:
        assert get_skillnet_client() is fake
    finally:
        _set_skillnet_client(None)
    # 重置后构造真实单例(读 config)
    c = get_skillnet_client()
    try:
        assert c._api_url == SKILLNET_API
        assert get_skillnet_client() is c
    finally:
        _set_skillnet_client(None)
