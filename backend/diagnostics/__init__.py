"""Diagnostics: the local store of system events and warnings.

Imports only ``common``; the container wires it to the log sink handler.
"""

from diagnostics.token import read_token

__all__ = ["read_token"]
