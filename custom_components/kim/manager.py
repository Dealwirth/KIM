"""The KIM manager: orchestrates a pool of OpenHands agents.

Responsibilities:
  * keep a pool of agent instances alive (one per role instance)
  * decompose a goal into a task DAG and schedule ready tasks
  * assign each task to the best idle worker (role affinity)
  * enforce a global parallelism limit so agents do not interfere
  * run a heartbeat loop and detect stalled/offline agents
  * fail over: retry or re-queue the work of a dead agent
  * expose everything the Home Assistant layer needs via ``snapshot``

The class is framework-free: it only depends on ``backends``, ``models``,
``planner``, ``roles`` and ``scratchpad``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from .backends import BackendError, BaseBackend
from .const import (
    EVENT_TASK_DONE,
    EVENT_TASK_FAILED,
    EVENT_WORKER_OFFLINE,
    POLICY_AFFINITY,
    STATE_DONE,
    STATE_FAILED,
    STATE_IDLE,
    STATE_OFFLINE,
    STATE_STALLED,
    STATE_WORKING,
    TASK_ASSIGNED,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_PENDING,
)
from .models import Plan, Task, Worker
from .planner import heuristic_plan, parse_plan, plan_progress
from .roles import RoleRegistry, select_worker
from .scratchpad import KIND_CLAIM, KIND_HEARTBEAT, KIND_LOG, KIND_RESULT, Scratchpad

_LOGGER = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]


class Manager:
    def __init__(
        self,
        backend: BaseBackend,
        scratchpad: Scratchpad,
        *,
        registry: RoleRegistry | None = None,
        model: str = "openhands/deepseek-v4.1-flash",
        repository: str | None = None,
        branch: str | None = None,
        max_parallel: int = 3,
        heartbeat_timeout: float = 180,
        claim_ttl: float = 900,
        stall_timeout: float = 900,
        policy: str = POLICY_AFFINITY,
        max_retries: int = 1,
        lazy_pool: bool = True,
        idle_worker_ttl: float = 900,
        on_event: EventCallback | None = None,
    ) -> None:
        self.backend = backend
        self.scratchpad = scratchpad
        self.registry = registry or RoleRegistry()
        self.model = model
        self.repository = repository
        self.branch = branch
        self.max_parallel = max(1, int(max_parallel))
        self.heartbeat_timeout = heartbeat_timeout
        self.claim_ttl = claim_ttl
        self.stall_timeout = stall_timeout
        self.policy = policy
        self.max_retries = max_retries
        self.lazy_pool = lazy_pool
        self.idle_worker_ttl = idle_worker_ttl
        self.on_event = on_event

        self.workers: dict[str, Worker] = {}
        self.worker_history: dict[str, Worker] = {}
        self.plans: dict[str, Plan] = {}
        self.tasks: dict[str, Task] = {}
        self.paused = False
        self.started_at = time.time()
        self._rr_cursor: dict[str, int] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Prepare the pool.

        In lazy mode (the default for cloud backends) no agent is started
        until the first task needs it, so a freshly loaded integration costs
        nothing. Eager mode spins the whole pool up front.
        """
        if self.lazy_pool:
            return
        for role in self.registry.all():
            for _ in range(role.instances):
                await self._spawn_worker(role.name)

    async def _spawn_worker(self, role_name: str) -> Worker | None:
        """Start a single agent instance for ``role_name`` (on demand)."""
        try:
            worker = await self.backend.start_worker(
                role_name, self.model, self.repository, self.branch
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("KIM: could not start worker for role %s: %s", role_name, err)
            return None
        worker.touch()
        self.workers[worker.worker_id] = worker
        self.worker_history[worker.worker_id] = worker
        self.scratchpad.post(
            KIND_LOG, worker.worker_id, f"gestartet (Rolle {role_name})", role=role_name
        )
        return worker

    def _role_worker_count(self, role_name: str) -> int:
        return sum(1 for w in self.workers.values() if w.role == role_name)

    async def _ensure_capacity(self) -> None:
        """Lazily grow the pool so pending work can be dispatched."""
        if not self.lazy_pool:
            return
        pending = [t for t in self.tasks.values() if t.state == TASK_PENDING and self._deps_done(t)]
        if not pending:
            return
        while self._running_count() < self.max_parallel and pending:
            task = pending[0]
            role = self._preferred_role(task)
            role_obj = self.registry.get(role) if role else None
            cap = role_obj.instances if role_obj else 1
            if role and self._role_worker_count(role) < cap and not self._idle_workers_for(role):
                worker = await self._spawn_worker(role)
                if worker is None:
                    break
            else:
                break
            pending = [t for t in self.tasks.values() if t.state == TASK_PENDING and self._deps_done(t)]
            if self._idle_workers():
                break

    def _preferred_role(self, task: Task) -> str | None:
        ranked = self.registry.matching(task)
        return ranked[0] if ranked else (task.role or None)

    def _idle_workers_for(self, role: str) -> list[Worker]:
        return [w for w in self._idle_workers() if w.role == role]

    async def _reap_idle_workers(self) -> None:
        """Stop agent instances that have been idle past ``idle_worker_ttl``."""
        if not self.lazy_pool or self.idle_worker_ttl <= 0:
            return
        now = time.time()
        for worker in list(self.workers.values()):
            if worker.current_task_id is not None:
                continue
            if self._running_count() >= self.max_parallel:
                continue
            if now - worker.last_heartbeat < self.idle_worker_ttl:
                continue
            try:
                await self.backend.stop_worker(worker)
            except Exception:  # noqa: BLE001 - best effort
                pass
            self.workers.pop(worker.worker_id, None)
            self.scratchpad.post(
                KIND_LOG, worker.worker_id, "in den Ruhestand versetzt (idle)", role=worker.role
            )

    async def stop(self) -> None:
        for worker in self.workers.values():
            try:
                await self.backend.stop_worker(worker)
            except Exception:  # noqa: BLE001 - best effort shutdown
                pass
        await self.backend.close()

    def _fire(self, event: str, data: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event, data)
            except Exception:  # noqa: BLE001 - never let a listener break the loop
                _LOGGER.exception("KIM: event callback failed for %s", event)

    # ------------------------------------------------------------------
    # submission
    # ------------------------------------------------------------------
    async def submit(self, goal: str, *, title: str | None = None,
                     plan_json: str | None = None) -> Plan:
        """Turn a natural-language goal into a plan and schedule it."""
        async with self._lock:
            plan = None
            if plan_json:
                plan = parse_plan(plan_json, title=title or goal[:80])
            if plan is None:
                plan = heuristic_plan(goal, title=title or goal[:80])
            self.plans[plan.plan_id] = plan
            for task in plan.tasks:
                task.run_id = plan.plan_id
                self.tasks[task.task_id] = task
            self.scratchpad.post(
                KIND_LOG,
                "manager",
                f"Plan '{plan.title}' mit {len(plan.tasks)} Aufgaben angelegt",
                data={"plan_id": plan.plan_id},
            )
        await self.tick()
        return plan

    async def cancel(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None or task.terminal:
            return False
        task.state = TASK_CANCELLED
        task.touch()
        if task.assigned_to:
            worker = self.workers.get(task.assigned_to)
            if worker is not None:
                worker.state = STATE_IDLE
                worker.current_task_id = None
        self.scratchpad.post(KIND_LOG, "manager", "Aufgabe abgebrochen", task_id=task_id)
        return True

    async def retry(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None or task.state not in (TASK_FAILED, TASK_CANCELLED):
            return False
        task.state = TASK_PENDING
        task.error = None
        task.assigned_to = None
        task.attempts = 0
        task.touch()
        await self.tick()
        return True

    def pause(self) -> None:
        self.paused = True
        self.scratchpad.post(KIND_LOG, "manager", "Manager pausiert")

    async def resume(self) -> None:
        self.paused = False
        self.scratchpad.post(KIND_LOG, "manager", "Manager fortgesetzt")
        await self.tick()

    # ------------------------------------------------------------------
    # scheduling
    # ------------------------------------------------------------------
    async def tick(self) -> None:
        """One scheduling step: heartbeat, detect failures, assign work."""
        async with self._lock:
            self._heartbeat()
            self._detect_failures()
            if self.paused:
                return
            await self._ensure_capacity()
            await self._reconcile()
            await self._assign_ready_tasks()
            await self._reap_idle_workers()

    def _heartbeat(self) -> None:
        for worker in self.workers.values():
            if worker.current_task_id is not None:
                worker.touch()
            self.scratchpad.heartbeat(worker.worker_id, role=worker.role)

    def _running_count(self) -> int:
        return sum(1 for w in self.workers.values() if w.current_task_id is not None)

    def _idle_workers(self) -> list[Worker]:
        return [
            w
            for w in self.workers.values()
            if w.current_task_id is None and w.state == STATE_IDLE
        ]

    def _detect_failures(self) -> None:
        now = time.time()
        for worker in self.workers.values():
            if worker.current_task_id is None:
                continue
            if now - worker.last_heartbeat > self.stall_timeout and worker.state == STATE_WORKING:
                worker.state = STATE_STALLED
            if now - worker.last_heartbeat > self.heartbeat_timeout * 3:
                self._fail_worker(worker, "Agent nicht erreichbar (Heartbeat-Timeout)")

    def _fail_worker(self, worker: Worker, reason: str) -> None:
        task_id = worker.current_task_id
        worker.state = STATE_OFFLINE
        worker.current_task_id = None
        worker.last_error = reason
        self.scratchpad.post(KIND_LOG, worker.worker_id, f"Ausfall: {reason}")
        self._fire(EVENT_WORKER_OFFLINE, {"worker_id": worker.worker_id, "reason": reason})
        if task_id and task_id in self.tasks:
            self._requeue(self.tasks[task_id], reason)

    def _requeue(self, task: Task, reason: str) -> None:
        task.assigned_to = None
        if task.attempts > task.max_retries:
            task.state = TASK_FAILED
            task.error = reason
            task.touch()
            self.scratchpad.post(
                KIND_LOG, "manager", f"Aufgabe endgueltig fehlgeschlagen: {reason}",
                task_id=task.task_id,
            )
            self._fire(EVENT_TASK_FAILED, {"task_id": task.task_id, "error": reason})
        else:
            task.state = TASK_PENDING
            task.touch()
            self.scratchpad.post(
                KIND_LOG, "manager", "Aufgabe zur Neuverteilung freigegeben", task_id=task.task_id
            )

    async def _assign_ready_tasks(self) -> None:
        """Hand out every ready task that has a free worker slot."""
        for task in self.tasks.values():
            if task.state in (TASK_ASSIGNED, TASK_IN_PROGRESS) and task.assigned_to:
                worker = self.workers.get(task.assigned_to)
                if worker is None or worker.state == STATE_OFFLINE:
                    self._requeue(task, "zugewiesener Agent offline")

        free_slots = self.max_parallel - self._running_count()
        if free_slots <= 0:
            return

        pending = [t for t in self.tasks.values() if t.state == TASK_PENDING]
        pending.sort(key=lambda t: t.created_at)
        for task in pending:
            if free_slots <= 0:
                break
            if not self._deps_done(task):
                continue
            idle = self._idle_workers()
            if not idle:
                break
            worker = select_worker(
                task, idle, self.registry, policy=self.policy, round_robin_cursor=self._rr_cursor
            )
            if worker is None:
                break
            await self._dispatch(task, worker)
            free_slots -= 1

    def _deps_done(self, task: Task) -> bool:
        for dep in task.depends_on:
            dep_task = self.tasks.get(dep)
            if dep_task is None or dep_task.state != TASK_DONE:
                return False
        return True

    async def _dispatch(self, task: Task, worker: Worker) -> None:
        task.attempts += 1
        task.state = TASK_ASSIGNED
        task.assigned_to = worker.worker_id
        task.touch()
        worker.state = STATE_WORKING
        worker.current_task_id = task.task_id
        worker.touch()

        self.scratchpad.post(
            KIND_CLAIM,
            worker.worker_id,
            f"uebernimmt '{task.title}'",
            task_id=task.task_id,
            role=worker.role,
            ttl=self.claim_ttl,
        )

        prompt = self._build_prompt(task, worker)
        try:
            await self.backend.send_task(worker, prompt)
            task.state = TASK_IN_PROGRESS
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("KIM: dispatch to %s failed: %s", worker.worker_id, err)
            worker.state = STATE_IDLE
            worker.current_task_id = None
            self._requeue(task, f"Zustellung fehlgeschlagen: {err}")

    def _build_prompt(self, task: Task, worker: Worker) -> str:
        deps = [self.tasks[d] for d in task.depends_on if d in self.tasks]
        dep_block = "\n".join(f"- {d.title}: {d.result or 'erledigt'}" for d in deps) or "keine"
        recent_notes = "\n".join(
            f"[{n.author}] {n.text}"
            for n in self.scratchpad.read_recent(10)
            if n.kind in (KIND_LOG, KIND_RESULT, KIND_CLAIM)
        )
        return (
            f"Du bist KIM-Agent mit Rolle '{worker.role}'.\n"
            f"Aufgabe ({task.task_id}): {task.title}\n"
            f"Beschreibung: {task.description}\n"
            f"Bereits erledigte Vorgaenger:\n{dep_block}\n\n"
            f"Letzte Notizen aus dem gemeinsamen Notizzettel:\n{recent_notes}\n\n"
            "Arbeite die Aufgabe vollstaendig ab und antworte am Ende mit "
            "'ERLEDIGT: <kurzes Ergebnis>'."
        )

    async def _reconcile(self) -> None:
        """Poll each busy worker once; complete or fail its task accordingly."""
        for worker in list(self.workers.values()):
            if worker.current_task_id is None:
                continue
            task = self.tasks.get(worker.current_task_id)
            if task is None:
                worker.current_task_id = None
                worker.state = STATE_IDLE
                continue
            try:
                status = await self.backend.get_status(worker)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("KIM: status of %s failed: %s", worker.worker_id, err)
                continue
            state = status.get("state", worker.state)
            if state == STATE_DONE:
                await self._complete_task(task, worker, status)
            elif state == STATE_FAILED:
                worker.failed_tasks += 1
                self._requeue(task, str(status.get("error", "Agent meldete Fehler")))
                worker.state = STATE_IDLE
                worker.current_task_id = None

    async def _complete_task(self, task: Task, worker: Worker, status: dict[str, Any]) -> None:
        events: list[dict[str, Any]] = []
        try:
            events = await self.backend.read_events(worker, limit=20)
        except Exception:  # noqa: BLE001 - events are best effort
            pass
        result = self._extract_result(events) or "erledigt"

        task.state = TASK_DONE
        task.result = result
        task.touch()
        worker.completed_tasks += 1
        worker.state = STATE_IDLE
        worker.current_task_id = None
        worker.touch()

        self.scratchpad.post(
            KIND_RESULT, worker.worker_id, result, task_id=task.task_id, role=worker.role
        )
        self._fire(EVENT_TASK_DONE, {"task_id": task.task_id, "result": result})
        await self._maybe_finish_plan(task)

    @staticmethod
    def _extract_result(events: list[dict[str, Any]]) -> str | None:
        """Pull the agent's final answer out of a raw event list."""
        assistant_texts: list[str] = []
        for event in events:
            source = str(event.get("source") or "")
            text = event.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if "ERLEDIGT:" in text:
                return text.split("ERLEDIGT:", 1)[1].strip()[:2000]
            if source in ("assistant", "agent"):
                assistant_texts.append(text.strip())
        if assistant_texts:
            return assistant_texts[0][:2000]
        for event in events:
            text = event.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()[:2000]
        return None

    async def _maybe_finish_plan(self, task: Task) -> None:
        plan = self.plans.get(task.run_id or "")
        if plan is None:
            return
        completed, total = plan_progress(plan)
        if completed >= total:
            self.scratchpad.post(
                KIND_LOG, "manager", f"Plan '{plan.title}' abgeschlossen ({completed}/{total})"
            )

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        completed = sum(1 for t in self.tasks.values() if t.state == TASK_DONE)
        failed = sum(1 for t in self.tasks.values() if t.state == TASK_FAILED)
        return {
            "paused": self.paused,
            "workers": [w.to_dict() for w in self.workers.values()],
            "worker_count": len(self.workers),
            "idle_workers": len(self._idle_workers()),
            "running_tasks": self._running_count(),
            "max_parallel": self.max_parallel,
            "tasks_total": len(self.tasks),
            "tasks_completed": completed,
            "tasks_failed": failed,
            "tasks_pending": sum(1 for t in self.tasks.values() if t.state == TASK_PENDING),
            "plans": [p.to_dict() for p in self.plans.values()],
            "notes": [n.to_dict() for n in self._visible_notes(25)],
            "uptime": time.time() - self.started_at,
        }

    def worker_by_id(self, worker_id: str) -> Worker | None:
        return self.workers.get(worker_id)

    def worker_for_task(self, task_id: str) -> Worker | None:
        """Resolve the agent that ran (or runs) a task, even after reaping."""
        task = self.tasks.get(task_id)
        if task is None or not task.assigned_to:
            return None
        return self.workers.get(task.assigned_to) or self.worker_history.get(task.assigned_to)

    async def fetch_task_file(self, task_id: str, path: str) -> bytes:
        """Read one file from the workspace of the agent that ran ``task_id``."""
        worker = self.worker_for_task(task_id)
        if worker is None:
            raise ValueError(f"keine Agent-Instanz fuer Aufgabe {task_id}")
        return await self.backend.fetch_file(worker, path)

    async def fetch_task_archive(self, task_id: str) -> bytes:
        """Read the whole workspace of the agent that ran ``task_id`` as zip."""
        worker = self.worker_for_task(task_id)
        if worker is None:
            raise ValueError(f"keine Agent-Instanz fuer Aufgabe {task_id}")
        return await self.backend.fetch_archive(worker)

    def _visible_notes(self, limit: int):
        """Recent scratchpad notes without the noisy heartbeats."""
        return self.scratchpad.read_recent(limit, exclude_kinds={KIND_HEARTBEAT})

    def note(self, author: str, text: str, task_id: str | None = None) -> None:
        self.scratchpad.post(KIND_LOG, author, text, task_id=task_id)
