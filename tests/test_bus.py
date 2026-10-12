"""
Tests for the bus.

The first one here is a regression test for a bug that shipped: bus.py created
its state directory at import time, against a default ROOT of /oracle. Any
unprivileged user importing it — which is everyone following the quickstart —
got PermissionError before a single line of their own code ran.

It passed locally because the author was root. CI, running as a normal user,
caught it. That is the entire argument for having CI.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys

import pytest

import bus


def test_importing_bus_does_not_touch_the_filesystem(tmp_path):
    """Import must stay side-effect free even when ROOT is uncreatable.

    Run in a subprocess so the parent's already-imported bus cannot mask it.
    """
    repo = os.path.dirname(os.path.abspath(bus.__file__))
    env = dict(os.environ, BLOB_ROOT="/proc/nonexistent/cannot-create", PYTHONPATH=repo)
    r = subprocess.run(
        [sys.executable, "-c", "import bus; print('imported')"],
        capture_output=True, text=True, env=env, cwd=repo,
    )
    assert r.returncode == 0, f"import crashed:\n{r.stderr}"
    assert "imported" in r.stdout


def test_emit_creates_state_lazily_and_appends(tmp_path, monkeypatch):
    monkeypatch.setattr(bus, "ROOT", tmp_path)
    monkeypatch.setattr(bus, "STATE", tmp_path / "state")
    monkeypatch.setattr(bus, "EVENTS", tmp_path / "state" / "events.jsonl")

    assert not (tmp_path / "state").exists()
    rec = bus.emit("token_launch", mint="abc", chain="solana")
    assert (tmp_path / "state").is_dir(), "emit must create the state dir"

    assert rec["kind"] == "token_launch"
    assert rec["mint"] == "abc"
    assert isinstance(rec["ts"], float)

    lines = (tmp_path / "state" / "events.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["mint"] == "abc"


def test_ensure_state_error_names_the_env_var(tmp_path, monkeypatch):
    """A failure here must tell the user what to set, not just errno 13."""
    monkeypatch.setattr(bus, "STATE", bus.Path("/proc/nope/state"))
    with pytest.raises(RuntimeError, match="BLOB_ROOT"):
        bus.ensure_state()


def test_tail_reads_only_new_lines(tmp_path, monkeypatch):
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps({"ts": 1.0, "kind": "a"}) + "\n")

    first, pos = bus.tail(p, 0)
    assert [e["kind"] for e in first] == ["a"]

    with p.open("a") as f:
        f.write(json.dumps({"ts": 2.0, "kind": "b"}) + "\n")

    second, pos2 = bus.tail(p, pos)
    assert [e["kind"] for e in second] == ["b"], "tail must not re-read old lines"
    assert pos2 > pos


def test_tail_skips_a_torn_write(tmp_path):
    """A half-written line is picked up next pass, not parsed as truncated.

    Regression: breaking out of `for line in f` and then calling f.tell()
    raises OSError("telling position disabled by next() call"), which took
    down the reader loop in exactly the situation this guard was written for.
    """
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps({"ts": 1.0, "kind": "a"}) + "\n" + '{"ts": 2.0, "ki')
    out, pos = bus.tail(p, 0)
    assert [e["kind"] for e in out] == ["a"]

    # the cursor must stop BEFORE the torn line, so it is re-read whole later
    with p.open("a") as f:
        f.write('nd": "b"}\n')
    out2, _ = bus.tail(p, pos)
    assert [e["kind"] for e in out2] == ["b"], "torn line must be recovered intact"


def test_tail_survives_rotation(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps({"ts": 1.0, "kind": "a"}) + "\n")
    _, pos = bus.tail(p, 0)
    p.write_text("")                       # rotated out from under us
    out, newpos = bus.tail(p, pos)
    assert out == []
    assert newpos == 0, "a shrunken file must reset the cursor, not seek past EOF"


def test_tail_on_missing_file_is_not_an_error(tmp_path):
    out, pos = bus.tail(tmp_path / "nope.jsonl", 0)
    assert out == [] and pos == 0


def test_halted_reads_the_control_flag(tmp_path, monkeypatch):
    ctl = tmp_path / "control.json"
    monkeypatch.setattr(bus, "CONTROL", ctl)

    assert bus.halted() is False, "absent control.json must mean running"

    for mode, expected in [("run", False), ("halt", True), ("kill", True)]:
        ctl.write_text(json.dumps({"mode": mode}))
        assert bus.halted() is expected, f"mode={mode}"


def test_corrupt_control_file_does_not_halt_everything(tmp_path, monkeypatch):
    """Unparseable control.json falls back to running, not to a silent stop."""
    ctl = tmp_path / "control.json"
    ctl.write_text("{not json")
    monkeypatch.setattr(bus, "CONTROL", ctl)
    assert bus.control()["mode"] == "run"
    assert bus.halted() is False


def test_module_reimports_cleanly():
    importlib.reload(bus)
