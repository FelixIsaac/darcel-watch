/**
 * Darcel Watch -> Spectrum (Photon)
 *
 * Sends the top N review-queue items (out/results.json) to a volunteer as a
 * text message, and adjudicates their reply. This is the human-in-the-loop
 * step: the agent abstains on weak evidence and a human decides, over the
 * channel volunteers actually check -- iMessage/RCS/SMS/WhatsApp -- not a
 * dashboard.
 *
 * READ-ONLY SAFETY: this file never POSTs to askdarcel.org. A confirmed (Y)
 * reply is written to out/approved.json for a human to submit themselves.
 *
 * npm install spectrum-ts
 * Docs: https://photon.codes/docs/  (fetched live; exact method names below
 * marked verified where the docs page confirmed them, TODO where inferred)
 */

import { Spectrum } from "spectrum-ts";
// Provider helpers. imessage + terminal confirmed in docs sample; whatsapp
// confirmed to exist as a provider but its exact export name/shape (e.g.
// whatsapp() vs whatsapp.config()) was not shown on the fetched pages.
// TODO verify against docs
import { imessage } from "spectrum-ts/providers";
// TODO verify against docs -- whatsapp provider import path/name unconfirmed
import { whatsapp } from "spectrum-ts/providers";

import * as fs from "node:fs";
import * as path from "node:path";

const HERE = __dirname;
const RESULTS_PATH = path.join(HERE, "..", "out", "results.json");
const APPROVED_PATH = path.join(HERE, "..", "out", "approved.json");
const REJECTED_PATH = path.join(HERE, "..", "out", "rejected.json");

const TOP_N = Number(process.env.NOTIFY_TOP_N ?? 5);

interface Finding {
  field: string;
  stored?: string;
  live?: string;
  evidence_url?: string;
  evidence_quote?: string;
}

interface ChangeRequest {
  resource_id: number;
  field: string;
  current: string;
  proposed: string;
  source_url: string;
  source_quote?: string;
  submitted_by: string;
}

interface QueueItem {
  resource_id: number;
  name: string;
  priority: number;
  fields?: Finding[];
  change_request?: ChangeRequest | null;
}

interface ResultsPayload {
  queue: QueueItem[];
}

function loadTopQueueItems(n: number): QueueItem[] {
  if (!fs.existsSync(RESULTS_PATH)) {
    console.log(`[spectrum] no ${RESULTS_PATH} -- run python3 run.py first. Exiting cleanly.`);
    return [];
  }
  const payload: ResultsPayload = JSON.parse(fs.readFileSync(RESULTS_PATH, "utf-8"));
  const queue = payload.queue ?? [];
  return [...queue].sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0)).slice(0, n);
}

/** Same compact format as notify/dry_run.py -- keep the two in sync by hand. */
function formatMessage(item: QueueItem): string {
  const cr = item.change_request;
  const field = cr?.field ?? item.fields?.[0]?.field ?? "?";
  const stored = cr?.current ?? item.fields?.[0]?.stored ?? "?";
  const live = cr?.proposed ?? item.fields?.[0]?.live ?? "?";
  const url = cr?.source_url ?? item.fields?.[0]?.evidence_url ?? "?";

  const msg = [
    `Darcel Watch — review #${item.resource_id}`,
    `${item.name}`,
    `${field}: stored "${stored}" → live "${live}"`,
    `Source: ${url}`,
    `Reply Y confirm · N reject · S skip`,
  ].join("\n");

  // Keep the demo/format contract honest -- under 320 chars.
  return msg.length > 320 ? msg.slice(0, 317) + "..." : msg;
}

function appendJsonLine(filePath: string, record: unknown): void {
  const existing = fs.existsSync(filePath)
    ? JSON.parse(fs.readFileSync(filePath, "utf-8"))
    : [];
  existing.push(record);
  fs.writeFileSync(filePath, JSON.stringify(existing, null, 2));
}

async function main(): Promise<void> {
  const apiKey = process.env.PHOTON_API_KEY ?? process.env.SPECTRUM_API_KEY;
  const reviewerNumber = process.env.REVIEWER_PHONE;

  if (!apiKey || !reviewerNumber) {
    console.log(
      "[spectrum] Missing PHOTON_API_KEY/SPECTRUM_API_KEY or REVIEWER_PHONE. " +
        "Skipping live send -- run `python3 notify/dry_run.py` for a terminal preview."
    );
    return; // never crash -- exit 0
  }

  const queueItems = loadTopQueueItems(TOP_N);
  if (queueItems.length === 0) {
    console.log("[spectrum] Review queue is empty. Nothing to send.");
    return;
  }

  // Client construction. projectId/projectSecret confirmed as the config
  // shape in the docs sample; we map our env vars onto it.
  // TODO verify against docs -- confirm projectId/projectSecret are the
  // correct field names vs. a single apiKey field for this account tier.
  const spectrum = await Spectrum({
    projectId: process.env.PHOTON_PROJECT_ID ?? apiKey,
    projectSecret: apiKey,
    providers: [
      // iMessage with automatic RCS/SMS fallback -- confirmed pattern from
      // docs: imessage().fallback(otherProvider()).
      // TODO verify against docs -- exact fallback target for RCS/SMS (the
      // docs sample only showed .fallback(telegram())).
      imessage.config(),
      whatsapp.config(), // TODO verify against docs
    ],
  });

  // Track which resource_id each outbound message maps to, so an inbound
  // reply can be matched back to the right review item. Spectrum's inbound
  // message doesn't carry our app-level review id, so we key by sender.
  const pendingByReviewer = new Map<string, QueueItem>();

  for (const item of queueItems) {
    const text = formatMessage(item);
    try {
      // TODO verify against docs -- send() signature confirmed as
      // spectrum.send(address, content) in the docs sample
      // (`await spectrum.send("+123456789", sticker(...))`), but the docs
      // didn't show plain-text send or the fallback-chain call shape
      // together. Using the most literal reading here.
      await spectrum.send(reviewerNumber, imessage().fallback(whatsapp()), text);
      pendingByReviewer.set(reviewerNumber, item);
      console.log(`[spectrum] sent review #${item.resource_id} to ${reviewerNumber}`);
    } catch (err) {
      console.log(`[spectrum] send failed for #${item.resource_id}: ${err}`);
    }
  }

  // Inbound reply loop. Docs confirm `for await (const [space] of
  // app.messages)` as the receive pattern; we adapt the variable name
  // (spectrum -> app equivalent) since our client is bound to `spectrum`.
  // TODO verify against docs -- confirm `space.from` / sender-address field
  // name, and that message.type === "text" / message.content are correct.
  for await (const [space, message] of spectrum.messages) {
    // TODO verify against docs -- exact accessor for the sender's address.
    const from = (space as any).from ?? (space as any).address ?? reviewerNumber;
    const item = pendingByReviewer.get(from);
    if (!item) continue; // reply we don't have a pending review for -- ignore

    const bodyRaw = (message as any).content ?? (message as any).text ?? "";
    const body = String(bodyRaw).trim().toUpperCase();

    if (body.startsWith("Y")) {
      appendJsonLine(APPROVED_PATH, {
        ...item.change_request,
        decided_by: from,
        decided_at: new Date().toISOString(),
      });
      // NOTE: read-only against production. We never POST to askdarcel.org --
      // a human takes this approved change_request and submits it themselves.
      await space.send(`✓ Confirmed #${item.resource_id}. Filed for human submission.`);
    } else if (body.startsWith("N")) {
      appendJsonLine(REJECTED_PATH, {
        resource_id: item.resource_id,
        decided_by: from,
        decided_at: new Date().toISOString(),
      });
      await space.send(`✗ Rejected #${item.resource_id}. Thanks for checking.`);
    } else if (body.startsWith("S")) {
      // Skip -- no file write, item stays in the queue for next run.
      await space.send(`Skipped #${item.resource_id}.`);
    } else {
      await space.send(`Didn't catch that -- reply Y, N, or S for #${item.resource_id}.`);
      continue; // keep waiting on this one
    }

    pendingByReviewer.delete(from);
    if (pendingByReviewer.size === 0) break;
  }
}

main().catch((err) => {
  // Never crash the process -- log and exit 0 per spec.
  console.log(`[spectrum] unexpected error: ${err}`);
  process.exit(0);
});
