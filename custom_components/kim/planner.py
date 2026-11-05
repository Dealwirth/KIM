"""Task planning: turn a natural-language goal into a dependency graph (DAG).

Two modes:
  * ``parse_plan`` turns a structured JSON plan (usually produced by a
    coordinator LLM) into :class:`Task` objects.
  * ``heuristic_plan`` is a deterministic fallback that splits a goal into a
    standard research -> implement -> test -> review pipeline. It keeps the
    whole system usable without any LLM planning step.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .models import Plan, Task, new_id

PIPELINE = [
    ("research", "research", ["research", "analysis"], "Analysiere die Anforderung und sammle Kontext."),
    ("implement", "code", ["code", "implementation"], "Implementiere die Aenderung."),
    ("test", "test", ["test", "qa", "verification"], "Verifiziere die Aenderung mit Tests."),
    ("review", "review", ["review", "quality"], "Pruefe Qualitaet und Dokumentation."),
]

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def _extract_json(text: str) -> Any | None:
    """Pull a JSON object/array out of an LLM answer (fenced or bare)."""
    if not text:
        return None
    match = _JSON_BLOCK.search(text)
    candidates = [match.group(1)] if match else []
    stripped = text.strip()
    candidates.append(stripped)
    # also try the widest {...} / [...] span
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def parse_plan(raw: str, *, title: str = "Plan", run_id: str | None = None) -> Plan | None:
    """Parse an LLM-produced JSON plan. Returns ``None`` if not parseable.

    Accepted shape::

        {"tasks": [
            {"id": "research", "title": "...", "role": "researcher",
             "tags": ["research"], "depends_on": []},
            ...
        ]}
    """
    data = _extract_json(raw)
    if data is None:
        return None
    if isinstance(data, dict):
        items = data.get("tasks", data.get("steps"))
        title = str(data.get("title", title))
    elif isinstance(data, list):
        items = data
    else:
        return None
    if not isinstance(items, list) or not items:
        return None

    tasks: list[Task] = []
    id_map: dict[str, str] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return None
        local_id = str(item.get("id", item.get("key", f"t{index}")))
        task = Task(
            task_id=new_id("task"),
            title=str(item.get("title", item.get("name", f"Schritt {index + 1}"))),
            description=str(item.get("description", item.get("detail", ""))),
            role=(str(item["role"]) if item.get("role") else None),
            tags=[str(t) for t in item.get("tags", [])],
        )
        id_map[local_id] = task.task_id
        tasks.append(task)

    # second pass: resolve dependency references to real task ids
    for index, item in enumerate(items):
        deps = item.get("depends_on", item.get("deps", [])) or []
        resolved = [id_map[d] for d in deps if d in id_map]
        tasks[index].depends_on = resolved

    if not _is_acyclic(tasks):
        return None
    return Plan(plan_id=new_id("plan"), title=title, tasks=tasks, run_id=run_id)


def heuristic_plan(
    goal: str,
    *,
    title: str | None = None,
    run_id: str | None = None,
    max_steps: int = 4,
) -> Plan:
    """Deterministic fallback plan: a linear research->...->review pipeline."""
    steps = PIPELINE[:max_steps]
    tasks: list[Task] = []
    previous: str | None = None
    for key, role, tags, description in steps:
        task = Task(
            task_id=new_id("task"),
            title=f"{key}: {goal}",
            description=f"{description}\n\nZiel: {goal}",
            role=role,
            tags=tags,
            depends_on=[previous] if previous else [],
        )
        tasks.append(task)
        previous = task.task_id
    return Plan(
        plan_id=new_id("plan"),
        title=title or goal[:80],
        tasks=tasks,
        run_id=run_id,
    )


def _is_acyclic(tasks: list[Task]) -> bool:
    """Kahn's algorithm to reject cyclic plans."""
    indegree = {t.task_id: 0 for t in tasks}
    adjacency: dict[str, list[str]] = {t.task_id: [] for t in tasks}
    for task in tasks:
        for dep in task.depends_on:
            if dep not in indegree:
                return False
            adjacency[dep].append(task.task_id)
            indegree[task.task_id] += 1
    queue = [tid for tid, deg in indegree.items() if deg == 0]
    seen = 0
    while queue:
        current = queue.pop()
        seen += 1
        for nxt in adjacency[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    return seen == len(tasks)


def ready_tasks(plan: Plan) -> list[Task]:
    """Tasks whose dependencies are all done and which are still pending."""
    done = {t.task_id for t in plan.tasks if t.state == "done"}
    ready: list[Task] = []
    for task in plan.tasks:
        if task.state != "pending":
            continue
        if all(dep in done for dep in task.depends_on):
            ready.append(task)
    return ready


def plan_progress(plan: Plan) -> tuple[int, int]:
    """Return ``(completed, total)``."""
    total = len(plan.tasks)
    completed = sum(1 for t in plan.tasks if t.state == "done")
    return completed, total
