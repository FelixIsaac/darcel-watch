"""Key normalisation, shared by the verifier and both graph backends.

These functions decide identity: two listings are "the same phone" or "the same
address" iff these return the same string. That makes them the one thing that
must not be reimplemented - a second copy that drifts does not crash, it just
quietly stops seeing contradictions (which is exactly what happened: graph.py
kept an extension-naive copy, so the same switchboard landed on two different
phone nodes and the shared-line contradiction became invisible).

Stdlib only, no project imports, so graph.py stays dependency-free.
"""

import re

EXT_RE = re.compile(r"(?i)\b(?:ext\.?|extension|x)\b.*$")
NON_DIGIT_RE = re.compile(r"\D+")


def phone_digits(p):
    """Digits of the dialable part, with any trailing extension dropped.

    v2 renders extensions inline - "(510) 654-4000 ext. 105" - so naively
    stripping non-digits yields 5106544000105 and taking the last 10 gives
    6544000105, a number that belongs to nobody. Cut at the ext marker first.
    """
    return NON_DIGIT_RE.sub("", EXT_RE.sub("", p or ""))


def norm_phone(p):
    """Last 10 digits - area code + number, ignores formatting/country code."""
    d = phone_digits(p)
    return d[-10:] if len(d) >= 10 else None


def norm_addr(a):
    """Street + city + postcode, lowercased and stripped of punctuation."""
    if not a:
        return None
    s = " ".join(str(a.get(k) or "") for k in ("address_1", "city", "postal_code"))
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip() or None
