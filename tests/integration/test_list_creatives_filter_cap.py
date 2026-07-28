"""Integration tests for the list_creatives filter-length cap (#1505).

Defense-in-depth: most CreativeFilters list fields are unbounded on the pinned
adcp schema (only creative_ids has MaxLen). An over-long list filter must be
rejected with a clean VALIDATION_ERROR rather than expanding into a very large
SQL IN (...) query. Uses the CreativeListEnv harness, mirroring
test_list_creatives_auth.py.
"""

import typing
from unittest.mock import MagicMock, patch

import pytest
from adcp import CreativeFilters

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
from src.core.exceptions import AdCPValidationError
from src.core.tools.creatives.listing import _CAPPED_FILTER_FIELDS, _MAX_FILTER_LIST_LEN
from tests.factories.principal import PrincipalFactory
from tests.harness import CreativeListEnv
from tests.harness.transport import Transport
from tests.helpers import assert_envelope_shape

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
    def test_over_cap_concept_ids_emits_validation_envelope(self, integration_db, transport):
        """Over-cap structured filter surfaces the spec VALIDATION_ERROR envelope
        on every wire transport (Error Verification Policy: grade the wire, not
        the reconstructed exception)."""
        with CreativeListEnv() as env:
            env.setup_default_data()
            result = env.call_via(
                transport,
                filters={"concept_ids": [f"c-{i}" for i in range(_MAX_FILTER_LIST_LEN + 1)]},
            )

            envelope = result.wire_error_envelope
            assert envelope is not None, f"{transport}: no wire error envelope captured"
            assert_envelope_shape(
                envelope,
                "VALIDATION_ERROR",
                recovery="correctable",
                message_substr="concept_ids",
            )
            # Pin the wire field label: the cap raises with the bare param name
            # (field=field), never a synthetic filters.<x> path the client never sent.
            assert envelope["errors"][0]["field"] == "concept_ids"
            assert envelope["adcp_error"]["field"] == "concept_ids"

    @pytest.mark.parametrize("transport", _ALL_WIRE)
    def test_over_cap_flat_media_buy_ids_rejected_on_wire(self, integration_db, transport):
        """FLAT list params are capped too — the cap runs on the MERGED filters.

        Oracle for the merge placement: with the cap checked only on the
        pre-merge ``filters`` argument (the original implementation), a flat
        ``media_buy_ids`` list of 101 entries reaches the query and this test
        fails with a 200-style success instead of the envelope.

        The flat ``media_buy_ids`` path reaches the merged filters and the
        ``IN (...)`` expansion on all three wire transports, so grade it on each
        (A2A/MCP/REST), not REST alone.
        """
        with CreativeListEnv() as env:
            env.setup_default_data()
            result = env.call_via(
                transport,
                media_buy_ids=[f"mb-{i}" for i in range(_MAX_FILTER_LIST_LEN + 1)],
            )

            envelope = result.wire_error_envelope
            assert envelope is not None, f"{transport}: no wire error envelope captured for flat media_buy_ids"
            assert_envelope_shape(
                envelope,
                "VALIDATION_ERROR",
                recovery="correctable",
                message_substr="media_buy_ids",
            )
            # Flat top-level media_buy_ids is labeled with the bare param name it was
            # sent under (field=field), not a synthetic filters.media_buy_ids path.
            assert envelope["errors"][0]["field"] == "media_buy_ids"
            assert envelope["adcp_error"]["field"] == "media_buy_ids"


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


def test_capped_fields_stay_in_parity_with_sdk_list_fields():
    """_CAPPED_FILTER_FIELDS is hand-maintained — pin it against the SDK.

    If a future adcp pin adds a list-typed field to CreativeFilters, this
    fails and the new field must be added to the cap (or explicitly excluded
    here with a reason) — no list filter can slip through uncapped silently.
    """
    sdk_list_fields = set()
    for name, field in CreativeFilters.model_fields.items():
        annotation = field.annotation
        candidates = [annotation, *typing.get_args(annotation)]
        if any(typing.get_origin(c) is list for c in candidates):
            sdk_list_fields.add(name)

    assert sdk_list_fields == set(_CAPPED_FILTER_FIELDS), (
        "CreativeFilters list-typed fields diverged from _CAPPED_FILTER_FIELDS — "
        f"sdk-only: {sorted(sdk_list_fields - set(_CAPPED_FILTER_FIELDS))}, "
        f"cap-only: {sorted(set(_CAPPED_FILTER_FIELDS) - sdk_list_fields)}"
    )
