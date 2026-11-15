"""Unit tests for agent URL helpers and agent-card validation."""

import pytest

from agent.card_validation import validate_agent_card_data
from agent.url_utils import normalize_agent_url

# =============================================================================
# URL Normalization Tests
# =============================================================================


class TestNormalizeAgentUrl:
    """Tests for normalize_agent_url function."""

    def test_removes_well_known_path(self):
        """Should remove .well-known paths."""
        url = "https://agent.example.com/.well-known/agent.json"
        result = normalize_agent_url(url)
        assert "/.well-known" not in result
        assert result == "https://agent.example.com"

    def test_removes_agent_card_path(self):
        """Should remove agent-card.json path."""
        url = "https://agent.example.com/.well-known/agent-card.json"
        result = normalize_agent_url(url)
        assert "agent-card.json" not in result

    def test_normalizes_localhost_aliases(self):
        """Should normalize localhost aliases to 'localhost'."""
        urls = [
            "http://127.0.0.1:8000/agent",
            "http://0.0.0.0:8000/agent",
            "http://[::1]:8000/agent",  # IPv6 addresses need brackets
        ]
        for url in urls:
            result = normalize_agent_url(url)
            assert "localhost" in result

    def test_removes_default_ports(self):
        """Should remove default ports (80 for http, 443 for https)."""
        assert ":443" not in normalize_agent_url("https://agent.example.com:443/path")
        assert ":80" not in normalize_agent_url("http://agent.example.com:80/path")

    def test_preserves_non_default_ports(self):
        """Should preserve non-default ports."""
        result = normalize_agent_url("https://agent.example.com:8443/path")
        assert ":8443" in result

    def test_removes_trailing_slash(self):
        """Should remove trailing slashes."""
        result = normalize_agent_url("https://agent.example.com/path/")
        assert not result.endswith("/")

    def test_lowercases_hostname(self):
        """Should lowercase the hostname."""
        result = normalize_agent_url("https://AGENT.EXAMPLE.COM/path")
        assert "agent.example.com" in result

    def test_preserves_query_string(self):
        """Should preserve query strings."""
        result = normalize_agent_url("https://agent.example.com/path?key=value")
        assert "key=value" in result

    def test_handles_empty_url(self):
        """Should handle empty URL gracefully."""
        assert normalize_agent_url("") == ""
        assert normalize_agent_url(None) is None

    def test_handles_invalid_url(self):
        """Should return invalid URL as-is."""
        invalid_url = "not-a-valid-url"
        result = normalize_agent_url(invalid_url)
        assert result == invalid_url


# =============================================================================
# Agent Card Validation Tests
# =============================================================================


class TestValidateAgentCard:
    """Tests for validate_agent_card_data."""

    @pytest.mark.asyncio
    async def test_validates_complete_card(self):
        """Should return no errors for valid card."""
        valid_card = {
            "name": "Test Agent",
            "description": "A test agent",
            "url": "https://agent.example.com",
            "version": "1.0.0",
            "capabilities": {"streaming": True},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [{"id": "skill-1", "name": "Test Skill"}],
        }

        errors = await validate_agent_card_data(valid_card)
        assert errors == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing_field",
        [
            "name",
            "description",
            "url",
            "version",
            "capabilities",
            "defaultInputModes",
            "defaultOutputModes",
            "skills",
        ],
    )
    async def test_detects_each_missing_required_field(self, missing_field):
        """Should detect each individual missing required field."""
        complete_card = {
            "name": "Test Agent",
            "description": "A test agent",
            "url": "https://agent.example.com",
            "version": "1.0.0",
            "capabilities": {"streaming": True},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [{"id": "skill-1", "name": "Test Skill"}],
        }
        del complete_card[missing_field]

        errors = await validate_agent_card_data(complete_card)
        assert len(errors) >= 1
        assert any(missing_field in e for e in errors)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid_url",
        [
            "not-a-valid-url",
            "ftp://agent.example.com",
            "agent.example.com",
            "",
        ],
    )
    async def test_validates_url_format(self, invalid_url):
        """Should reject URLs that don't start with http:// or https://."""
        card = {
            "name": "Test Agent",
            "description": "A test agent",
            "url": invalid_url,
            "version": "1.0.0",
            "capabilities": {},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [{"id": "s1", "name": "Skill"}],
        }

        errors = await validate_agent_card_data(card)
        assert any("url" in e.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_validates_url_type(self):
        """Should reject explicit null/non-string URLs instead of raising."""
        card = {
            "name": "Test Agent",
            "description": "A test agent",
            "url": None,
            "version": "1.0.0",
            "capabilities": {},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [{"id": "s1", "name": "Skill"}],
        }

        errors = await validate_agent_card_data(card)

        assert "Field 'url' must be a string." in errors

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_capabilities",
        [
            "not-an-object",
            ["a", "list"],
            42,
            True,
        ],
    )
    async def test_validates_capabilities_type(self, bad_capabilities):
        """Should reject non-dict capabilities."""
        card = {
            "name": "Test Agent",
            "description": "A test agent",
            "url": "https://agent.example.com",
            "version": "1.0.0",
            "capabilities": bad_capabilities,
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [{"id": "s1", "name": "Skill"}],
        }

        errors = await validate_agent_card_data(card)
        assert any("capabilities" in e.lower() for e in errors)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["defaultInputModes", "defaultOutputModes"])
    async def test_validates_mode_fields_must_be_list(self, field):
        """Should reject non-list mode fields."""
        card = {
            "name": "Test Agent",
            "description": "A test agent",
            "url": "https://agent.example.com",
            "version": "1.0.0",
            "capabilities": {},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [{"id": "s1", "name": "Skill"}],
        }
        card[field] = "not-a-list"

        errors = await validate_agent_card_data(card)
        assert any(field in e for e in errors)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["defaultInputModes", "defaultOutputModes"])
    async def test_validates_mode_fields_items_must_be_strings(self, field):
        """Should reject mode field items that are not strings."""
        card = {
            "name": "Test Agent",
            "description": "A test agent",
            "url": "https://agent.example.com",
            "version": "1.0.0",
            "capabilities": {},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [{"id": "s1", "name": "Skill"}],
        }
        card[field] = [123, True]

        errors = await validate_agent_card_data(card)
        assert any(field in e for e in errors)

    @pytest.mark.asyncio
    async def test_validates_skills_must_be_list(self):
        """Should reject non-list skills."""
        card = {
            "name": "Test Agent",
            "description": "A test agent",
            "url": "https://agent.example.com",
            "version": "1.0.0",
            "capabilities": {},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": "not-a-list",
        }

        errors = await validate_agent_card_data(card)
        assert any("skills" in e.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_validates_empty_skills_array(self):
        """Should warn when skills array is empty."""
        card = {
            "name": "Test Agent",
            "description": "A test agent",
            "url": "https://agent.example.com",
            "version": "1.0.0",
            "capabilities": {},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [],
        }

        errors = await validate_agent_card_data(card)
        assert any("skills" in e.lower() and "empty" in e.lower() for e in errors)
