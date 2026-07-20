from typing import Optional

from twinkle.agentserver.tools.schema_extractor import extract


def _fn_basic(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return its visible text."""
    return ""


def _fn_plain(a: str, b: int) -> str:
    return ""


def _fn_optional(a: str, b: Optional[int] = None) -> str:
    return ""


def _fn_list_optional(a: str, tags: Optional[list] = None) -> str:
    """Has a list param."""
    return ""


def _fn_nodecs(x: str) -> str:
    return ""


def _fn_floats(rate: float, enabled: bool = False) -> str:
    """A float and a bool."""
    return ""


def test_name_from_function_name() -> None:
    name, _, _ = extract(_fn_basic)
    assert name == "_fn_basic"


def test_description_from_docstring() -> None:
    _, desc, _ = extract(_fn_basic)
    assert desc == "Fetch a URL and return its visible text."


def test_description_empty_when_no_docstring() -> None:
    _, desc, _ = extract(_fn_nodecs)
    assert desc == ""


def test_required_is_params_without_defaults() -> None:
    _, _, params = extract(_fn_plain)
    assert params["required"] == ["a", "b"]
    assert params["type"] == "object"


def test_type_mapping_basic() -> None:
    _, _, params = extract(_fn_plain)
    props = params["properties"]
    assert props["a"] == {"type": "string"}
    assert props["b"] == {"type": "integer"}


def test_default_value_recorded_and_not_required() -> None:
    _, _, params = extract(_fn_basic)
    props = params["properties"]
    assert props["url"] == {"type": "string"}
    assert props["max_chars"] == {"type": "integer", "default": 8000}
    assert params["required"] == ["url"]


def test_optional_unwrapped_and_not_required() -> None:
    _, _, params = extract(_fn_optional)
    props = params["properties"]
    assert props["a"] == {"type": "string"}
    assert props["b"] == {"type": "integer"}  # Optional[int] -> integer, no default here
    assert params["required"] == ["a"]


def test_optional_list_maps_to_array() -> None:
    _, _, params = extract(_fn_list_optional)
    props = params["properties"]
    assert props["tags"] == {"type": "array"}


def test_float_and_bool_types() -> None:
    _, _, params = extract(_fn_floats)
    props = params["properties"]
    assert props["rate"] == {"type": "number"}
    assert props["enabled"] == {"type": "boolean", "default": False}
    assert params["required"] == ["rate"]
