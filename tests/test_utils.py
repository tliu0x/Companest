"""Tests for companest/utils.py  robust JSON extraction from LLM output."""

import pytest
from companest.utils import extract_json_object, extract_json_array, _extract_balanced


class TestExtractJsonObject:
    def test_pure_json(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_with_surrounding_text(self):
        raw = 'Here is my answer:\n{"team": "research", "done": false}\nHope that helps!'
        result = extract_json_object(raw)
        assert result == {"team": "research", "done": False}

    def test_markdown_code_block(self):
        raw = '```json\n{"team": "eng", "mode": "default"}\n```'
        result = extract_json_object(raw)
        assert result == {"team": "eng", "mode": "default"}

    def test_nested_braces(self):
        raw = '{"team": "research", "sub_task": "find {x: 1} values", "done": false}'
        result = extract_json_object(raw)
        assert result is not None
        assert result["team"] == "research"
        assert "{x: 1}" in result["sub_task"]

    def test_nested_json_object(self):
        raw = '{"outer": {"inner": "value"}, "done": true}'
        result = extract_json_object(raw)
        assert result == {"outer": {"inner": "value"}, "done": True}

    def test_returns_none_for_array(self):
        assert extract_json_object('["a", "b"]') is None

    def test_returns_none_for_garbage(self):
        assert extract_json_object("no json here at all") is None

    def test_empty_string(self):
        assert extract_json_object("") is None

    def test_braces_in_string_values(self):
        raw = '{"msg": "use {} for empty dict", "ok": true}'
        result = extract_json_object(raw)
        assert result is not None
        assert result["ok"] is True

    def test_markdown_code_block_no_lang(self):
        raw = '```\n{"x": 1}\n```'
        result = extract_json_object(raw)
        assert result == {"x": 1}


class TestExtractJsonArray:
    def test_pure_array(self):
        assert extract_json_array('["a", "b", "c"]') == ["a", "b", "c"]

    def test_with_surrounding_text(self):
        raw = 'Here are the subtasks:\n["Research", "Analyze", "Write"]\nDone.'
        result = extract_json_array(raw)
        assert result == ["Research", "Analyze", "Write"]

    def test_markdown_code_block(self):
        raw = '```json\n["step1", "step2"]\n```'
        result = extract_json_array(raw)
        assert result == ["step1", "step2"]

    def test_nested_brackets(self):
        raw = '[["a", "b"], "c"]'
        result = extract_json_array(raw)
        assert result == [["a", "b"], "c"]

    def test_max_items(self):
        raw = '["a", "b", "c", "d", "e"]'
        result = extract_json_array(raw, max_items=3)
        assert result == ["a", "b", "c"]

    def test_max_items_zero_means_unlimited(self):
        raw = '["a", "b", "c"]'
        result = extract_json_array(raw, max_items=0)
        assert result == ["a", "b", "c"]

    def test_returns_none_for_object(self):
        assert extract_json_array('{"a": 1}') is None

    def test_returns_none_for_garbage(self):
        assert extract_json_array("no json here") is None

    def test_empty_array(self):
        assert extract_json_array("[]") == []


class TestExtractBalanced:
    def test_simple_braces(self):
        assert _extract_balanced('{"a": 1}', "{", "}") == '{"a": 1}'

    def test_nested(self):
        assert _extract_balanced('{"a": {"b": 2}}', "{", "}") == '{"a": {"b": 2}}'

    def test_with_prefix(self):
        result = _extract_balanced('prefix {"x": 1} suffix', "{", "}")
        assert result == '{"x": 1}'

    def test_brackets(self):
        assert _extract_balanced('["a", ["b"]]', "[", "]") == '["a", ["b"]]'

    def test_strings_with_delimiters(self):
        text = '{"msg": "hello {world}"}'
        result = _extract_balanced(text, "{", "}")
        assert result == text

    def test_no_match(self):
        assert _extract_balanced("no braces", "{", "}") is None

    def test_unbalanced(self):
        assert _extract_balanced("{unclosed", "{", "}") is None
