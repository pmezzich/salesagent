"""Tests for REST API /api/v1/products endpoint.

Validates the first REST transport for get_products:
- Endpoint exists and returns 200
- Response has 'products' field
- Auth-optional (discovery skill)
- Version compat applied when adcp_version < 3.0
- Error responses use AdCPError format

beads: salesagent-b61l.13
"""

from unittest.mock import patch

import pytest
from adcp import BuyingMode
from fastmcp.tools.tool import Tool
from starlette.testclient import TestClient

from src.app import app
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import GetProductsResponse
from src.core.tools.products import get_products
from src.routes.api_v1 import GetProductsBody
from tests.harness.product import _BODY_FIELDS
from tests.helpers import assert_envelope_shape

_MOCK_IDENTITY = ResolvedIdentity(
    principal_id="test-principal",
    tenant_id="default",
    tenant={"tenant_id": "default"},
    auth_token="test-token",
    protocol="rest",
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

client = TestClient(app)


# ---------------------------------------------------------------------------
# Route Existence
# ---------------------------------------------------------------------------


class TestRESTProductsEndpoint:
    """Verify POST /api/v1/products endpoint."""

    @pytest.fixture
    def stub_impl(self):
        """Patch _get_products_impl to the fixed sentinel every happy path drives.

        Only _get_products_impl is patched here — not resolve_identity — so the
        no-auth discovery test can share this fixture while still exercising the
        real unauthenticated identity path. Tests that need a resolved identity add
        the resolve_identity patch as a decorator.
        """
        with patch("src.core.tools.products._get_products_impl") as mock_impl:
            mock_impl.return_value = GetProductsResponse(products=[], message="test")
            yield mock_impl

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"brief": "video ads"}, id="brief-only"),
            pytest.param({"brief": "video ads", "buying_mode": "brief"}, id="with-buying-mode"),
        ],
    )
    @patch("src.core.resolved_identity.resolve_identity", return_value=_MOCK_IDENTITY)
    def test_endpoint_accepts_spec_valid_body(self, mock_resolve, stub_impl, body):
        """A spec-valid body — with or without buying_mode — is accepted at 200.

        buying_mode is the sole entry in the required array of get-products-request
        at the pinned spec (3.1.1), so a spec-valid client always sends it. Under the
        dev/CI extra="forbid" policy an undeclared field is rejected, so REST has to
        declare it.

        Not framed as MCP/A2A parity: MCP rejects buying_mode in dev/CI
        (VALIDATION_ERROR, "Unexpected keyword argument") because its tool schema is
        additionalProperties: false without it, and A2A's acceptance is meaningless
        since it accepts any unknown field.

        Deletion oracle: drop ``buying_mode`` from GetProductsBody and the
        ``with-buying-mode`` row reddens with HTTP 400 (INVALID_REQUEST,
        recovery=correctable) — not the 422 an earlier version of this docstring
        claimed. FastAPI's default 422 is replaced by the RequestValidationError
        handler in src/app.py, which maps INVALID_REQUEST to 400.
        """
        response = client.post(
            "/api/v1/products",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200, (
            f"spec-valid body should be accepted, got {response.status_code}: {response.text}"
        )

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"brief": "video ads", "buying_mode": "garbage"}, id="out-of-enum-buying-mode"),
            pytest.param({"brief": "video ads", "not_a_real_field": "x"}, id="undeclared-field"),
        ],
    )
    @patch("src.core.resolved_identity.resolve_identity", return_value=_MOCK_IDENTITY)
    def test_spec_invalid_body_is_rejected_with_the_adcp_envelope(self, mock_resolve, stub_impl, body):
        """A spec-invalid body 400s with the two-layer AdCP envelope, not FastAPI's 422.

        Mirrors the accept parametrize above: both rejection shapes share one oracle
        (400 + INVALID_REQUEST/correctable envelope), differing only in the request body.

        out-of-enum-buying-mode: get-products-request.json@3.1.1 defines buying_mode as
        enum ["brief","wholesale","refine"], and the UC-001 storyboard grades the
        out-of-enum case as an error (@T-UC-001-partition-buying-mode: unknown_value →
        "buying_mode must be one of enum values"; @T-UC-001-boundary-buying-mode:
        buying_mode='auction' → invalid). Typing the REST field to that Literal makes
        the boundary reject a non-spec value rather than accept it at 200 — this row
        is the non-vacuity oracle for that narrowing.

        undeclared-field: the other half of the buying_mode case and the reason it
        cannot pass vacuously — it proves extra="forbid" is actually engaged on this
        route. Without it, a GetProductsBody that had silently fallen back to
        extra="ignore" would accept buying_mode for the wrong reason and the positive
        test would still be green. It also backs this module's docstring claim that
        error responses use the AdCPError format.

        Both surface through the same RequestValidationError handler as any structural
        rejection, so the wire code is INVALID_REQUEST/correctable (the value-vs-structural
        code split is a repo-wide follow-up, not this PR).
        """
        response = client.post(
            "/api/v1/products",
            json=body,
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 400, f"expected 400, got {response.status_code}: {response.text}"
        assert_envelope_shape(response.json(), "INVALID_REQUEST", recovery="correctable")

    @patch("src.core.resolved_identity.resolve_identity", return_value=_MOCK_IDENTITY)
    def test_response_has_products_field(self, mock_resolve, stub_impl):
        """Response must contain 'products' list."""
        response = client.post(
            "/api/v1/products",
            json={"brief": "video ads"},
            headers={"Authorization": "Bearer test-token"},
        )
        body = response.json()
        assert "products" in body
        assert isinstance(body["products"], list)

    def test_works_without_auth(self, stub_impl):
        """get_products is a discovery skill — should work without auth."""
        response = client.post(
            "/api/v1/products",
            json={"brief": "video ads"},
        )
        # Should return 200, not 401 — discovery skill allows unauthenticated access
        assert response.status_code == 200, f"Discovery skill should work without auth, got {response.status_code}"

    def test_body_fields_match_model(self):
        """The harness REST body builder must list exactly GetProductsBody's fields.

        tests/harness/product.py hand-lists GetProductsBody's fields in _BODY_FIELDS
        to shape the REST POST body. This PR had to edit that tuple in lockstep with
        adding buying_mode to the model; the next field added to the model would
        otherwise be silently dropped from every REST harness call. Pin the two
        together so drift fails here instead of shipping.
        """
        assert set(_BODY_FIELDS) == set(GetProductsBody.model_fields), (
            "tests/harness/product.py _BODY_FIELDS drifted from GetProductsBody.model_fields; "
            "update the tuple so REST harness calls forward every declared field"
        )

    def test_endpoint_not_404(self):
        """POST /api/v1/products must exist (not 404)."""
        response = client.post(
            "/api/v1/products",
            json={"brief": "test"},
        )
        assert response.status_code != 404, "REST endpoint /api/v1/products should exist"


# ---------------------------------------------------------------------------
# MCP transport: schema declaration
# ---------------------------------------------------------------------------


class TestMCPProductsSchemaDeclaration:
    """The MCP get_products tool must declare buying_mode, grounded on adcp.BuyingMode.

    buying_mode is the sole required entry of get-products-request@3.1.1, so a
    spec-valid client sends it on every transport. FastMCP advertises
    additionalProperties: false over the get_products params, so an *undeclared*
    buying_mode 400s (VALIDATION_ERROR, "Unexpected keyword argument") in dev/CI —
    the same rejection REST's extra="forbid" produces. This pins the MCP-side
    declaration (src/core/tools/products.py:800) so that removing buying_mode from
    the signature reddens here instead of silently shipping a tool that rejects the
    required field.

    It also pins the enum to adcp.BuyingMode: the REST GetProductsBody field
    (src/routes/api_v1.py:90) and this MCP declaration share that one SDK enum rather
    than two hand-transcribed Literals, so an adcp bump that adds a mode cannot leave
    one boundary stale. The live 4-transport UC-001 acceptance that exercises the wire
    is deferred to #1730; this unit test pins the declaration.
    """

    @staticmethod
    def _buying_mode_enum(node: dict, root_schema: dict) -> set[str]:
        """Resolve buying_mode's enum whether FastMCP inlines it or emits a $ref."""
        defs = root_schema.get("$defs", {})
        for branch in node.get("anyOf", [node]):
            if "$ref" in branch:
                branch = defs.get(branch["$ref"].split("/")[-1], {})
            if "enum" in branch:
                return set(branch["enum"])
        return set()

    def test_mcp_tool_declares_buying_mode_enum(self):
        """MCP get_products declares buying_mode as the adcp.BuyingMode enum.

        Deletion oracle: drop buying_mode from the get_products signature and the
        presence assertion reddens; retype it to a hand-written Literal that drifts
        from adcp.BuyingMode and the enum-equality assertion reddens.
        """
        schema = Tool.from_function(get_products).parameters
        props = schema["properties"]

        assert "buying_mode" in props, (
            "MCP get_products must declare buying_mode; FastMCP's additionalProperties: false "
            "otherwise 400s the sole required field of get-products-request@3.1.1"
        )
        assert self._buying_mode_enum(props["buying_mode"], schema) == {m.value for m in BuyingMode}, (
            "MCP buying_mode enum drifted from adcp.BuyingMode; both REST and MCP must ground on "
            "the SDK enum so an adcp bump updates them together"
        )
