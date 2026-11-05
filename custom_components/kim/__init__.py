"""KIM - KI Manager for Home Assistant.

A hub integration that runs a pool of OpenHands agents which work on a shared
task graph in parallel, coordinate through a locked shared scratchpad, and are
supervised (assignment, heartbeat, failover) by a manager.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .manager import Manager
from .panel import async_register_frontend, async_register_panel
from .runtime import KimRuntime
from .websocket import async_register as async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "conversation"]

__all__ = ["DOMAIN", "Manager", "KimRuntime", "async_setup", "async_setup_entry",
           "async_unload_entry"]

MANIFEST_VERSION = "0.3.0"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up KIM from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    runtime = KimRuntime(hass, entry)
    await runtime.async_start()
    hass.data[DOMAIN][entry.entry_id] = runtime

    async_register_websocket(hass)
    try:
        await async_register_frontend(hass, MANIFEST_VERSION)
    except Exception:  # noqa: BLE001 - a missing frontend must not block the engine
        _LOGGER.exception("KIM: Frontend konnte nicht bereitgestellt werden")
    async_register_panel(hass, entry.entry_id, MANIFEST_VERSION)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime: KimRuntime | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if runtime is not None:
        await runtime.async_stop()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
