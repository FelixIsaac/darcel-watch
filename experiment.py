"""Which way of ASKING Jev works best, measured on our own data.

The three primitives (Noul, Choice, Score) and the shape of the request are
design choices, and the documentation gives guidance rather than answers for a
specific corpus. This file turns the guidance into an experiment.

Every variant is scored against the SAME rows and the SAME mechanical oracle
produced by calibrate.py, so the only thing that differs is the formulation.
Page text is re-fetched through fetcher.py, which caches, so repeated runs cost
nothing extra in bandwidth.

Variants, and the documented claim each one tests:

  baseline          string state, claim in the instructions, prose criteria.
                    What this project shipped.

  structured_state  state as a JSON object with named fields. The docs say
                    "use an object for most requests" and that System One
                    models are "trained to understand structure".

  structured_all    structured state AND structured criteria, per
                    primitives/advanced.md.

  score             the Score primitive over four ORDERED levels
                    contradicts < says_nothing < partially < supports.
                    Hypothesis: evidence strength is a spectrum, not a
                    category, and Score is the primitive for spectrums.

  noul_pair         the original two independent nouls, kept as the control
                    so the comparison stays honest across reruns.

Reported per variant: AUROC and ECE against the oracle, precision/recall at
the operating threshold, and - the one that actually matters - how many
contradictions it raises and how many of those are on claims the oracle says
ARE on the page.

    .venv/bin/python experiment.py --limit 200
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import pathlib

import calibrate
import envfile
import fetcher
import jev

DATA = pathlib.Path(__file__).parent / "out" / "calibration.json"
OUT = pathlib.Path(__file__).parent / "out" / "experiment.json"

PAGE_CHARS = 60_000

# Ordered, lowest evidence to highest. Score returns a position that may fall
# between levels, which is exactly what "evidence strength" wants to be.
SCORE_LEVELS = [
    "The page states the opposite of the claim, or gives a different value for "
    "the same thing.",
    "The page does not address the claim at all. It simply does not mention it.",
    "The page mentions the subject of the claim but does not confirm the "
    "specific value.",
    "The page states the claim, or directly implies it is true.",
]


# --------------------------------------------------------------------------
# the variants
# --------------------------------------------------------------------------

def _state_string(page: str, org: str, url: str) -> str:
    return page[:PAGE_CHARS]


def _state_object(page: str, org: str, url: str) -> dict:
    return {
        "organisation": org,
        "page_url": url,
        "page_text": page[:PAGE_CHARS],
    }


def q_baseline(claim: str) -> dict:
    return jev.relates(claim)


def q_structured_criteria(claim: str) -> dict:
    """Criteria as JSON objects rather than prose sentences."""
    return {
        "type": "choice",
        "instructions": {
            "task": "Decide how the page in the state relates to the claim.",
            "claim": claim,
            "note": "The page is published by the organisation itself. The claim "
                    "comes from a third-party directory listing about it.",
        },
        "criteria": {
            "supports": {
                "what": "The page states the claim or directly implies it is true.",
                "examples": ["the same phone number appears",
                             "the same street address appears"],
            },
            "contradicts": {
                "what": "The page gives a different value for the same thing, or "
                        "states the claim is false.",
                "not_for": "The page simply not mentioning the subject.",
            },
            "says_nothing": {
                "what": "The page does not address what the claim asserts.",
                "examples": ["no phone number anywhere on the page",
                             "the page is about an unrelated programme"],
            },
        },
    }


def q_score(claim: str) -> dict:
    return {
        "type": "score",
        "instructions": {
            "task": "Rate how strongly the page in the state supports the claim.",
            "claim": claim,
        },
        "criteria": SCORE_LEVELS,
    }


VARIANTS = {
    # name              (state builder,  question builder, reader)
    "baseline":         (_state_string, q_baseline, "choice"),
    "structured_state": (_state_object, q_baseline, "choice"),
    "structured_all":   (_state_object, q_structured_criteria, "choice"),
    "score":            (_state_object, q_score, "score"),
    "noul_pair":        (_state_string, None, "noul_pair"),
}


def read_support(kind: str, answers: dict, key: str) -> tuple[float, float] | None:
    """-> (p_supports, p_contradicts), normalised to 0..1 for every variant."""
    if kind == "choice":
        a = answers.get(f"q__{key}") or {}
        p = a.get("probabilities") or {}
        if "supports" not in p:
            return None
        return float(p.get("supports") or 0.0), float(p.get("contradicts") or 0.0)
    if kind == "score":
        a = answers.get(f"q__{key}") or {}
        s = a.get("score")
        if s is None:
            return None
        # Levels are 0..3 in the order defined above. Map onto the same axis:
        # support rises toward level 3, contradiction toward level 0.
        n = len(SCORE_LEVELS) - 1
        pos = float(s) / n
        probs = a.get("probabilities") or {}
        contra = float(probs.get("0") or probs.get(SCORE_LEVELS[0]) or 0.0)
        return pos, contra
    if kind == "noul_pair":
        s = jev.noul(answers, f"sup__{key}")
        c = jev.noul(answers, f"con__{key}")
        if s is None or c is None:
            return None
        return float(s), float(c)
    raise ValueError(kind)


def run_variant(name, page_groups, workers=6):
    build_state, build_q, kind = VARIANTS[name]
    out: dict[tuple, tuple[float, float]] = {}
    usage = jev.Usage()

    def one(group):
        url, org, page, claims = group
        if kind == "noul_pair":
            qs = {}
            for key, claim in claims:
                qs[f"sup__{key}"] = jev.supports(claim)
                qs[f"con__{key}"] = jev.contradicts(claim)
        else:
            qs = {f"q__{key}": build_q(claim) for key, claim in claims}
        state = build_state(page, org, url)
        try:
            answers, u = jev.ask(state if isinstance(state, str) else json.dumps(state), qs)
        except jev.JevError:
            return {}, jev.Usage()
        res = {}
        for key, _claim in claims:
            v = read_support(kind, answers, key)
            if v:
                res[(url, key)] = v
        return res, u

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for res, u in ex.map(one, page_groups):
            out.update(res)
            usage = usage.add(u)
    return out, usage


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def evaluate(rows, scored, name):
    pairs = []
    for r in rows:
        key = (r["url"], r["field"] + "|" + r["stored"])
        if key in scored:
            s, c = scored[key]
            pairs.append((s, c, r["oracle_supported"], r["org_id"]))
    if not pairs:
        return {"name": name, "error": "no rows scored"}

    cal_ids, test_ids = calibrate.split_by_org(
        [{"org_id": p[3], **{}} for p in pairs])
    cal_set = {p["org_id"] for p in cal_ids}
    cal = [p for p in pairs if p[3] in cal_set]
    test = [p for p in pairs if p[3] not in cal_set]
    if not cal or not test:
        return {"name": name, "error": "split failed"}

    cs, cl = [p[0] for p in cal], [p[2] for p in cal]
    ts, tl = [p[0] for p in test], [p[2] for p in test]

    chosen = calibrate.choose_threshold(cs, cl, 0.95)
    held = calibrate.at_threshold(ts, tl, chosen["threshold"]) if chosen else None

    # The accusation path: high contradiction signal on claims the oracle says
    # ARE on the page. These would be false accusations against a nonprofit.
    accus = {}
    for thr in (0.9, 0.95, 0.99):
        flagged = [p for p in test if p[1] >= thr]
        accus[thr] = {"flagged": len(flagged),
                      "wrong": sum(1 for p in flagged if p[2])}

    return {
        "name": name,
        "rows": len(pairs),
        "auroc": calibrate.auroc(ts, tl),
        "ece": calibrate.ece(ts, tl),
        "threshold": chosen["threshold"] if chosen else None,
        "held_out": held,
        "accusations": accus,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap rows, 0 = all")
    ap.add_argument("--only", nargs="*", help="run only these variants")
    args = ap.parse_args()
    envfile.load()

    rows = json.loads(DATA.read_text())["rows"]
    if args.limit:
        rows = rows[: args.limit]

    # Group by page: one request per page carrying every claim about it. The
    # docs' own benchmark puts batching at ~11.5x cheaper and ~9.6x faster than
    # separate calls, and questions against one state stay independent.
    groups: dict[str, list] = collections.defaultdict(list)
    meta: dict[str, str] = {}
    for r in rows:
        key = r["field"] + "|" + r["stored"]
        claim = (f"The organisation can be reached on the telephone number {r['stored']}."
                 if r["field"] == "phone"
                 else f"The organisation has a location at {r['stored']}.")
        groups[r["url"]].append((key, claim))
        meta[r["url"]] = r["org"]

    page_groups = []
    for url, claims in groups.items():
        text, _ = fetcher.fetch(url)
        if not text:
            continue
        page_groups.append((url, meta[url], text, list(dict.fromkeys(claims))))
    print(f"{len(rows)} rows over {len(page_groups)} fetchable pages\n")

    names = args.only or list(VARIANTS)
    results = []
    for name in names:
        scored, usage = run_variant(name, page_groups)
        res = evaluate(rows, scored, name)
        res["cost_usd"] = round(usage.usd, 5)
        res["calls"] = usage.calls
        results.append(res)

        if "error" in res:
            print(f"{name:18} {res['error']}")
            continue
        h = res["held_out"] or {}
        pr = "n/a" if not h or h.get("precision") is None else f"{h['precision']:.3f}"
        rc = "n/a" if not h or h.get("recall") is None else f"{h['recall']:.3f}"
        print(f"{name:18} AUROC {res['auroc']:.3f}  ECE {res['ece']:.3f}  "
              f"thr {res['threshold']}  P {pr}  R {rc}  ${res['cost_usd']:.4f}")
        a = res["accusations"]
        print(f"{'':18} contradictions  "
              + "  ".join(f">={t}: {v['flagged']}({v['wrong']} wrong)"
                          for t, v in sorted(a.items())))
    OUT.write_text(json.dumps(results, indent=1))
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
