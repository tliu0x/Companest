"""
Companest Configuration Module

Defines all configuration structures for the Companest framework:
- APIConfig: FastAPI server configuration
- MasterConfig: Master connection configuration
- ProxyConfig: LiteLLM proxy configuration
- CompanestConfig: Root configuration combining all components

Configuration can be loaded from:
- Markdown files with JSON/YAML code blocks (.companest/config.md)
- Direct Python dictionaries
- Environment variables (for sensitive data like auth tokens)
"""

import os
import json
import logging
from typing import Dict, List, Optional, Any, Union
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration Models
# =============================================================================

class APIConfig(BaseModel):
    """
    Configuration for the Companest FastAPI server.

    Attributes:
        host: Bind address
        port: Listen port
        enable_webhooks: Enable webhook endpoints (for n8n)
        enable_websocket_events: Enable WebSocket event stream
        auth_token: Bearer token for API authentication (env: COMPANEST_API_TOKEN)
        allowed_origins: CORS allowed origins (empty = no CORS)
    """
    host: str = Field(default="0.0.0.0", description="API bind address")
    port: int = Field(default=8000, ge=1, le=65535, description="API listen port")
    enable_webhooks: bool = Field(default=True, description="Enable webhook endpoints")
    enable_websocket_events: bool = Field(default=True, description="Enable WS event stream")
    auth_token: Optional[str] = Field(None, repr=False, description="API bearer token (env: COMPANEST_API_TOKEN)")
    allowed_origins: List[str] = Field(default_factory=list, description="CORS allowed origins")

    @model_validator(mode="after")
    def resolve_api_auth(self):
        """Resolve API auth token from COMPANEST_API_TOKEN env var if not set."""
        if not self.auth_token:
            token = os.getenv("COMPANEST_API_TOKEN")
            if token:
                self.auth_token = token
        return self


class MasterConfig(BaseModel):
    """
    Configuration for connecting to a master gateway.

    When enabled, Companest connects TO the master as a controller and
    receives inbound task requests from it. The master is the
    user-facing gateway (Telegram, web chat, etc.).

    Attributes:
        enabled: Whether master connection is active
        host: Master gateway host address
        port: Master gateway port
        auth_token: Auth token (resolved from COMPANEST_MASTER_TOKEN env var if not set)
        auth_password: Optional password authentication
        max_concurrent_tasks: Max tasks to process concurrently
        task_timeout: Timeout for individual task processing (seconds)
        reconnect: Auto-reconnect on disconnection
        max_reconnect_attempts: Max reconnection attempts
    """
    enabled: bool = Field(default=False, description="Enable master connection")
    host: str = Field(default="", description="Master host address")
    port: int = Field(default=18789, ge=1, le=65535, description="Master gateway port")
    auth_token: Optional[str] = Field(None, repr=False, description="Auth token (prefer env vars)")
    auth_password: Optional[str] = Field(None, repr=False, description="Auth password")
    max_concurrent_tasks: int = Field(default=3, ge=1, le=20, description="Max concurrent tasks")
    task_timeout: int = Field(default=300, ge=1, le=3600, description="Task timeout in seconds")
    reconnect: bool = Field(default=True, description="Auto-reconnect on disconnect")
    max_reconnect_attempts: int = Field(default=10, ge=1, le=100, description="Max reconnect attempts")

    @property
    def ws_url(self) -> str:
        """WebSocket URL for the master"""
        return f"ws://{self.host}:{self.port}"

    @model_validator(mode="after")
    def resolve_auth_token(self):
        """Resolve auth token from COMPANEST_MASTER_TOKEN env var if not set"""
        if not self.auth_token:
            token = os.getenv("COMPANEST_MASTER_TOKEN")
            if token:
                self.auth_token = token
        return self


class ProxyConfig(BaseModel):
    """
    Configuration for LiteLLM proxy -key isolation + cost tracking.

    When enabled, Pi agents route all LLM calls through the proxy
    using virtual keys instead of real API keys.

    Attributes:
        enabled: Whether proxy routing is active
        base_url: LiteLLM proxy URL (e.g. http://litellm-host:4000)
        master_key: LiteLLM admin key for management API (env: LITELLM_MASTER_KEY)
        default_key: Default virtual key for Pi agents (env: LITELLM_DEFAULT_KEY)
    """
    enabled: bool = Field(default=False, description="Enable LiteLLM proxy routing")
    base_url: str = Field(default="http://localhost:4000", description="LiteLLM proxy URL")
    master_key: Optional[str] = Field(None, repr=False, description="LiteLLM admin key (env: LITELLM_MASTER_KEY)")
    default_key: Optional[str] = Field(None, repr=False, description="Default virtual key (env: LITELLM_DEFAULT_KEY)")

    @model_validator(mode="after")
    def resolve_env(self):
        """Resolve keys from environment variables if not set in config."""
        if not self.master_key:
            self.master_key = os.getenv("LITELLM_MASTER_KEY")
        if not self.default_key:
            self.default_key = os.getenv("LITELLM_DEFAULT_KEY")
        return self


class MCPServerConfig(BaseModel):
    """External MCP server configuration."""
    name: str
    transport: str = Field(default="stdio")  # "stdio" | "sse" | "http"
    command: Optional[str] = None       # stdio: executable
    args: List[str] = Field(default_factory=list)  # stdio: command args
    env: Dict[str, str] = Field(default_factory=dict)  # extra env vars
    url: Optional[str] = None           # sse/http: server URL
    headers: Dict[str, str] = Field(default_factory=dict)  # sse/http: headers

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _validate(self):
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires 'command'")
        if self.transport in ("sse", "http") and not self.url:
            raise ValueError(f"{self.transport} transport requires 'url'")
        return self

    def to_sdk_config(self) -> dict:
        """Convert to claude_agent_sdk McpServerConfig dict."""
        if self.transport == "stdio":
            cfg = {"type": "stdio", "command": self.command, "args": self.args}
            if self.env:
                resolved = {
                    k: os.getenv(v[2:-1], v) if v.startswith("${") and v.endswith("}") else v
                    for k, v in self.env.items()
                }
                cfg["env"] = resolved
            return cfg
        else:
            cfg = {"type": self.transport, "url": self.url}
            if self.headers:
                cfg["headers"] = self.headers
            return cfg


class CompanestConfig(BaseModel):
    """
    Root Companest configuration.

    This is the main configuration object that combines all Companest components:
    - api: FastAPI server configuration
    - master: Master connection configuration
    - proxy: LiteLLM proxy configuration

    Configuration can be loaded from:
    - Markdown files (.companest/config.md)
    - JSON/YAML files
    - Python dictionaries

    Example:
        config = CompanestConfig.from_markdown(".companest/config.md")
    """
    api: APIConfig = Field(
        default_factory=APIConfig,
        description="API server configuration"
    )
    master: MasterConfig = Field(
        default_factory=MasterConfig,
        description="Master connection configuration"
    )
    proxy: ProxyConfig = Field(
        default_factory=ProxyConfig,
        description="LiteLLM proxy configuration"
    )

    # Runtime data directory (teams, companies, memory, jobs)
    # Local dev: ".companest/", production: "/data/companest"
    data_dir: str = Field(
        default=".companest",
        description="Runtime data directory for teams, companies, memory, jobs",
    )

    # External MCP servers shared across all teams
    mcp_servers: List[MCPServerConfig] = Field(
        default_factory=list,
        description="External MCP servers shared across all teams",
    )

    # Memory backend
    memory_backend: str = Field(
        default="file",
        description=(
            "Memory backend type: 'file' (default), "
            "'qdrant' (file + vector search via Qdrant). "
            "'viking' is deprecated and falls back to 'file'."
        ),
    )
    memory_config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Backend-specific memory configuration",
    )

    # Global settings
    name: str = Field(default="companest-config", description="Configuration name")
    version: str = Field(default="1.0", description="Configuration version")
    global_timeout: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="Global timeout in seconds"
    )
    debug: bool = Field(default=False, description="Enable debug mode")

    @classmethod
    def from_markdown(cls, path: Union[str, Path]) -> "CompanestConfig":
        """
        Load configuration from a Markdown file.

        Args:
            path: Path to the Markdown file

        Returns:
            CompanestConfig instance

        Raises:
            ConfigurationError: If file not found or invalid format
        """
        from .parser import MarkdownConfigParser

        path = Path(path)
        if not path.exists():
            raise ConfigurationError(f"Configuration file not found: {path}")

        parser = MarkdownConfigParser(allow_env_interpolation=True)
        result = parser.parse_file(path)
        return cls(**result.config)

    @classmethod
    def from_markdown_content(cls, content: str) -> "CompanestConfig":
        """
        Parse configuration from Markdown content.

        Supports JSON, YAML code blocks and YAML frontmatter.

        Args:
            content: Markdown content string

        Returns:
            CompanestConfig instance
        """
        from .parser import MarkdownConfigParser

        parser = MarkdownConfigParser(allow_env_interpolation=True)
        result = parser.parse_content(content)
        return cls(**result.config)

    @classmethod
    def from_json_file(cls, path: Union[str, Path]) -> "CompanestConfig":
        """Load configuration from a JSON file"""
        path = Path(path)
        if not path.exists():
            raise ConfigurationError(f"Configuration file not found: {path}")

        try:
            config_dict = json.loads(path.read_text(encoding="utf-8"))
            return cls(**config_dict)
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"Invalid JSON: {e}")

    @classmethod
    def discover_config(cls, base_path: Union[str, Path] = ".") -> Optional["CompanestConfig"]:
        """
        Discover and load configuration from standard locations.

        Search order:
        1. .companest/config.md
        2. .companest/config.json
        3. companest.config.md
        4. companest.config.json

        Args:
            base_path: Base directory to search from

        Returns:
            CompanestConfig if found, None otherwise
        """
        base = Path(base_path)
        search_paths = [
            base / ".companest" / "config.md",
            base / ".companest" / "config.json",
            base / "companest.config.md",
            base / "companest.config.json",
        ]

        for path in search_paths:
            if path.exists():
                logger.info(f"Found Companest config at: {path}")
                if path.suffix == ".md":
                    return cls.from_markdown(path)
                else:
                    return cls.from_json_file(path)

        logger.warning("No Companest configuration found")
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary"""
        return self.model_dump(exclude_none=True)

    def to_json(self, indent: int = 2) -> str:
        """Convert configuration to JSON string"""
        return json.dumps(self.to_dict(), indent=indent)
