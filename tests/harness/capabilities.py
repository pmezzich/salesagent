"""CapabilitiesEnv — integration test environment for get_adcp_capabilities.

Nothing external is mocked: capabilities is a read-only discovery call whose
whole answer is derived from the tenant row, its publisher partnerships and the
bound ad-server adapter. Those all live in the real database, so the env seeds a
tenant/principal via factories (``ad_server="mock"`` → ``MockAdServer``) and lets
production resolve the adapter for real. The one scenario-scoped override is
``set_adapter_pricing_models`` (degrade partitions) — it pins the resolved
adapter's declared pricing surface without touching adapter resolution itself.

Transport coverage: A2A (``get_adcp_capabilities`` skill), MCP
(``get_adcp_capabilities`` tool), and REST. The REST route is
``GET /api/v1/capabilities`` — the only harness endpoint that is not a POST —
so this env derives the verb from the request: the parameterless discovery call
GETs, a request carrying a body POSTs it. ``build_rest_body`` records whether a
body was built and the ``REST_METHOD`` property reads that flag, so the
in-process and e2e dispatchers share one source of truth for the verb (precedent:
the ``REST_METHOD``/``REST_ENDPOINT`` properties on ``media_buy_dual.py``).

Usage::

    with CapabilitiesEnv() as env:
        env.setup_default_data()
        result = env.call_via(Transport.MCP)
        assert result.payload.media_buy.supported_pricing_models
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from adcp.types import GetAdcpCapabilitiesResponse

from tests.harness._base import IntegrationEnv
from tests.harness._realize import e2e_unsupported, realize_e2e


class CapabilitiesEnv(IntegrationEnv):
    """Integration test environment for ``_get_adcp_capabilities_impl``."""

    EXTERNAL_PATCHES: dict[str, str] = {}
    REST_ENDPOINT = "/api/v1/capabilities"
    # Whether the last-built REST body carried request params. Set by
    # ``build_rest_body`` (which both dispatch paths call first) and read by the
    # ``REST_METHOD`` property to derive the verb — a single source of truth for
    # the in-process and e2e dispatchers, instead of a hand-synced constant.
    _rest_has_body: bool = False

    @realize_e2e(
        e2e_unsupported(
            "the live stack resolves the tenant's real bound adapter, whose pricing "
            "surface is fixed production code — a degenerate or off-enum adapter "
            "cannot be injected over e2e"
        )
    )
    def set_adapter_pricing_models(self, models: set[str]) -> None:
        """Pin what the bound (mock) adapter reports as its pricing surface.

        The degrade partitions of POST-S10 need an adapter that reports nothing
        or off-enum strings; production still resolves the REAL ``MockAdServer``
        (``EXTERNAL_PATCHES`` stays empty), only its declared pricing surface is
        overridden. The patch rides ``self._patchers`` so ``__exit__`` stops it
        with the base teardown — no bleed into sibling scenarios.
        """
        from src.adapters.mock_ad_server import MockAdServer

        patcher = patch.object(MockAdServer, "get_supported_pricing_models", return_value=set(models))
        patcher.start()
        self._patchers.append(patcher)

    def call_impl(self, **kwargs: Any) -> GetAdcpCapabilitiesResponse:
        """Call ``_get_adcp_capabilities_impl`` directly (no wire)."""
        from src.core.tools.capabilities import _get_adcp_capabilities_impl

        self._commit_factory_data()
        kwargs.setdefault("identity", self.identity)
        kwargs.setdefault("req", None)
        return _get_adcp_capabilities_impl(kwargs["req"], kwargs["identity"])

    def call_a2a(self, **kwargs: Any) -> GetAdcpCapabilitiesResponse:
        """Call the get_adcp_capabilities skill via the real AdCPRequestHandler."""
        return self._run_a2a_handler("get_adcp_capabilities", GetAdcpCapabilitiesResponse, **kwargs)

    def call_mcp(self, **kwargs: Any) -> GetAdcpCapabilitiesResponse:
        """Call the get_adcp_capabilities tool via Client(mcp) — full pipeline."""
        return self._run_mcp_client("get_adcp_capabilities", GetAdcpCapabilitiesResponse, **kwargs)

    def build_rest_body(self, **kwargs: Any) -> dict[str, Any]:
        """Build the REST body and record whether the request carried params.

        Capabilities discovery is parameterless today (``req=None`` → ``{}``), so
        the recorded flag drives ``REST_METHOD`` to a bodyless GET. A future
        parameterized request (protocols filter, context echo, version) yields a
        non-empty body and POSTs it — the verb follows the request, not a
        hand-synced constant.
        """
        body = super().build_rest_body(**kwargs)
        self._rest_has_body = bool(body)
        return body

    @property
    def REST_METHOD(self) -> str:  # noqa: N802 — dispatcher reads getattr(env, "REST_METHOD", "post")
        """Verb derived from the request: POST when it carries a body, else GET.

        ``RestE2EDispatcher`` (which never calls ``_run_rest_request``) reads this
        AFTER it calls ``build_rest_body``, so the flag is current; the in-process
        ``_run_rest_request`` reads the same property, so the two dispatch paths
        can never disagree on the verb.
        """
        return "post" if self._rest_has_body else "get"

    def _run_rest_request(self, endpoint: str, **kwargs: Any) -> Any:
        """Dispatch capabilities over REST with the request-derived verb.

        The inherited implementation always POSTs a JSON body; ``/api/v1/capabilities``
        is a parameterless GET today, so a blind POST would 405. Build the body
        (which sets the verb), then GET the parameterless route or POST the body
        when params are present. Everything before the verb (identity pop, factory
        commit, auth-dep override) is reused via ``_prepare_rest_request``.
        """
        client, _identity = self._prepare_rest_request(kwargs)
        body = self.build_rest_body(**kwargs)
        if self.REST_METHOD == "get":
            return client.get(endpoint)
        return client.post(endpoint, json=body)

    def parse_rest_response(self, data: dict[str, Any]) -> GetAdcpCapabilitiesResponse:
        """Parse REST JSON into GetAdcpCapabilitiesResponse."""
        return GetAdcpCapabilitiesResponse(**data)
