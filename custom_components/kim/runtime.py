"""Runtime glue between Home Assistant and the KIM engine.

``KimRuntime`` owns the :class:`Manager`, the coordinator that polls it, and
the services/conversation-agent that let a user talk to KIM from Assist.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .backends import create_backend
from .const import (
    ARTIFACT_DIR,
    BACKEND_MOCK,
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
    CONF_ROLES,
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
    SERVICE_CANCEL,
    SERVICE_COLLECT_ARTIFACTS,
    SERVICE_FETCH_FILE,
    SERVICE_NOTE,
    SERVICE_PAUSE,
    SERVICE_RESUME,
    SERVICE_RETRY,
    SERVICE_SUBMIT,
    UPDATE_INTERVAL,
)
from .manager import Manager
from .roles import RoleRegistry
from .scratchpad import Scratchpad
from .services import SCHEMAS

_LOGGER = logging.getLogger(__name__)


class KimRuntime:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.manager: Manager | None = None
        self.coordinator: DataUpdateCoordinator[dict[str, Any]] | None = None
        self.scratchpad: Scratchpad | None = None

    # ------------------------------------------------------------------
    def _build_manager(self) -> Manager:
        options = {**self.entry.data, **self.entry.options}
        backend_type = options.get(CONF_BACKEND, BACKEND_MOCK)
        backend = create_backend(
            backend_type,
            base_url=options.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            api_key=options.get(CONF_API_KEY, ""),
            workspace=options.get(CONF_WORKSPACE, DEFAULT_WORKSPACE),
            model=options.get(CONF_MODEL, DEFAULT_MODEL),
        )
        scratch_path = self.hass.config.path(f".storage/{DOMAIN}_{self.entry.entry_id}_notes.jsonl")
        scratchpad = Scratchpad(scratch_path)
        self.scratchpad = scratchpad
        registry = RoleRegistry(options.get(CONF_ROLES))
        # Cloud agents cost money and are only started when work arrives.
        lazy_pool = backend_type != BACKEND_MOCK

        def on_event(event: str, data: dict[str, Any]) -> None:
            self.hass.bus.async_fire(event, data)
            if self.coordinator is not None and self.manager is not None:
                self.coordinator.async_set_updated_data(self.manager.snapshot())

        return Manager(
            backend,
            scratchpad,
            registry=registry,
            model=options.get(CONF_MODEL, DEFAULT_MODEL),
            repository=options.get(CONF_REPOSITORY) or None,
            branch=options.get(CONF_BRANCH) or None,
            max_parallel=int(options.get(CONF_MAX_PARALLEL, DEFAULT_MAX_PARALLEL)),
            heartbeat_timeout=float(options.get(CONF_HEARTBEAT_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT)),
            claim_ttl=float(options.get(CONF_CLAIM_TTL, DEFAULT_CLAIM_TTL)),
            stall_timeout=float(options.get(CONF_STALL_TIMEOUT, DEFAULT_STALL_TIMEOUT)),
            max_retries=int(options.get(CONF_MAX_RETRIES, DEFAULT_MAX_RETRIES)),
            lazy_pool=lazy_pool,
            idle_worker_ttl=float(options.get(CONF_IDLE_WORKER_TTL, DEFAULT_IDLE_WORKER_TTL)),
            on_event=on_event,
        )

    async def async_start(self) -> None:
        self.manager = self._build_manager()
        if self.scratchpad is not None:
            await self.hass.async_add_executor_job(self.scratchpad.wait_ready)
        await self.manager.start()

        self.coordinator = DataUpdateCoordinator(
            self.hass,
            _LOGGER,
            name=f"{DOMAIN} manager",
            update_method=self._async_update,
            update_interval=UPDATE_INTERVAL,
            config_entry=self.entry,
        )
        await self.coordinator.async_config_entry_first_refresh()
        self._register_services()

    async def async_stop(self) -> None:
        if self.coordinator is not None:
            await self.coordinator.async_shutdown()
        if self.manager is not None:
            await self.manager.stop()
        if self.scratchpad is not None:
            await self.hass.async_add_executor_job(self.scratchpad.close)

    async def _async_update(self) -> dict[str, Any]:
        assert self.manager is not None
        await self.manager.tick()
        return self.manager.snapshot()

    # ------------------------------------------------------------------
    def _register_services(self) -> None:
        hass = self.hass
        manager = self.manager
        assert manager is not None

        async def _submit(call: ServiceCall) -> None:
            goal = call.data.get("goal") or call.data.get("text", "")
            if not goal:
                _LOGGER.warning("KIM submit called without a goal")
                return
            await manager.submit(goal, title=call.data.get("title"), plan_json=call.data.get("plan"))
            if self.coordinator is not None:
                await self.coordinator.async_request_refresh()

        async def _cancel(call: ServiceCall) -> None:
            await manager.cancel(call.data["task_id"])

        async def _retry(call: ServiceCall) -> None:
            await manager.retry(call.data["task_id"])

        async def _pause(call: ServiceCall) -> None:
            manager.pause()

        async def _resume(call: ServiceCall) -> None:
            await manager.resume()

        async def _note(call: ServiceCall) -> None:
            manager.note(
                call.data.get("author", "user"), call.data.get("text", ""), call.data.get("task_id")
            )

        async def _fetch_file(call: ServiceCall) -> None:
            data = await self._fetch_file(call.data["task_id"], call.data["path"],
                                          call.data.get("save_as"))
            self.hass.bus.async_fire(f"{DOMAIN}_artifact", data)

        async def _collect(call: ServiceCall) -> None:
            data = await self._collect_artifacts(call.data["task_id"])
            self.hass.bus.async_fire(f"{DOMAIN}_artifact", data)

        handlers = {
            SERVICE_SUBMIT: _submit,
            SERVICE_CANCEL: _cancel,
            SERVICE_RETRY: _retry,
            SERVICE_PAUSE: _pause,
            SERVICE_RESUME: _resume,
            SERVICE_NOTE: _note,
            SERVICE_FETCH_FILE: _fetch_file,
            SERVICE_COLLECT_ARTIFACTS: _collect,
        }
        for name, handler in handlers.items():
            hass.services.async_register(DOMAIN, name, handler, schema=SCHEMAS[name])

    # ------------------------------------------------------------------
    # artifacts
    # ------------------------------------------------------------------
    def _artifact_root(self) -> str:
        root = self.hass.config.path(ARTIFACT_DIR)
        os.makedirs(root, exist_ok=True)
        return root

    @staticmethod
    def _safe_name(name: str) -> str:
        """Keep a user-supplied file name inside the artifact directory."""
        name = os.path.basename(name.replace("\\", "/")).strip()
        return name or "artifact.bin"

    async def _fetch_file(self, task_id: str, path: str, save_as: str | None = None) -> dict[str, Any]:
        assert self.manager is not None
        content = await self.manager.fetch_task_file(task_id, path)
        target = self._safe_name(save_as or path)
        dest = os.path.join(self._artifact_root(), target)
        await self.hass.async_add_executor_job(_write_bytes, dest, content)
        return {"task_id": task_id, "path": path, "saved": f"/local/kim/{target}",
                "bytes": len(content)}

    async def _collect_artifacts(self, task_id: str) -> dict[str, Any]:
        assert self.manager is not None
        archive = await self.manager.fetch_task_archive(task_id)
        target = f"{task_id}.zip"
        dest = os.path.join(self._artifact_root(), target)
        await self.hass.async_add_executor_job(_write_bytes, dest, archive)
        return {"task_id": task_id, "saved": f"/local/kim/{target}", "bytes": len(archive)}


def _write_bytes(path: str, content: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(content)
