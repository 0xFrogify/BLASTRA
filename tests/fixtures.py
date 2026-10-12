"""
Recorded-shape fixtures for Solana JSON-RPC responses.

These match the structure getTransaction returns under
`encoding: "jsonParsed", maxSupportedTransactionVersion: 0` — accountKeys as
objects with signer/writable flags, pre/postTokenBalances with uiTokenAmount,
innerInstructions grouped by index.

They are synthetic: addresses are placeholders and amounts are chosen to make
each case obvious. The *shapes* are what matter, because the shapes are what
the parser contracts with.
"""

from __future__ import annotations

from typing import Any

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
TOKEN_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM = "11111111111111111111111111111111"

MINT_A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
MINT_B = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
CREATOR = "CrEaToR11111111111111111111111111111111111"
BUYER = "BuYeR111111111111111111111111111111111111"
WHALE = "WhAlE111111111111111111111111111111111111"


def _bal(mint: str, owner: str, amount: int, decimals: int = 6,
         index: int = 1) -> dict[str, Any]:
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": owner,
        "programId": TOKEN_PROG,
        "uiTokenAmount": {
            "amount": str(amount),
            "decimals": decimals,
            "uiAmount": amount / (10**decimals),
            "uiAmountString": str(amount / (10**decimals)),
        },
    }


def _tx(*, programs: list[str], pre: list[dict[str, Any]], post: list[dict[str, Any]],
        err: Any = None, signature: str = "sig111", slot: int = 1000,
        signer: str = CREATOR, inner: list[str] | None = None) -> dict[str, Any]:
    return {
        "slot": slot,
        "transaction": {
            "signatures": [signature],
            "message": {
                "accountKeys": [
                    {"pubkey": signer, "signer": True, "writable": True},
                    {"pubkey": BUYER, "signer": False, "writable": True},
                ],
                "instructions": [{"programId": p} for p in programs],
            },
        },
        "meta": {
            "err": err,
            "preTokenBalances": pre,
            "postTokenBalances": post,
            "innerInstructions": (
                [{"index": 0, "instructions": [{"programId": p} for p in inner]}]
                if inner else []
            ),
        },
    }


# ---------------------------------------------------------------- cases

#: A genuine launch: mint appears in post, absent from pre, pump program invoked.
LAUNCH = _tx(
    programs=[PUMP],
    pre=[],
    post=[_bal(MINT_A, CREATOR, 1_000_000_000_000_000)],
    inner=[TOKEN_PROG],
    signature="launchSig",
    slot=1234,
)

#: Same shape but the transaction FAILED. Launched nothing.
LAUNCH_FAILED = _tx(
    programs=[PUMP],
    pre=[],
    post=[_bal(MINT_A, CREATOR, 1_000_000_000_000_000)],
    err={"InstructionError": [0, "Custom"]},
    signature="failedSig",
)

#: Pump program invoked, but the mint already existed — a buy, not a launch.
BUY_NOT_LAUNCH = _tx(
    programs=[PUMP],
    pre=[_bal(MINT_A, CREATOR, 1_000_000_000_000_000),
         _bal(MINT_A, BUYER, 0, index=2)],
    post=[_bal(MINT_A, CREATOR, 900_000_000_000_000),
          _bal(MINT_A, BUYER, 100_000_000_000_000, index=2)],
    signature="buySig",
)

#: A new mint, but the launch program was never invoked. Not ours.
FOREIGN_LAUNCH = _tx(
    programs=[SYSTEM, TOKEN_PROG],
    pre=[],
    post=[_bal(MINT_B, CREATOR, 1_000_000_000_000_000)],
    signature="foreignSig",
)

#: Graduation: the AMM program shows up for a mint we track.
GRADUATE = _tx(
    programs=[AMM],
    pre=[_bal(MINT_A, CREATOR, 200_000_000_000_000)],
    post=[_bal(MINT_A, CREATOR, 100_000_000_000_000)],
    signature="gradSig",
)

#: 2% of a 1e15 supply moving in one transaction.
WHALE_MOVE = _tx(
    programs=[PUMP],
    pre=[_bal(MINT_A, WHALE, 0), _bal(MINT_A, CREATOR, 1_000_000_000_000_000, index=2)],
    post=[_bal(MINT_A, WHALE, 20_000_000_000_000),
          _bal(MINT_A, CREATOR, 980_000_000_000_000, index=2)],
    signature="whaleSig",
)

#: Balances present but identical — a no-op for the transfer parser.
NO_CHANGE = _tx(
    programs=[PUMP],
    pre=[_bal(MINT_A, CREATOR, 500)],
    post=[_bal(MINT_A, CREATOR, 500)],
    signature="noopSig",
)

#: Malformed in every way a real endpoint has actually been seen to misbehave:
#: null meta, missing keys, string accountKeys, absent balance arrays.
MALFORMED: list[dict[str, Any]] = [
    {},
    {"meta": None, "transaction": None},
    {"meta": {}, "transaction": {"message": {}}},
    {"meta": {"preTokenBalances": None, "postTokenBalances": None}},
    {"transaction": {"message": {"accountKeys": ["AsAPlainString1111111111111111111"]}}},
    {"meta": {"postTokenBalances": [{"mint": None, "uiTokenAmount": {}}]}},
    {"meta": {"postTokenBalances": [{"mint": MINT_A,
                                     "uiTokenAmount": {"amount": "not-a-number"}}]}},
]
