"""Measure the judgment layer against a mechanical oracle, then set thresholds.

WHAT THIS MEASURES, STATED PRECISELY. The oracle is "the stored value appears,
in normalised form, in the text of the page we fetched". That is a fact about
the page, checkable by string comparison, with no human or model judgment in
it. So this calibrates FAITHFULNESS - does the judge read the page correctly -
and not FACTUALITY, which would be whether the directory is right about the
world.

That distinction matters and must not be smuggled over. A phone number can be
perfectly correct and simply not published on the organisation's website; the
oracle calls that "not supported", and so should the judge. Tier 4's actual
question is "does this page support the stored value", which is exactly what
the oracle answers. What this cannot tell you is how often a contradiction
found here corresponds to a genuinely wrong listing. That needs human review
of the findings, and it is not what this file does.

WHY A MECHANICAL ORACLE AT ALL. The alternative is labelling by hand. Every
label would then come from the same author as the thresholds being tuned,
which is how "0 of 80 false positives" became a number that could not be
defended - it was fitted to the data it was measured on. A string comparison
cannot flatter itself.

HOLD-OUT. Claims are split by ORGANISATION, not by claim. Splitting by claim
would put two phone numbers from one nonprofit on both sides of the split and
leak page text across it. Thresholds are chosen on the calibration half and
reported on the test half, which is the only number worth quoting.

    .venv/bin/python calibrate.py --orgs 40
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import re
import statistics
import time

import audit
import discover
import envfile
import jev

OUT = pathlib.Path(__file__).parent / "out" / "calibration.json"

# Which claim kinds have a meaningful mechanical oracle. "status" ("the
# organisation is still operating") has none - absence of a closure notice is
# not a string you can grep for - so it is excluded rather than guessed at.
ORACLE_FIELDS = ("phone", "address")


# --------------------------------------------------------------------------
# the oracle
# --------------------------------------------------------------------------

def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def phone_on_page(stored: str, page: str) -> bool:
    """Is this phone number on the page, ignoring formatting?

    Compares the last 10 digits, so "(415) 733-0990", "415.733.0990" and
    "+1 415 733 0990" all match. Requires 10 to avoid a short extension
    matching by luck.
    """
    d = _digits(stored)
    if len(d) < 10:
        return False
    target = d[-10:]
    return target in _digits(page)


def address_on_page(stored: str, page: str) -> bool | None:
    """Is this address on the page? Returns None when it cannot be decided.

    Returning None is the point. The first version returned False whenever its
    regex failed, which silently turned "cannot check this" into "the page
    does not contain it". Every disagreement in the first calibration run - all
    8 of them - was this bug, not a model error:

        "450 SUTTER ST 2522"   stripping "ST" left "sutter  2522", while the
                               page reads "450 Sutter St, Suite 2522"
        "P.O. Box 23403"       no leading street number, so the regex never
                               matched and the answer was always False
        "California St, ..."   no street number at all

    Grading a judge against a broken ruler produces a number that looks like
    the judge's error rate and is not. Cases this cannot decide are now
    EXCLUDED from the dataset rather than labelled.
    """
    head = re.split(r"[,\n]", stored.strip())[0].strip()

    box = re.search(r"\bbox\s*#?\s*(\d+)", head, re.I)
    if box:
        return box.group(1) in page

    m = re.match(r"(\d+)\s+(.+)", head)
    if not m:
        return None                      # no street number - cannot decide
    num, rest = m.group(1), m.group(2)

    # Distinctive words = everything that is not a street type or unit marker.
    stop = {"st", "street", "ave", "avenue", "rd", "road", "blvd", "boulevard",
            "dr", "drive", "ln", "lane", "way", "ct", "court", "pl", "place",
            "suite", "ste", "unit", "apt", "floor", "fl", "north", "south",
            "east", "west"}
    words = [w for w in re.findall(r"[a-z]+", rest.lower())
             if w not in stop and len(w) > 2]
    if not words:
        return None                      # nothing distinctive to look for

    low = page.lower()
    return num in page and any(w in low for w in words)


def oracle(field: str, stored: str, page: str) -> bool | None:
    if field == "phone":
        return phone_on_page(stored, page)
    if field == "address":
        return address_on_page(stored, page)
    return None


# --------------------------------------------------------------------------
# dataset
# --------------------------------------------------------------------------

def collect(records, max_pages=3) -> list[dict]:
    """One row per (claim, page) pair - the unit the judge actually scores."""
    rows: list[dict] = []
    for rec in records:
        website = (rec.get("website") or "").strip()
        if not website:
            continue
        claims = [c for c in audit.extract_claims(rec) if c.field in ORACLE_FIELDS]
        if not claims:
            continue
        try:
            inv = discover.discover(website)
        except Exception:
            continue
        if not len(inv) or inv.method == "robots-denied":
            continue

        targets = audit.select_pages(inv, claims, budget=max_pages)
        for url, lastmod in targets:
            text, method = audit.page_text(url)
            if len(text) < 200:
                continue
            # Ask BOTH forms against the same page in one request: the two
            # independent nouls this project started with, and TypeSafe's
            # documented single-Choice citation-check pattern. Scoring them on
            # identical evidence is the only way to choose between them on
            # measurement rather than taste.
            qs: dict = {}
            for c in claims:
                qs[f"sup__{c.key}"] = jev.supports(c.text)
                qs[f"con__{c.key}"] = jev.contradicts(c.text)
                qs[f"rel__{c.key}"] = jev.relates(c.text)
            try:
                answers, _ = jev.ask(text[:jev.MAX_STATE_CHARS], qs)
            except jev.JevError:
                continue

            for c in claims:
                truth = oracle(c.field, c.stored, text)
                if truth is None:
                    continue          # oracle cannot decide - excluded, not guessed
                s = jev.noul(answers, f"sup__{c.key}")
                k = jev.noul(answers, f"con__{c.key}")
                pick, conf = jev.choice(answers, f"rel__{c.key}")
                if s is None or k is None or pick is None:
                    continue
                probs = (answers.get(f"rel__{c.key}") or {}).get("probabilities") or {}
                rows.append({
                    "org_id": rec.get("id"),
                    "org": (rec.get("name") or "").strip(),
                    "field": c.field,
                    "stored": c.stored,
                    "url": url,
                    "via": method,
                    # two-noul form
                    "support": s,
                    "contradict": k,
                    # single-choice form (TypeSafe citation-check pattern)
                    "choice": pick,
                    "choice_confidence": conf,
                    "p_supports": probs.get("supports"),
                    "p_contradicts": probs.get("contradicts"),
                    "p_says_nothing": probs.get("says_nothing"),
                    "oracle_supported": truth,
                })
    return rows


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def auroc(scores: list[float], labels: list[bool]) -> float | None:
    """Probability a positive outranks a negative. Ties count a half.

    Written out rather than imported so this file has no dependencies beyond
    the standard library - the rest of the pipeline runs that way too.
    """
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def ece(scores: list[float], labels: list[bool], bins: int = 10) -> float | None:
    """Expected calibration error: |predicted - observed|, weighted by bin size.

    The vendor's claim is that a 0.9 means right about 90% of the time. This is
    the number that checks it on our data.
    """
    if not scores:
        return None
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, s in enumerate(scores) if (lo <= s < hi) or (b == bins - 1 and s == 1.0)]
        if not idx:
            continue
        conf = statistics.fmean(scores[i] for i in idx)
        acc = statistics.fmean(1.0 if labels[i] else 0.0 for i in idx)
        total += len(idx) / len(scores) * abs(conf - acc)
    return total


def at_threshold(scores, labels, t):
    tp = sum(1 for s, y in zip(scores, labels) if s >= t and y)
    fp = sum(1 for s, y in zip(scores, labels) if s >= t and not y)
    fn = sum(1 for s, y in zip(scores, labels) if s < t and y)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    return {"threshold": round(t, 3), "tp": tp, "fp": fp, "fn": fn,
            "precision": prec, "recall": rec}


def choose_threshold(scores, labels, target_precision=0.95):
    """Among thresholds meeting the precision target, pick the one with the
    HIGHEST precision, breaking ties toward recall.

    An earlier version maximised recall subject to the precision target, and it
    overfitted exactly as you would expect: it chose 0.19, scored 0.958
    precision on the calibration half, and fell to 0.932 held out - below the
    target it was selected to meet. A conservative 0.80 scored 1.000 precision
    held out and gave up only 5 points of recall.

    Precision-first is the right objective here because the two errors are not
    symmetric. A missed confirmation costs a listing some freshness score. A
    false one tells a volunteer that a nonprofit's working phone number is
    wrong.
    """
    cands = []
    for t in [i / 100 for i in range(0, 101)]:
        m = at_threshold(scores, labels, t)
        if m["precision"] is not None and m["precision"] >= target_precision and m["tp"] > 0:
            cands.append(m)
    if not cands:
        return None

    # Many thresholds usually tie at the best precision, because the score
    # distribution is bimodal - almost everything lands near 0 or near 1. Any
    # point on that plateau is equally good on the calibration half, but the
    # EDGES are where it stops being good on unseen data. Take the middle.
    best_p = max(m["precision"] for m in cands)
    plateau = sorted((m for m in cands if m["precision"] >= best_p - 1e-9),
                     key=lambda m: m["threshold"])
    return plateau[len(plateau) // 2]


def split_by_org(rows, seed=17):
    orgs = sorted({r["org_id"] for r in rows})
    rnd = random.Random(seed)
    rnd.shuffle(orgs)
    half = set(orgs[: len(orgs) // 2])
    cal = [r for r in rows if r["org_id"] in half]
    test = [r for r in rows if r["org_id"] not in half]
    return cal, test


SCORERS = {
    # name            -> how to read a "probability the page supports this"
    "two_noul": lambda r: r["support"],
    "choice": lambda r: r.get("p_supports"),
}


def report(rows: list[dict], target_precision: float = 0.95) -> dict:
    cal, test = split_by_org(rows)
    out: dict = {
        "rows": len(rows),
        "orgs": len({r["org_id"] for r in rows}),
        "positives": sum(1 for r in rows if r["oracle_supported"]),
        "calibration_rows": len(cal),
        "test_rows": len(test),
        "by_field": {},
        "scorers": {},
    }
    for f in sorted({r["field"] for r in rows}):
        sub = [r for r in rows if r["field"] == f]
        out["by_field"][f] = {"rows": len(sub),
                              "positives": sum(1 for r in sub if r["oracle_supported"])}
    if not cal or not test:
        out["error"] = "not enough organisations to split"
        return out

    for name, get in SCORERS.items():
        c = [(get(r), r["oracle_supported"]) for r in cal if get(r) is not None]
        t = [(get(r), r["oracle_supported"]) for r in test if get(r) is not None]
        if not c or not t:
            continue
        cs, cl = [x[0] for x in c], [x[1] for x in c]
        ts, tl = [x[0] for x in t], [x[1] for x in t]
        entry = {
            "rows_scored": len(c) + len(t),
            "auroc_test": auroc(ts, tl),
            "ece_test": ece(ts, tl),
            "chosen_on_calibration": choose_threshold(cs, cl, target_precision),
        }
        if entry["chosen_on_calibration"]:
            h = at_threshold(ts, tl, entry["chosen_on_calibration"]["threshold"])
            h["target_precision"] = target_precision
            entry["held_out"] = h
        # Best precision achievable at all, for honesty about the ceiling.
        entry["ceiling_test"] = max(
            (at_threshold(ts, tl, t_) for t_ in [i / 100 for i in range(50, 100)]),
            key=lambda m: (m["precision"] or 0, m["recall"] or 0))
        out["scorers"][name] = entry

    # The accusation path: how often does a high "contradicts" signal land on a
    # claim the oracle says IS on the page? Those would be false accusations.
    for name, key in (("two_noul", "contradict"), ("choice", "p_contradicts")):
        vals = [(r[key], r["oracle_supported"]) for r in test
                if r.get(key) is not None]
        if not vals:
            continue
        for thr in (0.9, 0.95, 0.99):
            flagged = [y for v, y in vals if v >= thr]
            out["scorers"].setdefault(name, {}).setdefault("false_accusations", {})[thr] = {
                "flagged": len(flagged),
                "of_which_actually_on_page": sum(1 for y in flagged if y),
            }
    out["current_threshold"] = at_threshold(
        [r["support"] for r in test], [r["oracle_supported"] for r in test],
        jev.SUPPORT_THRESHOLD)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orgs", type=int, default=40)
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--target-precision", type=float, default=0.95)
    ap.add_argument("--reuse", action="store_true", help="re-score the saved dataset")
    args = ap.parse_args()

    envfile.load()
    if args.reuse and OUT.exists():
        rows = json.loads(OUT.read_text())["rows"]
        print(f"re-scoring {len(rows)} saved rows\n")
    else:
        if not jev.available():
            print("JEV_API_KEY not set")
            raise SystemExit(1)
        recs = [r for r in audit._load()
                if r.get("status") == "approved" and r.get("website")
                and ((r.get("phones") or []) or (r.get("addresses") or []))]
        random.Random(args.seed).shuffle(recs)
        sample = recs[: args.orgs]
        print(f"collecting from {len(sample)} organisations "
              f"(<= {args.pages} pages each)...")
        t0 = time.time()
        rows = collect(sample, max_pages=args.pages)
        print(f"  {len(rows)} (claim, page) rows in {time.time() - t0:.0f}s\n")
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(json.dumps({"generated_at": time.time(), "rows": rows}, indent=1))

    r = report(rows, args.target_precision)
    print(f"dataset : {r['rows']} decidable rows over {r['orgs']} orgs "
          f"({r['positives']} supported by the oracle)")
    for f, v in r["by_field"].items():
        print(f"          {f:8} {v['rows']:4} rows, {v['positives']:4} positive")
    if "error" in r:
        print("  " + r["error"])
        return
    print(f"split   : {r['calibration_rows']} calibration / {r['test_rows']} test, "
          f"disjoint organisations\n")

    for name, e in r["scorers"].items():
        print(f"=== {name} " + "=" * (58 - len(name)))
        if "auroc_test" in e:
            print(f"  AUROC (test) {e['auroc_test']:.3f}    "
                  f"ECE (test) {e['ece_test']:.3f}")
        ch = e.get("chosen_on_calibration")
        if ch and e.get("held_out"):
            h = e["held_out"]
            pr = "n/a" if h["precision"] is None else f"{h['precision']:.3f}"
            rc = "n/a" if h["recall"] is None else f"{h['recall']:.3f}"
            print(f"  threshold {ch['threshold']} chosen on calibration "
                  f"(precision {ch['precision']:.3f} there)")
            print(f"  HELD OUT -> precision {pr}  recall {rc}  "
                  f"(tp {h['tp']}, fp {h['fp']}, fn {h['fn']})")
        else:
            cl = e.get("ceiling_test") or {}
            pr = "n/a" if cl.get("precision") is None else f"{cl['precision']:.3f}"
            print(f"  no threshold reaches {args.target_precision:.0%} precision "
                  f"on the calibration half")
            print(f"  best precision available on test: {pr} at {cl.get('threshold')}")
        fa = e.get("false_accusations")
        if fa:
            print("  false-accusation check (high 'contradicts' on a claim the "
                  "oracle says IS on the page):")
            for thr, v in sorted(fa.items()):
                print(f"    >= {thr}: flagged {v['flagged']:3}, "
                      f"of which actually on page {v['of_which_actually_on_page']}")
        print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
