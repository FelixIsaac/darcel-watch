"""Typed, calibrated judgments. A thin client for TypeSafe's Jev.

Why a second model at all, when verify.py already calls Gemini.

The failure that shaped this project was not a model being wrong. It was a
model being wrong AND CONFIDENT. Adjudicating a Sutter Health listing, Gemini
reported a careers-line number as the main switchboard at `confidence: 1.0`.
That number was free text the model chose to emit; nothing trained it to mean
anything. We then had to retract the finding.

Jev is a different shape of thing. It emits no strings at all - you hand it a
state and typed questions, it returns a probability distribution over answers
you defined. Two properties follow, and both matter here:

  1. IT CANNOT FABRICATE A CITATION. If the options are "span 7", "span 12",
     "none of these", it must pick one of those. A generative model asked for
     a supporting quote can invent one; we had to add a check that the quote
     literally appears in the fetched bytes. With a choice over indices we
     control, that whole class of error does not exist.

  2. THE PROBABILITY IS TRAINED TO MEAN SOMETHING. TypeSafe train with RLCD
     (Reinforcement Learning for Calibrated Decisions), targeting answers given
     90% probability being right about 90% of the time.

Two things this module must NOT be read as claiming:

  - "Cannot hallucinate" is a claim about TYPE SAFETY, not correctness. Jev can
    still select a wrong valid option, confidently. It scores ~68% on
    TypeSafe's own four-workflow benchmark, which is mid-tier. That is why
    nothing here is authorised to publish a finding on its own - see
    decide() in verify.py. Jev screens; humans decide.

  - Calibration is a vendor claim with no published independent metrics, and
    TypeSafe's own docs say thresholds must be tested per use case. So
    THRESHOLDS BELOW ARE PROVISIONAL until calibrate.py has been run against a
    hand-labelled holdout. They are wired to be data, not folklore.

Cost, measured: 6 claims about one organisation's page, in a single request,
206ms, $0.000165. Questions run in parallel against one state, so asking
twenty things costs barely more than asking one. That is what makes checking
every qualitative field on 813 listings affordable at all.

Docs: https://docs.typesafe.ai/api
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

# Published input price. Output tokens are free.
USD_PER_INPUT_MTOK = 0.042

# Jev's context window. States larger than this must be trimmed by the caller;
# we refuse rather than silently truncating the evidence out from under a
# judgment, because a claim marked "unsupported" because its page got cut is
# indistinguishable from one that is genuinely unsupported.
MAX_STATE_CHARS = 100_000      # ~32k tokens, conservatively

# MEASURED, not chosen by taste. calibrate.py, 444 (claim, page) rows over 43
# organisations, split by ORGANISATION so no site's text crosses the split.
# Ground truth is a mechanical oracle - does the stored value appear, normalised,
# in the fetched page - so no human or model graded its own work.
#
#   support >= 0.69   held out: precision 1.000, recall 0.867  (tp 65, fp 0)
#   contradict >= 0.99  held out: 12 flagged, 0 of them actually on the page
#
# Deliberately asymmetric, and the asymmetry is the whole point: corroboration
# preserves the status quo, while a contradiction can send a volunteer to
# replace a working phone number with a broken one.
#
# Both numbers sit in the MIDDLE of the plateau of thresholds that tied at best
# precision on the calibration half, not at its edge. Picking the edge - the
# most aggressive threshold that just meets the target - overfitted: it chose
# 0.19, scored 0.958 on calibration and fell to 0.932 held out.
#
# Re-run `.venv/bin/python calibrate.py --orgs 45` after any prompt change.
SUPPORT_THRESHOLD = 0.69
CONTRADICT_THRESHOLD = 0.99

# Retained for the two-noul path, which classify() still uses for the "absent"
# case: both signals low means the page is silent.
REFUTE_THRESHOLD = 0.05

_MAX_OPTIONS = 255             # API limit on a Choice


class JevError(RuntimeError):
    pass


class JevUnavailable(JevError):
    """No key, or the service could not be reached. Callers ABSTAIN on this -
    they must never fall back to a less careful judgment."""


@dataclasses.dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    ms: int = 0

    @property
    def usd(self) -> float:
        return self.input_tokens / 1e6 * USD_PER_INPUT_MTOK

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.calls + other.calls,
            self.ms + other.ms,
        )


def available() -> bool:
    return bool(os.environ.get("JEV_API_KEY"))


def _post(body: dict, timeout: int = 45, retries: int = 2) -> dict:
    key = os.environ.get("JEV_API_KEY")
    if not key:
        raise JevUnavailable("JEV_API_KEY is not set")
    payload = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(ENDPOINT, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:400]
            # 4xx other than 429 is our bug - a malformed question, a Choice
            # with too many options. Retrying cannot fix it.
            if e.code != 429 and 400 <= e.code < 500:
                raise JevError(f"HTTP {e.code}: {detail}") from e
            last = JevError(f"HTTP {e.code}: {detail}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = JevUnavailable(str(e))
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise last or JevUnavailable("unreachable")


def ask(state: str, questions: dict, timeout: int = 45) -> tuple[dict, Usage]:
    """One request, many questions, all against the same state.

    `questions` maps a name to a Jev question dict. Returns (answers, usage).
    """
    if not questions:
        return {}, Usage()
    if len(state) > MAX_STATE_CHARS:
        raise JevError(
            f"state is {len(state)} chars, over the {MAX_STATE_CHARS} limit. "
            "Shortlist or chunk before calling - do not truncate silently."
        )
    for name, q in questions.items():
        crit = q.get("criteria")
        if q.get("type") == "choice" and isinstance(crit, dict) and len(crit) > _MAX_OPTIONS:
            raise JevError(f"question {name!r} has {len(crit)} options, max {_MAX_OPTIONS}")

    t0 = time.time()
    out = _post({"model": MODEL, "state": state, "questions": questions}, timeout=timeout)
    ms = int((time.time() - t0) * 1000)
    u = out.get("usage") or {}
    return out.get("answers", {}), Usage(
        int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)), 1, ms
    )


# --------------------------------------------------------------------------
# question builders - so callers never hand-roll the wire format
# --------------------------------------------------------------------------

def supports(claim: str) -> dict:
    """Does this page support this statement? -> noul, probability 0..1.

    Phrased as a question about THE PAGE, never about the world. "The page
    does not say this" and "this is false" are different facts, and collapsing
    them is precisely how absence of evidence becomes a false finding.
    """
    return {
        "type": "noul",
        "instructions": (
            "You are shown the text of a web page published by an organisation, "
            "and a statement taken from a third-party directory listing about "
            "that organisation. Does the PAGE TEXT support the statement?\n\n"
            f"Statement: {claim}"
        ),
        "criteria": {
            "true": "The page states this, or states something that clearly entails it.",
            "false": "The page does not state this. This includes the page simply "
                     "not mentioning the subject at all - absence is 'false' here, "
                     "and the caller distinguishes 'absent' from 'contradicted'.",
        },
    }


def contradicts(claim: str) -> dict:
    """Does this page assert something INCOMPATIBLE with the statement?

    The separate, load-bearing question. `supports` going low can mean the page
    is silent. This one only goes high when the page actively says otherwise -
    a different published phone number for the same line, a closure notice.
    Only this one may escalate to a finding.
    """
    return {
        "type": "noul",
        "instructions": (
            "You are shown the text of a web page published by an organisation, "
            "and a statement taken from a third-party directory listing about "
            "that organisation. Does the page assert something that CONTRADICTS "
            "the statement - not merely fail to mention it?\n\n"
            f"Statement: {claim}"
        ),
        "criteria": {
            "true": "The page asserts something incompatible: a different value for "
                    "the same thing, or an explicit statement that it is no longer so.",
            "false": "The page is silent on this, or agrees. Silence is 'false'.",
        },
    }


def relates(claim: str) -> dict:
    """How does this page relate to this claim? -> one Choice, three options.

    This is TypeSafe's documented citation-check pattern
    (docs.typesafe.ai/cookbooks/citation_check), and it is better than the two
    independent nouls in supports()/contradicts() for one specific reason:
    "says nothing" becomes an option the MODEL SELECTS, rather than a state my
    code infers when two separate probabilities both come back low.

    That matters here more than anywhere else. Conflating "the page is silent"
    with "the page disagrees" is the single error behind most of what this
    project has had to retract, and an explicit option is a stronger guarantee
    against it than an inference rule written in application code.

    It also returns a `confidence` - the concentration of the distribution -
    which the two-noul form has no equivalent of, and costs one question
    instead of two.

    Deliberately paired with, not replacing, the noul form: calibrate.py scores
    both against the same pages so the choice is made on measurements.

    Criteria are STRUCTURED objects rather than prose, per
    docs.typesafe.ai/primitives/advanced - and here that is not a style
    preference, it is the single biggest measured win in experiment.py. Over
    the same 444 rows, prose criteria surfaced 14 safe contradictions at >=0.99;
    `what`/`not_for`/`examples` surfaced 25, still with zero false accusations.
    The `not_for` key on `contradicts` is doing most of that work: it states
    explicitly that silence is not disagreement, which is the boundary this
    whole project keeps getting wrong.

    Structured STATE, by contrast, measured no better (AUROC 0.987 vs 0.992) and
    is not used - our state is one blob of page text with no relational
    structure to preserve, which is not the case the docs' advice is about.
    """
    return {
        "type": "choice",
        "instructions": {
            "task": "Decide how the web page in the state relates to the claim.",
            "claim": claim,
            "context": "The page is published by the organisation itself. The "
                       "claim comes from a third-party directory listing about "
                       "that organisation.",
        },
        "criteria": {
            "supports": {
                "what": "The page states the claim, or directly implies it is true.",
                "examples": ["the same phone number appears on the page",
                             "the same street address appears on the page",
                             "the page describes the service the claim describes"],
            },
            "contradicts": {
                "what": "The page gives a DIFFERENT value for the same thing, or "
                        "states that the claim is false.",
                "not_for": "The page simply not mentioning the subject. Silence "
                           "is not disagreement - that is 'says_nothing'.",
                "examples": ["the page lists a different main phone number",
                             "the page says the programme has closed"],
            },
            "says_nothing": {
                "what": "The page does not address what the claim asserts, either "
                        "way. It simply does not mention it.",
                "examples": ["no phone number anywhere on the page",
                             "the page is about an unrelated programme"],
            },
        },
    }


def pick_span(claim: str, spans: list[str]) -> dict:
    """Which numbered span supports this claim? -> choice over OUR indices.

    The anti-fabrication primitive. The model selects from a list we built out
    of bytes we fetched, so a cited span is real by construction. There is
    always a "none" option, and it must be allowed to win.
    """
    if len(spans) > _MAX_OPTIONS - 1:
        raise JevError(f"{len(spans)} spans exceeds the Choice limit")
    criteria = {f"s{i}": s[:300] for i, s in enumerate(spans)}
    criteria["none"] = "No span here supports the statement."
    return {
        "type": "choice",
        "instructions": (
            "Which of these passages, taken from the organisation's own website, "
            "supports the statement? Choose 'none' if none of them do - 'none' is "
            "the correct answer more often than not, and is never penalised.\n\n"
            f"Statement: {claim}"
        ),
        "criteria": criteria,
    }


def best_page(claim: str, urls: list[str]) -> dict:
    """Which URL is most likely to answer this? -> choice over URL slugs.

    Runs on slugs alone, before any page is fetched, to spend the fetch budget
    well. It cannot invent a URL: every option is one discovery.py found.
    """
    urls = urls[:_MAX_OPTIONS - 1]
    criteria = {f"u{i}": u for i, u in enumerate(urls)}
    criteria["none"] = "None of these look likely."
    return {
        "type": "choice",
        "instructions": (
            "Given only these page URLs from one organisation's website, which is "
            "most likely to contain the information needed to check this statement?"
            f"\n\nStatement: {claim}"
        ),
        "criteria": criteria,
    }


# --------------------------------------------------------------------------
# reading answers
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Verdict:
    """What one page had to say about one claim.

    `label` is deliberately four-valued. A three-valued version that folded
    ABSENT into REFUTED is the bug this whole module exists to avoid.
    """
    claim: str
    support: float
    contradict: float
    label: str          # "supported" | "contradicted" | "absent" | "uncertain"
    span: str | None = None
    url: str | None = None
    lastmod: str | None = None

    @property
    def actionable(self) -> bool:
        """May this be shown to a human as a possible error? Only a page that
        actively disagrees qualifies. Silence never does."""
        return self.label == "contradicted"


def classify(support: float, contradict: float) -> str:
    """Probabilities -> one of four labels, using the measured thresholds.

    Contradiction is tested FIRST and against the strictest bar, because it is
    the only label that can reach a human as an accusation.
    """
    if contradict >= CONTRADICT_THRESHOLD:
        return "contradicted"
    if support >= SUPPORT_THRESHOLD:
        return "supported"
    if support <= REFUTE_THRESHOLD and contradict <= REFUTE_THRESHOLD:
        return "absent"
    return "uncertain"


def noul(answers: dict, name: str) -> float | None:
    a = answers.get(name)
    return None if not a else a.get("noul")


def choice(answers: dict, name: str) -> tuple[str | None, float]:
    a = answers.get(name) or {}
    return a.get("choice"), float(a.get("confidence") or 0.0)


def judge_page(page_text: str, claims: dict[str, str], url: str | None = None,
               lastmod: str | None = None) -> tuple[dict[str, Verdict], Usage]:
    """Every claim against one page, in a single request.

    Uses the single three-way Choice (supports / contradicts / says_nothing),
    which beat the two-independent-noul form on measurement, not on taste.
    calibrate.py scored both against the same 444 rows:

                    AUROC    contradictions flagged at >=0.99
        two nouls   0.983    0 - it cannot accuse at all at that bar, and at
                             0.90 it flags 14 of which 5 are actually on the page
        choice      0.992    12, of which 0 are actually on the page

    So the Choice form is the only one that can raise a contradiction safely.
    That is the decisive property: a verifier that can never accuse is not a
    verifier, and one that accuses wrongly a third of the time is worse.

    One question per claim rather than two also halves the cost, and "says
    nothing" becomes an option the MODEL selects instead of a state inferred by
    application code from two low numbers.
    """
    qs = {f"rel__{key}": relates(claim) for key, claim in claims.items()}
    answers, usage = ask(page_text[:MAX_STATE_CHARS], qs)

    out: dict[str, Verdict] = {}
    for key, claim in claims.items():
        a = answers.get(f"rel__{key}") or {}
        probs = a.get("probabilities") or {}
        s, c = probs.get("supports"), probs.get("contradicts")
        if s is None or c is None:
            continue
        out[key] = Verdict(claim, float(s), float(c), classify(float(s), float(c)),
                           url=url, lastmod=lastmod)
    return out, usage


if __name__ == "__main__":
    import sys

    import envfile

    envfile.load()
    if not available():
        print("JEV_API_KEY not set")
        raise SystemExit(1)

    piped = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    state = piped or (
        "Building Futures. 24-hour toll-free domestic violence crisis line: "
        "1-866-292-9688. Emergency shelter for survivors in Alameda County."
    )
    if not piped:
        print("(no stdin; using the built-in example)\n")
    claims = {
        "crisis": "The organisation runs a 24-hour crisis line on 1-866-292-9688.",
        "other": "The organisation's crisis line is 1-800-111-2222.",
        "vet": "The organisation runs a veterinary clinic.",
    }
    verdicts, usage = judge_page(state, claims)
    for k, v in verdicts.items():
        print(f"  {v.label:13} sup={v.support:.2f} con={v.contradict:.2f}  {k}")
    print(f"\n  {usage.calls} call, {usage.ms}ms, ${usage.usd:.6f}")
