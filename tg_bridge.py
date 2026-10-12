"""
tg_bridge.py — Telegram as BLOB's control plane. Nothing more.

CHIEF signs and submits its own transactions. This file never touches a key,
never places an order, never sits in the hot path. It does three things:

  1. tells you what CHIEF already did
  2. asks for APPROVE only when torah.md says to ask
  3. gives you a kill switch that works from a train, in one tap

    CHIEF ──signs & submits──► Robinhood Chain
      │
      └──card──► orders.jsonl ──► tg_bridge ──► phone
                 control.json ◄──── your taps

Telegram is downstream of execution. If the Bot API dies, CHIEF keeps
trading and keeps running its ladders — you just stop hearing about it.
That is the correct failure mode. The reverse would strand open positions.

DEFAULT IS AUTO. A trade that passed every gate in torah.md does not need
your permission; that is what the gates were for. You get a card after the
fact with a KILL button on it.
"""

import json, time, os, threading
from pathlib import Path

import requests
import yaml

import bus

TOKEN = os.environ["TG_BOT_TOKEN"]          # revocable. never a user session.
CHAT = str(os.environ["TG_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"

TORAH = bus.ROOT / "torah.md"
DECISIONS = bus.STATE / "decisions.jsonl"

# hot-tier social noise only. warm and cold chatter stays in the logs, or you
# will train yourself to ignore your own phone inside two days.
ALERT_EVENTS = {"velocity_spike", "follow", "wallet_signal", "cluster_detected"}
HOT_ONLY = {"velocity_spike", "follow"}


# --------------------------------------------------------------- config
def cfg():
    """Execution policy lives in torah.md, not here. This file has no opinions."""
    d = {"mode": "auto", "auto_below_usd": 400, "ask_above_usd": 400,
         "approval_timeout_s": 90, "on_timeout": "skip"}
    try:
        text = TORAH.read_text()
        block = text.split("```yaml")[1].split("```")[0] if "```yaml" in text else text
        d.update((yaml.safe_load(block) or {}).get("execution", {}))
    except Exception:
        pass
    return d


def control():
    c = bus.control()
    c.setdefault("mode", "run")
    c.setdefault("manual", cfg()["mode"] == "manual")
    return c


def set_control(**kw):
    c = control()
    c.update(kw)
    c["updated"] = time.time()
    bus.CONTROL.write_text(json.dumps(c, indent=2))
    return c


# --------------------------------------------------------------- telegram
def send(text, buttons=None):
    body = {"chat_id": CHAT, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if buttons:
        body["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        return requests.post(f"{API}/sendMessage", json=body, timeout=10).json()
    except Exception as e:
        bus.emit("tg_error", err=str(e))       # never raise into the loop
        return None


def ack(cb_id, text):
    try:
        requests.post(f"{API}/answerCallbackQuery",
                      json={"callback_query_id": cb_id, "text": text}, timeout=8)
    except Exception:
        pass


KILL = [[{"text": "KILL ALL", "callback_data": "kill"},
         {"text": "HALT NEW", "callback_data": "halt"}]]


def approve_kb(oid):
    return [[{"text": "APPROVE", "callback_data": f"ok:{oid}"},
             {"text": "SKIP", "callback_data": f"no:{oid}"}],
            [{"text": "KILL ALL", "callback_data": "kill"}]]


# --------------------------------------------------------------- cards
def fmt_gates(g):
    return "\n".join(f"  {'PASS' if v else 'FAIL'}  {k}" for k, v in g.items())


def card_entry(o, pending):
    head = ("ENTRY — NEEDS APPROVAL" if pending else "FILLED")
    tx = o.get("tx")
    return (f"<b>BLOB · {head}</b>\n"
            f"<code>{o['ticker']}</code>  {o['contract'][:12]}…\n\n"
            f"score      <b>{o['score']}</b> / {o['threshold']}\n"
            f"indep nodes {o.get('independent','—')}   "
            f"coord {o.get('coordination','—')}\n"
            f"mcap       {o.get('mcap','—')}\n"
            f"size       <b>${o['size_usd']:,.0f}</b>  "
            f"({o.get('risk_pct','—')}% of book)\n"
            f"audit      {o.get('audit','—')}\n\n"
            f"<b>gates</b>\n<pre>{fmt_gates(o.get('gates', {}))}</pre>"
            + (f"\ntx <code>{tx[:14]}…</code>" if tx else "")
            + (f"\nladder {o.get('ladder','—')}" if not pending else "")
            + ("\n\n<i>auto — already submitted</i>" if not pending else
               f"\n\n<i>expires in {cfg()['approval_timeout_s']}s → skip</i>"))


def card_exit(o):
    p = o["pnl"]
    s = "+" if p >= 0 else "−"
    return (f"<b>BLOB · {o['pct']}% SOLD</b>\n"
            f"<code>{o['ticker']}</code> @ {o['mcap']}\n"
            f"trigger: {o['trigger']}\n"
            f"realised <b>{s}${abs(p):,.2f}</b>   "
            f"session {s}${abs(o.get('session_total', p)):,.2f}")


def card_blocked(o):
    return (f"<b>BLOB · BLOCKED</b>\n<code>{o['ticker']}</code>\n"
            f"{o['reason']}\nscore {o.get('score','—')} — no entry.")


# --------------------------------------------------------------- routing
PENDING = {}          # order_id -> deadline
OPEN = {}             # ticker -> size_usd


def needs_approval(o):
    """The only two reasons to ask. Everything else fires and reports."""
    c = cfg()
    if control()["manual"]:
        return True
    return float(o.get("size_usd", 0)) > float(c["ask_above_usd"])


def decide(oid, verdict):
    with DECISIONS.open("a") as f:
        f.write(json.dumps({"id": oid, "decision": verdict,
                            "ts": time.time()}) + "\n")
    PENDING.pop(oid, None)


def push(o):
    kind = o.get("kind")

    if kind == "entry":
        if needs_approval(o):
            oid = o["id"]
            PENDING[oid] = time.time() + cfg()["approval_timeout_s"]
            send(card_entry(o, pending=True), approve_kb(oid))
        else:
            OPEN[o["ticker"]] = o.get("size_usd", 0)
            send(card_entry(o, pending=False), KILL)

    elif kind == "exit":
        if o.get("pct", 0) >= 100 or o.get("closed"):
            OPEN.pop(o["ticker"], None)
        send(card_exit(o))

    elif kind == "blocked":
        send(card_blocked(o))

    elif kind == "submit_failed":
        send(f"<b>BLOB · SUBMIT FAILED</b>\n<code>{o['ticker']}</code>\n"
             f"{o.get('err','')}\n<i>not retried — duplicate buy is worse "
             f"than a missed one</i>", KILL)


def expire_pending():
    """No answer is not a yes. Never fire on a timeout."""
    now = time.time()
    for oid, deadline in list(PENDING.items()):
        if now > deadline:
            decide(oid, cfg()["on_timeout"])
            send(f"<b>expired</b> — <code>{oid}</code> skipped, no answer in "
                 f"{cfg()['approval_timeout_s']}s")


# --------------------------------------------------------------- inbound
HELP = ("<b>BLOB control</b>\n"
        "/auto — fire without asking (default)\n"
        "/manual — approve every entry\n"
        "/kill — flatten everything, stop\n"
        "/halt — keep ladders, no new entries\n"
        "/resume — un-trip\n"
        "/status — mode, open positions")


def status():
    c = control()
    k = cfg()
    body = (f"mode <b>{c['mode']}</b>\n"
            f"entries <b>{'MANUAL' if c['manual'] else 'AUTO'}</b>"
            f"  <i>(ask above ${k['ask_above_usd']:,})</i>\n"
            f"open <b>{len(OPEN)}</b>"
            + (f" — {', '.join(OPEN)}" if OPEN else "")
            + f"\npending approval <b>{len(PENDING)}</b>")
    return body


def handle(u):
    if "callback_query" in u:
        cb = u["callback_query"]
        d, cid = cb["data"], cb["id"]

        if d == "kill":
            set_control(mode="kill"); ack(cid, "KILL SENT")
            send("<b>KILL SWITCH ENGAGED</b>\nEverything flattens on CHIEF's "
                 "next tick. No new entries until /resume.")
        elif d == "halt":
            set_control(mode="halt"); ack(cid, "halted")
            send("<b>HALTED</b>\nOpen ladders keep running. No new entries.")
        elif d.startswith(("ok:", "no:")):
            v, oid = d.split(":", 1)
            decide(oid, "approve" if v == "ok" else "skip")
            ack(cid, "approved" if v == "ok" else "skipped")
            send(("approved <code>%s</code>" % oid) if v == "ok"
                 else ("skipped <code>%s</code>" % oid))
        return

    msg = (u.get("message", {}).get("text") or "").strip().lower()
    if msg == "/auto":
        set_control(manual=False)
        send("entries <b>AUTO</b> — gates decide, you get told after.")
    elif msg == "/manual":
        set_control(manual=True)
        send("entries <b>MANUAL</b> — every entry waits for APPROVE.")
    elif msg == "/kill":
        set_control(mode="kill"); send("<b>KILL SWITCH ENGAGED</b>")
    elif msg == "/halt":
        set_control(mode="halt"); send("<b>HALTED</b>")
    elif msg == "/resume":
        set_control(mode="run"); send("resumed · mode <b>run</b>")
    elif msg == "/status":
        send(status())
    elif msg in ("/start", "/help"):
        send(HELP, KILL)


# --------------------------------------------------------------- loops
def poll_updates():
    """Own thread. A slow getUpdates must never delay an order card."""
    offset = 0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"offset": offset + 1, "timeout": 25},
                             timeout=35).json()
            for u in r.get("result", []):
                offset = u["update_id"]
                if str(u.get("message", {}).get("chat", {}).get("id",
                        u.get("callback_query", {}).get("message", {})
                         .get("chat", {}).get("id", ""))) != CHAT:
                    continue                      # one chat, nobody else
                try:
                    handle(u)
                except Exception as e:
                    bus.emit("tg_error", err=str(e))
        except Exception:
            time.sleep(2)


def run():
    op = ep = 0
    threading.Thread(target=poll_updates, daemon=True).start()
    send(f"BLOB online · {status()}", KILL)

    while True:
        new, op = bus.tail(bus.ORDERS, op)
        for o in new:
            try:
                push(o)
            except Exception as e:
                bus.emit("tg_error", err=str(e), order=o.get("id"))

        new, ep = bus.tail(bus.EVENTS, ep)
        for e in new:
            k = e.get("kind")
            if k not in ALERT_EVENTS:
                continue
            if k in HOT_ONLY and e.get("tier") != "hot":
                continue
            if k == "follow":
                send(f"<b>{e['source']}</b> followed <b>{e['target']}</b>")
            elif k == "velocity_spike":
                send(f"<b>{e['handle']}</b> velocity {e['ratio']}x baseline")
            elif k == "wallet_signal":
                send(f"tracked <b>{e['label']}</b> wallet moved "
                     f"{e['pct_supply']}% of <code>{e['contract'][:10]}…</code>")
            elif k == "cluster_detected":
                send(f"cluster — {e['collapsed_from']} wallets → "
                     f"{e['collapsed_to']} funders")

        expire_pending()
        time.sleep(0.5)


if __name__ == "__main__":
    run()
