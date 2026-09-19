/**
  * SF Service Guide Watch — the web transport.
 * =================================
 *
 * A small `node:http` server that carries the same review session
 * (notify/session.ts) to a browser instead of a phone. No framework, no build
 * step, no dependencies beyond the Node standard library.
 *
 * The UI at ui/review.html is a messaging thread, not a dashboard — because
 * that is the point of the project. The review reaches the volunteer as a
 * conversation, and the browser is just one more way for that conversation to
 * arrive.
 *
 * Because session.ts is shared, a review answered here and one answered over
 * iMessage take the identical code path and write the identical change_request
 * payload. This file contributes a `Send` that appends to a transcript and an
 * HTTP route that feeds replies back in. That is all it does.
 *
 * WHY POLLING, NOT SSE
 * --------------------
 * The transcript is exposed as a monotonic log and the client asks for
 * "everything after N". That is stateless on both ends: no hanging connection
 * to be buffered by a proxy, no reconnect bookkeeping, no duplicate-delivery
 * window, and a client that was asleep in a backgrounded phone tab catches up
 * correctly on its next poll. For a local demo with a handful of messages the
 * latency difference is imperceptible, and the failure modes are gone.
 *
 * READ-ONLY AGAINST PRODUCTION: this server binds to localhost, serves two
 * files and two JSON routes, and never contacts askdarcel.org.
 */

import * as fs from "node:fs";
import * as http from "node:http";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  describeLoad,
  loadQueue,
  QueuePool,
  ReviewSession,
} from "./session.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const UI_PATH = path.join(HERE, "..", "ui", "review.html");

const PORT = Number(process.env.PORT ?? 8778);
const HOST = process.env.HOST ?? "127.0.0.1";

// ---------------------------------------------------------------------------
// The transcript: an append-only log the browser catches up against
// ---------------------------------------------------------------------------

interface Bubble {
  /** Monotonic, 1-based. The client polls with `?since=<last seq it has>`. */
  seq: number;
  from: "agent" | "volunteer";
  body: string;
  at: string;
}

const transcript: Bubble[] = [];

function append(from: Bubble["from"], body: string): void {
  transcript.push({
    seq: transcript.length + 1,
    from,
    body,
    at: new Date().toISOString(),
  });
}

// ---------------------------------------------------------------------------
// Session wiring
// ---------------------------------------------------------------------------

const load = loadQueue();
const banner = describeLoad(load);

/**
 * An empty queue still gets a server and a page.
 *
 * On the messaging transports there is nobody to talk to, so they print the
 * reason and exit. Here somebody has typed a URL and is looking at a browser —
 * handing them a connection error to explain "there was nothing to review"
 * is the worst way to say it. The session simply starts finished.
 */
const isEmpty = !banner;

const pool = new QueuePool(load.items);

const session = new ReviewSession({
  pool,
  // The `Send` seam. On the messaging transport this is `space.send(text(…))`;
  // here it is one line appending to the log the browser reads.
  send: async (body) => {
    append("agent", body);
  },
  reviewerId: "web-reviewer",
  platform: "web",
  // Every action is a button on screen, so reviews don't repeat the key hints.
  affordances: "buttons",
});

/**
 * Replies are serialised through a promise chain.
 *
 * Two taps in quick succession (or two open tabs) would otherwise interleave
 * inside `handle()` — both could read the same `current` review and file two
 * decisions for one listing. Queueing them makes the ordering the volunteer saw
 * the ordering that gets recorded.
 */
let queue: Promise<void> = Promise.resolve();

function submitReply(body: string): Promise<void> {
  queue = queue.then(async () => {
    if (session.ended) return;
    append("volunteer", body);
    try {
      await session.handleText(body);
    } catch (err) {
      console.error(`[sfsg-watch] error handling reply: ${String(err)}`);
      append("agent", "Something went wrong handling that. Try again.");
    }
  });
  return queue;
}

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------

function sendJson(res: http.ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    // The transcript changes on every reply; never let a proxy or the browser
    // serve a stale one.
    "cache-control": "no-store",
  });
  res.end(payload);
}

function readBody(req: http.IncomingMessage, limitBytes = 64 * 1024): Promise<string> {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > limitBytes) {
        reject(new Error("request body too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

/** Everything the client needs to render, in one response. */
function stateSince(since: number): {
  messages: Bubble[];
  ended: boolean;
  empty: boolean;
  total: number;
  remaining: number;
  reviewed: number;
  latest: number;
} {
  return {
    messages: transcript.filter((m) => m.seq > since),
    ended: session.ended,
    empty: isEmpty,
    total: pool.total,
    remaining: pool.remaining,
    // What the progress indicator counts: decided, not merely seen.
    reviewed: pool.total - pool.remaining,
    latest: transcript.length,
  };
}

const server = http.createServer((req, res) => {
  void (async () => {
    const url = new URL(req.url ?? "/", `http://${req.headers.host ?? HOST}`);

    try {
      // --- the UI -------------------------------------------------------
      if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
        if (!fs.existsSync(UI_PATH)) {
          res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
          res.end(`Missing ${UI_PATH}`);
          return;
        }
        const html = fs.readFileSync(UI_PATH);
        res.writeHead(200, {
          "content-type": "text/html; charset=utf-8",
          "content-length": html.byteLength,
          "cache-control": "no-store",
        });
        res.end(html);
        return;
      }

      // --- catch up on the transcript ------------------------------------
      if (req.method === "GET" && url.pathname === "/api/state") {
        const raw = Number(url.searchParams.get("since") ?? "0");
        const since = Number.isFinite(raw) && raw >= 0 ? raw : 0;
        sendJson(res, 200, stateSince(since));
        return;
      }

      // --- send a reply ---------------------------------------------------
      if (req.method === "POST" && url.pathname === "/api/reply") {
        let text: unknown;
        try {
          const parsed: unknown = JSON.parse(await readBody(req));
          text = (parsed as { text?: unknown } | null)?.text;
        } catch {
          sendJson(res, 400, { error: "expected JSON { text: string }" });
          return;
        }

        if (typeof text !== "string" || !text.trim()) {
          sendJson(res, 400, { error: "text must be a non-empty string" });
          return;
        }

        // Cap it: this is a command word, not an essay.
        await submitReply(text.trim().slice(0, 500));
        sendJson(res, 200, stateSince(0));
        return;
      }

      res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
      res.end("Not found");
    } catch (err) {
      console.error(`[sfsg-watch] request failed: ${String(err)}`);
      if (!res.headersSent) {
        res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      }
      res.end("Internal error");
    }
  })();
});

server.listen(PORT, HOST, () => {
  console.log(`[sfsg-watch] ${banner ?? load.emptyReason} · transport: web`);
  console.log(`[sfsg-watch] review queue at http://${HOST}:${PORT}/`);

  if (isEmpty) {
    // Seed the thread with the explanation and mark it finished, so the page
    // renders a real, readable end state rather than an empty scroller.
    append(
      "agent",
      [
        "SF Service Guide Watch",
        "",
        "Nothing to review right now.",
        "",
        load.emptyReason ?? "The review queue is empty.",
      ].join("\n"),
    );
    session.ended = true;
    return;
  }

  // Open the conversation immediately, so the greeting and the first review are
  // already waiting in the transcript when the browser connects.
  void session.start();
});

// Ctrl-C should end the process cleanly, not leave a bound port behind.
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
    // If sockets are still open a moment later, stop waiting for them.
    setTimeout(() => process.exit(0), 500).unref();
  });
}
