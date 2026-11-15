"""Fail-closed validators for persisted HITL aggregates and effect journals."""

from __future__ import annotations

import hashlib
from typing import Any

from common.dto.hitl import (
    HITLApplicationRoute,
    HITLEvidenceOrigin,
    HITLPublicSource,
    HITLRouteSnapshot,
)
from models.hitl import (
    HITLInteraction,
    HITLRequest,
    HITLSupervisorEffectCommand,
)


class HITLAggregateCorruptionError(ValueError):
    """Persisted HITL state does not prove a safe application target."""


def _without_mongo_id(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "_id"}


def validate_route_classifications(
    interaction: dict[str, Any],
) -> HITLRouteSnapshot:
    """Validate the complete aggregate and its independent route classes."""

    try:
        validated = HITLInteraction.model_validate(_without_mongo_id(interaction))
        application_route = HITLApplicationRoute(validated.application_route)
        HITLPublicSource(validated.public_source)
        HITLEvidenceOrigin(validated.evidence_origin)
        snapshot = HITLRouteSnapshot.model_validate(validated.route_snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        raise HITLAggregateCorruptionError(
            f"interaction route classifications are invalid: {exc}"
        ) from exc
    if snapshot.route != application_route:
        raise HITLAggregateCorruptionError(
            "application_route does not match route snapshot"
        )
    if interaction.get("route_fingerprint") != snapshot.fingerprint:
        raise HITLAggregateCorruptionError("route snapshot fingerprint mismatch")
    if interaction.get("orchestration_run_id") != snapshot.orchestration_run_id:
        raise HITLAggregateCorruptionError(
            "supervisor orchestration_run_id does not match route snapshot"
        )
    return snapshot


def validate_exact_member_inventory(  # noqa: C901
    interaction: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    """Require an exact, ordered, consistently classified member inventory."""

    validate_route_classifications(interaction)
    expected_ids = interaction.get("request_ids")
    expected_count = interaction.get("expected_request_count")
    inventory = interaction.get("creation_inventory")
    inventory_ids = (
        [item.get("request_id") for item in inventory]
        if isinstance(inventory, list)
        else None
    )
    if (
        not isinstance(expected_ids, list)
        or not expected_ids
        or len(set(expected_ids)) != len(expected_ids)
        or expected_count != len(expected_ids)
        or interaction.get("required_request_ids") != expected_ids
        or inventory_ids != expected_ids
    ):
        raise HITLAggregateCorruptionError("interaction member inventory is invalid")
    if [row.get("request_id") for row in rows] != expected_ids:
        raise HITLAggregateCorruptionError("interaction member order mismatch")
    immutable_inventory_fields = (
        "request_id",
        "prompt",
        "prompt_type",
        "choices",
        "agent_id",
        "agent_name",
        "source_step_id",
        "continuation_message_id",
        "display_message_id",
    )
    for index, row in enumerate(rows):
        try:
            HITLRequest.model_validate(_without_mongo_id(row))
        except (TypeError, ValueError) as exc:
            raise HITLAggregateCorruptionError(
                "interaction member schema is invalid"
            ) from exc
        inventory_member = inventory[index]
        for field in immutable_inventory_fields:
            inventory_value = getattr(
                inventory_member.get(field), "value", inventory_member.get(field)
            )
            row_value = getattr(row.get(field), "value", row.get(field))
            if row_value != inventory_value:
                raise HITLAggregateCorruptionError(
                    f"interaction member immutable {field} mismatch"
                )
        if row.get("interaction_id") != interaction.get("interaction_id"):
            raise HITLAggregateCorruptionError("interaction member identity mismatch")
        if row.get("question_index") != index:
            raise HITLAggregateCorruptionError("interaction question_index mismatch")
        if row.get("question_count") != expected_count:
            raise HITLAggregateCorruptionError("interaction question_count mismatch")
        for field in (
            "room_id",
            "user_message_id",
            "orchestration_run_id",
            "application_route",
            "public_source",
            "evidence_origin",
        ):
            interaction_value = getattr(
                interaction.get(field), "value", interaction.get(field)
            )
            row_value = getattr(row.get(field), "value", row.get(field))
            if row_value != interaction_value:
                raise HITLAggregateCorruptionError(
                    f"interaction member {field} mismatch"
                )


def validate_command_route_consistency(
    interaction: dict[str, Any], command: dict[str, Any]
) -> None:
    """Require a durable command to target exactly the canonical route snapshot."""

    snapshot = validate_route_classifications(interaction)
    if command.get("kind") != "supervisor_resume":
        raise HITLAggregateCorruptionError(
            "command kind does not match supervisor route"
        )
    try:
        HITLSupervisorEffectCommand.model_validate(_without_mongo_id(command))
    except (TypeError, ValueError) as exc:
        raise HITLAggregateCorruptionError("command schema is invalid") from exc
    if command.get("interaction_id") != interaction.get("interaction_id"):
        raise HITLAggregateCorruptionError("command interaction target mismatch")
    if command.get("answer_request_ids") != interaction.get("request_ids"):
        raise HITLAggregateCorruptionError("command member inventory mismatch")
    if command.get("orchestration_run_id") != snapshot.orchestration_run_id:
        raise HITLAggregateCorruptionError("supervisor command target mismatch")


def deterministic_request_id(interaction_id: str, question_index: int) -> str:
    digest = hashlib.sha256(
        f"{interaction_id}:question:{question_index}".encode()
    ).hexdigest()
    return f"hitl-request-{digest}"
