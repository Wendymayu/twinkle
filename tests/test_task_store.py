import asyncio
import pytest

from twinkle.agentserver.team.task_store import TeamTaskStore
from twinkle.agentserver.todo import TodoError, _set_todo_store


@pytest.fixture
def store(tmp_path):
    from twinkle.agentserver.todo.store import TodoStore
    s = TodoStore(str(tmp_path / "todos"))
    _set_todo_store(s)
    yield s
    _set_todo_store(None)


def _new(store):
    return TeamTaskStore("team:s1")


def test_create_task_pending(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("调研 X"))
    assert t.status == "pending"
    assert t.subject == "调研 X"
    assert t.owner == ""


def test_claim_sets_owner_and_in_progress(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    claimed = asyncio.run(ts.claim_task(t.id, "researcher"))
    assert claimed.owner == "researcher"
    assert claimed.status == "in_progress"


def test_claim_rejects_already_claimed(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    with pytest.raises(TodoError):
        asyncio.run(ts.claim_task(t.id, "writer"))


def test_claim_rejects_blocked_by_uncompleted(store):
    ts = _new(store)
    t1 = asyncio.run(ts.create_task("T1"))
    t2 = asyncio.run(ts.create_task("T2", blocked_by=[t1.id]))
    with pytest.raises(TodoError):  # T1 未完成,T2 不能 claim
        asyncio.run(ts.claim_task(t2.id, "writer"))


def test_claim_allows_after_dependency_completed(store):
    ts = _new(store)
    t1 = asyncio.run(ts.create_task("T1"))
    t2 = asyncio.run(ts.create_task("T2", blocked_by=[t1.id]))
    asyncio.run(ts.claim_task(t1.id, "researcher"))
    asyncio.run(ts.complete_task(t1.id, "result", "researcher"))
    # T1 completed → T2 解除 blocked,可 claim
    claimed = asyncio.run(ts.claim_task(t2.id, "writer"))
    assert claimed.owner == "writer"


def test_complete_rejects_wrong_owner(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    with pytest.raises(TodoError):
        asyncio.run(ts.complete_task(t.id, "r", "writer"))


def test_complete_sets_result_and_completed(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    done = asyncio.run(ts.complete_task(t.id, "调研结果", "researcher"))
    assert done.status == "completed"
    assert done.result == "调研结果"


def test_no_false_cycle_on_linear_chain(store):
    """线性依赖链 T3→T2→T1 不应误报环。"""
    ts = _new(store)
    t1 = asyncio.run(ts.create_task("T1"))
    t2 = asyncio.run(ts.create_task("T2", blocked_by=[t1.id]))
    t3 = asyncio.run(ts.create_task("T3", blocked_by=[t2.id]))
    assert t3.status == "pending"


def test_has_cycle_detects_mutual_dependency(store):
    """直接构造 A↔B 互依(A.blocked_by=[B], B.blocked_by=[A]),create C blocked_by=[A]
    时从 A 出发 DFS 命中 visited 的 B→A,应拒绝。"""
    import time as _time
    from twinkle.agentserver.todo import TodoTask
    ts = _new(store)
    now = _time.time()
    a = TodoTask(id="A", subject="A", status="pending", blocked_by=["B"],
                 created_at=now, updated_at=now)
    b = TodoTask(id="B", subject="B", status="pending", blocked_by=["A"],
                 created_at=now, updated_at=now)
    # 绕过 create_task 的顺序校验,直接 seed 一对互依 task
    async def _seed():
        async with ts._store._lock("team:s1"):
            ts._store._save("team:s1", [a, b])
    asyncio.run(_seed())
    # create C blocked_by=[A]:从 A 走 → B → A(visited 命中)→ 环
    with pytest.raises(TodoError):
        asyncio.run(ts.create_task("C", blocked_by=["A"]))


def test_request_help_sets_metadata(store):
    """member 求助:标 metadata.help_reason,不改 status(spec §1.4)。"""
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    helped = asyncio.run(ts.request_help(t.id, "need X data", "researcher"))
    assert helped.metadata.get("help_reason") == "need X data"
    assert helped.status == "in_progress"  # 不改 status,留 release 回 pending
    assert helped.owner == "researcher"     # owner 保留(释放时才清)


def test_request_help_rejects_wrong_owner(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    with pytest.raises(TodoError):
        asyncio.run(ts.request_help(t.id, "reason", "writer"))


def test_release_claims_preserves_help_reason(store):
    """release 把 in_progress 回 pending + owner 清空,但 metadata.help_reason 保留(spec §7)。"""
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    asyncio.run(ts.request_help(t.id, "stuck on X", "researcher"))
    count = asyncio.run(ts.release_claims("researcher"))
    assert count == 1
    after = asyncio.run(ts.get_task(t.id))
    assert after.status == "pending"
    assert after.owner == ""
    assert after.metadata.get("help_reason") == "stuck on X"  # 保留


def test_cancel_rejects_completed(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    asyncio.run(ts.complete_task(t.id, "done", "researcher"))
    with pytest.raises(TodoError):  # completed 不能 cancel
        asyncio.run(ts.cancel_task(t.id))


def test_complete_rejects_already_completed(store):
    ts = _new(store)
    t = asyncio.run(ts.create_task("T1"))
    asyncio.run(ts.claim_task(t.id, "researcher"))
    asyncio.run(ts.complete_task(t.id, "done", "researcher"))
    with pytest.raises(TodoError):  # 不能重复 complete
        asyncio.run(ts.complete_task(t.id, "again", "researcher"))
