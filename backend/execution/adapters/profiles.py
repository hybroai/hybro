"""Re-export shim: this module moved to ``execution.model_loop.profiles``."""

from execution.model_loop.profiles import (
    BASE_ORCHESTRATOR_SYSTEM_PROMPT,
    DEFAULT_ORCHESTRATOR_PROMPTS,
    FAST_ORCHESTRATOR_SYSTEM_PROMPT,
    ULTIMATE_ORCHESTRATOR_SYSTEM_PROMPT,
    OrchestratorProfileResolutionError,
    OrchestratorProfileResolver,
    ProfileId,
    PromptAssetRegistry,
)

__all__ = [
    "BASE_ORCHESTRATOR_SYSTEM_PROMPT",
    "DEFAULT_ORCHESTRATOR_PROMPTS",
    "FAST_ORCHESTRATOR_SYSTEM_PROMPT",
    "ULTIMATE_ORCHESTRATOR_SYSTEM_PROMPT",
    "OrchestratorProfileResolutionError",
    "OrchestratorProfileResolver",
    "ProfileId",
    "PromptAssetRegistry",
]
