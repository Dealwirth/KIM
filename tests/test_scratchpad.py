"""Tests for the concurrent scratchpad."""
from __future__ import annotations

import threading

from custom_components.kim.scratchpad import (
    KIND_CLAIM,
    KIND_RESULT,
    Note,
    Scratchpad,
)


def test_append_and_read(tmp_path):
    pad = Scratchpad(str(tmp_path / "notes.jsonl"))
    pad.post("note", "agent-a", "hallo")
    pad.post("note", "agent-b", "welt")
    notes = pad.read_all()
    assert [n.text for n in notes] == ["hallo", "welt"]
    assert notes[0].author == "agent-a"


def test_recent_limit(tmp_path):
    pad = Scratchpad(str(tmp_path / "notes.jsonl"))
    for i in range(10):
        pad.post("note", "a", str(i))
    recent = pad.read_recent(3)
    assert [n.text for n in recent] == ["7", "8", "9"]


def test_claims_respect_ttl(tmp_path):
    pad = Scratchpad(str(tmp_path / "notes.jsonl"))
    pad.post(KIND_CLAIM, "w1", "mine", task_id="t1", ttl=1000)
    pad.post(KIND_CLAIM, "w2", "mine too", task_id="t2", ttl=-1)  # already expired
    claims = pad.claims()
    assert set(claims) == {"t1"}
    assert claims["t1"].author == "w1"


def test_latest_claim_wins(tmp_path):
    pad = Scratchpad(str(tmp_path / "notes.jsonl"))
    pad.post(KIND_CLAIM, "w1", "first", task_id="t1", ttl=1000)
    pad.post(KIND_CLAIM, "w2", "second", task_id="t1", ttl=1000)
    claims = pad.claims()
    assert claims["t1"].author == "w2"


def test_results_latest_wins(tmp_path):
    pad = Scratchpad(str(tmp_path / "notes.jsonl"))
    pad.post(KIND_RESULT, "w1", "v1", task_id="t1")
    pad.post(KIND_RESULT, "w2", "v2", task_id="t1")
    results = pad.results()
    assert results["t1"].text == "v2"


def test_concurrent_writers_do_not_corrupt(tmp_path):
    pad = Scratchpad(str(tmp_path / "notes.jsonl"))
    writers = 8
    per_writer = 50

    def worker(wid: int) -> None:
        for i in range(per_writer):
            pad.post("note", f"w{wid}", f"{wid}-{i}")

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    notes = pad.read_all()
    assert len(notes) == writers * per_writer
    # every line must be valid JSON (no interleaved writes)
    assert all(isinstance(n.author, str) and n.author.startswith("w") for n in notes)


def test_compact_keeps_newest(tmp_path):
    pad = Scratchpad(str(tmp_path / "notes.jsonl"), max_entries=5)
    for i in range(20):
        pad.post("note", "a", str(i))
    kept = pad.compact()
    assert kept == 5
    assert [n.text for n in pad.read_all()] == ["15", "16", "17", "18", "19"]


def test_note_roundtrip():
    note = Note(kind="note", author="a", text="x", data={"k": "v"}, ttl=10)
    restored = Note.from_dict(note.to_dict())
    assert restored == note


def test_persists_and_reloads_from_disk(tmp_path):
    path = str(tmp_path / "notes.jsonl")
    pad = Scratchpad(path)
    pad.post("note", "a", "bleibt erhalten")
    pad.close()

    reopened = Scratchpad(path)
    reopened.wait_ready()
    assert [n.text for n in reopened.read_all()] == ["bleibt erhalten"]
    reopened.close()


def test_posts_before_load_are_not_lost(tmp_path):
    path = str(tmp_path / "notes.jsonl")
    first = Scratchpad(path)
    first.post("note", "old", "alt")
    first.close()

    second = Scratchpad(path)
    second.post("note", "new", "neu")  # posted before the load completes
    second.wait_ready()
    texts = [n.text for n in second.read_all()]
    assert "alt" in texts and "neu" in texts
    second.close()


def test_close_is_idempotent(tmp_path):
    pad = Scratchpad(str(tmp_path / "notes.jsonl"))
    pad.close()
    pad.close()


def test_heartbeats_are_not_persisted(tmp_path):
    path = str(tmp_path / "notes.jsonl")
    pad = Scratchpad(path)
    pad.post("note", "a", "echte notiz")
    pad.heartbeat("a")
    pad.close()

    raw = open(path, encoding="utf-8").read()
    assert "heartbeat" not in raw
    assert "echte notiz" in raw

    reopened = Scratchpad(path)
    reopened.wait_ready()
    assert [n.kind for n in reopened.read_all()] == ["note"]
    reopened.close()


def test_heartbeats_stay_in_memory(tmp_path):
    pad = Scratchpad(str(tmp_path / "notes.jsonl"))
    pad.heartbeat("worker-a")
    pad.heartbeat("worker-b")
    beats = pad.heartbeats(window=60)
    assert set(beats) == {"worker-a", "worker-b"}
    pad.close()
