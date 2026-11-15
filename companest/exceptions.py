"""Companest Framework Exceptions"""


class CompanestError(Exception):
    """Base exception for all Companest framework errors."""

    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


class ConfigurationError(CompanestError):
    """Raised when Companest configuration is invalid."""
    pass


class OrchestratorError(CompanestError):
    """Raised when orchestration fails."""
    pass


class GatewayError(CompanestError):
    """Gateway WebSocket protocol errors."""
    pass


class GatewayConnectionError(GatewayError):
    """Raised when WebSocket connection to the gateway fails."""
    pass


class GatewayAuthError(GatewayError):
    """Raised when authentication to the gateway fails."""
    pass


class MasterError(CompanestError):
    """Raised for master connection failures."""
    pass


class JobError(CompanestError):
    """Raised for job management failures."""
    pass


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

