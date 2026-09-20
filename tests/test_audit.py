"""Tests for claim extraction, verdict classification and reconciliation.

No network and no API key: these drive the decision logic, which is where the
damage happens. A model being uncertain is survivable. Code that turns silence
into an accusation is not.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import audit  # noqa: E402
import jev  # noqa: E402


# --------------------------------------------------------------------------
# claim extraction
# --------------------------------------------------------------------------

def test_phone_claims_skip_empty_numbers():
    """Building Futures stores its number in the LABEL with `number` empty.
    There is nothing to verify against the live site, so no claim is made -
    that defect is caught structurally instead."""
    rec = {"id": 1, "phones": [{"number": "", "service_type": "Voice"},
                               {"number": "510-555-0100", "service_type": "Voice"}]}
    claims = audit._phone_claims(rec)
    assert len(claims) == 1
    assert claims[0].stored == "510-555-0100"


def test_phone_claims_cover_every_number_not_just_the_first():
    """REGRESSION: comparing only the first of ten stored numbers produced the
    Meals on Wheels false positive."""
    rec = {"id": 1, "phones": [{"number": f"510-555-010{i}"} for i in range(10)]}
    assert len(audit._phone_claims(rec)) == 10


def test_phone_claim_labels_non_voice_lines():
    rec = {"id": 1, "phones": [{"number": "510-555-0100", "service_type": "TTY"}]}
    assert "(TTY)" in audit._phone_claims(rec)[0].text


def test_address_claim_joins_parts_and_skips_blanks():
    rec = {"id": 1, "addresses": [{"address_1": "433 Hegenberger Rd.", "city": "Oakland",
                                   "state_province": "CA", "postal_code": "94621"},
                                  {"address_1": "", "city": ""}]}
    claims = audit._address_claims(rec)
    assert len(claims) == 1
    assert claims[0].stored == "433 Hegenberger Rd., Oakland, CA, 94621"


def test_extract_claims_always_includes_status():
    assert any(c.field == "status" for c in audit.extract_claims({"id": 1}))


def test_extract_claims_makes_no_model_call(monkeypatch):
    """Claim extraction must stay deterministic. If this ever starts calling a
    model, cost becomes proportional to corpus size for no benefit."""
    def boom(*a, **k):
        raise AssertionError("extract_claims must not call the judgment model")
    monkeypatch.setattr(jev, "ask", boom)
    audit.extract_claims({"id": 1, "phones": [{"number": "510-555-0100"}]})


# --------------------------------------------------------------------------
# classification - absence vs contradiction
# --------------------------------------------------------------------------

def test_silence_is_absent_not_contradicted():
    """THE load-bearing rule. A page that never mentions a phone number scores
    low on both questions. That is ABSENT. Calling it a contradiction is how
    every retracted finding in this project was born."""
    assert jev.classify(support=0.02, contradict=0.01) == "absent"


def test_active_disagreement_is_contradicted():
    assert jev.classify(support=0.0, contradict=jev.CONTRADICT_THRESHOLD) == "contradicted"


def test_agreement_is_supported():
    assert jev.classify(support=jev.SUPPORT_THRESHOLD, contradict=0.03) == "supported"


def test_middling_evidence_is_uncertain_not_a_finding():
    assert jev.classify(support=0.55, contradict=0.40) == "uncertain"


def test_evidence_just_below_the_contradiction_bar_is_not_a_finding():
    """The bar is measured, and just under it must stay silent. Community
    Forward SF sat at 0.94 under an earlier hand-picked bar of 0.95 and was
    suppressed - that case turned out to be real, which is exactly why the
    threshold is now set by calibrate.py instead of by taste."""
    v = jev.Verdict("c", 0.0, jev.CONTRADICT_THRESHOLD - 0.01,
                    jev.classify(0.0, jev.CONTRADICT_THRESHOLD - 0.01))
    assert not v.actionable


def test_only_contradiction_is_actionable():
    for label, sup, con in (("absent", 0.02, 0.01), ("uncertain", 0.5, 0.4),
                            ("supported", jev.SUPPORT_THRESHOLD, 0.02)):
        v = jev.Verdict("c", sup, con, jev.classify(sup, con))
        assert v.label == label
        assert not v.actionable
    v = jev.Verdict("c", 0.0, jev.CONTRADICT_THRESHOLD,
                    jev.classify(0.0, jev.CONTRADICT_THRESHOLD))
    assert v.actionable


def test_thresholds_are_asymmetric():
    """Contradicting a stored value must need more evidence than confirming
    one: confirmation preserves the status quo, contradiction can send a
    volunteer to replace a working phone number with a broken one."""
    assert jev.CONTRADICT_THRESHOLD > jev.SUPPORT_THRESHOLD


def test_thresholds_came_from_calibration():
    """Guards against someone quietly reverting to hand-picked round numbers.
    Both values are plateau midpoints from calibrate.py, so neither should be
    a suspiciously tidy 0.5/0.8/0.9."""
    assert jev.SUPPORT_THRESHOLD not in (0.5, 0.8, 0.85, 0.9)
    assert 0.0 < jev.SUPPORT_THRESHOLD < jev.CONTRADICT_THRESHOLD <= 1.0


# --------------------------------------------------------------------------
# reconciliation across pages
# --------------------------------------------------------------------------

def _v(label):
    table = {"supported": (0.97, 0.02), "contradicted": (0.02, 0.97),
             "absent": (0.02, 0.02), "uncertain": (0.5, 0.4)}
    s, c = table[label]
    return jev.Verdict("claim", s, c, label)


def test_one_supporting_page_beats_two_silent_ones():
    """REGRESSION, Building Futures: 510-808-7410 is absent from the homepage
    and from /services/domestic-violence/, and present on /get-help/. The
    number is correct. Majority voting over pages would have filed it as an
    error."""
    out = audit.reconcile([{"p": _v("absent")}, {"p": _v("absent")}, {"p": _v("supported")}])
    assert out["p"].label == "supported"


def test_support_outranks_contradiction():
    out = audit.reconcile([{"p": _v("contradicted")}, {"p": _v("supported")}])
    assert out["p"].label == "supported"


def test_contradiction_stands_when_nothing_supports():
    out = audit.reconcile([{"p": _v("absent")}, {"p": _v("contradicted")}])
    assert out["p"].label == "contradicted"


def test_reconcile_handles_no_pages():
    assert audit.reconcile([]) == {}


def test_reconcile_keeps_strongest_support():
    a = jev.Verdict("c", 0.91, 0.02, "supported")
    b = jev.Verdict("c", 0.99, 0.01, "supported")
    assert audit.reconcile([{"p": a}, {"p": b}])["p"].support == 0.99


# --------------------------------------------------------------------------
# failure modes must abstain, never assert
# --------------------------------------------------------------------------

def test_no_website_abstains(monkeypatch):
    monkeypatch.setattr(jev, "available", lambda: True)
    a = audit.audit_org({"id": 1, "name": "X", "website": ""})
    assert a.findings == [] and "no website" in a.note


def test_missing_api_key_abstains(monkeypatch):
    monkeypatch.setattr(jev, "available", lambda: False)
    a = audit.audit_org({"id": 1, "name": "X", "website": "https://x.test/"})
    assert a.findings == [] and "abstaining" in a.note


def test_robots_denied_is_honoured(monkeypatch):
    monkeypatch.setattr(jev, "available", lambda: True)
    monkeypatch.setattr(audit.discover, "discover", lambda w: audit.discover.Inventory(
        w, "https://x.test", [], "robots-denied", audit.discover.Robots(), None))
    a = audit.audit_org({"id": 1, "name": "X", "website": "https://x.test/",
                         "phones": [{"number": "510-555-0100"}]})
    assert a.findings == [] and "robots.txt" in a.note
