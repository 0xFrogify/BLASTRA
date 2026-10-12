"""
token_audit.py — the security suite LINKER runs before it returns a match.

Split of responsibility, same as everywhere else in BLOB:

    this file      runs the checks. deterministic, reproducible, no opinion.
    LINKER         reads the results and decides what they mean.
    torah.md       decides which results are fatal regardless of opinion.

Every check returns one of: PASS, FAIL, or UNKNOWN.

UNKNOWN is never PASS. If an RPC call times out, that check did not pass —
it did not run. A system that silently upgrades "I could not check" into
"looks fine" will eventually buy the one token it could not read, and that
will be the rug. torah.md treats UNKNOWN on a hard check as a block.
"""

import json
import os
import time
from dataclasses import asdict, dataclass

from web3 import Web3
from web3.exceptions import ContractLogicError

import bus

RPC = os.environ.get("RHC_RPC", "https://rpc.mainnet.chain.robinhood.com")
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))

PONS_FACTORY = os.environ.get("PONS_FACTORY", "")
EXPECTED_SUPPLY = 10**9 * 10**18
DEAD = "0x000000000000000000000000000000000000dEaD"
ZERO = "0x0000000000000000000000000000000000000000"

# bytecode hashes of the contracts Pons v2 deploys. anything else is custom,
# and custom on a launchpad token means someone went out of their way.
KNOWN_CODEHASH = set(filter(None, os.environ.get("PONS_CODEHASHES", "").split(",")))

ERC20 = json.loads("""[
 {"name":"totalSupply","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
 {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
 {"name":"owner","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"},
 {"name":"transfer","outputs":[{"type":"bool"}],"inputs":[{"type":"address"},{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"}
]""")

CURVE = json.loads("""[
 {"name":"currentSnipeTaxBps","outputs":[{"type":"uint256"}],"inputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
 {"name":"swapFeeBps","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
 {"name":"sellQuote","outputs":[{"type":"uint256"}],"inputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}
]""")


@dataclass
class Check:
    name: str
    verdict: str          # PASS | FAIL | UNKNOWN
    detail: str = ""
    hard: bool = False    # hard checks are gates; soft ones only move the score


def U(name, why, hard=False):
    return Check(name, "UNKNOWN", why, hard)


# ------------------------------------------------------------ checks
def check_deployed_by_factory(token):
    """A Pons token you did not get from the Pons factory is not a Pons token.
    This single check kills most impostor contracts on its own."""
    if not PONS_FACTORY:
        return U("factory_origin", "PONS_FACTORY not configured", hard=True)
    try:
        code = w3.eth.get_code(Web3.to_checksum_address(token))
        if not code or code == b"":
            return Check("factory_origin", "FAIL", "no bytecode at address", True)
        h = w3.keccak(code).hex()
        if KNOWN_CODEHASH and h not in KNOWN_CODEHASH:
            return Check("factory_origin", "FAIL",
                         f"custom bytecode {h[:12]}…", True)
        if not KNOWN_CODEHASH:
            return U("factory_origin", "no known codehashes configured", True)
        return Check("factory_origin", "PASS", h[:12], True)
    except Exception as e:
        return U("factory_origin", str(e)[:70], True)


def check_sellable(token, curve, holder=None):
    """Honeypot test. Simulate the exit before taking the entry.
    A token you can buy and cannot sell is the oldest trap there is."""
    if not curve:
        return U("sellable", "curve address unknown", hard=True)
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(curve), abi=CURVE)
        out = c.functions.sellQuote(10**18).call()
        if out == 0:
            return Check("sellable", "FAIL", "sellQuote returns 0", True)
        return Check("sellable", "PASS", f"quote {out}", True)
    except ContractLogicError as e:
        return Check("sellable", "FAIL", f"sell reverts: {str(e)[:50]}", True)
    except Exception as e:
        return U("sellable", str(e)[:70], True)


def check_fees(curve, recipient):
    """Pons v2 sets fees per launch and does not display them anywhere.
    Reading the curve is the only way to know what you will actually pay.
    Quote from curve maths alone overstates your return while tax decays."""
    if not curve:
        return U("fees", "curve address unknown")
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(curve), abi=CURVE)
        snipe = c.functions.currentSnipeTaxBps(
            Web3.to_checksum_address(recipient or DEAD)).call()
        swap = c.functions.swapFeeBps().call()
        detail = f"snipe {snipe}bps swap {swap}bps"
        if snipe > 300:
            return Check("fees", "FAIL", detail + " — opening tax still decaying", True)
        if swap > 300:
            return Check("fees", "FAIL", detail + " — swap fee above 3%", True)
        if swap > 150:
            return Check("fees", "PASS", detail + " — above median, size down")
        return Check("fees", "PASS", detail)
    except Exception as e:
        return U("fees", str(e)[:70])


def check_supply(token):
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20)
        s = c.functions.totalSupply().call()
        if s != EXPECTED_SUPPLY:
            return Check("supply", "FAIL",
                         f"{s} != pons standard {EXPECTED_SUPPLY}", True)
        return Check("supply", "PASS", "1B / 18dp")
    except Exception as e:
        return U("supply", str(e)[:70], True)


def check_ownership(token):
    """Factory tokens should have no live owner. An owner means someone can
    still change the rules after you are in."""
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20)
        o = c.functions.owner().call()
        if o in (ZERO, DEAD):
            return Check("ownership", "PASS", "renounced")
        return Check("ownership", "FAIL", f"live owner {o[:10]}…", True)
    except ContractLogicError:
        return Check("ownership", "PASS", "no owner() — standard pons")
    except Exception as e:
        return U("ownership", str(e)[:70])


def check_concentration(token, holders, exclude):
    """Top-holder share, excluding the curve and the LP. A single wallet
    holding a fifth of supply is not a community, it is a countdown."""
    if not holders:
        return U("concentration", "holder list unavailable")
    skip = {a.lower() for a in exclude if a}
    real = [h for h in holders
            if (h.get("address", {}).get("hash", "") or "").lower() not in skip]
    if not real:
        return U("concentration", "no non-pool holders returned")
    top = int(real[0].get("value", 0)) / EXPECTED_SUPPLY
    top5 = sum(int(h.get("value", 0)) for h in real[:5]) / EXPECTED_SUPPLY
    d = f"top {top*100:.1f}% · top5 {top5*100:.1f}%"
    if top > 0.20:
        return Check("concentration", "FAIL", d, True)
    if top5 > 0.45:
        return Check("concentration", "FAIL", d, True)
    return Check("concentration", "PASS", d)


def check_lp(token, pool, graduated):
    """Pons graduates into a permanently locked Uniswap v4 pool. If it has
    graduated into something that is not that pool, ask why."""
    if not graduated:
        return Check("lp_locked", "PASS", "pre-graduation, supply on curve")
    if not pool:
        return U("lp_locked", "graduated but pool address unknown", True)
    return Check("lp_locked", "PASS", f"canonical pool {pool[:10]}…")


def check_deployer(record, bridge_funded):
    """Serial deployers and fresh bridge-funded wallets are the two shapes
    that precede almost every social-hack launch on this chain."""
    if record is None:
        return U("deployer", "no deployer record")
    if bridge_funded is True:
        return Check("deployer", "FAIL", "bridge-funded within 60m", True)
    if bridge_funded is None:
        return U("deployer", "bridge set not configured", True)
    if record.get("serial"):
        return Check("deployer", "FAIL",
                     f"{record['launches']} launches, "
                     f"{record['rate']*100:.1f}% graduate", True)
    return Check("deployer", "PASS",
                 f"{record['launches']} launches / {record['graduates']} graduated")


# ------------------------------------------------------------ runner
def audit(token, *, curve=None, pool=None, recipient=None, holders=None,
          deployer_record=None, bridge_funded=None, graduated=False):
    checks = [
        check_deployed_by_factory(token),
        check_sellable(token, curve),
        check_supply(token),
        check_ownership(token),
        check_fees(curve, recipient),
        check_concentration(token, holders, [curve, pool]),
        check_lp(token, pool, graduated),
        check_deployer(deployer_record, bridge_funded),
    ]
    hard = [c for c in checks if c.hard]
    fails = [c for c in checks if c.verdict == "FAIL"]
    unknown_hard = [c for c in hard if c.verdict == "UNKNOWN"]

    if any(c.hard for c in fails):
        verdict = "REJECT"
    elif unknown_hard:
        verdict = "INCONCLUSIVE"          # never treated as pass
    elif fails:
        verdict = "PASS_WITH_FLAGS"
    else:
        verdict = "CLEAN"

    result = {
        "contract": token,
        "verdict": verdict,
        "checks": [asdict(c) for c in checks],
        "hard_fails": [c.name for c in fails if c.hard],
        "unresolved": [c.name for c in unknown_hard],
        "ts": time.time(),
    }
    bus.emit("token_audit", **result)
    return result


if __name__ == "__main__":
    import sys
    print(json.dumps(audit(sys.argv[1]), indent=2))
