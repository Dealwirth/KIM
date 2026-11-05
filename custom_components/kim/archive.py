"""Off-device archive for KIM runs.

Home Assistant storage is small, so completed runs (plan, tasks, results,
scratchpad) are pushed to a free GitHub repository as JSON. When no repository
is configured the archive degrades to a bounded local ring buffer inside the HA
config directory, so nothing is ever lost silently and nothing grows without
limit.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
LOCAL_KEEP = 25  # how many runs the local fallback keeps


class ArchiveStore:
    """Persist run snapshots to GitHub, or to a bounded local file."""

    def __init__(self, repo: str = "", token: str = "", *, local_dir: str = "") -> None:
        self.repo = (repo or "").strip().strip("/")
        self.token = token or ""
        self.local_dir = local_dir
        self._session: aiohttp.ClientSession | None = None

    # -- setup ----------------------------------------------------------
    def attach(self, session: aiohttp.ClientSession, local_dir: str | None = None) -> None:
        self._session = session
        if local_dir:
            self.local_dir = local_dir

    @property
    def enabled(self) -> bool:
        return bool(self.repo and self.token)

    @property
    def target(self) -> str:
        return f"github:{self.repo}" if self.enabled else "lokal"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # -- public API -----------------------------------------------------
    async def save_run(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Store one run. Returns ``{target, path, ok}``."""
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:120]
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = f"runs/{stamp}-{safe}.json"
        body = json.dumps(payload, ensure_ascii=False, indent=1)
        if self.enabled and self._session is not None:
            ok = await self._github_put(path, body)
            if ok:
                return {"target": self.target, "path": path, "ok": True}
            _LOGGER.warning("KIM: GitHub-Archiv fehlgeschlagen, nutze lokalen Speicher")
        return {"target": "lokal", "path": await self._local_put(name, payload), "ok": True}

    async def list_runs(self) -> list[dict[str, Any]]:
        if self.enabled and self._session is not None:
            runs = await self._github_list()
            if runs is not None:
                return runs
        return self._local_list()

    async def load_run(self, name: str) -> dict[str, Any] | None:
        if self.enabled and self._session is not None:
            data = await self._github_get(name)
            if data is not None:
                return data
        return self._local_get(name)

    # -- GitHub ---------------------------------------------------------
    async def _github_put(self, path: str, body: str) -> bool:
        assert self._session is not None
        url = f"{GITHUB_API}/repos/{self.repo}/contents/{path}"
        payload: dict[str, Any] = {
            "message": f"KIM Archiv {path}",
            "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        }
        try:
            async with self._session.put(url, json=payload, headers=self._headers()) as resp:
                if resp.status in (200, 201):
                    return True
                _LOGGER.warning("KIM: GitHub-Archiv PUT %s -> %s", path, resp.status)
        except Exception as err:  # noqa: BLE001 - archive is best effort
            _LOGGER.warning("KIM: GitHub-Archiv nicht erreichbar: %s", err)
        return False

    async def _github_list(self) -> list[dict[str, Any]] | None:
        assert self._session is not None
        url = f"{GITHUB_API}/repos/{self.repo}/contents/runs"
        try:
            async with self._session.get(url, headers=self._headers()) as resp:
                if resp.status == 404:
                    return []
                if resp.status >= 400:
                    return None
                items = await resp.json()
        except Exception:  # noqa: BLE001
            return None
        return [
            {"name": i.get("name"), "size": i.get("size"), "path": i.get("path")}
            for i in items
            if isinstance(i, dict) and str(i.get("name", "")).endswith(".json")
        ]

    async def _github_get(self, name: str) -> dict[str, Any] | None:
        assert self._session is not None
        url = f"{GITHUB_API}/repos/{self.repo}/contents/runs/{name}"
        try:
            async with self._session.get(url, headers=self._headers()) as resp:
                if resp.status >= 400:
                    return None
                data = await resp.json()
            raw = base64.b64decode(data.get("content", "")).decode("utf-8")
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return None

    # -- local fallback -------------------------------------------------
    def _local_file(self) -> str:
        return os.path.join(self.local_dir, "archive.json")

    def _read_local(self) -> list[dict[str, Any]]:
        try:
            with open(self._local_file(), encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    async def _local_put(self, name: str, payload: dict[str, Any]) -> str:
        runs = self._read_local()
        runs.append({"name": name, "saved_at": time.time(), "payload": payload})
        runs = runs[-LOCAL_KEEP:]
        os.makedirs(self.local_dir, exist_ok=True)
        with open(self._local_file(), "w", encoding="utf-8") as handle:
            json.dump(runs, handle, ensure_ascii=False)
        return f"lokal:{name}"

    def _local_list(self) -> list[dict[str, Any]]:
        return [
            {"name": r.get("name"), "size": len(json.dumps(r.get("payload", {}))), "path": "lokal"}
            for r in self._read_local()
        ]

    def _local_get(self, name: str) -> dict[str, Any] | None:
        for run in reversed(self._read_local()):
            if run.get("name") == name:
                return run.get("payload")
        return None
