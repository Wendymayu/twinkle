"""手写的最小 schema 提取器。

把 Python 函数的签名 + docstring 转成 OpenAI function-calling 的
`parameters` JSON schema。纯标准库,约 50 行。

支持的类型映射:
  str -> string, int -> integer, float -> number, bool -> boolean,
  list/List[...] -> array, dict/Dict[...] -> object(无 properties)。
  Optional[X] / X | None -> 解包 X,标记为非必需。
未知类型回退为 {"type": "string"}。

不做逐参数 description 解析(YAGNI)。需要覆盖时用 @tool(input_params=...)。
"""
from __future__ import annotations

import inspect
import types
import typing
from typing import Any, Callable, get_args, get_origin, get_type_hints

_PRIMITIVE = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}

_TYPES_NONE = (type(None),)


def _unwrap_optional(type_: Any) -> tuple[Any, bool]:
    """返回 (inner_type, is_optional)。识别 Optional[X] / X | None。"""
    origin = get_origin(type_)
    if origin is typing.Union or origin is types.UnionType:
        args = [arg for arg in get_args(type_) if arg not in _TYPES_NONE]
        is_optional = len(args) < len(get_args(type_))
        inner = args[0] if args else str
        return inner, is_optional
    return type_, False


def _type_to_schema(type_: Any) -> dict:
    inner, _ = _unwrap_optional(type_)
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
    return {"type": "string"}  # 未知 -> 安全回退


def _description_from_docstring(func: Callable) -> str:
    doc = inspect.getdoc(func)
    if not doc:
        return ""
    first_para = doc.split("\n\n")[0].strip()
    # 把内部换行折叠成空格
    return " ".join(first_para.split())


def extract(func: Callable) -> tuple[str, str, dict]:
    """返回从 `func` 提取的 (name, description, parameters)。"""
    name = func.__name__
    description = _description_from_docstring(func)

    hints = get_type_hints(func)
    sig = inspect.signature(func)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for param_name, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        type_ = hints.get(param_name, str)
        schema = _type_to_schema(type_)
        if param.default is not inspect.Parameter.empty and param.default is not None:
            schema["default"] = param.default
        else:
            # 无默认值的 Optional 类型(Optional[X])不是必需参数。
            _, is_optional = _unwrap_optional(type_)
            if not is_optional:
                required.append(param_name)
        properties[param_name] = schema

    parameters = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    return name, description, parameters
