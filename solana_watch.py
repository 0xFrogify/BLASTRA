"""
solana_watch.py — the Solana half of BLASTRA.

Mirror of chain_watch.py. Same job, same event vocabulary, different chain, so
the desks upstream never learn which chain a signal came from. Everything lands
on bus.events as:

  token_launch / token_graduate     lifecycle
  mcap_cross                        the clock that defines "early"
  whale_transfer                    a large fraction of supply in one tx
  cluster_detected                  wallets sharing a funder
  outcome_scored                    closes the loop back into scout_watch

Plain JSON-RPC, no SDK. Watching Solana needs exactly four methods —
getSignaturesForAddress, getTransaction, getAccountInfo, getTokenSupply — all
of which are ordinary HTTP POSTs. The heavy client libraries exist for signing,
which this file does not do. One less dependency to pin, audit and upgrade.

Transfers are read by diffing meta.preTokenBalances against
meta.postTokenBalances rather than decoding instruction data. That is
deliberate: the balance diff is the *effect*, and it stays correct no matter
which instruction variant, CPI depth or program version produced it. Decoders
rot every time a program is upgraded; a balance diff does not.

A note on the public endpoint: api.mainnet-beta.solana.com is free, keyless,
heavily rate limited and will drop you under any real polling load. Fine to
prove the thing runs; swap in Helius / Triton / QuickNode before you depend on
it. Only SOL_RPC changes.

Offline:
    python solana_watch.py --selftest       live check against mainnet
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import requests

import bus

# --------------------------------------------------------------- config

SOL_RPC = os.environ.get("SOL_RPC", "https://api.mainnet-beta.solana.com")

# Program ids are configurable for the same reason PONS_FACTORY is: a hardcoded
# address becomes a silent lie the moment the program is migrated. These are the
# well-known mainnet defaults — VERIFY THEM against an explorer before you rely
# on this in anger. An id you did not check is an assumption, not a constant.
PUMP_PROGRAM = os.environ.get(
    "SOL_LAUNCH_PROGRAM", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)
PUMPSWAP_PROGRAM = os.environ.get(
    "SOL_AMM_PROGRAM", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
)
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# A transfer of at least this fraction of supply, in one transaction, is a whale.
WHALE_FRACTION = float(os.environ.get("SOL_WHALE_FRACTION", "0.015"))

# Market-cap bands, in SOL, that emit mcap_cross. These define "early" and they
# are deliberately not tuned here — see torah.md §3. Yours will differ.
MCAP_BANDS_SOL = [
    float(x) for x in os.environ.get("SOL_MCAP_BANDS", "50,100,250,420,1000,2500").split(",")
]

POLL_S = float(os.environ.get("SOL_POLL_S", "2.0"))
HTTP_TIMEOUT_S = float(os.environ.get("SOL_HTTP_TIMEOUT_S", "15"))


# --------------------------------------------------------------- types


@dataclass(frozen=True)
class Transfer:
    """One account's token balance change inside a single transaction."""

    mint: str
    owner: str
    delta: int          # raw base units, signed
    decimals: int

    @property
    def ui(self) -> float:
        return self.delta / (10**self.decimals)


@dataclass(frozen=True)
class Launch:
    mint: str
    creator: str
    signature: str
    slot: int


@dataclass
class Tracked:
    mint: str
    supply: int = 0
    decimals: int = 6
    bands_hit: set[float] = field(default_factory=set)
    first_seen: float = field(default_factory=time.time)


# --------------------------------------------------------------- rpc


class RpcError(RuntimeError):
    pass


_session = requests.Session()
_rpc_id = 0


def rpc(method: str, params: list[Any] | None = None, *, timeout: float | None = None) -> Any:
    """One JSON-RPC call. Raises RpcError rather than returning a falsy result.

    A failed call must never be indistinguishable from an empty one. 'No
    launches this slot' and 'the endpoint rate limited us' are different facts
    and the desks downstream act on them differently.
    """
    global _rpc_id
    _rpc_id += 1
    body = {"jsonrpc": "2.0", "id": _rpc_id, "method": method, "params": params or []}
    try:
        r = _session.post(
            SOL_RPC, json=body, timeout=timeout or HTTP_TIMEOUT_S,
            headers={"content-type": "application/json"},
        )
    except requests.RequestException as e:
        raise RpcError(f"{method}: {type(e).__name__}: {e}") from e

    if r.status_code == 429:
        raise RpcError(f"{method}: rate limited (429)")
    if r.status_code != 200:
        raise RpcError(f"{method}: HTTP {r.status_code}")

    try:
        payload = r.json()
    except ValueError as e:
        raise RpcError(f"{method}: response was not JSON") from e

    if "error" in payload:
        raise RpcError(f"{method}: {payload['error']}")
    return payload.get("result")


# --------------------------------------------------------- pure parsing
# Everything below is a pure function of its arguments. No network, no clock,
# no globals. That is what makes it testable without an endpoint, and the test
# suite covers this half rather than the transport.


def parse_transfers(tx: dict[str, Any]) -> list[Transfer]:
    """Token balance changes in one transaction, from the pre/post diff.

    Robust to instruction layout, CPI depth and program upgrades, because it
    reads the effect rather than the intent.
    """
    meta = (tx or {}).get("meta") or {}
    pre_raw = meta.get("preTokenBalances") or []
    post_raw = meta.get("postTokenBalances") or []

    def key(b: dict[str, Any]) -> tuple[str, str]:
        return (b.get("mint", ""), b.get("owner", ""))

    def amount(b: dict[str, Any]) -> int:
        try:
            return int((b.get("uiTokenAmount") or {}).get("amount", 0))
        except (TypeError, ValueError):
            return 0

    def dp(b: dict[str, Any]) -> int:
        try:
            return int((b.get("uiTokenAmount") or {}).get("decimals", 0))
        except (TypeError, ValueError):
            return 0

    pre = {key(b): amount(b) for b in pre_raw if b.get("mint")}
    decimals: dict[tuple[str, str], int] = {key(b): dp(b) for b in pre_raw if b.get("mint")}
    post = {key(b): amount(b) for b in post_raw if b.get("mint")}
    decimals.update({key(b): dp(b) for b in post_raw if b.get("mint")})

    out: list[Transfer] = []
    for k in set(pre) | set(post):
        delta = post.get(k, 0) - pre.get(k, 0)
        if delta == 0:
            continue
        mint, owner = k
        if not mint:
            continue
        out.append(Transfer(mint=mint, owner=owner, delta=delta, decimals=decimals.get(k, 0)))
    return out


def program_ids(tx: dict[str, Any]) -> set[str]:
    """Every program invoked, top level and inner. Order is not meaningful."""
    out: set[str] = set()
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    for ins in msg.get("instructions") or []:
        pid = ins.get("programId")
        if pid:
            out.add(pid)
    for group in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        for ins in group.get("instructions") or []:
            pid = ins.get("programId")
            if pid:
                out.add(pid)
    return out


def parse_launch(tx: dict[str, Any], *, launch_program: str = PUMP_PROGRAM) -> Launch | None:
    """A new mint created through the launch program, or None.

    The test is structural, not nominal: a mint that has a post balance and no
    pre balance anywhere, in a transaction that invoked the launch program.
    """
    if launch_program not in program_ids(tx):
        return None

    meta = (tx or {}).get("meta") or {}
    if meta.get("err") is not None:
        return None                     # a failed tx launched nothing

    pre_mints = {b.get("mint") for b in (meta.get("preTokenBalances") or [])}
    post = meta.get("postTokenBalances") or []
    new = [b for b in post if b.get("mint") and b.get("mint") not in pre_mints]
    if not new:
        return None

    signers = fee_payer(tx)
    sig = ((tx or {}).get("transaction") or {}).get("signatures") or []
    return Launch(
        mint=str(new[0].get("mint")),
        creator=signers or "",
        signature=str(sig[0]) if sig else "",
        slot=int((tx or {}).get("slot") or 0),
    )


def fee_payer(tx: dict[str, Any]) -> str:
    """The first signer. On Solana this is who paid, which is who we track."""
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    for k in keys:
        if isinstance(k, dict):
            if k.get("signer"):
                return str(k.get("pubkey", ""))
        elif isinstance(k, str):
            return k
    return ""


def is_whale(t: Transfer, supply: int, *, fraction: float = WHALE_FRACTION) -> bool:
    """A single-transaction move of at least `fraction` of total supply.

    Supply of zero returns False rather than dividing by zero and rather than
    guessing. An unknown supply is not a small supply.
    """
    if supply <= 0:
        return False
    return abs(t.delta) / supply >= fraction


def mcap_sol(virtual_sol_reserves: int, virtual_token_reserves: int,
             total_supply_ui: float = 1_000_000_000.0) -> float | None:
    """Market cap in SOL from a constant-product bonding curve's reserves.

    price = sol_reserves / token_reserves, both in their own base units; the
    lamport/token-decimal scaling cancels into the 1e9 / 1e6 ratio below.
    Returns None rather than 0.0 when the curve is unreadable — see torah.md on
    why an unknown must never arrive downstream wearing a number's clothes.
    """
    if virtual_token_reserves <= 0 or virtual_sol_reserves < 0:
        return None
    price_sol = (virtual_sol_reserves / 1e9) / (virtual_token_reserves / 1e6)
    return price_sol * total_supply_ui


def bands_crossed(mcap: float, already: Iterable[float],
                  bands: Iterable[float] = ()) -> list[float]:
    """Which bands this mcap has newly crossed. Monotonic: never re-emits."""
    seen = set(already)
    return sorted(b for b in (bands or MCAP_BANDS_SOL) if mcap >= b and b not in seen)


def funding_root(first_funders: dict[str, str], holders: Iterable[str]) -> str | None:
    """The single wallet that funded every holder, if there is one.

    Returns None when the holders have more than one funder, or when any
    holder's funder is unknown. An unknown funder cannot be shown to be the
    same as a known one, and treating it as 'probably the same' is how a
    coordination check turns into a coordination guess.
    """
    roots: set[str] = set()
    for h in holders:
        r = first_funders.get(h)
        if not r:
            return None
        roots.add(r)
    if len(roots) == 1:
        return roots.pop()
    return None


# --------------------------------------------------------------- io layer


def signatures_since(address: str, until: str | None, limit: int = 100) -> list[dict[str, Any]]:
    params: list[Any] = [address, {"limit": limit}]
    if until:
        params[1]["until"] = until
    return rpc("getSignaturesForAddress", params) or []


def get_tx(signature: str) -> dict[str, Any] | None:
    return rpc(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )


def token_supply(mint: str) -> tuple[int, int]:
    """(raw supply, decimals). Raises RpcError rather than returning zeros."""
    res = rpc("getTokenSupply", [mint]) or {}
    val = res.get("value") or {}
    return int(val.get("amount", 0)), int(val.get("decimals", 0))


def largest_holders(mint: str, n: int = 20) -> list[str]:
    res = rpc("getTokenLargestAccounts", [mint]) or {}
    return [a.get("address", "") for a in (res.get("value") or [])[:n]]


# --------------------------------------------------------------- loop


def iter_new_transactions(address: str, cursor: str | None) -> Iterator[tuple[dict[str, Any], str]]:
    """Yield (tx, signature) for new signatures, oldest first."""
    sigs = signatures_since(address, cursor)
    for s in reversed(sigs):                    # RPC returns newest first
        sig = s.get("signature")
        if not sig or s.get("err"):
            continue
        tx = get_tx(sig)
        if tx:
            yield tx, sig


def run() -> None:
    bus.emit("chain_watch_up", chain="solana", rpc=SOL_RPC, program=PUMP_PROGRAM)
    tracked: dict[str, Tracked] = {}
    cursor: str | None = None
    backoff = POLL_S

    while True:
        if bus.halted():
            time.sleep(1.0)
            continue
        try:
            newest: str | None = None
            for tx, sig in iter_new_transactions(PUMP_PROGRAM, cursor):
                newest = newest or sig

                launch = parse_launch(tx)
                if launch:
                    tracked[launch.mint] = Tracked(mint=launch.mint)
                    bus.emit("token_launch", chain="solana", mint=launch.mint,
                             creator=launch.creator, signature=launch.signature,
                             slot=launch.slot)

                if PUMPSWAP_PROGRAM in program_ids(tx):
                    for t in parse_transfers(tx):
                        if t.mint in tracked:
                            bus.emit("token_graduate", chain="solana", mint=t.mint)
                            break

                for t in parse_transfers(tx):
                    tk = tracked.get(t.mint)
                    if not tk:
                        continue
                    if not tk.supply:
                        try:
                            tk.supply, tk.decimals = token_supply(t.mint)
                        except RpcError as e:
                            bus.emit("chain_error", chain="solana", where="getTokenSupply",
                                     mint=t.mint, detail=str(e))
                            continue
                    if is_whale(t, tk.supply):
                        bus.emit("whale_transfer", chain="solana", mint=t.mint,
                                 owner=t.owner, delta=t.delta,
                                 fraction=round(abs(t.delta) / tk.supply, 5))

            if newest:
                cursor = newest
            backoff = POLL_S

        except RpcError as e:
            bus.emit("chain_error", chain="solana", detail=str(e))
            backoff = min(backoff * 2, 60.0)      # the endpoint is telling us to slow down

        time.sleep(backoff)


# --------------------------------------------------------------- selftest


def selftest() -> int:
    """Prove this file talks to mainnet correctly, from YOUR machine.

    Written because the environment this was authored in has no network access
    at all, so the transport layer could not be verified there. The parsing is
    covered by tests/; this covers the half tests cannot reach.
    """
    print(f"endpoint: {SOL_RPC}\n")
    ok = True

    def step(label: str, fn: Any) -> Any:
        nonlocal ok
        try:
            t0 = time.time()
            res = fn()
            print(f"  PASS  {label}  ({(time.time()-t0)*1000:.0f}ms)")
            return res
        except Exception as e:
            print(f"  FAIL  {label}  — {type(e).__name__}: {e}")
            ok = False
            return None

    step("getHealth", lambda: rpc("getHealth"))
    slot = step("getSlot", lambda: rpc("getSlot"))
    if slot:
        print(f"        current slot {slot}")

    sigs = step(
        f"getSignaturesForAddress({PUMP_PROGRAM[:8]}…)",
        lambda: signatures_since(PUMP_PROGRAM, None, limit=5),
    )
    if not sigs:
        print("        no signatures returned — check SOL_LAUNCH_PROGRAM is correct")
        ok = False
    else:
        print(f"        {len(sigs)} recent signatures")
        tx = step("getTransaction(most recent)", lambda: get_tx(sigs[0]["signature"]))
        if tx:
            tr = parse_transfers(tx)
            pids = program_ids(tx)
            print(f"        parsed {len(tr)} balance change(s), {len(pids)} program(s)")
            if PUMP_PROGRAM not in pids:
                print("        WARNING: launch program absent from its own transaction —"
                      " SOL_LAUNCH_PROGRAM is probably wrong")
                ok = False

    print("\n" + ("selftest PASSED" if ok else "selftest FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    if "--config" in sys.argv:
        print(json.dumps({
            "SOL_RPC": SOL_RPC, "launch_program": PUMP_PROGRAM,
            "amm_program": PUMPSWAP_PROGRAM, "whale_fraction": WHALE_FRACTION,
            "mcap_bands_sol": MCAP_BANDS_SOL, "poll_s": POLL_S,
        }, indent=2))
        raise SystemExit(0)
    run()
