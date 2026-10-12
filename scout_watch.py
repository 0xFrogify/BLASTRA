"""
scout_watch.py — the sensor layer for BLOB.

This is not an agent. It is a plain Python loop.
Agents cost money and make things up; a differ does not.
SCOUT (the agent) only ever sees the events this file emits.

What it does:
  - keeps a live watchlist of 200-300 X accounts
  - polls them on three tiers so the rate limit is never the bottleneck
  - diffs each account's following list against the last snapshot
  - tracks reply-velocity against a per-account EWMA baseline
  - promotes / demotes / evicts accounts based on realised hit rate
  - discovers new accounts from the early buyers of tokens that graduated

Emits JSON events to /oracle/state/events.jsonl for the agents to read.
"""

import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import bus

ROOT = Path("/oracle")
DB = ROOT / "scout.db"
EVENTS = ROOT / "state" / "events.jsonl"

# ---- tiers -----------------------------------------------------------------
# Poll budget is finite. Not every account deserves the same attention.
TIERS = {
    "hot":  {"interval": 60,   "cap": 40},    # proven early. 1 min.
    "warm": {"interval": 300,  "cap": 120},   # sometimes early. 5 min.
    "cold": {"interval": 1800, "cap": 160},   # unproven / decaying. 30 min.
}
MAX_WATCHLIST = 320
EWMA_ALPHA = 0.25          # how fast the velocity baseline adapts
PROMOTE_AT = 2             # hits before an account moves up a tier
DEMOTE_AFTER_DAYS = 14     # silence that long and you drop


@dataclass
class Account:
    handle: str
    tier: str = "cold"
    hits: int = 0            # times it was early on a token that graduated
    misses: int = 0
    baseline: float = 0.0    # EWMA replies/hour
    last_poll: float = 0.0
    last_hit: float = 0.0
    following: set = None


# ---- storage ---------------------------------------------------------------
def db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS accounts(
      handle TEXT PRIMARY KEY, tier TEXT, hits INT DEFAULT 0,
      misses INT DEFAULT 0, baseline REAL DEFAULT 0,
      last_poll REAL DEFAULT 0, last_hit REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS following(
      handle TEXT, target TEXT, PRIMARY KEY(handle, target));
    CREATE TABLE IF NOT EXISTS seen_tokens(
      contract TEXT PRIMARY KEY, graduated INT DEFAULT 0, ts REAL);
    """)
    return c


emit = bus.emit          # one writer contract for all four processes


# ---- the actual fetch ------------------------------------------------------
# Swap this for whatever you use. Logged-in browser session, API, scraper.
# Everything above and below this function is transport-agnostic on purpose.
def fetch_profile(handle):
    """Return {'following': set[str], 'replies_last_hour': int} or None."""
    raise NotImplementedError("wire your session here")


# ---- polling ---------------------------------------------------------------
def due(acc, now):
    return now - acc.last_poll >= TIERS[acc.tier]["interval"]


def poll_once(conn, acc: Account):
    now = time.time()
    data = fetch_profile(acc.handle)
    if data is None:
        # A failed fetch is not a zero. Never invent an observation.
        emit("source_fail", handle=acc.handle)
        return

    # --- follow delta -------------------------------------------------------
    prev = {r[0] for r in conn.execute(
        "SELECT target FROM following WHERE handle=?", (acc.handle,))}
    new = data["following"] - prev

    if prev and new:                      # ignore the very first snapshot
        for target in new:
            emit("follow", source=acc.handle, target=target, tier=acc.tier)

    conn.executemany(
        "INSERT OR IGNORE INTO following VALUES(?,?)",
        [(acc.handle, t) for t in new])

    # --- reply velocity vs its own baseline ---------------------------------
    r = data["replies_last_hour"]
    if acc.baseline > 0 and r > acc.baseline * 3:
        emit("velocity_spike", handle=acc.handle,
             ratio=round(r / acc.baseline, 2), tier=acc.tier)
    acc.baseline = (EWMA_ALPHA * r) + (1 - EWMA_ALPHA) * (acc.baseline or r)

    acc.last_poll = now
    conn.execute("UPDATE accounts SET baseline=?, last_poll=? WHERE handle=?",
                 (acc.baseline, now, acc.handle))
    conn.commit()


# ---- the watchlist learns --------------------------------------------------
def record_outcome(conn, contract, graduated, early_handles):
    """Called by JEFFREY after a token resolves. This is what makes the
    watchlist better instead of just bigger."""
    conn.execute("INSERT OR REPLACE INTO seen_tokens VALUES(?,?,?)",
                 (contract, int(graduated), time.time()))
    for h in early_handles:
        col = "hits" if graduated else "misses"
        conn.execute(f"UPDATE accounts SET {col}={col}+1, last_hit=? "
                     f"WHERE handle=?", (time.time(), h))
        if graduated:
            discover(conn, h)
    conn.commit()
    rebalance(conn)


def discover(conn, handle):
    """An account that was early on a graduated token is worth watching,
    whether or not we were already watching it."""
    row = conn.execute("SELECT handle FROM accounts WHERE handle=?",
                       (handle,)).fetchone()
    if row:
        return
    if count(conn) >= MAX_WATCHLIST:
        evict_worst(conn)
    conn.execute("INSERT INTO accounts(handle, tier) VALUES(?, 'warm')",
                 (handle,))
    emit("watchlist_add", handle=handle, reason="early_on_graduate")


def rebalance(conn):
    """Promote what works, demote what went quiet, evict what never worked."""
    now = time.time()
    for h, tier, hits, misses, last_hit in conn.execute(
            "SELECT handle,tier,hits,misses,last_hit FROM accounts"):

        stale_days = (now - last_hit) / 86400 if last_hit else 999
        target = tier

        if hits >= PROMOTE_AT and tier != "hot":
            target = "hot"
        elif hits >= 1 and tier == "cold":
            target = "warm"
        elif stale_days > DEMOTE_AFTER_DAYS:
            target = {"hot": "warm", "warm": "cold", "cold": "cold"}[tier]

        if target != tier and not full(conn, target):
            conn.execute("UPDATE accounts SET tier=? WHERE handle=?",
                         (target, h))
            emit("watchlist_tier", handle=h, frm=tier, to=target)

        # never produced a hit, plenty of misses, long dead -> drop it
        if hits == 0 and misses >= 8 and stale_days > DEMOTE_AFTER_DAYS:
            conn.execute("DELETE FROM accounts WHERE handle=?", (h,))
            conn.execute("DELETE FROM following WHERE handle=?", (h,))
            emit("watchlist_drop", handle=h, reason="no_hits")
    conn.commit()


def count(conn, tier=None):
    q = "SELECT COUNT(*) FROM accounts" + (" WHERE tier=?" if tier else "")
    return conn.execute(q, (tier,) if tier else ()).fetchone()[0]


def full(conn, tier):
    return count(conn, tier) >= TIERS[tier]["cap"]


def evict_worst(conn):
    row = conn.execute(
        "SELECT handle FROM accounts ORDER BY hits ASC, misses DESC, "
        "last_hit ASC LIMIT 1").fetchone()
    if row:
        conn.execute("DELETE FROM accounts WHERE handle=?", (row[0],))
        emit("watchlist_drop", handle=row[0], reason="capacity")


# ---- loop ------------------------------------------------------------------
def load(conn):
    return [Account(*r) for r in conn.execute(
        "SELECT handle,tier,hits,misses,baseline,last_poll,last_hit "
        "FROM accounts")]


def run():
    conn = db()
    while True:
        now = time.time()
        queue = [a for a in load(conn) if due(a, now)]
        # hot first, then oldest-polled. Jitter so we never look like a cron.
        queue.sort(key=lambda a: (["hot", "warm", "cold"].index(a.tier),
                                  a.last_poll))
        for acc in queue:
            try:
                poll_once(conn, acc)
            except Exception as e:
                emit("error", handle=acc.handle, err=str(e))
            time.sleep(0.4 + random.random() * 0.9)
        time.sleep(2)


if __name__ == "__main__":
    run()
