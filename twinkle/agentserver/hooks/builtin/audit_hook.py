"""AuditHook — always-on 工具执行审计(与 permissions.enabled 解耦)。

priority=95,在 PermissionHook(100)之后跑,可读到 force_finish 控制流信号。
三回调覆盖全部 outcome:
  - before_tool_call:被拦截(当前=权限 DENY,高 priority hook 已 request_force_finish)
    → @hook 跳过方法体,after/on_tool_exception 都不触发,在此读
    ctx.is_force_finish_requested() 捕获 outcome=denied(不读任何 hook 的私有标记,
    不与 PermissionHook 耦合)。
  - after_tool_call:成功 → outcome=success,result 取 ctx.extra['_tool_result']
    (@hook 装饰器存入)。
  - on_tool_exception:异常 → outcome=ToolError.kind(denied/validation/failed)或 error。

配置懒读 settings.audit.tool_execution(对齐 RepeatToolCallDetectorHook 的
_get_* 模式):构造处直接 AuditHook() 无参即可;__init__ 参数仅用于测试 override。
不脱敏,只截断(脱敏是独立模块,先不做;审计文件在可信本地 workspace)。
fail-soft:写失败只告警(由 _append_jsonl 兜底)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from twinkle.agentserver.hooks.base import AgentHook, HookContext, ToolCallInputs
from twinkle.agentserver.permissions.audit import _append_jsonl
from twinkle.agentserver.permissions.models import ToolExecutionAuditEntry


class AuditHook(AgentHook):
    """Records every tool call's name/args/result/outcome to a JSONL audit file."""

    priority = 95  # after PermissionHook(100), before LoggingHook(10)

    def __init__(
        self,
        *,
        path: str | None = None,
        enabled: bool | None = None,
        max_arg_chars: int | None = None,
        max_result_chars: int | None = None,
    ) -> None:
        self._path_override = path
        self._enabled_override = enabled
        self._max_arg_override = max_arg_chars
        self._max_result_override = max_result_chars

    async def before_tool_call(self, ctx: HookContext) -> None:
        # 被拦截路径:高 priority hook(当前=PermissionHook 权限 deny)已 request_force_finish,
        # 方法体将被 @hook 跳过,after/on_tool_exception 都不触发,故在此记一行 outcome=denied。
        # 读控制流状态而非某个 hook 的私有标记 → 不与 PermissionHook 耦合。
        if not self._enabled():
            return
        if ctx.is_force_finish_requested():
            self._write(self._entry(ctx, outcome="denied"))

    async def after_tool_call(self, ctx: HookContext) -> None:
        if not self._enabled():
            return
        result = ctx.extra.get("_tool_result", "")
        self._write(self._entry(
            ctx, outcome="success", result=self._truncate(str(result), self._max_result())))

    async def on_tool_exception(self, ctx: HookContext) -> None:
        if not self._enabled():
            return
        exc = ctx.exception
        outcome = getattr(exc, "kind", None) or "error"  # ToolError.kind 或 "error"
        self._write(self._entry(
            ctx, outcome=outcome, error=self._truncate(str(exc), self._max_result())))

    # --- config resolve (lazy,对齐 RepeatToolCallDetectorHook) ---

    def _enabled(self) -> bool:
        # bool 字段不能 `or`(False or fallback 会误读 config),显式 None 判断。
        return self._enabled_override if self._enabled_override is not None else _get_enabled()

    def _path(self) -> Path:
        return Path(self._path_override or _get_audit_file())

    def _max_arg(self) -> int:
        return self._max_arg_override or _get_max_arg_chars()

    def _max_result(self) -> int:
        return self._max_result_override or _get_max_result_chars()

    # --- helpers ---

    def _entry(
        self,
        ctx: HookContext,
        outcome: str,
        result: str = "",
        error: str = "",
    ) -> ToolExecutionAuditEntry:
        inputs: ToolCallInputs = ctx.inputs  # type: ignore[assignment]
        return ToolExecutionAuditEntry(
            tool=inputs.name,
            outcome=outcome,
            args=self._fmt_args(inputs.args),
            result=result,
            error=error,
            tool_call_id=inputs.tool_call_id,
            session_id=ctx.session_id,
            request_id=ctx.request_id,
        )

    def _fmt_args(self, args: Any) -> str:
        try:
            s = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            s = str(args)
        return self._truncate(s, self._max_arg())

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if limit <= 0 or len(value) <= limit:
            return value
        return f"{value[:limit]}...[truncated]"

    def _write(self, entry: ToolExecutionAuditEntry) -> None:
        _append_jsonl(self._path(), entry.to_dict())


# --- Config lazy reads (对齐 RepeatToolCallDetectorHook 的 _get_* 模式) ---

def _get_enabled() -> bool:
    from twinkle.config import settings
    return settings.audit.tool_execution.enabled


def _get_audit_file() -> str:
    from twinkle.config import settings
    return settings.audit.tool_execution.file


def _get_max_arg_chars() -> int:
    from twinkle.config import settings
    return settings.audit.tool_execution.max_arg_chars


def _get_max_result_chars() -> int:
    from twinkle.config import settings
    return settings.audit.tool_execution.max_result_chars
