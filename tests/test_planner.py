"""Tests for planning and the dependency graph."""
from __future__ import annotations

from custom_components.kim.models import Task, new_id
from custom_components.kim.planner import (
    _is_acyclic,
    heuristic_plan,
    parse_plan,
    plan_progress,
    ready_tasks,
)


def test_heuristic_plan_is_linear_pipeline():
    plan = heuristic_plan("Baue ein Login")
    assert len(plan.tasks) == 4
    assert plan.tasks[0].depends_on == []
    for prev, task in zip(plan.tasks, plan.tasks[1:]):
        assert task.depends_on == [prev.task_id]
    assert plan.tasks[1].role == "code"


def test_ready_tasks_only_after_deps_done():
    plan = heuristic_plan("X")
    ready = ready_tasks(plan)
    assert ready == [plan.tasks[0]]
    plan.tasks[0].state = "done"
    ready = ready_tasks(plan)
    assert ready == [plan.tasks[1]]


def test_plan_progress():
    plan = heuristic_plan("X")
    assert plan_progress(plan) == (0, 4)
    plan.tasks[0].state = "done"
    assert plan_progress(plan) == (1, 4)


def test_parse_plan_from_json_block():
    raw = """
    Hier ist der Plan:
    ```json
    {"title": "Mein Plan", "tasks": [
      {"id": "a", "title": "Analyse", "role": "researcher", "tags": ["research"]},
      {"id": "b", "title": "Bauen", "role": "coder", "depends_on": ["a"]}
    ]}
    ```
    """
    plan = parse_plan(raw)
    assert plan is not None
    assert plan.title == "Mein Plan"
    assert len(plan.tasks) == 2
    assert plan.tasks[1].depends_on == [plan.tasks[0].task_id]


def test_parse_plan_rejects_cycles():
    raw = '{"tasks": [{"id":"a","depends_on":["b"]},{"id":"b","depends_on":["a"]}]}'
    assert parse_plan(raw) is None


def test_parse_plan_rejects_garbage():
    assert parse_plan("kein json hier") is None


def test_is_acyclic_detects_cycle():
    a = Task(task_id="a", title="a")
    b = Task(task_id="b", title="b")
    a.depends_on = ["b"]
    b.depends_on = ["a"]
    assert not _is_acyclic([a, b])


def test_is_acyclic_accepts_diamond():
    a = Task(task_id="a", title="a")
    b = Task(task_id="b", title="b", depends_on=["a"])
    c = Task(task_id="c", title="c", depends_on=["a"])
    d = Task(task_id="d", title="d", depends_on=["b", "c"])
    assert _is_acyclic([a, b, c, d])


def test_new_id_unique():
    assert new_id("t") != new_id("t")
