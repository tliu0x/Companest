import os
import re
import json
import logging
import tomllib
from typing import Dict, List, Any, Optional, Union
from pathlib import Path
from dataclasses import dataclass, field

from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    config: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)


class MarkdownConfigParser:
    FRONTMATTER_PATTERN = re.compile(
        r'^---\s*\n(.*?)\n---\s*\n',
        re.DOTALL
    )

    CODE_BLOCK_PATTERN = re.compile(
        r'```(json|yaml|toml)(?:\s+(\w+))?\s*\n(.*?)\n```',
        re.DOTALL | re.IGNORECASE
    )

    INCLUDE_PATTERN = re.compile(
        r'^\s*#\s*@include\s*["\']([^"\']+)["\']\s*$',
        re.MULTILINE
    )

    def __init__(self, strict_mode: bool = False):
        self.strict_mode = strict_mode

    def parse_file(self, path: Union[str, Path]) -> ParseResult:
        path = Path(path)
        if not path.is_file():
            raise ConfigurationError(
                f"Path is not a file: {path}",
                details={"path": str(path)}
            )

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            raise ConfigurationError(
                f"Failed to read file: {e}",
                details={"path": str(path)}
            )

        return self.parse_content(content, path.parent)

    def parse_content(self, content: str, base_path: Optional[Path] = None) -> ParseResult:
        warnings: List[str] = []
        content = self._process_includes(content, base_path or Path.cwd(), warnings)

        frontmatter = self._extract_frontmatter(content)
        if frontmatter:
            content = self.FRONTMATTER_PATTERN.sub('', content, count=1)

        blocks = list(self.CODE_BLOCK_PATTERN.finditer(content))
        if not blocks and not frontmatter:
            raise ConfigurationError(
                "No configuration found in Markdown. "
                "Add a ```json or ```yaml code block, or YAML frontmatter."
            )

        parsers = {"json": self._parse_json, "yaml": self._parse_yaml, "toml": self._parse_toml}
        parsed_configs = []
        for match in blocks:
            block_type = match.group(1).lower()
            line_start = content[:match.start()].count('\n') + 1
            try:
                parsed_configs.append(parsers[block_type](match.group(3).strip(), line_start))
            except Exception as e:
                error_msg = f"Failed to parse {block_type} block at line {line_start}: {e}"
                if self.strict_mode:
                    raise ConfigurationError(error_msg)
                warnings.append(error_msg)

        config = parsed_configs[0] if parsed_configs else frontmatter or {}
        return ParseResult(config=self._interpolate_env_vars(config), warnings=warnings)

    def _extract_frontmatter(self, content: str) -> Optional[Dict[str, Any]]:
        match = self.FRONTMATTER_PATTERN.match(content)
        if not match:
            return None

        import yaml
        try:
            return yaml.safe_load(match.group(1))
        except Exception as e:
            logger.warning(f"Failed to parse frontmatter: {e}")
            return None

    def _parse_json(self, content: str, line_offset: int) -> Dict[str, Any]:
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            actual_line = line_offset + e.lineno
            raise ConfigurationError(
                f"JSON parse error at line {actual_line}, column {e.colno}: {e.msg}",
                details={
                    "line": actual_line,
                    "column": e.colno,
                    "context": self._get_error_context(content, e.lineno)
                }
            )

    def _parse_yaml(self, content: str, line_offset: int) -> Dict[str, Any]:
        import yaml

        try:
            result = yaml.safe_load(content)
            return result if isinstance(result, dict) else {"value": result}
        except Exception as e:
            raise ConfigurationError(
                f"YAML parse error: {e}",
                details={"line_offset": line_offset}
            )

    def _parse_toml(self, content: str, line_offset: int) -> Dict[str, Any]:
        try:
            return tomllib.loads(content)
        except Exception as e:
            raise ConfigurationError(
                f"TOML parse error: {e}",
                details={"line_offset": line_offset}
            )

    def _interpolate_env_vars(self, config: Any) -> Any:
        if isinstance(config, str):
            return self._interpolate_string(config)
        elif isinstance(config, dict):
            return {k: self._interpolate_env_vars(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._interpolate_env_vars(item) for item in config]
        return config

    def _interpolate_string(self, value: str) -> str:
        strict = self.strict_mode

        pattern = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}')

        def replacer(match):
            var_name = match.group(1)
            default = match.group(2)
            env_value = os.environ.get(var_name)

            if env_value is not None:
                return env_value
            elif default is not None:
                return default
            else:
                if strict:
                    raise ConfigurationError(
                        f"Environment variable not set: ${{{var_name}}} "
                        f"(set the variable or provide a default: ${{{var_name}:-default}})"
                    )
                logger.warning(f"Environment variable not set: {var_name}")
                return match.group(0)

        result = pattern.sub(replacer, value)

        simple_pattern = re.compile(r'\$([A-Za-z_][A-Za-z0-9_]*)')

        def simple_replacer(match):
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is not None:
                return env_value
            if strict:
                raise ConfigurationError(
                    f"Environment variable not set: ${var_name}"
                )
            return match.group(0)

        return simple_pattern.sub(simple_replacer, result)

    def _process_includes(self, content: str, base_path: Path, warnings: List[str]) -> str:
        def include_replacer(match):
            include_path = match.group(1).strip()

            if include_path.startswith("/") or ".." in include_path:
                msg = f"Include path rejected (path traversal not allowed): {include_path}"
                warnings.append(msg)
                if self.strict_mode:
                    raise ConfigurationError(msg)
                return f"<!-- {msg} -->"

            full_path = (base_path / include_path).resolve()
            base_resolved = base_path.resolve()

            if not str(full_path).startswith(str(base_resolved) + os.sep) and full_path != base_resolved:
                msg = f"Include path escapes base directory: {include_path}"
                warnings.append(msg)
                if self.strict_mode:
                    raise ConfigurationError(msg)
                return f"<!-- {msg} -->"

            if not full_path.exists():
                msg = f"Include file not found: {include_path}"
                warnings.append(msg)
                return f"<!-- {msg} -->"

            try:
                included_content = full_path.read_text(encoding="utf-8")
                logger.debug(f"Included: {include_path}")
                return included_content
            except Exception as e:
                msg = f"Failed to include {include_path}: {e}"
                warnings.append(msg)
                return f"<!-- {msg} -->"

        return self.INCLUDE_PATTERN.sub(include_replacer, content)

    def _get_error_context(self, content: str, line_num: int, context_lines: int = 2) -> str:
        lines = content.split('\n')
        start = max(0, line_num - context_lines - 1)
        end = min(len(lines), line_num + context_lines)

        result = []
        for i in range(start, end):
            prefix = ">>> " if i == line_num - 1 else "    "
            result.append(f"{prefix}{i + 1}: {lines[i]}")

        return '\n'.join(result)


def generate_config_template(
    output_path: Union[str, Path],
    format: str = "json",
) -> None:
    template = {
        "name": "my-companest-config",
        "version": "1.0",
        "api": {
            "host": "0.0.0.0",
            "port": 8000,
            "enable_webhooks": True,
            "enable_websocket_events": True,
        },
        "master": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 19000,
        },
    }

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if format == "yaml":
        import yaml
        code_block = yaml.dump(template, default_flow_style=False, sort_keys=False)
        lang = "yaml"
    else:
        code_block = json.dumps(template, indent=2)
        lang = "json"

    content = f"""# Companest Configuration

Generated configuration template for Companest orchestrator.

## Setup

Set your API keys as environment variables:
```bash
export ANTHROPIC_API_KEY="your-key"
export COMPANEST_API_TOKEN="your-api-token"
export COMPANEST_MASTER_TOKEN="your-master-token"
```

## Configuration

```{lang}
{code_block}
```
"""

    output_path.write_text(content, encoding="utf-8")
    logger.info(f"Generated config template: {output_path}")
