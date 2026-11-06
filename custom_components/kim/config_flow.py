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
    BACKEND_AUTO,
    BACKEND_CLOUD,
    BACKENDS,
    CONF_API_KEY,
    CONF_ARCHIVE_REPO,
    CONF_BACKEND,
    CONF_BASE_URL,
    CONF_LOCAL_URL,
    CONF_MAX_PARALLEL,
    CONF_MODEL,
    CONF_RETRY_BACKOFF,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_PARALLEL,
    DEFAULT_MODEL,
    DEFAULT_RETRY_BACKOFF,
    DOMAIN,
    KNOWN_MODELS,
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


def _model_options(current: str | None) -> list[str]:
    """Known models, always including the currently configured one.

    A dropdown prevents picking a model the account cannot use (the mistake
    that silently broke every agent), while still accepting a custom model
    that was configured earlier.
    """
    options = list(KNOWN_MODELS)
    if current and current not in options:
        options.insert(0, current)
    return options


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Only the few settings that matter. Everything else has sane defaults.

    ``auto`` (the default) chains Cloud -> Local -> Mock for free, so a single
    API key is enough and the system never stops when the free quota runs out.
    """
    return vol.Schema(
        {
            vol.Required(CONF_BACKEND, default=defaults.get(CONF_BACKEND, BACKEND_AUTO)): vol.In(
                BACKENDS
            ),
            vol.Optional(CONF_API_KEY, default=defaults.get(CONF_API_KEY, "")): str,
            vol.Optional(
                CONF_BASE_URL, default=defaults.get(CONF_BASE_URL, DEFAULT_BASE_URL)
            ): str,
            vol.Optional(
                CONF_LOCAL_URL, default=defaults.get(CONF_LOCAL_URL, "")
            ): str,
            vol.Optional(CONF_MODEL, default=defaults.get(CONF_MODEL, DEFAULT_MODEL)): vol.In(
                _model_options(defaults.get(CONF_MODEL))
            ),
            vol.Optional(
                CONF_MAX_PARALLEL, default=defaults.get(CONF_MAX_PARALLEL, DEFAULT_MAX_PARALLEL)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=10, mode=selector.NumberSelectorMode.SLIDER)
            ),
            vol.Optional(
                CONF_ARCHIVE_REPO, default=defaults.get(CONF_ARCHIVE_REPO, "")
            ): str,
            vol.Optional(
                CONF_RETRY_BACKOFF,
                default=defaults.get(CONF_RETRY_BACKOFF, DEFAULT_RETRY_BACKOFF),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=300, mode=selector.NumberSelectorMode.BOX)
            ),
        }
    )


class KimConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the KIM config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            backend = user_input.get(CONF_BACKEND)
            if backend in (BACKEND_CLOUD, BACKEND_AUTO):
                # 'auto' works without a key (falls back to local/mock), but a
                # key is validated when given so typos surface immediately.
                if not user_input.get(CONF_API_KEY) and backend == BACKEND_CLOUD:
                    errors[CONF_API_KEY] = "api_key_required"
                elif user_input.get(CONF_API_KEY):
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
