"""PlanCodeValidator — 在 exec() 前对 plan_code 做 AST 级安全检查。"""

from __future__ import annotations

import ast


# 禁止的函数调用名
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

# 禁止的 dunder 属性名
# 注意：__init__ 未被禁止 — super().__init__() 是合法的
_DENIED_DUNDER_ATTRS: frozenset[str] = frozenset(
    {
        "__import__",
        "__builtins__",
        "__code__",
        "__globals__",
        "__locals__",
        "__dict__",
        "__traceback__",
        "__class__",
        "__bases__",
        "__subclasses__",
        "__mro__",
    }
)

# 对象上禁止的属性名（如 tb_frame、f_back、f_builtins）
_DENIED_ATTR_NAMES: frozenset[str] = frozenset(
    {
        "tb_frame",
        "f_back",
        "f_builtins",
        "f_globals",
        "f_locals",
    }
)

# 禁止裸 import 的模块
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

# from-import 允许的导入前缀
_ALLOWED_IMPORT_PREFIX = "twinkle.agentserver.workflow"


class PlanCodeValidator:
    """plan_code 的 AST 级校验器。

    通过检查 AST 确认代码可安全 exec()：
    - 只允许 ``from ... import``（不允许裸 ``import x``）
    - from-import 必须以允许的前缀开头
    - 禁止的函数调用（exec、eval、open 等）
    - 禁止的 dunder 属性访问
    - 禁止裸 import 的模块
    - 禁止相对导入
    - 语法错误优雅上报
    """

    def validate(self, plan_code: str) -> list[str]:
        """校验 plan_code，返回错误列表。空列表表示通过。"""
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
    # 内部节点 visitor
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
        # 禁止相对导入
        if node.level and node.level > 0:
            errors.append(
                f"Relative imports are forbidden (line {node.lineno})"
            )
            return

        module = node.module or ""
        # 检查 module 是否以允许的前缀开头
        if not module.startswith(_ALLOWED_IMPORT_PREFIX):
            errors.append(
                f"Forbidden import: {module} — only imports from {_ALLOWED_IMPORT_PREFIX} are allowed (line {node.lineno})"
            )

    def _check_attribute(self, node: ast.Attribute, errors: list[str]) -> None:
        if node.attr in _DENIED_DUNDER_ATTRS:
            errors.append(
                f"Forbidden dunder access: {node.attr} (line {node.lineno})"
            )
        if node.attr in _DENIED_ATTR_NAMES:
            errors.append(
                f"Forbidden attribute access: {node.attr} (line {node.lineno})"
            )

    def _check_call(self, node: ast.Call, errors: list[str]) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in _DENIED_CALL_NAMES:
            errors.append(
                f"Forbidden call: {func.id} (line {node.lineno})"
            )
