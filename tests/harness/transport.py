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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypedDict

from pydantic import BaseModel

from tests.helpers import pinned_schema


@functools.lru_cache(maxsize=1)
def _pinned_error_metadata() -> dict[str, dict[str, str]]:
    """code -> {recovery, suggestion} from the installed SDK's error-code enum.

    Only the ``recovery`` field is actually read by this module (assert_wire_error
    below) — verified safe to source from the SDK tree: the SDK's enum is a
    strict superset of the older vendored fixture (92 vs 64 codes, fixture-only
    set empty) and its ``recovery`` classification is IDENTICAL across every one
    of the 64 shared codes (0 divergences). ``suggestion`` DOES diverge on 4
    codes between the two sources — but this module never reads that field
    (extract_wire_suggestion below reads the WIRE's own suggestion text, not
    this metadata), so that divergence has no effect here. Consumers that DO
    grade ``suggestion`` content (test_architecture_error_suggestion_enum_conformance.py)
    stay on the vendored fixture — see docs/adcp-spec-version.md "Pinned schema sources"
    (which also cites the command to reproduce the 64-code fixture count).
    """
    return pinned_schema.load("error-code.json")["enumMetadata"]


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


class InvalidAuthHint(TypedDict):
    """Shape of the transport-blind ``_invalid_auth`` hint the BDD step forwards.

    Names the two keys ONCE, so the producer (``uc002_create_media_buy``'s
    ``_dispatch_full_create``) and both REST consumers (``_run_rest_request`` in
    ``_base``, ``RestE2EDispatcher`` in ``dispatchers``) are bound to one
    declaration instead of re-spelling the string keys three times. ``tenant``
    carries the bare host-routed tenant id the whole non-disclosure contract
    turns on: a typo or a dropped key would otherwise be caught by nothing until
    the leg silently stopped reaching the redacted raise.

    Lives here, in the leaf transport module, rather than in ``_base``: both
    consumers already import down from ``transport``, and ``transport`` imports
    nothing from ``tests.harness``, so neither consumer has to reach up into the
    env layer to name the contract.
    """

    token: str
    tenant: str


def _invalid_auth_headers(hint: InvalidAuthHint) -> dict[str, str]:
    """Realize the ``_invalid_auth`` hint as REST auth headers — one recipe, both legs.

    The in-process leg (``_run_rest_request``) and the e2e leg
    (``RestE2EDispatcher``) send the identical pair. Hand-copying it per leg is
    how the two silently diverge, and a divergence here does not fail loudly —
    it false-floors the grade, because a leg that stops reaching the redacted
    raise still reports no disclosure.
    """
    return {"x-adcp-auth": hint["token"], "x-adcp-tenant": hint["tenant"]}


def _discard_invalid_auth_hint(kwargs: dict[str, Any]) -> None:
    """Drop the transport-blind ``_invalid_auth`` hint on the A2A and MCP legs.

    The BDD invalid-token scenario forwards the bad token uniformly as
    ``_invalid_auth`` (``_dispatch_full_create``) so no step carries
    transport-specific knowledge. A2A and MCP need no special realization: the
    bad token already rides the dispatched identity's ``auth_token`` and the real
    auth chain (header → token → DB lookup → ``ResolvedIdentity``) runs against
    it, so both simply discard the hint. REST is the only transport that realizes
    it specially — the in-process leg routes the bad token through the real
    auth dep as headers (``_run_rest_request``) because its dep override would
    otherwise inject a resolved identity and skip the raise, and the e2e leg
    CONSUMES the hint into real headers (``RestE2EDispatcher``, not this discard)
    because the identity's tenant dict carries only the derived ``pub-<uuid>``
    subdomain, not the bare host-routed tenant id under test.
    """
    kwargs.pop("_invalid_auth", None)


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
        wire_error_envelope_is_synthesized: ``True`` when the A2A dispatcher could
            not find real wire bytes and SYNTHESIZED ``wire_error_envelope`` from
            the reconstructed exception. That happens for an ``A2AError`` raised
            with no ``data``: the buyer receives a bare protocol error carrying no
            AdCP envelope, yet ``wire_error_envelope`` is populated anyway, so a
            wire assertion can pass against an envelope nobody was ever sent.
            Always ``False`` on REST/MCP (they read real bytes) and on success.
            Pass ``require_real_wire=True`` to ``assert_wire_error`` to refuse
            the synthesized envelope. Distinct from ``synthesized_error_envelope``
            above: this flag marks the A2A wire FALLBACK, while that field is the
            IMPL-only envelope no wire was ever involved in.
    """

    payload: BaseModel | None = None
    envelope: dict[str, Any] = field(default_factory=dict)
    error: Exception | None = None
    raw_response: Any = None
    wire_response: dict[str, Any] | None = None
    wire_error_envelope: dict[str, Any] | None = None
    synthesized_error_envelope: dict[str, Any] | None = None
    wire_error_envelope_is_synthesized: bool = False

    @property
    def is_success(self) -> bool:
        return self.error is None and self.payload is not None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    def assert_wire_error(
        self,
        code: str,
        *,
        recovery: str | None = None,
        require_suggestion: bool = False,
        message_substr: str | None = None,
        require_real_wire: bool = False,
    ) -> None:
        """Assert this result carries the AdCP two-layer wire error ``code``.

        Transport-independent: reads the normalized ``wire_error_envelope`` the
        dispatcher captured for whatever transport produced this result, so the
        same call holds on a2a/mcp/rest. Recovery defaults to the PINNED AdCP
        enum's classification for ``code`` (pin-wins), making the assertion
        non-vacuous without per-scenario duplication. This is the single
        harness-provided way to verify an error on the wire — step definitions
        must not hand-roll envelope parsing.

        Args:
            require_real_wire: Refuse an envelope the A2A dispatcher SYNTHESIZED
                from the reconstructed exception (see
                ``wire_error_envelope_is_synthesized``). Use it when the point of
                the test is what the buyer actually receives — a security or
                disclosure contract — rather than the envelope shape alone.
                Without it, an ``A2AError`` raised with no ``data`` still
                produces a passing wire assertion even though the buyer got a
                bare protocol error with no AdCP envelope.
        """
        from tests.helpers import assert_envelope_shape

        meta = _pinned_error_metadata()
        spec = meta.get(code)
        assert spec is not None, (
            f"{code!r} is not a canonical AdCP error code (pinned error-code.json). "
            "Reconcile the feature to a canonical code."
        )
        expected_recovery = recovery if recovery is not None else spec["recovery"]

        envelope = self.wire_error_envelope
        assert envelope is not None, (
            f"Expected a wire rejection with {code}, but no wire_error_envelope was captured "
            f"(is_error={self.is_error}, payload={self.payload!r}). The operation either "
            "succeeded or errored before reaching a transport."
        )
        assert not (require_real_wire and self.wire_error_envelope_is_synthesized), (
            f"Expected {code} on the REAL wire, but the envelope was synthesized from the reconstructed "
            f"exception: the transport raised with no envelope attached, so the buyer received a bare "
            f"protocol error carrying no AdCP envelope at all. Synthesized envelope: {envelope}"
        )
        assert_envelope_shape(envelope, code, recovery=expected_recovery, message_substr=message_substr)
        if require_suggestion:
            suggestion = extract_wire_suggestion(envelope)
            assert suggestion, f"Expected a non-empty suggestion in the {code} wire envelope: {envelope}"
