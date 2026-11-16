"""Production adapters for the orchestrator A2A runtime.

These adapters implement the orchestrator's provider-neutral ports
(``execution.orchestrator.a2a_runtime.ports``) over existing product services.
They are allowed to import orchestrator contracts; the reverse direction
(orchestrator → adapters) is forbidden by the architecture gates.
"""

from .agent_candidates import AgentServiceCandidateSource
from .authorization import MembershipAuthorizationRefresh
from .hitl import (
    DurableHITLApplicationPort,
    HITLApplicationStore,
    StoredHITLInteraction,
)
from .resources import RoomFilesResourceMaterializer

__all__ = [
    "AgentServiceCandidateSource",
    "DurableHITLApplicationPort",
    "HITLApplicationStore",
    "MembershipAuthorizationRefresh",
    "RoomFilesResourceMaterializer",
    "StoredHITLInteraction",
]
