"""WebSocket API for the KIM panel.

Gives the frontend live, typed access to the manager without polling the
sensors: snapshot, archive listing/loading and a one-call ``submit``.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _runtime(hass: HomeAssistant, entry_id: str | None):
    entries: dict[str, Any] = hass.data.get(DOMAIN, {})
    if entry_id and entry_id in entries:
        return entries[entry_id]
    if entries:
        return next(iter(entries.values()))
    return None


def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_snapshot)
    websocket_api.async_register_command(hass, ws_submit)
    websocket_api.async_register_command(hass, ws_archive_list)
    websocket_api.async_register_command(hass, ws_archive_save)
    websocket_api.async_register_command(hass, ws_archive_load)


@websocket_api.websocket_command(
    {vol.Required("type"): "kim/snapshot", vol.Optional("entry_id"): str}
)
@websocket_api.async_response
async def ws_snapshot(hass: HomeAssistant, connection, msg) -> None:
    runtime = _runtime(hass, msg.get("entry_id"))
    if runtime is None or runtime.manager is None:
        connection.send_error(msg["id"], "not_loaded", "KIM ist nicht geladen")
        return
    snapshot = runtime.manager.snapshot()
    snapshot["archive_target"] = runtime.archive.target if runtime.archive else "?"
    connection.send_result(msg["id"], snapshot)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kim/submit",
        vol.Required("goal"): str,
        vol.Optional("title"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_submit(hass: HomeAssistant, connection, msg) -> None:
    runtime = _runtime(hass, msg.get("entry_id"))
    if runtime is None or runtime.manager is None:
        connection.send_error(msg["id"], "not_loaded", "KIM ist nicht geladen")
        return
    await runtime.manager.submit(msg["goal"], title=msg.get("title"))
    if runtime.coordinator is not None:
        await runtime.coordinator.async_request_refresh()
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {vol.Required("type"): "kim/archive/list", vol.Optional("entry_id"): str}
)
@websocket_api.async_response
async def ws_archive_list(hass: HomeAssistant, connection, msg) -> None:
    runtime = _runtime(hass, msg.get("entry_id"))
    if runtime is None or runtime.archive is None:
        connection.send_error(msg["id"], "not_loaded", "KIM ist nicht geladen")
        return
    connection.send_result(
        msg["id"], {"target": runtime.archive.target, "runs": await runtime.archive.list_runs()}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kim/archive/save",
        vol.Optional("name"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_archive_save(hass: HomeAssistant, connection, msg) -> None:
    runtime = _runtime(hass, msg.get("entry_id"))
    if runtime is None:
        connection.send_error(msg["id"], "not_loaded", "KIM ist nicht geladen")
        return
    result = await runtime._archive_run()
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kim/archive/load",
        vol.Required("name"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_archive_load(hass: HomeAssistant, connection, msg) -> None:
    runtime = _runtime(hass, msg.get("entry_id"))
    if runtime is None or runtime.archive is None:
        connection.send_error(msg["id"], "not_loaded", "KIM ist nicht geladen")
        return
    data = await runtime.archive.load_run(msg["name"])
    connection.send_result(msg["id"], {"ok": data is not None, "name": msg["name"], "run": data})
