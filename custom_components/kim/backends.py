"""Agent backends: how KIM actually drives OpenHands agents.

A backend knows how to create an agent instance, send it work, poll its
status, read its event stream and shut it down. Three implementations:

* :class:`CloudBackend`  - OpenHands Cloud (``/api/v1/app-conversations``)
* :class:`LocalBackend`  - a local agent server (``/api/conversations``)
* :class:`MockBackend`   - deterministic in-process fake, used for tests and
  for a fully offline demo. It never touches the network.

The cloud/local backends talk to the sandbox **agent server** for messaging and
events (``X-Session-API-Key``) and to the app server for lifecycle
(``Authorization: Bearer``).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from .const import (
    BACKEND_CLOUD,
    BACKEND_LOCAL,
    BACKEND_MOCK,
    STATE_DONE,
    STATE_FAILED,
    STATE_WORKING,
)
from .models import Worker, new_id

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=60)
# Sandbox agent-server event stream (X-Session-API-Key).
MESSAGE_ENDPOINT = "/api/conversations/{cid}/events"
# Cloud app-server endpoints (Authorization: Bearer).
APP_CONVERSATIONS = "/api/v1/app-conversations"
APP_SEND_MESSAGE = "/api/v1/app-conversations/{cid}/send-message"
APP_EVENTS = "/api/v1/conversation/{cid}/events/search"
APP_START_TASKS = "/api/v1/app-conversations/start-tasks"


class BackendError(RuntimeError):
    """Raised when a backend call fails."""


def _items(data: Any) -> list[dict[str, Any]]:
    """Normalise the several list shapes the OpenHands API returns."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return items
        if "id" in data:
            return [data]
    return []


class BaseBackend:
    def __init__(self, *, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()

    # interface -----------------------------------------------------------
    async def start_worker(self, role: str, model: str, repository: str | None = None,
                           branch: str | None = None) -> Worker:  # pragma: no cover
        raise NotImplementedError

    async def send_task(self, worker: Worker, prompt: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_status(self, worker: Worker) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def read_events(self, worker: Worker, limit: int = 20) -> list[dict[str, Any]]:
        return []

    async def fetch_file(self, worker: Worker, path: str) -> bytes:
        """Read a single file from the agent workspace. Optional per backend."""
        raise NotImplementedError

    async def fetch_archive(self, worker: Worker) -> bytes:
        """Read the whole workspace as a zip archive. Optional per backend."""
        raise NotImplementedError

    async def stop_worker(self, worker: Worker) -> None:
        return None


# ---------------------------------------------------------------------------
# Cloud backend
# ---------------------------------------------------------------------------
class CloudBackend(BaseBackend):
    def __init__(self, base_url: str, api_key: str, *, session: aiohttp.ClientSession | None = None) -> None:
        super().__init__(session=session)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _message_payload(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        async with session.post(url, json=payload, headers=self._auth_headers) as resp:
            body = await self._read_json(resp)
            if resp.status >= 400:
                raise BackendError(f"POST {path} -> {resp.status}: {body}")
            return body

    async def _get(self, path: str) -> Any:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        async with session.get(url, headers=self._auth_headers) as resp:
            body = await self._read_json(resp)
            if resp.status >= 400:
                raise BackendError(f"GET {path} -> {resp.status}: {body}")
            return body

    async def _get_bytes(self, path: str) -> bytes:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        async with session.get(url, headers=self._auth_headers) as resp:
            if resp.status >= 400:
                raise BackendError(f"GET {path} -> {resp.status}")
            return await resp.read()

    @staticmethod
    async def _read_json(resp: aiohttp.ClientResponse) -> Any:
        try:
            return await resp.json()
        except Exception:  # noqa: BLE001 - non-JSON error bodies
            text = await resp.text()
            return {"raw": text[:1000]}

    async def start_worker(self, role, model, repository=None, branch=None) -> Worker:
        payload: dict[str, Any] = {
            "initial_message": self._message_payload(
                f"Du bist der KIM-Agent mit der Rolle '{role}'. "
                "Melde dich bereit und warte auf Aufgaben. Antworte knapp."
            ),
            "title": f"KIM {role}",
            "trigger": "openhands_api",
            "llm_model": model,
        }
        if repository:
            payload["selected_repository"] = repository
        if branch:
            payload["selected_branch"] = branch

        start = await self._post(APP_CONVERSATIONS, payload)
        app_conversation_id = start.get("app_conversation_id")
        if not app_conversation_id:
            app_conversation_id = await self._poll_start_task(str(start.get("id", "")))

        record = await self._conversation_record(app_conversation_id)
        worker = Worker(
            worker_id=new_id("worker"),
            role=role,
            backend=BACKEND_CLOUD,
            conversation_id=app_conversation_id,
        )
        self._apply_runtime_info(worker, record)
        return worker

    async def _poll_start_task(self, start_task_id: str, timeout: float = 300.0) -> str:
        if not start_task_id:
            raise BackendError("start task without id")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = await self._get(f"{APP_START_TASKS}?ids={start_task_id}")
            for item in _items(data):
                if item.get("app_conversation_id"):
                    return str(item["app_conversation_id"])
                if item.get("status") == "ERROR":
                    raise BackendError(f"start task failed: {item.get('detail') or item}")
            await asyncio.sleep(3)
        raise BackendError("timed out waiting for start task")

    async def _conversation_record(self, cid: str) -> dict[str, Any]:
        data = await self._get(f"{APP_CONVERSATIONS}?ids={cid}")
        items = _items(data)
        return items[0] if items else {}

    @staticmethod
    def _apply_runtime_info(worker: Worker, record: dict[str, Any]) -> None:
        worker.sandbox_status = record.get("sandbox_status")
        worker.sandbox_id = record.get("sandbox_id")
        session_key = record.get("session_api_key")
        conv_url = record.get("conversation_url") or ""
        if session_key:
            worker.session_api_key = session_key
        if conv_url and "/api/conversations" in conv_url:
            worker.agent_server_url = conv_url.rsplit("/api/conversations", 1)[0]

    async def send_task(self, worker: Worker, prompt: str) -> None:
        await self._post(
            APP_SEND_MESSAGE.format(cid=worker.conversation_id),
            {**self._message_payload(prompt), "run": True},
        )

    async def get_status(self, worker: Worker) -> dict[str, Any]:
        record = await self._conversation_record(str(worker.conversation_id))
        self._apply_runtime_info(worker, record)
        return {
            "state": _cloud_state(record.get("execution_status"), worker.state),
            "execution_status": record.get("execution_status"),
            "sandbox_status": record.get("sandbox_status"),
        }

    async def read_events(self, worker: Worker, limit: int = 20) -> list[dict[str, Any]]:
        data = await self._get(
            f"{APP_EVENTS.format(cid=worker.conversation_id)}"
            f"?limit={limit}&sort_order=TIMESTAMP_DESC"
        )
        return [_flatten_event(e) for e in _items(data)]

    async def fetch_file(self, worker: Worker, path: str) -> bytes:
        return await self._get_bytes(
            f"{APP_CONVERSATIONS}/{worker.conversation_id}/file?file_path={path}"
        )

    async def fetch_archive(self, worker: Worker) -> bytes:
        return await self._get_bytes(f"{APP_CONVERSATIONS}/{worker.conversation_id}/download")

    async def stop_worker(self, worker: Worker) -> None:
        try:
            session = await self._get_session()
            url = f"{self.base_url}{APP_CONVERSATIONS}/{worker.conversation_id}"
            await session.delete(url, headers=self._auth_headers)
        except Exception:  # noqa: BLE001 - best effort teardown
            return None


def _cloud_state(execution_status: Any, fallback: str) -> str:
    """Map an OpenHands execution status onto a KIM worker state."""
    if execution_status in ("running", "waiting_for_confirmation"):
        return STATE_WORKING
    if execution_status in ("idle", "paused", "finished"):
        return STATE_DONE
    if execution_status in ("error", "stuck"):
        return STATE_FAILED
    return fallback


def _flatten_event(event: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw OpenHands event into ``{kind, source, text, ...}``."""
    out: dict[str, Any] = dict(event)
    llm = event.get("llm_message") or event.get("message")
    text = event.get("text")
    if isinstance(llm, dict):
        out.setdefault("source", llm.get("role") or event.get("sender"))
        parts = []
        for block in llm.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            text = "\n".join(p for p in parts if p)
        if llm.get("tool_calls"):
            out["tool_calls"] = llm["tool_calls"]
    if text is not None:
        out["text"] = text
    return out


# ---------------------------------------------------------------------------
# Local agent-server backend
# ---------------------------------------------------------------------------
class LocalBackend(BaseBackend):
    def __init__(self, base_url: str, api_key: str, workspace: str = "/workspace",
                 model: str = "openhands/deepseek-v4.1-flash",
                 *, session: aiohttp.ClientSession | None = None) -> None:
        super().__init__(session=session)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.workspace = workspace
        self.model = model

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Session-API-Key": self.api_key}

    async def start_worker(self, role, model, repository=None, branch=None) -> Worker:
        session = await self._get_session()
        payload = {
            "agent": {
                "kind": "Agent",
                "llm": {"model": model or self.model},
                "tools": [{"name": "terminal"}, {"name": "file_editor"}, {"name": "task_tracker"}],
            },
            "workspace": {"kind": "LocalWorkspace", "working_dir": self.workspace},
            "initial_message": {
                "content": [{"text": f"Rolle: {role}. Warte auf Aufgaben."}],
                "run": True,
            },
        }
        url = f"{self.base_url}/api/conversations"
        async with session.post(url, json=payload, headers=self._headers) as resp:
            data = await CloudBackend._read_json(resp)
            if resp.status >= 400:
                raise BackendError(f"POST /api/conversations -> {resp.status}: {data}")
        worker = Worker(
            worker_id=new_id("worker"),
            role=role,
            backend=BACKEND_LOCAL,
            conversation_id=str(data.get("id", "")),
            agent_server_url=self.base_url,
            session_api_key=self.api_key,
        )
        return worker

    async def send_task(self, worker: Worker, prompt: str) -> None:
        session = await self._get_session()
        url = f"{self.base_url}{MESSAGE_ENDPOINT.format(cid=worker.conversation_id)}"
        async with session.post(
            url,
            json={"message": {"content": [{"type": "text", "text": prompt}]}, "run": True},
            headers=self._headers,
        ) as resp:
            if resp.status >= 400:
                body = await CloudBackend._read_json(resp)
                raise BackendError(f"send_task -> {resp.status}: {body}")

    async def get_status(self, worker: Worker) -> dict[str, Any]:
        session = await self._get_session()
        url = f"{self.base_url}/api/conversations/{worker.conversation_id}"
        async with session.get(url, headers=self._headers) as resp:
            data = await CloudBackend._read_json(resp)
            if resp.status >= 400:
                return {"state": STATE_FAILED, "error": data}
        execution = data.get("execution_status") or data.get("status")
        if execution in ("running", "working"):
            state = STATE_WORKING
        elif execution in ("finished", "completed", "idle"):
            state = STATE_DONE
        elif execution in ("error", "failed"):
            state = STATE_FAILED
        else:
            state = worker.state
        return {"state": state, "execution_status": execution}

    async def read_events(self, worker: Worker, limit: int = 20) -> list[dict[str, Any]]:
        session = await self._get_session()
        url = (
            f"{self.base_url}/api/conversations/{worker.conversation_id}"
            f"/events/search?limit={limit}&sort_order=TIMESTAMP_DESC"
        )
        async with session.get(url, headers=self._headers) as resp:
            data = await CloudBackend._read_json(resp)
        return [_flatten_event(e) for e in _items(data)]

    async def fetch_file(self, worker: Worker, path: str) -> bytes:
        session = await self._get_session()
        url = f"{self.base_url}/api/conversations/{worker.conversation_id}/files?path={path}"
        async with session.get(url, headers=self._headers) as resp:
            if resp.status >= 400:
                raise BackendError(f"fetch_file -> {resp.status}")
            return await resp.read()

    async def stop_worker(self, worker: Worker) -> None:
        session = await self._get_session()
        url = f"{self.base_url}/api/conversations/{worker.conversation_id}"
        try:
            await session.delete(url, headers=self._headers)
        except Exception:  # noqa: BLE001 - best effort teardown
            return None


# ---------------------------------------------------------------------------
# Mock backend (offline / tests)
# ---------------------------------------------------------------------------
class MockBackend(BaseBackend):
    """Deterministic fake backend.

    Each dispatched task completes after ``completion_delay`` status polls,
    so tests can advance the manager clock deterministically.
    """

    def __init__(self, completion_delay: int = 1, fail_roles: list[str] | None = None,
                 model: str = "mock/model") -> None:
        super().__init__(session=None)
        self.completion_delay = completion_delay
        self.fail_roles = set(fail_roles or [])
        self.model = model
        self.created: list[Worker] = []
        self.prompts: dict[str, list[str]] = {}
        self._poll_counts: dict[str, int] = {}

    async def close(self) -> None:
        return None

    async def start_worker(self, role, model, repository=None, branch=None) -> Worker:
        worker = Worker(
            worker_id=new_id("worker"),
            role=role,
            backend=BACKEND_MOCK,
            conversation_id=new_id("mock"),
        )
        self.created.append(worker)
        return worker

    async def send_task(self, worker: Worker, prompt: str) -> None:
        cid = str(worker.conversation_id)
        self.prompts.setdefault(cid, []).append(prompt)
        self._poll_counts[cid] = 0

    async def get_status(self, worker: Worker) -> dict[str, Any]:
        cid = str(worker.conversation_id)
        self._poll_counts[cid] = self._poll_counts.get(cid, 0) + 1
        if worker.role in self.fail_roles:
            return {"state": STATE_FAILED, "execution_status": "error"}
        if self._poll_counts[cid] >= self.completion_delay:
            return {"state": STATE_DONE, "execution_status": "finished"}
        return {"state": STATE_WORKING, "execution_status": "running"}

    async def read_events(self, worker: Worker, limit: int = 20) -> list[dict[str, Any]]:
        cid = str(worker.conversation_id)
        return [
            {
                "kind": "MessageEvent",
                "source": "assistant",
                "text": f"mock result for {worker.role}",
                "prompt": p,
            }
            for p in self.prompts.get(cid, [])
        ][-limit:]

    async def fetch_file(self, worker: Worker, path: str) -> bytes:
        return f"mock file {path} for {worker.role}".encode()

    async def fetch_archive(self, worker: Worker) -> bytes:
        return b"PK\x03\x04mock-archive"


def create_backend(backend_type: str, **kwargs) -> BaseBackend:
    if backend_type == BACKEND_CLOUD:
        return CloudBackend(kwargs["base_url"], kwargs["api_key"])
    if backend_type == BACKEND_LOCAL:
        return LocalBackend(
            kwargs["base_url"],
            kwargs["api_key"],
            workspace=kwargs.get("workspace", "/workspace"),
            model=kwargs.get("model", "openhands/deepseek-v4.1-flash"),
        )
    if backend_type == BACKEND_MOCK:
        return MockBackend(
            completion_delay=int(kwargs.get("completion_delay", 1)),
            fail_roles=kwargs.get("fail_roles"),
        )
    raise ValueError(f"unknown backend: {backend_type}")
