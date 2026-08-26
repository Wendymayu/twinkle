"""Sandbox — 为 exec(plan_code) 隔离提供安全 namespace。

为 plan_code 提供受限的执行环境：
1. 用安全白名单替换 __builtins__（无 open/exec/eval/getattr）
2. 用自定义 safe_import 替换 __import__，阻止禁止的模块
3. 向 plan_code 暴露 PlanNode、HookInterrupt 和受限的 asyncio
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

# 约 40 个安全 builtin — 无 open/exec/eval/getattr/type
_SAFE_BUILTINS: dict[str, Any] = {
    "__build_class__": __build_class__,  # required for class statements in exec()
    "True": True,
    "False": False,
    "None": None,
    "abs": abs,
    "all": all,
    "any": any,
    "bin": bin,
    "bool": bool,
    "chr": chr,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "hex": hex,
    "int": int,
    "isinstance": isinstance,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "oct": oct,
    "ord": ord,
    "range": range,
    "repr": repr,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    # I/O — print 用于 workflow 日志（输出到 AgentServer stdout）
    "print": print,
    # 异常类型 — plan_code 可能需要抛出
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "RuntimeError": RuntimeError,
    "NotImplementedError": NotImplementedError,
    "StopIteration": StopIteration,
    "AttributeError": AttributeError,
    # 类继承所需
    "super": super,
    "property": property,
}

# plan_code 绝不能 import 的模块
_FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "signal",
        "ctypes",
        "socket",
        "http",
        "urllib",
    }
)

# 只允许这些前缀下的 import
_ALLOWED_IMPORT_PREFIXES: tuple[str, ...] = ("twinkle.agentserver.workflow",)


class _SafeAsyncio:
    """受限的 asyncio 代理 — 只暴露安全的协程。

    阻止 create_subprocess_shell/exec、open_connection、start_server 等。
    """

    _ALLOWED = frozenset({
        "sleep", "gather", "create_task", "wait_for",
        "shield", "timeout", "Event", "Lock", "Queue",
        "run", "iscoroutine", "iscoroutinefunction",
    })

    def __getattr__(self, name: str) -> Any:
        if name in self._ALLOWED:
            return getattr(asyncio, name)
        raise AttributeError(
            f"asyncio.{name} is forbidden in plan_code"
        )


def safe_import(
    name: str,
    globals_: dict[str, Any] | None = None,
    locals_: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    """sandbox 的自定义 __import__ 替换。

    阻止：
    - 相对导入（level > 0）
    - 禁止的模块（os、sys、subprocess 等）
    - 任何不在允许前缀下的模块

    任何违规都抛 ImportError。
    """
    if level:
        raise ImportError(f"Relative imports are forbidden in plan_code (level={level})")

    # 检查禁止的模块
    top_level = name.split(".")[0]
    if top_level in _FORBIDDEN_MODULES or name in _FORBIDDEN_MODULES:
        raise ImportError(f"plan_code cannot import forbidden module: {name}")

    # 检查允许的前缀
    if not any(name.startswith(prefix) for prefix in _ALLOWED_IMPORT_PREFIXES):
        raise ImportError(
            f"plan_code cannot import: {name} — "
            f"only imports from {_ALLOWED_IMPORT_PREFIXES} are allowed"
        )

    # 用 importlib.import_module 真正加载模块
    module = importlib.import_module(name)
    if fromlist:
        # 确保子模块属性可访问
        for item_name in fromlist:
            if not hasattr(module, item_name):
                try:
                    importlib.import_module(f"{name}.{item_name}")
                except ImportError:
                    pass  # 非子模块属性 — 同 __import__ 的行为
        return module
    # fromlist 为空：import x.y 返回顶层包 x
    if "." in name:
        import sys

        return sys.modules[top_level]
    return module


def build_namespace() -> dict[str, Any]:
    """为 exec(plan_code) 构建沙箱 namespace。

    - 用安全白名单替换 __builtins__
    - 通过 safe_import 注入自定义 __import__
    - 惰性导入 PlanNode 和 HookInterrupt 到 namespace
    """
    # 惰性 import — PlanNode 此时可能还不存在（Task 4）
    from twinkle.agentserver.hooks.base import HookInterrupt

    try:
        from twinkle.agentserver.workflow.node import PlanNode
    except ImportError:
        PlanNode = None  # type: ignore[assignment,misc]

    builtins = dict(_SAFE_BUILTINS)
    builtins["__import__"] = safe_import

    namespace: dict[str, Any] = {
        "__builtins__": builtins,
        "__name__": "__workflow_plan__",
        "__qualname__": "__workflow_plan__",
        "PlanNode": PlanNode,
        "HookInterrupt": HookInterrupt,
        "asyncio": _SafeAsyncio(),
    }
    return namespace
