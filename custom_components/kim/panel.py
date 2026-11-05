"""Register the KIM sidebar panel and serve its frontend assets."""
from __future__ import annotations

import logging
import os

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")
PANEL_URL = "/kim"
MODULE = "kim-panel.js"


async def async_register_frontend(hass: HomeAssistant, version: str) -> None:
    """Serve ``frontend/`` under ``/kim`` and add the sidebar panel."""
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_URL, FRONTEND_DIR, cache_headers=False)]
        )
    except ImportError:  # pragma: no cover - older HA
        hass.http.register_static_path(PANEL_URL, FRONTEND_DIR, cache_headers=False)


def async_register_panel(hass: HomeAssistant, entry_id: str, version: str) -> None:
    """Add the KIM panel to the Home Assistant sidebar (idempotent)."""
    try:
        from homeassistant.components import panel_custom
    except ImportError:  # pragma: no cover - panel is optional
        _LOGGER.warning("KIM: panel_custom fehlt, Panel wird uebersprungen")
        return

    hass.async_create_task(
        panel_custom.async_register_panel(
            hass,
            webcomponent_name="kim-panel",
            frontend_url_path="kim",
            module_url=f"{PANEL_URL}/{MODULE}?v={version}",
            sidebar_title="KIM",
            sidebar_icon="mdi:crown",
            config={"entry_id": entry_id},
            require_admin=False,
            embed_iframe=False,
        )
    )
