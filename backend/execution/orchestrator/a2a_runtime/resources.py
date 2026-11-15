"""Frozen resource selection and ownership validation."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256

from ..models import AgentToolInput, RunResourceManifestSnapshot
from .errors import RecoverableResourceError
from .models import (
    AgentToolBindingRecord,
    DurableResourceProjection,
    FrozenCallResourceManifest,
    FrozenCallResourceRef,
    MaterializedResourcePart,
)

_OWNED_ROOM_FILE_URL_PATTERN = re.compile(r"^/api/v1/files/([a-zA-Z0-9_-]+)/content$")


OutboundLoader = Callable[
    [FrozenCallResourceRef, list[str], datetime],
    Awaitable[MaterializedResourcePart],
]
InboundArtifactWriter = Callable[[object, str, str], Awaitable[str]]


class ResourceSelectionError(ValueError):
    pass


class BoundedResourceMaterializer:
    """Concrete adapter around owner loaders; it never dereferences paths or URIs."""

    def __init__(
        self,
        *,
        outbound_loader: OutboundLoader,
        inbound_writer: InboundArtifactWriter,
        max_outbound_count: int = 20,
        max_outbound_bytes: int = 25 * 1024 * 1024,
        max_inbound_count: int = 20,
        max_outbound_encoded_bytes: int = 34 * 1024 * 1024,
        max_inbound_encoded_bytes: int = 34 * 1024 * 1024,
        allow_guarded_remote_artifact_refs: bool = False,
        verify_room_file_ownership: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.outbound_loader = outbound_loader
        self.inbound_writer = inbound_writer
        self.max_outbound_count = max_outbound_count
        self.max_outbound_bytes = max_outbound_bytes
        self.max_inbound_count = max_inbound_count
        self.max_outbound_encoded_bytes = max_outbound_encoded_bytes
        self.max_inbound_encoded_bytes = max_inbound_encoded_bytes
        self.allow_guarded_remote_artifact_refs = allow_guarded_remote_artifact_refs
        self.verify_room_file_ownership = verify_room_file_ownership

    async def materialize(
        self,
        manifest: FrozenCallResourceManifest,
        *,
        conversation_id: str,
        room_epoch: int,
        allowed_input_modes: list[str],
        deadline_at: datetime,
    ) -> list[MaterializedResourcePart]:
        if len(manifest.refs) > self.max_outbound_count:
            raise ResourceSelectionError("outbound resource count exceeds limit")
        if sum(ref.size_bytes for ref in manifest.refs) > self.max_outbound_bytes:
            raise ResourceSelectionError("outbound resource bytes exceed limit")
        parts: list[MaterializedResourcePart] = []
        for ref in manifest.refs:
            if ref.conversation_id != conversation_id or ref.room_epoch != room_epoch:
                raise ResourceSelectionError("resource Room ownership changed")
            if datetime.now(UTC) >= deadline_at:
                raise TimeoutError("resource materialization deadline exceeded")
            try:
                part = await self.outbound_loader(ref, allowed_input_modes, deadline_at)
            except (ConnectionError, TimeoutError) as exc:
                raise RecoverableResourceError(
                    "resource owner is temporarily unavailable"
                ) from exc
            parts.append(part)
            if (
                sum(_encoded_part_size(item) for item in parts)
                > self.max_outbound_encoded_bytes
            ):
                raise ResourceSelectionError("outbound encoded bytes exceed limit")
        verify_materialized_digests(manifest, parts)
        return parts

    async def materialize_inbound_artifacts(
        self,
        *,
        call: object,
        artifact_refs: list[str],
        observation_id: str,
    ) -> list[str]:
        if len(artifact_refs) > self.max_inbound_count:
            raise ResourceSelectionError("inbound artifact count exceeds limit")
        if (
            sum(len(ref.encode()) for ref in artifact_refs)
            > self.max_inbound_encoded_bytes
        ):
            raise ResourceSelectionError("inbound encoded bytes exceed limit")
        durable = []
        for artifact_ref in artifact_refs:
            if isinstance(artifact_ref, str):
                match = _OWNED_ROOM_FILE_URL_PATTERN.match(artifact_ref)
                if match:
                    if self.verify_room_file_ownership is None:
                        raise ResourceSelectionError(
                            "owned room file refs require room ownership verification"
                        )
                    await self.verify_room_file_ownership(
                        getattr(call, "conversation_id", ""), match.group(1)
                    )
                    durable.append(artifact_ref)
                    continue
            if not artifact_ref or artifact_ref.startswith("/"):
                raise ResourceSelectionError("raw path artifact refs are forbidden")
            if "://" in artifact_ref and not self.allow_guarded_remote_artifact_refs:
                raise ResourceSelectionError(
                    "raw URI/path refs require the guarded owner adapter"
                )
            try:
                durable.append(
                    await self.inbound_writer(call, artifact_ref, observation_id)
                )
            except (ConnectionError, TimeoutError) as exc:
                raise RecoverableResourceError(
                    "artifact owner is temporarily unavailable"
                ) from exc
        return list(dict.fromkeys(durable))


class InMemoryDurableResourceProjectionStore:
    def __init__(self) -> None:
        self._records: dict[str, DurableResourceProjection] = {}

    async def insert(self, projection: DurableResourceProjection) -> str:
        existing = self._records.get(projection.projection_id)
        if existing is not None:
            return "replayed" if existing == projection else "conflict"
        self._records[projection.projection_id] = (
            DurableResourceProjection.model_validate(
                projection.model_dump(mode="python")
            )
        )
        return "accepted"

    async def load(self, projection_id: str) -> DurableResourceProjection | None:
        value = self._records.get(projection_id)
        return (
            DurableResourceProjection.model_validate(value.model_dump(mode="python"))
            if value is not None
            else None
        )


class DurableProjectionResourceLoader:
    """Load a frozen projection or deterministically regenerate and persist it."""

    def __init__(
        self,
        *,
        projection_store: InMemoryDurableResourceProjectionStore,
        regenerate: OutboundLoader,
    ) -> None:
        self.projection_store = projection_store
        self.regenerate = regenerate

    async def __call__(
        self,
        ref: FrozenCallResourceRef,
        allowed_input_modes: list[str],
        deadline_at: datetime,
    ) -> MaterializedResourcePart:
        if ref.projection_id is None:
            return await self.regenerate(ref, allowed_input_modes, deadline_at)
        persisted = await self.projection_store.load(ref.projection_id)
        if persisted is not None:
            self._validate(ref, persisted)
            return persisted.materialized
        part = await self.regenerate(ref, allowed_input_modes, deadline_at)
        projection = DurableResourceProjection(
            projection_id=ref.projection_id,
            source_ref_id=ref.ref_id,
            source_content_digest=ref.content_digest,
            materialized=part,
            created_at=datetime.now(UTC),
        )
        self._validate(ref, projection)
        outcome = await self.projection_store.insert(projection)
        if outcome not in {"accepted", "replayed"}:
            raise ResourceSelectionError("durable projection identity conflict")
        replay = await self.projection_store.load(ref.projection_id)
        if replay is None:
            raise ResourceSelectionError("durable projection disappeared")
        self._validate(ref, replay)
        return replay.materialized

    @staticmethod
    def _validate(
        ref: FrozenCallResourceRef, projection: DurableResourceProjection
    ) -> None:
        if (
            projection.projection_id != ref.projection_id
            or projection.source_ref_id != ref.ref_id
            or projection.source_content_digest != ref.content_digest
            or projection.materialized.ref_id != ref.ref_id
            or projection.materialized.content_digest
            != (ref.materialization_digest or ref.content_digest)
        ):
            raise ResourceSelectionError(
                "durable projection does not match frozen source"
            )


def resource_is_compatible(
    *, kind: str, mime_type: str | None, input_modes: list[str]
) -> bool:
    """Whether an agent accepts a resource of ``kind``/``mime_type``.

    Single source of truth for resource-to-agent compatibility, shared by the
    catalog assembler (schema enum) and ``freeze_call_manifest`` (authorization)
    so the model can never be offered a ref that would later be denied.
    """
    modes = {mode.lower() for mode in input_modes}
    if kind == "context":
        return "text" in modes or "text/plain" in modes
    if "file" in modes or "*/*" in modes:
        return True
    if mime_type is None:
        return False
    normalized = mime_type.lower()
    major = normalized.split("/", 1)[0] + "/*" if "/" in normalized else normalized
    return normalized in modes or major in modes


def freeze_call_manifest(
    *,
    arguments: dict[str, object],
    run_manifest: RunResourceManifestSnapshot | None,
    binding: AgentToolBindingRecord,
    source_conversation_id: str,
    source_room_epoch: int,
    root_context_source_message_id: str | None = None,
) -> FrozenCallResourceManifest:
    """Freeze the exact authorized resources for one Agent call.

    The root user message is durable evidence, whereas ``task`` is a
    model-authored instruction. If the model omitted ``context_refs``, the
    preparation boundary implicitly selects only compatible context refs owned
    by that exact root message. This narrow default prevents instruction text
    from being mistaken for user evidence without auto-forwarding arbitrary
    artifacts or attachments. Any non-empty explicit context selection remains
    exact.
    """
    parsed = AgentToolInput.model_validate(arguments)
    inventory_refs = list(run_manifest.refs if run_manifest else [])
    context_refs = list(parsed.context_refs)
    if not context_refs and root_context_source_message_id is not None:
        context_refs = [
            ref.ref_id
            for ref in inventory_refs
            if ref.kind == "context"
            and ref.source_message_id == root_context_source_message_id
            and ref.ref_id in binding.compatible_resource_refs
            and resource_is_compatible(
                kind=ref.kind,
                mime_type=ref.mime_type,
                input_modes=binding.input_modes,
            )
        ]
    selected = (
        [("context", ref) for ref in context_refs]
        + [("artifact", ref) for ref in parsed.artifact_refs]
        + [("attachment", ref) for ref in parsed.attachment_refs]
    )
    if run_manifest is None and selected:
        raise ResourceSelectionError("Run has no prepared resources")
    inventory = {ref.ref_id: ref for ref in inventory_refs}
    frozen: list[FrozenCallResourceRef] = []
    for requested_kind, ref_id in selected:
        prepared = inventory.get(ref_id)
        # Authorization is derived live from the run manifest + the bound
        # agent's input modes rather than a run-start pre-filtered allowlist,
        # so artifacts produced mid-run and registered into the manifest are
        # authorizable without re-freezing the binding.
        if prepared is None or not resource_is_compatible(
            kind=prepared.kind,
            mime_type=prepared.mime_type,
            input_modes=binding.input_modes,
        ):
            raise ResourceSelectionError(f"resource ref {ref_id!r} is not allowed")
        if prepared.kind != requested_kind:
            raise ResourceSelectionError(f"resource ref {ref_id!r} has wrong kind")
        frozen.append(
            FrozenCallResourceRef(
                ref_id=ref_id,
                kind=requested_kind,
                conversation_id=source_conversation_id,
                room_epoch=source_room_epoch,
                source_message_id=prepared.source_message_id,
                mime_type=prepared.mime_type,
                size_bytes=prepared.size_bytes,
                content_digest=prepared.content_digest,
            )
        )
    digest = _digest_json([_frozen_ref_digest_payload(ref) for ref in frozen])
    return FrozenCallResourceManifest(
        manifest_id=f"call-resources-{digest}", refs=frozen, content_digest=digest
    )


def verify_materialized_digests(
    manifest: FrozenCallResourceManifest,
    materialized: list[object],
) -> None:
    expected = {
        ref.ref_id: ref.materialization_digest or ref.content_digest
        for ref in manifest.refs
    }
    actual = {
        getattr(item, "ref_id", None): getattr(item, "content_digest", None)
        for item in materialized
    }
    if expected != actual:
        raise ResourceSelectionError(
            "materialized resources changed from frozen manifest"
        )


def _encoded_part_size(part: MaterializedResourcePart) -> int:
    payload = part.payload
    if isinstance(payload, str):
        return len(payload.encode())
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def _frozen_ref_digest_payload(ref: FrozenCallResourceRef) -> dict[str, object]:
    """Canonical digest input for one frozen ref.

    The manifest digest is persisted on the Agent-call record and recomputed for
    acceptance replay, so keys (including the stored "room_id") are literals
    independent of attribute names.
    """

    return {
        "ref_id": ref.ref_id,
        "kind": ref.kind,
        "room_id": ref.conversation_id,
        "room_epoch": ref.room_epoch,
        "source_message_id": ref.source_message_id,
        "source_agent_id": ref.source_agent_id,
        "mime_type": ref.mime_type,
        "size_bytes": ref.size_bytes,
        "content_digest": ref.content_digest,
        "projection_id": ref.projection_id,
        "materialization_digest": ref.materialization_digest,
    }


def _digest_json(value: object) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(canonical.encode()).hexdigest()
