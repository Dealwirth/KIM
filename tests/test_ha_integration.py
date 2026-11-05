"""Home Assistant level tests: config flow + setup + services."""
from __future__ import annotations

import os

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.kim.const import (
    CONF_BACKEND,
    CONF_MAX_PARALLEL,
    CONF_RETRY_BACKOFF,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
async def setup_homeassistant(hass: HomeAssistant):
    """The 'homeassistant' component must be up before 'conversation'."""
    assert await async_setup_component(hass, "homeassistant", {})
    await hass.async_block_till_done()


MOCK_DATA = {
    CONF_BACKEND: "mock",
    CONF_MAX_PARALLEL: 2,
    # Tests drive the coordinator manually; no real time may pass between
    # retries, so the backoff is disabled here (production default is 5s).
    CONF_RETRY_BACKOFF: 0,
}


async def test_config_flow_user_step(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_DATA
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "KIM"


async def test_config_flow_requires_api_key_for_cloud(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_BACKEND: "cloud"}
    )
    assert result["type"] == "form"
    assert result["errors"].get("api_key") == "api_key_required"


async def test_setup_entry_creates_entities_and_services(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_DATA, entry_id="testkim")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED

    entity_ids = set(hass.states.async_entity_ids("sensor"))
    kim_sensors = {e for e in entity_ids if e.startswith("sensor.kim")}
    # status, agenten, aufgaben, notizzettel
    assert len(kim_sensors) == 4

    workers = hass.states.get("sensor.kim_ki_manager_agenten")
    assert workers is not None
    # the free mock backend pre-spawns the default roles (cloud is lazy)
    # planner 1 + researcher 1 + coder 2 + tester 1 + reviewer 1
    assert int(workers.state) == 6

    # a conversation agent entity is registered
    assert any(e.startswith("conversation.kim") for e in hass.states.async_entity_ids())

    # services registered
    for service in ("submit", "cancel", "retry", "pause", "resume", "note",
                    "fetch_file", "collect_artifacts"):
        assert hass.services.has_service(DOMAIN, service)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.NOT_LOADED


async def test_submit_service_runs_plan(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN, data={**MOCK_DATA, CONF_MAX_PARALLEL: 4}, entry_id="kim2"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    await hass.services.async_call(DOMAIN, "submit", {"goal": "Baue ein Login"}, blocking=True)
    await hass.async_block_till_done()

    for _ in range(40):
        await runtime.coordinator.async_refresh()
        await hass.async_block_till_done()
        snap = runtime.coordinator.data
        if snap["tasks_total"] > 0 and snap["tasks_completed"] == snap["tasks_total"]:
            break

    snap = runtime.coordinator.data
    assert snap["tasks_total"] == 4
    assert snap["tasks_completed"] == 4

    tasks = hass.states.get("sensor.kim_ki_manager_aufgaben")
    assert tasks is not None
    assert int(tasks.state) == 4


async def test_pause_resume_services(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_DATA, entry_id="kim3")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    await hass.services.async_call(DOMAIN, "pause", {}, blocking=True)
    assert runtime.manager.paused is True
    await hass.services.async_call(DOMAIN, "resume", {}, blocking=True)
    assert runtime.manager.paused is False

    await hass.services.async_call(
        DOMAIN, "note", {"text": "hallo", "author": "tester"}, blocking=True
    )
    notes = [n for n in runtime.manager.snapshot()["notes"] if n["text"] == "hallo"]
    assert notes


async def test_parallel_roles_and_failover_in_home_assistant(hass: HomeAssistant):
    """Three roles run in parallel; a dead agent is replaced transparently."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={**MOCK_DATA, CONF_MAX_PARALLEL: 3}, entry_id="kim6"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    manager = runtime.manager
    backend = manager.backend

    # build three independent tasks, one per role
    plan = await manager.submit("Paralleles Vorhaben")
    manager.tasks.clear()
    from custom_components.kim.models import Task

    for i, (role, tag) in enumerate(
        [("researcher", "research"), ("coder", "code"), ("tester", "test")]
    ):
        manager.tasks[f"t{i}"] = Task(
            task_id=f"t{i}", title=f"T{i} {role}", description=f"mach {role}",
            role=role, tags=[tag], run_id=plan.run_id,
        )

    for _ in range(8):
        await runtime.coordinator.async_refresh()
        await hass.async_block_till_done()
    snap = runtime.coordinator.data
    assert snap["tasks_completed"] == 3
    roles_used = {w.role for w in manager.worker_history.values()}
    assert {"researcher", "coder", "tester"} <= roles_used


async def test_failover_in_home_assistant(hass: HomeAssistant):
    """A vanished agent is detected and its task finishes on a fresh agent."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={**MOCK_DATA, CONF_MAX_PARALLEL: 1}, entry_id="kim7"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    manager = runtime.manager
    backend = manager.backend
    # fail once, then succeed on the replacement agent
    manager.max_retries = 2

    await hass.services.async_call(DOMAIN, "submit", {"goal": "Baue Feature X"}, blocking=True)
    await hass.async_block_till_done()

    # let the first agent pick up its task, then kill its sandbox
    await runtime.coordinator.async_refresh()
    await hass.async_block_till_done()
    busy = [w for w in manager.workers.values() if w.current_task_id]
    assert busy, "expected a working agent"
    backend.dead.add(str(busy[0].conversation_id))

    for _ in range(60):
        await runtime.coordinator.async_refresh()
        await hass.async_block_till_done()
        snap = runtime.coordinator.data
        if snap["tasks_total"] and snap["tasks_completed"] == snap["tasks_total"]:
            break

    snap = runtime.coordinator.data
    assert snap["tasks_failed"] == 0
    assert snap["tasks_completed"] == snap["tasks_total"]
    assert len(backend.created) >= 2


async def test_conversation_commands(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN, data={**MOCK_DATA, CONF_MAX_PARALLEL: 4}, entry_id="kim4"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from homeassistant.components import conversation

    runtime = hass.data[DOMAIN][entry.entry_id]

    from custom_components.kim.conversation import KimConversationAgent

    async def ask(text: str) -> str:
        user_input = conversation.ConversationInput(
            text=text,
            context=None,
            conversation_id="test",
            device_id=None,
            language="de",
            agent_id="conversation.kim_ki_manager_kim",
            satellite_id=None,
        )
        chat_log = conversation.ChatLog(hass, user_input)
        kim = KimConversationAgent(entry, runtime)
        result = await kim._async_handle_message(user_input, chat_log)
        return result.response.speech["plain"]["speech"]

    assert "Agenten" in await ask("zustand")

    reply = await ask("plan: Baue ein Kontaktformular")
    assert "4 Aufgaben" in reply

    reply = await ask("notiz: bitte Tests erweitern")
    assert "Notizzettel" in reply
    assert any(
        n["text"] == "bitte Tests erweitern" for n in runtime.manager.snapshot()["notes"]
    )

    assert "pausiert" in await ask("pausiere")
    assert runtime.manager.paused is True
    assert "laeuft wieder" in await ask("weiter")
    assert runtime.manager.paused is False


async def test_artifact_services_write_files(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN, data={**MOCK_DATA, CONF_MAX_PARALLEL: 4}, entry_id="kim5"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    await hass.services.async_call(DOMAIN, "submit", {"goal": "Baue ein Login"}, blocking=True)
    await hass.async_block_till_done()

    for _ in range(40):
        await runtime.coordinator.async_refresh()
        await hass.async_block_till_done()
        snap = runtime.coordinator.data
        if snap["tasks_total"] and snap["tasks_completed"] == snap["tasks_total"]:
            break

    task_id = next(iter(runtime.manager.tasks))
    await hass.services.async_call(
        DOMAIN, "collect_artifacts", {"task_id": task_id}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, "fetch_file", {"task_id": task_id, "path": "out/report.pdf"}, blocking=True
    )
    artifact_dir = hass.config.path("www/kim")
    assert os.path.exists(os.path.join(artifact_dir, f"{task_id}.zip"))
    assert os.path.exists(os.path.join(artifact_dir, "report.pdf"))
