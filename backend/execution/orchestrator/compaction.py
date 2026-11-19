"""Re-export shim: this module moved to ``execution.model_loop.compaction``."""

from execution.model_loop.compaction import (
    CompactionError,
    DeterministicFakeCompactor,
    ModelBackedCompactor,
)

__all__ = [
    "CompactionError",
    "DeterministicFakeCompactor",
    "ModelBackedCompactor",
]
