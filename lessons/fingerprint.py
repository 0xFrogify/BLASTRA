"""
fingerprint.py — deduplication for the lesson file.

The failure mode this exists to prevent: you open the brain map, grab three
spheres in the same cluster, and they all say the same thing in different
words. That is not a brain with three lessons. It is a brain with one lesson
and a counting bug — and every duplicate pulls on decisions as though it were
independent evidence.

Two mechanisms, because one is not enough:

  fingerprint()   exact hash of the normalised rule. Catches copy-paste and
                  trivial edits. Cheap, deterministic, stored in the file.

  similarity()    Jaccard overlap of the normalised token sets. Catches the
                  case that actually happens — the same rule rewritten weeks
                  later in different words, which no hash will ever catch,
                  because hashing is designed to make near-misses look nothing
                  alike.

Normalisation lowercases, strips punctuation, drops a small stopword list,
stems crudely, maps a handful of domain synonyms onto one spelling, and buckets
numbers by order of magnitude — so "more than 6 holders" and "more than 7
holders" normalise identically, while 6 and 60 stay apart.

Usage:
    python fingerprint.py "reject when the funding root resolves to one wallet"
    python fingerprint.py --check lessons.jsonl
    python fingerprint.py --check lessons.jsonl --threshold 0.55
"""

import hashlib
import json
import re
import sys
from collections import defaultdict

# Deliberately small. A long stopword list starts deleting the words that carry
# the rule, and then unrelated lessons begin to collide.
STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "at", "by", "for", "with", "from", "as", "that",
    "this", "it", "its", "and", "or", "if", "then", "than", "when", "while",
    "do", "does", "did", "not", "no", "any", "all", "more", "most", "very",
    "into", "across", "after", "before", "never", "always", "which", "what",
    "there", "their", "them", "they", "you", "your", "we", "our", "will",
}

# Words this domain uses interchangeably. Without this, the same rule written
# twice by two different desks never collides.
SYNONYM = {
    "single": "one", "sole": "one", "lone": "one",
    "refuse": "reject", "block": "reject", "decline": "reject", "skip": "reject",
    "enter": "entry", "buy": "entry", "fill": "entry",
    "collapse": "merge", "fold": "merge", "combine": "merge",
    "account": "acct", "wallet": "acct", "address": "acct",
    "funder": "funding", "root": "funding",
    "score": "weight", "scoring": "weight", "rank": "weight",
    "afterward": "afterwards", "later": "afterwards",
    "contract": "token", "coin": "token", "mint": "token",
    "size": "sizing", "position": "sizing",
    "cap": "mcap", "marketcap": "mcap",
    "halve": "half", "cut": "half", "reduce": "half",
    "take": "entry", "taking": "entry",
}

NUMBUCKET = re.compile(r"^n\d+$")

SUFFIXES = ("ingly", "edly", "ing", "ed", "es", "s", "ly")


def _stem(w):
    for suf in SUFFIXES:
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _bucket(tok):
    """Bucket a number by ORDER OF MAGNITUDE only.

    6 and 7 both become n1, so two derivations of the same rule at slightly
    different cutoffs merge instead of becoming two lessons. 6 and 60 stay
    apart, because an order of magnitude is a real difference.
    """
    try:
        v = abs(float(tok))
    except ValueError:
        return tok
    if v == 0:
        return "n0"
    mag = 0
    while v >= 10:
        v /= 10
        mag += 1
    return f"n{mag + 1}"


def tokens(rule):
    """Normalised token SET. Order is discarded; it carries no rule meaning."""
    s = rule.lower().replace("—", " ").replace("–", " ")
    s = re.sub(r"[^a-z0-9\.\s-]", " ", s)
    out = set()
    for tok in s.split():
        tok = tok.strip(".-")
        if not tok:
            continue
        if re.fullmatch(r"\d+(\.\d+)?", tok):
            out.add(_bucket(tok))
            continue
        tok = SYNONYM.get(tok, tok)
        if tok in STOP:
            continue
        tok = _stem(tok)
        out.add(SYNONYM.get(tok, tok))
    return out


def normalise(rule):
    return " ".join(sorted(tokens(rule)))


def fingerprint(rule):
    return hashlib.sha256(normalise(rule).encode("utf-8")).hexdigest()[:16]


def similarity(a, b):
    """Jaccard overlap of two rules' token sets. 1.0 is identical."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def conflicts(a, b):
    """True when two rules are the same sentence at different magnitudes.

    "reject above 6 holders" and "reject above 600 holders" are not duplicates
    to merge — they are the same rule calibrated two different ways, and one of
    them is wrong. Merging them silently averages a disagreement. Surfacing it
    is the useful behaviour.
    """
    ta, tb = tokens(a), tokens(b)
    diff = ta ^ tb
    return bool(diff) and all(NUMBUCKET.match(t) for t in diff)


# Tuned against reworded pairs of real lessons. Above this, two rules are
# saying the same thing; raise it if you are getting false merges, lower it if
# your map is filling up with spheres that read alike.
DEFAULT_THRESHOLD = 0.55


def check(path, threshold=DEFAULT_THRESHOLD):
    """Report lessons that should have been merged. Exit 1 if any are found."""
    recs, bad = [], 0
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"line {i}: not valid JSON — {e}")
                bad += 1
                continue
            want = fingerprint(rec["rule"])
            if rec.get("fingerprint") != want:
                print(f"line {i}: {rec.get('id')} fingerprint is "
                      f"{rec.get('fingerprint')}, computed {want}")
                bad += 1
            recs.append(rec)

    exact = defaultdict(list)
    for r in recs:
        exact[fingerprint(r["rule"])].append(r["id"])
    dupes = {k: v for k, v in exact.items() if len(v) > 1}
    for fp, ids in dupes.items():
        print(f"EXACT    {fp}: {', '.join(map(str, ids))} — merge these")

    near, clash = [], []
    for i in range(len(recs)):
        for j in range(i + 1, len(recs)):
            ra, rb = recs[i]["rule"], recs[j]["rule"]
            if fingerprint(ra) == fingerprint(rb):
                continue
            if conflicts(ra, rb):
                clash.append((recs[i], recs[j]))
                continue
            s = similarity(ra, rb)
            if s >= threshold:
                near.append((s, recs[i], recs[j]))

    for a, b in clash:
        print(f"CONFLICT {a['id']} / {b['id']}  — same rule, different magnitude. "
              f"Do not merge; one of these is miscalibrated")
        print(f"           A: {a['rule'][:72]}…")
        print(f"           B: {b['rule'][:72]}…")

    for s, a, b in sorted(near, key=lambda x: -x[0]):
        print(f"NEAR     {s:.2f}  {a['id']} / {b['id']}  — likely the same lesson")
        print(f"           A: {a['rule'][:72]}…")
        print(f"           B: {b['rule'][:72]}…")

    print(f"\n{len(recs)} lessons, {len(exact)} distinct by hash, "
          f"{len(dupes)} exact collision group(s), {len(near)} near-duplicate pair(s) "
          f"at threshold {threshold}, {len(clash)} calibration conflict(s), "
          f"{bad} malformed")
    return 1 if (dupes or near or clash or bad) else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--check" and len(args) >= 2:
        thr = DEFAULT_THRESHOLD
        if "--threshold" in args:
            thr = float(args[args.index("--threshold") + 1])
        sys.exit(check(args[1], thr))
    if len(args) >= 2 and args[0] == "--compare":
        print(f"similarity: {similarity(args[1], args[2]):.3f}")
        sys.exit(0)
    if not args:
        print(__doc__)
        sys.exit(2)
    rule = " ".join(args)
    print(f"normalised : {normalise(rule)}")
    print(f"fingerprint: {fingerprint(rule)}")
