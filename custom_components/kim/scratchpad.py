"""Shared scratchpad used by all agents to communicate.

The scratchpad is an append-only JSONL file. Appends use an advisory file
lock (``fcntl`` on POSIX) so that several agent processes can write to the
same file concurrently without corrupting it or overwriting each other. This
is what lets the agents "exchange notes" without interfering with each other.

Reads are served from an in-memory copy and all disk I/O happens on a single
background writer thread. That keeps the Home Assistant event loop free of
blocking file access while preserving the synchronous, framework-free API the
engine uses.

Message kinds:
  - ``note``: free-form information for other agents
  - ``claim``: an agent claims a task (with a TTL, so stale claims expire)
  - ``result``: an agent publishes the result of a task
  - ``heartbeat``: liveness signal
  - ``log``: structured progress line
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

try:  # POSIX only; Windows fallback below
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX
    fcntl = None  # type: ignore[assignment]

KIND_NOTE = "note"
KIND_CLAIM = "claim"
KIND_RESULT = "result"
KIND_HEARTBEAT = "heartbeat"
KIND_LOG = "log"


@dataclass
class Note:
    kind: str
    author: str
    text: str = ""
    task_id: str | None = None
    role: str | None = None
    ts: float = field(default_factory=time.time)
    ttl: float | None = None
    data: dict[str, Any] = field(default_factory=dict)
    # transient notes (heartbeats) are kept in memory only so the on-disk log
    # does not grow without bound
    persist: bool = True

    @property
    def expired(self) -> bool:
        return self.ttl is not None and time.time() > self.ts + self.ttl

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "author": self.author,
            "text": self.text,
            "task_id": self.task_id,
            "role": self.role,
            "ts": self.ts,
        }
        if self.ttl is not None:
            out["ttl"] = self.ttl
        if self.data:
            out["data"] = self.data
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Note":
        return cls(
            kind=str(data.get("kind", KIND_NOTE)),
            author=str(data.get("author", "unknown")),
            text=str(data.get("text", "")),
            task_id=data.get("task_id"),
            role=data.get("role"),
            ts=float(data.get("ts", time.time())),
            ttl=data.get("ttl"),
            data=dict(data.get("data", {})),
        )


def _read_notes_file(path: str) -> list[Note]:
    notes: list[Note] = []
    if not os.path.exists(path):
        return notes
    with open(path, "r", encoding="utf-8") as fh:
        _lock(fh)
        try:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    notes.append(Note.from_dict(json.loads(line)))
                except (json.JSONDecodeError, ValueError):
                    # skip a partially written / corrupt line
                    continue
        finally:
            _unlock(fh)
    return notes


def _lock(fh) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock(fh) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _append_line(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        _lock(fh)
        try:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            _unlock(fh)


def _write_lines(path: str, lines: list[str]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
    os.replace(tmp, path)


class Scratchpad:
    """Append-only, lock-protected shared notebook.

    All public methods are synchronous and non-blocking: they only touch
    in-memory state. Persistence is handled by a single daemon writer thread
    started lazily on first use, so callers on an event loop never block.
    """

    def __init__(self, path: str, max_entries: int = 5000) -> None:
        self.path = path
        self.max_entries = max_entries
        self._notes: list[Note] = []
        self._lock = threading.RLock()
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loaded = threading.Event()
        self._closed = False
        self._since_compact = 0
        self._ensure_thread()
        self._queue.put(("load", os.path.dirname(path)))

    # -- persistence plumbing ---------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._writer_loop, name="kim-scratchpad-writer", daemon=True
            )
            self._thread.start()

    def _writer_loop(self) -> None:
        while True:
            op, payload = self._queue.get()
            try:
                if op == "load":
                    self._load(payload)
                elif op == "append":
                    _append_line(self.path, payload)
                elif op == "compact":
                    _write_lines(self.path, payload)
                elif op == "stop":
                    return
            except Exception:  # noqa: BLE001 - a writer error must not kill the thread
                continue
            finally:
                self._queue.task_done()

    def _load(self, directory: str | None) -> None:
        if directory:
            os.makedirs(directory, exist_ok=True)
        if not os.path.exists(self.path):
            open(self.path, "a", encoding="utf-8").close()
            self._loaded.set()
            return
        loaded = _read_notes_file(self.path)
        with self._lock:
            # keep anything posted before the load finished
            self._notes = loaded + self._notes
        self._loaded.set()

    def wait_ready(self, timeout: float = 5.0) -> None:
        """Block until existing notes are loaded (used by tests/startup)."""
        self._ensure_thread()
        self._loaded.wait(timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._queue.put(("stop", None))
            self._thread.join(timeout=2.0)

    # -- writing -----------------------------------------------------------
    def append(self, note: Note) -> None:
        """Record one note. In-memory is immediate; disk write is async."""
        with self._lock:
            self._notes.append(note)
            if len(self._notes) > self.max_entries * 2:
                self._prune_locked()
        if not note.persist:
            return
        self._ensure_thread()
        line = json.dumps(note.to_dict(), ensure_ascii=False)
        self._queue.put(("append", line))
        self._maybe_compact()

    def _prune_locked(self) -> None:
        """Drop transient notes and, if needed, the oldest persistent ones."""
        keep = [n for n in self._notes if n.persist]
        transient = [n for n in self._notes if not n.persist][-self.max_entries :]
        merged = (keep + transient)[-self.max_entries :]
        self._notes = sorted(merged, key=lambda n: n.ts)

    def _maybe_compact(self) -> None:
        """Compact the file once it has grown past the soft limit."""
        self._since_compact = getattr(self, "_since_compact", 0) + 1
        if self._since_compact >= self.max_entries:
            self._since_compact = 0
            self.compact()

    def post(
        self,
        kind: str,
        author: str,
        text: str = "",
        *,
        task_id: str | None = None,
        role: str | None = None,
        ttl: float | None = None,
        data: dict[str, Any] | None = None,
    ) -> Note:
        note = Note(
            kind=kind,
            author=author,
            text=text,
            task_id=task_id,
            role=role,
            ttl=ttl,
            data=data or {},
        )
        self.append(note)
        return note

    def heartbeat(self, author: str, *, role: str | None = None) -> Note:
        """Post a transient heartbeat (memory only, never persisted)."""
        note = Note(kind=KIND_HEARTBEAT, author=author, role=role, persist=False)
        with self._lock:
            self._notes.append(note)
            if len(self._notes) > self.max_entries * 2:
                self._prune_locked()
        return note

    # -- reading -----------------------------------------------------------
    def read_all(self) -> list[Note]:
        with self._lock:
            return list(self._notes)

    def read_recent(self, limit: int = 50, *, exclude_kinds: set[str] | None = None) -> list[Note]:
        notes = self.read_all()
        if exclude_kinds:
            notes = [n for n in notes if n.kind not in exclude_kinds]
        return notes[-limit:]

    def claims(self, now: float | None = None) -> dict[str, Note]:
        """Return the newest non-expired claim per task_id."""
        now = now if now is not None else time.time()
        claims: dict[str, Note] = {}
        for note in self.read_all():
            if note.kind != KIND_CLAIM or not note.task_id:
                continue
            if note.ttl is not None and now > note.ts + note.ttl:
                continue
            current = claims.get(note.task_id)
            if current is None or note.ts >= current.ts:
                claims[note.task_id] = note
        return claims

    def results(self) -> dict[str, Note]:
        """Return the newest result per task_id."""
        results: dict[str, Note] = {}
        for note in self.read_all():
            if note.kind != KIND_RESULT or not note.task_id:
                continue
            current = results.get(note.task_id)
            if current is None or note.ts >= current.ts:
                results[note.task_id] = note
        return results

    def heartbeats(self, window: float) -> dict[str, float]:
        """Return the latest heartbeat timestamp per author within a window."""
        now = time.time()
        beats: dict[str, float] = {}
        for note in self.read_all():
            if note.kind != KIND_HEARTBEAT:
                continue
            if now - note.ts > window:
                continue
            beats[note.author] = max(beats.get(note.author, 0.0), note.ts)
        return beats

    def compact(self) -> int:
        """Rewrite the file keeping only the newest ``max_entries`` notes.

        Also trims the in-memory persistent history; transient notes
        (heartbeats) are retained separately. Returns the number of notes kept.
        """
        with self._lock:
            kept = [n for n in self._notes if n.persist][-self.max_entries :]
            transient = [n for n in self._notes if not n.persist][-self.max_entries :]
            self._notes = sorted(kept + transient, key=lambda n: n.ts)
        self._ensure_thread()
        lines = [json.dumps(n.to_dict(), ensure_ascii=False) for n in kept]
        self._queue.put(("compact", lines))
        return len(kept)
