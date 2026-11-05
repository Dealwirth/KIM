"""Config flow for KIM."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    BACKEND_CLOUD,
    BACKEND_MOCK,
    BACKENDS,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_BASE_URL,
    CONF_BRANCH,
    CONF_CLAIM_TTL,
    CONF_HEARTBEAT_TIMEOUT,
    CONF_IDLE_WORKER_TTL,
    CONF_MAX_PARALLEL,
    CONF_MAX_RETRIES,
    CONF_MODEL,
    CONF_REPOSITORY,
    CONF_STALL_TIMEOUT,
    CONF_WORKSPACE,
    DEFAULT_BASE_URL,
    DEFAULT_CLAIM_TTL,
    DEFAULT_HEARTBEAT_TIMEOUT,
    DEFAULT_IDLE_WORKER_TTL,
    DEFAULT_MAX_PARALLEL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MODEL,
    DEFAULT_STALL_TIMEOUT,
    DEFAULT_WORKSPACE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def _validate_cloud(user_input: dict[str, Any]) -> dict[str, str]:
    """Reject a cloud setup whose API key cannot be used."""
    base_url = (user_input.get(CONF_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    api_key = user_input.get(CONF_API_KEY, "")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.get(
                f"{base_url}/api/v1/app-conversations/search?limit=1",
                headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status in (401, 403):
                    return {CONF_API_KEY: "invalid_auth"}
                if resp.status >= 400:
                    return {"base_url": "cannot_connect"}
    except Exception:  # noqa: BLE001 - network problems
        return {"base_url": "cannot_connect"}
    return {}


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_BACKEND, default=defaults.get(CONF_BACKEND, BACKEND_CLOUD)): vol.In(
                BACKENDS
            ),
            vol.Optional(
                CONF_BASE_URL, default=defaults.get(CONF_BASE_URL, DEFAULT_BASE_URL)
            ): str,
            vol.Optional(CONF_API_KEY, default=defaults.get(CONF_API_KEY, "")): str,
            vol.Optional(CONF_MODEL, default=defaults.get(CONF_MODEL, DEFAULT_MODEL)): str,
            vol.Optional(CONF_REPOSITORY, default=defaults.get(CONF_REPOSITORY, "")): str,
            vol.Optional(CONF_BRANCH, default=defaults.get(CONF_BRANCH, "")): str,
            vol.Optional(
                CONF_WORKSPACE, default=defaults.get(CONF_WORKSPACE, DEFAULT_WORKSPACE)
            ): str,
            vol.Optional(
                CONF_MAX_PARALLEL, default=defaults.get(CONF_MAX_PARALLEL, DEFAULT_MAX_PARALLEL)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=10, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_HEARTBEAT_TIMEOUT,
                default=defaults.get(CONF_HEARTBEAT_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=30, max=3600, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_STALL_TIMEOUT, default=defaults.get(CONF_STALL_TIMEOUT, DEFAULT_STALL_TIMEOUT)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=60, max=7200, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_CLAIM_TTL, default=defaults.get(CONF_CLAIM_TTL, DEFAULT_CLAIM_TTL)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=60, max=7200, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_MAX_RETRIES, default=defaults.get(CONF_MAX_RETRIES, DEFAULT_MAX_RETRIES)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=5, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_IDLE_WORKER_TTL,
                default=defaults.get(CONF_IDLE_WORKER_TTL, DEFAULT_IDLE_WORKER_TTL),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=60, max=86400, mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
    )


class KimConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the KIM config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_BACKEND) == BACKEND_CLOUD:
                if not user_input.get(CONF_API_KEY):
                    errors[CONF_API_KEY] = "api_key_required"
                else:
                    errors.update(await _validate_cloud(user_input))
            if not errors:
                return self.async_create_entry(title="KIM", data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return KimOptionsFlow()


class KimOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(defaults))
