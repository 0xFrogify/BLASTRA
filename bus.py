"""
bus.py — the only thing the four processes share.

scout_watch (people)  ─┐
chain_watch (tokens)  ─┼─►  events.jsonl  ──►  agents  ──►  orders.jsonl
whale_watch (wallets) ─┘                                          │
                                                            tg_bridge
                                                                  │
                          control.json  ◄─────────────────────────┘

Nothing imports anything else. No process calls another's functions. One
append-only file in, one append-only file out, one control flag. That is
the entire integration surface, and it is why a dead Telegram API or a
throttled RPC cannot take the system down with it.
"""

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

ROOT = Path(os.environ.get("BLOB_ROOT", "/oracle"))
STATE = ROOT / "state"
EVENTS = STATE / "events.jsonl"
ORDERS = STATE / "orders.jsonl"
CONTROL = ROOT / "control.json"


def ensure_state():
    """Create the state directory. Called on first write, never on import.

    Importing a module must not touch the filesystem. The default ROOT is an
    absolute path that an unprivileged user cannot create, so doing this at
    import time made `import bus` — and therefore every module that imports
    it, and therefore the test suite — crash for anyone who had not already
    set BLOB_ROOT and did not happen to be root.
    """
    try:
        STATE.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"cannot create state directory {STATE}: {e}. "
            f"Set BLOB_ROOT to a path you can write to, e.g. "
            f'export BLOB_ROOT="$PWD/run"'
        ) from e


def emit(kind, **payload):
    """Append one event. Locked, so four writers never interleave a line."""
    ensure_state()
    rec = {"ts": time.time(), "kind": kind, **payload}
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    with EVENTS.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        fcntl.flock(f, fcntl.LOCK_UN)
    return rec


def tail(path, pos):
    """Read new lines since pos. Survives rotation. Skips torn writes.

    readline() rather than `for line in f`: iterating a text file disables
    tell() until the iterator is exhausted, so breaking out early on a partial
    line — the exact case this function exists to handle — made the following
    tell() raise OSError and take the reader's loop down with it.

    pos advances only past COMPLETE lines, so a torn write is re-read whole on
    the next pass instead of being skipped.
    """
    p = Path(path)
    if not p.exists():
        return [], pos
    if p.stat().st_size < pos:
        pos = 0
    out = []
    with p.open() as f:
        f.seek(pos)
        while True:
            line = f.readline()
            if not line:
                break                       # EOF
            if not line.endswith("\n"):
                break                       # partial write, pick it up next pass
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
            pos = f.tell()
    return out, pos


def control():
    if CONTROL.exists():
        try:
            return json.loads(CONTROL.read_text())
        except json.JSONDecodeError:
            pass
    return {"mode": "run", "manual": True}


def halted():
    """Every loop checks this. One tap in Telegram stops all four."""
    return control().get("mode") in ("kill", "halt")
