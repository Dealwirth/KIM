"""Agent role registry and assignment policies."""
from __future__ import annotations

from typing import Iterable

from .const import POLICY_AFFINITY, POLICY_LEAST_LOADED, POLICY_ROUND_ROBIN
from .models import AgentRole, Task, Worker

# Sensible default role set for a software factory. Users can override or
# extend this in the config flow.
DEFAULT_ROLES: list[dict] = [
    {
        "name": "planner",
        "tags": ["planning", "design", "architecture", "requirements"],
        "skills": ["decomposition", "estimation"],
        "description": "Zerlegt eine Aufgabe in Teilaufgaben und baut den Abhaengigkeitsgraph.",
        "instances": 1,
    },
    {
        "name": "researcher",
        "tags": ["research", "analysis", "search", "docs"],
        "skills": ["web_search", "summarization"],
        "description": "Sammelt Informationen und liefert Analysen.",
        "instances": 1,
    },
    {
        "name": "coder",
        "tags": ["code", "implementation", "frontend", "backend"],
        "skills": ["python", "javascript", "refactoring"],
        "description": "Implementiert Code und schreibt Tests.",
        "instances": 2,
    },
    {
        "name": "tester",
        "tags": ["test", "qa", "verification", "ci"],
        "skills": ["pytest", "e2e", "regression"],
        "description": "Verifiziert Ergebnisse und meldet Fehler zurueck.",
        "instances": 1,
    },
    {
        "name": "reviewer",
        "tags": ["review", "quality", "security", "docs"],
        "skills": ["code_review", "security"],
        "description": "Prueft Qualitaet, Sicherheit und Dokumentation.",
        "instances": 1,
    },
]


class RoleRegistry:
    def __init__(self, roles: Iterable[dict] | None = None) -> None:
        data = list(roles) if roles else list(DEFAULT_ROLES)
        self._roles: dict[str, AgentRole] = {}
        for item in data:
            role = AgentRole.from_dict(item)
            self._roles[role.name] = role

    def __contains__(self, name: str) -> bool:
        return name in self._roles

    def get(self, name: str) -> AgentRole | None:
        return self._roles.get(name)

    def all(self) -> list[AgentRole]:
        return list(self._roles.values())

    def names(self) -> list[str]:
        return list(self._roles.keys())

    def matching(self, task: Task) -> list[str]:
        """Roles that can handle a task, ranked by tag overlap."""
        wanted = {t.lower() for t in task.tags}
        if task.role:
            wanted.add(task.role.lower())
        scores: list[tuple[int, str]] = []
        for role in self._roles.values():
            role_tags = {t.lower() for t in role.tags} | {role.name.lower()}
            overlap = len(wanted & role_tags) if wanted else 0
            scores.append((overlap, role.name))
        scores.sort(key=lambda item: item[0], reverse=True)
        return [name for _, name in scores]


def _role_load(workers: list[Worker]) -> dict[str, int]:
    load: dict[str, int] = {}
    for worker in workers:
        if worker.current_task_id:
            load[worker.role] = load.get(worker.role, 0) + 1
    return load


def select_worker(
    task: Task,
    candidates: list[Worker],
    registry: RoleRegistry,
    *,
    policy: str = POLICY_AFFINITY,
    round_robin_cursor: dict[str, int] | None = None,
) -> Worker | None:
    """Pick the best idle worker for a task.

    ``candidates`` must already be filtered to *idle, online* workers.
    """
    if not candidates:
        return None

    ranked_roles = registry.matching(task)

    if policy == POLICY_LEAST_LOADED:
        return min(candidates, key=lambda w: (w.completed_tasks, w.started_at))

    if policy == POLICY_ROUND_ROBIN:
        cursor = round_robin_cursor if round_robin_cursor is not None else {}
        ordered = sorted(candidates, key=lambda w: w.worker_id)
        idx = cursor.get(task.role or "*", 0) % len(ordered)
        chosen = ordered[idx]
        cursor[task.role or "*"] = idx + 1
        return chosen

    # affinity: prefer a worker whose role ranks highest for this task
    def affinity_key(worker: Worker) -> tuple[int, int, float]:
        try:
            rank = ranked_roles.index(worker.role)
        except ValueError:
            rank = len(ranked_roles)
        return (rank, worker.completed_tasks, worker.started_at)

    return min(candidates, key=affinity_key)
