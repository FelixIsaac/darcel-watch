/**
 * SF Service Guide Watch — the review session. Transport-agnostic.
 * ================================================================
 *
 * This module is the whole product: load the ranked review queue, hand a
 * volunteer one item at a time, interpret their answer, and record it.
 *
 * It knows NOTHING about Spectrum, iMessage, terminals, or HTTP. `ReviewQueue`
 * exposes the state structurally (for the web app) and `ReviewSession` renders
 * the same state as chat messages (for iMessage and the terminal). Both sit on
 * one queue, one set of outcomes, one set of files.
 *
 * TWO KINDS OF REVIEW — the distinction this file exists to preserve
 * ------------------------------------------------------------------
 * The agent produces two genuinely different results, and collapsing them into
 * one "confirm / reject" prompt makes both of them meaningless:
 *
 *   DISCREPANCY  The agent read the organisation's website and believes the
 *                stored value is wrong. There is a proposed replacement.
 *                Question: "should it be changed to X?"
 *                Answers:  confirm → out/approved.json (a change request)
 *                          reject  → out/rejected.json (the agent was wrong)
 *
 *   ABSTENTION   The agent could NOT establish the truth — the site was
 *                unreachable, JS-only, or the evidence was ambiguous. There is
 *                no proposed replacement, so there is nothing to "confirm".
 *                Question: "can you check this listing?"
 *                Answers:  looks_right  → out/verified.json (a HUMAN
 *                                         verification — of real value, since
 *                                         most listings have never had one)
 *                          needs_fixing → out/flagged.json (manual follow-up)
 *
 * A confirmed abstention is not a change request and is never written as one.
 *
 * READ-ONLY AGAINST PRODUCTION
 * ----------------------------
 * Nothing here ever POSTs to sfserviceguide.org or askdarcel.org. There is no
 * HTTP client in this file. Every outcome is a local file for a human to act
 * on. The agent proposes, a volunteer judges, a human with an account submits.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.join(HERE, "..", "out");

export const RESULTS_PATH = path.join(OUT_DIR, "results.json");
export const APPROVED_PATH = path.join(OUT_DIR, "approved.json");
export const REJECTED_PATH = path.join(OUT_DIR, "rejected.json");
export const VERIFIED_PATH = path.join(OUT_DIR, "verified.json");
export const FLAGGED_PATH = path.join(OUT_DIR, "flagged.json");

/** Stamped onto every recorded outcome so the provenance is never ambiguous. */
const NOT_SUBMITTED_NOTE =
  "Recorded locally only. SF Service Guide Watch never POSTs to " +
  "sfserviceguide.org — a human must submit this.";

// ---------------------------------------------------------------------------
// 1. Types
// ---------------------------------------------------------------------------

/** A change request in the shape ShelterTech's volunteers already work with. */
export interface ChangeRequest {
  resource_id: number | string;
  field: string;
  current: string;
  proposed: string;
  source_url: string;
  source_quote?: string;
  listing_url?: string;
  listing_edit_url?: string;
  org_website?: string;
  submitted_by: string;
}

/**
 * What kind of question this review asks. Three genuinely different questions —
 * collapsing any of them into "confirm / reject" makes it meaningless.
 *
 *   discrepancy  the agent found a different value and proposes it
 *   abstention   the agent could not establish the truth at all
 *   structural   the stored value is malformed on its face — it cannot be
 *                dialled as written. There is no proposed replacement, because
 *                nobody knows the right number; only that this one is wrong.
 */
export type ReviewKind = "discrepancy" | "abstention" | "structural";

/** Every action a volunteer can take, across both kinds. */
export type Action =
  | "confirm"
  | "reject"
  | "skip"
  | "looks_right"
  | "needs_fixing";

/** Where each action's outcome is recorded. `skip` records nothing. */
const OUTCOME_FILE: Record<Exclude<Action, "skip">, string> = {
  confirm: APPROVED_PATH,
  reject: REJECTED_PATH,
  looks_right: VERIFIED_PATH,
  needs_fixing: FLAGGED_PATH,
};

/** The actions each kind of review actually offers. */
export const ACTIONS_FOR: Record<ReviewKind, Action[]> = {
  discrepancy: ["confirm", "reject", "skip"],
  abstention: ["looks_right", "needs_fixing", "skip"],
  // "Is this broken?" — so the affirmative answer comes first, and it means
  // needs_fixing. Same two outcome files as an abstention, opposite ordering,
  // because the question is inverted.
  structural: ["needs_fixing", "looks_right", "skip"],
};

/**
 * One review, already normalised. Everything here is a known-good value — the
 * messy optional-field handling happens once, in `normalise()`, so nothing
 * downstream has to defend itself.
 */
export interface ReviewItem {
  resourceId: number | string;
  kind: ReviewKind;
  name: string;
  field: string;
  stored: string;
  /** Empty unless the agent is proposing a replacement value. */
  live: string;
  /**
   * For a structural finding: what is wrong with the stored value
   * ("not dialable as stored"). Never a replacement — see ReviewKind.
   */
  defect: string;
  sourceUrl: string;
  sourceQuote: string;
  /** The listing on SF Service Guide — the "stored" side's own page. */
  listingUrl: string;
  /** Where a human actually makes the fix. Surfaced only after a confirm. */
  listingEditUrl: string;
  /** The organisation's own site — the "live" side. */
  orgWebsite: string;
  verdict: string;
  reason: string;
  confidence: number | null;
  fetched: string[];
  blastRadiusSize: number;
  blastRadiusServices: string[];
  priority: number;
  /** Only meaningful for a discrepancy. */
  changeRequest: ChangeRequest | null;
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/**
 * Control characters, collapsed to spaces.
 *
 * Everything in a review ultimately came from scraping somebody's website, and
 * real pages contain form feeds, vertical tabs and stray C1 bytes. They render
 * as nothing, they break alignment in a terminal bubble, and they are a
 * standing hazard for anything that has to serialise this text. Strip them once
 * here, at the boundary, rather than in each transport.
 */
const CONTROL_CHARS = /[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g;

/** Coerce anything into a trimmed, control-character-free string. */
function str(v: unknown, fallback = ""): string {
  if (v === null || v === undefined) return fallback;
  if (typeof v === "string") {
    return v.replace(CONTROL_CHARS, " ").replace(/\s+/g, " ").trim() || fallback;
  }
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return fallback;
}

function num(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

/** Strings only, from an unknown array. Silently drops anything else. */
function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.map((e) => str(e)).filter(Boolean) : [];
}

/** Only return a URL we can actually render as a link. */
function url(v: unknown): string {
  const s = str(v);
  return /^https?:\/\//i.test(s) ? s : "";
}

/**
 * Turn an upstream field name into something a volunteer can read.
 *
 * verify.py names fields for what it found — `phone_missing`, `hours_mismatch`
 * — which is right for the data and wrong in a sentence: "could not verify this
 * listing's phone_missing". The suffix describes the finding, which the review
 * already states in words, so it is dropped and underscores become spaces.
 */
const FIELD_WORDS: Record<string, string> = {
  phone: "phone number",
  // A structural finding is about the number itself, not about "format" —
  // "this listing's phone format can't be dialled" reads like jargon.
  phone_format: "phone number",
  phone_missing: "phone number",
  address: "address",
  hours: "opening hours",
  website: "website",
  email: "email address",
  operating_status: "open/closed status",
  listing: "listing",
};

export function fieldLabel(field: string): string {
  const base =
    field
      .replace(/_(missing|mismatch|notfound|not_found|changed|stale)$/i, "")
      .trim() || field;
  return FIELD_WORDS[base] ?? base.replace(/_/g, " ");
}

/** "feedingseniors.org" — what to label the live side with. */
export function hostOf(u: string): string {
  try {
    return new URL(u).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// 2. Reading out/results.json without trusting it
// ---------------------------------------------------------------------------

/**
 * Turn one raw queue entry into a `ReviewItem`, or return null to drop it.
 *
 * This is the "never crash on a malformed entry" boundary, and also where the
 * two kinds are separated. A discrepancy needs a proposed value to be worth
 * asking about; an abstention needs only a field the agent failed to settle.
 */
function normalise(raw: unknown, kind: ReviewKind): ReviewItem | null {
  if (!isRecord(raw)) return null;

  const resourceId =
    typeof raw.resource_id === "number" || typeof raw.resource_id === "string"
      ? raw.resource_id
      : null;
  if (resourceId === null || str(resourceId) === "") return null;

  const cr = isRecord(raw.change_request) ? raw.change_request : {};

  // `fields` is the agent's raw evidence; change_request is its distilled
  // proposal. A record often carries SEVERAL findings — #2258 arrives with a
  // weak `phone_missing` inference at [0] and a hard `phone_format` defect at
  // [1]. Taking [0] blindly buries the certain finding behind the speculative
  // one, so pick the most actionable: a structural defect is arithmetic on the
  // stored value and cannot be a scraping false positive, so it wins.
  const allFields = Array.isArray(raw.fields) ? raw.fields.filter(isRecord) : [];
  const firstField =
    allFields.find((f) => f.structural === true) ?? allFields[0] ?? {};

  // A discrepancy is always about one field — that's what makes it a proposed
  // change. An abstention usually isn't: the agent couldn't reach the site at
  // all, so it has no field, no stored value and no opinion. Requiring a field
  // here would silently drop every abstention, which is most of the queue and
  // exactly the case that most needs a human.
  // When a structural defect is present it IS the review, so it is read
  // straight off its own field rather than through `change_request` — which
  // describes whichever finding the pipeline distilled, not this one.
  const structuralField = allFields.find((f) => f.structural === true);

  const field = structuralField
    ? str(structuralField.field)
    : str(cr.field) || str(firstField.field) || (kind === "abstention" ? "listing" : "");
  if (!field) return null;

  const stored = structuralField
    ? str(structuralField.stored)
    : str(cr.current) || str(firstField.stored);
  let live = structuralField
    ? str(structuralField.live)
    : str(cr.proposed) || str(firstField.live);

  // Defensive, regardless of what the JSON says: a "change" from a value to
  // the same value is not a change. verify.py now filters these out, but a
  // stale results.json must not be able to render a diff of a thing against
  // itself — that reads as a bug to the volunteer, and it is one.
  if (live && stored && live === stored) live = "";

  // A structural finding says the STORED value is malformed — `live` carries a
  // description of the defect ("not dialable as stored"), never a replacement.
  // Rendering it as a proposed new value produces the nonsense question
  // "should it be changed to 'not dialable as stored'?".
  const isStructural =
    structuralField !== undefined ||
    raw.structural === true ||
    cr.structural === true ||
    field === "phone_format";

  // A discrepancy with nothing to propose isn't a discrepancy; it's really an
  // abstention, and asking "should it be changed to (nothing)?" is nonsense.
  const effectiveKind: ReviewKind = isStructural
    ? "structural"
    : kind === "discrepancy" && !live
      ? "abstention"
      : kind;

  // Past this point `live` means "the value being proposed". A structural
  // finding has none, so it is cleared rather than left to leak into a diff.
  const defect = isStructural ? live : "";
  if (isStructural) live = "";

  if (effectiveKind === "discrepancy" && !live) return null;
  if (effectiveKind === "structural" && !stored) return null;
  // An abstention is worth a volunteer's time as long as they can act on it —
  // which means a place to look. Without a website there is nothing to check.
  const hasSomewhereToLook =
    url(raw.org_website) || url(cr.org_website) || url(raw.listing_url) || stored;
  if (effectiveKind === "abstention" && !hasSomewhereToLook) return null;

  const sourceUrl = structuralField
    ? url(structuralField.evidence_url)
    : url(cr.source_url) || url(firstField.evidence_url);
  const sourceQuote = structuralField
    ? ""  // the "quote" is just the stored value repeated; already on screen
    : str(cr.source_quote) || str(firstField.evidence_quote);

  const orgWebsite = url(raw.org_website) || url(cr.org_website);
  const listingUrl = url(raw.listing_url) || url(cr.listing_url);
  const listingEditUrl = url(raw.listing_edit_url) || url(cr.listing_edit_url);

  const blast = isRecord(raw.blast_radius) ? raw.blast_radius : {};
  const blastServices = Array.isArray(blast.services)
    ? blast.services.map((s) => (isRecord(s) ? str(s.name) : "")).filter(Boolean)
    : [];

  const changeRequest: ChangeRequest | null =
    effectiveKind === "discrepancy"
      ? {
          resource_id: resourceId,
          field,
          current: stored,
          proposed: live,
          source_url: sourceUrl,
          ...(sourceQuote ? { source_quote: sourceQuote } : {}),
          ...(listingUrl ? { listing_url: listingUrl } : {}),
          ...(listingEditUrl ? { listing_edit_url: listingEditUrl } : {}),
          ...(orgWebsite ? { org_website: orgWebsite } : {}),
          submitted_by: str(
            cr.submitted_by,
            "shelflife (agent, human review required)",
          ),
        }
      : null;

  return {
    resourceId,
    kind: effectiveKind,
    name: str(raw.name, "Unknown organisation"),
    field,
    stored,
    live,
    defect,
    sourceUrl,
    sourceQuote,
    listingUrl,
    listingEditUrl,
    orgWebsite,
    verdict: str(raw.verdict, effectiveKind),
    reason: str(raw.reason),
    confidence: typeof raw.confidence === "number" ? raw.confidence : null,
    fetched: strList(raw.fetched),
    blastRadiusSize: num(blast.size, blastServices.length),
    blastRadiusServices: blastServices,
    priority: num(raw.priority, 0),
    changeRequest,
  };
}

/** A recorded outcome, as replayed from the outcome files. */
export interface Decision {
  action: Action;
  at: string;
}

/**
 * Every resource a volunteer has already ruled on, across all four outcome
 * files.
 *
 * Decisions are durable, so the queue has to be. Without this, every restart
 * hands the volunteer the same listings they already answered — and each pass
 * appends another record for a thing already recorded.
 *
 * Skips are deliberately NOT durable: skipping means "not me, not now".
 */
export function loadDecisions(): Map<string, Decision> {
  const decisions = new Map<string, Decision>();

  for (const [action, file] of Object.entries(OUTCOME_FILE) as [
    Exclude<Action, "skip">,
    string,
  ][]) {
    if (!fs.existsSync(file)) continue;
    try {
      const parsed: unknown = JSON.parse(fs.readFileSync(file, "utf8"));
      if (!Array.isArray(parsed)) continue;
      for (const row of parsed) {
        if (!isRecord(row) || row.resource_id === undefined) continue;
        decisions.set(str(row.resource_id), {
          action,
          at: str(row.decided_at),
        });
      }
    } catch {
      // An unreadable outcome log shouldn't block the queue. It gets moved
      // aside on the next write; until then, treat it as holding nothing.
    }
  }

  return decisions;
}

export interface LoadResult {
  items: ReviewItem[];
  dropped: number;
  /** Ruled on in an earlier session, so not offered again. */
  alreadyDecided: number;
  /** Set when there is nothing to review; the reason is shown to the operator. */
  emptyReason?: string;
}

/**
 * Load and rank the review queue.
 *
 * `priority = confidence * (1 + blast_radius_size * 0.15)` was already computed
 * upstream by graph.py — a wrong record that invalidates twelve services should
 * reach a volunteer before a high-confidence typo. We just sort by it.
 *
 * Both lists are reviewable. `queue` holds the discrepancies; `abstained` holds
 * the ones the agent refused to call. They ask different questions, so they are
 * tagged with different kinds here and stay distinct all the way down.
 */
export function loadQueue(): LoadResult {
  if (!fs.existsSync(RESULTS_PATH)) {
    return {
      items: [],
      dropped: 0,
      alreadyDecided: 0,
      emptyReason:
        `No ${path.relative(process.cwd(), RESULTS_PATH)} found.\n` +
        "Run the audit first:  BUDGET=5 python3 run.py",
    };
  }

  let payload: unknown;
  try {
    payload = JSON.parse(fs.readFileSync(RESULTS_PATH, "utf8"));
  } catch (err) {
    return {
      items: [],
      dropped: 0,
      alreadyDecided: 0,
      emptyReason:
        `${path.relative(process.cwd(), RESULTS_PATH)} is not valid JSON ` +
        `(${(err as Error).message}).\nRe-run:  BUDGET=5 python3 run.py`,
    };
  }

  const root = isRecord(payload) ? payload : {};
  const raw: [unknown, ReviewKind][] = [
    ...(Array.isArray(root.queue) ? root.queue : []).map(
      (e): [unknown, ReviewKind] => [e, "discrepancy"],
    ),
    ...(Array.isArray(root.abstained) ? root.abstained : []).map(
      (e): [unknown, ReviewKind] => [e, "abstention"],
    ),
  ];

  const decided = loadDecisions();

  const items: ReviewItem[] = [];
  let dropped = 0;
  let alreadyDecided = 0;
  for (const [entry, kind] of raw) {
    const item = normalise(entry, kind);
    if (!item) {
      dropped++;
      continue;
    }
    if (decided.has(str(item.resourceId))) {
      alreadyDecided++;
      continue;
    }
    items.push(item);
  }

  items.sort((a, b) => b.priority - a.priority);

  let emptyReason: string | undefined;
  if (items.length === 0) {
    emptyReason =
      alreadyDecided > 0
        ? `Nothing left to review — all ${alreadyDecided} outstanding ` +
          `listing(s) have already been answered. See out/approved.json, ` +
          `out/rejected.json, out/verified.json and out/flagged.json.`
        : "The review queue is empty — every audited listing either matched " +
          "its live site or produced nothing a human could act on.";
  }

  return { items, dropped, alreadyDecided, emptyReason };
}

// ---------------------------------------------------------------------------
// 3. Recording outcomes (local files only — never the network)
// ---------------------------------------------------------------------------

/**
 * Append one outcome to a JSON array file, creating it if needed.
 *
 * If the file exists but is unreadable, it is moved aside rather than
 * overwritten. Losing a volunteer's earlier answers to a parse error would be
 * worse than leaving a stray file on disk.
 */
function appendOutcome(filePath: string, record: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });

  let existing: unknown[] = [];
  if (fs.existsSync(filePath)) {
    try {
      const parsed: unknown = JSON.parse(fs.readFileSync(filePath, "utf8"));
      if (Array.isArray(parsed)) existing = parsed;
      else throw new Error("expected a JSON array");
    } catch {
      const aside = `${filePath}.corrupt-${Date.now()}`;
      fs.renameSync(filePath, aside);
      console.warn(`[shelflife] ${filePath} was unreadable; moved to ${aside}`);
      existing = [];
    }
  }

  existing.push(record);
  fs.writeFileSync(filePath, `${JSON.stringify(existing, null, 2)}\n`, "utf8");
}

/**
 * Write one outcome to disk.
 *
 * Every transport lands here, so the record is identical whether the volunteer
 * tapped a button in the browser or texted "Y" from a bus stop.
 *
 * A `confirm` produces a change_request. A `looks_right` produces a human
 * verification, which is a different artifact in a different file — it asserts
 * "a person checked this and it was fine", which for a directory where most
 * listings carry no machine-readable record of having been confirmed is worth
 * recording on its own.
 */
export function recordOutcome(
  item: ReviewItem,
  action: Exclude<Action, "skip">,
  decidedBy: string,
  platform: string,
): void {
  const common = {
    resource_id: item.resourceId,
    name: item.name,
    field: item.field,
    agent_verdict: item.verdict,
    agent_confidence: item.confidence,
    blast_radius_size: item.blastRadiusSize,
    ...(item.listingUrl ? { listing_url: item.listingUrl } : {}),
    ...(item.listingEditUrl ? { listing_edit_url: item.listingEditUrl } : {}),
    ...(item.orgWebsite ? { org_website: item.orgWebsite } : {}),
    decided_by: decidedBy,
    decided_via: platform,
    decided_at: new Date().toISOString(),
    submitted_to_askdarcel: false,
    note: NOT_SUBMITTED_NOTE,
  };

  if (action === "confirm") {
    appendOutcome(APPROVED_PATH, {
      change_request: item.changeRequest,
      decision: "confirmed",
      ...common,
    });
    return;
  }

  if (action === "reject") {
    appendOutcome(REJECTED_PATH, {
      change_request: item.changeRequest,
      decision: "rejected",
      ...common,
    });
    return;
  }

  if (action === "looks_right") {
    // NOT a change request. A human verification: the listing was checked by a
    // person on this date and found correct.
    appendOutcome(VERIFIED_PATH, {
      decision: "verified_by_human",
      stored_value: item.stored,
      agent_could_not_verify: item.reason,
      ...common,
    });
    return;
  }

  appendOutcome(FLAGGED_PATH, {
    decision: "needs_manual_fix",
    stored_value: item.stored,
    agent_could_not_verify: item.reason,
    ...common,
  });
}

// ---------------------------------------------------------------------------
// 4. Wording — shared by every transport
// ---------------------------------------------------------------------------

/** Keep quotes phone-sized; a wall of scraped text is unreadable on a phone. */
function clip(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, max - 1).trimEnd()}…`;
}

/** The question this review is actually asking, in plain words. */
export function askLine(item: ReviewItem): string {
  if (item.kind === "structural") {
    // No website was consulted and none is needed: the stored value is
    // self-evidently unusable. The question is whether to flag it, not what
    // to replace it with — nobody knows that yet.
    return (
      `This listing's ${fieldLabel(item.field)} can't be dialled as stored. ` +
      `Should it be flagged for correction?`
    );
  }
  if (item.kind === "abstention") {
    const what =
      item.field === "listing"
        ? "this listing"
        : `this listing's ${fieldLabel(item.field)}`;
    const why = item.reason ? ` (${item.reason})` : "";
    return `The agent could not verify ${what}${why}. Can you check it?`;
  }
  return (
    `The agent thinks the stored ${fieldLabel(item.field)} is wrong. ` +
    `Should it be changed to "${clip(item.live, 60)}"?`
  );
}

/**
 * Human label for each action, in the words of the question being asked.
 *
 * The same recorded outcome reads differently depending on the question. A
 * structural finding asks "is this broken?", so `needs_fixing` is "Yes, it's
 * broken" — not "Needs fixing", which would be an odd answer to a yes/no.
 */
export function actionLabel(action: Action, kind: ReviewKind = "discrepancy"): string {
  if (kind === "structural") {
    if (action === "needs_fixing") return "Yes, it's broken";
    if (action === "looks_right") return "No, it's fine";
    return "Skip";
  }
  switch (action) {
    case "confirm":
      return "Yes, change it";
    case "reject":
      return "No, leave it";
    case "looks_right":
      return "Looks right";
    case "needs_fixing":
      return "Needs fixing";
    default:
      return "Skip";
  }
}

/** Where the volunteer is told each side of the comparison came from. */
export function storedLabel(): string {
  return "SF Service Guide";
}

export function liveLabel(item: ReviewItem): string {
  return hostOf(item.orgWebsite || item.sourceUrl) || "their website";
}

/**
 * How the volunteer can act on this review.
 *
 * On a phone or a terminal the only affordance is typing, so every message has
 * to carry its own instructions. In the web app those actions are always on
 * screen as buttons, and repeating them under each review is noise.
 */
export type Affordances = "text" | "buttons";

export function reviewCard(
  item: ReviewItem,
  position: number,
  total: number,
  affordances: Affordances = "text",
): string {
  const lines = [
    `SF Service Guide Watch — review #${item.resourceId}`,
    `Review ${position} of ${total}`,
    "",
    item.name,
    "",
    askLine(item),
    "",
  ];

  // A structural finding has one side, not two: the stored value and what is
  // wrong with it. There is no arrow because there is nothing to point at.
  if (item.kind === "structural") {
    lines.push(`${fieldLabel(item.field)} as stored:`);
    for (const part of item.stored.split(";")) {
      const trimmed = part.trim();
      if (trimmed) lines.push(`  ${trimmed}`);
    }
    if (item.defect) lines.push(`  → ${item.defect}`);
    if (item.listingUrl) {
      lines.push("", `The listing: ${item.listingUrl}`);
    }
    if (affordances === "text") {
      lines.push("", "Y it's broken · N it's fine · S skip · ? more evidence");
    }
    return lines.join("\n");
  }

  // Both sides, each labelled with where it came from. A reviewer under time
  // pressure must never have to work out which of two URLs is which.
  if (item.stored) {
    lines.push(
      `${fieldLabel(item.field)}`,
      `  stored  ${clip(item.stored, 70)}`,
      `          ← ${storedLabel()}${item.listingUrl ? `: ${item.listingUrl}` : ""}`,
    );
  } else if (item.listingUrl) {
    lines.push(`The listing (${storedLabel()}): ${item.listingUrl}`);
  }

  // Only when there genuinely is a different live value. A row of a value
  // against itself is never rendered — that reads as a bug, because it is one.
  if (item.live) {
    lines.push(
      `  live    ${clip(item.live, 70)}`,
      `          ← ${liveLabel(item)}${item.sourceUrl ? `: ${item.sourceUrl}` : ""}`,
    );
  } else if (item.orgWebsite) {
    lines.push(`Their website: ${item.orgWebsite}`);
  }

  if (item.sourceQuote) {
    lines.push("", `Quoted from ${liveLabel(item)}:`, `  "${clip(item.sourceQuote, 200)}"`);
  }

  if (item.blastRadiusSize > 0) {
    lines.push(
      "",
      `If this is wrong, ${item.blastRadiusSize} other listing(s) are wrong too.`,
    );
  }

  if (affordances === "text") {
    lines.push(
      "",
      item.kind === "abstention"
        ? "R looks right · F needs fixing · S skip · ? more evidence"
        : "Y confirm · N reject · S skip · ? more evidence",
    );
  }

  return lines.join("\n");
}

/**
 * The deep dive behind a review — everything the agent read.
 *
 * The headline evidence (the quote, both source links) is already on the review
 * card, because a reviewer needs it to answer at all. This is the rest.
 */
export function evidenceCard(item: ReviewItem): string {
  const lines = [`Everything the agent read · #${item.resourceId} ${item.name}`, ""];

  const confidence =
    item.confidence === null ? "" : ` (confidence ${item.confidence.toFixed(2)})`;
  lines.push(`Agent verdict: ${item.verdict}${confidence}`);
  if (item.reason) lines.push(`Reasoning: ${item.reason}`);

  if (item.listingUrl) lines.push("", `The listing: ${item.listingUrl}`);
  if (item.orgWebsite) lines.push(`Their website: ${item.orgWebsite}`);

  if (item.sourceQuote) {
    lines.push("", `Quoted from the live page:`, `  "${clip(item.sourceQuote, 260)}"`);
  }

  if (item.fetched.length > 0) {
    lines.push("", "Pages the agent fetched:");
    for (const u of item.fetched) lines.push(`  · ${u}`);
  }

  if (item.blastRadiusServices.length > 0) {
    lines.push("", `Blast radius (${item.blastRadiusSize}):`);
    for (const name of item.blastRadiusServices.slice(0, 6)) {
      lines.push(`  · ${clip(name, 60)}`);
    }
    const hidden = item.blastRadiusServices.length - 6;
    if (hidden > 0) lines.push(`  · …and ${hidden} more`);
  }

  return lines.join("\n");
}

/**
 * What to say once an outcome is recorded.
 *
 * On a confirm this is where the edit link belongs — the follow-through, not
 * the question. The volunteer has just decided the listing should change; the
 * most useful next thing is where to change it.
 */
export function outcomeMessage(item: ReviewItem, action: Action): string {
  switch (action) {
    case "confirm": {
      const lines = [
        `Confirmed #${item.resourceId}. Change request saved to out/approved.json.`,
      ];
      if (item.listingEditUrl) {
        lines.push("", `Make the fix here: ${item.listingEditUrl}`);
      }
      return lines.join("\n");
    }
    case "reject":
      return (
        `Rejected #${item.resourceId}. Logged to out/rejected.json — ` +
        `the agent was wrong, and that's worth knowing.`
      );
    case "looks_right":
      if (item.kind === "structural") {
        return (
          `Recorded #${item.resourceId} as fine as stored — you checked it, ` +
          `the agent was wrong. Saved to out/verified.json.`
        );
      }
      return (
        `Marked #${item.resourceId} as verified by you, on today's date. ` +
        `Saved to out/verified.json.\nMost listings have never had that.`
      );
    case "needs_fixing": {
      const lines = [
        item.kind === "structural"
          ? `Flagged #${item.resourceId}: the stored ${fieldLabel(item.field)} ` +
            `can't be dialled. Saved to out/flagged.json.`
          : `Flagged #${item.resourceId} for manual follow-up. ` +
            `Saved to out/flagged.json.`,
      ];
      // Same follow-through as a confirm: they've decided it's wrong, so the
      // useful next thing is where to fix it.
      if (item.listingEditUrl) lines.push("", `Fix it here: ${item.listingEditUrl}`);
      return lines.join("\n");
    }
    default:
      return `Skipped #${item.resourceId}.`;
  }
}

export const HELP_TEXT = [
  "SF Service Guide Watch — commands",
  "",
  "When the agent proposes a change:",
  "  Y      yes, the listing should be changed",
  "  N      no, the agent is wrong, leave it",
  "",
  "When the agent could not verify a listing:",
  "  R      looks right — records YOUR verification",
  "  F      needs fixing — flags it for manual follow-up",
  "",
  "Any time:",
  "  S      skip — someone else can take this one",
  "  ?      show everything the agent read",
  "  queue  how many reviews are left",
  "  help   this message",
  "  stop   end the session",
  "",
  "SF Service Guide Watch never writes to sfserviceguide.org.",
].join("\n");

// ---------------------------------------------------------------------------
// 5. Reading the volunteer's reply
// ---------------------------------------------------------------------------

export type Command =
  | { kind: "action"; action: Action }
  | { kind: "evidence" }
  | { kind: "queue" }
  | { kind: "help" }
  | { kind: "stop" }
  | { kind: "unknown" };

/**
 * Parse a reply into a command.
 *
 * Deliberately forgiving. A volunteer answering from a bus stop types "yes",
 * "y.", "Y 👍" or "/queue" — all unambiguous to a human, so they should be
 * unambiguous here. The `/`-prefixed forms exist because the terminal provider
 * offers them as slash-command autocomplete.
 */
export function parseCommand(raw: string): Command {
  const body = raw.trim().replace(/^\//, "").toLowerCase();
  if (!body) return { kind: "unknown" };

  const word = body.split(/\s+/)[0] ?? "";

  if (word.startsWith("?")) return { kind: "evidence" };
  if (word === "queue" || word === "count" || word === "left") {
    return { kind: "queue" };
  }
  if (word === "help" || word === "commands") return { kind: "help" };
  if (word === "stop" || word === "quit" || word === "done" || word === "exit") {
    return { kind: "stop" };
  }

  // The web app POSTs these verbatim.
  if (word === "looks_right") return { kind: "action", action: "looks_right" };
  if (word === "needs_fixing") return { kind: "action", action: "needs_fixing" };
  if (word === "confirm") return { kind: "action", action: "confirm" };
  if (word === "reject") return { kind: "action", action: "reject" };

  if (/^y(es|ep|eah)?[.!]?$/.test(word)) return { kind: "action", action: "confirm" };
  if (/^n(o|ope|ah)?[.!]?$/.test(word)) return { kind: "action", action: "reject" };
  if (/^s(kip)?[.!]?$/.test(word)) return { kind: "action", action: "skip" };
  if (/^r(ight|ok)?[.!]?$/.test(word)) return { kind: "action", action: "looks_right" };
  if (/^f(ix)?[.!]?$/.test(word)) return { kind: "action", action: "needs_fixing" };

  return { kind: "unknown" };
}

// ---------------------------------------------------------------------------
// 6. The queue — the structural core both transports share
// ---------------------------------------------------------------------------

export interface Counts {
  confirmed: number;
  rejected: number;
  skipped: number;
  verified: number;
  flagged: number;
}

export interface ApplyResult {
  /** Whether the action was valid for the item that was open. */
  ok: boolean;
  /** Human-readable outcome, for the chat thread. */
  message: string;
  /** The item the action applied to, if any. */
  item: ReviewItem | null;
  action: Action | null;
}

/**
 * Hands out reviews one at a time, records outcomes, and tracks progress.
 *
 * The web server reads this structurally; `ReviewSession` renders it as chat.
 * Both go through `apply()`, so an answer given in the browser and one texted
 * from a phone take the same path to the same file.
 */
export class ReviewQueue {
  private readonly items: ReviewItem[];
  private readonly settled = new Set<number>();
  private readonly skippedIndices = new Set<number>();
  private cursor: number | null = null;

  readonly counts: Counts = {
    confirmed: 0,
    rejected: 0,
    skipped: 0,
    verified: 0,
    flagged: 0,
  };

  private readonly reviewerId: string;
  private readonly platform: string;

  ended = false;

  constructor(items: ReviewItem[], reviewerId: string, platform: string) {
    this.items = items;
    this.reviewerId = reviewerId;
    this.platform = platform;
  }

  get total(): number {
    return this.items.length;
  }

  /** How many this volunteer has actually answered. */
  get answered(): number {
    return this.settled.size;
  }

  get remaining(): number {
    return this.items.length - this.settled.size;
  }

  /** The review on screen right now, claiming the next one if none is open. */
  current(): { item: ReviewItem; index: number; position: number } | null {
    if (this.ended) return null;
    if (this.cursor === null) this.advance();
    if (this.cursor === null) return null;
    const item = this.items[this.cursor];
    if (!item) return null;
    return { item, index: this.cursor, position: this.cursor + 1 };
  }

  /**
   * Move to the next unsettled review this volunteer hasn't skipped.
   *
   * `skip` has to mean "not me", or it means nothing — so a lone reviewer who
   * skips the last open item is not handed it straight back.
   */
  private advance(): void {
    for (let i = 0; i < this.items.length; i++) {
      if (this.settled.has(i) || this.skippedIndices.has(i)) continue;
      this.cursor = i;
      return;
    }
    this.cursor = null;
    this.ended = true;
  }

  /** Apply one action to the open review and move on. */
  apply(requested: Action): ApplyResult {
    let action: Action = requested;
    const open = this.current();
    if (!open) {
      return {
        ok: false,
        message: "No review open right now.",
        item: null,
        action: null,
      };
    }

    const { item, index } = open;

    // A structural review is a yes/no question ("is this broken?"), and it is
    // presented that way — "Y it's broken · N it's fine". So a yes/no answer
    // has to land on the right outcome rather than being refused on a
    // technicality the volunteer never saw.
    if (item.kind === "structural") {
      if (action === "confirm") action = "needs_fixing";
      else if (action === "reject") action = "looks_right";
    }

    // Reject an action that doesn't belong to this kind of review, rather than
    // silently recording something the volunteer didn't mean.
    if (!ACTIONS_FOR[item.kind].includes(action)) {
      const offered = ACTIONS_FOR[item.kind].map((a) => actionLabel(a, item.kind)).join(" · ");
      return {
        ok: false,
        message:
          item.kind === "abstention"
            ? `The agent didn't propose a change here, so there's nothing to ` +
              `confirm. Options: ${offered}.`
            : `Options for this one: ${offered}.`,
        item,
        action: null,
      };
    }

    if (action === "skip") {
      this.skippedIndices.add(index);
      this.counts.skipped++;
    } else {
      recordOutcome(item, action, this.reviewerId, this.platform);
      this.settled.add(index);
      if (action === "confirm") this.counts.confirmed++;
      else if (action === "reject") this.counts.rejected++;
      else if (action === "looks_right") this.counts.verified++;
      else this.counts.flagged++;
    }

    this.cursor = null;
    this.advance();

    return { ok: true, message: outcomeMessage(item, action), item, action };
  }

  /** End the session early; the open review goes back for someone else. */
  stop(): void {
    this.cursor = null;
    this.ended = true;
  }

  summary(): string {
    const c = this.counts;
    const answered = c.confirmed + c.rejected + c.verified + c.flagged;
    const lines = [
      `You answered ${answered} listing${answered === 1 ? "" : "s"}.`,
    ];

    const bits: string[] = [];
    if (c.confirmed) bits.push(`${c.confirmed} change request(s) filed`);
    if (c.rejected) bits.push(`${c.rejected} agent error(s) recorded`);
    if (c.verified) bits.push(`${c.verified} verified by you`);
    if (c.flagged) bits.push(`${c.flagged} flagged for manual fixing`);
    if (c.skipped) bits.push(`${c.skipped} skipped`);
    if (bits.length) lines.push("", bits.join(", ") + ".");

    if (c.verified > 0) {
      lines.push(
        "",
        `${c.verified} listing(s) now carry a human verification dated today.`,
        "Most listings in the guide have never had one.",
      );
    }
    if (c.confirmed > 0) {
      lines.push(
        "",
        `Change requests are in out/approved.json, ready for someone with a`,
        "ShelterTech account to submit.",
      );
    }

    lines.push("", "Nothing was written to sfserviceguide.org — that's a human's call.");
    lines.push("", "Thank you. This is the expensive part of keeping the guide true.");
    return lines.join("\n");
  }
}

// ---------------------------------------------------------------------------
// 7. The chat session — messaging transports
// ---------------------------------------------------------------------------

/**
 * How this session talks to its volunteer. The single seam between the review
 * logic and any messaging transport.
 */
export type Send = (body: string) => Promise<void>;

export interface SessionOptions {
  queue: ReviewQueue;
  send: Send;
  affordances?: Affordances;
}

/** One volunteer's conversation, rendered as chat over a `ReviewQueue`. */
export class ReviewSession {
  private readonly queue: ReviewQueue;
  private sendFn: Send;
  private readonly affordances: Affordances;
  private heardFrom = false;

  constructor(options: SessionOptions) {
    this.queue = options.queue;
    this.sendFn = options.send;
    this.affordances = options.affordances ?? "text";
  }

  get ended(): boolean {
    return this.queue.ended;
  }

  /**
   * Point this session at wherever the volunteer actually replied from.
   *
   * Outbound and inbound addresses are not guaranteed to match. The Spectrum
   * terminal provider is a live example: `space.create()` mints `chat-1`, but
   * replies from the plain-readline client arrive on a space called `terminal`.
   * Without rebinding, the first reply looks like a brand-new volunteer — the
   * session forks and the review already on screen is stranded.
   */
  rebind(send: Send): void {
    this.sendFn = send;
  }

  get awaitingFirstReply(): boolean {
    return !this.heardFrom;
  }

  markHeard(): void {
    this.heardFrom = true;
  }

  private send(body: string): Promise<void> {
    return this.sendFn(body);
  }

  async start(): Promise<void> {
    const intro = [
      "SF Service Guide Watch",
      "",
      "The SF Service Guide tells unhoused San Franciscans where to find food,",
      "a shelter bed, a clinic, legal aid. Most of its listings have never been",
      "checked since they were added — some are seven years old.",
      "",
      `A software agent re-checked ${this.queue.total} listing(s) against the`,
      "organisation's own website. It isn't trusted to decide alone, so for",
      "each one it's asking you.",
    ];

    if (this.affordances === "text") {
      intro.push("", "Type `help` any time to see the options.");
    } else {
      intro.push("", "Nothing you do here changes the live directory.");
    }

    await this.send(intro.join("\n"));
    await this.sendCurrent();
  }

  private async sendCurrent(): Promise<void> {
    const open = this.queue.current();
    if (!open) {
      await this.send(
        ["That's the whole queue — nothing left to review.", "", this.queue.summary()].join(
          "\n",
        ),
      );
      return;
    }
    await this.send(
      reviewCard(open.item, open.position, this.queue.total, this.affordances),
    );
  }

  handleText(raw: string): Promise<void> {
    return this.handle(parseCommand(raw));
  }

  async handle(command: Command): Promise<void> {
    if (this.queue.ended) return;

    switch (command.kind) {
      case "help":
        await this.send(HELP_TEXT);
        return;

      case "queue":
        await this.send(
          `${this.queue.remaining} of ${this.queue.total} review(s) still open.`,
        );
        return;

      case "stop": {
        this.queue.stop();
        await this.send(["Session ended.", "", this.queue.summary()].join("\n"));
        return;
      }

      case "evidence": {
        const open = this.queue.current();
        if (!open) {
          await this.send("No review open right now.");
          return;
        }
        await this.send(evidenceCard(open.item));
        return;
      }

      case "action": {
        const result = this.queue.apply(command.action);
        await this.send(result.message);
        if (result.ok) await this.sendCurrent();
        return;
      }

      default: {
        const open = this.queue.current();
        const offered = open
          ? ACTIONS_FOR[open.item.kind].map((a) => actionLabel(a, open.item.kind)).join(" · ")
          : "queue · stop";
        await this.send(`Didn't catch that. Options: ${offered}. Or \`help\`.`);
      }
    }
  }
}

/**
 * Shared startup banner. Returns null when there is nothing to review, in which
 * case a messaging transport should print `emptyReason` and exit 0 rather than
 * opening a chat client to show somebody an empty queue.
 */
export function describeLoad(load: LoadResult): string | null {
  if (load.emptyReason) return null;
  const bits = [`${load.items.length} review(s) ready`];
  if (load.alreadyDecided > 0) bits.push(`${load.alreadyDecided} already answered`);
  if (load.dropped > 0) bits.push(`${load.dropped} with nothing to rule on`);
  return bits.join(" · ");
}
