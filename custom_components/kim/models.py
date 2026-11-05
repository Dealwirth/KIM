"""Core data models for KIM.

These are deliberately framework-free (no Home Assistant imports) so the whole
engine can be unit-tested without an HA installation.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from .const import (
    STATE_IDLE,
    TASK_PENDING,
)


def _now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class AgentRole:
    """Definition of an agent role (its function + assignment tags).

    A role is the "genaue Zuweisung zu ihrer Funktion". Each role can be
    realised by one or more workers (agent instances) for redundancy.
    """

    name: str
    tags: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    description: str = ""
    instances: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentRole":
        return cls(
            name=str(data.get("name", "worker")),
            tags=[str(t) for t in data.get("tags", [])],
            skills=[str(s) for s in data.get("skills", [])],
            description=str(data.get("description", "")),
            instances=max(1, int(data.get("instances", 1))),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Worker:
    """A concrete agent instance (one OpenHands conversation/sandbox)."""

    worker_id: str
    role: str
    backend: str
    conversation_id: str | None = None
    agent_server_url: str | None = None
    session_api_key: str | None = None
    state: str = STATE_IDLE
    current_task_id: str | None = None
    started_at: float = field(default_factory=_now)
    last_heartbeat: float = field(default_factory=_now)
    last_error: str | None = None
    completed_tasks: int = 0
    failed_tasks: int = 0
    sandbox_status: str | None = None

    @property
    def online(self) -> bool:
        return self.conversation_id is not None

    def touch(self) -> None:
        self.last_heartbeat = _now()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # never leak the session key through the UI/attributes
        data.pop("session_api_key", None)
        return data


@dataclass
class Task:
    """A unit of work for an agent."""

    task_id: str
    title: str
    description: str = ""
    role: str | None = None
    tags: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    state: str = TASK_PENDING
    assigned_to: str | None = None
    result: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    attempts: int = 0
    max_retries: int = 1
    run_id: str | None = None

    @property
    def terminal(self) -> bool:
        from .const import TERMINAL_TASK_STATES

        return self.state in TERMINAL_TASK_STATES

    def touch(self) -> None:
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Plan:
    """A named collection of tasks forming a dependency graph (DAG)."""

    plan_id: str
    title: str
    tasks: list[Task] = field(default_factory=list)
    created_at: float = field(default_factory=_now)
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "title": self.title,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "tasks": [t.to_dict() for t in self.tasks],
        }
