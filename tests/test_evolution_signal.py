"""测试 ConversationSignalDetector — 规则信号检测 + skill 归因。"""
from twinkle.agentserver.evolution.signal_detector import ConversationSignalDetector


def _make_tool_msg(role, content="", name="", tool_call_id=None):
    msg = {"role": role, "content": content}
    if name:
        msg["name"] = name
    if tool_call_id:
        msg["tool_call_id"] = tool_call_id
    return msg


def _make_assistant_with_tool_calls(tool_calls_data):
    """构造 assistant 消息含 tool_calls。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{i}",
                "function": {"name": tc["name"], "arguments": tc.get("arguments", "{}")},
            }
            for i, tc in enumerate(tool_calls_data)
        ],
    }


def test_detect_failure_signal():
    detector = ConversationSignalDetector()
    messages = [
        _make_assistant_with_tool_calls([
            {"name": "read_file", "arguments": '{"path": "/workspace/skills/weather/SKILL.md"}'},
        ]),
        _make_tool_msg("tool", "Error: connection refused", name="read_file"),
    ]
    signals = detector.detect(messages, ["weather"])
    assert len(signals) == 1
    assert signals[0].type == "execution_failure"
    assert signals[0].skill_name == "weather"


def test_detect_script_success():
    detector = ConversationSignalDetector()
    messages = [
        _make_assistant_with_tool_calls([
            {"name": "read_file", "arguments": '{"path": "/workspace/skills/chart/SKILL.md"}'},
        ]),
        _make_tool_msg("tool", "import matplotlib\nplt.savefig('chart.png')\nChart generated successfully.", name="command_exec"),
    ]
    signals = detector.detect(messages, ["chart"])
    assert len(signals) == 1
    assert signals[0].type == "script_artifact"
    assert signals[0].skill_name == "chart"


def test_no_signal_on_normal_result():
    detector = ConversationSignalDetector()
    messages = [
        _make_tool_msg("tool", "File read successfully.\n\n## Weather API\n...", name="read_file"),
    ]
    signals = detector.detect(messages, ["weather"])
    assert len(signals) == 0


def test_skill_attribution_from_path():
    """验从工具参数路径中提取 skill 名。"""
    detector = ConversationSignalDetector()
    messages = [
        _make_assistant_with_tool_calls([
            {"name": "read_file", "arguments": '{"path": "/skills/calculator/SKILL.md"}'},
        ]),
        _make_tool_msg("tool", "Traceback (most recent call last):\nError: division by zero", name="read_file"),
    ]
    signals = detector.detect(messages, ["calculator", "weather"])
    assert len(signals) == 1
    assert signals[0].skill_name == "calculator"


def test_resolve_active_skill():
    detector = ConversationSignalDetector()
    history = [(0, "weather"), (2, "calculator")]
    assert detector._resolve_active_skill(1, history) == "weather"
    assert detector._resolve_active_skill(3, history) == "calculator"
    assert detector._resolve_active_skill(0, history) == "weather"


def test_is_failure():
    detector = ConversationSignalDetector()
    assert detector._is_failure("Error: something went wrong")
    assert detector._is_failure("connection refused")
    assert detector._is_failure("Traceback (most recent call last)")
    assert not detector._is_failure("File read successfully")
    assert not detector._is_failure("OK")


def test_user_intent_disabled_by_default():
    detector = ConversationSignalDetector()
    messages = [
        _make_tool_msg("user", "不对，我要的是上海不是北京"),
    ]
    signals = detector.detect(messages, ["weather"])
    assert len(signals) == 0  # user_intent 默认关


def test_user_intent_enabled():
    detector = ConversationSignalDetector()
    messages = [
        _make_assistant_with_tool_calls([
            {"name": "read_file", "arguments": '{"path": "skills/weather/SKILL.md"}'},
        ]),
        _make_tool_msg("user", "不对，should be 上海 not 北京"),
    ]
    signals = detector.detect(messages, ["weather"], enabled_signals={"user_intent"})
    assert len(signals) == 1
    assert signals[0].type == "user_intent"
    assert signals[0].skill_name == "weather"


def test_detect_without_skill_match():
    """没有归因到任何 skill 的信号不应被产出。"""
    detector = ConversationSignalDetector()
    messages = [
        _make_tool_msg("tool", "Error: timeout", name="unknown_tool"),
    ]
    signals = detector.detect(messages, ["weather", "calculator"])
    assert len(signals) == 0  # 无法归因到已知 skill
