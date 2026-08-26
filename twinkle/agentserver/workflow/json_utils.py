"""JSON 提取工具 — 从 LLM 输出中稳健地提取 JSON。"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_llm_json(
    raw: str | dict | list,
    expected_type: type = dict,
) -> Any:
    """
    从 LLM 输出中稳健地提取 JSON。

    处理四种返回形式：
      1. 已是 dict/list → 原样返回
      2. 纯 JSON 字符串 → 解析
      3. ```json ... ``` 代码块 → 提取并解析
      4. 嵌入文本中的 JSON → 括号计数提取

    Args:
        raw: 原始 LLM 输出数据
        expected_type: 期望的 JSON 类型（dict 或 list）

    Returns:
        解析后的 JSON 对象

    Raises:
        ValueError: JSON 无法解析时抛出
    """
    # 已是目标类型 — 原样返回
    if isinstance(raw, expected_type):
        return raw

    # 也接受其他结构化类型
    if isinstance(raw, (dict, list)):
        return raw

    if not isinstance(raw, str):
        raise ValueError(f"LLM返回了未预期的类型: {type(raw)}")

    # 尝试直接解析
    first_error: json.JSONDecodeError | None = None
    try:
        result = json.loads(raw)
        if isinstance(result, expected_type):
            return result
        # 解析成功但类型不匹配
        first_error = None
    except json.JSONDecodeError as e:
        first_error = e

    # 提取 ```json ... ``` / ``` ... ``` 代码块
    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if code_block:
        try:
            result = json.loads(code_block.group(1).strip())
            if isinstance(result, expected_type):
                return result
        except json.JSONDecodeError:
            pass

    # 用括号计数提取第一个完整的 JSON 结构
    open_char = "[" if expected_type == list else "{"
    close_char = "]" if expected_type == list else "}"
    candidate = _extract_outermost_json(raw, open_char, close_char)
    if candidate is not None:
        try:
            result = json.loads(candidate)
            if isinstance(result, expected_type):
                return result
        except json.JSONDecodeError:
            pass

    # 构建带上下文的错误信息
    if first_error is not None:
        context_start = max(0, first_error.pos - 80)
        context_end = min(len(raw), first_error.pos + 80)
        error_context = raw[context_start:context_end].replace("\n", "\\n")
        raise ValueError(
            f"无法从LLM输出中解析JSON（期望{expected_type.__name__}）："
            f"{first_error.msg}（第{first_error.lineno}行第{first_error.colno}列）。"
            f"出错位置附近：...{error_context}..."
        )
    raise ValueError(
        f"无法从LLM输出中解析JSON（期望{expected_type.__name__}）：{raw[:300]}"
    )


def _extract_outermost_json(
    text: str,
    open_char: str,
    close_char: str,
) -> str | None:
    """
    用括号计数提取最外层完整的 JSON 结构。

    Args:
        text: 原始文本
        open_char: 开括号字符（{ 或 [）
        close_char: 闭括号字符（} 或 ]）

    Returns:
        提取出的 JSON 字符串，未找到则返回 None
    """
    depth = 0
    start_idx = -1
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == close_char:
            if depth > 0:
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    return text[start_idx : i + 1]

    return None
