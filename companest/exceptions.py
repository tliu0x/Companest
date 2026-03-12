"""
Companest Framework Exceptions

Hierarchical exception structure for the Companest framework:

CompanestError (base)
 ConfigurationError - Invalid configuration
 OrchestratorError - Orchestration failures
 GatewayError - Gateway WebSocket protocol errors
    GatewayConnectionError - WebSocket connection failures
    GatewayAuthError - Auth handshake failures
 MasterError - Master connection failures
 JobError - Job management failures
 PiError - Pi agent execution failures
 TeamError - Team-related failures
 CostGateError - Cost gate failures
 ArchiverError - Memory archiver failures
 SchedulerError - Scheduler failures
 CompanyError - Company registry failures
"""


class CompanestError(Exception):
    """
    Base exception for all Companest framework errors.

    All Companest-specific exceptions inherit from this class,
    allowing for easy exception handling at the framework level.
    """

    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


class ConfigurationError(CompanestError):
    """
    Raised when Companest configuration is invalid.

    Examples:
    - Missing required fields
    - Invalid server configuration
    - Invalid routing rules
    """
    pass


class OrchestratorError(CompanestError):
    """
    Raised when orchestration fails.

    Examples:
    - Failed to route task to team
    - All servers failed
    - Timeout during orchestration
    - Invalid task state
    """
    pass


class GatewayError(CompanestError):
    """
    Gateway WebSocket protocol errors.

    Examples:
    - Invalid frame format
    - Unexpected message type
    - Protocol version mismatch
    """
    pass


class GatewayConnectionError(GatewayError):
    """
    Raised when WebSocket connection to the gateway fails.

    Examples:
    - Connection refused
    - DNS resolution failure
    - TLS handshake failure
    - Connection timeout
    """
    pass


class GatewayAuthError(GatewayError):
    """
    Raised when authentication to the gateway fails.

    Examples:
    - Invalid auth token
    - Invalid password
    - Insufficient scopes
    - Auth handshake timeout
    """
    pass


class MasterError(CompanestError):
    """
    Raised for master connection failures.

    Examples:
    - Master connection lost
    - Task processing failure
    - Inbound request handling error
    """
    pass


class JobError(CompanestError):
    """
    Raised for job management failures.

    Examples:
    - Job not found
    - Job already completed
    - Job queue full
    - Persistence failure
    """
    pass


#  Pi Agent Team exceptions 

class PiError(CompanestError):
    """Raised for Pi agent execution failures."""
    pass


class TeamError(CompanestError):
    """Raised for team-related failures."""
    pass


class CostGateError(CompanestError):
    """Raised for cost gate failures."""
    pass


class ArchiverError(CompanestError):
    """Raised for memory archiver failures."""
    pass


class SchedulerError(CompanestError):
    """Raised for scheduler failures."""
    pass


class CompanyError(CompanestError):
    """Raised for company registry failures."""
    pass


class EvolutionError(CompanestError):
    """Raised for evolution engine failures."""
    pass


class CanaryError(CompanestError):
    """Raised for canary workflow failures."""
    pass
