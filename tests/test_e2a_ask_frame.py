from twinkle.e2a.models import E2AResponse
from twinkle.schema.message import EventType


def test_e2a_ask_response_kind_usable():
    r = E2AResponse(request_id="r1", sequence=0, is_final=False,
                   status="in_progress", response_kind="e2a.ask",
                   body={"approval_id": "a1", "tool": "echo"})
    assert r.response_kind == "e2a.ask" and r.is_final is False


def test_approval_ask_event_type():
    assert EventType.APPROVAL_ASK == "approval.ask"
