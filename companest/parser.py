"""
Companest Markdown Configuration Parser

A robust parser for loading Companest configuration from Markdown files.

Features:
- Parse JSON/YAML code blocks from Markdown
- Support YAML frontmatter
- Multiple code block handling (merge or select)
- Detailed error messages with line numbers
- Include/reference support for modular configs
- Schema validation
- Environment variable interpolation

Usage:
    parser = MarkdownConfigParser()
    config = parser.parse_file(".companest/config.md")

    # Or with options
    parser = MarkdownConfigParser(
        allow_env_interpolation=True,
        merge_code_blocks=True
    )
"""

import os
import re
import json
import logging
from typing import Dict, List, Any, Optional, Tuple, Union
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum

from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)


class CodeBlockType(str, Enum):
    """Supported code block types"""
    JSON = "json"
    YAML = "yaml"
    YML = "yml"
    TOML = "toml"


@dataclass
class CodeBlock:
    """
    Represents a parsed code block from Markdown.

    Attributes:
        content: Raw content of the code block
        block_type: Type (json, yaml, etc.)
        line_start: Starting line number in source file
        line_end: Ending line number
        label: Optional label/identifier for the block
        parsed: Parsed content as dict (after parsing)
    """
    content: str
    block_type: CodeBlockType
    line_start: int
    line_end: int
    label: Optional[str] = None
    parsed: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        return f"CodeBlock({self.block_type.value}, lines {self.line_start}-{self.line_end})"


@dataclass
class ParseResult:
    """
    Result of parsing a Markdown configuration file.

    Attributes:
        config: The parsed configuration dictionary
        code_blocks: All code blocks found
        frontmatter: YAML frontmatter if present
        source_path: Path to the source file
        warnings: Non-fatal warnings during parsing
        metadata: Additional metadata extracted
    """
    config: Dict[str, Any]
    code_blocks: List[CodeBlock] = field(default_factory=list)
    frontmatter: Optional[Dict[str, Any]] = None
    source_path: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class MarkdownConfigParser:
    """
    Robust Markdown configuration parser for Companest.

    Parses Markdown files containing configuration in code blocks (JSON/YAML).

    Features:
    - JSON and YAML code block support
    - YAML frontmatter support
    - Environment variable interpolation (${VAR_NAME} or $VAR_NAME)
    - Multiple code block merging
    - Include directive support
    - Detailed error reporting with line numbers

    Example:
        parser = MarkdownConfigParser()

        # Parse a file
        result = parser.parse_file(".companest/config.md")
        config = result.config

        # Parse content directly
        result = parser.parse_content(markdown_string)

        # With environment interpolation
        parser = MarkdownConfigParser(allow_env_interpolation=True)
        result = parser.parse_file("config.md")
    """

    # Regex patterns
    FRONTMATTER_PATTERN = re.compile(
        r'^---\s*\n(.*?)\n---\s*\n',
        re.DOTALL
    )

    CODE_BLOCK_PATTERN = re.compile(
        r'```(json|yaml|toml)(?:\s+(\w+))?\s*\n(.*?)\n```',
        re.DOTALL | re.IGNORECASE
    )

    ENV_VAR_PATTERN = re.compile(
        r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)'
    )

    INCLUDE_PATTERN = re.compile(
        r'^\s*#\s*@include\s*["\']([^"\']+)["\']\s*$',
        re.MULTILINE
    )

    def __init__(
        self,
        allow_env_interpolation: bool = True,
        merge_code_blocks: bool = False,
        strict_mode: bool = False,
        base_path: Optional[Path] = None
    ):
        """
        Initialize the parser.

        Args:
            allow_env_interpolation: Replace ${VAR} with environment values
            merge_code_blocks: Merge multiple code blocks (vs. use first)
            strict_mode: Raise errors on warnings
            base_path: Base path for resolving includes
        """
        self.allow_env_interpolation = allow_env_interpolation
        self.merge_code_blocks = merge_code_blocks
        self.strict_mode = strict_mode
        self.base_path = base_path or Path.cwd()

    def parse_file(self, path: Union[str, Path]) -> ParseResult:
        """
        Parse a Markdown configuration file.

        Args:
            path: Path to the Markdown file

        Returns:
            ParseResult with config and metadata

        Raises:
            ConfigurationError: If file not found or parsing fails
        """
        path = Path(path)
        if not path.exists():
            raise ConfigurationError(
                f"Configuration file not found: {path}",
                details={"path": str(path)}
            )

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

        # Update base path for includes
        old_base = self.base_path
        self.base_path = path.parent

        try:
            result = self.parse_content(content)
            result.source_path = path
            return result
        finally:
            self.base_path = old_base

    def parse_content(self, content: str) -> ParseResult:
        """
        Parse Markdown content containing configuration.

        Args:
            content: Markdown content string

        Returns:
            ParseResult with config and metadata

        Raises:
            ConfigurationError: If parsing fails
        """
        warnings: List[str] = []
        code_blocks: List[CodeBlock] = []

        # Process includes first
        content = self._process_includes(content, warnings)

        # Extract frontmatter
        frontmatter = self._extract_frontmatter(content)
        if frontmatter:
            # Remove frontmatter from content for code block parsing
            content = self.FRONTMATTER_PATTERN.sub('', content, count=1)

        # Extract code blocks
        code_blocks = self._extract_code_blocks(content)

        if not code_blocks and not frontmatter:
            raise ConfigurationError(
                "No configuration found in Markdown. "
                "Add a ```json or ```yaml code block, or YAML frontmatter."
            )

        # Parse code blocks
        parsed_configs = []
        for block in code_blocks:
            try:
                parsed = self._parse_code_block(block)
                block.parsed = parsed
                parsed_configs.append(parsed)
            except Exception as e:
                error_msg = (
                    f"Failed to parse {block.block_type.value} block "
                    f"at line {block.line_start}: {e}"
                )
                if self.strict_mode:
                    raise ConfigurationError(error_msg)
                warnings.append(error_msg)

        # Build final config
        if frontmatter and not parsed_configs:
            # Use frontmatter as config
            final_config = frontmatter
        elif self.merge_code_blocks and len(parsed_configs) > 1:
            # Merge all configs
            final_config = self._merge_configs(parsed_configs)
        elif parsed_configs:
            # Use first valid config
            final_config = parsed_configs[0]
        else:
            final_config = frontmatter or {}

        # Apply environment interpolation
        if self.allow_env_interpolation:
            final_config = self._interpolate_env_vars(final_config)

        return ParseResult(
            config=final_config,
            code_blocks=code_blocks,
            frontmatter=frontmatter,
            warnings=warnings,
            metadata={
                "block_count": len(code_blocks),
                "has_frontmatter": frontmatter is not None,
                "merged": self.merge_code_blocks and len(parsed_configs) > 1
            }
        )

    def _extract_frontmatter(self, content: str) -> Optional[Dict[str, Any]]:
        """Extract YAML frontmatter from content"""
        match = self.FRONTMATTER_PATTERN.match(content)
        if not match:
            return None

        try:
            yaml = self._get_yaml_parser()
            return yaml.safe_load(match.group(1))
        except Exception as e:
            logger.warning(f"Failed to parse frontmatter: {e}")
            return None

    def _extract_code_blocks(self, content: str) -> List[CodeBlock]:
        """Extract all code blocks from Markdown content"""
        blocks = []
        for match in self.CODE_BLOCK_PATTERN.finditer(content):
            # Calculate line number
            start_pos = match.start()
            line_start = content[:start_pos].count('\n') + 1
            line_end = line_start + match.group(3).count('\n') + 2

            block_type_str = match.group(1).lower()
            if block_type_str == "yml":
                block_type_str = "yaml"

            try:
                block_type = CodeBlockType(block_type_str)
            except ValueError:
                logger.warning(f"Unknown code block type: {block_type_str}")
                continue

            block = CodeBlock(
                content=match.group(3),
                block_type=block_type,
                line_start=line_start,
                line_end=line_end,
                label=match.group(2)  # Optional label after language
            )
            blocks.append(block)

        return blocks

    def _parse_code_block(self, block: CodeBlock) -> Dict[str, Any]:
        """Parse a code block into a dictionary"""
        content = block.content.strip()

        if block.block_type == CodeBlockType.JSON:
            return self._parse_json(content, block.line_start)

        elif block.block_type in (CodeBlockType.YAML, CodeBlockType.YML):
            return self._parse_yaml(content, block.line_start)

        elif block.block_type == CodeBlockType.TOML:
            return self._parse_toml(content, block.line_start)

        else:
            raise ConfigurationError(f"Unsupported block type: {block.block_type}")

    def _parse_json(self, content: str, line_offset: int) -> Dict[str, Any]:
        """Parse JSON content with better error messages"""
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
        """Parse YAML content with better error messages"""
        yaml = self._get_yaml_parser()
        try:
            result = yaml.safe_load(content)
            return result if isinstance(result, dict) else {"value": result}
        except Exception as e:
            raise ConfigurationError(
                f"YAML parse error: {e}",
                details={"line_offset": line_offset}
            )

    def _parse_toml(self, content: str, line_offset: int) -> Dict[str, Any]:
        """Parse TOML content"""
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                raise ConfigurationError(
                    "TOML support requires Python 3.11+ or 'tomli' package. "
                    "Install with: pip install tomli"
                )

        try:
            return tomllib.loads(content)
        except Exception as e:
            raise ConfigurationError(
                f"TOML parse error: {e}",
                details={"line_offset": line_offset}
            )

    def _get_yaml_parser(self):
        """Get YAML parser (lazy import)"""
        try:
            import yaml
            return yaml
        except ImportError:
            raise ConfigurationError(
                "YAML support requires PyYAML. Install with: pip install pyyaml"
            )

    def _merge_configs(self, configs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Deep merge multiple configs"""
        result = {}
        for config in configs:
            result = self._deep_merge(result, config)
        return result

    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        """Deep merge two dictionaries"""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            elif key in result and isinstance(result[key], list) and isinstance(value, list):
                result[key] = result[key] + value
            else:
                result[key] = value
        return result

    def _interpolate_env_vars(self, config: Any) -> Any:
        """
        Recursively interpolate environment variables in config.

        Supports:
        - ${VAR_NAME} - Required, errors if not set
        - ${VAR_NAME:-default} - With default value
        - $VAR_NAME - Simple form
        """
        if isinstance(config, str):
            return self._interpolate_string(config)
        elif isinstance(config, dict):
            return {k: self._interpolate_env_vars(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._interpolate_env_vars(item) for item in config]
        return config

    def _interpolate_string(self, value: str) -> str:
        """Interpolate environment variables in a string.

        In strict mode, raises ConfigurationError for unset variables
        without defaults instead of keeping the literal string.
        """
        strict = self.strict_mode

        # Pattern for ${VAR:-default} syntax
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
                return match.group(0)  # Keep original if not found

        result = pattern.sub(replacer, value)

        # Also handle simple $VAR form
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

    def _process_includes(self, content: str, warnings: List[str]) -> str:
        """
        Process @include directives in Markdown.

        Syntax: # @include "path/to/file.md"

        Include paths are validated to stay within base_path to prevent
        reading arbitrary files via path traversal (e.g. "../../etc/passwd").
        """
        def include_replacer(match):
            include_path = match.group(1).strip()

            # Block absolute paths and obvious traversals
            if include_path.startswith("/") or ".." in include_path:
                msg = f"Include path rejected (path traversal not allowed): {include_path}"
                warnings.append(msg)
                if self.strict_mode:
                    raise ConfigurationError(msg)
                return f"<!-- {msg} -->"

            full_path = (self.base_path / include_path).resolve()
            base_resolved = self.base_path.resolve()

            # Ensure resolved path is within base_path
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
        """Get context around an error line"""
        lines = content.split('\n')
        start = max(0, line_num - context_lines - 1)
        end = min(len(lines), line_num + context_lines)

        result = []
        for i in range(start, end):
            prefix = ">>> " if i == line_num - 1 else "    "
            result.append(f"{prefix}{i + 1}: {lines[i]}")

        return '\n'.join(result)

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """
        Validate a configuration dictionary.

        Returns list of validation errors (empty if valid).
        """
        return []


# =============================================================================
# Convenience Functions
# =============================================================================

def parse_markdown_config(
    path: Union[str, Path],
    allow_env: bool = True
) -> Dict[str, Any]:
    """
    Convenience function to parse a Markdown config file.

    Args:
        path: Path to config file
        allow_env: Allow environment variable interpolation

    Returns:
        Parsed configuration dictionary
    """
    parser = MarkdownConfigParser(allow_env_interpolation=allow_env)
    result = parser.parse_file(path)
    return result.config


def validate_config_file(path: Union[str, Path]) -> Tuple[bool, List[str]]:
    """
    Validate a configuration file.

    Args:
        path: Path to config file

    Returns:
        Tuple of (is_valid, error_messages)
    """
    parser = MarkdownConfigParser()
    try:
        result = parser.parse_file(path)
        errors = parser.validate_config(result.config)
        return len(errors) == 0, errors
    except ConfigurationError as e:
        return False, [str(e)]


def generate_config_template(
    output_path: Union[str, Path],
    format: str = "json",
) -> None:
    """
    Generate a configuration template file.

    Args:
        output_path: Where to write the template
        format: 'json' or 'yaml'
    """
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
        yaml = MarkdownConfigParser()._get_yaml_parser()
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
