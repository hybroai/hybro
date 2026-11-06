"""Diagnostics: the local store of system events and warnings, and its API.

Imports only ``common``; the container wires it to the log sink handler and
builds the snapshot sources over its runtime objects.
"""

from diagnostics.service import DiagnosticsService, SnapshotSources
from diagnostics.token import read_token

__all__ = ["DiagnosticsService", "SnapshotSources", "read_token"]
