"""PlanCodeValidator 的测试——AST 级安全检查。"""

from __future__ import annotations

import pytest

from twinkle.agentserver.workflow.validator import PlanCodeValidator


@pytest.fixture
def validator() -> PlanCodeValidator:
    return PlanCodeValidator()


# 1. 只允许 `from ... import`
def test_valid_import_only(validator: PlanCodeValidator) -> None:
    code = "from twinkle.agentserver.workflow.nodes import StepNode"
    assert validator.validate(code) == []


# 2. 裸 `import x` 被拒绝
def test_reject_bare_import(validator: PlanCodeValidator) -> None:
    code = "import json"
    errors = validator.validate(code)
    assert len(errors) == 1
    assert "Only 'from ... import' is allowed" in errors[0]


# 3. exec() 被禁止
def test_reject_exec_call(validator: PlanCodeValidator) -> None:
    code = "exec('print(1)')"
    errors = validator.validate(code)
    assert len(errors) == 1
    assert "Forbidden call: exec" in errors[0]


# 4. eval() 被禁止
def test_reject_eval_call(validator: PlanCodeValidator) -> None:
    code = "eval('1+1')"
    errors = validator.validate(code)
    assert len(errors) == 1
    assert "Forbidden call: eval" in errors[0]


# 5. open() 被禁止
def test_reject_open_call(validator: PlanCodeValidator) -> None:
    code = "open('/etc/passwd')"
    errors = validator.validate(code)
    assert len(errors) == 1
    assert "Forbidden call: open" in errors[0]


# 6. import os 被禁止
def test_reject_os_import(validator: PlanCodeValidator) -> None:
    code = "import os"
    errors = validator.validate(code)
    assert len(errors) == 1
    assert "Forbidden import: os" in errors[0]


# 7. import subprocess 被禁止
def test_reject_subprocess_import(validator: PlanCodeValidator) -> None:
    code = "import subprocess"
    errors = validator.validate(code)
    assert len(errors) == 1
    assert "Forbidden import: subprocess" in errors[0]


# 8. from twinkle.agentserver.workflow... import 允许
def test_valid_from_import_allowed_prefix(validator: PlanCodeValidator) -> None:
    code = "from twinkle.agentserver.workflow.nodes import StepNode"
    assert validator.validate(code) == []


# 9. __import__ 被禁止
def test_reject_dunder_access(validator: PlanCodeValidator) -> None:
    code = "__import__('os')"
    errors = validator.validate(code)
    assert any("Forbidden call: __import__" in e for e in errors)


# 10. getattr() 被禁止
def test_reject_getattr_call(validator: PlanCodeValidator) -> None:
    code = "getattr(obj, 'secret')"
    errors = validator.validate(code)
    assert len(errors) == 1
    assert "Forbidden call: getattr" in errors[0]


# 11. 语法错误被上报
def test_syntax_error_in_code(validator: PlanCodeValidator) -> None:
    code = "def foo("
    errors = validator.validate(code)
    assert len(errors) == 1
    assert "Syntax error" in errors[0]


# 12. 空代码通过
def test_empty_code(validator: PlanCodeValidator) -> None:
    assert validator.validate("") == []
    assert validator.validate("   ") == []
