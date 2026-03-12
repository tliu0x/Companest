"""
Companest shared utilities.

Robust JSON extraction from LLM output, shared across modes and dreamer.
"""

import json
import re
from typing import Any, Dict, List, Optional


def extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON object from LLM output that may contain surrounding text.

    Strategy (in order):
    1. Direct json.loads
    2. Extract from markdown code block (```json ... ``` or ``` ... ```)
    3. Balanced-brace extraction (handles nested {})
    """
    stripped = raw.strip()

    # 1. Direct parse
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Markdown code block
    md_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", stripped, re.DOTALL)
    if md_match:
        try:
            parsed = json.loads(md_match.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. Balanced brace extraction  find the first top-level { ... }
    result = _extract_balanced(stripped, "{", "}")
    if result is not None:
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def extract_json_array(raw: str, max_items: int = 0) -> Optional[List[Any]]:
    """Extract a JSON array from LLM output that may contain surrounding text.

    Strategy (in order):
    1. Direct json.loads
    2. Extract from markdown code block
    3. Balanced-bracket extraction (handles nested [])

    Args:
        raw: Raw LLM output text
        max_items: If >0, truncate the array to this many items
    """
    stripped = raw.strip()

    # 1. Direct parse
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return parsed[:max_items] if max_items else parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Markdown code block
    md_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", stripped, re.DOTALL)
    if md_match:
        try:
            parsed = json.loads(md_match.group(1).strip())
            if isinstance(parsed, list):
                return parsed[:max_items] if max_items else parsed
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. Balanced bracket extraction
    result = _extract_balanced(stripped, "[", "]")
    if result is not None:
        try:
            parsed = json.loads(result)
            if isinstance(parsed, list):
                return parsed[:max_items] if max_items else parsed
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _extract_balanced(text: str, open_char: str, close_char: str) -> Optional[str]:
    """Extract the first balanced substring delimited by open/close chars.

    Handles nested delimiters and JSON strings (skips chars inside quotes).
    Returns the matched substring including delimiters, or None.
    """
    start = text.find(open_char)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape:
            escape = False
            continue

        if ch == "\\":
            if in_string:
                escape = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None
