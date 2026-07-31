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


def test_search_passes_query_to_client():
    # 服务端搜索:q 原样透传给 search_remote_skills(不 lower、不客户端过滤)
    catalog = [RemoteSkill("foo", "a foo skill", "url_foo", "")]
    send = FakeSend()
    client = FakeClient(search_result=catalog)
    _run(run_skill_rpc(_env("skills.search", params={"q": "诗词"}), send, client))
    assert client.last_q == "诗词"
    f = send.frames[0]
    assert f.body["type"] == "skills.search"
    assert [s["name"] for s in f.body["skills"]] == ["foo"]


def test_search_empty_q_returns_empty_without_api():
    client = FakeClient(search_result=[RemoteSkill("foo", "d", "u", "")])
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
