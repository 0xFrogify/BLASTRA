# torah.md — the origin file

This file is the brain. Everything else in this repository is plumbing.

The Python modules are deterministic: they fetch, diff, check and report. They
hold no opinions and they never decide anything. Every opinion the system has
lives in this one file, in plain language, where you can read it, argue with it
and change it without touching a line of code.

Two different readers consume this file:

| Reader | Reads | How |
|---|---|---|
| `tg_bridge.py` | the `execution` block below | parsed as YAML |
| the four agents | everything else | passed in as system context |

That means the YAML block is **machine-read and must stay valid**. The prose is
model-read, so write it the way you would brief a competent colleague who will
take you literally and has no common sense to fall back on.

**This file as shipped is a starting point, not a recommendation.** The numbers
are the ones a working configuration used; they are not correct in any universal
sense, and they will not fit your capital, your chain or your risk tolerance.
Change them. That is the entire point of keeping them here instead of in code.

---

## execution

```yaml
execution:
  # auto     — fire without asking, report after
  # manual   — ask before every order, no exceptions
  mode: auto

  # orders at or below this notional never ask
  auto_below_usd: 400

  # orders above this notional always ask, even in auto
  ask_above_usd: 400

  # how long an approval card stays live before on_timeout applies
  approval_timeout_s: 90

  # skip | fill — what an unanswered approval card does when it expires.
  # 'skip' is the only safe default: silence must never become a fill.
  on_timeout: skip
```

Everything below this line is read by the agents, not by the code.

---

## 1. Hard gates

A hard gate is not a score. It does not get weighed against anything, it does
not get overridden by conviction, and no amount of narrative strength buys a
pass. If a hard gate fails, the answer is no and the loop moves on.

`token_audit.py` runs eight checks. These are the ones that are fatal:

| Check | Fails when | Why it is fatal |
|---|---|---|
| `factory_origin` | no bytecode, or codehash not in the known factory set | an unknown contract can do anything, including nothing you tested for |
| `sellable` | `sellQuote` returns 0, or the sell call reverts | you can buy and cannot sell — this is the definition of a honeypot |
| `fees` | opening tax still decaying, or swap fee above 3% | arriving early costs up to 99% to the opening tax; the fee schedule, not your timing, decides the trade |
| `supply` | not 1B / 18dp | a non-standard supply means a non-standard contract |
| `ownership` | a live `owner()` that has not renounced | an owner can change the rules after you are in |
| `concentration` | holders exceed the threshold once curve and pool are excluded | the exit is owned by someone else |
| `deployer` | bridge-funded within 60 minutes, or a prior rug on record | the single highest-precision signal available before entry |

`lp_locked` is not hard pre-graduation, because supply sits on the curve and
there is nothing to lock yet.

### The UNKNOWN rule

Every check returns `PASS`, `FAIL` or `UNKNOWN`.

**`UNKNOWN` is never `PASS`.** If an RPC call timed out, the check did not pass —
it did not run. A system that quietly promotes "I could not check" into "looks
fine" will eventually buy the one token it could not read, and that is the one
that rugs.

`UNKNOWN` on any hard check produces `INCONCLUSIVE`, and `INCONCLUSIVE` blocks
exactly like `REJECT`. The difference is only in what you do about it: a reject
is finished, an inconclusive is worth retrying once the source recovers.

Verdicts, in full:

```
any hard check FAILED          -> REJECT            block
any hard check UNKNOWN         -> INCONCLUSIVE      block, retriable
only soft checks FAILED        -> PASS_WITH_FLAGS   proceed at reduced size
nothing failed                 -> CLEAN             proceed
```

---

## 2. What counts as attention

The system exists to answer one question: **is anyone who matters paying
attention to this, and did they start recently?**

Attention is scored, not counted. Volume of mentions is close to worthless —
it is the cheapest thing on the internet to manufacture. Three properties
decide whether a mention is worth anything:

- **authority** — would this account's attention move anyone else's?
- **independence** — did this account arrive at it separately, or is it one
  node in a cluster that all moved within the same hour from the same funder?
- **object** — is there a *specific thing* being endorsed, or is the account
  merely posting near the topic?

An endorsement with no object is not an endorsement. If the account did not
name the thing, it did not endorse the thing.

A cluster of fifty accounts that share a funding root is **one** account. Collapse
it before scoring, never after. The collapse is the whole job; counting is not.

### Gates

- `independence` below threshold → the cluster collapses to a single node and
  is rescored as one.
- `endorsed_object = NONE` → reject regardless of authority.
- `coordination` above threshold with a single funder → reject.

---

## 3. The clock

"Early" is not a feeling, it is a market-cap band, and it is recorded by
`mcap_cross` events rather than inferred afterwards.

Arriving earlier is not free. On a chain with no gas auction — first-come,
first-served, `eth_maxPriorityFeePerGas` returning zero — you cannot outbid.
You can only arrive earlier, and arriving earlier costs you the opening tax.
There is a band where the tax has decayed enough and the attention has not yet
arrived. That band is the entire edge. Outside it, do nothing.

Define your own band. Do not inherit one.

---

## 4. Sizing and the ladder

- `CLEAN` → full size.
- `PASS_WITH_FLAGS` → half size. Not "full size with a note."
- Anything blocked → zero. There is no "small position to keep an eye on it."

The exit ladder is armed at entry and it is not renegotiated while the position
is open:

```
[33 / 33 / 34]
```

A ladder you are allowed to change mid-position is not a ladder, it is a hope.
Switching to manual mid-trade is permitted and sometimes correct, but it is
logged as a deviation and it is scored against the ladder afterwards, so you
find out whether your intervention actually helped or whether you got lucky.

---

## 5. What makes a lesson

A lesson is written after a position closes, by the autopsy desk. Most closes
produce no lesson, and that is the correct outcome — if every trade generated
a lesson, the brain would be a diary rather than a model.

A lesson qualifies only when all four hold:

1. It is **falsifiable.** "Be more careful" is not a lesson. "Reject when the
   funding root resolves to one wallet across more than six holders" is.
2. It would have **changed a decision.** If applying it to the closed position
   changes nothing, it is an observation, not a lesson.
3. It is **not already in the file.** Near-duplicates are merged into the
   existing lesson with a second piece of evidence, never appended as new.
   A brain that holds the same lesson forty times has not learned it forty
   times; it has one lesson and a counting bug.
4. It survives a **counterfactual.** If the trade would have gone the same way
   with the lesson applied, log the counterfactual and write no lesson.

Rule 3 is the one that breaks first and it is the one that matters most. When
you open the map and three spheres in a cluster say the same thing in different
words, the merge step is not running.

---

## 6. Scope

This file describes a system for one chain, one asset class and one kind of
attention. It does not generalise, and nothing in it has been tested outside
that setting.

The exit rule, the size bands and the clock are all derived from a specific
book of closed positions. Yours will differ. Treat every number here as a
worked example with the working shown, not as a result.

---

## Making it yours

1. Rewrite section 2 first. What counts as attention in your niche is almost
   certainly not what counts in this one.
2. Set your own band in section 3. Inheriting someone else's clock is the
   fastest way to be consistently late.
3. Leave section 1 alone until you have closed positions of your own. The hard
   gates are the only part of this file that is not a matter of taste — they
   are there because each of them has already cost somebody money.
