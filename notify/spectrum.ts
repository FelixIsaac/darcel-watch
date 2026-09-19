/**
 * Darcel Watch — the volunteer review service, over messaging.
 * ============================================================
 *
 * WHAT THIS IS
 * ------------
 * Darcel Watch audits the SF Service Guide — the directory unhoused San
 * Franciscans use to find food, a shelter bed, a clinic, legal aid. Its agent
 * re-checks each listing against the organisation's own live website, and
 * deliberately ABSTAINS when the evidence is weak. Abstention and disagreement
 * are not failures; they are the two cases that need a human.
 *
 * ShelterTech's reviewers are volunteers, not staff sitting at desks. Another
 * dashboard is another thing nobody opens. So the review queue is delivered the
 * way a volunteer is actually reachable: as a MESSAGE.
 *
 * This file is that service. It hands a volunteer one review at a time, highest
 * priority first, and reads their reply:
 *
 *     Y  confirm    the agent is right, file the change
 *     N  reject     the agent is wrong, drop it
 *     S  skip       not sure / not now, leave it in the queue
 *     ?  evidence   show the quote, the URLs fetched, the blast radius
 *
 * plus `queue` (how many left), `help`, and `stop` (end the session).
 *
 * READ-ONLY AGAINST PRODUCTION  <-- the load-bearing safety property
 * -----------------------------------------------------------------
 * This process NEVER POSTs to askdarcel.org. It never writes to ShelterTech's
 * API, database, or anything they operate. There is no HTTP client in this file
 * at all. A confirmed review is appended to out/approved.json as a
 * `change_request` payload, shaped for the endpoint their volunteers already
 * use — and a human submits it. The agent proposes; a volunteer judges; a human
 * with an account presses the button. Three steps, and we only own the first.
 *
 * ONE APPLICATION, SWAPPABLE TRANSPORT
 * ------------------------------------
 * The whole app runs today with ZERO credentials, on the `terminal` provider —
 * a real Spectrum client, a real message loop, real reply handling, rendered in
 * a terminal chat UI instead of a phone. Set PHOTON_PROJECT_ID and
 * PHOTON_PROJECT_SECRET and the exact same code adds `imessage` (and
 * `whatsapp-business` if configured) to the providers array. Not a simulation
 * with a real mode bolted on: one code path, different transport.
 *
 * See notify/README.md to run it.
 */

import { Spectrum, text } from "spectrum-ts";
import { imessage, terminal, whatsappBusiness } from "spectrum-ts/providers";
import type { Content, Message, Space, SpectrumInstance } from "spectrum-ts";

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.join(HERE, "..", "out");
const RESULTS_PATH = path.join(OUT_DIR, "results.json");
const APPROVED_PATH = path.join(OUT_DIR, "approved.json");
const REJECTED_PATH = path.join(OUT_DIR, "rejected.json");

/** Stamped onto every filed decision so the provenance is never ambiguous. */
const NOT_SUBMITTED_NOTE =
  "Recorded locally only. Darcel Watch never POSTs to askdarcel.org — " +
  "a human must submit this change request.";

// ---------------------------------------------------------------------------
// 1. The queue: reading out/results.json without trusting it
// ---------------------------------------------------------------------------

/** A change request in the shape ShelterTech's volunteers already work with. */
interface ChangeRequest {
  resource_id: number | string;
  field: string;
  current: string;
  proposed: string;
  source_url: string;
  source_quote?: string;
  submitted_by: string;
}

/**
 * One review, already normalised. Everything here is a known-good value — the
 * messy optional-field handling happens once, in `normalise()`, so the message
 * builders below never have to defend themselves.
 */
interface ReviewItem {
  resourceId: number | string;
  name: string;
  field: string;
  stored: string;
  live: string;
  sourceUrl: string;
  sourceQuote: string;
  verdict: string;
  reason: string;
  confidence: number | null;
  fetched: string[];
  blastRadiusSize: number;
  blastRadiusServices: string[];
  priority: number;
  changeRequest: ChangeRequest;
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** Coerce anything into a trimmed string; `fallback` for null/undefined/"". */
function str(v: unknown, fallback = ""): string {
  if (v === null || v === undefined) return fallback;
  if (typeof v === "string") return v.trim() || fallback;
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

/**
 * Turn one raw queue entry into a `ReviewItem`, or return null to drop it.
 *
 * This is the "never crash on a malformed entry" boundary. A queue entry with
 * no identifiable resource, or with no field/value pair to review, is not
 * actionable by a volunteer — showing it would waste the scarcest resource in
 * this whole system, which is a volunteer's attention. So it is skipped and
 * counted, not rendered.
 */
function normalise(raw: unknown): ReviewItem | null {
  if (!isRecord(raw)) return null;

  const resourceId =
    typeof raw.resource_id === "number" || typeof raw.resource_id === "string"
      ? raw.resource_id
      : null;
  if (resourceId === null || str(resourceId) === "") return null;

  const cr = isRecord(raw.change_request) ? raw.change_request : {};
  // `fields` is the agent's raw evidence; change_request is its distilled
  // proposal. Prefer the proposal, fall back to the first evidence row.
  const firstField =
    Array.isArray(raw.fields) && isRecord(raw.fields[0]) ? raw.fields[0] : {};

  const field = str(cr.field) || str(firstField.field);
  const stored = str(cr.current) || str(firstField.stored);
  const live = str(cr.proposed) || str(firstField.live);
  // Nothing to decide about → not a review.
  if (!field || (!stored && !live)) return null;

  const sourceUrl = str(cr.source_url) || str(firstField.evidence_url);
  const sourceQuote = str(cr.source_quote) || str(firstField.evidence_quote);

  const blast = isRecord(raw.blast_radius) ? raw.blast_radius : {};
  const blastServices = Array.isArray(blast.services)
    ? blast.services.map((s) => (isRecord(s) ? str(s.name) : "")).filter(Boolean)
    : [];

  return {
    resourceId,
    name: str(raw.name, "Unknown organisation"),
    field,
    stored: stored || "(none stored)",
    live: live || "(nothing found live)",
    sourceUrl,
    sourceQuote,
    verdict: str(raw.verdict, "review"),
    reason: str(raw.reason),
    confidence: typeof raw.confidence === "number" ? raw.confidence : null,
    fetched: strList(raw.fetched),
    blastRadiusSize: num(blast.size, blastServices.length),
    blastRadiusServices: blastServices,
    priority: num(raw.priority, 0),
    changeRequest: {
      resource_id: resourceId,
      field,
      current: stored,
      proposed: live,
      source_url: sourceUrl,
      ...(sourceQuote ? { source_quote: sourceQuote } : {}),
      submitted_by: str(
        cr.submitted_by,
        "darcel-watch (agent, human review required)",
      ),
    },
  };
}

/**
 * Resource ids a volunteer has already confirmed or rejected in an earlier run.
 *
 * Decisions are durable, so the queue has to be. Without this, every restart
 * hands the volunteer the same listings they already ruled on — and each pass
 * appends another change request for a change that was already filed.
 *
 * Skips are deliberately NOT durable: skipping means "not me, not now".
 */
function loadDecidedIds(): Set<string> {
  const decided = new Set<string>();

  for (const file of [APPROVED_PATH, REJECTED_PATH]) {
    if (!fs.existsSync(file)) continue;
    try {
      const parsed: unknown = JSON.parse(fs.readFileSync(file, "utf8"));
      if (!Array.isArray(parsed)) continue;
      for (const row of parsed) {
        if (isRecord(row) && row.resource_id !== undefined) {
          decided.add(str(row.resource_id));
        }
      }
    } catch {
      // An unreadable decision log shouldn't block the queue. It gets moved
      // aside on the next write; until then, treat it as holding nothing.
    }
  }

  return decided;
}

interface LoadResult {
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
 * Both abstentions and discrepancies are reviewable: `queue` holds the listings
 * where stored data contradicts the live site, `abstained` holds the ones where
 * the agent refused to call it. Both need a human, so both are offered.
 */
function loadQueue(): LoadResult {
  if (!fs.existsSync(RESULTS_PATH)) {
    return {
      items: [],
      dropped: 0,
      alreadyDecided: 0,
      emptyReason:
        `No ${path.relative(process.cwd(), RESULTS_PATH)} found.\n` +
        "Run the audit first:  BUDGET=12 python3 run.py",
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
        `(${(err as Error).message}).\nRe-run:  BUDGET=12 python3 run.py`,
    };
  }

  const root = isRecord(payload) ? payload : {};
  const raw = [
    ...(Array.isArray(root.queue) ? root.queue : []),
    ...(Array.isArray(root.abstained) ? root.abstained : []),
  ];

  const decided = loadDecidedIds();

  const items: ReviewItem[] = [];
  let dropped = 0;
  let alreadyDecided = 0;
  for (const entry of raw) {
    const item = normalise(entry);
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
          `listing(s) have already been ruled on. See out/approved.json ` +
          `and out/rejected.json.`
        : "The review queue is empty — every audited listing either matched " +
          "its live site or produced nothing a human could act on.";
  }

  return { items, dropped, alreadyDecided, emptyReason };
}

// ---------------------------------------------------------------------------
// 2. Recording decisions (local files only — never the network)
// ---------------------------------------------------------------------------

/**
 * Append one decision to a JSON array file, creating it if needed.
 *
 * If the file exists but is unreadable, it is moved aside rather than
 * overwritten. Losing a volunteer's earlier decisions to a parse error would be
 * worse than leaving a stray file on disk.
 */
function appendDecision(filePath: string, record: unknown): void {
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
      console.warn(`[darcel-watch] ${filePath} was unreadable; moved to ${aside}`);
      existing = [];
    }
  }

  existing.push(record);
  fs.writeFileSync(filePath, `${JSON.stringify(existing, null, 2)}\n`, "utf8");
}

type Verdict = "confirmed" | "rejected";

function fileDecision(
  item: ReviewItem,
  verdict: Verdict,
  decidedBy: string,
  platform: string,
): void {
  const target = verdict === "confirmed" ? APPROVED_PATH : REJECTED_PATH;
  appendDecision(target, {
    // The payload a human will submit by hand. Read-only service: we produce
    // this file and stop. Nothing in this process talks to askdarcel.org.
    change_request: item.changeRequest,
    decision: verdict,
    resource_id: item.resourceId,
    name: item.name,
    agent_verdict: item.verdict,
    agent_confidence: item.confidence,
    blast_radius_size: item.blastRadiusSize,
    decided_by: decidedBy,
    decided_via: platform,
    decided_at: new Date().toISOString(),
    submitted_to_askdarcel: false,
    note: NOT_SUBMITTED_NOTE,
  });
}

// ---------------------------------------------------------------------------
// 3. What the volunteer actually reads
// ---------------------------------------------------------------------------

/** Keep quotes phone-sized; a wall of scraped text is unreadable on a lock screen. */
function clip(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, max - 1).trimEnd()}…`;
}

/**
 * The one-line framing of what the volunteer is being asked.
 *
 * A discrepancy and an abstention are different questions, and a volunteer who
 * can't tell them apart can't answer either one well. On a discrepancy the
 * agent is asserting a mismatch and `Y` endorses its proposed value. On an
 * abstention the agent refused to call it at all — `Y` is the human supplying
 * the judgement the agent declined to make.
 */
function askLine(item: ReviewItem): string {
  if (item.verdict === "abstain") {
    return "The agent ABSTAINED here — evidence too weak to call.";
  }
  if (item.verdict === "discrepancy") {
    return "The agent found the stored value contradicted by the live site.";
  }
  return "Needs a human.";
}

function reviewCard(item: ReviewItem, position: number, total: number): string {
  const lines = [
    `Darcel Watch · review ${position} of ${total}`,
    `${item.name} (#${item.resourceId})`,
    "",
    askLine(item),
    "",
    `${item.field}`,
    `  stored: ${clip(item.stored, 80)}`,
    `  live:   ${clip(item.live, 80)}`,
  ];

  // Worth calling out: it means the agent read the page and still wouldn't
  // commit, so "they match" is exactly the judgement being asked for.
  if (item.stored === item.live) {
    lines.push("  (these match — the agent still wouldn't commit)");
  }

  if (item.blastRadiusSize > 0) {
    lines.push(
      "",
      `If this is wrong, ${item.blastRadiusSize} downstream listing(s) are wrong too.`,
    );
  }
  if (item.sourceUrl) lines.push(`Source: ${item.sourceUrl}`);

  lines.push("", "Y confirm · N reject · S skip · ? evidence");
  return lines.join("\n");
}

function evidenceCard(item: ReviewItem): string {
  const lines = [`Evidence · #${item.resourceId} ${item.name}`, ""];

  const confidence =
    item.confidence === null ? "" : ` (confidence ${item.confidence.toFixed(2)})`;
  lines.push(`Agent verdict: ${item.verdict}${confidence}`);
  if (item.reason) lines.push(`Reasoning: ${item.reason}`);

  if (item.sourceQuote) {
    lines.push("", `Quoted from the live page:`, `  "${clip(item.sourceQuote, 260)}"`);
  }

  if (item.fetched.length > 0) {
    lines.push("", "Pages the agent fetched:");
    for (const url of item.fetched) lines.push(`  · ${url}`);
  }

  if (item.blastRadiusServices.length > 0) {
    lines.push("", `Blast radius (${item.blastRadiusSize}):`);
    for (const name of item.blastRadiusServices.slice(0, 6)) {
      lines.push(`  · ${clip(name, 60)}`);
    }
    const hidden = item.blastRadiusServices.length - 6;
    if (hidden > 0) lines.push(`  · …and ${hidden} more`);
  }

  lines.push("", "Still your call: Y confirm · N reject · S skip");
  return lines.join("\n");
}

const HELP_TEXT = [
  "Darcel Watch commands",
  "",
  "  Y      confirm — the agent is right, file the change request",
  "  N      reject  — the agent is wrong, drop it",
  "  S      skip    — leave it in the queue for someone else",
  "  ?      show the evidence behind this review",
  "",
  "  queue  how many reviews are left",
  "  help   this message",
  "  stop   end the session",
  "",
  "Confirmed reviews are saved locally for a human to submit.",
  "Darcel Watch never writes to sfserviceguide.org.",
].join("\n");

// ---------------------------------------------------------------------------
// 4. Reading the volunteer's reply
// ---------------------------------------------------------------------------

/**
 * Pull plain text out of inbound `Content`.
 *
 * `Content` is a discriminated union over `type` — a volunteer might send text,
 * markdown, or a reply wrapping either. Anything else (a photo, a voice note, a
 * reaction) carries no command, so it reads as empty and gets the nudge.
 */
function contentToText(content: Content): string {
  if (content.type === "text") return content.text;
  if (content.type === "markdown") return content.markdown;
  if (content.type === "reply") return contentToText(content.content);
  return "";
}

type Command =
  | { kind: "confirm" }
  | { kind: "reject" }
  | { kind: "skip" }
  | { kind: "evidence" }
  | { kind: "queue" }
  | { kind: "help" }
  | { kind: "stop" }
  | { kind: "unknown" };

/**
 * Parse a reply into a command.
 *
 * Deliberately forgiving. A volunteer answering from a bus stop types "yes",
 * "y.", "Y 👍" or "/queue" — all of those are unambiguous to a human, so they
 * should be unambiguous here. The `/`-prefixed forms exist because the terminal
 * provider offers them as slash-command autocomplete.
 */
function parseCommand(raw: string): Command {
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
  if (/^y(es|ep|eah)?[.!]?$/.test(word)) return { kind: "confirm" };
  if (/^n(o|ope|ah)?[.!]?$/.test(word)) return { kind: "reject" };
  if (/^s(kip)?[.!]?$/.test(word)) return { kind: "skip" };

  return { kind: "unknown" };
}

// ---------------------------------------------------------------------------
// 5. The queue pool and one volunteer's session
// ---------------------------------------------------------------------------

/**
 * Hands out reviews, one at a time, and makes sure two volunteers working at
 * once never get handed the same listing. A skipped item is released back so
 * somebody else can pick it up.
 */
class QueuePool {
  private readonly items: ReviewItem[];
  private readonly claimed = new Set<number>();
  private readonly settled = new Set<number>();

  constructor(items: ReviewItem[]) {
    this.items = items;
  }

  get total(): number {
    return this.items.length;
  }

  /** Reviews nobody has decided on yet — what `queue` reports. */
  get remaining(): number {
    return this.items.length - this.settled.size;
  }

  /**
   * Take the highest-priority review nobody is holding.
   *
   * `avoid` carries the indices this volunteer already skipped. Without it, a
   * lone reviewer who skips the last open item is handed it straight back —
   * "skip" has to mean "not me", or it means nothing.
   */
  claim(avoid: ReadonlySet<number> = new Set()): {
    item: ReviewItem;
    index: number;
  } | null {
    for (let i = 0; i < this.items.length; i++) {
      if (this.claimed.has(i) || this.settled.has(i) || avoid.has(i)) continue;
      const item = this.items[i];
      if (!item) continue;
      this.claimed.add(i);
      return { item, index: i };
    }
    return null;
  }

  settle(index: number): void {
    this.claimed.delete(index);
    this.settled.add(index);
  }

  release(index: number): void {
    this.claimed.delete(index);
  }

  /** Position of a review in the original ranked order, for "review 2 of 7". */
  position(index: number): number {
    return index + 1;
  }
}

/** One volunteer's conversation. Keyed by space id, so several can run at once. */
class ReviewSession {
  private current: { item: ReviewItem; index: number } | null = null;
  private confirmed = 0;
  private rejected = 0;
  private skipped = 0;
  ended = false;

  /** Indices this volunteer passed on, so `skip` doesn't hand them back. */
  private readonly skippedIndices = new Set<number>();

  /** Whether this volunteer has ever replied — the window for rebinding. */
  private heardFrom = false;

  // Written out longhand rather than as constructor parameter properties:
  // Node runs this file by stripping types, and parameter properties are
  // syntax that would have to be *rewritten*, not stripped.
  private space: Space;
  private readonly pool: QueuePool;
  private reviewerId: string;

  constructor(space: Space, pool: QueuePool, reviewerId: string) {
    this.space = space;
    this.pool = pool;
    this.reviewerId = reviewerId;
  }

  /**
   * Point this session at the space the volunteer actually replied from.
   *
   * Outbound and inbound space ids are not guaranteed to match. The terminal
   * provider is a live example: `space.create()` mints `chat-1`, but replies
   * from the plain-readline client arrive on a space called `terminal`. Without
   * rebinding, the first reply looks like a brand-new volunteer — the session
   * forks, the greeting repeats, and the review already on screen is stranded
   * as claimed-but-never-decided.
   */
  rebind(space: Space, reviewerId: string): void {
    this.space = space;
    this.reviewerId = reviewerId;
  }

  /** True until the volunteer has said anything — the window for rebinding. */
  get awaitingFirstReply(): boolean {
    return !this.heardFrom;
  }

  markHeard(): void {
    this.heardFrom = true;
  }

  private send(body: string): Promise<unknown> {
    return this.space.send(text(body));
  }

  async start(): Promise<void> {
    await this.send(
      [
        "Darcel Watch — SF Service Guide review queue",
        "",
        `${this.pool.total} listing(s) need a human. The agent checked each one`,
        "against the organisation's own live website and either disagreed with",
        "the stored data or refused to call it.",
        "",
        "Reply Y, N, S or ? — `help` for the full list.",
      ].join("\n"),
    );
    await this.sendNext();
  }

  /** Claim and present the next review, or close out if the queue is drained. */
  private async sendNext(): Promise<void> {
    const next = this.pool.claim(this.skippedIndices);
    if (!next) {
      this.current = null;
      const headline =
        this.skippedIndices.size > 0
          ? `Nothing left for you — the ${this.skippedIndices.size} you skipped ` +
            "stay open for another reviewer."
          : "That's the whole queue — nothing left to review.";
      await this.send([headline, "", this.summary()].join("\n"));
      this.ended = true;
      return;
    }
    this.current = next;
    await this.send(
      reviewCard(next.item, this.pool.position(next.index), this.pool.total),
    );
  }

  private summary(): string {
    const lines = [
      `You reviewed ${this.confirmed + this.rejected} listing(s): ` +
        `${this.confirmed} confirmed, ${this.rejected} rejected` +
        (this.skipped > 0 ? `, ${this.skipped} skipped` : "") +
        ".",
    ];
    if (this.confirmed > 0) {
      lines.push(
        "",
        `Confirmed change requests are in out/approved.json.`,
        "Nothing was submitted to sfserviceguide.org — that's a human's call.",
      );
    }
    lines.push("", "Thank you. This is the expensive part of keeping the guide true.");
    return lines.join("\n");
  }

  /** Route one inbound reply. Returns once any outbound response has been sent. */
  async handle(command: Command, platform: string): Promise<void> {
    if (this.ended) return;

    switch (command.kind) {
      case "help":
        await this.send(HELP_TEXT);
        return;

      case "queue":
        await this.send(
          `${this.pool.remaining} of ${this.pool.total} review(s) still open.`,
        );
        return;

      case "stop":
        if (this.current) this.pool.release(this.current.index);
        this.current = null;
        this.ended = true;
        await this.send(["Session ended.", "", this.summary()].join("\n"));
        return;

      case "evidence":
        if (!this.current) {
          await this.send("No review open right now.");
          return;
        }
        await this.send(evidenceCard(this.current.item));
        return;

      case "confirm":
      case "reject": {
        if (!this.current) {
          await this.send("No review open right now. Reply `queue` or `stop`.");
          return;
        }
        const { item, index } = this.current;
        const verdict: Verdict =
          command.kind === "confirm" ? "confirmed" : "rejected";

        // The only write this process performs. Local file, no network.
        fileDecision(item, verdict, this.reviewerId, platform);
        this.pool.settle(index);
        if (verdict === "confirmed") this.confirmed++;
        else this.rejected++;

        await this.send(
          verdict === "confirmed"
            ? `Confirmed #${item.resourceId}. Change request filed to ` +
                `out/approved.json for a human to submit.`
            : `Rejected #${item.resourceId}. Logged to out/rejected.json — ` +
                `the agent was wrong, and that's worth knowing.`,
        );
        await this.sendNext();
        return;
      }

      case "skip": {
        if (!this.current) {
          await this.send("No review open right now. Reply `queue` or `stop`.");
          return;
        }
        // Released, not settled — it goes back in the pool for someone else,
        // but this session won't be offered it again.
        this.pool.release(this.current.index);
        this.skippedIndices.add(this.current.index);
        this.skipped++;
        await this.send(`Skipped #${this.current.item.resourceId}.`);
        this.current = null;
        await this.sendNext();
        return;
      }

      default:
        await this.send(
          "Didn't catch that. Reply Y, N, S or ? — or `help`.",
        );
    }
  }
}

// ---------------------------------------------------------------------------
// 6. Wiring: providers, client, message loop
// ---------------------------------------------------------------------------

/** Either the Photon or the Spectrum spelling of a credential, or undefined. */
function credential(name: string): string | undefined {
  const value = process.env[`PHOTON_${name}`] ?? process.env[`SPECTRUM_${name}`];
  return value && value.trim() ? value.trim() : undefined;
}

/** WhatsApp Business credentials, when the operator has supplied them. */
interface WhatsAppConfig {
  accessToken: string;
  phoneNumberId: string;
  appSecret?: string;
}

/**
 * How we're going to reach the volunteer. A discriminated union rather than a
 * bag of optional fields, so each branch below is separately, fully typed —
 * `Spectrum()` infers its provider tuple from the literal array it is given,
 * and widening that array to `PlatformProviderConfig[]` would throw the
 * inference away and break `imessage(spectrum)`.
 */
type Transport =
  | { mode: "terminal"; recipient: string; description: string }
  | {
      mode: "imessage";
      projectId: string;
      projectSecret: string;
      recipient: string;
      whatsapp: WhatsAppConfig | null;
      description: string;
    };

/**
 * Decide how to reach the volunteer.
 *
 * No credentials → the `terminal` provider. That is not a fallback or a mock:
 * it is a first-class Spectrum platform that renders a real chat client, and
 * everything above this line is unaware of which branch was taken.
 */
function chooseTransport(): Transport {
  const projectId = credential("PROJECT_ID");
  const projectSecret = credential("PROJECT_SECRET");
  const reviewerPhone = process.env.REVIEWER_PHONE?.trim();

  if (projectId && projectSecret && reviewerPhone) {
    const accessToken = process.env.WHATSAPP_ACCESS_TOKEN?.trim();
    const phoneNumberId = process.env.WHATSAPP_PHONE_NUMBER_ID?.trim();
    const appSecret = process.env.WHATSAPP_APP_SECRET?.trim();

    const whatsapp: WhatsAppConfig | null =
      accessToken && phoneNumberId
        ? { accessToken, phoneNumberId, ...(appSecret ? { appSecret } : {}) }
        : null;

    return {
      mode: "imessage",
      projectId,
      projectSecret,
      recipient: reviewerPhone,
      whatsapp,
      description:
        `iMessage → ${reviewerPhone}` +
        (whatsapp ? " (WhatsApp Business also configured)" : ""),
    };
  }

  return {
    mode: "terminal",
    recipient: "volunteer",
    description: "terminal (no credentials required)",
  };
}

/** What the app needs from a transport: a client to listen on, and a chat. */
interface Connection {
  spectrum: SpectrumInstance;
  space: Space;
}

/**
 * Build the Spectrum client and open the conversation.
 *
 * `space.create(user)` resolves-or-creates the 1:1 chat on whichever platform
 * is primary — a phone number for iMessage, a chat handle for the terminal
 * client. Both branches produce the same two values, which is why the review
 * logic above never learns which one ran.
 */
async function connect(transport: Transport): Promise<Connection> {
  if (transport.mode === "imessage") {
    // Authenticated overload: projectId + projectSecret.
    //
    // The two provider tuples are built in separate branches rather than by
    // pushing onto one array. `Spectrum()` infers its `Providers` tuple from
    // the literal it receives, and `imessage(spectrum)` is only typed as an
    // iMessage instance when that inference survives — a union of two client
    // types defeats the overload, and a widened array erases it entirely.
    if (transport.whatsapp) {
      const spectrum = await Spectrum({
        projectId: transport.projectId,
        projectSecret: transport.projectSecret,
        providers: [
          imessage.config(),
          whatsappBusiness.config(transport.whatsapp),
        ],
      });
      const space = await imessage(spectrum).space.create(transport.recipient);
      return { spectrum, space };
    }

    const spectrum = await Spectrum({
      projectId: transport.projectId,
      projectSecret: transport.projectSecret,
      providers: [imessage.config()],
    });
    const space = await imessage(spectrum).space.create(transport.recipient);
    return { spectrum, space };
  }

  // Credential-free overload: `Spectrum({ providers })`, where projectId and
  // projectSecret are typed `never` — supported, not a degraded mode.
  const spectrum = await Spectrum({
    providers: [
      terminal.config({
        commands: [
          { name: "/queue", description: "How many reviews are left" },
          { name: "/help", description: "Show the commands" },
          { name: "/stop", description: "End the review session" },
        ],
      }),
    ],
  });

  const space = await terminal(spectrum).space.create(transport.recipient);
  return { spectrum, space };
}

async function main(): Promise<void> {
  const { items, dropped, alreadyDecided, emptyReason } = loadQueue();

  if (dropped > 0) {
    // Almost always abstentions where the agent reached no readable evidence
    // at all (dead site, JS-only shell). Real, and reported upstream as the
    // abstention count — but there is nothing here for a volunteer to decide.
    console.warn(
      `[darcel-watch] ${dropped} entr${dropped === 1 ? "y" : "ies"} had no ` +
        `field a volunteer could rule on — not queued.`,
    );
  }

  // Nothing to review is a normal outcome, not an error. Say so and exit 0
  // rather than opening a chat client to show somebody an empty queue.
  if (emptyReason) {
    console.log(`[darcel-watch] ${emptyReason}`);
    return;
  }

  const transport = chooseTransport();
  console.log(
    `[darcel-watch] ${items.length} review(s) ready · transport: ${transport.description}`,
  );

  const { spectrum, space } = await connect(transport);

  const pool = new QueuePool(items);
  const sessions = new Map<string, ReviewSession>();

  try {
    const session = new ReviewSession(space, pool, transport.recipient);
    sessions.set(space.id, session);
    await session.start();

    // The message loop. `spectrum.messages` yields `[space, message]` for every
    // inbound message across every configured provider, so this one loop serves
    // iMessage and the terminal identically.
    for await (const [replySpace, message] of spectrum.messages as AsyncIterable<
      [Space, Message]
    >) {
      if (message.direction !== "inbound") continue;

      const senderId = message.sender?.id ?? replySpace.id;
      let active = sessions.get(replySpace.id);

      if (!active) {
        // Unknown space. Two possibilities, and they need opposite handling.
        //
        // (a) It's the volunteer we just messaged, replying on a space id that
        //     differs from the one `space.create()` returned. Rebind — do NOT
        //     start a second session, or the greeting repeats and the review
        //     already on their screen is stranded.
        // (b) It's genuinely a second reviewer messaging in. Give them their
        //     own session over the same pool.
        const unbound = [...sessions.entries()].find(
          ([, s]) => s.awaitingFirstReply && !s.ended,
        );

        if (unbound) {
          const [oldId, session] = unbound;
          sessions.delete(oldId);
          session.rebind(replySpace, senderId);
          sessions.set(replySpace.id, session);
          active = session;
        } else {
          active = new ReviewSession(replySpace, pool, senderId);
          sessions.set(replySpace.id, active);
          await active.start();
          continue;
        }
      }

      active.markHeard();
      const body = contentToText(message.content);
      try {
        await active.handle(parseCommand(body), replySpace.__platform);
      } catch (err) {
        // One bad reply must not take down the session for everyone else.
        console.error(`[darcel-watch] error handling reply: ${String(err)}`);
      }

      // Everyone who was reviewing has stopped, or the queue is drained.
      const allDone = [...sessions.values()].every((s) => s.ended);
      if (allDone) break;
    }
  } finally {
    // Closes provider connections and tears down the terminal client.
    await spectrum.stop();
  }
}

main().catch((err: unknown) => {
  console.error(`[darcel-watch] ${err instanceof Error ? err.stack : String(err)}`);
  process.exitCode = 1;
});
