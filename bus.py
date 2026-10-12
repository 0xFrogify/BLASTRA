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

import json, time, os, fcntl
from pathlib import Path

ROOT = Path(os.environ.get("BLOB_ROOT", "/oracle"))
STATE = ROOT / "state"
EVENTS = STATE / "events.jsonl"
ORDERS = STATE / "orders.jsonl"
CONTROL = ROOT / "control.json"

STATE.mkdir(parents=True, exist_ok=True)


def emit(kind, **payload):
    """Append one event. Locked, so four writers never interleave a line."""
    rec = {"ts": time.time(), "kind": kind, **payload}
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    with EVENTS.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        fcntl.flock(f, fcntl.LOCK_UN)
    return rec


def tail(path, pos):
    """Read new lines since pos. Survives rotation. Skips torn writes."""
    p = Path(path)
    if not p.exists():
        return [], pos
    if p.stat().st_size < pos:
        pos = 0
    out = []
    with p.open() as f:
        f.seek(pos)
        for line in f:
            if not line.endswith("\n"):
                break                       # partial write, pick it up next pass
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
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
