"""Re-export shim: this module moved to ``execution.model_loop.model_runtime``."""

from execution.model_loop.model_runtime import (
    GatewayModelRuntime,
    _to_gateway_request,
    route_configuration_from_gateway,
)

__all__ = [
    "GatewayModelRuntime",
    "_to_gateway_request",
    "route_configuration_from_gateway",
]
