"""Locate + load JSON schemas bundled inside the INSTALLED ``adcp`` wheel.

Single locator for the ``adcp/_schemas`` tree shipped inside the pinned ``adcp``
package, so callers don't each re-derive the wheel path (``importlib.resources.files``
vs ``os.path.dirname(adcp.__file__)``). Resolves against the installed wheel
regardless of the working directory.

NOTE: distinct from ``tests.helpers.pinned_schema``, which reads the *vendored*
fixture tree under ``tests/fixtures/adcp_schemas_pinned/``. This module reads the
schemas shipped inside the installed ``adcp`` package itself.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


def load_installed_adcp_schema(*relative_parts: str) -> dict[str, Any]:
    """Load an adcp-bundled JSON Schema by its path under the wheel's ``_schemas`` tree.

    ``relative_parts`` are the path segments below ``adcp/_schemas``. Examples::

        load_installed_adcp_schema("3.1", "bundled", "media-buy", "get-media-buys-response.json")
        load_installed_adcp_schema("3.1", "core", "placement.json")
    """
    resource = files("adcp").joinpath("_schemas", *relative_parts)
    return json.loads(resource.read_text(encoding="utf-8"))
