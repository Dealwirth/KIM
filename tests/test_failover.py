"""Tests for the free-first failover backend chain and the off-device archive."""
from __future__ import annotations

import asyncio
import json

import pytest

from custom_components.kim.archive import ArchiveStore
from custom_components.kim.backends import (
    BackendError,
    FailoverBackend,
    MockBackend,
    create_failover_backend,
    is_quota_error,
)
from custom_components.kim.const import BACKEND_MOCK, CONF_BACKEND, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
async def setup_homeassistant(hass):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "homeassistant", {})
    await hass.async_block_till_done()


class _FlakyBackend(MockBackend):
    """Mock that fails to start every worker while ``broken`` is set."""

    def __init__(self, name: str, *, broken: bool = False, quota: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.backend = name
        self.broken = broken
        self.quota = quota
        self.start_attempts = 0

    async def start_worker(self, role, model, repository=None, branch=None):
        self.start_attempts += 1
        if self.broken:
            message = "Daily sandbox limit of 20 reached" if self.quota else "boom"
            raise RuntimeError(message)
        return await super().start_worker(role, model, repository, branch)


def test_is_quota_error_matches_known_messages():
    assert is_quota_error("Daily sandbox limit of 20 reached")
    assert is_quota_error("HTTP 429 Too Many Requests")
    assert not is_quota_error("connection reset by peer")


@pytest.mark.asyncio
async def test_failover_uses_next_backend_on_quota():
    cloud = _FlakyBackend("cloud", broken=True)
    local = _FlakyBackend("local")
    chain = FailoverBackend([("cloud", cloud), ("local", local)], cooldown=3600)

    worker = await chain.start_worker("coder", "m")

    assert worker.backend == "local"
    assert chain.status()["active"] == "local"
    assert "cloud" in chain.status()["cooldowns"]


@pytest.mark.asyncio
async def test_cooldown_backend_is_not_retried_every_call():
    cloud = _FlakyBackend("cloud", broken=True)
    local = _FlakyBackend("local")
    chain = FailoverBackend([("cloud", cloud), ("local", local)], cooldown=3600)

    for _ in range(3):
        await chain.start_worker("coder", "m")

    assert cloud.start_attempts == 1  # cooled down after the first quota error
    assert local.start_attempts == 3


@pytest.mark.asyncio
async def test_worker_stays_with_its_owning_backend():
    cloud = _FlakyBackend("cloud")
    local = _FlakyBackend("local")
    chain = FailoverBackend([("cloud", cloud), ("local", local)], cooldown=3600)

    worker = await chain.start_worker("coder", "m")
    assert worker.backend == "cloud"
    await chain.send_task(worker, "hallo")

    assert cloud.prompts.get(str(worker.conversation_id)) == ["hallo"]
    assert not local.prompts


@pytest.mark.asyncio
async def test_chain_falls_back_to_mock_without_credentials():
    chain = create_failover_backend(base_url="", api_key="")
    assert [name for name, _ in chain.chain] == ["mock"]
    worker = await chain.start_worker("planner", "m")
    assert worker.backend == "mock"


@pytest.mark.asyncio
async def test_all_backends_down_raises():
    broken = _FlakyBackend("cloud", broken=True, quota=False)
    chain = FailoverBackend([("cloud", broken)], cooldown=3600)
    with pytest.raises(RuntimeError):
        await chain.start_worker("coder", "m")

    # a cooled-down chain reports the cooldown instead of hammering the backend
    chain._cool("cloud", 3600)
    with pytest.raises(BackendError):
        await chain.start_worker("coder", "m")


@pytest.mark.asyncio
async def test_create_backend_covers_every_backend_type():
    """Regression: 'auto' blew up in production because BACKEND_AUTO was unimported."""
    from custom_components.kim.backends import create_backend
    from custom_components.kim.const import (
        BACKEND_AUTO,
        BACKEND_CLOUD,
        BACKEND_LOCAL,
        BACKEND_MOCK,
    )

    assert create_backend(BACKEND_MOCK).backend == "mock"
    assert create_backend(BACKEND_LOCAL, local_url="http://localhost:3000").backend == "local"
    assert create_backend(BACKEND_CLOUD, api_key="x").backend == "cloud"
    auto = create_backend(BACKEND_AUTO, api_key="", local_url="")
    assert isinstance(auto, FailoverBackend)
    assert [name for name, _ in auto.chain] == ["mock"]
    auto_with_key = create_backend(BACKEND_AUTO, api_key="x", local_url="http://localhost:3000")
    assert [name for name, _ in auto_with_key.chain] == ["cloud", "local", "mock"]


@pytest.mark.asyncio
async def test_archive_local_roundtrip(tmp_path):
    store = ArchiveStore(repo="", token="", local_dir=str(tmp_path))
    result = await store.save_run("kim-run-1", {"plans": [{"title": "x"}], "notes": []})

    assert result["target"] == "lokal"
    names = [r["name"] for r in await store.list_runs()]
    assert "kim-run-1" in names
    loaded = await store.load_run("kim-run-1")
    assert loaded["plans"][0]["title"] == "x"


@pytest.mark.asyncio
async def test_archive_local_is_bounded(tmp_path):
    store = ArchiveStore(repo="", token="", local_dir=str(tmp_path))
    for i in range(40):
        await store.save_run(f"run-{i}", {"i": i})

    runs = await store.list_runs()
    assert len(runs) <= 25
    assert runs[-1]["name"] == "run-39"


@pytest.mark.asyncio
async def test_archive_without_repo_reports_local_target():
    store = ArchiveStore(repo="", token="")
    assert store.target == "lokal"
    assert not store.enabled


async def test_websocket_snapshot_and_submit(hass, hass_ws_client):
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_BACKEND: BACKEND_MOCK}, entry_id="kimws"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    await client.send_json({"id": 5, "type": "kim/snapshot", "entry_id": "kimws"})
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"]["worker_count"] == 6
    assert "backend" in msg["result"]

    await client.send_json(
        {"id": 6, "type": "kim/submit", "entry_id": "kimws", "goal": "Baue ein Login"}
    )
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["ok"]

    await client.send_json({"id": 7, "type": "kim/archive/save", "entry_id": "kimws"})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["ok"]

    await client.send_json({"id": 8, "type": "kim/archive/list", "entry_id": "kimws"})
    msg = await client.receive_json()
    assert msg["success"]
    assert len(msg["result"]["runs"]) >= 1

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_websocket_unknown_entry_errors(hass, hass_ws_client):
    client = await hass_ws_client(hass)
    await client.send_json({"id": 9, "type": "kim/snapshot", "entry_id": "nope"})
    msg = await client.receive_json()
    assert not msg["success"]
