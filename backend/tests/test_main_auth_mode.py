from common.auth import (
    ClerkUser,
    get_current_user,
    get_current_user_with_query_token,
    get_optional_user,
)
from main import app, settings


def test_mock_auth_overrides_all_user_auth_dependencies():
    assert settings.auth_mode == "mock"

    auth_dependencies = (
        get_current_user,
        get_current_user_with_query_token,
        get_optional_user,
    )
    overrides = [
        app.dependency_overrides[dependency] for dependency in auth_dependencies
    ]

    assert len(set(overrides)) == 1
    user = overrides[0]()
    assert isinstance(user, ClerkUser)
    assert user.user_id == "user_local_developer"
