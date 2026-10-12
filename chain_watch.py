"""
chain_watch.py — the on-chain half of BLOB.

Talks to Robinhood Chain two ways, because they are good at different things:

  RPC        https://rpc.mainnet.chain.robinhood.com   chain 4663 (0x1237)
             live logs. Arbitrum Orbit, standard EVM, no chain-specific SDK.
             Public endpoint is free and keyless but shared and rate limited —
             fine to prototype, swap in QuickNode/Alchemy/Chainstack/Dwellir
             before you poll every block. Only the URL changes.

  Blockscout https://robinhoodchain.blockscout.com/api
             pre-indexed history. Address txlists, token transfers, holders.
             Free, no key. Use it for anything historical instead of
             backfilling yourself — it is already done.

Note for anyone porting a sniper here: ordering is first-come-first-served
and eth_maxPriorityFeePerGas returns zero. There is no gas auction on this
chain. You cannot outbid, only arrive earlier — and arriving earlier costs
you 99% to the opening tax. See torah.md.

What this file produces, all of it onto bus.events:

  token_launch / token_graduate     lifecycle
  mcap_cross                        the clock that defines "early"
  whale_transfer                    >1.5% of supply in one tx
  wallet_signal                     a tracked whale/KOL moved
  cluster_detected                  wallets sharing a funder
  outcome_scored                    -> closes the loop into scout_watch

It never claims a wallet belongs to an X account. That link does not exist
publicly, an agent asked to guess it will invent one, and because the
weights train on these outcomes a single invented link poisons every score
downstream. Wallet identity here is either self-disclosed or absent.
"""

import json
import os
import sqlite3
import statistics
import time

import requests
from web3 import Web3

import bus

# --------------------------------------------------------------- endpoints
RPC = os.environ.get("RHC_RPC", "https://rpc.mainnet.chain.robinhood.com")
BS = os.environ.get("RHC_BLOCKSCOUT", "https://robinhoodchain.blockscout.com/api")
CHAIN_ID = 4663

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))

DB = bus.ROOT / "chain.db"
STEP = 500                      # small windows survive a shared RPC
CONFIRMATIONS = 3
SUPPLY = 10**9 * 10**18         # pons standard, 18dp
EARLY_MCAP = 250_000            # "early" = touched it before this
WHALE_PCT = 0.015               # 1.5% of supply in one tx
BRIDGE_WINDOW = 3600

TRANSFER = w3.keccak(text="Transfer(address,address,uint256)").hex()

# ---- FILL FROM THE VERIFIED PONS V2 CONTRACT ------------------------------
FACTORY = os.environ.get("PONS_FACTORY", "")
EV_LAUNCH = w3.keccak(text="TokenLaunched(address,address,string,uint256)").hex()
EV_BUY = w3.keccak(text="Bought(address,address,uint256,uint256)").hex()
EV_GRADUATE = w3.keccak(text="Graduated(address,address,uint256)").hex()
# Decoders live in one place (below) so an ABI change is 20 lines, not a rewrite.

KNOWN_BRIDGES = {
    # lowercase addr -> label. from docs.robinhood.com/chain + relay/across routers
}


# --------------------------------------------------------------- storage
def db():
    c = sqlite3.connect(DB, timeout=30)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS tokens(
      contract TEXT PRIMARY KEY, ticker TEXT, deployer TEXT,
      launched_at REAL, early_at REAL, graduated INT DEFAULT 0,
      graduated_at REAL, peak_mcap REAL DEFAULT 0, resolved INT DEFAULT 0);

    CREATE TABLE IF NOT EXISTS deployers(
      addr TEXT PRIMARY KEY, launches INT DEFAULT 0, graduates INT DEFAULT 0);

    -- one row per wallet we have ever seen do something interesting
    CREATE TABLE IF NOT EXISTS wallets(
      addr TEXT PRIMARY KEY,
      label TEXT DEFAULT 'unknown',      -- whale | kol | deployer | fresh | unknown
      handle TEXT,                       -- ONLY if self-disclosed. never inferred
      first_seen REAL, last_seen REAL,
      entries INT DEFAULT 0,             -- tokens entered
      early_entries INT DEFAULT 0,       -- entered before EARLY_MCAP
      wins INT DEFAULT 0,                -- entered early AND it graduated
      avg_hold_s REAL DEFAULT 0,
      funder TEXT,                       -- first inbound address = funding root
      tracked INT DEFAULT 0);

    -- every position a wallet took, so behaviour is comparable across wallets
    CREATE TABLE IF NOT EXISTS positions(
      addr TEXT, contract TEXT,
      first_in REAL, last_out REAL,
      in_mcap REAL, out_mcap REAL,
      was_early INT DEFAULT 0,
      PRIMARY KEY(addr, contract));

    CREATE TABLE IF NOT EXISTS cursor(k TEXT PRIMARY KEY, v INT);
    """)
    return c


# --------------------------------------------------------------- blockscout
def bs_get(params, v2=None, retries=2):
    """v1 is the Etherscan-compatible surface; v2 is richer. Both free."""
    url = f"{BS.replace('/api','')}/api/v2{v2}" if v2 else BS
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=None if v2 else params, timeout=15)
            if r.status_code == 429:
                time.sleep(2 + i * 3)
                continue
            j = r.json()
            return j.get("items") if v2 else j.get("result")
        except Exception as e:
            if i == retries:
                bus.emit("blockscout_error", err=str(e))
            time.sleep(1 + i * 2)
    return None


def addr_txs(addr, n=30, order="asc"):
    return bs_get({"module": "account", "action": "txlist", "address": addr,
                   "page": 1, "offset": n, "sort": order}) or []


def token_transfers(contract, n=100):
    return bs_get({"module": "account", "action": "tokentx",
                   "contractaddress": contract, "page": 1,
                   "offset": n, "sort": "asc"}) or []


def token_holders(contract):
    return bs_get(None, v2=f"/tokens/{contract}/holders") or []


# --------------------------------------------------------------- indexing
def dec_launch(lg):
    tok = Web3.to_checksum_address("0x" + lg["topics"][1].hex()[-40:])
    dep = Web3.to_checksum_address("0x" + lg["topics"][2].hex()[-40:])
    ticker = w3.codec.decode(["string", "uint256"], lg["data"])[0]
    return tok, dep, ticker


def dec_buy(lg):
    tok = Web3.to_checksum_address("0x" + lg["topics"][1].hex()[-40:])
    buyer = Web3.to_checksum_address("0x" + lg["topics"][2].hex()[-40:])
    _amt, price = w3.codec.decode(["uint256", "uint256"], lg["data"])
    return tok, buyer, price / 1e18


def scan_pons(c, frm, to):
    if not FACTORY:
        return
    logs = w3.eth.get_logs({"fromBlock": frm, "toBlock": to,
                            "address": Web3.to_checksum_address(FACTORY),
                            "topics": [[EV_LAUNCH, EV_BUY, EV_GRADUATE]]})
    for lg in logs:
        t = lg["topics"][0].hex()
        ts = w3.eth.get_block(lg["blockNumber"])["timestamp"]

        if t == EV_LAUNCH:
            tok, dep, ticker = dec_launch(lg)
            c.execute("INSERT OR IGNORE INTO tokens(contract,ticker,deployer,"
                      "launched_at) VALUES(?,?,?,?)", (tok, ticker, dep, ts))
            c.execute("INSERT INTO deployers(addr,launches) VALUES(?,1) "
                      "ON CONFLICT(addr) DO UPDATE SET launches=launches+1", (dep,))
            touch_wallet(c, dep, ts, label="deployer")
            bus.emit("token_launch", contract=tok, ticker=ticker, deployer=dep,
                     deployer_record=deployer_record(c, dep),
                     bridge_funded_60m=bridge_funded(dep))

        elif t == EV_BUY:
            tok, buyer, price = dec_buy(lg)
            mcap = price * (SUPPLY / 1e18)
            row = c.execute("SELECT peak_mcap, early_at, ticker FROM tokens "
                            "WHERE contract=?", (tok,)).fetchone()
            if not row:
                continue
            peak, early_at, ticker = row
            if mcap > peak:
                c.execute("UPDATE tokens SET peak_mcap=? WHERE contract=?", (mcap, tok))
            if early_at is None and mcap >= EARLY_MCAP:
                c.execute("UPDATE tokens SET early_at=? WHERE contract=?", (ts, tok))
                bus.emit("mcap_cross", contract=tok, ticker=ticker,
                         mcap=EARLY_MCAP, at=ts)
            record_entry(c, buyer, tok, ts, mcap, early=(early_at is None))

        elif t == EV_GRADUATE:
            tok = Web3.to_checksum_address("0x" + lg["topics"][1].hex()[-40:])
            c.execute("UPDATE tokens SET graduated=1, graduated_at=? "
                      "WHERE contract=?", (ts, tok))
            d = c.execute("SELECT deployer,ticker FROM tokens WHERE contract=?",
                          (tok,)).fetchone()
            if d:
                c.execute("UPDATE deployers SET graduates=graduates+1 WHERE addr=?",
                          (d[0],))
                bus.emit("token_graduate", contract=tok, ticker=d[1])
    c.commit()


def scan_transfers(c, frm, to):
    """Whale movement on tokens we still care about."""
    live = [r[0] for r in c.execute(
        "SELECT contract FROM tokens WHERE resolved=0 LIMIT 120")]
    if not live:
        return
    logs = w3.eth.get_logs({"fromBlock": frm, "toBlock": to,
                            "address": live, "topics": [TRANSFER]})
    for lg in logs:
        try:
            frm_a = Web3.to_checksum_address("0x" + lg["topics"][1].hex()[-40:])
            to_a = Web3.to_checksum_address("0x" + lg["topics"][2].hex()[-40:])
            amt = int(lg["data"].hex(), 16)
        except Exception:
            continue
        pct = amt / SUPPLY
        if pct < WHALE_PCT:
            continue
        tok = Web3.to_checksum_address(lg["address"])
        tracked = c.execute("SELECT label, handle FROM wallets WHERE addr=? "
                            "AND tracked=1", (to_a,)).fetchone()
        bus.emit("whale_transfer", contract=tok, frm=frm_a, to=to_a,
                 pct_supply=round(pct * 100, 3),
                 tx=lg["transactionHash"].hex())
        if tracked:
            bus.emit("wallet_signal", addr=to_a, label=tracked[0],
                     handle=tracked[1], contract=tok,
                     pct_supply=round(pct * 100, 3))


# --------------------------------------------------- wallet behaviour
def touch_wallet(c, addr, ts, label=None):
    c.execute("INSERT INTO wallets(addr,first_seen,last_seen) VALUES(?,?,?) "
              "ON CONFLICT(addr) DO UPDATE SET last_seen=?", (addr, ts, ts, ts))
    if label:
        c.execute("UPDATE wallets SET label=? WHERE addr=? AND label='unknown'",
                  (label, addr))


def record_entry(c, addr, contract, ts, mcap, early):
    touch_wallet(c, addr, ts)
    c.execute("INSERT OR IGNORE INTO positions(addr,contract,first_in,in_mcap,"
              "was_early) VALUES(?,?,?,?,?)", (addr, contract, ts, mcap, int(early)))


def profile(c, addr):
    """Behaviour, not identity. Everything here is observed."""
    rows = c.execute("SELECT p.was_early, p.first_in, p.last_out, p.in_mcap, "
                     "t.graduated, t.peak_mcap FROM positions p "
                     "JOIN tokens t ON t.contract=p.contract "
                     "WHERE p.addr=?", (addr,)).fetchall()
    if not rows:
        return None
    n = len(rows)
    early = sum(r[0] for r in rows)
    wins = sum(1 for r in rows if r[0] and r[4])
    holds = [r[2] - r[1] for r in rows if r[2] and r[1]]
    entry_pct = [r[3] / r[5] for r in rows if r[5]]
    return {
        "entries": n,
        "early_rate": round(early / n, 3),
        "win_rate": round(wins / early, 3) if early else None,
        "median_hold_s": int(statistics.median(holds)) if holds else None,
        "median_entry_vs_peak": round(statistics.median(entry_pct), 4) if entry_pct else None,
    }


def promote_wallets(c, min_entries=4, min_early=0.5, min_wins=2):
    """A whale is a wallet that is repeatedly early on things that work.
    Not a wallet that is merely big."""
    for (addr,) in c.execute("SELECT addr FROM wallets WHERE tracked=0"):
        p = profile(c, addr)
        if not p or p["entries"] < min_entries:
            continue
        wins = int((p["win_rate"] or 0) * p["entries"] * p["early_rate"])
        if p["early_rate"] >= min_early and wins >= min_wins:
            c.execute("UPDATE wallets SET tracked=1, label=CASE WHEN label='unknown' "
                      "THEN 'whale' ELSE label END WHERE addr=?", (addr,))
            bus.emit("wallet_tracked", addr=addr, **p)
    c.commit()


def funding_cluster(c, addrs):
    """The on-chain twin of social cluster collapse: wallets sharing a first
    funder are one actor wearing several faces."""
    roots = {}
    for a in addrs:
        r = c.execute("SELECT funder FROM wallets WHERE addr=?", (a,)).fetchone()
        f = r[0] if r and r[0] else first_funder(a)
        if f:
            c.execute("UPDATE wallets SET funder=? WHERE addr=?", (f, a))
            roots.setdefault(f.lower(), []).append(a)
    c.commit()
    clusters = {k: v for k, v in roots.items() if len(v) > 1}
    if clusters:
        bus.emit("cluster_detected", clusters={k: len(v) for k, v in clusters.items()},
                 collapsed_from=len(addrs), collapsed_to=len(roots))
    return clusters


def first_funder(addr):
    txs = addr_txs(addr, n=5, order="asc")
    for t in txs:
        if t.get("to", "").lower() == addr.lower() and int(t.get("value", 0)) > 0:
            return t.get("from")
    return None


def bridge_funded(addr):
    """Hard gate in torah.md, so it must be exact. Never inferred."""
    if not KNOWN_BRIDGES:
        return None                     # honest 'unknown', not a false 'no'
    now = time.time()
    for t in addr_txs(addr, n=25, order="asc"):
        if t.get("to", "").lower() != addr.lower():
            continue
        if now - int(t.get("timeStamp", 0)) > BRIDGE_WINDOW:
            continue
        if t.get("from", "").lower() in KNOWN_BRIDGES:
            return True
    return False


def deployer_record(c, addr):
    r = c.execute("SELECT launches,graduates FROM deployers WHERE addr=?",
                  (addr,)).fetchone()
    if not r:
        return {"launches": 0, "graduates": 0, "rate": None, "serial": False}
    launches, graduates = r
    return {"launches": launches, "graduates": graduates,
            "rate": round(graduates / launches, 4) if launches else None,
            "serial": launches >= 10 and (graduates / launches) < 0.05}


def first_buyers(c, contract, n=50):
    """On-chain half of the coordination ratio. WEIGHER intersects this with
    the loud accounts."""
    out, seen = [], set()
    for t in token_transfers(contract, n * 2):
        a = (t.get("to") or "").lower()
        if a and a not in seen:
            seen.add(a)
            out.append(a)
        if len(out) >= n:
            break
    return out


# ------------------------------------------------------------- THE JOIN
def social_early(ticker, before_ts, window_h=6):
    """Which watchlist accounts touched this ticker before it crossed the
    mcap floor. Text + timestamp only. No identity claim, nothing to invent."""
    tag = ticker.upper().lstrip("$")
    floor = before_ts - window_h * 3600
    # Full scan, deliberately: this is a backward-looking join over a closed
    # time window, not a streaming tail, so there is no cursor to carry. On a
    # long-lived events.jsonl this is the call to rotate the log for.
    hits = set()
    evs, _ = bus.tail(bus.EVENTS, 0)
    for e in evs:
        if not (floor <= e["ts"] < before_ts):
            continue
        blob = json.dumps(e).upper()
        if f"${tag}" in blob or f'"{tag}"' in blob:
            h = e.get("handle") or e.get("source")
            if h:
                hits.add(h)
    return hits


def correlate(c):
    """Close the loop. Hands outcomes back to the watchlist so it learns."""
    import scout_watch
    now = time.time()
    rows = c.execute("SELECT contract,ticker,graduated,launched_at,early_at "
                     "FROM tokens WHERE resolved=0").fetchall()
    for contract, ticker, grad, launched, early_at in rows:
        if not grad and (now - launched) / 3600 < 24:
            continue                        # still has a chance
        cutoff = early_at or (launched + 3600)
        handles = social_early(ticker, cutoff)
        buyers = first_buyers(c, contract)
        funding_cluster(c, buyers[:25])
        if handles:
            scout_watch.record_outcome(scout_watch.db(), contract,
                                       bool(grad), list(handles))
            bus.emit("outcome_scored", contract=contract, ticker=ticker,
                     graduated=bool(grad), early_accounts=len(handles),
                     first_buyers=len(buyers))
        c.execute("UPDATE tokens SET resolved=1 WHERE contract=?", (contract,))
    c.commit()


# --------------------------------------------------------------- loop
def run():
    c = db()
    assert w3.eth.chain_id == CHAIN_ID, f"wrong chain: {w3.eth.chain_id}"
    r = c.execute("SELECT v FROM cursor WHERE k='block'").fetchone()
    cur = r[0] if r else w3.eth.block_number - 5000
    bus.emit("chain_watch_up", rpc=RPC.split("//")[-1].split("/")[0], chain=CHAIN_ID)

    while True:
        if bus.halted():
            time.sleep(5)
            continue
        head = w3.eth.block_number - CONFIRMATIONS
        while cur < head:
            to = min(cur + STEP, head)
            try:
                scan_pons(c, cur + 1, to)
                scan_transfers(c, cur + 1, to)
                cur = to
                c.execute("INSERT OR REPLACE INTO cursor VALUES('block',?)", (cur,))
                c.commit()
            except Exception as e:
                bus.emit("chain_error", err=str(e), block=cur)
                time.sleep(8)               # shared RPC throttling looks like this
                break
        promote_wallets(c)
        correlate(c)
        time.sleep(10)


if __name__ == "__main__":
    run()
