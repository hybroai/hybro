"""Install synthetic JSON before application imports during pytest collection.

This is configuration isolation, not permission to run live/network test lanes.
"""

import json
import os
import tempfile
from pathlib import Path

_directory = tempfile.TemporaryDirectory(
    prefix="hybro-test-config-", dir=os.environ.get("TMPDIR")
)
home = Path(_directory.name)
os.environ["HYBRO_HOME"] = str(home)
for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(key, None)
(home / "config.json").write_text(
    json.dumps(
        {
            "version": 1,
            "provider": {"id": "openai", "auth": "api_key"},
            "models": {"text": "gpt-4o-mini"},
            "backend": {"auth_mode": "mock"},
        }
    )
)
(home / "auth.json").write_text(
    json.dumps(
        {
            "provider": "openai",
            "auth": "api_key",
            "api_key": "fixture-not-a-provider-key",
            "services": {
                "default_agent_llm_token": "fixture-inference-" * 4,
            },
        }
    )
)
(home / "auth.json").chmod(0o600)
