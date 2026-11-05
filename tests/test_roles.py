"""Tests for role assignment policies."""
from __future__ import annotations

from custom_components.kim.const import (
    POLICY_AFFINITY,
    POLICY_LEAST_LOADED,
    POLICY_ROUND_ROBIN,
)
from custom_components.kim.models import Task, Worker
from custom_components.kim.roles import DEFAULT_ROLES, RoleRegistry, select_worker


def _worker(role: str, *, completed: int = 0, wid: str | None = None) -> Worker:
    return Worker(
        worker_id=wid or f"{role}-1",
        role=role,
        backend="mock",
        conversation_id="c",
        completed_tasks=completed,
    )


def test_default_registry_contains_pipeline_roles():
    registry = RoleRegistry()
    for name in ("researcher", "coder", "tester", "reviewer", "planner"):
        assert name in registry


def test_matching_ranks_by_tag_overlap():
    registry = RoleRegistry()
    task = Task(task_id="t", title="x", tags=["test", "qa"])
    ranked = registry.matching(task)
    assert ranked[0] == "tester"


def test_affinity_prefers_matching_role():
    registry = RoleRegistry()
    task = Task(task_id="t", title="x", tags=["code"])
    coder = _worker("coder")
    tester = _worker("tester")
    chosen = select_worker(task, [tester, coder], registry, policy=POLICY_AFFINITY)
    assert chosen is coder


def test_least_loaded_picks_lowest_completed():
    registry = RoleRegistry()
    task = Task(task_id="t", title="x", tags=["code"])
    busy = _worker("coder", completed=5)
    free = _worker("coder", completed=1, wid="coder-2")
    chosen = select_worker(task, [busy, free], registry, policy=POLICY_LEAST_LOADED)
    assert chosen is free


def test_round_robin_cycles():
    registry = RoleRegistry()
    task = Task(task_id="t", title="x", tags=["code"])
    a = _worker("coder", wid="a")
    b = _worker("coder", wid="b")
    cursor: dict[str, int] = {}
    first = select_worker(task, [a, b], registry, policy=POLICY_ROUND_ROBIN, round_robin_cursor=cursor)
    second = select_worker(task, [a, b], registry, policy=POLICY_ROUND_ROBIN, round_robin_cursor=cursor)
    assert first is not second


def test_no_candidates_returns_none():
    registry = RoleRegistry()
    task = Task(task_id="t", title="x", tags=["code"])
    assert select_worker(task, [], registry) is None


def test_custom_roles_override_defaults():
    registry = RoleRegistry([{"name": "specialist", "tags": ["x"]}])
    assert registry.names() == ["specialist"]
    assert "coder" not in registry


def test_unknown_role_task_waits_instead_of_misassigning():
    registry = RoleRegistry()
    task = Task(task_id="t", title="x", role="tester", tags=["test"])
    coder = _worker("coder")
    assert select_worker(task, [coder], registry) is None


def test_compatible_role_can_help_out():
    registry = RoleRegistry(
        [
            {"name": "coder", "tags": ["code", "test"]},
            {"name": "tester", "tags": ["test"]},
        ]
    )
    task = Task(task_id="t", title="x", role="tester", tags=["test"])
    coder = _worker("coder")
    tester = _worker("tester")
    # the dedicated tester is preferred over the coder that also fits
    assert select_worker(task, [coder, tester], registry) is tester
    # but the coder can take over when no tester is free
    assert select_worker(task, [coder], registry) is coder


def test_best_role_picks_strongest_tag_overlap():
    registry = RoleRegistry(
        [
            {"name": "coder", "tags": ["code", "test"]},
            {"name": "tester", "tags": ["test", "qa"]},
        ]
    )
    task = Task(task_id="t", title="x", tags=["test", "qa"])
    assert registry.best_role(task) == "tester"
    assert registry.best_role(Task(task_id="u", title="y", tags=["code"])) == "coder"
    assert registry.best_role(Task(task_id="v", title="z")) is None
