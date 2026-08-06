import asyncio

import pytest

from twinkle.agentserver.skills import _set_skill_manager, _set_skillhub_client, SkillManager
from twinkle.agentserver.skills.remote import SkillNetSkill, SkillNetError
from twinkle.agentserver.skills.skillhub import SkillHubSkill
from twinkle.agentserver.skills.rpc import (
    handles_skill_rpc, dispatch_skill_rpc, run_skill_rpc,
)
from twinkle.e2a.models import E2AEnvelope


def _env(method, request_id="r1", params=None):
    return E2AEnvelope(request_id=request_id, session_id="s1", method=method, params=params or {})


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

    async def download_skill(self, url):
        if self._download_error:
            raise self._download_error
        return self._download_result


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


def test_handles_skill_rpc():
    assert handles_skill_rpc("skills.list_local")
    assert handles_skill_rpc("skills.search")
    assert handles_skill_rpc("skills.install")
    assert handles_skill_rpc("skills.uninstall")
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


def test_search_passes_query_to_client():
    # 服务端搜索:q 原样透传给 search_remote_skills(不 lower、不客户端过滤)
    catalog = [SkillNetSkill("foo", "a foo skill", "url_foo", "")]
    send = FakeSend()
    client = FakeClient(search_result=catalog)
    _run(run_skill_rpc(_env("skills.search", params={"q": "诗词"}), send, client))
    assert client.last_q == "诗词"
    f = send.frames[0]
    assert f.body["type"] == "skills.search"
    assert [s["name"] for s in f.body["skills"]] == ["foo"]


def test_search_empty_q_returns_empty_without_api():
    client = FakeClient(search_result=[SkillNetSkill("foo", "d", "u", "")])
    send = FakeSend()
    _run(run_skill_rpc(_env("skills.search", params={"q": ""}), send, client))
    assert client.last_q is None  # 空查询不调 search_remote_skills
    assert send.frames[0].body["skills"] == []


def test_search_force_refresh_passes_through():
    client = FakeClient(search_result=[])
    send = FakeSend()
    _run(run_skill_rpc(_env("skills.search", params={"q": "x", "force_refresh": True}), send, client))
    assert client.last_force is True


def test_install_success_copies_and_reports(monkeypatch, tmp_path):
    skills_dir = tmp_path / "installed"
    skills_dir.mkdir()
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    src = tmp_path / "_src" / "foo"
    src.mkdir(parents=True)
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
    src.mkdir(parents=True)
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


def test_uninstall_removes_dir(monkeypatch, tmp_path):
    skills_dir = tmp_path / "installed"
    (skills_dir / "foo").mkdir(parents=True)
    (skills_dir / "foo" / "SKILL.md").write_text(
        "---\nname: foo\ndescription: d\n---\n", encoding="utf-8")
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    send = FakeSend()
    _run(run_skill_rpc(_env("skills.uninstall", params={"name": "foo"}),
                       send, FakeClient()))
    f = send.frames[0]
    assert f.body["ok"] is True
    assert f.body["skill_name"] == "foo"
    assert not (skills_dir / "foo").exists()


def test_uninstall_missing_reports_error(monkeypatch, tmp_path):
    skills_dir = tmp_path / "installed"
    skills_dir.mkdir()
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    send = FakeSend()
    _run(run_skill_rpc(_env("skills.uninstall", params={"name": "nope"}),
                       send, FakeClient()))
    f = send.frames[0]
    assert f.body["ok"] is False
    assert "未安装" in f.body["error"]


def test_uninstall_rejects_traversal_name(monkeypatch, tmp_path):
    skills_dir = tmp_path / "installed"
    skills_dir.mkdir()
    monkeypatch.setattr("twinkle.config.SKILLS_DIR", str(skills_dir))
    send = FakeSend()
    _run(run_skill_rpc(_env("skills.uninstall", params={"name": "../x"}),
                       send, FakeClient()))
    f = send.frames[0]
    assert f.status == "failed"  # safe_skill_name 拒绝含斜杠的名字
