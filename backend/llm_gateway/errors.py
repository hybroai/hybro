from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from llm_gateway._diagnostics import _Diagnostic
    from llm_gateway.error_classification import ClassifiedGatewayError


class LLMGatewayError(RuntimeError):
    """Base exception for gateway-owned runtime failures."""


class LLMStreamingUnsupportedError(LLMGatewayError):
    """Raised when a resolved provider cannot stream text chunks."""


class LLMModelRoutingError(LLMGatewayError):
    """Raised when a model cannot be resolved to a provider safely."""


class LLMProviderConfigurationError(LLMGatewayError):
    """Raised when a selected provider has no valid credential/configuration."""


class UnsupportedConfiguredProvider(LLMProviderConfigurationError):
    """Raised for a retired provider found in deployment settings."""

    code = "unsupported_configured_provider"


ProviderUnavailableCode = Literal["provider_not_configured", "credential_invalid"]

_UNAVAILABLE_MESSAGES: dict[str, str] = {
    "provider_not_configured": "No model provider is configured; open Settings, Model provider.",
    "credential_invalid": (
        "The model provider credential is missing or does not match the "
        "configuration; open Settings, Model provider."
    ),
}


class ProviderNotConfigured(LLMProviderConfigurationError):
    """No working Provider pair: unconfigured, or a credential that cannot be used.

    Never retried: the state changes only when setup saves a new pair.
    """

    def __init__(self, code: ProviderUnavailableCode) -> None:
        super().__init__(_UNAVAILABLE_MESSAGES[code])
        self.code: ProviderUnavailableCode = code


class LLMProviderFailure(LLMGatewayError):
    """Sanitized failure retaining only retry classification and correlation."""

    def __init__(
        self,
        classification: ClassifiedGatewayError,
        client_request_id: str | None,
        *,
        diagnostic: _Diagnostic | None = None,
    ) -> None:
        super().__init__("Provider text operation failed")
        self._diagnostic = diagnostic
        self.classification = classification
        self.client_request_id = client_request_id
        self.code = classification.error_class
