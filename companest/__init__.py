"""
Companest - Pi Agent Team Orchestrator

Routes tasks to Pi Agent Teams that call LLM APIs directly via SDK.
Master connection receives tasks from Telegram/chat gateway.

Core Components:
- CompanestOrchestrator: Central coordinator (team routing)
- Pi Agent Teams: Department-structured agent collaboration
- CostGate: Three-tier cost approval (auto/notify/escalate)
- SmartRouter: LLM-powered auto-routing
- MemoryManager: Hierarchical memory (master -> team -> pi)
- EventBus: Async pub/sub lifecycle events
- ToolRegistry: Pluggable tool provider system
"""

#  Always-available core imports 
from .config import (
    CompanestConfig,
    APIConfig,
    MasterConfig,
)
from .orchestrator import CompanestOrchestrator
from .events import EventBus, Event, EventType
from .tools import ToolRegistry, ToolProvider, ToolContext, DEFAULT_TOOLS_DENY, SESSIONS_TOOL_NAMES
from .parser import (
    MarkdownConfigParser,
    ParseResult,
    CodeBlock,
    parse_markdown_config,
    validate_config_file,
    generate_config_template,
)
from .watcher import ConfigWatcher, ConfigChangeEvent, HotReloadOrchestrator
from .memory import (
    MemoryManager, EnrichmentSource, Dreamer, DreamerError,
    MemoryBackend, FileBackend, QdrantBackend, MemorySearchService,
    S3Sync, S3SyncConfig,
)
from .pi import Pi, PiConfig
from .team import AgentTeam, TeamConfig, TeamRegistry
from .cost_gate import CostGate, CostEstimate, CostDecision, UserNotifier
from .cascade import (
    CascadeEngine,
    CascadeStrategy,
    CascadeMetrics,
    AdequacyChecker,
    ModelTier,
)
from .scheduler import Scheduler
from .router import TeamRouter, SmartRouter, RoutingDecision, TeamAssignment, RoutingBinding
from .modes import (
    ExecutionMode,
    ModeRegistry,
    build_default_registry,
    VALID_MODES,
    DefaultMode,
    LoopMode,
    CouncilMode,
    CollaborativeMode,
)
from .company import CompanyConfig, CompanyRegistry, CompanyError, GlobalBinding, ResolveResult
from .component import CompanyComponent, CompanyContext, CompanyMemoryNamespace
from .evolution import (
    EvolutionEngine,
    EvolutionProposal,
    ObservationSource,
    Observation,
    SourceType,
    SourceTier,
    ProposalStatus,
)
from .canary import CanaryManager, CanaryDeployment, CanaryStage
from .app import Companest, CompanyBuilder
from .templates import (
    BUILTIN_TEMPLATES,
    get_template,
    list_templates,
    TemplateNotFoundError,
)
from .output import OutputSink, MemorySink, WebhookSink, CallbackSink, TelegramReportSink
from .ceo_engine import build_cycle_prompt, generate_ceo_soul
from .exceptions import (
    CompanestError,
    ConfigurationError,
    OrchestratorError,
    GatewayError,
    GatewayConnectionError,
    GatewayAuthError,
    MasterError,
    JobError,
    PiError,
    TeamError,
    CostGateError,
    ArchiverError,
    SchedulerError,
    EvolutionError,
    CanaryError,
)

#  Optional imports (may fail if extra deps not installed) 

try:
    from .client import GatewayClient
except ImportError:
    GatewayClient = None

try:
    from .jobs import JobManager, Job, JobStatus
except ImportError:
    JobManager = None
    Job = None
    JobStatus = None

try:
    from .server import CompanestAPIServer
except ImportError:
    CompanestAPIServer = None

try:
    from .master import MasterConnection
except ImportError:
    MasterConnection = None

try:
    from .archiver import MemoryArchiver
except ImportError:
    MemoryArchiver = None

try:
    from .user_scheduler import UserScheduler
except ImportError:
    UserScheduler = None


#  Build __all__ dynamically (exclude None entries) 

_all_names = [
    # Config
    "CompanestConfig", "APIConfig", "MasterConfig",
    # Company
    "CompanyConfig", "CompanyRegistry", "CompanyError", "GlobalBinding", "ResolveResult",
    # Component
    "CompanyComponent", "CompanyContext", "CompanyMemoryNamespace",
    # Evolution
    "EvolutionEngine", "EvolutionProposal", "ObservationSource", "Observation",
    "SourceType", "SourceTier", "ProposalStatus",
    # Canary
    "CanaryManager", "CanaryDeployment", "CanaryStage",
    # SDK entry point
    "Companest", "CompanyBuilder",
    # Templates
    "BUILTIN_TEMPLATES", "get_template", "list_templates", "TemplateNotFoundError",
    # Output sinks
    "OutputSink", "MemorySink", "WebhookSink", "CallbackSink", "TelegramReportSink",
    # CEO engine
    "build_cycle_prompt", "generate_ceo_soul",
    # Orchestrator
    "CompanestOrchestrator",
    # Events
    "EventBus", "Event", "EventType",
    # Tools
    "ToolRegistry", "ToolProvider", "ToolContext",
    # Client (optional)
    "GatewayClient",
    # Jobs (optional)
    "JobManager", "Job", "JobStatus",
    # Server (optional)
    "CompanestAPIServer",
    # Master (optional)
    "MasterConnection",
    # Parser
    "MarkdownConfigParser", "ParseResult", "CodeBlock",
    "parse_markdown_config", "validate_config_file", "generate_config_template",
    # Watcher
    "ConfigWatcher", "ConfigChangeEvent", "HotReloadOrchestrator",
    # Pi Agent Teams
    "MemoryManager", "EnrichmentSource", "Dreamer", "DreamerError",
    "Pi", "PiConfig",
    "AgentTeam", "TeamConfig", "TeamRegistry",
    "CostGate", "CostEstimate", "CostDecision", "UserNotifier",
    # Cascade
    "CascadeEngine", "CascadeStrategy", "CascadeMetrics", "AdequacyChecker", "ModelTier",
    "MemoryArchiver",
    "UserScheduler",
    "Scheduler",
    "TeamRouter", "SmartRouter", "RoutingDecision", "TeamAssignment", "RoutingBinding",
    "DEFAULT_TOOLS_DENY", "SESSIONS_TOOL_NAMES",
    # Execution Modes
    "ExecutionMode", "ModeRegistry", "build_default_registry", "VALID_MODES",
    "DefaultMode", "LoopMode", "CouncilMode", "CollaborativeMode",
    # Exceptions
    "CompanestError", "ConfigurationError", "OrchestratorError",
    "GatewayError", "GatewayConnectionError", "GatewayAuthError",
    "MasterError", "JobError", "PiError", "TeamError",
    "CostGateError", "ArchiverError", "SchedulerError", "CompanyError",
    "EvolutionError", "CanaryError",
]

__all__ = [name for name in _all_names if globals().get(name) is not None]

__version__ = "1.0.0"
