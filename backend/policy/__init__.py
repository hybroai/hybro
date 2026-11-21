"""Policy: the owner's check rules, and the answer they give for an agent call.

Imports only ``common``. It never calls another module and never waits.
``api_gateway`` reaches it through ``policy.protocols``; the container builds
the service and hands it to ``execution``, which asks ``check_agent_call``
before it accepts a new Agent call.
"""

from policy.evaluate import RuleDecision, evaluate
from policy.repository import MongoRuleRepository
from policy.service import PolicyService

__all__ = ["MongoRuleRepository", "PolicyService", "RuleDecision", "evaluate"]
