"""Transport enum and TransportResult for multi-transport behavioral tests.

Defines the seven dispatch transports (IMPL, A2A, REST, MCP + E2E variants)
and a frozen result container that separates transport-specific envelope from
shared payload.

Usage::

    result = env.call_via(Transport.REST, creatives=[...])
    assert result.is_success
    assert result.payload.creatives[0].action == CreativeAction.created
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_PINNED_ERROR_ENUM = (
    Path(__file__).resolve().parents[1] / "fixtures" / "adcp_schemas_pinned" / "enums" / "error-code.json"
)


@functools.lru_cache(maxsize=1)
def _pinned_error_metadata() -> dict[str, dict[str, str]]:
    """code -> {recovery, suggestion} from the pinned AdCP error-code enum.

    The pinned enum (@04f59d2d5) is the authoritative recovery classification;
    the installed SDK ships fewer codes and diverges on several recovery values,
    so it is NOT used here (pin-wins).
    """
    return json.loads(_PINNED_ERROR_ENUM.read_text())["enumMetadata"]


def extract_wire_suggestion(envelope: dict | None) -> str | None:
    """The buyer-facing ``suggestion`` from a two-layer AdCP wire error envelope.

    STRICT error.json conformance: ``suggestion`` is a top-level sibling of
    code/message/field/retry_after/recovery on the error object (in either the
    ``errors[0]`` or the envelope-level ``adcp_error`` layer). A suggestion
    buried in the free-form ``details`` dict is NOT at the protocol position
    and deliberately does not satisfy this lookup — emitters that bury it are
    conformance bugs the harness must surface, not mask (#1417).
    Single source of truth for both ``TransportResult.assert_wire_error`` and
    the BDD ``_wire_suggestion`` step (#1417). Returns ``None`` when
    there is no envelope (IMPL / no-wire).
    """
    if not envelope:
        return None
    errors = envelope.get("errors") or [{}]
    adcp_error = envelope.get("adcp_error") or {}
    return errors[0].get("suggestion") or adcp_error.get("suggestion")


class Transport(StrEnum):
    """Dispatch transports for behavioral tests."""

    IMPL = "impl"  # Direct _impl() call
    A2A = "a2a"  # _raw() A2A wrapper
    REST = "rest"  # FastAPI TestClient → route → _raw() → _impl()
    MCP = "mcp"  # Mock Context → MCP wrapper → _impl()
    E2E_REST = "e2e_rest"  # Real HTTP via httpx → nginx → server
    E2E_MCP = "e2e_mcp"  # Real MCP via httpx → nginx → server (placeholder)
    E2E_A2A = "e2e_a2a"  # Real A2A via httpx → nginx → server (placeholder)


# Maps Transport → ResolvedIdentity.protocol value
TRANSPORT_PROTOCOL: dict[Transport, str] = {
    Transport.IMPL: "mcp",  # _impl doesn't inspect protocol; keep default
    Transport.A2A: "a2a",
    Transport.REST: "rest",
    Transport.MCP: "mcp",
    Transport.E2E_REST: "rest",
    Transport.E2E_MCP: "mcp",
    Transport.E2E_A2A: "a2a",
}


@dataclass(frozen=True)
class E2EConfig:
    """Configuration for E2E transport dispatch.

    Attributes:
        base_url: Docker stack URL (e.g., ``http://localhost:8092``).
        postgres_url: Docker PostgreSQL URL for factory data writes.
    """

    base_url: str
    postgres_url: str


@dataclass(frozen=True)
class TransportResult:
    """Normalized result from any transport dispatch.

    Attributes:
        payload: Pydantic response model (shared assertions target this).
        envelope: Transport-specific metadata (HTTP status, ToolResult, etc.).
        error: Exception raised during dispatch, if any.
        raw_response: Unprocessed transport response (httpx.Response, ToolResult, etc.).
        wire_response: Serialized success-path response body as a dict, captured
            from the real wire (REST HTTP JSON body, MCP structured_content, A2A
            artifact DataPart). ``None`` on error and on IMPL (no wire — serialize
            the typed ``payload`` instead). Lets success-path tests assert the
            actual serialized shape (e.g. the v3.1 format_id federation contract).
        wire_error_envelope: Raw two-layer error envelope dict captured from
            the actual wire bytes (REST HTTP body, MCP ToolError content text,
            A2A failed-Task artifact DataPart). ``None`` on success or on the
            IMPL transport, which has no wire. This is the canonical field
            for error verification — see ``tests/CLAUDE.md`` § Error
            Verification Policy.
        synthesized_error_envelope: Two-layer envelope produced by
            ``build_two_layer_error_envelope`` against the IMPL-caught
            ``AdCPError`` — what production WOULD emit at the boundary.
            ``None`` on success and on REST/MCP/A2A (those expose the real
            wire envelope above instead). Tests asserting on this field
            verify the envelope-builder contract, NOT the wire shape — a
            regression in the production boundary translator would not be
            caught here. Use REST/MCP/A2A for wire-shape regressions.
    """

    payload: BaseModel | None = None
    envelope: dict[str, Any] = field(default_factory=dict)
    error: Exception | None = None
    raw_response: Any = None
    wire_response: dict[str, Any] | None = None
    wire_error_envelope: dict[str, Any] | None = None
    synthesized_error_envelope: dict[str, Any] | None = None

    @property
    def is_success(self) -> bool:
        return self.error is None and self.payload is not None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    def _require_wire_envelope(self, expectation: str) -> dict[str, Any]:
        """Return the captured wire error envelope, or fail explaining ``expectation``.

        Single home for the "did a transport actually reject this?" guard shared
        by every wire grader below: a success, an error raised before the
        transport boundary, and a 500 / non-AdCP body all leave the field
        ``None``, and all three are failures for a grader that was asked to
        inspect a rejection.
        """
        envelope = self.wire_error_envelope
        assert envelope is not None, (
            f"{expectation}, but no wire_error_envelope was captured "
            f"(is_error={self.is_error}, payload={self.payload!r}). The operation either "
            "succeeded or errored before reaching a transport."
        )
        return envelope

    @staticmethod
    def _pinned_recovery(code: str) -> str:
        """The PINNED AdCP enum's recovery classification for ``code``.

        Doubles as the canonical-code gate: a code absent from the pinned
        ``error-code.json`` is not a spec code, and fails here.
        """
        spec = _pinned_error_metadata().get(code)
        assert spec is not None, (
            f"{code!r} is not a canonical AdCP error code (pinned error-code.json @04f59d2d5). "
            "Reconcile the feature to a canonical code."
        )
        return spec["recovery"]

    def assert_wire_error(
        self,
        code: str,
        *,
        recovery: str | None = None,
        require_suggestion: bool = False,
        message_substr: str | None = None,
    ) -> None:
        """Assert this result carries the AdCP two-layer wire error ``code``.

        Transport-independent: reads the normalized ``wire_error_envelope`` the
        dispatcher captured for whatever transport produced this result, so the
        same call holds on a2a/mcp/rest. Recovery defaults to the PINNED AdCP
        enum's classification for ``code`` (pin-wins), making the assertion
        non-vacuous without per-scenario duplication. This is the single
        harness-provided way to verify an error on the wire — step definitions
        must not hand-roll envelope parsing.

        ``code`` must be an expectation the caller brought with it. Feeding this
        grader a code read back out of ``self.wire_error_envelope`` makes the
        code arm unfailable; callers that only mean to grade the shape or the
        recovery hint use the code-free graders below instead.
        """
        from tests.helpers import assert_envelope_shape

        pinned_recovery = self._pinned_recovery(code)
        expected_recovery = recovery if recovery is not None else pinned_recovery

        envelope = self._require_wire_envelope(f"Expected a wire rejection with {code}")
        assert_envelope_shape(envelope, code, recovery=expected_recovery, message_substr=message_substr)
        if require_suggestion:
            suggestion = extract_wire_suggestion(envelope)
            assert suggestion, f"Expected a non-empty suggestion in the {code} wire envelope: {envelope}"

    def _grade_wire_envelope(self, *, recovery: str | None, expectation: str) -> None:
        """Grade the captured envelope's SHAPE without a caller-supplied code.

        Shared body of the two code-free graders below. ``recovery=None`` means
        "grade against the PINNED enum's classification for whatever code the
        envelope carries" (which also requires that code to be canonical);
        a string means "grade against this caller-supplied hint".

        The envelope's own ``adcp_error.code`` is read here only to drive
        ``assert_envelope_shape``'s cross-layer arm — does ``errors[0].code``
        AGREE with ``adcp_error.code``? That is a real check. It is NOT a check
        of *which* error occurred: a value taken out of the envelope under test
        cannot grade that envelope's code. Scenarios pin the code with their own
        ``the error code should be "X"`` step, which reads the wire independently.
        """
        from tests.helpers import assert_envelope_shape

        envelope = self._require_wire_envelope(expectation)
        code = (envelope.get("adcp_error") or {}).get("code")
        assert isinstance(code, str) and code, f"Wire envelope carries no envelope-level adcp_error.code: {envelope}"
        expected_recovery = recovery if recovery is not None else self._pinned_recovery(code)
        assert_envelope_shape(envelope, code, recovery=expected_recovery)

    def assert_wire_recovery(self, recovery: str) -> None:
        """Assert the wire error envelope carries ``recovery`` on BOTH layers.

        Code-free companion to ``assert_wire_error`` for scenarios that grade the
        buyer-facing retry semantics alone, leaving the code to their own
        ``the error code should be "X"`` step. Takes no code argument by design:
        the only expectation this grader compares against — ``recovery`` — is
        supplied by the caller, so nothing it asserts is derived from the
        envelope it is grading.

        Fails when no envelope was captured, when the two layers disagree on the
        code, or when either layer's ``recovery`` differs from ``recovery``.
        """
        self._grade_wire_envelope(
            recovery=recovery,
            expectation=f"Expected a wire rejection with recovery={recovery!r}",
        )

    def assert_wire_is_adcp_envelope(self) -> None:
        """Assert the failure surfaced as a spec-shaped AdCP two-layer envelope.

        Code-free: says nothing about WHICH error occurred, only that whatever
        the transport emitted is spec-shaped. Non-vacuous — it fails when

        * no envelope was captured at all (a 500 / non-AdCP body has none),
        * either layer of the two-layer envelope is missing,
        * the two layers disagree on the code,
        * the code is absent from the PINNED AdCP ``error-code.json``, or
        * either layer's ``recovery`` drifts from that enum's classification.

        The recovery expectation comes from the pinned enum rather than from the
        envelope, so an emitter that ships a spec-divergent recovery fails here.
        """
        self._grade_wire_envelope(
            recovery=None,
            expectation="Expected the AdCP two-layer error envelope (a 500 or non-AdCP body has none)",
        )
