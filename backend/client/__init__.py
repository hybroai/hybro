"""Clients: each program that asks for work, and its token.

Imports only ``common``. ``api_gateway`` and ``session`` reach it through
``client.protocols``; the container builds the service.
"""

from client.repository import MongoClientRepository
from client.service import ClientService

__all__ = ["ClientService", "MongoClientRepository"]
