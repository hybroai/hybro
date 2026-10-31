"""
Clerk Authentication for FastAPI
Validates JWT tokens from Clerk and provides user authentication.
"""

from functools import lru_cache

from clerk_backend_api import Clerk, authenticate_request
from clerk_backend_api.security.types import AuthenticateRequestOptions
from fastapi import HTTPException, Request, status

from common.observability import get_logger

logger = get_logger(__name__)

clerk_secret_key: str | None = None
clerk_authorized_parties: tuple[str, ...] = (
    "https://hybro.ai",
    "http://localhost:3000",
)


def bind_auth_config(
    *,
    clerk_secret_key_value: str,
    authorized_parties: tuple[str, ...] | None = None,
) -> None:
    global clerk_secret_key, clerk_authorized_parties

    clerk_secret_key = clerk_secret_key_value
    if authorized_parties is not None:
        clerk_authorized_parties = tuple(authorized_parties)


def _require_clerk_secret_key() -> str:
    if clerk_secret_key is None:
        raise RuntimeError("Auth configuration has not been bound")
    return clerk_secret_key


def _get_clerk_client() -> Clerk:
    return Clerk(bearer_auth=_require_clerk_secret_key())


@lru_cache(maxsize=256)
def _cached_clerk_user_name(user_id: str) -> str | None:
    """Fetch and cache user display name from Clerk."""
    try:
        client = _get_clerk_client()
        user = client.users.get(user_id=user_id)
        if not user:
            return None
        parts = [user.first_name, user.last_name]
        full = " ".join(p for p in parts if p)
        return full or user.username or None
    except Exception as exc:
        logger.warning(
            "clerk_user_name_resolution_failed",
            extra={"user_id": user_id, "error_type": type(exc).__name__},
        )
        return None


def resolve_provider_name(provider_id: str | None) -> str | None:
    """Resolve a Clerk user ID to a display name (cached)."""
    if not provider_id:
        return None
    return _cached_clerk_user_name(provider_id)


class ClerkUser:
    """Represents an authenticated Clerk user"""

    def __init__(self, user_id: str, session_id: str, claims: dict):
        self.user_id = user_id
        self.session_id = session_id
        self.claims = claims
        self.email = claims.get("email")
        self.username = claims.get("username")


async def verify_clerk_token_from_request(request: Request) -> ClerkUser:
    """
    Verify a Clerk JWT token from a FastAPI Request using Clerk SDK.
    The SDK will automatically extract and verify the token from the request.

    Args:
        request: The FastAPI Request object

    Returns:
        ClerkUser object with user information

    Raises:
        HTTPException: If token is invalid or verification fails
    """
    try:
        # Use Clerk SDK to authenticate the request
        # The SDK handles JWKS fetching, caching, and JWT verification automatically
        # Create authentication options with secret key and authorized parties
        options = AuthenticateRequestOptions(
            secret_key=_require_clerk_secret_key(),
            authorized_parties=clerk_authorized_parties,
        )
        request_state = authenticate_request(request, options)

        if not request_state.is_signed_in:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Extract user information from the verified token payload
        payload = request_state.payload
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing payload",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user_id = payload.get("sub")  # Subject claim contains user ID
        session_id = payload.get("sid")  # Session ID claim

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing user ID",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return ClerkUser(user_id=user_id, session_id=session_id, claims=payload)

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "clerk_token_verification_failed",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication verification failed",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_user(
    request: Request,
) -> ClerkUser:
    """
    FastAPI dependency to get the current authenticated user.
    Uses Clerk SDK for token verification.

    Usage:
        @app.get("/protected")
        async def protected_route(user: ClerkUser = Depends(get_current_user)):
            return {"user_id": user.user_id}
    """
    # The Clerk SDK will verify the token from the request automatically
    return await verify_clerk_token_from_request(request)


async def get_optional_user(
    request: Request,
) -> ClerkUser | None:
    """
    FastAPI dependency for optional authentication.
    Returns None if no token is provided, otherwise validates the token using Clerk SDK.

    Usage:
        @app.get("/optional-auth")
        async def optional_route(user: Optional[ClerkUser] = Depends(get_optional_user)):
            if user:
                return {"message": f"Hello {user.user_id}"}
            return {"message": "Hello guest"}
    """
    # Try to verify token from request, return None if no token provided
    try:
        return await verify_clerk_token_from_request(request)
    except HTTPException:
        return None


async def get_current_user_with_query_token(
    request: Request,
) -> ClerkUser:
    """
    FastAPI dependency for authentication that supports both:
    - Query parameter token (for SSE/EventSource which can't send custom headers) - checked first
    - Authorization header (fallback) - verified by Clerk SDK

    Usage:
        @app.get("/sse/stream")
        async def sse_stream(user: ClerkUser = Depends(get_current_user_with_query_token)):
            return StreamingResponse(...)
    """
    # Check for query parameter token first (primary for SSE)
    token = request.query_params.get("token")
    if token:
        # For query parameter tokens, we need to create a modified request
        # with the token in the Authorization header for the SDK to verify

        # Build headers list from original request, replacing/adding authorization
        # ASGI headers must be lowercase bytes tuples
        original_headers = list(request.scope.get("headers", []))

        # Remove any existing authorization header (case-insensitive)
        filtered_headers = [
            (k, v) for k, v in original_headers if k.lower() != b"authorization"
        ]

        # Add the new authorization header
        filtered_headers.append((b"authorization", f"Bearer {token}".encode()))

        # Create a modified request scope
        modified_scope = dict(request.scope)
        modified_scope["headers"] = filtered_headers

        # Create new request with modified headers
        modified_request = Request(scope=modified_scope)

        return await verify_clerk_token_from_request(modified_request)

    # Fall back to Authorization header (standard auth)
    return await verify_clerk_token_from_request(request)
