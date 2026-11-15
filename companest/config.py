import os
import json
import logging
from typing import Dict, List, Optional, Any, Union
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)


class APIConfig(BaseModel):
    host: str = Field(default="0.0.0.0", description="API bind address")
    port: int = Field(default=8000, ge=1, le=65535, description="API listen port")
    enable_webhooks: bool = Field(default=True, description="Enable webhook endpoints")
    enable_websocket_events: bool = Field(default=True, description="Enable WS event stream")
    auth_token: Optional[str] = Field(None, repr=False, description="API bearer token (env: COMPANEST_API_TOKEN)")
    allowed_origins: List[str] = Field(default_factory=list, description="CORS allowed origins")

    @model_validator(mode="after")
    def resolve_api_auth(self):
        placeholders = ("$COMPANEST_API_TOKEN", "${COMPANEST_API_TOKEN}")
        if not self.auth_token or self.auth_token in placeholders:
            token = os.getenv("COMPANEST_API_TOKEN")
            self.auth_token = token if token and token not in placeholders else None
        return self


class MasterConfig(BaseModel):
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
        return f"ws://{self.host}:{self.port}"

    @model_validator(mode="after")
    def resolve_auth_token(self):
        placeholders = ("$COMPANEST_MASTER_TOKEN", "${COMPANEST_MASTER_TOKEN}")
        if not self.auth_token or self.auth_token in placeholders:
            token = os.getenv("COMPANEST_MASTER_TOKEN")
            self.auth_token = token if token and token not in placeholders else None
        return self


class ProxyConfig(BaseModel):
    enabled: bool = Field(default=False, description="Enable LiteLLM proxy routing")
    base_url: str = Field(default="http://localhost:4000", description="LiteLLM proxy URL")
    master_key: Optional[str] = Field(None, repr=False, description="LiteLLM admin key (env: LITELLM_MASTER_KEY)")
    default_key: Optional[str] = Field(None, repr=False, description="Default virtual key (env: LITELLM_DEFAULT_KEY)")

    @model_validator(mode="before")
    @classmethod
    def reject_removed_subscription_mode(cls, data):
        if isinstance(data, dict) and "subscription_mode" in data:
            raise ValueError(
                "proxy.subscription_mode has been removed. "
                "Use Anthropic API credentials or another officially supported provider path."
            )
        return data

    @model_validator(mode="after")
    def resolve_env(self):
        if not self.master_key:
            self.master_key = os.getenv("LITELLM_MASTER_KEY")
        if not self.default_key:
            self.default_key = os.getenv("LITELLM_DEFAULT_KEY")
        return self


class MCPServerConfig(BaseModel):
    name: str
    transport: str = Field(default="stdio")  # "stdio" | "sse" | "http"
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    url: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _validate(self):
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires 'command'")
        if self.transport in ("sse", "http") and not self.url:
            raise ValueError(f"{self.transport} transport requires 'url'")
        return self

    def to_sdk_config(self) -> dict:
        """Convert to the MCP server config dict stored in the ToolRegistry."""
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

    data_dir: str = Field(
        default=".companest",
        description="Runtime data directory for teams, companies, memory, jobs",
    )

    mcp_servers: List[MCPServerConfig] = Field(
        default_factory=list,
        description="External MCP servers shared across all teams",
    )

    memory_backend: str = Field(
        default="file",
        description=(
            "Memory backend type: 'file' (default), "
            "'qdrant' (file + vector search via Qdrant)."
        ),
    )
    memory_config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Backend-specific memory configuration",
    )

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
        from .parser import MarkdownConfigParser

        path = Path(path)
        if not path.exists():
            raise ConfigurationError(f"Configuration file not found: {path}")

        return cls(**MarkdownConfigParser().parse_file(path).config)

    @classmethod
    def from_markdown_content(cls, content: str) -> "CompanestConfig":
        from .parser import MarkdownConfigParser

        return cls(**MarkdownConfigParser().parse_content(content).config)

    @classmethod
    def from_json_file(cls, path: Union[str, Path]) -> "CompanestConfig":
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
        return self.model_dump(exclude_none=True)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)
