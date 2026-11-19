"""One model turn of a Run, behind the ``ModelLoop`` entry point."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from execution.orchestrator.budget import BudgetExceeded
from execution.orchestrator.models import (
    AssistantMessage,
    ModelStreamEvent,
    OrchestratorRunState,
    PreparedResourceRef,
    RunResourceManifestSnapshot,
    SessionNotice,
    TextPart,
    ToolInteractionMessage,
    UsageRecord,
)
from execution.orchestrator.ports import CancellationSignal, ModelLoop, ModelLoopHost
from execution.orchestrator.public_text import (
    DEFAULT_COALESCE_INTERVAL_MS,
    PublicTextCoalescer,
    PublicTextSanitizer,
)
from execution.orchestrator.run_state import (
    REQUEST_USER_INPUT_TOOL_DEFINITION,
    KernelConflict,
    KernelLifecycle,
    KernelRunResult,
    _assistant_text,
    _canonical_turn_closure,
    _flush_batch,
    _has_presented_interactions,
    _presentation_id,
    _surface_agent_questions_tool_definition,
    _tool_error,
)

from .context import UnresolvedToolBatchError
from .streaming import ModelStreamAssembler, ModelStreamAssemblyError

# Gateway codes for a missing or unusable model Provider. The Run fails with the
# code as its reason instead of spending retries that cannot succeed.
_PROVIDER_UNAVAILABLE_CODES = frozenset(
    {"provider_not_configured", "credential_invalid"}
)


def _task_text(arguments: object) -> str:
    if isinstance(arguments, dict):
        task = arguments.get("task")
        if isinstance(task, str) and task.strip():
            return task.strip()[:4000]
    return ""


def _backfill_presentation_ids(
    run: OrchestratorRunState,
) -> OrchestratorRunState:
    """Upgrade checkpointed presented entries/messages without a migration."""

    batches = list(run.tool_batches)
    replacements: dict[tuple[str, str], str] = {}
    changed = False
    for batch_index, batch in enumerate(batches):
        entries = list(batch.entries)
        batch_changed = False
        for entry_index, entry in enumerate(entries):
            if (
                not entry.presented
                or entry.state not in {"input_required", "auth_required"}
                or entry.interaction_fingerprint is None
            ):
                continue
            presentation_id = _presentation_id(run, entry)
            replacements[(entry.call_id, entry.interaction_fingerprint)] = (
                presentation_id
            )
            if entry.presentation_id is None:
                entries[entry_index] = entry.model_copy(
                    update={"presentation_id": presentation_id}
                )
                batch_changed = True
                changed = True
        if batch_changed:
            batches[batch_index] = batch.model_copy(update={"entries": entries})
    transcript = list(run.transcript)
    for index, message in enumerate(transcript):
        if not isinstance(message, ToolInteractionMessage):
            continue
        presentation_id = replacements.get(
            (message.call_id, message.interaction_fingerprint)
        )
        if presentation_id is not None and message.presentation_id is None:
            transcript[index] = message.model_copy(
                update={"presentation_id": presentation_id}
            )
            changed = True
    if not changed:
        return run
    return run.model_copy(update={"tool_batches": batches, "transcript": transcript})


def _model_turn_outcome(model_outcome) -> str:
    """Public-safe outcome label for a completed model turn."""

    kind = str(getattr(model_outcome, "kind", "") or "")
    if kind == "assistant":
        return "completed"
    if kind in {"context_overflow", "provider_error", "aborted"}:
        return kind
    return "unknown"


def _elapsed_ms(now: datetime, started_at: datetime) -> int:
    return max(0, int((now - started_at).total_seconds() * 1000))


def _usage_public_fields(usage) -> dict[str, object] | None:
    if usage is None:
        return None
    return {
        "input": int(getattr(usage, "input_tokens", 0) or 0),
        "output": int(getattr(usage, "output_tokens", 0) or 0),
    }


@dataclass
class _ModelStepState:
    """Model-turn state that lives across the iterations of one Run loop."""

    invalid_observations: int = 0


def _resource_manifest_from_refs(
    refs: list[PreparedResourceRef],
) -> RunResourceManifestSnapshot:
    canonical = json.dumps(
        [ref.model_dump(mode="json") for ref in refs],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = sha256(canonical.encode()).hexdigest()
    return RunResourceManifestSnapshot(
        manifest_id=f"manifest-{digest}",
        refs=refs,
        content_digest=digest,
    )


class _HostModelLoop:
    """The model loop of one Run loop, reading its host at call time."""

    def __init__(self, host: ModelLoopHost) -> None:
        self._host = host
        self._state = _ModelStepState()

    async def step(
        self,
        run: OrchestratorRunState,
        *,
        signal: CancellationSignal,
        lifecycle: KernelLifecycle | None,
    ) -> KernelRunResult | None:
        """Take one model turn of a Run.

        Returns the Run result to end the Run, or None to take another loop
        iteration.
        """
        grace = run.budget.model_turns_used >= run.profile.max_model_turns
        if grace and not run.budget.wrap_up_requested:
            budget = self._host.budget_policy.request_wrap_up(run.budget)
            notice = SessionNotice(
                notice_id=self._host._stable_id(run, "wrap_up", 0),
                code="wrap_up",
                content="Tools are disabled. Produce the best final answer now.",
                created_at=self._host.clock.now(),
            )
            run = await self._host._checkpoint(
                run,
                updates={
                    "budget": budget,
                    "transcript": [*run.transcript, notice],
                },
                command_id=f"wrap-up:{run.run_id}",
            )
        try:
            self._host.budget_policy.before_model_turn(
                run.budget,
                run.profile,
                now=self._host.clock.now(),
            )
        except BudgetExceeded as exc:
            return await self._host._terminate(
                run, status="budget_exhausted", reason=exc.reason
            )

        run = await self._refresh_resource_manifest(run)
        presentation_backfill = _backfill_presentation_ids(run)
        if presentation_backfill is not run:
            run = await self._host._checkpoint(
                run,
                updates={
                    "tool_batches": presentation_backfill.tool_batches,
                    "transcript": presentation_backfill.transcript,
                },
                command_id=f"backfill-presentations:{run.run_id}",
            )
        tools = (
            []
            if run.budget.wrap_up_requested
            else self._host.tool_catalog.list_tools(run)
        )
        # The structured ask_user action pauses the Run into the unified
        # Execution HITL service. It is only exposed to canonical Runs so
        # the strict run_waiting_input/run_resumed lifecycle owns it.
        if not run.budget.wrap_up_requested and self._host.supervisor_hitl is not None:
            tools = [*tools, REQUEST_USER_INPUT_TOOL_DEFINITION]
            # Forwarding an Agent's questions verbatim is only meaningful
            # while a presented interaction is parked; hide it otherwise.
            if _has_presented_interactions(run):
                tools = [*tools, _surface_agent_questions_tool_definition(run)]
        try:
            if run.background_context:
                compiled = self._host.context_compiler.compile(
                    run,
                    tools=tools,
                    background=run.background_context,
                    summary=run.compaction_summary,
                )
            else:
                compiled = self._host.context_compiler.compile(
                    run, tools=tools, summary=run.compaction_summary
                )
        except UnresolvedToolBatchError:
            return await self._host._terminate(
                run, status="failed", reason="unresolved tool batch"
            )
        if compiled.kind == "context_unfit":
            return await self._host._terminate(
                run, status="failed", reason="context_unfit"
            )
        if compiled.kind == "needs_compaction":
            compacted = await self._compact(
                run,
                compiled.messages,
                baseline=compiled.estimated_input_tokens,
                signal=signal,
            )
            if isinstance(compacted, KernelRunResult):
                return compacted
            return None
        if (
            run.compaction_baseline_tokens is not None
            and compiled.estimated_input_tokens >= run.compaction_baseline_tokens
        ):
            return await self._host._terminate(
                run,
                status="budget_exhausted",
                reason="compaction did not reduce context",
            )
        if run.compaction_baseline_tokens is not None:
            run = await self._host._checkpoint(
                run,
                updates={"compaction_baseline_tokens": None},
                command_id=(
                    f"compaction-validated:{run.run_id}:{run.budget.compactions_used}"
                ),
            )

        retry_reserved = await self._host._reserve_model_retry(run)
        if isinstance(retry_reserved, KernelRunResult):
            return retry_reserved
        run = retry_reserved

        decision_continuation = bool(
            run.active_internal_turn_id
        ) and _has_presented_interactions(run)
        if decision_continuation:
            request = self._model_request(
                run,
                compiled.messages,
                tools,
                turn_id=run.active_internal_turn_id,
            )
        else:
            request = self._model_request(run, compiled.messages, tools)
        assistant_message_id = self._host.id_factory.new_id("assistant")
        attempt_key = f"{request.turn_id}:{assistant_message_id}:1"
        budget = self._host.budget_policy.record_provider_attempt(
            run.budget, run.profile, attempt_key=attempt_key, retry=False
        )
        run = await self._host._checkpoint(
            run,
            updates={
                "budget": budget,
                "active_internal_turn_id": request.turn_id,
                "active_assistant_message_id": assistant_message_id,
                "active_attempt": 1,
                "greatest_public_text_offset": 0,
                "active_public_text": "",
            },
            command_id=(
                f"public-turn-start:{request.turn_id}:{assistant_message_id}"
                if decision_continuation
                else f"public-turn-start:{request.turn_id}"
            ),
        )
        if not decision_continuation:
            await self._host._emit(
                lifecycle,
                "turn_started",
                run,
                {"internal_turn_id": request.turn_id, "attempt": 1},
            )
        await self._host._emit(
            lifecycle,
            "message_started",
            run,
            {
                "internal_turn_id": request.turn_id,
                "message_id": assistant_message_id,
            },
        )
        assembler = ModelStreamAssembler()
        public_sanitizer = PublicTextSanitizer(
            secret_values=self._host.public_secret_values
        )
        public_coalescer = PublicTextCoalescer(
            run_id=run.run_id,
            internal_turn_id=request.turn_id,
            message_id=assistant_message_id,
        )
        turn_started_at = self._host.clock.now()
        turn_usage: UsageRecord | None = None
        turn_finish_reason: str | None = None
        turn_attempt: int | None = None
        stream = None
        next_event: asyncio.Task[ModelStreamEvent] | None = None
        try:
            stream = self._host.model_runtime.stream_turn(
                request, signal=signal
            ).__aiter__()
            next_event = asyncio.create_task(anext(stream))
            while True:
                done, _ = await asyncio.wait(
                    {next_event},
                    timeout=DEFAULT_COALESCE_INTERVAL_MS / 1000,
                )
                if not done:
                    # The provider may stall after a short safe fragment.
                    # Flush the coalescer timer without cancelling/reordering
                    # the outstanding provider ``anext`` operation.
                    run = await self._publish_public_text(
                        lifecycle,
                        run,
                        public_coalescer,
                        "",
                        timer_flush=True,
                    )
                    continue
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    break
                deferred_attempt = (
                    event.kind == "attempt_started" and (event.attempt or 1) > 1
                )
                if not deferred_attempt:
                    next_event = asyncio.create_task(anext(stream))
                assembler.accept(event)
                if event.kind == "text_delta" and event.delta:
                    decidable = public_sanitizer.feed(event.delta)
                    run = await self._publish_public_text(
                        lifecycle,
                        run,
                        public_coalescer,
                        decidable,
                    )
                elif event.kind in {"tool_call_start", "finish", "error"}:
                    decidable = public_sanitizer.flush()
                    run = await self._publish_public_text(
                        lifecycle,
                        run,
                        public_coalescer,
                        decidable,
                        semantic_boundary=True,
                    )
                run = await self._host._record_model_event(
                    run,
                    request.turn_id,
                    event,
                    message_id=assistant_message_id,
                )
                # Attempt 1 was admitted before stream creation. Later
                # Gateway attempts must likewise be durable before prefetch
                # can resume the generator into its next provider call.
                if deferred_attempt:
                    next_event = asyncio.create_task(anext(stream))
                await self._emit_model_event(lifecycle, run, event, request)
                if event.kind == "usage" and event.usage is not None:
                    turn_usage = event.usage
                if event.kind == "finish" and event.finish_reason:
                    turn_finish_reason = str(event.finish_reason)
                if event.attempt is not None:
                    turn_attempt = event.attempt
            final_fragment = public_sanitizer.flush()
            run = await self._publish_public_text(
                lifecycle,
                run,
                public_coalescer,
                final_fragment,
                semantic_boundary=True,
            )
            public_text = public_sanitizer.public_text
            model_outcome = assembler.build_outcome(
                message_id=assistant_message_id,
                created_at=self._host.clock.now(),
            )
            if model_outcome.assistant is not None:
                non_text_parts = [
                    part
                    for part in model_outcome.assistant.content
                    if not isinstance(part, TextPart)
                ]
                model_outcome = model_outcome.__class__(
                    kind=model_outcome.kind,
                    assistant=model_outcome.assistant.model_copy(
                        update={
                            "content": (
                                [TextPart(text=public_text)] if public_text else []
                            )
                            + non_text_parts
                        }
                    ),
                    error_class=model_outcome.error_class,
                    provider_request_id=model_outcome.provider_request_id,
                )
            await self._host._emit(
                lifecycle,
                "model_turn_completed",
                run,
                {
                    "model": request.model.model_id,
                    "provider": request.model.provider,
                    "attempt": turn_attempt,
                    "outcome": _model_turn_outcome(model_outcome),
                    "duration_ms": _elapsed_ms(self._host.clock.now(), turn_started_at),
                    "usage": _usage_public_fields(turn_usage),
                    "finish_reason": turn_finish_reason,
                },
            )
        except BudgetExceeded as exc:
            return await self._host._terminate(
                run, status="budget_exhausted", reason=exc.reason
            )
        except ModelStreamAssemblyError as exc:
            retry_reserved = await self._host._reserve_model_retry(run)
            if isinstance(retry_reserved, KernelRunResult):
                return retry_reserved
            run = retry_reserved
            run, closed_turn_id = await self._host._close_active_attempt(
                run,
                disposition="error",
                error_summary="The model response could not be assembled.",
            )
            notice = self._assembly_notice(run, exc)
            run = await self._append_notice(run, notice)
            self._state.invalid_observations += 1
            if self._state.invalid_observations > run.profile.grace_model_turns + 1:
                return await self._host._terminate(
                    run, status="failed", reason="invalid model output loop"
                )
            await self._host._emit(
                lifecycle,
                "model_retry_scheduled",
                run,
                {
                    "internal_turn_id": closed_turn_id or request.turn_id,
                    "attempt": 2,
                    "error_class": "assembly_error",
                    "retry_delay_ms": 0,
                },
            )
            return None
        except ValueError:
            return await self._host._terminate(
                run, status="failed", reason="public_text_oversized"
            )
        finally:
            if next_event is not None:
                if not next_event.done():
                    next_event.cancel()
                try:
                    await next_event
                except (asyncio.CancelledError, Exception):
                    pass
            if stream is not None:
                close_stream = getattr(stream, "aclose", None)
                if close_stream is not None:
                    try:
                        await close_stream()
                    except (asyncio.CancelledError, Exception):
                        pass

        if model_outcome.kind == "aborted":
            current = await self._host._load(run.run_id)
            if current.status == "canceling":
                return KernelRunResult("cancellation_pending", current)
            raise KernelConflict(
                "model cancellation observed without durable cancellation"
            )
        if model_outcome.kind == "context_overflow":
            if (
                self._host.context_compactor is None
                or run.budget.compactions_used >= run.profile.max_compactions
            ):
                return await self._host._terminate(
                    run, status="budget_exhausted", reason="context overflow"
                )
            # Persist classification and reserve only the independent
            # compaction budget before closure can clear attempt identity.
            number = run.budget.compactions_used + 1
            notice = SessionNotice(
                notice_id=(
                    f"context-overflow:{request.turn_id}:"
                    f"{assistant_message_id}:{number}"
                ),
                code="context_overflow",
                content=(
                    "The model context limit was exceeded; compact before continuing."
                ),
                created_at=self._host.clock.now(),
            )
            await self._host._checkpoint(
                run,
                updates={
                    "transcript": [*run.transcript, notice],
                    "budget": self._host.budget_policy.record_compaction(
                        run.budget, run.profile
                    ),
                    "compaction_baseline_tokens": compiled.estimated_input_tokens,
                },
                command_id=notice.notice_id,
            )
            return None
        if model_outcome.kind == "provider_error":
            if model_outcome.error_code in _PROVIDER_UNAVAILABLE_CODES:
                # Only a setup save changes this state, so a retry would
                # fail the same way; fail now with the reason instead.
                return await self._host._terminate(
                    run, status="failed", reason=model_outcome.error_code
                )
            retry_reserved = await self._host._reserve_model_retry(run)
            if isinstance(retry_reserved, KernelRunResult):
                return retry_reserved
            run = retry_reserved
            run, closed_turn_id = await self._host._close_active_attempt(
                run,
                disposition="error",
                error_summary="The model provider did not complete the response.",
            )
            notice = SessionNotice(
                notice_id=self._host._stable_id(
                    run,
                    model_outcome.error_class or "provider_error",
                    run.budget.model_turns_used,
                ),
                code=model_outcome.error_class or "provider_error",
                content="The previous model attempt failed; retry within bounds.",
                created_at=self._host.clock.now(),
            )
            run = await self._append_notice(run, notice)
            await self._host._emit(
                lifecycle,
                "model_retry_scheduled",
                run,
                {
                    "internal_turn_id": closed_turn_id or request.turn_id,
                    "attempt": 2,
                    "error_class": "provider_error",
                    "retry_delay_ms": 0,
                },
            )
            return None
        assistant = model_outcome.assistant
        if assistant is None:
            return await self._host._terminate(
                run, status="failed", reason="missing assistant outcome"
            )
        run = await self._host._checkpoint(
            run,
            updates={
                "budget": self._host.budget_policy.record_assistant_turn(
                    run.budget, grace=grace
                )
            },
            command_id=(
                f"assistant-turn:{request.turn_id}:{assistant.message_id}"
                if decision_continuation
                else f"assistant-turn:{request.turn_id}"
            ),
        )
        run = await self._host._append_assistant(run, assistant)
        # A no-tool Assistant message is always final. Canonical final
        # publication is delayed until every parked child and Tool entry
        # has been durably closed and flushed.
        if not assistant.tool_calls:
            return await self._host._finalize_canonical_answer(
                run, assistant, lifecycle=lifecycle
            )
        # Commentary is public only after the complete source-ordered
        # declared Tool batch is durable, so restart recovery never has to
        # guess declarations from public text.
        run = await self._host._ensure_tool_batch(run, assistant)
        assistant_public_text = _assistant_text(assistant)
        await self._host._emit(
            lifecycle,
            "message_completed",
            run,
            {
                "public_event_id": (
                    f"public:{run.run_id}:{request.turn_id}:"
                    f"{assistant.message_id}:message_end"
                ),
                "internal_turn_id": request.turn_id,
                "message_id": assistant.message_id,
                "stop_reason": "tool_use",
                "disposition": "commentary",
                "text": assistant_public_text,
            },
        )
        run = await self._host._checkpoint(
            run,
            updates={
                "active_assistant_message_id": None,
                "active_public_text": "",
                "greatest_public_text_offset": 0,
            },
            command_id=f"public-message-end:{assistant.message_id}",
        )
        await self._host._emit(
            lifecycle,
            "orchestrator_decision",
            run,
            {
                "plan_steps": [
                    {
                        "agent": self._host._tool_label(run, call.tool_name)
                        or call.tool_name,
                        "summary": _task_text(call.arguments),
                    }
                    for call in assistant.tool_calls
                ],
                "reason": _assistant_text(assistant),
            },
        )
        if run.budget.wrap_up_requested:
            run = await self._reject_grace_tools(run, assistant)
            return None
        try:
            result = await self._host._execute_tool_batch(
                run, assistant, signal, lifecycle=lifecycle
            )
        except asyncio.CancelledError:
            if signal.cancelled:
                current = await self._host._load(run.run_id)
                if current.status == "canceling":
                    return KernelRunResult("cancellation_pending", current)
                raise KernelConflict(
                    "tool cancellation observed without durable cancellation"
                ) from None
            raise
        if result == "retry":
            return None
        if result == "decide":
            # Model-first HITL decision turn continues within the same
            # internal turn; the loop re-compiles and calls the model.
            return None
        if result is not None:
            return result
        completed_run = await self._host._load(run.run_id)
        closure = _canonical_turn_closure(completed_run, request.turn_id)
        if closure is None:
            # The active internal turn still has open entries (for
            # example a presented interaction awaiting a model join
            # reply). Defer turn_end; the loop continues to the next
            # decision turn within the same internal turn.
            return None
        closing_message_id = closure.message_id
        turn_public_ids = list(closure.public_tool_call_ids)
        await self._host._emit(
            lifecycle,
            "turn_completed",
            completed_run,
            {
                "internal_turn_id": request.turn_id,
                "message_id": closing_message_id,
                "tool_call_ids": turn_public_ids,
                "status": "completed",
            },
        )
        run = await self._host._checkpoint(
            completed_run,
            updates={
                "active_internal_turn_id": None,
                "active_assistant_message_id": None,
                "active_attempt": None,
            },
            command_id=f"public-turn-end:{request.turn_id}",
        )
        return None

    async def resume_overflow(
        self,
        run: OrchestratorRunState,
        notice: SessionNotice,
        *,
        signal: CancellationSignal,
        lifecycle: KernelLifecycle | None,
    ) -> OrchestratorRunState | KernelRunResult:
        """Finish the existing overflow closure/compaction after a lost ack."""
        turn_id = notice.notice_id.split(":", 2)[1]
        if run.active_internal_turn_id is not None:
            run, _ = await self._host._close_active_attempt(
                run,
                disposition="error",
                error_summary="The model context limit was exceeded.",
            )
        command = f"compaction:{run.run_id}:{run.budget.compactions_used}"
        if command not in run.processed_command_ids:
            compiled = self._host.context_compiler.compile(
                run,
                tools=self._host.tool_catalog.list_tools(run),
                summary=run.compaction_summary,
                **(
                    {"background": run.background_context}
                    if run.background_context
                    else {}
                ),
            )
            assert run.compaction_baseline_tokens is not None
            compacted = await self._compact(
                run,
                list(compiled.messages),
                baseline=run.compaction_baseline_tokens,
                signal=signal,
                reserved=True,
            )
            if isinstance(compacted, KernelRunResult):
                return compacted
            run = compacted
        await self._host._emit(
            lifecycle,
            "model_retry_scheduled",
            run,
            {
                "internal_turn_id": turn_id,
                "attempt": 2,
                "error_class": "context_overflow",
                "retry_delay_ms": 0,
            },
        )
        return await self._host._checkpoint(
            run,
            updates={
                "processed_command_ids": [
                    *run.processed_command_ids,
                    f"resumed:{notice.notice_id}",
                ]
            },
            command_id=f"resumed:{notice.notice_id}",
        )

    async def _compact(
        self,
        run: OrchestratorRunState,
        messages: list[object],
        *,
        baseline: int,
        signal: CancellationSignal,
        reserved: bool = False,
    ) -> OrchestratorRunState | KernelRunResult:
        if self._host.context_compactor is None or (
            not reserved and run.budget.compactions_used >= run.profile.max_compactions
        ):
            return await self._host._terminate(
                run, status="budget_exhausted", reason="context overflow"
            )
        try:
            self._host.budget_policy.before_model_turn(
                run.budget.model_copy(
                    update={"compactions_used": run.budget.compactions_used - 1}
                )
                if reserved
                else run.budget,
                run.profile,
                now=self._host.clock.now(),
                purpose="compaction",
            )
            turn_id = self._host._stable_id(
                run, "compaction", run.budget.compactions_used + (0 if reserved else 1)
            )

            async def record_event(event) -> None:
                nonlocal run
                run = await self._host._record_model_event(run, turn_id, event)

            compaction = await self._host.context_compactor.compact(
                messages,
                turn_id=turn_id,
                remaining_provider_retries=(
                    self._host.budget_policy.remaining_provider_retries(
                        run.budget, run.profile
                    )
                ),
                deadline_at=run.budget.deadline_at,
                on_event=record_event,
                signal=signal,
            )
            summary = compaction.summary
            budget = (
                run.budget
                if reserved
                else self._host.budget_policy.record_compaction(run.budget, run.profile)
            )
        except BudgetExceeded as exc:
            return await self._host._terminate(
                run, status="budget_exhausted", reason=exc.reason
            )
        except Exception:
            summary = "Older completed turns omitted; preserve recent context."
            budget = (
                run.budget
                if reserved
                else self._host.budget_policy.record_compaction(run.budget, run.profile)
            )
        run = await self._host._checkpoint(
            run,
            updates={
                "budget": budget,
                "compaction_summary": summary,
                "compaction_baseline_tokens": baseline,
                **(
                    {
                        "processed_command_ids": [
                            *run.processed_command_ids,
                            f"compaction:{run.run_id}:{run.budget.compactions_used}",
                        ]
                    }
                    if reserved
                    else {}
                ),
            },
            command_id=(
                f"compaction:{run.run_id}:{run.budget.compactions_used + (0 if reserved else 1)}"
            ),
        )
        return run

    async def _publish_public_text(
        self,
        lifecycle: KernelLifecycle | None,
        run: OrchestratorRunState,
        coalescer: PublicTextCoalescer,
        text: str,
        *,
        semantic_boundary: bool = False,
        timer_flush: bool = False,
    ) -> OrchestratorRunState:
        now = self._host.clock.now()
        deltas = coalescer.add(text, now=now)
        if timer_flush:
            deltas.extend(coalescer.timed_flush(now=now))
        if semantic_boundary:
            deltas.extend(coalescer.semantic_flush(now=now))
        for delta in deltas:
            await self._host._emit(
                lifecycle,
                "message_updated",
                run,
                {
                    "public_event_id": delta.event_id,
                    "internal_turn_id": coalescer.internal_turn_id,
                    "message_id": coalescer.message_id,
                    "content_index": delta.content_index,
                    "delta_index": delta.delta_index,
                    "start_offset": delta.start_offset,
                    "end_offset": delta.end_offset,
                    "delta": delta.delta,
                },
            )
            # Public append acknowledgement precedes this advisory checkpoint.
            active_text = f"{run.active_public_text}{delta.delta}"
            run = await self._host._checkpoint(
                run,
                updates={
                    "greatest_public_text_offset": delta.end_offset,
                    "active_public_text": active_text,
                },
                command_id=(
                    f"public-text-offset:{coalescer.message_id}:{delta.end_offset}"
                ),
            )
        return run

    async def _emit_model_event(
        self,
        lifecycle: KernelLifecycle | None,
        run: OrchestratorRunState,
        event,
        request=None,
    ) -> None:
        event_type = {
            "attempt_started": "model_attempt_started",
            "retry_scheduled": "model_retry_scheduled",
            "attempt_failed": "model_attempt_failed",
        }.get(event.kind)
        if event_type is not None:
            # Gateway-internal transport attempts do not own independent public
            # Assistant identities. Canonical retry facts are emitted only by
            # the Kernel after closing the public attempt below.
            if event_type == "model_retry_scheduled":
                return
            payload: dict[str, object] = {
                "attempt": event.attempt or 0,
                "error_class": event.error_class or "",
            }
            if request is not None:
                payload["internal_turn_id"] = request.turn_id
                payload["model"] = request.model.model_id
                payload["provider"] = request.model.provider
            if event.kind == "retry_scheduled" and event.retry_delay_ms is not None:
                payload["retry_delay_ms"] = event.retry_delay_ms
            await self._host._emit(lifecycle, event_type, run, payload)

    def _model_request(
        self,
        run: OrchestratorRunState,
        messages: list[object],
        tools: list[object],
        *,
        turn_id: str | None = None,
    ):
        from execution.orchestrator.models import (
            ModelMessage,
            ModelTurnRequest,
            ToolDefinition,
        )

        return ModelTurnRequest(
            turn_id=turn_id
            or self._host._stable_id(run, "model-turn", run.state_version),
            model=run.profile.model,
            system_prompt=run.profile.prompt.rendered_system_prompt,
            messages=[item for item in messages if isinstance(item, ModelMessage)],
            tools=[item for item in tools if isinstance(item, ToolDefinition)],
            tool_choice="none" if run.budget.wrap_up_requested else "auto",
            purpose="agent_turn",
            thinking_level=run.profile.thinking_level,
            remaining_provider_retries=self._host.budget_policy.remaining_provider_retries(
                run.budget, run.profile
            ),
            absolute_deadline_at=run.budget.deadline_at,
        )

    def _assembly_notice(
        self, run: OrchestratorRunState, exc: ModelStreamAssemblyError
    ) -> SessionNotice:
        notice_id = self._host._stable_id(
            run,
            exc.code,
            len(run.budget.provider_attempt_keys),
            exc.provider_call_id or "",
            exc.tool_index if exc.tool_index is not None else "",
            exc.raw_arguments_digest or "",
        )
        return SessionNotice(
            notice_id=notice_id,
            code=exc.code,
            content="The prior tool call was malformed or incomplete; retry safely.",
            related_call_id=exc.provider_call_id,
            created_at=self._host.clock.now(),
        )

    async def _append_notice(
        self, run: OrchestratorRunState, notice: SessionNotice
    ) -> OrchestratorRunState:
        if any(
            isinstance(message, SessionNotice) and message.notice_id == notice.notice_id
            for message in run.transcript
        ):
            return run
        return await self._host._checkpoint(
            run,
            updates={"transcript": [*run.transcript, notice]},
            command_id=f"notice:{notice.notice_id}",
        )

    async def _reject_grace_tools(
        self, run: OrchestratorRunState, assistant: AssistantMessage
    ) -> OrchestratorRunState:
        batch_index = next(
            (
                index
                for index, batch in enumerate(run.tool_batches)
                if batch.assistant_message_id == assistant.message_id
                and not batch.results_flushed
            ),
            None,
        )
        if batch_index is None:
            raise KernelConflict("wrap-up tool batch is missing")
        batches = list(run.tool_batches)
        batch = batches[batch_index]
        calls = {call.call_id: call for call in assistant.tool_calls}
        entries = []
        for entry in batch.entries:
            call = calls.get(entry.call_id)
            if call is None:
                raise KernelConflict("wrap-up tool batch does not correlate")
            entries.append(
                entry.model_copy(
                    update={
                        "state": "terminal",
                        "buffered_terminal_result": _tool_error(
                            call,
                            "grace_tools_disabled",
                            "Tools are disabled during wrap-up.",
                        ),
                    }
                )
            )
        batch = batch.model_copy(update={"entries": entries})
        transcript, batch = _flush_batch(run.transcript, batch, self._host.clock.now())
        batches[batch_index] = batch
        return await self._host._checkpoint(
            run,
            updates={"transcript": transcript, "tool_batches": batches},
            command_id=f"reject-grace-tools:{assistant.message_id}",
        )

    async def _refresh_resource_manifest(
        self, run: OrchestratorRunState
    ) -> OrchestratorRunState:
        """Fold produced Agent artifacts into the live resource manifest.

        Inline DataParts are already durable in the accepted A2A observation.
        The metadata reader promotes their refs into real MIME/digest-bearing
        resources so the next model turn can select them from strict Tool
        schemas and the A2A runtime can rematerialize the original DataPart.
        """
        if run.resource_manifest is None:
            return run
        current = run
        for _attempt in range(3):
            existing = {ref.ref_id for ref in current.resource_manifest.refs}
            pending = [
                ref_id
                for ref_id in current.artifact_refs
                if ref_id and ref_id not in existing
            ]
            if not pending:
                return current
            new_refs: list[PreparedResourceRef] = []
            for ref_id in pending:
                described = (
                    await self._host.artifact_metadata_reader(
                        ref_id,
                        conversation_id=current.conversation_id,
                        room_epoch=current.request.room_epoch,
                    )
                    if self._host.artifact_metadata_reader is not None
                    else None
                )
                if described is None:
                    if ref_id.startswith("art_"):
                        raise KernelConflict(
                            f"inline artifact metadata is unavailable: {ref_id}"
                        )
                    described = PreparedResourceRef(
                        ref_id=ref_id,
                        kind="artifact",
                        source_message_id=current.request.user_message_id,
                        mime_type=None,
                        size_bytes=0,
                        content_digest="",
                    )
                new_refs.append(described)
            manifest = _resource_manifest_from_refs(
                [*current.resource_manifest.refs, *new_refs]
            )
            try:
                return await self._host._checkpoint(
                    current,
                    updates={"resource_manifest": manifest},
                    command_id=(
                        f"resource-manifest:{current.run_id}:{manifest.content_digest}"
                    ),
                )
            except KernelConflict:
                current = await self._host._load(current.run_id)
        raise KernelConflict("resource manifest refresh did not converge")


def model_loop_for(host: ModelLoopHost) -> ModelLoop:
    """Build the model loop for one Run loop of ``host``."""

    return _HostModelLoop(host)


__all__ = ["model_loop_for"]
