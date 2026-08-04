"""PlanCodeValidator — AST-level safety checks for plan_code before exec()."""

from __future__ import annotations

import ast


# Forbidden function call names
_DENIED_CALL_NAMES: frozenset[str] = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "open",
        "input",
        "getattr",
        "setattr",
        "delattr",
        "type",
        "__import__",
        "globals",
        "locals",
        "vars",
        "dir",
    }
)

# Forbidden dunder attribute names
_DENIED_DUNDER_ATTRS: frozenset[str] = frozenset(
    {
        "__import__",
        "__builtins__",
        "__code__",
        "__globals__",
        "__locals__",
        "__dict__",
    }
)

# Forbidden modules for bare import
_DENIED_MODULES: frozenset[str] = frozenset(
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
        "asyncio.subprocess",
    }
)

# Allowed import prefix for from-imports
_ALLOWED_IMPORT_PREFIX = "twinkle.agentserver.workflow"


class PlanCodeValidator:
    """AST-level validator for plan_code.

    Checks that the code is safe to exec() by inspecting the AST for:
    - Only ``from ... import`` allowed (no bare ``import x``)
    - from-imports must start with the allowed prefix
    - Forbidden function calls (exec, eval, open, etc.)
    - Forbidden dunder attribute access
    - Forbidden modules for bare import
    - Relative imports forbidden
    - Syntax errors reported gracefully
    """

    def validate(self, plan_code: str) -> list[str]:
        """Validate plan_code, returning a list of errors. Empty list means pass."""
        if not plan_code.strip():
            return []

        try:
            tree = ast.parse(plan_code)
        except SyntaxError as exc:
            return [f"Syntax error: {exc}"]

        errors: list[str] = []
        for node in ast.walk(tree):
            self._check_node(node, errors)
        return errors

    # ------------------------------------------------------------------
    # Internal node visitors
    # ------------------------------------------------------------------

    def _check_node(self, node: ast.AST, errors: list[str]) -> None:
        if isinstance(node, ast.Import):
            self._check_import(node, errors)
        elif isinstance(node, ast.ImportFrom):
            self._check_import_from(node, errors)
        elif isinstance(node, ast.Attribute):
            self._check_attribute(node, errors)
        elif isinstance(node, ast.Call):
            self._check_call(node, errors)

    def _check_import(self, node: ast.Import, errors: list[str]) -> None:
        for alias in node.names:
            module = alias.name
            if module in _DENIED_MODULES:
                errors.append(
                    f"Forbidden import: {module} (line {node.lineno})"
                )
            else:
                errors.append(
                    f"Only 'from ... import' is allowed, not bare import (line {node.lineno})"
                )

    def _check_import_from(self, node: ast.ImportFrom, errors: list[str]) -> None:
        # Relative imports forbidden
        if node.level and node.level > 0:
            errors.append(
                f"Relative imports are forbidden (line {node.lineno})"
            )
            return

        module = node.module or ""
        # Check if module starts with allowed prefix
        if not module.startswith(_ALLOWED_IMPORT_PREFIX):
            errors.append(
                f"Forbidden import: {module} — only imports from {_ALLOWED_IMPORT_PREFIX} are allowed (line {node.lineno})"
            )

    def _check_attribute(self, node: ast.Attribute, errors: list[str]) -> None:
        if node.attr in _DENIED_DUNDER_ATTRS:
            errors.append(
                f"Forbidden dunder access: {node.attr} (line {node.lineno})"
            )

    def _check_call(self, node: ast.Call, errors: list[str]) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in _DENIED_CALL_NAMES:
            errors.append(
                f"Forbidden call: {func.id} (line {node.lineno})"
            )
