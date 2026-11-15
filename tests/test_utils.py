"""Tests for companest/utils.py  robust JSON extraction from LLM output."""

import pytest
from companest.utils import extract_json_object, extract_json_array


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    (
        'Here is my answer:\n{"team": "research", "done": false}\nHope that helps!',
        {"team": "research", "done": False},
    ),
    ('```json\n{"team": "eng", "mode": "default"}\n```', {"team": "eng", "mode": "default"}),
    ('```\n{"x": 1}\n```', {"x": 1}),
    ('Result: {"outer": {"inner": "value"}, "done": true} end', {"outer": {"inner": "value"}, "done": True}),
    # braces inside string values must not confuse the balanced scan
    ('Answer: {"msg": "use {} or {x: 1}", "ok": true} done', {"msg": "use {} or {x: 1}", "ok": True}),
    ('["a", "b"]', None),
    ("no json here at all", None),
    ("prefix {unclosed", None),
    ("", None),
])
def test_extract_json_object(raw, expected):
    assert extract_json_object(raw) == expected


@pytest.mark.parametrize("raw,max_items,expected", [
    ('["a", "b", "c"]', 0, ["a", "b", "c"]),
    ('Here are the subtasks:\n["Research", "Analyze", "Write"]\nDone.', 0, ["Research", "Analyze", "Write"]),
    ('```json\n["step1", "step2"]\n```', 0, ["step1", "step2"]),
    ('Steps: [["a", "b"], "c"] done', 0, [["a", "b"], "c"]),
    ('["a", "b", "c", "d", "e"]', 3, ["a", "b", "c"]),
    ("[]", 0, []),
    ('{"a": 1}', 0, None),
    ("no json here", 0, None),
])
def test_extract_json_array(raw, max_items, expected):
    assert extract_json_array(raw, max_items=max_items) == expected
