# lessons/

The brain's memory. One falsifiable rule per line, JSONL.

**This directory ships with eight example lessons and no real ones.** They
exist to show the format and to give the tooling something to chew on. They are
not a starter brain, and copying them into production would mean running
somebody else's conclusions on your own money.

## The format

See [`schema.json`](schema.json) for the authoritative definition.

```json
{"id":"b-0003","cluster":"coordination",
 "rule":"Collapse accounts sharing a funding root to a single node before scoring, never after.",
 "fingerprint":"6235766a533d4a40","weight":0.52,"active":true,
 "evidence":[{"ref":"p-0077","outcome":"confirmed","note":"fifty mouths, one funder"}],
 "merged_from":[],"written_ts":1759568000.0,"last_touched_ts":1760000000.0}
```

`evidence[].ref` is deliberately opaque. It is your own reference for a closed
position, and nothing in this schema requires you to publish your book in order
to publish your lessons.

## What qualifies

Four tests, all of which must hold. The long version is in
[`../torah.md`](../torah.md) §5.

1. **Falsifiable.** "Be more careful" is not a lesson.
2. **Would have changed a decision.** Otherwise it is an observation.
3. **Not already in the file.** This is the one that breaks.
4. **Survives a counterfactual.** If the trade goes the same way with the rule
   applied, log the counterfactual and write nothing.

## The duplication problem

A brain that holds the same lesson forty times has not learned it forty times.
It has one lesson and a counting bug — and every duplicate pulls on decisions
as though it were independent evidence, so the brain becomes progressively more
confident about whatever it happened to write down most often.

The symptom is visible the moment you look at a brain map: you open three
spheres in one cluster and they all say the same thing in different words.

[`fingerprint.py`](fingerprint.py) catches it two ways, because one is not
enough:

```bash
# exact + near-duplicate + calibration audit, exits 1 if anything is wrong
python fingerprint.py --check example.lessons.jsonl

# what does one rule normalise to?
python fingerprint.py "reject when the funding root resolves to one wallet"

# how close are two rules?
python fingerprint.py --compare "rule one here" "rule two here"
```

**`fingerprint`** is a hash of the normalised rule. Normalisation lowercases,
strips punctuation and stopwords, stems, maps domain synonyms onto one spelling
(`single`→`one`, `refuse`/`block`→`reject`, `wallet`/`address`→`acct`), and
buckets numbers by order of magnitude. So these two collide exactly:

```
Reject when the funding root resolves to one wallet across more than 6 holders.
Reject when the funding root resolves to a single wallet across more than 7 holders.
```

**`similarity`** is Jaccard overlap of the token sets, because the case that
actually happens is the same rule rewritten weeks later in different words, and
no hash will ever catch that — hashing is designed to make near-misses look
nothing alike. Default threshold is `0.55`, tuned on reworded pairs.

**Calibration conflicts** are reported separately and are *not* merged. When two
rules are the same sentence at different orders of magnitude — 6 holders versus
600 holders — that is not a duplicate. It is one rule calibrated two ways, and
one of them is wrong. Merging them silently averages a disagreement you should
be looking at.

```
CONFLICT b-0012 / b-0044  — same rule, different magnitude. Do not merge;
                            one of these is miscalibrated
```

### What it will not catch

Bag-of-words similarity misses paraphrases that share no vocabulary. "Size down
when the exit is owned by someone else" and "halve on high concentration" are
the same lesson and score near zero. If you want those caught you need
embeddings, which this repository deliberately does not require — the point is
a check you can run in CI with no API key and no model.

Run it in CI. A brain nobody audits drifts back into duplicates within weeks.

## Loading your own

The loader that reads this file into the agents' context is not in this
repository yet. See the status note in the [root README](../README.md).

The format is stable and documented, so a file you write today will load when
it lands.
