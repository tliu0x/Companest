class CompanestError(Exception):
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


class ConfigurationError(CompanestError):
    pass


class OrchestratorError(CompanestError):
    pass


class GatewayError(CompanestError):
    pass


class GatewayConnectionError(GatewayError):
    pass


class GatewayAuthError(GatewayError):
    pass


class MasterError(CompanestError):
    pass


class JobError(CompanestError):
    pass


class PiError(CompanestError):
    pass


class TeamError(CompanestError):
    pass


class ArchiverError(CompanestError):
    pass


class SchedulerError(CompanestError):
    pass


class CompanyError(CompanestError):
    pass
