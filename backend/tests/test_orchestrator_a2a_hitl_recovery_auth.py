from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from common.dto.hitl import A2AInteractionSpec, HITLQuestionAnswer
from execution.orchestrator.a2a_runtime.errors import (
    AmbiguousRemoteEffectError,
    RecoverableAdapterError,
    RecoverableCheckpointError,
)
from execution.orchestrator.a2a_runtime.hitl import (
    A2AContinuationCoordinator,
    InMemoryHITLApplicationPort,
)
from execution.orchestrator.a2a_runtime.in_memory import (
    InMemoryAgentCallLedgerStore,
    InMemoryAgentToolBindingStore,
    InMemoryObservationConflictStore,
    InMemoryObservationInboxStore,
    InMemoryRoomEpochStore,
)
from execution.orchestrator.a2a_runtime.ingress import (
    A2AObservationIngress,
    RejectExternalIngressAuthenticator,
)
from execution.orchestrator.a2a_runtime.ledger import (
    apply_observation,
    ownership_alias_keys,
    transition_call,
)
from execution.orchestrator.a2a_runtime.models import (
    A2ADispatchReceipt,
    NormalizedA2AObservation,
)
from execution.orchestrator.a2a_runtime.recovery import A2AContinuationRecoveryService

from ._orchestrator_a2a_helpers import binding, ledger_record
from ._orchestrator_helpers import NOW


class Authorization:
    def __init__(self, *outcomes: str):
        self.outcomes = list(outcomes) or ["authorized"]
        self.calls = 0

    async def authorize(self, **kwargs):
        del kwargs
        index = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        return self.outcomes[index]


class Dispatch:
    def __init__(self, *, terminal_status=None):
        self.commands = []
        self.inspections = []
        self.terminal_status = terminal_status
        self.interaction_receipt: A2ADispatchReceipt | None = None

    async def continue_task(self, command):
        self.commands.append(command)
        if self.interaction_receipt is not None:
            return self.interaction_receipt
        if self.terminal_status is not None:
            return A2ADispatchReceipt(
                outcome="terminal",
                terminal_observation=NormalizedA2AObservation(
                    observation_id=f"receipt-{self.terminal_status}",
                    call_record_id=command.call_record_id,
                    source_kind="direct",
                    source_identity=f"receipt:{self.terminal_status}",
                    binding_scope="endpoint",
                    event_kind="terminal",
                    observed_at=NOW,
                    task_id=command.task_id,
                    context_id=command.context_id,
                    status=self.terminal_status,
                ),
            )
        return A2ADispatchReceipt(outcome="accepted")

    async def inspect_continuation(self, command):
        self.inspections.append(command)
        if self.interaction_receipt is not None:
            return self.interaction_receipt
        return A2ADispatchReceipt(outcome="accepted")


class AmbiguousThenRetryDispatch(Dispatch):
    def __init__(self):
        super().__init__()
        self._fail_continue = True

    async def continue_task(self, command):
        self.commands.append(command)
        if self._fail_continue:
            self._fail_continue = False
            raise AmbiguousRemoteEffectError(
                "continuation acknowledgement is ambiguous"
            )
        return A2ADispatchReceipt(outcome="accepted")


class RepeatingTerminalDispatch(Dispatch):
    async def inspect_continuation(self, command):
        self.inspections.append(command)
        assert self.terminal_status is not None
        return A2ADispatchReceipt(
            outcome="terminal",
            terminal_observation=NormalizedA2AObservation(
                observation_id=f"inspection-{self.terminal_status}",
                call_record_id=command.call_record_id,
                source_kind="direct",
                source_identity=f"receipt:{self.terminal_status}",
                binding_scope="endpoint",
                event_kind="terminal",
                observed_at=NOW,
                task_id=command.task_id,
                context_id=command.context_id,
                status=self.terminal_status,
            ),
        )


class AmbiguousContinuationDispatch(Dispatch):
    async def continue_task(self, command):
        self.commands.append(command)
        raise AmbiguousRemoteEffectError("continuation acknowledgement is ambiguous")


class TrustedAuthReferences:
    def __init__(self):
        self.records = {}
        self.uses = {}

    def issue(
        self,
        reference,
        *,
        answerer_id="user-1",
        call_record_id,
        binding_id="binding-run-1",
        binding_digest="binding-digest-run-1",
        conversation_id="room-1",
        room_epoch=1,
        expires_at=None,
        **challenge_identity,
    ):
        self.records[reference] = {
            "answerer_id": answerer_id,
            "call_record_id": call_record_id,
            "binding_id": binding_id,
            "binding_digest": binding_digest,
            "room_id": conversation_id,
            "room_epoch": room_epoch,
            "expires_at": expires_at or datetime.now(UTC) + timedelta(minutes=5),
            **challenge_identity,
        }

    async def verify(self, authorization_reference, **context):
        record = self.records.get(authorization_reference)
        if record is None:
            raise PermissionError("forged authorization reference")
        if record["expires_at"] <= datetime.now(UTC):
            raise PermissionError("expired authorization reference")
        expected = {
            "answerer_id": context["authenticated_answerer_id"],
            "call_record_id": context["call_record_id"],
            "binding_id": context["binding_id"],
            "binding_digest": context["binding_digest"],
            "room_id": context["conversation_id"],
            "room_epoch": context["room_epoch"],
        }
        expected.update(
            interaction_id=context["interaction_id"],
            interaction_revision=context["interaction_revision"],
            route_fingerprint=context["route_fingerprint"],
            interaction_fingerprint=context["interaction_fingerprint"],
            question_id=context["question_id"],
            challenge_digest=context["challenge_digest"],
            answer_digest=context["answer_digest"],
        )
        if any(
            record.get(key) != value for key, value in expected.items() if key in record
        ):
            raise PermissionError(
                "cross-call or cross-challenge authorization reference"
            )
        use_identity = tuple(sorted(expected.items()))
        prior = self.uses.get(authorization_reference)
        if prior is not None and prior != use_identity:
            raise PermissionError("unsafe authorization reference replay")
        self.uses.setdefault(authorization_reference, use_identity)
        return sha256(f"{authorization_reference}:{use_identity}".encode()).hexdigest()


class CrashAfterAnswerHITL(InMemoryHITLApplicationPort):
    def __init__(self):
        super().__init__()
        self.crash_once = True

    async def answer(self, **kwargs):
        result = await super().answer(**kwargs)
        if self.crash_once:
            self.crash_once = False
            raise OSError("crash after durable HITL answer")
        return result


class ContinuationWinnerRaceLedger(InMemoryAgentCallLedgerStore):
    def __init__(
        self,
        *,
        terminal_status,
        branch,
        reported_outcome,
        winner_visibility="visible",
    ):
        super().__init__()
        self.terminal_status = terminal_status
        self.branch = branch
        self.reported_outcome = reported_outcome
        self.winner_visibility = winner_visibility
        self.hide_next_winner = False
        self.raced = False
        self.durable_winner = None

    async def cas(self, record, *, expected_state_version):
        target_state = "working" if self.branch == "working" else self.terminal_status
        if (
            not self.raced
            and record.continuation_state == "accepted"
            and record.state == target_state
        ):
            current = await self.load_by_record_id(record.call_record_id)
            assert current is not None
            winner = apply_observation(
                current,
                NormalizedA2AObservation(
                    observation_id=f"race-{self.branch}-{self.terminal_status}",
                    call_record_id=current.call_record_id,
                    source_kind="direct",
                    source_identity=f"race:{self.branch}:{self.terminal_status}",
                    binding_scope=current.endpoint_scope_digest,
                    event_kind="terminal",
                    observed_at=NOW,
                    task_id=current.a2a_task_id,
                    context_id=current.a2a_context_id,
                    status=self.terminal_status,
                ),
                recent_limit=current.runtime_policy.recent_observation_id_limit,
            )
            assert (
                await super().cas(winner, expected_state_version=current.state_version)
                == "accepted"
            )
            self.raced = True
            self.durable_winner = winner
            self.hide_next_winner = self.winner_visibility != "visible"
            if self.reported_outcome == "error":
                return "error"
        return await super().cas(record, expected_state_version=expected_state_version)

    async def load_by_record_id(self, call_record_id):
        if self.hide_next_winner:
            self.hide_next_winner = False
            if self.winner_visibility == "missing":
                return None
            raise RecoverableCheckpointError("winner temporarily unavailable")
        return await super().load_by_record_id(call_record_id)


class AckLossObservationRecorder:
    def __init__(self, delegate):
        self.delegate = delegate
        self.fail_once = True

    async def record(self, observation):
        result = await self.delegate.record(observation)
        if self.fail_once and observation.source_identity.startswith(
            "continuation-expired:"
        ):
            self.fail_once = False
            raise RecoverableCheckpointError("observation acknowledgement lost")
        return result

    async def mark_ledger_applied(self, observation_id):
        await self.delegate.mark_ledger_applied(observation_id)

    async def mark_executor_outcome(self, observation_id, *, outcome_digest):
        await self.delegate.mark_executor_outcome(
            observation_id, outcome_digest=outcome_digest
        )


class FinalizerFaultHITL(InMemoryHITLApplicationPort):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.failed = False

    async def abandon(self, interaction_id, **kwargs):
        if not self.failed:
            self.failed = True
            if self.mode == "absent":
                self._interactions.pop(interaction_id, None)
                self._eligible_interactions.discard(interaction_id)
                return "absent"
            if self.mode == "conflict":
                return "conflict"
            if self.mode == "error":
                return "error"
            if self.mode == "outage":
                raise RecoverableCheckpointError("HITL owner unavailable")
            outcome = await super().abandon(interaction_id, **kwargs)
            assert outcome == "accepted"
            if self.mode == "replayed":
                return "replayed"
            raise RecoverableCheckpointError("HITL close acknowledgement lost")
        return await super().abandon(interaction_id, **kwargs)


class ProofTamperingHITL(InMemoryHITLApplicationPort):
    def __init__(self):
        super().__init__()
        self.tamper_proof = False

    async def read_answer_record(self, interaction_id, interaction_revision):
        record = await super().read_answer_record(interaction_id, interaction_revision)
        if (
            not self.tamper_proof
            or record is None
            or not record.verified_auth_references
        ):
            return record
        changed = record.verified_auth_references[0].model_copy(
            update={"proof_digest": "changed-proof-digest"}
        )
        return record.model_copy(update={"verified_auth_references": [changed]})


class FaultLedger(InMemoryAgentCallLedgerStore):
    def __init__(self, *, fail_resuming_cas=False, fail_renew_after_write=False):
        super().__init__()
        self.fail_resuming_cas = fail_resuming_cas
        self.fail_renew_after_write = fail_renew_after_write

    async def cas(self, record, *, expected_state_version):
        if self.fail_resuming_cas and record.state == "resuming":
            self.fail_resuming_cas = False
            raise OSError("crash before continuation command CAS")
        return await super().cas(record, expected_state_version=expected_state_version)

    async def renew(self, *args, **kwargs):
        result = await super().renew(*args, **kwargs)
        if self.fail_renew_after_write and result is not None:
            self.fail_renew_after_write = False
            raise OSError("crash after authorization and durable renew")
        return result


def questionnaire_spec(interaction_id="interaction-1"):
    return A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": interaction_id,
            "questions": [
                {
                    "question_id": "q1",
                    "interaction_kind": "questionnaire",
                    "prompt": "Choose",
                    "answer_kind": "single_choice",
                    "choices": ["a", "b"],
                }
            ],
        }
    )


def auth_spec(interaction_id="auth-interaction"):
    return A2AInteractionSpec.model_validate(
        {
            "schema_version": 1,
            "interaction_id": interaction_id,
            "questions": [
                {
                    "question_id": "auth-q",
                    "interaction_kind": "auth_challenge",
                    "prompt": "Authenticate",
                    "answer_kind": "authorization_result",
                }
            ],
        }
    )


def questionnaire_answers():
    return [
        HITLQuestionAnswer.model_validate(
            {
                "question_id": "q1",
                "answer": {"kind": "single_choice", "choice": "a"},
            }
        )
    ]


def _digest_json(value):
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(canonical.encode()).hexdigest()


def auth_reference_identity(call, route, reference, *, answers=None):
    answers = answers or auth_answers(reference)
    answer_digest = _digest_json([answer.model_dump(mode="json") for answer in answers])
    challenge_digest = _digest_json(
        {
            "interaction_id": call.pending_interaction_id,
            "interaction_revision": call.interaction_revision,
            "route_fingerprint": route.fingerprint,
            "interaction_fingerprint": call.interaction_fingerprint,
            "question_id": "auth-q",
        }
    )
    return {
        "interaction_id": call.pending_interaction_id,
        "interaction_revision": call.interaction_revision,
        "route_fingerprint": route.fingerprint,
        "interaction_fingerprint": call.interaction_fingerprint,
        "question_id": "auth-q",
        "challenge_digest": challenge_digest,
        "answer_digest": answer_digest,
    }


def auth_answers(reference):
    return [
        HITLQuestionAnswer.model_validate(
            {
                "question_id": "auth-q",
                "answer": {
                    "kind": "authorization_result",
                    "authorization_reference": reference,
                },
            }
        )
    ]


async def setup_waiting(
    *,
    auth_required=False,
    ledger=None,
    hitl=None,
    dispatch=None,
    authorization=None,
):
    ledger = ledger or InMemoryAgentCallLedgerStore()
    hitl = hitl or InMemoryHITLApplicationPort()
    call = ledger_record()
    call = transition_call(call, to_state="ready_to_dispatch", updated_at=NOW)
    call = transition_call(call, to_state="dispatching", updated_at=NOW)
    aliases = []
    from execution.orchestrator.a2a_runtime.models import A2AOwnershipAlias

    aliases.append(
        A2AOwnershipAlias(kind="task", value="task-1", binding_scope="endpoint")
    )
    call = transition_call(
        call,
        to_state="working",
        updated_at=NOW,
        a2a_task_id="task-1",
        a2a_context_id="context-1",
        ownership_aliases=aliases,
        ownership_alias_keys=ownership_alias_keys(aliases),
    )
    call = transition_call(call, to_state="continuation_pending", updated_at=NOW)
    spec = auth_spec() if auth_required else questionnaire_spec()
    fingerprint = _digest_json(spec.model_dump(mode="json"))
    call = transition_call(
        call,
        to_state="auth_required" if auth_required else "input_required",
        updated_at=NOW,
        pending_interaction_id=spec.interaction_id,
        interaction_revision=1,
        interaction_fingerprint=fingerprint,
    )
    await ledger.insert(call)
    await hitl.create_or_replay(
        call=call, interaction=spec, interaction_fingerprint=fingerprint
    )
    _, route, _ = hitl.read_interaction_for_test(spec.interaction_id)
    bindings = InMemoryAgentToolBindingStore()
    await bindings.insert(binding())
    epochs = InMemoryRoomEpochStore()
    await epochs.activate("room-1", "create-1", activated_at=NOW)
    observations = A2AObservationIngress(
        inbox=InMemoryObservationInboxStore(),
        conflicts=InMemoryObservationConflictStore(),
        ledger=ledger,
        authenticator=RejectExternalIngressAuthenticator(),
    )
    auth_refs = TrustedAuthReferences()
    dispatch = dispatch or Dispatch()
    coordinator = A2AContinuationCoordinator(
        ledger=ledger,
        bindings=bindings,
        hitl=hitl,
        room_epochs=epochs,
        authorization=authorization or Authorization(),
        auth_references=auth_refs,
        dispatch=dispatch,
        observations=observations,
    )
    return coordinator, ledger, hitl, auth_refs, dispatch, call, route


async def test_in_memory_hitl_answer_requires_exact_revision_and_excludes_cancel():
    _coordinator, _ledger, hitl, _refs, _dispatch, call, route = await setup_waiting()
    assert await hitl.publish("interaction-1", call_record_id=call.call_record_id) == (
        "accepted"
    )

    with pytest.raises(ValueError, match="fingerprint or revision"):
        await hitl.answer(
            interaction_id="interaction-1",
            interaction_revision=2,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )
    assert [
        interaction.interaction_id
        for interaction, _route, _fingerprint in await hitl.get_published_interactions(
            call.conversation_id
        )
    ] == ["interaction-1"]

    await hitl.answer(
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
        verified_auth_reference_digests=[],
        verified_auth_references=[],
    )
    assert (
        await hitl.abandon(
            "interaction-1",
            call_record_id=call.call_record_id,
            reason="user_canceled",
        )
        == "conflict"
    )

    (
        _coordinator,
        _ledger,
        canceled,
        _refs,
        _dispatch,
        call,
        route,
    ) = await setup_waiting()
    assert (
        await canceled.abandon(
            "interaction-1",
            call_record_id=call.call_record_id,
            reason="user_canceled",
        )
        == "accepted"
    )
    with pytest.raises(KeyError):
        await canceled.answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )
    assert await canceled.read_answer_record("interaction-1", 1) is None


async def test_continuation_authorization_denial_terminalizes_without_hot_loop():
    authorization = Authorization("denied")
    coordinator, ledger, _hitl, _refs, dispatch, call, route = await setup_waiting(
        authorization=authorization
    )

    state = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )

    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert state == persisted.state == "failed"
    assert persisted.error_code == "continuation_authorization_denied"
    assert persisted.next_attempt_at is None
    assert persisted.authorization_refresh_attempts == 1
    assert authorization.calls == 1
    assert dispatch.commands == []
    recovery = A2AContinuationRecoveryService(coordinator, ledger)
    assert await recovery.recover_due(due_at=datetime.now(UTC), limit=10) == 0
    assert authorization.calls == 1


async def test_transient_authorization_refresh_uses_bounded_backoff_then_fails():
    authorization = Authorization(
        "transient_failure", "transient_failure", "transient_failure"
    )
    coordinator, ledger, _hitl, _refs, dispatch, call, route = await setup_waiting(
        authorization=authorization
    )
    kwargs = {
        "call_record_id": call.call_record_id,
        "interaction_id": "interaction-1",
        "interaction_revision": 1,
        "route_fingerprint": route.fingerprint,
        "answers": questionnaire_answers(),
        "authenticated_answerer_id": "user-1",
    }

    assert await coordinator.resume(**kwargs) == "delivery_uncertain"
    first = await ledger.load_by_record_id(call.call_record_id)
    assert first.state == "input_required"
    assert first.authorization_refresh_attempts == 1
    assert first.next_attempt_at > datetime.now(UTC)
    assert await coordinator.reconcile_answer(call_record_id=call.call_record_id) == (
        "input_required"
    )
    assert authorization.calls == 1

    for expected_attempt in (2, 3):
        current = await ledger.load_by_record_id(call.call_record_id)
        due = current.model_copy(
            update={
                "next_attempt_at": datetime.now(UTC) - timedelta(seconds=1),
                "state_version": current.state_version + 1,
                "updated_at": datetime.now(UTC),
            }
        )
        assert (
            await ledger.cas(due, expected_state_version=current.state_version)
            == "accepted"
        )
        state = await coordinator.reconcile_answer(call_record_id=call.call_record_id)
        persisted = await ledger.load_by_record_id(call.call_record_id)
        assert persisted.authorization_refresh_attempts == expected_attempt
        if expected_attempt < 3:
            assert state == "delivery_uncertain"
            assert persisted.next_attempt_at > datetime.now(UTC)
        else:
            assert state == persisted.state == "failed"
            assert persisted.error_code == "continuation_authorization_unavailable"
            assert persisted.next_attempt_at is None
    assert authorization.calls == 3
    assert dispatch.commands == []


@pytest.mark.parametrize("branch", ["working", "terminal_marker"])
@pytest.mark.parametrize("reported_outcome", ["conflict", "error"])
@pytest.mark.parametrize(
    "terminal_status", ["completed", "failed", "canceled", "rejected", "expired"]
)
async def test_continuation_delivery_cas_race_returns_durable_terminal_winner(
    branch, reported_outcome, terminal_status
):
    ledger = ContinuationWinnerRaceLedger(
        terminal_status=terminal_status,
        branch=branch,
        reported_outcome=reported_outcome,
    )
    dispatch = Dispatch(
        terminal_status=terminal_status if branch == "terminal_marker" else None
    )
    coordinator, ledger, hitl, _, dispatch, call, route = await setup_waiting(
        ledger=ledger, dispatch=dispatch
    )
    resume_kwargs = {
        "call_record_id": call.call_record_id,
        "interaction_id": "interaction-1",
        "interaction_revision": 1,
        "route_fingerprint": route.fingerprint,
        "answers": questionnaire_answers(),
        "authenticated_answerer_id": "user-1",
    }

    first = await coordinator.resume(**resume_kwargs)
    persisted = await ledger.load_by_record_id(call.call_record_id)
    retry = await coordinator.resume(**resume_kwargs)

    assert ledger.raced is True
    assert persisted is not None
    assert persisted == ledger.durable_winner
    assert persisted.state == terminal_status
    assert first == retry == persisted.state
    assert len(dispatch.commands) == 1
    assert persisted.continuation_command == dispatch.commands[0]
    assert hitl.read_interaction_for_test("interaction-1") is None
    assert await hitl.read_answers("interaction-1", 1) is None
    with pytest.raises(KeyError):
        await hitl.answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )


@pytest.mark.parametrize(
    "terminal_status", ["completed", "failed", "canceled", "rejected", "expired"]
)
async def test_continuation_terminal_receipt_persists_winner_without_redispatch(
    terminal_status,
):
    dispatch = Dispatch(terminal_status=terminal_status)
    coordinator, ledger, hitl, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    resume_kwargs = {
        "call_record_id": call.call_record_id,
        "interaction_id": "interaction-1",
        "interaction_revision": 1,
        "route_fingerprint": route.fingerprint,
        "answers": questionnaire_answers(),
        "authenticated_answerer_id": "user-1",
    }

    assert await coordinator.resume(**resume_kwargs) == terminal_status
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == terminal_status
    assert persisted.terminal_result is not None
    assert persisted.continuation_state == "accepted"
    assert await coordinator.resume(**resume_kwargs) == terminal_status
    assert len(dispatch.commands) == 1
    assert hitl.read_interaction_for_test("interaction-1") is None


async def test_conflicting_terminal_receipt_applies_canonical_evidence():
    dispatch = Dispatch(terminal_status="completed")
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    canonical = NormalizedA2AObservation(
        observation_id="canonical-failed",
        call_record_id=call.call_record_id,
        source_kind="direct",
        source_identity="receipt:completed",
        binding_scope=call.endpoint_scope_digest,
        event_kind="terminal",
        observed_at=NOW,
        task_id=call.a2a_task_id,
        context_id=call.a2a_context_id,
        status="failed",
    )
    assert (await coordinator.observations.record(canonical))[0] == "accepted"

    outcome = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )

    assert outcome == "failed"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "failed"
    assert persisted.terminal_result is not None
    assert persisted.terminal_result.status == "failed"
    assert persisted.continuation_state == "accepted"
    assert persisted.claim_owner is None
    conflicts = await coordinator.observations.conflicts.list_for_source(
        canonical.source_identity
    )
    assert len(conflicts) == 1
    assert conflicts[0].accepted_observation_id == canonical.observation_id
    assert len(dispatch.commands) == 1


async def test_conflicting_terminal_receipt_preserves_uncertainty_budget():
    dispatch = RepeatingTerminalDispatch(terminal_status="completed")
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    canonical = NormalizedA2AObservation(
        observation_id="canonical-failed-artifact",
        call_record_id=call.call_record_id,
        source_kind="direct",
        source_identity="receipt:completed",
        binding_scope=call.endpoint_scope_digest,
        event_kind="terminal",
        observed_at=NOW,
        task_id=call.a2a_task_id,
        context_id=call.a2a_context_id,
        status="failed",
        artifact_refs=["raw-agent-artifact"],
    )
    assert (await coordinator.observations.record(canonical))[0] == "accepted"

    outcome = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )

    assert outcome == "delivery_uncertain"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "delivery_uncertain"
    assert persisted.terminal_result is None
    assert persisted.continuation_attempts == 1
    assert canonical.observation_id not in persisted.recent_observation_ids
    inbox_record = await coordinator.observations.inbox.load(canonical.observation_id)
    assert inbox_record.state == "pending"
    assert inbox_record.observation.artifact_refs == ["raw-agent-artifact"]
    conflicts = await coordinator.observations.conflicts.list_for_source(
        canonical.source_identity
    )
    assert len(conflicts) == 1
    assert conflicts[0].accepted_observation_id == canonical.observation_id
    assert len(dispatch.commands) == 1

    for expected_attempts in (2, 3):
        await make_due(ledger, call.call_record_id)
        assert (
            await coordinator.recover_call(call_record_id=call.call_record_id)
            == "delivery_uncertain"
        )
        persisted = await ledger.load_by_record_id(call.call_record_id)
        assert persisted.continuation_attempts == expected_attempts

    await make_due(ledger, call.call_record_id)
    assert await coordinator.recover_call(call_record_id=call.call_record_id) == (
        "expired"
    )
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "expired"
    assert persisted.terminal_result is not None
    assert persisted.terminal_result.status == "expired"
    assert len(dispatch.inspections) == 2


async def test_conflicting_terminal_receipt_defers_nonterminal_canonical_evidence():
    dispatch = Dispatch(terminal_status="completed")
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    canonical = NormalizedA2AObservation(
        observation_id="canonical-working",
        call_record_id=call.call_record_id,
        source_kind="direct",
        source_identity="receipt:completed",
        binding_scope=call.endpoint_scope_digest,
        event_kind="working",
        observed_at=NOW,
        task_id=call.a2a_task_id,
        context_id=call.a2a_context_id,
    )
    assert (await coordinator.observations.record(canonical))[0] == "accepted"

    outcome = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )

    assert outcome == "delivery_uncertain"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "delivery_uncertain"
    assert persisted.terminal_result is None
    assert persisted.continuation_command is not None
    assert persisted.continuation_state == "delivery_uncertain"
    assert persisted.continuation_attempts == 1
    assert persisted.claim_owner is None
    assert canonical.observation_id not in persisted.recent_observation_ids
    conflicts = await coordinator.observations.conflicts.list_for_source(
        canonical.source_identity
    )
    assert len(conflicts) == 1
    assert conflicts[0].accepted_observation_id == canonical.observation_id
    assert len(dispatch.commands) == 1


async def test_conflicting_terminal_receipt_defers_canonical_hitl_to_processor():
    dispatch = Dispatch(terminal_status="completed")
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    canonical = NormalizedA2AObservation(
        observation_id="canonical-input-required",
        call_record_id=call.call_record_id,
        source_kind="direct",
        source_identity="receipt:completed",
        binding_scope=call.endpoint_scope_digest,
        event_kind="input_required",
        observed_at=NOW,
        task_id=call.a2a_task_id,
        context_id=call.a2a_context_id,
    )
    assert (await coordinator.observations.record(canonical))[0] == "accepted"

    outcome = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )

    assert outcome == "delivery_uncertain"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "delivery_uncertain"
    assert persisted.continuation_command is not None
    assert persisted.continuation_state == "delivery_uncertain"
    assert persisted.continuation_attempts == 1
    assert persisted.answer_applied is not None
    assert canonical.observation_id not in persisted.recent_observation_ids
    inbox_record = await coordinator.observations.inbox.load(canonical.observation_id)
    assert inbox_record.state == "pending"
    conflicts = await coordinator.observations.conflicts.list_for_source(
        canonical.source_identity
    )
    assert len(conflicts) == 1
    assert conflicts[0].accepted_observation_id == canonical.observation_id
    assert len(dispatch.commands) == 1


async def test_continuation_expiry_replays_after_observation_ack_loss():
    dispatch = AmbiguousContinuationDispatch()
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
        )
        == "delivery_uncertain"
    )
    uncertain = await ledger.load_by_record_id(call.call_record_id)
    exhausted = uncertain.model_copy(
        update={
            "continuation_attempts": (
                uncertain.runtime_policy.max_uncertain_inspection_attempts
            ),
            "next_attempt_at": datetime.now(UTC) - timedelta(seconds=1),
            "state_version": uncertain.state_version + 1,
        }
    )
    assert (
        await ledger.cas(exhausted, expected_state_version=uncertain.state_version)
        == "accepted"
    )
    recorder = AckLossObservationRecorder(coordinator.observations)
    coordinator.observations = recorder

    assert (
        await coordinator.recover_call(call_record_id=call.call_record_id)
        == "delivery_uncertain"
    )
    await expire_claim(ledger, call.call_record_id)
    assert await coordinator.recover_call(call_record_id=call.call_record_id) == (
        "expired"
    )

    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "expired"
    assert persisted.terminal_result is not None
    assert len(dispatch.commands) == 1
    conflicts = await recorder.delegate.conflicts.list_for_source(
        f"continuation-expired:{persisted.continuation_command.command_id}"
    )
    assert conflicts == []


async def test_conflicting_continuation_expiry_applies_canonical_evidence():
    dispatch = AmbiguousContinuationDispatch()
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
        )
        == "delivery_uncertain"
    )
    uncertain = await ledger.load_by_record_id(call.call_record_id)
    exhausted = uncertain.model_copy(
        update={
            "continuation_attempts": (
                uncertain.runtime_policy.max_uncertain_inspection_attempts
            ),
            "next_attempt_at": datetime.now(UTC) - timedelta(seconds=1),
            "state_version": uncertain.state_version + 1,
        }
    )
    assert (
        await ledger.cas(exhausted, expected_state_version=uncertain.state_version)
        == "accepted"
    )
    uncertain = exhausted
    command = uncertain.continuation_command
    canonical = NormalizedA2AObservation(
        observation_id="canonical-expiry-failed",
        call_record_id=call.call_record_id,
        source_kind="inspection",
        source_identity=f"continuation-expired:{command.command_id}",
        binding_scope=call.endpoint_scope_digest,
        event_kind="terminal",
        observed_at=NOW,
        task_id=call.a2a_task_id,
        context_id=call.a2a_context_id,
        status="failed",
    )
    assert (await coordinator.observations.record(canonical))[0] == "accepted"

    outcome = await coordinator.recover_call(call_record_id=call.call_record_id)

    assert outcome == "failed"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "failed"
    assert persisted.terminal_result is not None
    assert persisted.terminal_result.status == "failed"
    assert persisted.continuation_state == "accepted"
    assert persisted.claim_owner is None
    assert (
        await coordinator.recover_call(call_record_id=call.call_record_id) == "failed"
    )
    conflicts = await coordinator.observations.conflicts.list_for_source(
        canonical.source_identity
    )
    assert len(conflicts) == 1
    assert conflicts[0].accepted_observation_id == canonical.observation_id
    assert len(dispatch.commands) == 1


async def test_conflicting_continuation_expiry_defers_nonterminal_canonical_evidence():
    dispatch = AmbiguousContinuationDispatch()
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
        )
        == "delivery_uncertain"
    )
    uncertain = await ledger.load_by_record_id(call.call_record_id)
    exhausted = uncertain.model_copy(
        update={
            "continuation_attempts": (
                uncertain.runtime_policy.max_uncertain_inspection_attempts
            ),
            "next_attempt_at": datetime.now(UTC) - timedelta(seconds=1),
            "state_version": uncertain.state_version + 1,
        }
    )
    assert (
        await ledger.cas(exhausted, expected_state_version=uncertain.state_version)
        == "accepted"
    )
    command = exhausted.continuation_command
    canonical = NormalizedA2AObservation(
        observation_id="canonical-expiry-working",
        call_record_id=call.call_record_id,
        source_kind="inspection",
        source_identity=f"continuation-expired:{command.command_id}",
        binding_scope=call.endpoint_scope_digest,
        event_kind="working",
        observed_at=NOW,
        task_id=call.a2a_task_id,
        context_id=call.a2a_context_id,
    )
    assert (await coordinator.observations.record(canonical))[0] == "accepted"

    outcome = await coordinator.recover_call(call_record_id=call.call_record_id)

    assert outcome == "delivery_uncertain"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "delivery_uncertain"
    assert persisted.terminal_result is None
    assert (
        persisted.continuation_attempts
        == persisted.runtime_policy.max_uncertain_inspection_attempts
    )
    assert persisted.claim_owner is None
    assert canonical.observation_id not in persisted.recent_observation_ids
    due = persisted.model_copy(
        update={
            "next_attempt_at": datetime.now(UTC) - timedelta(seconds=1),
            "state_version": persisted.state_version + 1,
        }
    )
    assert (
        await ledger.cas(due, expected_state_version=persisted.state_version)
        == "accepted"
    )
    assert await coordinator.recover_call(call_record_id=call.call_record_id) == (
        "delivery_uncertain"
    )
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert (
        persisted.continuation_attempts
        == persisted.runtime_policy.max_uncertain_inspection_attempts
    )
    assert dispatch.inspections == []
    conflicts = await coordinator.observations.conflicts.list_for_source(
        canonical.source_identity
    )
    assert len(conflicts) == 1
    assert conflicts[0].accepted_observation_id == canonical.observation_id
    assert len(dispatch.commands) == 1


@pytest.mark.parametrize(
    "terminal_status", ["completed", "failed", "canceled", "rejected", "expired"]
)
@pytest.mark.parametrize(
    "close_mode",
    ["accepted", "replayed", "absent", "conflict", "error", "outage", "ack_loss"],
)
async def test_continuation_terminal_return_requires_hitl_finalization(
    terminal_status, close_mode
):
    ledger = ContinuationWinnerRaceLedger(
        terminal_status=terminal_status,
        branch="working",
        reported_outcome="conflict",
    )
    hitl = (
        FinalizerFaultHITL(close_mode)
        if close_mode != "accepted"
        else InMemoryHITLApplicationPort()
    )
    coordinator, ledger, hitl, _, dispatch, call, route = await setup_waiting(
        ledger=ledger, hitl=hitl
    )
    resume_kwargs = {
        "call_record_id": call.call_record_id,
        "interaction_id": "interaction-1",
        "interaction_revision": 1,
        "route_fingerprint": route.fingerprint,
        "answers": questionnaire_answers(),
        "authenticated_answerer_id": "user-1",
    }

    first = await coordinator.resume(**resume_kwargs)
    persisted = await ledger.load_by_record_id(call.call_record_id)
    if close_mode in {"conflict", "error", "outage", "ack_loss"}:
        assert first == "delivery_uncertain"
    else:
        assert first == terminal_status
    retry = await coordinator.resume(**resume_kwargs)

    assert persisted.state == terminal_status
    assert retry == terminal_status
    assert hitl.read_interaction_for_test("interaction-1") is None
    assert await hitl.read_answers("interaction-1", 1) is None
    with pytest.raises(KeyError):
        await hitl.answer(
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
            verified_auth_reference_digests=[],
            verified_auth_references=[],
        )
    assert len(dispatch.commands) == 1


@pytest.mark.parametrize("winner_visibility", ["missing", "unloadable"])
async def test_continuation_unclassifiable_winner_is_typed_recoverable(
    winner_visibility,
):
    ledger = ContinuationWinnerRaceLedger(
        terminal_status="completed",
        branch="working",
        reported_outcome="conflict",
        winner_visibility=winner_visibility,
    )
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        ledger=ledger
    )
    resume_kwargs = {
        "call_record_id": call.call_record_id,
        "interaction_id": "interaction-1",
        "interaction_revision": 1,
        "route_fingerprint": route.fingerprint,
        "answers": questionnaire_answers(),
        "authenticated_answerer_id": "user-1",
    }

    assert await coordinator.resume(**resume_kwargs) == "delivery_uncertain"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted is not None
    assert persisted.state == "completed"
    assert await coordinator.resume(**resume_kwargs) == "completed"
    assert len(dispatch.commands) == 1


async def make_due(ledger, call_record_id):
    current = await ledger.load_by_record_id(call_record_id)
    due = current.model_copy(
        update={
            "next_attempt_at": datetime.now(UTC) - timedelta(seconds=1),
            "state_version": current.state_version + 1,
        }
    )
    assert (
        await ledger.cas(due, expected_state_version=current.state_version)
        == "accepted"
    )


async def expire_claim(ledger, call_record_id):
    current = await ledger.load_by_record_id(call_record_id)
    expired = current.model_copy(
        update={
            "claim_expires_at": datetime.now(UTC) - timedelta(seconds=1),
            "state_version": current.state_version + 1,
        }
    )
    assert (
        await ledger.cas(expired, expected_state_version=current.state_version)
        == "accepted"
    )


async def test_recovery_reconciles_crash_after_answer_before_marker():
    hitl = CrashAfterAnswerHITL()
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(hitl=hitl)
    with pytest.raises(OSError, match="durable HITL answer"):
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
        )
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.answer_applied is None
    recovery = A2AContinuationRecoveryService(coordinator, ledger)
    assert await recovery.recover_due(due_at=datetime.now(UTC)) == 1
    assert (await ledger.load_by_record_id(call.call_record_id)).state == "working"
    assert len(dispatch.commands) == 1


@pytest.mark.parametrize(
    "failure", ["after_claim", "after_authorization", "before_command_cas"]
)
async def test_answer_marker_recovers_all_pre_command_crash_boundaries(failure):
    ledger = FaultLedger(
        fail_resuming_cas=failure == "before_command_cas",
        fail_renew_after_write=failure == "after_authorization",
    )
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        ledger=ledger
    )
    if failure == "after_claim":
        original_verify = coordinator.room_epochs.verify_active
        crashed = False

        async def crash_verify(conversation_id, epoch):
            nonlocal crashed
            if not crashed:
                crashed = True
                raise OSError("crash after claim")
            return await original_verify(conversation_id, epoch)

        coordinator.room_epochs.verify_active = crash_verify
    resume_kwargs = {
        "call_record_id": call.call_record_id,
        "interaction_id": "interaction-1",
        "interaction_revision": 1,
        "route_fingerprint": route.fingerprint,
        "answers": questionnaire_answers(),
        "authenticated_answerer_id": "user-1",
    }
    with pytest.raises(OSError):
        await coordinator.resume(**resume_kwargs)
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.answer_applied is not None
    assert persisted.continuation_command is None
    await expire_claim(ledger, call.call_record_id)
    assert await coordinator.resume(**resume_kwargs) == "working"
    assert len(dispatch.commands) == 1


async def test_multiple_hitl_cycles_persist_distinct_deterministic_commands():
    coordinator, ledger, hitl, _, dispatch, call, route = await setup_waiting()
    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="interaction-1",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
        )
        == "working"
    )
    first_command_id = (
        await ledger.load_by_record_id(call.call_record_id)
    ).continuation_command.command_id
    current = await ledger.load_by_record_id(call.call_record_id)
    pending = transition_call(
        current,
        to_state="continuation_pending",
        updated_at=NOW,
        pending_interaction_id=None,
        interaction_revision=None,
        interaction_fingerprint=None,
        answer_applied=None,
        continuation_command=None,
        continuation_state=None,
        continuation_attempts=0,
        next_attempt_at=None,
    )
    second_spec = questionnaire_spec("interaction-2")
    assert (
        await ledger.cas(pending, expected_state_version=current.state_version)
        == "accepted"
    )
    required = transition_call(
        pending,
        to_state="input_required",
        updated_at=NOW,
        pending_interaction_id="interaction-2",
        interaction_revision=1,
        interaction_fingerprint="second-fingerprint",
    )
    assert (
        await ledger.cas(required, expected_state_version=pending.state_version)
        == "accepted"
    )
    await hitl.create_or_replay(
        call=required,
        interaction=second_spec,
        interaction_fingerprint="second-fingerprint",
    )
    _, second_route, _ = hitl.read_interaction_for_test("interaction-2")
    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="interaction-2",
            interaction_revision=1,
            route_fingerprint=second_route.fingerprint,
            answers=questionnaire_answers(),
            authenticated_answerer_id="user-1",
        )
        == "working"
    )
    second_command_id = (
        await ledger.load_by_record_id(call.call_record_id)
    ).continuation_command.command_id
    assert first_command_id != second_command_id
    assert len(dispatch.commands) == 2


async def test_continuation_interaction_receipt_parks_typed_second_round():
    second_spec = questionnaire_spec("interaction-2")
    dispatch = Dispatch()
    coordinator, ledger, hitl, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    current = await ledger.load_by_record_id(call.call_record_id)
    dispatch.interaction_receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id=current.a2a_task_id,
        context_id=current.a2a_context_id,
        interaction_observation=NormalizedA2AObservation(
            observation_id="obs-second-round",
            call_record_id=current.call_record_id,
            source_kind="direct",
            source_identity="direct:endpoint:task-1:input_required:second",
            binding_scope=current.endpoint_scope_digest,
            event_kind="input_required",
            observed_at=NOW,
            task_id=current.a2a_task_id,
            context_id=current.a2a_context_id,
            agent_id=current.agent_id,
            content=[],
            artifact_refs=[],
            interaction_spec=second_spec.model_dump(mode="json"),
        ),
    )

    state = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )

    assert state == "input_required"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "input_required"
    assert persisted.pending_interaction_id == "interaction-2"
    assert persisted.interaction_revision == 1
    assert persisted.continuation_command is None
    stored = hitl.read_interaction_for_test("interaction-2")
    assert stored is not None
    assert stored[2] == persisted.interaction_fingerprint
    assert await hitl.get_published_interactions(call.conversation_id) == []
    assert hitl.read_interaction_for_test("interaction-1") is None
    assert len(dispatch.commands) == 1
    first_command = dispatch.commands[0]

    # The second answer uses the interaction produced by the real first
    # continuation receipt; no ledger fields are manually cleared or replaced.
    dispatch.interaction_receipt = None
    second_route = stored[1]
    second_state = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-2",
        interaction_revision=1,
        route_fingerprint=second_route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )
    final = await ledger.load_by_record_id(call.call_record_id)
    assert second_state == final.state == "working"
    assert final.continuation_command is not None
    assert final.continuation_command.command_id != first_command.command_id
    assert len(dispatch.commands) == 2
    assert {command.task_id for command in dispatch.commands} == {"task-1"}
    assert {command.context_id for command in dispatch.commands} == {"context-1"}


async def test_inspect_same_answered_interaction_never_repeats_continuation_send():
    spec = questionnaire_spec()
    dispatch = AmbiguousThenRetryDispatch()
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    current = await ledger.load_by_record_id(call.call_record_id)
    dispatch.interaction_receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id=current.a2a_task_id,
        context_id=current.a2a_context_id,
        interaction_observation=NormalizedA2AObservation(
            observation_id="obs-same-challenge",
            call_record_id=current.call_record_id,
            source_kind="inspection",
            source_identity="inspection:endpoint:task-1:input_required:",
            binding_scope=current.endpoint_scope_digest,
            event_kind="input_required",
            observed_at=NOW,
            task_id=current.a2a_task_id,
            context_id=current.a2a_context_id,
            agent_id=current.agent_id,
            content=[],
            artifact_refs=[],
            interaction_spec=spec.model_dump(mode="json"),
        ),
    )

    first = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )
    assert first == "delivery_uncertain"

    await make_due(ledger, call.call_record_id)
    recovered = await coordinator.recover_call(call_record_id=call.call_record_id)

    assert recovered == "delivery_uncertain"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "delivery_uncertain"
    assert persisted.pending_interaction_id == "interaction-1"
    assert len(dispatch.inspections) == 1
    assert len(dispatch.commands) == 1


async def test_inspect_missing_interaction_spec_never_repeats_continuation_send():
    """Cleared status.message stays uncertain without repeating remote effects."""
    dispatch = AmbiguousThenRetryDispatch()
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    current = await ledger.load_by_record_id(call.call_record_id)
    dispatch.interaction_receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id=current.a2a_task_id,
        context_id=current.a2a_context_id,
        interaction_observation=NormalizedA2AObservation(
            observation_id="obs-cleared-message",
            call_record_id=current.call_record_id,
            source_kind="inspection",
            source_identity="inspection:endpoint:task-1:input_required:",
            binding_scope=current.endpoint_scope_digest,
            event_kind="input_required",
            observed_at=NOW,
            task_id=current.a2a_task_id,
            context_id=current.a2a_context_id,
            agent_id=current.agent_id,
            content=[{"kind": "text", "text": "How many days will you stay in NYC?"}],
            artifact_refs=[],
            interaction_spec=None,
        ),
    )

    first = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )
    assert first == "delivery_uncertain"

    await make_due(ledger, call.call_record_id)
    recovered = await coordinator.recover_call(call_record_id=call.call_record_id)

    assert recovered == "delivery_uncertain"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "delivery_uncertain"
    assert persisted.terminal_result is None
    assert len(dispatch.inspections) == 1
    assert len(dispatch.commands) == 1


async def test_continuation_missing_spec_does_not_untyped_complete():
    """Direct continue that returns input_required without a typed spec must not
    finalize the call as a completed tool result (kernel would narrate the ask)."""
    dispatch = Dispatch()
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    current = await ledger.load_by_record_id(call.call_record_id)
    dispatch.interaction_receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id=current.a2a_task_id,
        context_id=current.a2a_context_id,
        interaction_observation=NormalizedA2AObservation(
            observation_id="obs-no-spec-continue",
            call_record_id=current.call_record_id,
            source_kind="direct",
            source_identity="direct:endpoint:task-1:input_required:nospec",
            binding_scope=current.endpoint_scope_digest,
            event_kind="input_required",
            observed_at=NOW,
            task_id=current.a2a_task_id,
            context_id=current.a2a_context_id,
            agent_id=current.agent_id,
            content=[{"kind": "text", "text": "How many days will you stay in NYC?"}],
            artifact_refs=[],
            interaction_spec=None,
        ),
    )

    state = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )

    assert state == "delivery_uncertain"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "delivery_uncertain"
    assert persisted.terminal_result is None
    assert persisted.continuation_command is not None


async def test_continuation_same_answered_challenge_fails_as_no_progress():
    """A synchronous repeated challenge proves Agent no-progress, not uncertainty."""
    spec = questionnaire_spec()
    dispatch = Dispatch()
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    current = await ledger.load_by_record_id(call.call_record_id)
    dispatch.interaction_receipt = A2ADispatchReceipt(
        outcome="interaction",
        task_id=current.a2a_task_id,
        context_id=current.a2a_context_id,
        interaction_observation=NormalizedA2AObservation(
            observation_id="obs-stale-same-challenge",
            call_record_id=current.call_record_id,
            source_kind="direct",
            source_identity="direct:endpoint:task-1:input_required:stale",
            binding_scope=current.endpoint_scope_digest,
            event_kind="input_required",
            observed_at=NOW,
            task_id=current.a2a_task_id,
            context_id=current.a2a_context_id,
            agent_id=current.agent_id,
            content=[],
            artifact_refs=[],
            interaction_spec=spec.model_dump(mode="json"),
        ),
    )

    state = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )

    assert state == "failed"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "failed"
    assert persisted.pending_interaction_id == "interaction-1"
    assert persisted.terminal_result is not None
    assert persisted.terminal_result.error_code == "agent_interaction_no_progress"
    assert persisted.continuation_state == "accepted"
    assert len(dispatch.commands) == 1


async def test_accepted_continuation_still_working_during_inspect_reschedules():
    """Healthy working polls must not expire via the inspection budget."""
    dispatch = Dispatch()
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting(
        dispatch=dispatch
    )
    resume_kwargs = {
        "call_record_id": call.call_record_id,
        "interaction_id": "interaction-1",
        "interaction_revision": 1,
        "route_fingerprint": route.fingerprint,
        "answers": questionnaire_answers(),
        "authenticated_answerer_id": "user-1",
    }

    assert await coordinator.resume(**resume_kwargs) == "working"

    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.state == "working"
    assert persisted.continuation_state == "accepted"
    max_attempts = persisted.runtime_policy.max_uncertain_inspection_attempts

    # Poll past the uncertainty budget; healthy accepted receipts must reset it.
    for _ in range(max_attempts + 2):
        await make_due(ledger, call.call_record_id)
        recovered = await coordinator.recover_call(call_record_id=call.call_record_id)
        assert recovered == "working"
        current = await ledger.load_by_record_id(call.call_record_id)
        assert current.state == "working"
        assert current.continuation_state == "accepted"
        assert current.continuation_attempts == 0

    final = await ledger.load_by_record_id(call.call_record_id)
    assert final.state == "working"
    assert len(dispatch.inspections) == max_attempts + 2


async def test_authref_is_bound_expiring_and_replay_safe_before_answer_persistence():
    coordinator, ledger, hitl, refs, dispatch, call, route = await setup_waiting(
        auth_required=True
    )
    with pytest.raises(PermissionError, match="forged"):
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="auth-interaction",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=auth_answers("authref:forged"),
            authenticated_answerer_id="user-1",
        )
    assert await hitl.read_answer_record("auth-interaction", 1) is None

    refs.issue(
        "authref:expired",
        call_record_id=call.call_record_id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(PermissionError, match="expired"):
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="auth-interaction",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=auth_answers("authref:expired"),
            authenticated_answerer_id="user-1",
        )

    refs.issue("authref:cross", call_record_id="other-call")
    with pytest.raises(PermissionError, match="cross-call"):
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="auth-interaction",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=auth_answers("authref:cross"),
            authenticated_answerer_id="user-1",
        )

    refs.issue(
        "authref:valid",
        call_record_id=call.call_record_id,
        **auth_reference_identity(call, route, "authref:valid"),
    )
    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="auth-interaction",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=auth_answers("authref:valid"),
            authenticated_answerer_id="user-1",
        )
        == "working"
    )
    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="auth-interaction",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=auth_answers("authref:valid"),
            authenticated_answerer_id="user-1",
        )
        == "working"
    )
    assert len(dispatch.commands) == 1
    with pytest.raises(PermissionError, match="changed challenge identity"):
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="auth-interaction",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=auth_answers("authref:valid"),
            authenticated_answerer_id="user-2",
        )
    with pytest.raises(PermissionError, match="changed challenge identity"):
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="auth-interaction",
            interaction_revision=1,
            route_fingerprint="changed-route",
            answers=auth_answers("authref:valid"),
            authenticated_answerer_id="user-1",
        )
    with pytest.raises(PermissionError, match="changed challenge identity"):
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="auth-interaction",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=auth_answers("authref:changed-answer"),
            authenticated_answerer_id="user-1",
        )
    assert len(dispatch.commands) == 1
    answer_record = await hitl.read_answer_record("auth-interaction", 1)
    assert len(answer_record.verified_auth_reference_digests) == 1
    binding = answer_record.verified_auth_references[0]
    assert binding.interaction_id == "auth-interaction"
    assert binding.interaction_revision == 1
    assert binding.route_fingerprint == route.fingerprint
    assert binding.interaction_fingerprint == _digest_json(
        auth_spec().model_dump(mode="json")
    )
    assert binding.question_id == "auth-q"
    assert binding.answer_digest == answer_record.answer_digest
    exact_identity = auth_reference_identity(call, route, "authref:valid")
    exact_proof = await refs.verify(
        "authref:valid",
        authenticated_answerer_id="user-1",
        call_record_id=call.call_record_id,
        binding_id=call.binding_id,
        binding_digest=call.binding_digest,
        conversation_id=call.conversation_id,
        room_epoch=call.room_epoch,
        **exact_identity,
    )
    assert (
        await refs.verify(
            "authref:valid",
            authenticated_answerer_id="user-1",
            call_record_id=call.call_record_id,
            binding_id=call.binding_id,
            binding_digest=call.binding_digest,
            conversation_id=call.conversation_id,
            room_epoch=call.room_epoch,
            **exact_identity,
        )
        == exact_proof
    )
    for changed in (
        {"interaction_id": "other-interaction"},
        {"question_id": "other-question"},
        {"answer_digest": "changed-answer"},
    ):
        unsafe = exact_identity | changed
        with pytest.raises(PermissionError, match="cross-challenge|unsafe"):
            await refs.verify(
                "authref:valid",
                authenticated_answerer_id="user-1",
                call_record_id=call.call_record_id,
                binding_id=call.binding_id,
                binding_digest=call.binding_digest,
                conversation_id=call.conversation_id,
                room_epoch=call.room_epoch,
                **unsafe,
            )
    with pytest.raises(PermissionError, match="cross-call|cross-challenge|unsafe"):
        await refs.verify(
            "authref:valid",
            authenticated_answerer_id="user-1",
            call_record_id="other-call",
            binding_id=call.binding_id,
            binding_digest=call.binding_digest,
            conversation_id=call.conversation_id,
            room_epoch=call.room_epoch,
            **auth_reference_identity(call, route, "authref:valid"),
        )


async def test_exact_auth_retry_rejects_changed_durable_proof():
    hitl = ProofTamperingHITL()
    coordinator, _, _, refs, dispatch, call, route = await setup_waiting(
        auth_required=True, hitl=hitl
    )
    reference = "authref:proof-tamper"
    refs.issue(
        reference,
        call_record_id=call.call_record_id,
        **auth_reference_identity(call, route, reference),
    )
    resume_kwargs = {
        "call_record_id": call.call_record_id,
        "interaction_id": "auth-interaction",
        "interaction_revision": 1,
        "route_fingerprint": route.fingerprint,
        "answers": auth_answers(reference),
        "authenticated_answerer_id": "user-1",
    }
    assert await coordinator.resume(**resume_kwargs) == "working"

    hitl.tamper_proof = True
    with pytest.raises(PermissionError, match="applied HITL marker changed"):
        await coordinator.resume(**resume_kwargs)
    assert len(dispatch.commands) == 1


async def test_same_call_later_auth_challenge_cannot_reuse_consumed_reference():
    coordinator, ledger, hitl, refs, dispatch, call, route = await setup_waiting(
        auth_required=True
    )
    reference = "authref:one-challenge"
    refs.issue(
        reference,
        call_record_id=call.call_record_id,
        **auth_reference_identity(call, route, reference),
    )
    assert (
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id="auth-interaction",
            interaction_revision=1,
            route_fingerprint=route.fingerprint,
            answers=auth_answers(reference),
            authenticated_answerer_id="user-1",
        )
        == "working"
    )
    current = await ledger.load_by_record_id(call.call_record_id)
    pending = transition_call(
        current,
        to_state="continuation_pending",
        updated_at=NOW,
        pending_interaction_id=None,
        interaction_revision=None,
        interaction_fingerprint=None,
        answer_applied=None,
        continuation_command=None,
        continuation_state=None,
        continuation_attempts=0,
        next_attempt_at=None,
    )
    assert await ledger.cas(pending, expected_state_version=current.state_version) == (
        "accepted"
    )
    second_spec = auth_spec("auth-interaction-2")
    required = transition_call(
        pending,
        to_state="auth_required",
        updated_at=NOW,
        pending_interaction_id=second_spec.interaction_id,
        interaction_revision=1,
        interaction_fingerprint="second-auth-fingerprint",
    )
    assert await ledger.cas(required, expected_state_version=pending.state_version) == (
        "accepted"
    )
    await hitl.create_or_replay(
        call=required,
        interaction=second_spec,
        interaction_fingerprint="second-auth-fingerprint",
    )
    _, second_route, _ = hitl.read_interaction_for_test(second_spec.interaction_id)
    with pytest.raises(PermissionError, match="cross-challenge|consumed|unsafe"):
        await coordinator.resume(
            call_record_id=call.call_record_id,
            interaction_id=second_spec.interaction_id,
            interaction_revision=1,
            route_fingerprint=second_route.fingerprint,
            answers=auth_answers(reference),
            authenticated_answerer_id="user-1",
        )
    assert await hitl.read_answer_record(second_spec.interaction_id, 1) is None
    assert len(dispatch.commands) == 1


async def _schedule_call(ledger, call_record_id, next_attempt_at):
    """Simulate call recovery releasing a parked call into a retry window."""
    current = await ledger.load_by_record_id(call_record_id)
    scheduled = current.model_copy(
        update={
            "next_attempt_at": next_attempt_at,
            "state_version": current.state_version + 1,
        }
    )
    assert (
        await ledger.cas(scheduled, expected_state_version=current.state_version)
        == "accepted"
    )


async def test_owner_answer_in_a_recovery_retry_window_dispatches_now():
    """A parked call scheduled for its next poll must not refuse the answer.

    Call recovery releases parked calls with a short retry delay. An owner
    answer that lands inside that window used to lose the claim and return
    the parked state without dispatching the continuation.
    """
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting()
    await _schedule_call(
        ledger, call.call_record_id, datetime.now(UTC) + timedelta(seconds=30)
    )

    state = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )

    assert state == "working"
    assert len(dispatch.commands) == 1


async def test_recovery_dispatches_an_applied_answer_left_without_a_command():
    """An answer recorded while another worker held the claim is recovered."""
    coordinator, ledger, _, _, dispatch, call, route = await setup_waiting()
    current = await ledger.load_by_record_id(call.call_record_id)
    held = current.model_copy(
        update={
            "claim_owner": "other-worker",
            "claim_expires_at": datetime.now(UTC) + timedelta(seconds=30),
            "state_version": current.state_version + 1,
        }
    )
    assert (
        await ledger.cas(held, expected_state_version=current.state_version)
        == "accepted"
    )

    state = await coordinator.resume(
        call_record_id=call.call_record_id,
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
    )
    assert state == "input_required"
    persisted = await ledger.load_by_record_id(call.call_record_id)
    assert persisted.answer_applied is not None
    assert persisted.continuation_command is None
    assert dispatch.commands == []

    await expire_claim(ledger, call.call_record_id)
    recovery = A2AContinuationRecoveryService(coordinator, ledger)
    assert await recovery.recover_due(due_at=datetime.now(UTC)) == 1
    assert (await ledger.load_by_record_id(call.call_record_id)).state == "working"
    assert len(dispatch.commands) == 1


async def test_answer_for_closed_interaction_stops_reconciling(caplog):
    coordinator, ledger, hitl, _refs, dispatch, call, route = await setup_waiting()
    await hitl.answer(
        interaction_id="interaction-1",
        interaction_revision=1,
        route_fingerprint=route.fingerprint,
        answers=questionnaire_answers(),
        authenticated_answerer_id="user-1",
        verified_auth_reference_digests=[],
        verified_auth_references=[],
    )
    assert (
        await hitl.abandon(
            "interaction-1",
            call_record_id=call.call_record_id,
            reason="superseded_by_new_interaction",
        )
        == "accepted"
    )

    with caplog.at_level("WARNING"):
        state = await coordinator.reconcile_answer(call_record_id=call.call_record_id)
        recovery = A2AContinuationRecoveryService(coordinator, ledger)
        assert await recovery.recover_due(due_at=datetime.now(UTC), limit=10) == 0

    assert state == "input_required"
    assert dispatch.commands == []
    assert "hitl_answer_reconcile_deferred" not in caplog.text


@pytest.mark.parametrize(
    ("operation", "event"),
    [
        ("reconcile_answer", "hitl_answer_reconcile_deferred"),
        ("recover_call", "hitl_continuation_recovery_deferred"),
    ],
)
async def test_store_outage_defers_hitl_continuation(caplog, operation, event):
    coordinator, ledger, _hitl, _refs, dispatch, call, _route = await setup_waiting()

    async def unavailable(_call_record_id):
        raise RecoverableAdapterError("ledger down")

    ledger.load_by_record_id = unavailable
    with caplog.at_level("WARNING"):
        state = await getattr(coordinator, operation)(
            call_record_id=call.call_record_id
        )

    assert state == "delivery_uncertain"
    assert dispatch.commands == []
    [record] = [r for r in caplog.records if r.getMessage() == event]
    assert record.call_record_id == call.call_record_id
    assert record.error_type == "RecoverableAdapterError"
    assert "ledger down" not in caplog.text
