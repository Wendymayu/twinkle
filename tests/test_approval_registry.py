# tests/test_approval_registry.py
import asyncio

from twinkle.agentserver.permissions.approval_registry import ApprovalRegistry
from twinkle.e2a.models import E2AEnvelope, E2AResponse


def test_register_then_resolve():
    async def go():
        reg = ApprovalRegistry()
        fut = reg.register("a1")
        assert not fut.done()
        assert reg.resolve("a1", "allow") is True
        assert fut.result() == "allow"
    asyncio.run(go())


def test_resolve_unknown_returns_false():
    async def go():
        reg = ApprovalRegistry()
        assert reg.resolve("nope", "allow") is False
    asyncio.run(go())


def test_resolve_twice_returns_false():
    async def go():
        reg = ApprovalRegistry()
        reg.register("a1")
        assert reg.resolve("a1", "allow") is True
        assert reg.resolve("a1", "deny") is False  # already resolved
    asyncio.run(go())


def test_handle_respond_sends_ack_and_resolves():
    async def go():
        reg = ApprovalRegistry()
        fut = reg.register("a1")
        env = E2AEnvelope(request_id="r2", method="approval.respond",
                          params={"approval_id": "a1", "decision": "allow_always"})
        sent = []
        await reg.handle_respond(env, lambda r: sent.append(r) or asyncio.sleep(0))
        assert fut.result() == "allow_always"
        assert isinstance(sent[0], E2AResponse)
        assert sent[0].response_kind == "e2a.result" and sent[0].body["accepted"] is True
    asyncio.run(go())


def test_handle_respond_unknown_sends_failed_ack():
    async def go():
        reg = ApprovalRegistry()
        env = E2AEnvelope(request_id="r2", method="approval.respond",
                          params={"approval_id": "nope", "decision": "deny"})
        sent = []
        await reg.handle_respond(env, lambda r: sent.append(r) or asyncio.sleep(0))
        assert sent[0].status == "failed" and sent[0].body["accepted"] is False
    asyncio.run(go())


def test_cancel_all():
    async def go():
        reg = ApprovalRegistry()
        fut = reg.register("a1")
        reg.cancel_all()
        assert fut.cancelled()
    asyncio.run(go())


def test_handle_respond_missing_decision_sends_failed_ack_and_leaves_future_pending():
    async def go():
        reg = ApprovalRegistry()
        fut = reg.register("a1")
        # malformed: approval_id present but no decision
        env = E2AEnvelope(request_id="r2", method="approval.respond",
                          params={"approval_id": "a1"})  # no "decision"
        sent = []
        await reg.handle_respond(env, lambda r: sent.append(r) or asyncio.sleep(0))
        # must NOT have resolved the future (no set_result(None))
        assert not fut.done()
        # ack must be failed, not a lying "succeeded"
        assert sent[0].status == "failed" and sent[0].body["accepted"] is False
    asyncio.run(go())


def test_handle_respond_missing_approval_id_sends_failed_ack():
    async def go():
        reg = ApprovalRegistry()
        # malformed: no approval_id at all
        env = E2AEnvelope(request_id="r2", method="approval.respond",
                          params={"decision": "allow"})  # no "approval_id"
        sent = []
        await reg.handle_respond(env, lambda r: sent.append(r) or asyncio.sleep(0))
        assert sent[0].status == "failed" and sent[0].body["accepted"] is False
    asyncio.run(go())
