"""The user a route acts for.

The owner's sign-in cookie is resolved in ``api_gateway.owner_session``; this
module keeps only the type every route receives. ``ClerkUser`` keeps its name
until Clerk mode is removed.
"""


class ClerkUser:
    """The signed-in owner, as the routes see them."""

    def __init__(self, user_id: str, session_id: str, claims: dict):
        self.user_id = user_id
        self.session_id = session_id
        self.claims = claims
        self.email = claims.get("email")
        self.username = claims.get("username")
