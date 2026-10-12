# agents/

Four roles. No framework, no orchestration library, no graph.

Each agent is a loop that tails `state/events.jsonl`, thinks, and appends to
`state/orders.jsonl`. They never call each other. They never share memory. If
one dies, the other three keep running and the dead one picks up from its own
cursor when it comes back.

```
events.jsonl ──► SCOUT ──► WEIGHER ──► LINKER ──► CHIEF ──► orders.jsonl
```

The arrow is not a function call. Every agent reads from the same append-only
log and writes back to it. The order above is the order the evidence tends to
accumulate in, not a pipeline anyone enforces.

## Why these files are prose and not code

The role files in this directory are **system prompts**, not implementations.
They are deliberately model-agnostic: nothing here names a provider, a model or
an SDK, because the one thing guaranteed about whichever model is best today is
that it will not be the best one in six months.

To wire a model in, you need about forty lines: tail the bus, build a context
window from the last N events plus `torah.md` plus the role file, call your
model, parse the response, emit. That glue is not in this repository yet — see
the status note in the root README.

## The division of labour that matters

The Python modules are deterministic and hold no opinions. The agents hold all
the opinions and touch no data directly. This split is the whole architecture:

| | sees | decides | can be wrong about |
|---|---|---|---|
| `scout_watch.py` | X accounts | nothing | nothing — it diffs lists |
| `chain_watch.py` | the chain | nothing | nothing — it reads logs |
| `token_audit.py` | contracts | nothing | nothing — checks return PASS/FAIL/UNKNOWN |
| **SCOUT** | events | what is worth opening | which narratives matter |
| **WEIGHER** | narratives | what attention is real | how much a mention is worth |
| **LINKER** | narratives + contracts | which token is *the* token | identity |
| **CHIEF** | everything | enter / size / skip | the trade |

An agent asked to do a deterministic job will invent one. A deterministic
module asked to hold an opinion will encode yesterday's opinion forever. Keep
the line where it is.

## Files

- [`scout.md`](scout.md) — opens and closes narratives
- [`weigher.md`](weigher.md) — scores attention, collapses clusters
- [`linker.md`](linker.md) — binds a narrative to a contract, or refuses to
- [`chief.md`](chief.md) — the only desk that can spend money
