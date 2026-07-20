"""Minimal hand-written schema extractor.

Turns a Python function's signature + docstring into the OpenAI
function-calling `parameters` JSON schema. Pure stdlib, ~50 lines.

Supported type mapping:
  str -> string, int -> integer, float -> number, bool -> boolean,
  list/List[...] -> array, dict/Dict[...] -> object (no properties).
  Optional[X] / X | None -> unwrap X, mark non-required.
Unknown types fall back to {"type": "string"}.

No per-param description parsing (YAGNI). Override via @tool(input_params=...).
"""
from __future__ import annotations

import inspect
import typing
from typing import Any, Callable, get_args, get_origin, get_type_hints

_PRIMITIVE = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

_TYPES_NONE = (type(None),)


def _unwrap_optional(tp: Any) -> tuple[Any, bool]:
    """Return (inner_type, is_optional). Detects Optional[X] / X | None."""
    origin = get_origin(tp)
    if origin is typing.Union:
        args = [a for a in get_args(tp) if a not in _TYPES_NONE]
        is_optional = len(args) < len(get_args(tp))
        inner = args[0] if args else str
        return inner, is_optional
    return tp, False


def _type_to_schema(tp: Any) -> dict:
    inner, _ = _unwrap_optional(tp)
    if inner in _PRIMITIVE:
        return {"type": _PRIMITIVE[inner]}
    origin = get_origin(inner)
    if origin in (list, typing.List):
        return {"type": "array"}
    if origin in (dict, typing.Dict):
        return {"type": "object"}
    if inner is dict:
        return {"type": "object"}
    if inner is list:
        return {"type": "array"}
    return {"type": "string"}  # unknown -> safe fallback


def _description_from_docstring(func: Callable) -> str:
    doc = inspect.getdoc(func)
    if not doc:
        return ""
    first_para = doc.split("\n\n")[0].strip()
    # collapse internal newlines to spaces
    return " ".join(first_para.split())


def extract(func: Callable) -> tuple[str, str, dict]:
    """Return (name, description, parameters) extracted from `func`."""
    name = func.__name__
    description = _description_from_docstring(func)

    hints = get_type_hints(func)
    sig = inspect.signature(func)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        tp = hints.get(pname, str)
        schema = _type_to_schema(tp)
        if param.default is not inspect.Parameter.empty and param.default is not None:
            schema["default"] = param.default
        else:
            # Optional types (Optional[X] with no default) are not required.
            _, is_optional = _unwrap_optional(tp)
            if not is_optional:
                required.append(pname)
        properties[pname] = schema

    parameters = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    return name, description, parameters
