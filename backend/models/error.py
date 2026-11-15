from typing import Any

from pydantic import BaseModel


class Error(BaseModel):
    status_code: int
    error_code: str
    error_message: str
    error_data: Any | None = None


# Fix: Create proper exception classes
class AgentNotFoundError(Exception):
    def __init__(
        self,
        status_code: int = 400,
        error_code: str = "Agent Not Found",
        error_message: str = "Agent not found",
    ):
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(self.error_message)
