"""Executable provider-neutral ModelRuntime conformance harness."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from execution.model_loop.streaming import (
    MalformedToolArgumentsError,
    ModelStreamAssembler,
    ModelStreamAssemblyError,
    TruncatedToolCallError,
)
from execution.orchestrator.models import (
    AssistantMessage,
    ContractModel,
    ModelTurnRequest,
)
from execution.orchestrator.ports import CancellationSignal, ModelRuntime

_EXPECTED_ASSEMBLY_ERRORS: dict[str, tuple[type[ModelStreamAssemblyError], str]] = {
    "malformed_arguments": (
        MalformedToolArgumentsError,
        "malformed_tool_arguments",
    ),
    "truncated_call": (TruncatedToolCallError, "truncated_tool_call"),
}


ProviderScenario = Literal[
    "final_text",
    "one_tool_call",
    "parallel_tool_calls",
    "malformed_arguments",
    "truncated_call",
    "streaming_text",
    "usage",
    "abort",
    "retry_classification",
]


class ProviderConformanceCase(ContractModel):
    """One deterministic request and its provider-neutral expected behavior."""

    scenario: ProviderScenario
    request: ModelTurnRequest
    created_at: datetime
    expected_text: str | None = None
    expected_error_class: str | None = None


class ProviderConformanceResult(ContractModel):
    scenario: ProviderScenario
    provider_request_id: str | None = None


class ProviderConformanceError(AssertionError):
    """Raised when a ModelRuntime violates a normalized stream contract."""


def _validate_successful_case(
    case: ProviderConformanceCase,
    assistant: AssistantMessage,
    assembler: ModelStreamAssembler,
) -> None:
    if case.scenario in {"final_text", "streaming_text"}:
        actual = "".join(part.text for part in assistant.content if part.kind == "text")
        if assistant.finish_reason != "stop" or actual != case.expected_text:
            raise ProviderConformanceError(
                f"{case.scenario} returned unexpected final text"
            )
    elif case.scenario == "one_tool_call" and len(assistant.tool_calls) != 1:
        raise ProviderConformanceError("one_tool_call did not return exactly one call")
    elif case.scenario == "parallel_tool_calls" and len(assistant.tool_calls) < 2:
        raise ProviderConformanceError(
            "parallel_tool_calls did not return parallel calls"
        )
    elif case.scenario == "usage" and assistant.usage is None:
        raise ProviderConformanceError("usage scenario omitted normalized usage")
    elif case.scenario == "retry_classification":
        _validate_retry_case(case, assembler)


def _validate_retry_case(
    case: ProviderConformanceCase, assembler: ModelStreamAssembler
) -> None:
    failed = [
        event for event in assembler.retry_events if event.kind == "attempt_failed"
    ]
    scheduled = [
        event for event in assembler.retry_events if event.kind == "retry_scheduled"
    ]
    if (
        len(failed) != 1
        or len(scheduled) != 1
        or failed[0].error_class != case.expected_error_class
        or scheduled[0].error_class != case.expected_error_class
        or failed[0].attempt != 1
        or scheduled[0].attempt != 2
    ):
        raise ProviderConformanceError(
            "retry classification or stable attempt metadata was lost"
        )


async def run_provider_conformance(
    runtime: ModelRuntime,
    cases: list[ProviderConformanceCase],
    *,
    signal: CancellationSignal,
) -> list[ProviderConformanceResult]:
    """Execute the complete offline conformance matrix against any ModelRuntime."""

    required = set(get_args(ProviderScenario))
    supplied = {case.scenario for case in cases}
    if supplied != required:
        missing = sorted(required - supplied)
        extra = sorted(supplied - required)
        raise ProviderConformanceError(
            f"conformance matrix mismatch; missing={missing}, extra={extra}"
        )

    results: list[ProviderConformanceResult] = []
    for case in cases:
        assembler = ModelStreamAssembler()
        assembly_error: ModelStreamAssemblyError | None = None
        try:
            async for event in runtime.stream_turn(case.request, signal=signal):
                assembler.accept(event)
            outcome = assembler.build_outcome(
                message_id=f"conformance-{case.scenario}",
                created_at=case.created_at,
            )
            assistant = outcome.assistant
        except ModelStreamAssemblyError as exc:
            assembly_error = exc
            assistant = None

        expected_assembly_error = _EXPECTED_ASSEMBLY_ERRORS.get(case.scenario)
        if expected_assembly_error is not None:
            expected_type, expected_code = expected_assembly_error
            if assembly_error is None:
                raise ProviderConformanceError(
                    f"{case.scenario} did not fail normalized assembly"
                )
            if type(assembly_error) is not expected_type or (
                assembly_error.code != expected_code
            ):
                raise ProviderConformanceError(
                    f"{case.scenario} failed with unexpected assembly error "
                    f"{type(assembly_error).__name__}:{assembly_error.code}"
                )
        else:
            if case.scenario == "abort":
                if assembly_error is not None or outcome.kind != "aborted":
                    raise ProviderConformanceError("abort exposed an assistant result")
            else:
                if assembly_error is not None or assistant is None:
                    raise ProviderConformanceError(
                        f"{case.scenario} failed normalized assembly: {assembly_error}"
                    )
                _validate_successful_case(case, assistant, assembler)

        results.append(
            ProviderConformanceResult(
                scenario=case.scenario,
                provider_request_id=assembler.provider_request_id,
            )
        )
    return results
