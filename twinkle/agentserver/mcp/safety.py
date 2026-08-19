"""stdio 配置安全——危险参数拦截(本机单用户基本兜底,不做多租户全套)。"""
from __future__ import annotations

# 代码执行类参数:允许 MCP server 跑自己的逻辑,但不允许 config 里借 args 注入任意代码。
_DANGEROUS_ARGS = ("-e", "--eval", "-c", "--command", "-i", "-m", "--interactive")


def check_dangerous_args(args: list[str]) -> None:
    """stdio server 的 args 命中危险 flag → raise ValueError。connect 前调。"""
    for a in args:
        if a in _DANGEROUS_ARGS:
            raise ValueError(f"dangerous argument '{a}' in stdio server args (code-injection risk)")
