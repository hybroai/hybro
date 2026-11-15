class PlatformRouteError(Exception):
    def __init__(self, status_code: int, detail) -> None:
        self.status_code = status_code
        self.detail = detail
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
        super().__init__(message)


class FileStoragePlatformError(PlatformRouteError):
    pass


class RetryableFileStoragePlatformError(FileStoragePlatformError):
    """A transient file backend failure safe to retry with identical bytes."""


__all__ = [
    "FileStoragePlatformError",
    "RetryableFileStoragePlatformError",
    "PlatformRouteError",
]
