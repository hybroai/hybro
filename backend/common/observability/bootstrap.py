"""Application-shell logging bootstrap.

Import this module before runtime modules that may emit warnings or logs.
"""

from common.observability.logging import configure_logging

# Loading settings can log (removed config fields), so install the structured
# handler with defaults first, then apply the configured level and format.
configure_logging()

from common.config.loader import settings  # noqa: E402

configure_logging(settings)

__all__ = ["settings"]
