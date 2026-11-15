"""Companest - Pi Agent Team Orchestrator"""

from .config import (
    CompanestConfig,
    APIConfig,
    MasterConfig,
)
from .orchestration.orchestrator import CompanestOrchestrator
from .events import EventBus, Event, EventType
from .agents.tools import ToolRegistry, ToolProvider, ToolContext, DEFAULT_TOOLS_DENY, SESSIONS_TOOL_NAMES
from .parser import (
    MarkdownConfigParser,
    ParseResult,
    generate_config_template,
)
from .memory import (
    MemoryManager, EnrichmentSource, Dreamer, DreamerError,
    MemoryBackend, FileBackend, QdrantBackend, MemorySearchService,
)
from .agents.pi import Pi, PiConfig
from .agents.team import AgentTeam, TeamConfig, TeamRegistry
from .orchestration.cost_gate import CostGate, CostEstimate, CostDecision, UserNotifier
from .agents.cascade import (
    CascadeEngine,
    CascadeStrategy,
    CascadeMetrics,
    AdequacyChecker,
    ModelTier,
)
from .jobs.scheduler import Scheduler
from .orchestration.router import TeamRouter, SmartRouter, RoutingDecision, TeamAssignment, RoutingBinding
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
from .companies.company import CompanyConfig, CompanyRegistry, CompanyError, GlobalBinding, ResolveResult
from .companies.component import CompanyComponent, CompanyContext, CompanyMemoryNamespace
from .companies.output import OutputSink, MemorySink
from .companies.ceo_engine import build_cycle_prompt, generate_ceo_soul
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
    ArchiverError,
    SchedulerError,
)


try:
    from .gateway.client import GatewayClient
except ImportError:
    GatewayClient = None

try:
    from .jobs.jobs import JobManager, Job, JobStatus
except ImportError:
    JobManager = None
    Job = None
    JobStatus = None

try:
    from .server.server import CompanestAPIServer
except ImportError:
    CompanestAPIServer = None

try:
    from .gateway.master import MasterConnection
except ImportError:
    MasterConnection = None

try:
    from .memory.archiver import MemoryArchiver
except ImportError:
    MemoryArchiver = None

try:
    from .jobs.user_scheduler import UserScheduler
except ImportError:
    UserScheduler = None


_all_names = [
    # Config
    "CompanestConfig", "APIConfig", "MasterConfig",
    # Company
    "CompanyConfig", "CompanyRegistry", "CompanyError", "GlobalBinding", "ResolveResult",
    # Component
    "CompanyComponent", "CompanyContext", "CompanyMemoryNamespace",
    # Output sinks
    "OutputSink", "MemorySink",
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
    "MarkdownConfigParser", "ParseResult", "generate_config_template",
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
    "ArchiverError", "SchedulerError", "CompanyError",
]

__all__ = [name for name in _all_names if globals().get(name) is not None]

__version__ = "1.0.0"
