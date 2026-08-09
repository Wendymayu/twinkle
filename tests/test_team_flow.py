"""End-to-end team flow tests — spec §6 validation (Task 8).

Verifies the full chain: leader create_task (with dependency) → member
claim/complete → dependency lift → second member claim → complete.
Plus steer injection into a member run.
"""
import asyncio

from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.team.context import CURRENT_TEAM


def test_full_flow_create_claim_complete_dependency(session_store, isolated_todo_store):
    """leader 拆 T1/T2(blocked_by T1)→ researcher claim+complete T1 →
    writer claim T2(依赖已解除)→ 全完成。"""
    from tests.test_team import _team_with_scripted_llm

    # researcher 跑一轮(脚本供 delegate 时用;此处直接走 task_store 模拟 member 工具调用)
    team = _team_with_scripted_llm(session_store, [
        [TextDelta("done"), Finish("stop", {"role": "assistant",
          "content": "T1 done", "tool_calls": None})],
    ])
    CURRENT_TEAM.set(team)

    # leader 建 T1, T2(blocked_by T1)
    t1 = asyncio.run(team.task_store.create_task("调研 X"))
    t2 = asyncio.run(team.task_store.create_task("写报告", blocked_by=[t1.id]))

    # researcher 认领+完成 T1(直接走 task_store 模拟 member 工具调用)
    asyncio.run(team.task_store.claim_task(t1.id, "researcher"))
    asyncio.run(team.task_store.complete_task(t1.id, "调研结果", "researcher"))

    # T1 completed → T2 依赖解除,writer 可 claim
    claimed_t2 = asyncio.run(team.task_store.claim_task(t2.id, "writer"))
    assert claimed_t2.owner == "writer"

    # writer 读 T1 result(get_task)
    t1_after = asyncio.run(team.task_store.get_task(t1.id))
    assert t1_after.result == "调研结果"

    # writer 完成 T2
    asyncio.run(team.task_store.complete_task(t2.id, "报告写好", "writer"))
    tasks = asyncio.run(team.task_store.list_tasks())
    assert all(t.status == "completed" for t in tasks)


def test_steer_injection_into_member_run(session_store, isolated_todo_store):
    """leader send_member → member run 下一轮 drain 注入(spec §6 可选 steer 演示)。"""
    from tests.test_team import _team_with_scripted_llm

    team = _team_with_scripted_llm(session_store, [
        [TextDelta("got it"), Finish("stop", {"role": "assistant",
          "content": "got it", "tool_calls": None})],
    ])
    asyncio.run(team._ensure_member("writer", "写手"))
    # leader 在 member 跑前/中发 steer(member inbox)
    asyncio.run(team.send_member("writer", "加风险提示节"))
    # member run → drain 注入(member 的 _inbox)
    asyncio.run(team.delegate("writer", "写手", "写报告"))
    # member 的 inbox 应已 drain(send_member 投的已取走)
    assert team._inboxes["writer"].drain() == []
