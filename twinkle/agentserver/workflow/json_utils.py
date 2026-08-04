"""JSON extraction utilities — robustly extract JSON from LLM output."""

from __future__ import annotations

import json
import re
from typing import Any


def extract_llm_json(
    raw: str | dict | list,
    expected_type: type = dict,
) -> Any:
    """
    Robustly extract JSON from LLM output.

    Handles four return forms:
      1. Already dict/list → return as-is
      2. Pure JSON string → parse
      3. ```json ... ``` code block → extract and parse
      4. JSON embedded in text → bracket counting extraction

    Args:
        raw: Raw LLM output data
        expected_type: Expected JSON type (dict or list)

    Returns:
        Parsed JSON object

    Raises:
        ValueError: When JSON cannot be parsed
    """
    # Already the target type — return as-is
    if isinstance(raw, expected_type):
        return raw

    # Accept other structured types too
    if isinstance(raw, (dict, list)):
        return raw

    if not isinstance(raw, str):
        raise ValueError(f"LLM返回了未预期的类型: {type(raw)}")

    # Try direct parse
    first_error: json.JSONDecodeError | None = None
    try:
        result = json.loads(raw)
        if isinstance(result, expected_type):
            return result
        # Parsed successfully but type mismatch
        first_error = None
    except json.JSONDecodeError as e:
        first_error = e

    # Extract ```json ... ``` / ``` ... ``` code block
    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if code_block:
        try:
            result = json.loads(code_block.group(1).strip())
            if isinstance(result, expected_type):
                return result
        except json.JSONDecodeError:
            pass

    # Bracket counting extraction for the first complete JSON structure
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

    # Build error message with context
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
    Extract the outermost complete JSON structure using bracket counting.

    Args:
        text: Raw text
        open_char: Opening bracket character ({ or [)
        close_char: Closing bracket character (} or ])

    Returns:
        Extracted JSON string, or None if not found
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
