"""Integration tests for the KIM manager using the mock backend."""
from __future__ import annotations

import json

import pytest

from custom_components.kim.backends import MockBackend
from custom_components.kim.const import (
    EVENT_TASK_DONE,
    STATE_IDLE,
    STATE_OFFLINE,
    TASK_DONE,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_PENDING,
)
from custom_components.kim.manager import Manager
from custom_components.kim.roles import RoleRegistry
from custom_components.kim.scratchpad import Scratchpad


def build_manager(tmp_path, *, max_parallel=2, roles=None, completion_delay=1,
                  fail_roles=None, events=None, lazy_pool=False, idle_worker_ttl=0):
    backend = MockBackend(completion_delay=completion_delay, fail_roles=fail_roles)
    pad = Scratchpad(str(tmp_path / "notes.jsonl"))
    registry = RoleRegistry(roles)
    collected: list[tuple[str, dict]] = events if events is not None else []

    def on_event(name, data):
        collected.append((name, data))

    manager = Manager(
        backend,
        pad,
        registry=registry,
        max_parallel=max_parallel,
        heartbeat_timeout=1000,
        stall_timeout=1000,
        lazy_pool=lazy_pool,
        idle_worker_ttl=idle_worker_ttl,
        on_event=on_event,
    )
    return manager, backend, pad


@pytest.mark.asyncio
async def test_start_creates_worker_per_role_instance(tmp_path):
    roles = [
        {"name": "coder", "tags": ["code"], "instances": 2},
        {"name": "tester", "tags": ["test"], "instances": 1},
    ]
    manager, backend, _ = build_manager(tmp_path, roles=roles)
    await manager.start()
    assert len(manager.workers) == 3
    assert len(backend.created) == 3
    assert manager.snapshot()["worker_count"] == 3


@pytest.mark.asyncio
async def test_submit_runs_plan_to_completion(tmp_path):
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review", "qa"], "instances": 2}]
    manager, _, pad = build_manager(tmp_path, roles=roles, completion_delay=1)
    await manager.start()
    plan = await manager.submit("Baue Feature X")
    assert len(plan.tasks) == 4

    # drive the scheduler until every task is terminal
    for _ in range(50):
        await manager.tick()
        if all(t.state in (TASK_DONE, TASK_FAILED) for t in plan.tasks):
            break

    assert all(t.state == TASK_DONE for t in plan.tasks)
    results = pad.results()
    assert len(results) == 4


@pytest.mark.asyncio
async def test_parallelism_limit_is_respected(tmp_path):
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review"], "instances": 5}]
    # tasks only complete after 3 polls, so several stay in-flight at once
    manager, _, _ = build_manager(tmp_path, roles=roles, max_parallel=2, completion_delay=3)
    await manager.start()
    await manager.submit("Grosses Ziel")

    await manager.tick()
    # never more than max_parallel workers busy
    running = sum(1 for w in manager.workers.values() if w.current_task_id)
    assert running <= 2

    for _ in range(60):
        await manager.tick()
        running = sum(1 for w in manager.workers.values() if w.current_task_id)
        assert running <= 2
        if manager.snapshot()["tasks_pending"] == 0 and running == 0:
            break


@pytest.mark.asyncio
async def test_dependencies_enforced(tmp_path):
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review"], "instances": 4}]
    manager, _, _ = build_manager(tmp_path, roles=roles, max_parallel=4, completion_delay=2)
    await manager.start()
    plan = await manager.submit("Ziel")
    # first tick: only the first task (no deps) may be assigned
    await manager.tick()
    assigned = [t for t in plan.tasks if t.state in (TASK_IN_PROGRESS, TASK_DONE)]
    assert len(assigned) == 1
    assert assigned[0] is plan.tasks[0]


@pytest.mark.asyncio
async def test_failing_worker_requeues_then_fails(tmp_path):
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review"], "instances": 1}]
    manager, _, _ = build_manager(
        tmp_path, roles=roles, max_parallel=1, fail_roles=["coder"], completion_delay=1
    )
    await manager.start()
    plan = await manager.submit("Ziel")
    for _ in range(20):
        await manager.tick()
        if plan.tasks[0].state == TASK_FAILED:
            break
    assert plan.tasks[0].state == TASK_FAILED
    assert plan.tasks[0].attempts >= 1


@pytest.mark.asyncio
async def test_events_are_emitted(tmp_path):
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review"], "instances": 2}]
    events: list[tuple[str, dict]] = []
    manager, _, _ = build_manager(tmp_path, roles=roles, events=events)
    await manager.start()
    plan = await manager.submit("Ziel")
    for _ in range(50):
        await manager.tick()
        if all(t.state == TASK_DONE for t in plan.tasks):
            break
    done_events = [e for e in events if e[0] == EVENT_TASK_DONE]
    assert len(done_events) == 4


@pytest.mark.asyncio
async def test_failover_on_offline_worker(tmp_path):
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review"], "instances": 1}]
    manager, _, _ = build_manager(tmp_path, roles=roles, max_parallel=1, completion_delay=100)
    await manager.start()
    plan = await manager.submit("Ziel")
    await manager.tick()
    task = plan.tasks[0]
    assert task.state == TASK_IN_PROGRESS

    # simulate the agent going silent past the heartbeat timeout
    worker = manager.workers[task.assigned_to]
    worker.last_heartbeat = 0
    manager.heartbeat_timeout = 0  # force the timeout check to trigger
    await manager.tick()

    assert worker.state == STATE_OFFLINE
    assert task.assigned_to is None
    assert task.state in (TASK_PENDING, TASK_FAILED)


@pytest.mark.asyncio
async def test_pause_and_resume(tmp_path):
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review"], "instances": 1}]
    manager, _, _ = build_manager(tmp_path, roles=roles)
    await manager.start()
    await manager.submit("Ziel")
    manager.pause()
    assert manager.snapshot()["paused"] is True
    await manager.resume()
    assert manager.snapshot()["paused"] is False


@pytest.mark.asyncio
async def test_cancel_task(tmp_path):
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review"], "instances": 1}]
    manager, _, _ = build_manager(tmp_path, roles=roles, completion_delay=100)
    await manager.start()
    plan = await manager.submit("Ziel")
    await manager.tick()
    task = plan.tasks[0]
    assert await manager.cancel(task.task_id)
    assert task.state == "cancelled"


@pytest.mark.asyncio
async def test_retry_failed_task(tmp_path):
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review"], "instances": 1}]
    manager, _, _ = build_manager(
        tmp_path, roles=roles, max_parallel=1, fail_roles=["coder"], completion_delay=1
    )
    await manager.start()
    plan = await manager.submit("Ziel")
    for _ in range(20):
        await manager.tick()
        if plan.tasks[0].state == TASK_FAILED:
            break
    assert await manager.retry(plan.tasks[0].task_id)
    assert plan.tasks[0].state in (TASK_PENDING, TASK_IN_PROGRESS, TASK_DONE)


@pytest.mark.asyncio
async def test_snapshot_shape(tmp_path):
    manager, _, _ = build_manager(tmp_path)
    await manager.start()
    snap = manager.snapshot()
    for key in ("workers", "worker_count", "running_tasks", "max_parallel",
                "tasks_total", "tasks_completed", "tasks_failed", "notes", "plans"):
        assert key in snap
    # session keys must never leak
    for worker in snap["workers"]:
        assert "session_api_key" not in worker
    # heartbeats are noise and must not show up in the reported notes
    assert all(n["kind"] != "heartbeat" for n in snap["notes"])


@pytest.mark.asyncio
async def test_lazy_pool_starts_nothing_until_needed(tmp_path):
    """A freshly loaded manager must not spawn any (paid) agent."""
    roles = [
        {"name": "coder", "tags": ["code", "research", "test", "review", "qa"], "instances": 2},
        {"name": "tester", "tags": ["test"], "instances": 1},
    ]
    manager, backend, _ = build_manager(tmp_path, roles=roles, lazy_pool=True)
    await manager.start()
    assert manager.workers == {}
    assert backend.created == []

    # the first submit grows the pool just enough to work
    plan = await manager.submit("Baue Feature X")
    assert len(plan.tasks) == 4
    assert len(manager.workers) >= 1

    for _ in range(60):
        await manager.tick()
        if all(t.state in (TASK_DONE, TASK_FAILED) for t in plan.tasks):
            break
    assert all(t.state == TASK_DONE for t in plan.tasks)


@pytest.mark.asyncio
async def test_lazy_pool_reaps_idle_workers(tmp_path):
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review", "qa"], "instances": 2}]
    manager, backend, _ = build_manager(
        tmp_path, roles=roles, lazy_pool=True, idle_worker_ttl=0.001
    )
    await manager.start()
    plan = await manager.submit("Baue Feature Y")
    for _ in range(60):
        await manager.tick()
        if all(t.state in (TASK_DONE, TASK_FAILED) for t in plan.tasks):
            break
    # after the run the pool shrinks back down once workers go idle
    import asyncio
    await asyncio.sleep(0.02)
    for _ in range(5):
        await manager.tick()
    assert len(manager.workers) < len(backend.created)


def test_extract_result_ignores_the_prompt():
    """Our own task prompt also contains 'ERLEDIGT:' and must not win."""
    events = [
        {"source": "assistant", "text": "Fertig. ERLEDIGT: HA ist ein System."},
        {
            "source": "user",
            "text": "Arbeite die Aufgabe ab und antworte mit 'ERLEDIGT: <Ergebnis>'.",
        },
    ]
    assert Manager._extract_result(events) == "HA ist ein System."


def test_extract_result_falls_back_to_any_agent_text():
    events = [{"source": "assistant", "text": "Die Antwort lautet 42."}]
    assert Manager._extract_result(events) == "Die Antwort lautet 42."


def test_extract_result_never_returns_our_own_prompt():
    """Only agent-authored text counts; the task prompt is not a result."""
    events = [{"source": "user", "text": "Arbeite die Aufgabe ab ... ERLEDIGT: <Ergebnis>"}]
    assert Manager._extract_result(events) is None


def test_extract_result_is_scoped_to_the_current_task():
    """A reused conversation must not report the previous task's answer."""
    events = [
        {"source": "assistant", "text": "Antwort A"},
        {"source": "user", "text": "Aufgabe (task_a): tu etwas"},
        {"source": "assistant", "text": "alte Antwort B"},
        {"source": "user", "text": "Aufgabe (task_b): tu anderes"},
    ]
    assert Manager._extract_result(events, "task_a") == "Antwort A"


def test_extract_result_returns_none_when_only_stale_answers_exist():
    """If the agent never answered the current task, report nothing."""
    events = [
        {"source": "user", "text": "Aufgabe (task_a): tu etwas"},
        {"source": "assistant", "text": "alte Antwort B"},
    ]
    assert Manager._extract_result(events, "task_a") is None


@pytest.mark.asyncio
async def test_each_role_gets_its_own_agent(tmp_path):
    """Three distinct roles must run on three distinct agents in parallel."""
    manager, backend, _ = build_manager(tmp_path, lazy_pool=True, max_parallel=3)
    await manager.start()

    plan = await manager.submit(
        "Parallel",
        plan_json=json.dumps(
            {
                "title": "Rollen-Test",
                "tasks": [
                    {"id": "r", "title": "Recherche", "role": "researcher",
                     "tags": ["research"], "depends_on": []},
                    {"id": "c", "title": "Code", "role": "coder",
                     "tags": ["code"], "depends_on": []},
                    {"id": "t", "title": "Test", "role": "tester",
                     "tags": ["test"], "depends_on": []},
                ],
            }
        ),
    )
    await manager.tick()
    roles = sorted(w.role for w in manager.workers.values())
    assert roles == ["coder", "researcher", "tester"]

    for _ in range(60):
        await manager.tick()
        if all(t.state in (TASK_DONE, TASK_FAILED) for t in plan.tasks):
            break
    for task in plan.tasks:
        worker = manager.worker_by_id(task.assigned_to)
        assert worker is not None
        assert worker.role == task.role


@pytest.mark.asyncio
async def test_missing_answer_requeues_instead_of_faking_done(tmp_path):
    """A 'finished' agent with no readable answer must not fake a result."""
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review", "qa"], "instances": 1}]
    manager, backend, _ = build_manager(tmp_path, roles=roles, lazy_pool=True, max_parallel=1)
    await manager.start()
    backend.echo_only = True

    plan = await manager.submit("Baue Feature W")
    task = plan.tasks[0]
    for _ in range(40):
        await manager.tick()
        if task.state in (TASK_DONE, TASK_FAILED):
            break
    assert task.state != TASK_DONE
    assert task.attempts > 1


@pytest.mark.asyncio
async def test_one_failing_role_does_not_block_the_pool(tmp_path):
    """A role whose agent cannot start must not stop other roles."""
    roles = [
        {"name": "researcher", "tags": ["research"], "instances": 1},
        {"name": "coder", "tags": ["code"], "instances": 1},
        {"name": "tester", "tags": ["test"], "instances": 1},
    ]
    manager, backend, _ = build_manager(tmp_path, roles=roles, lazy_pool=True, max_parallel=3)
    await manager.start()
    backend.fail_start_roles.add("researcher")

    plan = await manager.submit("Paralleles Vorhaben")
    manager.tasks.clear()
    from custom_components.kim.models import Task

    for i, (role, tag) in enumerate([("researcher", "research"), ("coder", "code"), ("tester", "test")]):
        manager.tasks[f"t{i}"] = Task(
            task_id=f"t{i}", title=f"T{i} {role}", description=f"mach {role}",
            role=role, tags=[tag], run_id=plan.run_id,
        )

    for _ in range(6):
        await manager.tick()

    # the two healthy roles started and finished, despite the broken one
    states = {t.role: t.state for t in manager.tasks.values()}
    assert states["coder"] == TASK_DONE
    assert states["tester"] == TASK_DONE
    assert len(backend.created) == 2


@pytest.mark.asyncio
async def test_worker_that_never_becomes_ready_is_replaced(tmp_path):
    """A sandbox that never answers readiness must be swapped out."""
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review", "qa"], "instances": 1}]
    manager, backend, _ = build_manager(tmp_path, roles=roles, lazy_pool=True, max_parallel=1)
    await manager.start()
    backend.never_ready = True

    plan = await manager.submit("Baue Feature R")
    task = plan.tasks[0]
    for _ in range(20):
        await manager.tick()
    # the broken instance was dropped and replaced by a fresh one
    assert all(w.not_ready_ticks < manager.max_not_ready_ticks for w in manager.workers.values())
    assert task.state == TASK_PENDING
    assert len(backend.created) >= 2


@pytest.mark.asyncio
async def test_dead_send_fails_over_without_burning_retries(tmp_path):
    """A delivery failure must not consume the task's retry budget."""
    roles = [{"name": "coder", "tags": ["code", "research", "test", "review", "qa"], "instances": 1}]
    manager, backend, _ = build_manager(tmp_path, roles=roles, lazy_pool=True, max_parallel=1)
    await manager.start()
    backend.fail_send_all = True

    plan = await manager.submit("Baue Feature S")
    task = plan.tasks[0]

    for _ in range(12):
        await manager.tick()
    # the dead agent was replaced, the task is still alive (not failed)
    assert task.state != TASK_FAILED
    assert task.attempts == 0
    assert len(backend.created) >= 2


@pytest.mark.asyncio
async def test_dead_worker_is_replaced_and_task_recovers(tmp_path):
    """A vanished agent must not block the role; a fresh one takes over."""
    roles = [
        {"name": "coder", "tags": ["code", "research", "test", "review", "qa"], "instances": 1}
    ]
    manager, backend, _ = build_manager(tmp_path, roles=roles, lazy_pool=True, max_parallel=1)
    await manager.start()

    plan = await manager.submit("Baue Feature Z")
    await manager.tick()
    first = next(iter(manager.workers.values()))
    # the agent's sandbox disappears mid-task
    backend.dead.add(str(first.conversation_id))

    for _ in range(manager.max_status_errors + 2):
        await manager.tick()
    assert first.worker_id not in manager.workers
    assert len(backend.created) > 1

    for _ in range(80):
        await manager.tick()
        if all(t.state in (TASK_DONE, TASK_FAILED) for t in plan.tasks):
            break
    assert all(t.state == TASK_DONE for t in plan.tasks)
