"""Tests for workflow json_utils — extract_llm_json."""

from __future__ import annotations

import pytest

from twinkle.agentserver.workflow.json_utils import extract_llm_json


# 1. Already a dict → return as-is
def test_extract_dict_passthrough():
    data = {"key": "value"}
    assert extract_llm_json(data) is data


# 2. Already a list → return as-is
def test_extract_list_passthrough():
    data = [1, 2, 3]
    assert extract_llm_json(data, expected_type=list) is data


# 3. Plain JSON string → parse
def test_extract_pure_json_string():
    raw = '{"name": "twinkle", "version": 1}'
    result = extract_llm_json(raw)
    assert result == {"name": "twinkle", "version": 1}


# 4. ```json ... ``` code block
def test_extract_json_code_block():
    raw = 'Here is the result:\n```json\n{"status": "ok"}\n```\nDone.'
    result = extract_llm_json(raw)
    assert result == {"status": "ok"}


# 5. ``` ... ``` without 'json' label
def test_extract_json_code_block_no_lang():
    raw = 'Result:\n```\n{"status": "ok"}\n```\nDone.'
    result = extract_llm_json(raw)
    assert result == {"status": "ok"}


# 6. JSON embedded in text (bracket counting)
def test_extract_embedded_json():
    raw = 'The answer is {"a": 1, "b": 2} and that is final.'
    result = extract_llm_json(raw)
    assert result == {"a": 1, "b": 2}


# 7. List embedded in text
def test_extract_list_from_text():
    raw = 'Items are [1, 2, 3] as you requested.'
    result = extract_llm_json(raw, expected_type=list)
    assert result == [1, 2, 3]


# 8. No valid JSON → ValueError with Chinese error message
def test_extract_raises_on_invalid():
    with pytest.raises(ValueError, match="无法从LLM输出中解析JSON"):
        extract_llm_json("no json here at all")


# 9. Valid JSON but wrong type → ValueError
def test_extract_raises_on_wrong_type():
    with pytest.raises(ValueError, match="无法从LLM输出中解析JSON"):
        extract_llm_json('{"key": "value"}', expected_type=list)


# 10. Nested JSON → extract outermost
def test_extract_nested_json():
    raw = 'Result: {"outer": {"inner": 42}, "flag": true} end'
    result = extract_llm_json(raw)
    assert result == {"outer": {"inner": 42}, "flag": True}


# 11. Escaped quotes in string values
def test_extract_json_with_escaped_quotes():
    raw = '{"text": "He said \\"hello\\""}'
    result = extract_llm_json(raw)
    assert result == {"text": 'He said "hello"'}
