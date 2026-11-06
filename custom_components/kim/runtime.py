"""Runtime glue between Home Assistant and the KIM engine.

``KimRuntime`` owns the :class:`Manager`, the coordinator that polls it, and
the services/conversation-agent that let a user talk to KIM from Assist.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .backends import FailoverBackend, create_backend
from .const import (
    ARTIFACT_DIR,
    BACKEND_AUTO,
    BACKEND_MOCK,
    CONF_API_KEY,
    CONF_ARCHIVE_REPO,
    CONF_BACKEND,
    CONF_BASE_URL,
    CONF_BRANCH,
    CONF_CLAIM_TTL,
    CONF_HEARTBEAT_TIMEOUT,
    CONF_IDLE_WORKER_TTL,
    CONF_LOCAL_API_KEY,
    CONF_LOCAL_URL,
    CONF_MAX_PARALLEL,
    CONF_MAX_RETRIES,
    CONF_MODEL,
    CONF_REPOSITORY,
    CONF_RETRY_BACKOFF,
    CONF_ROLES,
    CONF_STALL_TIMEOUT,
    CONF_WORKSPACE,
    DEFAULT_BASE_URL,
    DEFAULT_CLAIM_TTL,
    DEFAULT_HEARTBEAT_TIMEOUT,
    DEFAULT_IDLE_WORKER_TTL,
    DEFAULT_MAX_PARALLEL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MODEL,
    DEFAULT_RETRY_BACKOFF,
    DEFAULT_RETRY_BACKOFF_MAX,
    DEFAULT_STALL_TIMEOUT,
    DEFAULT_WORKSPACE,
    DOMAIN,
    SERVICE_ARCHIVE,
    SERVICE_ARCHIVE_LIST,
    SERVICE_ARCHIVE_LOAD,
    SERVICE_CANCEL,
    SERVICE_COLLECT_ARTIFACTS,
    SERVICE_FETCH_FILE,
    SERVICE_NOTE,
    SERVICE_PAUSE,
    SERVICE_RESUME,
    SERVICE_RETRY,
    SERVICE_SUBMIT,
    TASK_DONE,
    UPDATE_INTERVAL,
)
from .archive import ArchiveStore
from .manager import Manager
from .models import Worker
from .roles import RoleRegistry
from .scratchpad import KIND_LOG, Scratchpad
from .services import SCHEMAS

_LOGGER = logging.getLogger(__name__)


class KimRuntime:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.manager: Manager | None = None
        self.coordinator: DataUpdateCoordinator[dict[str, Any]] | None = None
        self.scratchpad: Scratchpad | None = None
        self.archive: ArchiveStore | None = None
        # Saved file lists per task (path/url/size), shown in the panel.
        self._artifacts: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    def _build_manager(self) -> Manager:
        options = {**self.entry.data, **self.entry.options}
        backend_type = options.get(CONF_BACKEND, BACKEND_AUTO)
        backend = create_backend(
            backend_type,
            base_url=options.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            api_key=options.get(CONF_API_KEY, ""),
            local_url=options.get(CONF_LOCAL_URL, ""),
            local_api_key=options.get(CONF_LOCAL_API_KEY, ""),
            workspace=options.get(CONF_WORKSPACE, DEFAULT_WORKSPACE),
            model=options.get(CONF_MODEL, DEFAULT_MODEL),
        )
        scratch_path = self.hass.config.path(f".storage/{DOMAIN}_{self.entry.entry_id}_notes.jsonl")
        scratchpad = Scratchpad(scratch_path)
        self.scratchpad = scratchpad
        registry = RoleRegistry(options.get(CONF_ROLES))
        # Cloud agents cost money and are only started when work arrives.
        lazy_pool = backend_type != BACKEND_MOCK
        # Keep only the newest plans/notes in RAM; the full history is archived
        # off-device (GitHub) so Home Assistant storage stays small.
        self.archive = ArchiveStore(
            repo=options.get(CONF_ARCHIVE_REPO, ""),
            token=os.environ.get("GITHUB_TOKEN", ""),
        )

        def on_event(event: str, data: dict[str, Any]) -> None:
            self.hass.bus.async_fire(event, data)
            if self.coordinator is not None and self.manager is not None:
                self.coordinator.async_set_updated_data(self.manager.snapshot())

        return Manager(
            backend,
            scratchpad,
            registry=registry,
            context_provider=self._home_context,
            cleanup_when_idle=True,
            before_teardown=self._save_worker_workspace,
            model=options.get(CONF_MODEL, DEFAULT_MODEL),
            repository=options.get(CONF_REPOSITORY) or None,
            branch=options.get(CONF_BRANCH) or None,
            max_parallel=int(options.get(CONF_MAX_PARALLEL, DEFAULT_MAX_PARALLEL)),
            heartbeat_timeout=float(options.get(CONF_HEARTBEAT_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT)),
            claim_ttl=float(options.get(CONF_CLAIM_TTL, DEFAULT_CLAIM_TTL)),
            stall_timeout=float(options.get(CONF_STALL_TIMEOUT, DEFAULT_STALL_TIMEOUT)),
            max_retries=int(options.get(CONF_MAX_RETRIES, DEFAULT_MAX_RETRIES)),
            lazy_pool=lazy_pool,
            idle_worker_ttl=float(options.get(CONF_IDLE_WORKER_TTL, DEFAULT_IDLE_WORKER_TTL)),
            retry_backoff_base=float(options.get(CONF_RETRY_BACKOFF, DEFAULT_RETRY_BACKOFF)),
            retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
            on_event=on_event,
        )

    # ------------------------------------------------------------------
    def _home_context(self) -> str:
        """A compact, real snapshot of Home Assistant for the agents.

        Agents should reason about the actual house, not invent values, so the
        most useful live data (entities, areas, notable states) is summarised
        into a few lines and attached to every task prompt.
        """
        hass = self.hass
        lines: list[str] = []
        states = hass.states.async_all()
        lines.append(f"Entitaeten gesamt: {len(states)}")
        domains: dict[str, int] = {}
        for state in states:
            domain = state.entity_id.split(".", 1)[0]
            domains[domain] = domains.get(domain, 0) + 1
        top = sorted(domains.items(), key=lambda kv: kv[1], reverse=True)[:10]
        lines.append("Domains: " + ", ".join(f"{d}={c}" for d, c in top))
        try:
            from homeassistant.helpers import area_registry as ar

            areas = ar.async_get(hass).async_list_areas()
            if areas:
                lines.append("Bereiche: " + ", ".join(a.name for a in areas[:20]))
        except Exception:  # noqa: BLE001 - optional registry
            pass
        notable = [
            s
            for s in states
            if s.domain in ("sensor", "binary_sensor", "climate", "light", "switch")
            and s.state not in ("unknown", "unavailable")
        ]
        lines.append("Beispiel-Entitaeten:")
        for state in notable[:25]:
            unit = state.attributes.get("unit_of_measurement", "")
            lines.append(f"  {state.entity_id} = {state.state} {unit}".rstrip())
        return "\n".join(lines)

    async def async_start(self) -> None:
        self.manager = self._build_manager()
        if self.scratchpad is not None:
            await self.hass.async_add_executor_job(self.scratchpad.wait_ready)
        # Restore the plan/task graph from the previous HA run so a restart
        # does not lose work in flight.
        store = Store(self.hass, 1, f"{DOMAIN}_state_{self.entry.entry_id}")
        self._store = store
        try:
            saved = await store.async_load()
        except Exception:  # noqa: BLE001 - a broken file must not block setup
            saved = None
        restored = self.manager.load_state(saved)
        if restored:
            _LOGGER.info("KIM: %d Aufgaben aus dem letzten Lauf wiederhergestellt", restored)
        await self.manager.start()
        # Give the archive a shared HTTP session and a local fallback folder.
        if self.archive is not None:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            self._archive_session = session
            self.archive.attach(session, self.hass.config.path(f".storage/{DOMAIN}_archive"))
            entry_archive = self.entry.options.get(CONF_ARCHIVE_REPO) or self.entry.data.get(CONF_ARCHIVE_REPO)
            if entry_archive:
                self.archive.repo = str(entry_archive).strip().strip("/")

        self.coordinator = DataUpdateCoordinator(
            self.hass,
            _LOGGER,
            name=f"{DOMAIN} manager",
            update_method=self._async_update,
            update_interval=UPDATE_INTERVAL,
            config_entry=self.entry,
        )
        await self.coordinator.async_config_entry_first_refresh()
        self._register_services()

    async def async_stop(self) -> None:
        await self._async_persist()
        if self.coordinator is not None:
            await self.coordinator.async_shutdown()
        if self.manager is not None:
            await self.manager.stop()
        if self.scratchpad is not None:
            await self.hass.async_add_executor_job(self.scratchpad.close)
        session = getattr(self, "_archive_session", None)
        if session is not None and not session.closed:
            await session.close()

    async def _async_persist(self) -> None:
        store = getattr(self, "_store", None)
        if store is None or self.manager is None:
            return
        try:
            await store.async_save(self.manager.dump_state())
        except Exception:  # noqa: BLE001 - persistence is best effort
            _LOGGER.warning("KIM: Zustand konnte nicht gespeichert werden", exc_info=True)

    async def _async_update(self) -> dict[str, Any]:
        assert self.manager is not None
        await self.manager.tick()
        snapshot = self.manager.snapshot()
        await self._async_persist()
        return snapshot

    # ------------------------------------------------------------------
    def _register_services(self) -> None:
        hass = self.hass
        manager = self.manager
        assert manager is not None

        async def _submit(call: ServiceCall) -> None:
            goal = call.data.get("goal") or call.data.get("text", "")
            if not goal:
                _LOGGER.warning("KIM submit called without a goal")
                return
            await manager.submit(goal, title=call.data.get("title"), plan_json=call.data.get("plan"))
            if self.coordinator is not None:
                await self.coordinator.async_request_refresh()

        async def _cancel(call: ServiceCall) -> None:
            await manager.cancel(call.data["task_id"])

        async def _retry(call: ServiceCall) -> None:
            await manager.retry(call.data["task_id"])

        async def _pause(call: ServiceCall) -> None:
            manager.pause()

        async def _resume(call: ServiceCall) -> None:
            await manager.resume()

        async def _note(call: ServiceCall) -> None:
            manager.note(
                call.data.get("author", "user"), call.data.get("text", ""), call.data.get("task_id")
            )

        async def _fetch_file(call: ServiceCall) -> None:
            data = await self._fetch_file(call.data["task_id"], call.data["path"],
                                          call.data.get("save_as"))
            self.hass.bus.async_fire(f"{DOMAIN}_artifact", data)

        async def _collect(call: ServiceCall) -> None:
            data = await self._collect_artifacts(call.data["task_id"])
            self.hass.bus.async_fire(f"{DOMAIN}_artifact", data)

        async def _archive(call: ServiceCall) -> None:
            data = await self._archive_run()
            self.hass.bus.async_fire(f"{DOMAIN}_archive", data)

        async def _archive_list(call: ServiceCall) -> None:
            data = await self._archive_list_runs()
            self.hass.bus.async_fire(f"{DOMAIN}_archive_list", data)

        async def _archive_load(call: ServiceCall) -> None:
            data = await self._archive_load_run(call.data["name"])
            self.hass.bus.async_fire(f"{DOMAIN}_archive_loaded", data)

        handlers = {
            SERVICE_SUBMIT: _submit,
            SERVICE_CANCEL: _cancel,
            SERVICE_RETRY: _retry,
            SERVICE_PAUSE: _pause,
            SERVICE_RESUME: _resume,
            SERVICE_NOTE: _note,
            SERVICE_FETCH_FILE: _fetch_file,
            SERVICE_COLLECT_ARTIFACTS: _collect,
            SERVICE_ARCHIVE: _archive,
            SERVICE_ARCHIVE_LIST: _archive_list,
            SERVICE_ARCHIVE_LOAD: _archive_load,
        }
        for name, handler in handlers.items():
            hass.services.async_register(DOMAIN, name, handler, schema=SCHEMAS[name])

    # ------------------------------------------------------------------
    # artifacts
    # ------------------------------------------------------------------
    def _artifact_root(self) -> str:
        root = self.hass.config.path(ARTIFACT_DIR)
        os.makedirs(root, exist_ok=True)
        return root

    @staticmethod
    def _safe_name(name: str) -> str:
        """Keep a user-supplied file name inside the artifact directory."""
        name = os.path.basename(name.replace("\\", "/")).strip()
        return name or "artifact.bin"

    async def _fetch_file(self, task_id: str, path: str, save_as: str | None = None) -> dict[str, Any]:
        assert self.manager is not None
        content = await self.manager.fetch_task_file(task_id, path)
        target = self._safe_name(save_as or path)
        dest = os.path.join(self._artifact_root(), target)
        await self.hass.async_add_executor_job(_write_bytes, dest, content)
        return {"task_id": task_id, "path": path, "saved": f"/local/kim/{target}",
                "bytes": len(content)}

    async def _collect_artifacts(self, task_id: str) -> dict[str, Any]:
        """Unpack the agent workspace into www/kim/<task_id>/ and list the files.

        The workspace archive is a tar.gz of the real project files, so any
        deliverable the agent produced (PDF, report, code) ends up published
        under ``/local/kim/<task_id>/`` where Home Assistant serves it.
        """
        assert self.manager is not None
        archive = await self.manager.fetch_task_archive(task_id)
        target = self._safe_name(task_id)
        dest_dir = os.path.join(self._artifact_root(), target)
        files = await self.hass.async_add_executor_job(
            _unpack_archive, archive, dest_dir, target
        )
        return {
            "task_id": task_id,
            "saved": f"/local/kim/{target}/",
            "bytes": len(archive),
            "files": files,
        }

    async def collect_all_artifacts(self) -> dict[str, Any]:
        """Collect the workspace of every task that ran in the current plan."""
        assert self.manager is not None
        collected: list[dict[str, Any]] = []
        for task in self.manager.tasks.values():
            if task.state != TASK_DONE:
                continue
            try:
                collected.append(await self._collect_artifacts(task.task_id))
            except Exception as err:  # noqa: BLE001 - one bad task must not stop the rest
                collected.append({"task_id": task.task_id, "error": str(err)[:200]})
        return {"collected": collected}

    async def _save_worker_workspace(self, worker: Worker) -> None:
        """Persist an agent's workspace before its sandbox is destroyed.

        Wired into ``Manager.before_teardown`` so ``cleanup_when_idle`` never
        throws away deliverables. Each finished task gets its own folder; the
        saved file list is kept per task and exposed through the snapshot.
        """
        assert self.manager is not None
        done = [
            t
            for t in self.manager.tasks.values()
            if t.assigned_to == worker.worker_id and t.state == TASK_DONE
        ]
        if not done:
            return
        try:
            archive = await self.manager.fetch_task_archive(done[-1].task_id)
        except Exception as err:  # noqa: BLE001 - teardown must still proceed
            _LOGGER.warning("KIM: workspace of %s not saved: %s", worker.worker_id, err)
            return
        for task in done:
            label = self._safe_name(task.task_id)
            dest_dir = os.path.join(self._artifact_root(), label)
            try:
                files = await self.hass.async_add_executor_job(
                    _unpack_archive, archive, dest_dir, label
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("KIM: could not unpack %s: %s", label, err)
                continue
            self._artifacts[task.task_id] = files
            self.scratchpad_note(f"{len(files)} Dateien gesichert unter /local/kim/{label}/")

    def scratchpad_note(self, text: str) -> None:
        if self.manager is not None:
            self.manager.scratchpad.post(KIND_LOG, "manager", text)

    def artifacts_summary(self) -> list[dict[str, Any]]:
        """Per-task published files, newest task first, for the panel."""
        if self.manager is None:
            return []
        order = {tid: i for i, tid in enumerate(self.manager.tasks.keys())}
        out: list[dict[str, Any]] = []
        for task_id, files in self._artifacts.items():
            task = self.manager.tasks.get(task_id)
            out.append({
                "task_id": task_id,
                "title": task.title if task else task_id,
                "role": task.role if task else "?",
                "dir": f"/local/kim/{task_id}/",
                "files": files,
            })
        out.sort(key=lambda e: order.get(e["task_id"], 0), reverse=True)
        return out

    # ------------------------------------------------------------------
    # off-device archive
    # ------------------------------------------------------------------
    def _run_payload(self) -> dict[str, Any]:
        assert self.manager is not None
        snap = self.manager.snapshot()
        return {
            "saved_at": snap.get("uptime"),
            "backend": self._backend_status(),
            "plans": snap.get("plans", []),
            "notes": snap.get("notes", []),
        }

    def _backend_status(self) -> dict[str, Any]:
        if isinstance(self.manager.backend, FailoverBackend):
            return self.manager.backend.status()
        return {"active": getattr(self.manager.backend, "backend", "?"), "chain": []}

    def backend_is_degraded(self) -> bool:
        """True when agents run on a fallback (mock/local) instead of the cloud."""
        status = self._backend_status()
        return bool(status.get("degraded")) or status.get("active") == BACKEND_MOCK

    async def _archive_run(self) -> dict[str, Any]:
        if self.archive is None:
            return {"ok": False, "error": "Archiv nicht verfuegbar"}
        name = f"kim-run-{int(time.time())}"
        result = await self.archive.save_run(name, self._run_payload())
        result["name"] = name
        return result

    async def _archive_list_runs(self) -> dict[str, Any]:
        if self.archive is None:
            return {"runs": [], "target": "?"}
        return {"runs": await self.archive.list_runs(), "target": self.archive.target}

    async def _archive_load_run(self, name: str) -> dict[str, Any]:
        if self.archive is None:
            return {"ok": False, "error": "Archiv nicht verfuegbar"}
        data = await self.archive.load_run(name)
        if data is None:
            return {"ok": False, "name": name, "error": "Lauf nicht gefunden"}
        restored = 0
        if self.manager is not None:
            restored = self.manager.restore_archive(data)
            await self._async_persist()
            if self.coordinator is not None:
                await self.coordinator.async_request_refresh()
        return {"ok": True, "name": name, "restored": restored, "run": data}


def _write_bytes(path: str, content: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(content)


def _unpack_archive(archive: bytes, dest_dir: str, label: str) -> list[dict[str, Any]]:
    """Extract a tar.gz/zip workspace archive and return the published files.

    Guards against path traversal so a malicious archive cannot write outside
    the artifact directory, and reports a size/summary per file for the panel.
    """
    import io
    import tarfile
    import zipfile

    os.makedirs(dest_dir, exist_ok=True)
    root = os.path.realpath(dest_dir)
    files: list[dict[str, Any]] = []

    def _safe_member(name: str) -> str | None:
        target = os.path.realpath(os.path.join(dest_dir, name))
        if target != root and not target.startswith(root + os.sep):
            return None
        return target

    if archive[:2] == b"\x1f\x8b":
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                target = _safe_member(member.name)
                if target is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                data = handle.read()
                with open(target, "wb") as out:
                    out.write(data)
                files.append(_artifact_entry(member.name, len(data), label))
    elif archive[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = _safe_member(info.filename)
                if target is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                data = zf.read(info)
                with open(target, "wb") as out:
                    out.write(data)
                files.append(_artifact_entry(info.filename, len(data), label))
    else:
        # Unknown container: keep the raw bytes so nothing is lost.
        raw = os.path.join(dest_dir, f"{label}.bin")
        with open(raw, "wb") as out:
            out.write(archive)
        files.append({"path": f"{label}.bin", "url": f"/local/kim/{label}/{label}.bin",
                      "bytes": len(archive), "kind": "binary"})

    return files


def _artifact_entry(name: str, size: int, label: str) -> dict[str, Any]:
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    return {
        "path": name,
        "url": f"/local/kim/{label}/{name}",
        "bytes": size,
        "kind": ext or "file",
    }
