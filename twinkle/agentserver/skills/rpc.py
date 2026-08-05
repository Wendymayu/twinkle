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

_SKILL_METHODS = {"skills.list_local", "skills.search", "skills.install", "skills.uninstall",
                  "skills.evolve", "skills.evolve_list", "skills.evolve_simplify",
                  "skills.evolve_pending", "skills.evolve_approve", "skills.evolve_reject"}


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
        elif method == "skills.evolve_list":
            yield await _dispatch_evolve_list(envelope)
        elif method == "skills.evolve_pending":
            yield await _dispatch_evolve_pending(envelope)
        else:
            return  # search/install/evolve* 非内联 —— server.py 走 run_skill_rpc
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
                # 服务端搜索:q 原样透传给 SkillNet API(非客户端拉全量过滤);空查询不发 API。
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
        elif method in ("skills.evolve", "skills.evolve_simplify",
                        "skills.evolve_approve", "skills.evolve_reject"):
            await _run_evolve_rpc(envelope, send)
        elif method == "skills.uninstall":
            name = (envelope.params.get("name") or "").strip()
            safe_skill_name(name)
            from twinkle.config import SKILLS_DIR
            dest = safe_child_path(Path(SKILLS_DIR), name)
            if not dest.exists():
                await send(_result(envelope,
                    {"type": "skills.uninstall", "ok": False,
                     "error": f"skill '{name}' 未安装"}, succeeded=False))
                return
            shutil.rmtree(dest)
            await send(_result(envelope,
                {"type": "skills.uninstall", "ok": True, "skill_name": name}))
        else:
            await send(_result(envelope,
                {"type": method, "error": f"unknown skill method: {method}"}, succeeded=False))
    except Exception as exc:
        log.exception("skill rpc %s failed: %s", method, exc)
        await send(_result(envelope, {"type": method, "error": str(exc)}, succeeded=False))


# --- evolution RPC 内联 dispatch ---


async def _dispatch_evolve_list(envelope: E2AEnvelope) -> E2AResponse:
    """skills.evolve_list <name> — 查经验记录与分数。"""
    from twinkle.agentserver.evolution import get_evolution_store
    name = (envelope.params.get("name") or "").strip()
    if not name:
        return _result(envelope, {"type": "skills.evolve_list", "error": "skill name required"}, succeeded=False)
    store = get_evolution_store()
    records = store.get_records_by_score(name, limit=50)
    body = {
        "type": "skills.evolve_list",
        "skill_name": name,
        "records": [
            {"id": r.id, "source": r.source, "score": r.score,
             "section": r.change.section, "summary": r.summary or r.change.summary,
             "used": r.usage_stats.times_used if r.usage_stats else 0,
             "positive": r.usage_stats.times_positive if r.usage_stats else 0}
            for r in records
        ],
    }
    return _result(envelope, body)


async def _dispatch_evolve_pending(envelope: E2AEnvelope) -> E2AResponse:
    """skills.evolve_pending [name] — 查待批列表。"""
    from twinkle.agentserver.evolution import get_orchestrator
    name = (envelope.params.get("name") or "").strip() or None
    orch = get_orchestrator()
    pending = orch.get_pending(name)
    body = {
        "type": "skills.evolve_pending",
        "pending": {
            sn: [{"id": r.id, "source": r.source, "section": r.change.section,
                  "summary": r.summary or r.change.summary}
                 for r in recs]
            for sn, recs in pending.items()
        },
    }
    return _result(envelope, body)


# --- evolution RPC 后台任务 ---


async def _run_evolve_rpc(envelope: E2AEnvelope, send) -> None:
    """非内联 evolution RPC（evolve / simplify / approve / reject）。"""
    from twinkle.agentserver.evolution import get_orchestrator, get_evolution_store
    method = envelope.method
    name = (envelope.params.get("name") or "").strip()
    if not name:
        await send(_result(envelope,
            {"type": method, "error": "skill name required"}, succeeded=False))
        return

    orch = get_orchestrator()
    store = get_evolution_store()

    try:
        if method == "skills.evolve":
            # 手动触发进化：从 store 读 SKILL.md 内容 + 已有消息
            skill_md = store._skill_md_path(name)
            if not skill_md.exists():
                await send(_result(envelope,
                    {"type": method, "error": f"skill '{name}' not found"}, succeeded=False))
                return
            skill_content = skill_md.read_text(encoding="utf-8")
            messages_raw = envelope.params.get("messages") or []
            result = await orch.evolve(name, messages_raw, skill_content=skill_content)
            body = {"type": method, "skill_name": name, "status": result.status,
                    "message": result.message,
                    "record_count": len(result.records)}
            await send(_result(envelope, body, succeeded=result.status not in ("persistence_failed",)))

        elif method == "skills.evolve_simplify":
            result = await orch.simplify(name)
            body = {"type": method, "skill_name": name, "status": result.status,
                    "message": result.message}
            await send(_result(envelope, body))

        elif method == "skills.evolve_approve":
            record_ids = envelope.params.get("record_ids") or None
            result = await orch.approve(name, record_ids)
            body = {"type": method, "skill_name": name, "status": result.status,
                    "message": result.message}
            await send(_result(envelope, body))

        elif method == "skills.evolve_reject":
            record_ids = envelope.params.get("record_ids") or None
            result = await orch.reject(name, record_ids)
            body = {"type": method, "skill_name": name, "status": result.status,
                    "message": result.message}
            await send(_result(envelope, body))

    except Exception as exc:
        log.exception("evolve rpc %s failed: %s", method, exc)
        await send(_result(envelope, {"type": method, "error": str(exc)}, succeeded=False))
