"""Sandbox — safe namespace for exec(plan_code) isolation.

Provides a restricted execution environment for plan_code that:
1. Replaces __builtins__ with a safe whitelist (no open/exec/eval/getattr)
2. Replaces __import__ with a custom safe_import that blocks forbidden modules
3. Exposes PlanNode and HookInterrupt for plan_code to use
"""

from __future__ import annotations

import importlib
from typing import Any

# ~40 safe builtins — no open/exec/eval/getattr/type
_SAFE_BUILTINS: dict[str, Any] = {
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
    # Exception types — plan_code may need to raise
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "RuntimeError": RuntimeError,
    "NotImplementedError": NotImplementedError,
    "StopIteration": StopIteration,
    "AttributeError": AttributeError,
}

# Modules that plan_code must never import
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

# Only imports under these prefixes are allowed
_ALLOWED_IMPORT_PREFIXES: tuple[str, ...] = ("twinkle.agentserver.workflow",)


def safe_import(
    name: str,
    globals_: dict[str, Any] | None = None,
    locals_: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    """Custom __import__ replacement for the sandbox.

    Blocks:
    - Relative imports (level > 0)
    - Forbidden modules (os, sys, subprocess, etc.)
    - Any module not under an allowed prefix

    Raises ImportError for any violation.
    """
    if level:
        raise ImportError(f"Relative imports are forbidden in plan_code (level={level})")

    # Check forbidden modules
    top_level = name.split(".")[0]
    if top_level in _FORBIDDEN_MODULES or name in _FORBIDDEN_MODULES:
        raise ImportError(f"plan_code cannot import forbidden module: {name}")

    # Check allowed prefixes
    if not any(name.startswith(prefix) for prefix in _ALLOWED_IMPORT_PREFIXES):
        raise ImportError(
            f"plan_code cannot import: {name} — "
            f"only imports from {_ALLOWED_IMPORT_PREFIXES} are allowed"
        )

    # Use importlib.import_module to actually load the module
    module = importlib.import_module(name)
    if fromlist:
        # Ensure sub-module attributes are accessible
        for item_name in fromlist:
            if not hasattr(module, item_name):
                try:
                    importlib.import_module(f"{name}.{item_name}")
                except ImportError:
                    pass  # Non-submodule attribute — same as __import__ behavior
        return module
    # fromlist empty: import x.y returns top-level package x
    if "." in name:
        import sys

        return sys.modules[top_level]
    return module


def build_namespace() -> dict[str, Any]:
    """Build a sandboxed namespace for exec(plan_code).

    - Replaces __builtins__ with the safe whitelist
    - Injects custom __import__ via safe_import
    - Lazy-imports PlanNode and HookInterrupt into the namespace
    """
    # Lazy imports — PlanNode may not exist yet (Task 4)
    from twinkle.agentserver.hooks.base import HookInterrupt

    try:
        from twinkle.agentserver.workflow.node import PlanNode
    except ImportError:
        PlanNode = None  # type: ignore[assignment,misc]

    builtins = dict(_SAFE_BUILTINS)
    builtins["__import__"] = safe_import

    namespace: dict[str, Any] = {
        "__builtins__": builtins,
        "PlanNode": PlanNode,
        "HookInterrupt": HookInterrupt,
    }
    return namespace
