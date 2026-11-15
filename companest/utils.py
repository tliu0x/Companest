import json
import re
from typing import Any, Dict, Iterator, List, Optional


def _json_candidates(text: str, open_char: str, close_char: str) -> Iterator[str]:
    yield text
    md_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if md_match:
        yield md_match.group(1).strip()
    balanced = _extract_balanced(text, open_char, close_char)
    if balanced is not None:
        yield balanced


def _extract_json(raw: str, kind: type, open_char: str, close_char: str):
    for candidate in _json_candidates(raw.strip(), open_char, close_char):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, kind):
            return parsed
    return None


def extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON object from LLM output that may contain surrounding text."""
    return _extract_json(raw, dict, "{", "}")


def extract_json_array(raw: str, max_items: int = 0) -> Optional[List[Any]]:
    """Extract a JSON array from LLM output that may contain surrounding text."""
    parsed = _extract_json(raw, list, "[", "]")
    return parsed[:max_items] if parsed and max_items else parsed


def _extract_balanced(text: str, open_char: str, close_char: str) -> Optional[str]:
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
