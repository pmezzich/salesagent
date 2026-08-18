"""Integration tests for the list_creatives filter-length cap (#1505).

Defense-in-depth: most CreativeFilters list fields are unbounded on the pinned
adcp schema (only creative_ids has MaxLen). An over-long list filter must be
rejected with a clean VALIDATION_ERROR rather than expanding into a very large
SQL IN (...) query. Uses the CreativeListEnv harness, mirroring
test_list_creatives_auth.py.
"""

from unittest.mock import MagicMock, patch

import pytest
from adcp import CreativeFilters

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
from src.core.exceptions import AdCPValidationError
from src.core.tools.creatives.listing import _CAPPED_FILTER_FIELDS, _MAX_FILTER_LIST_LEN
from tests.factories.principal import PrincipalFactory
from tests.harness import CreativeListEnv
from tests.harness.transport import Transport
from tests.helpers import pinned_schema

# Wire transports only — IMPL has no wire envelope. The cap raises from
# _enforce_filter_list_caps inside _build_list_creatives_request, a
# transport-blind path, so the same VALIDATION_ERROR envelope surfaces on every
# wire transport (mirrors test_list_creatives_concept_filter.py's _ALL_WIRE,
# which grades MCP too).
_ALL_WIRE = [Transport.A2A, Transport.MCP, Transport.REST]

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


class TestListCreativesFilterCap:
    def test_over_long_filter_rejected(self, integration_db):
        """A list filter longer than the cap -> VALIDATION_ERROR (correctable).

        Oracle: if ``_enforce_filter_list_caps`` (called from
        ``_build_list_creatives_request``) is removed, the request builds and
        ``_list_creatives_impl`` runs the query and returns a response instead of
        raising, so this test fails.
        """
        with CreativeListEnv() as env:
            env.setup_default_data()
            over = CreativeFilters(concept_ids=[f"concept-{i}" for i in range(_MAX_FILTER_LIST_LEN + 1)])
            with pytest.raises(AdCPValidationError) as exc:
                env.call_impl(filters=over)

        assert exc.value.recovery == "correctable"
        assert "concept_ids" in str(exc.value)
        assert str(_MAX_FILTER_LIST_LEN) in str(exc.value)
        assert exc.value.suggestion  # a remediation suggestion is surfaced

    def test_filter_at_cap_is_allowed(self, integration_db):
        """Exactly at the cap is accepted (boundary / negative control)."""
        with CreativeListEnv() as env:
            env.setup_default_data()
            at_cap = CreativeFilters(concept_ids=[f"concept-{i}" for i in range(_MAX_FILTER_LIST_LEN)])
            response = env.call_impl(filters=at_cap)

        # Concrete post-condition: the query RAN (did not raise) and returned
        # an empty, well-formed result for the unmatched concept ids.
        assert response.query_summary is not None
        assert response.query_summary.total_matching == 0

    @pytest.mark.parametrize("transport", _ALL_WIRE)
    @pytest.mark.parametrize(
        ("call_kwargs", "expected_field"),
        [
            pytest.param(
                {"filters": {"concept_ids": [f"c-{i}" for i in range(_MAX_FILTER_LIST_LEN + 1)]}},
                "concept_ids",
                id="structured-concept_ids",
            ),
            pytest.param(
                {"media_buy_ids": [f"mb-{i}" for i in range(_MAX_FILTER_LIST_LEN + 1)]},
                "media_buy_ids",
                id="flat-media_buy_ids",
            ),
        ],
    )
    def test_over_cap_filter_emits_validation_envelope(self, integration_db, transport, call_kwargs, expected_field):
        """Over-cap list filter -> spec VALIDATION_ERROR envelope on every wire transport.

        Two entry paths, one behavior (the cap runs on the MERGED filters, so both
        reach it):

        * ``structured-concept_ids`` — an over-cap value inside the structured
          ``filters`` object.
        * ``flat-media_buy_ids`` — a flat top-level list param. Merge-placement
          oracle: with the cap checked only on the pre-merge ``filters`` argument
          (the original implementation), 101 flat ``media_buy_ids`` reach the
          ``IN (...)`` expansion and this case fails with a 200-style success
          instead of the envelope.

        The wire ``field`` is the bare param name the client actually sent
        (``field=field`` at listing.py:105), never a synthetic ``filters.<x>``
        path. Assertion is routed through the harness-guarded ``assert_wire_error``
        (recovery defaults to the pinned AdCP enum; ``field=`` pins both envelope
        layers) rather than hand-indexing the envelope. (Error Verification Policy:
        grade the wire, not the reconstructed exception.)
        """
        with CreativeListEnv() as env:
            env.setup_default_data()
            result = env.call_via(transport, **call_kwargs)
            result.assert_wire_error(
                "VALIDATION_ERROR",
                require_suggestion=True,
                message_substr=expected_field,
                field=expected_field,
            )


_A2A_IDENTITY = PrincipalFactory.make_identity(
    principal_id="test_principal", tenant_id="test_tenant", tenant={"tenant_id": "test_tenant"}, protocol="a2a"
)


@pytest.mark.asyncio
async def test_a2a_list_creatives_handler_forwards_projection_and_enrichment_params():
    """The A2A list_creatives skill handler forwards every projection/enrichment param.

    ``list_creatives_raw`` accepts ``fields`` / ``include_performance`` /
    ``include_assignments`` / ``include_sub_assets`` (listing.py:606-609) and the REST
    route forwards all four (api_v1.py:449-452). The A2A skill handler previously passed
    none of them, so an A2A client asking for a field projection, performance metrics,
    package assignments, or sub-assets silently got the defaults. This pins that the
    handler now forwards all four with the values the client sent.
    """
    handler = AdCPRequestHandler()
    with patch("src.a2a_server.adcp_a2a_server.core_list_creatives_tool") as mock_core_tool:
        mock_core_tool.return_value = MagicMock()
        parameters = {
            "fields": ["creative_id", "name"],
            "include_performance": True,
            "include_assignments": True,
            "include_sub_assets": True,
        }

        await handler._handle_list_creatives_skill(parameters, _A2A_IDENTITY)

    # call_count + call_args.kwargs rather than a bare assert_called_once() + call_args,
    # which the weak-mock-assertion guard forbids as a new violation.
    assert mock_core_tool.call_count == 1
    call_kwargs = mock_core_tool.call_args.kwargs
    assert call_kwargs["fields"] == ["creative_id", "name"]
    assert call_kwargs["include_performance"] is True
    assert call_kwargs["include_assignments"] is True
    assert call_kwargs["include_sub_assets"] is True


# The AdCP JSON schema is the authority for what CreativeFilters declares.
# tests/helpers/pinned_schema.py reads it from the installed adcp SDK's own schema
# tree: the SDK's installed version IS the pin. #1868 retired the separately
# vendored fixture tree (it had drifted a full spec-minor behind), so there is now
# exactly one upstream pin. Deriving the expected set from
# ``CreativeFilters.model_fields`` instead would grade one wheel-derived artifact
# against another, so the schema — not the model — stays the source.
_PINNED_CREATIVE_FILTERS_REF = "core/creative-filters.json"


def _pinned_array_filter_fields() -> set[str]:
    """Array-typed properties of the pinned ``creative-filters.json`` schema."""
    schema = pinned_schema.load(_PINNED_CREATIVE_FILTERS_REF)
    fields = {name for name, spec in schema["properties"].items() if spec.get("type") == "array"}
    assert fields, f"non-vacuity: {_PINNED_CREATIVE_FILTERS_REF} declared no array-typed properties"
    return fields


def test_capped_fields_match_pinned_schema_array_filters():
    """_CAPPED_FILTER_FIELDS is hand-maintained — pin it against the SDK-pinned schema.

    Every array-typed CreativeFilters property expands into an ``IN (...)``
    predicate, so every one of them must be capped. If a future pin refresh adds
    an array filter, this fails and the new field must be added to the cap (or
    explicitly excluded here with a reason) — no list filter slips through
    uncapped silently.
    """
    pinned = _pinned_array_filter_fields()
    capped = set(_CAPPED_FILTER_FIELDS)

    assert pinned == capped, (
        f"{_PINNED_CREATIVE_FILTERS_REF} array filters diverged from _CAPPED_FILTER_FIELDS — "
        f"schema-only (uncapped, would expand into IN (...)): {sorted(pinned - capped)}, "
        f"cap-only (not a schema array filter): {sorted(capped - pinned)}"
    )
