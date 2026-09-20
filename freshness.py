"""Is this listing still true?

"Broken" and "stale" are different questions and need different machinery.

  BROKEN  - provably wrong right now, from the stored value alone.
            A phone row with no number. Hours that close before they open.
            Free to detect, certain, and it can be fixed today.

  STALE   - may well be fine, but nobody has confirmed it in a long time.
            Not an error. An expiry.

This module answers the second one. Every field carries an implicit promise
with a shelf life: a phone number stays true for years, opening hours for
months. We score each field by what kind of evidence last supported it and how
long ago, then roll that up per listing and across the directory.

The distinction that matters most, and the one easy to get wrong: a structural
check proves a value is WELL-FORMED, never that it is CURRENT. "(415) 555-0123"
is a perfectly well-formed number for an organisation that closed in 2019. Only
agreement with the organisation's own source, or a human who checked, resets
the clock.
"""

import datetime as dt

NOW = dt.datetime.now(dt.timezone.utc)

# How long a field stays believable without re-confirmation. Half-life in days:
# after this long, confidence in the value has halved.
HALF_LIFE = {
    "phone": 1095,     # 3y  - orgs keep numbers for a long time
    "address": 1095,   # 3y  - moves are rare but consequential
    "website": 730,    # 2y
    "email": 730,      # 2y
    "schedule": 180,   # 6mo - hours change with seasons, funding, staffing
    "services": 365,   # 1y  - programmes start and end
    "status": 365,     # 1y  - is this organisation still operating
}

# How much a person depends on the field being right. An address that is wrong
# strands someone at a door; a stale email wastes an afternoon.
WEIGHT = {
    "phone": 0.25,
    "address": 0.20,
    "schedule": 0.25,
    "status": 0.15,
    "website": 0.08,
    "services": 0.05,
    "email": 0.02,
}

# What the evidence is worth. A structural pass cannot exceed its ceiling no
# matter how recent it is - being well-formed is not being true.
EVIDENCE_CEILING = {
    "human_verified": 100,   # someone checked it against reality
    "source_agreement": 90,  # the organisation's own site says the same thing
    "structural_ok": 40,     # well-formed. Says nothing about currency.
    "none": 0,
}


def age_days(ts):
    if not ts:
        return None
    try:
        return max(0, (NOW - dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).days)
    except (ValueError, TypeError):
        return None


def decay(age, half_life):
    """Confidence remaining after `age` days. 1.0 at zero, 0.5 at one half-life."""
    if age is None:
        return 0.0
    return 0.5 ** (age / half_life)


def field_freshness(field, evidence, age):
    """0-100 for one field, given the best evidence we have and its age."""
    ceiling = EVIDENCE_CEILING.get(evidence, 0)
    if not ceiling:
        return 0.0
    return round(ceiling * decay(age, HALF_LIFE.get(field, 365)), 1)


def best_evidence(record, field, confirmations=None):
    """The strongest evidence supporting this field, and how old it is.

    confirmations: {field: iso_timestamp} from agent runs where the live source
    agreed with the stored value - a "match" verdict is a real confirmation and
    should count, not be thrown away because it wasn't interesting.
    """
    human = record.get("verified_at") or record.get("certified_at")
    ha = age_days(human)
    if ha is not None:
        return "human_verified", ha

    conf = (confirmations or {}).get(field)
    ca = age_days(conf)
    if ca is not None:
        return "source_agreement", ca

    # Nothing confirmed it. If the value is at least present and well-formed,
    # it earns the structural ceiling, aged from whenever the record last moved.
    if has_value(record, field):
        return "structural_ok", age_days(record.get("updated_at"))
    return "none", None


def has_value(record, field):
    if field == "phone":
        return any((p.get("number") or "").strip() for p in record.get("phones") or [])
    if field == "address":
        return bool(record.get("addresses"))
    if field == "schedule":
        return bool((record.get("schedule") or {}).get("schedule_days"))
    if field == "services":
        return bool(record.get("services"))
    if field == "status":
        return bool(record.get("status"))
    return bool((record.get(field) or "").strip())


def score(record, confirmations=None):
    """Freshness for one listing: 0-100, plus the per-field breakdown."""
    fields, total, weight_used = {}, 0.0, 0.0
    for field, w in WEIGHT.items():
        evidence, age = best_evidence(record, field, confirmations)
        f = field_freshness(field, evidence, age)
        fields[field] = {
            "score": f,
            "evidence": evidence,
            "age_days": age,
            "half_life": HALF_LIFE.get(field),
        }
        # Absent fields don't drag the average down - you can't be stale about
        # something you never claimed. They show up in coverage instead.
        if evidence != "none":
            total += f * w
            weight_used += w
    overall = round(total / weight_used, 1) if weight_used else 0.0
    return {
        "resource_id": record.get("id"),
        "name": record.get("name"),
        "freshness": overall,
        "band": band(overall),
        "coverage": round(weight_used, 2),
        "fields": fields,
    }


def band(s):
    if s >= 70:
        return "fresh"
    if s >= 40:
        return "aging"
    if s >= 15:
        return "stale"
    return "expired"


def what_would_help(scored):
    """The single cheapest action that would most raise this listing's score.

    A queue of 800 listings is useless. A queue of 800 listings each with one
    named next action is a work plan.
    """
    best, gain = None, 0.0
    for field, f in scored["fields"].items():
        if f["evidence"] == "none":
            continue
        headroom = (EVIDENCE_CEILING["source_agreement"] - f["score"]) * WEIGHT[field]
        if headroom > gain:
            best, gain = field, headroom
    if not best:
        return None
    return {
        "field": best,
        "action": f"confirm {best} against the organisation's own source",
        "score_gain": round(gain, 1),
    }


def directory_report(records, confirmations_by_id=None):
    """Roll the whole directory up. This is the number that should move."""
    confirmations_by_id = confirmations_by_id or {}
    scored = [score(r, confirmations_by_id.get(r.get("id"))) for r in records]
    scored.sort(key=lambda s: s["freshness"])
    bands = {}
    for s in scored:
        bands[s["band"]] = bands.get(s["band"], 0) + 1
    n = len(scored) or 1
    return {
        "generated_at": NOW.isoformat(),
        "listings": len(scored),
        "median_freshness": sorted(s["freshness"] for s in scored)[len(scored) // 2] if scored else 0,
        "mean_freshness": round(sum(s["freshness"] for s in scored) / n, 1),
        "bands": bands,
        "band_pct": {k: round(100 * v / n, 1) for k, v in bands.items()},
        "worst": [
            {**s, "next_action": what_would_help(s)} for s in scored[:25]
        ],
        "half_lives": HALF_LIFE,
        "weights": WEIGHT,
    }


def load_records(path="data_v2"):
    import glob
    import json
    import pathlib
    out = []
    for f in glob.glob(str(pathlib.Path(__file__).parent / path / "*.json")):
        try:
            r = json.loads(open(f).read())
        except (OSError, ValueError):
            continue
        r = r.get("resource", r)
        if isinstance(r, dict) and r.get("status") == "approved":
            out.append(r)
    return out


if __name__ == "__main__":
    import json
    import pathlib

    out = pathlib.Path(__file__).parent / "out"
    out.mkdir(exist_ok=True)

    records = load_records()
    # A "match" verdict from a run IS a confirmation against the source - the
    # most common outcome, and previously thrown away because it wasn't a
    # finding. Feed it back in: confirming a listing is real work.
    confirmations = {}
    try:
        res = json.loads((out / "results.json").read_text())
        for v in (res.get("abstained") or []) + (res.get("queue") or []):
            pass
        for v in res.get("confirmed_matches", []):
            confirmations[v["resource_id"]] = {
                f: res.get("generated_at") for f in v.get("fields", [])
            }
    except (OSError, ValueError, KeyError):
        pass

    report = directory_report(records, confirmations)
    (out / "freshness.json").write_text(json.dumps(report, indent=1))
    print(f"  {report['listings']} listings | median freshness {report['median_freshness']}/100")
    for b in ("fresh", "aging", "stale", "expired"):
        print(f"    {b:8} {report['bands'].get(b, 0):4}  ({report['band_pct'].get(b, 0):4.1f}%)")
    print(f"  -> {out / 'freshness.json'}")
