"""skill RPC dispatch.

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
from twinkle.agentserver.skills.remote import safe_child_path, safe_skill_name

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
            q = (envelope.params.get("q") or "").strip()
            force = bool(envelope.params.get("force_refresh"))
            # 服务端搜索:q 原样透传给 SkillNet API(非客户端拉全量过滤);空查询不发 API。
            skills = await client.search_remote_skills(q, force_refresh=force) if q else []
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
