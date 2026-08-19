"""Task 3: mcp.safety.check_dangerous_args — stdio server args 危险参数拦截。

stdio 配置里 args 命中代码执行类 flag(如 python -c "..." )→ raise ValueError,
防 config 借 args 注入任意代码(本机单用户基本兜底)。
"""
import pytest

from twinkle.agentserver.mcp.safety import check_dangerous_args


def test_safe_args_pass() -> None:
    check_dangerous_args(["-y", "@modelcontextprotocol/server-filesystem", "/tmp"])  # no raise


@pytest.mark.parametrize("bad", ["-e", "--eval", "-c", "--command", "-i", "-m", "--interactive"])
def test_dangerous_args_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="dangerous argument"):
        check_dangerous_args([bad, "x"])


def test_dangerous_flag_anywhere_in_args_rejected() -> None:
    """v1 简单实现:args 列表任意位置出现危险 flag 即拦(不做 flag/value 语义区分)。"""
    with pytest.raises(ValueError, match="dangerous argument"):
        check_dangerous_args(["script.py", "-e", "print(1)"])
