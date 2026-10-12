"""
Tests for the pure half of solana_watch.

The transport layer is not tested here and cannot be: these run with no
network. What is tested is every function that turns an RPC response into a
decision, which is where the bugs that cost money live. Use
`python solana_watch.py --selftest` to verify the transport against mainnet.
"""

from __future__ import annotations

import pytest

import solana_watch as sw
from tests import fixtures as fx

# ------------------------------------------------------------ transfers


def test_parse_transfers_reads_the_diff_not_the_instruction():
    ts = {(t.owner, t.delta) for t in sw.parse_transfers(fx.BUY_NOT_LAUNCH)}
    assert ts == {(fx.CREATOR, -100_000_000_000_000),
                  (fx.BUYER, 100_000_000_000_000)}


def test_parse_transfers_ignores_unchanged_balances():
    assert sw.parse_transfers(fx.NO_CHANGE) == []


def test_parse_transfers_handles_accounts_absent_from_pre():
    ts = {t.owner: t.delta for t in sw.parse_transfers(fx.WHALE_MOVE)}
    assert ts[fx.WHALE] == 20_000_000_000_000


def test_transfer_ui_scales_by_decimals():
    t = sw.Transfer(mint="m", owner="o", delta=1_500_000, decimals=6)
    assert t.ui == 1.5


@pytest.mark.parametrize("tx", fx.MALFORMED)
def test_parse_transfers_never_raises_on_malformed_input(tx):
    """A malformed response must degrade to 'no information', never crash the loop."""
    assert sw.parse_transfers(tx) == []


# -------------------------------------------------------------- launches


def test_parse_launch_detects_a_real_launch():
    got = sw.parse_launch(fx.LAUNCH)
    assert got is not None
    assert got.mint == fx.MINT_A
    assert got.creator == fx.CREATOR
    assert got.signature == "launchSig"
    assert got.slot == 1234


def test_parse_launch_rejects_failed_transactions():
    """A reverted transaction created nothing, whatever its balances imply."""
    assert sw.parse_launch(fx.LAUNCH_FAILED) is None


def test_parse_launch_rejects_a_buy_on_an_existing_mint():
    assert sw.parse_launch(fx.BUY_NOT_LAUNCH) is None


def test_parse_launch_rejects_mints_from_other_programs():
    assert sw.parse_launch(fx.FOREIGN_LAUNCH) is None


@pytest.mark.parametrize("tx", fx.MALFORMED)
def test_parse_launch_never_raises_on_malformed_input(tx):
    assert sw.parse_launch(tx) is None


def test_program_ids_includes_inner_instructions():
    assert sw.program_ids(fx.LAUNCH) == {fx.PUMP, fx.TOKEN_PROG}


def test_fee_payer_prefers_the_signer():
    assert sw.fee_payer(fx.LAUNCH) == fx.CREATOR


def test_fee_payer_tolerates_plain_string_account_keys():
    tx = {"transaction": {"message": {"accountKeys": ["Abc123"]}}}
    assert sw.fee_payer(tx) == "Abc123"


# ---------------------------------------------------------------- whales


def test_is_whale_at_the_threshold():
    t = sw.Transfer(mint="m", owner="o", delta=15_000, decimals=6)
    assert sw.is_whale(t, supply=1_000_000, fraction=0.015) is True


def test_is_whale_just_below_the_threshold():
    t = sw.Transfer(mint="m", owner="o", delta=14_999, decimals=6)
    assert sw.is_whale(t, supply=1_000_000, fraction=0.015) is False


def test_is_whale_counts_outflows_too():
    t = sw.Transfer(mint="m", owner="o", delta=-50_000, decimals=6)
    assert sw.is_whale(t, supply=1_000_000, fraction=0.015) is True


@pytest.mark.parametrize("supply", [0, -1])
def test_unknown_supply_is_not_a_small_supply(supply):
    """Supply 0 must not divide by zero and must not guess. See torah.md."""
    t = sw.Transfer(mint="m", owner="o", delta=10**18, decimals=6)
    assert sw.is_whale(t, supply=supply) is False


# ----------------------------------------------------------------- mcap


def test_mcap_sol_from_curve_reserves():
    # 30 SOL against 1,000,000 tokens -> 3e-5 SOL each -> 30,000 SOL for 1B.
    got = sw.mcap_sol(virtual_sol_reserves=30 * 10**9,
                      virtual_token_reserves=1_000_000 * 10**6)
    assert got == pytest.approx(30_000.0)


def test_mcap_sol_refuses_to_invent_a_number():
    """An unreadable curve returns None, never 0.0 — a zero reads as a fact."""
    assert sw.mcap_sol(0, 0) is None
    assert sw.mcap_sol(10**9, 0) is None
    assert sw.mcap_sol(-1, 10**6) is None


def test_bands_crossed_is_monotonic():
    bands = [50.0, 100.0, 250.0]
    first = sw.bands_crossed(120.0, already=set(), bands=bands)
    assert first == [50.0, 100.0]
    again = sw.bands_crossed(120.0, already=set(first), bands=bands)
    assert again == [], "a band already crossed must never re-emit"


def test_bands_crossed_handles_a_jump_past_several():
    assert sw.bands_crossed(9_999.0, already=set(), bands=[50.0, 100.0, 250.0]) == [
        50.0, 100.0, 250.0
    ]


# ------------------------------------------------------------ clustering


def test_funding_root_found_when_all_holders_share_one_funder():
    funders = {"h1": "F", "h2": "F", "h3": "F"}
    assert sw.funding_root(funders, ["h1", "h2", "h3"]) == "F"


def test_funding_root_none_when_funders_differ():
    funders = {"h1": "F", "h2": "G"}
    assert sw.funding_root(funders, ["h1", "h2"]) is None


def test_unknown_funder_blocks_the_cluster_claim():
    """One unknown funder must not be assumed to match the known ones.

    This is the difference between a coordination check and a coordination
    guess, and the guess is the one that enters the wrong trade.
    """
    funders = {"h1": "F", "h2": "F"}
    assert sw.funding_root(funders, ["h1", "h2", "h3_unknown"]) is None


def test_funding_root_on_empty_holders_is_none():
    assert sw.funding_root({}, []) is None


# ------------------------------------------------------------------ rpc


def test_rpc_error_is_raised_not_swallowed(monkeypatch):
    """'Endpoint failed' and 'nothing happened' must never look the same."""
    class Boom:
        status_code = 500
        def json(self):      # pragma: no cover
            return {}

    monkeypatch.setattr(sw._session, "post", lambda *a, **k: Boom())
    with pytest.raises(sw.RpcError):
        sw.rpc("getSlot")


def test_rpc_surfaces_rate_limiting_distinctly(monkeypatch):
    class Limited:
        status_code = 429
        def json(self):      # pragma: no cover
            return {}

    monkeypatch.setattr(sw._session, "post", lambda *a, **k: Limited())
    with pytest.raises(sw.RpcError, match="rate limited"):
        sw.rpc("getSlot")


def test_rpc_raises_on_jsonrpc_error_payload(monkeypatch):
    class Err:
        status_code = 200
        def json(self):
            return {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32602, "message": "Invalid param"}}

    monkeypatch.setattr(sw._session, "post", lambda *a, **k: Err())
    with pytest.raises(sw.RpcError, match="Invalid param"):
        sw.rpc("getTokenSupply", ["bad"])
