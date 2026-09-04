"""Internal control outcomes shared by the Kernel and transport runtimes."""


class ClientCancellationRequested(RuntimeError):
    """A durable Run cancellation interrupted local external work."""


__all__ = ["ClientCancellationRequested"]
