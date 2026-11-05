"""Sensors that expose KIM state to Home Assistant."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .runtime import KimRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: KimRuntime = hass.data[DOMAIN][entry.entry_id]
    coordinator = runtime.coordinator
    assert coordinator is not None

    entities: list[SensorEntity] = [
        KimStatusSensor(coordinator, entry),
        KimWorkersSensor(coordinator, entry),
        KimTasksSensor(coordinator, entry),
        KimScratchpadSensor(coordinator, entry),
    ]
    async_add_entities(entities)


class _KimBaseSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "KIM KI-Manager",
            "manufacturer": "KIM",
            "model": "Agent Orchestrator",
        }


class KimStatusSensor(_KimBaseSensor):
    _attr_name = "Status"
    _attr_icon = "mdi:robot"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "status")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        if data.get("paused"):
            return "paused"
        if data.get("running_tasks", 0) > 0:
            return "working"
        if data.get("tasks_pending", 0) > 0:
            return "scheduling"
        return "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "worker_count": data.get("worker_count", 0),
            "idle_workers": data.get("idle_workers", 0),
            "running_tasks": data.get("running_tasks", 0),
            "max_parallel": data.get("max_parallel", 0),
            "tasks_total": data.get("tasks_total", 0),
            "tasks_completed": data.get("tasks_completed", 0),
            "tasks_failed": data.get("tasks_failed", 0),
            "tasks_pending": data.get("tasks_pending", 0),
        }


class KimWorkersSensor(_KimBaseSensor):
    _attr_name = "Agenten"
    _attr_icon = "mdi:account-group"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "workers")

    @property
    def native_value(self) -> int:
        return (self.coordinator.data or {}).get("worker_count", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        workers = (self.coordinator.data or {}).get("workers", [])
        return {"workers": workers}


class KimTasksSensor(_KimBaseSensor):
    _attr_name = "Aufgaben"
    _attr_icon = "mdi:clipboard-list"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "tasks")

    @property
    def native_value(self) -> int:
        return (self.coordinator.data or {}).get("tasks_completed", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plans = (self.coordinator.data or {}).get("plans", [])
        return {"plans": plans}


class KimScratchpadSensor(_KimBaseSensor):
    _attr_name = "Notizzettel"
    _attr_icon = "mdi:note-text"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "scratchpad")

    @property
    def native_value(self) -> int:
        return len((self.coordinator.data or {}).get("notes", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"notes": (self.coordinator.data or {}).get("notes", [])}
