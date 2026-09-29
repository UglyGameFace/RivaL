class ProviderError(RuntimeError):
    """Base error for external sports-data providers."""


class ProviderRateLimited(ProviderError):
    """Provider rejected a request because its endpoint cooldown was exceeded."""


class ProviderQuotaExhausted(ProviderError):
    """The provider account has reached its hard request limit."""


class QuotaReserveReached(ProviderError):
    """A billable request would consume the configured emergency reserve."""


class ProviderProtocolError(ProviderError):
    """Provider returned a response that does not match the documented contract."""
