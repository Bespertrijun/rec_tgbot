class DomainError(Exception):
    """Expected business-rule failure safe to show to an operator/user."""


class BindingError(DomainError):
    pass


class EligibilityError(DomainError):
    pass


class UpstreamError(DomainError):
    pass


class AuthenticationCircuitOpen(UpstreamError):
    pass
