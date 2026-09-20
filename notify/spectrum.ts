/**
  * SF Service Guide Watch — the messaging transport.
 * =======================================
 *
 * Carries the review session (notify/session.ts) over Spectrum: the `terminal`
 * provider by default, iMessage and WhatsApp Business when credentials exist.
 *
 * This file is deliberately thin. It knows how to open a conversation and how
 * to pump messages between Spectrum and a `ReviewSession`; it knows nothing
 * about what a review is, how a decision is recorded, or what `Y` means. All of
 * that lives in session.ts and is shared with the web transport — so a review
 * answered on a phone and one answered in the browser take the identical code
 * path and write the identical change_request payload.
 *
 * ONE APPLICATION, SWAPPABLE TRANSPORT
 * ------------------------------------
 * With no credentials this runs on the `terminal` provider — a real Spectrum
 * client, a real message loop, real reply handling, rendered in a terminal chat
 * client instead of on a phone. Set SPECTRUM_PROJECT_ID / SPECTRUM_PROJECT_SECRET
 * (or the PHOTON_ spellings) plus REVIEWER_PHONE and the same code adds
 * `imessage` to the providers array instead.
 *
 * READ-ONLY AGAINST PRODUCTION: nothing here POSTs to askdarcel.org. There is
 * no HTTP client in this file. See notify/session.ts for where decisions go.
 */

import { Spectrum, text } from "spectrum-ts";
import { imessage, terminal, whatsappBusiness } from "spectrum-ts/providers";
import type { Content, Message, Space, SpectrumInstance } from "spectrum-ts";

import {
  describeLoad,
  loadQueue,
  ReviewQueue,
  ReviewSession,
  type Send,
} from "./session.ts";

// ---------------------------------------------------------------------------
// Reading the volunteer's reply off the wire
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

/** Adapt a Spectrum space into the session's `Send` seam. */
const sendVia =
  (space: Space): Send =>
  async (body) => {
    await space.send(text(body));
  };

// ---------------------------------------------------------------------------
// Choosing and building the transport
// ---------------------------------------------------------------------------

/** Either the Photon or the Spectrum spelling of a credential, or undefined. */
function credential(name: string): string | undefined {
  const value = process.env[`SPECTRUM_${name}`] ?? process.env[`PHOTON_${name}`];
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
 * the session logic is unaware of which branch was taken.
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
 * client. Both branches produce the same two values, which is why the session
 * logic never learns which one ran.
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

// ---------------------------------------------------------------------------
// The message loop
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const load = loadQueue();
  const banner = describeLoad(load);

  // Nothing to review is a normal outcome, not an error. Say so and exit 0
  // rather than opening a chat client to show somebody an empty queue.
  if (!banner) {
    console.log(`[shelflife] ${load.emptyReason}`);
    return;
  }

  const transport = chooseTransport();
  console.log(`[shelflife] ${banner} · transport: ${transport.description}`);

  const { spectrum, space } = await connect(transport);

  // One queue, shared by every volunteer in this process — the same class the
  // web server drives, so an answer here and an answer in the browser take the
  // identical path to the identical file.
  const queue = new ReviewQueue(
    load.items,
    transport.recipient,
    space.__platform,
  );
  const sessions = new Map<string, ReviewSession>();

  try {
    const session = new ReviewSession({
      queue,
      send: sendVia(space),
    });
    sessions.set(space.id, session);
    await session.start();

    // `spectrum.messages` yields `[space, message]` for every inbound message
    // across every configured provider, so this one loop serves iMessage,
    // WhatsApp and the terminal identically.
    for await (const [replySpace, message] of spectrum.messages as AsyncIterable<
      [Space, Message]
    >) {
      if (message.direction !== "inbound") continue;

      let active = sessions.get(replySpace.id);

      if (!active) {
        // Unknown space. Two possibilities, and they need opposite handling.
        //
        // (a) It's the volunteer we just messaged, replying on a space id that
        //     differs from the one `space.create()` returned. Rebind — do NOT
        //     start a second session, or the greeting repeats and the review
        //     already on their screen is stranded.
        // (b) It's genuinely a second reviewer messaging in. Give them their
        //     own session over the same queue.
        const unbound = [...sessions.entries()].find(
          ([, s]) => s.awaitingFirstReply && !s.ended,
        );

        if (unbound) {
          const [oldId, existing] = unbound;
          sessions.delete(oldId);
          existing.rebind(sendVia(replySpace));
          sessions.set(replySpace.id, existing);
          active = existing;
        } else {
          active = new ReviewSession({
            queue,
            send: sendVia(replySpace),
          });
          sessions.set(replySpace.id, active);
          await active.start();
          continue;
        }
      }

      active.markHeard();
      try {
        await active.handleText(contentToText(message.content));
      } catch (err) {
        // One bad reply must not take down the session for everyone else.
        console.error(`[shelflife] error handling reply: ${String(err)}`);
      }

      // Everyone who was reviewing has stopped, or the queue is drained.
      if ([...sessions.values()].every((s) => s.ended)) break;
    }
  } finally {
    // Closes provider connections and tears down the terminal client.
    await spectrum.stop();
  }
}

/**
 * Turn the one failure an operator is actually likely to hit into instructions.
 *
 * On Photon's Free/Pro plans a project sends through a shared pool of lines,
 * and a shared line will only message recipients registered as users of the
 * project. Everything else — credentials, provider, routing — can be perfectly
 * correct and the send still fails. A raw gRPC stack trace does not say that.
 */
function explain(err: unknown): string | null {
  const message = err instanceof Error ? err.message : String(err);
  if (!message.includes("Target not allowed for this project")) return null;

  return [
    "iMessage rejected the recipient: “Target not allowed for this project”.",
    "",
    "This is NOT an authentication failure and NOT a region problem — the",
    "project authenticated fine and the provider is enabled. On the Free/Pro",
    "plans Spectrum sends through a SHARED pool of lines, and a shared line",
    "will only message recipients registered as users of the project.",
    "",
    "Fix: add the recipient as a user, then run this again.",
    "  npx @photon-ai/cli login",
    "  npx @photon-ai/cli spectrum users add --phone <E.164> --first-name <name>",
    "or add them under Users at https://app.photon.codes",
    "",
    "Docs: https://photon.codes/docs/spectrum-ts/providers/imessage",
    "",
    "Meanwhile both other transports work with no credentials at all:",
    "  npm start     terminal chat client",
    "  npm run web   browser chat UI",
  ].join("\n");
}

main().catch((err: unknown) => {
  const hint = explain(err);
  if (hint) {
    console.error(`\n[shelflife] ${hint}\n`);
  } else {
    console.error(`[shelflife] ${err instanceof Error ? err.stack : String(err)}`);
  }
  process.exitCode = 1;
});
