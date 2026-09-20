/**
 * SF Service Guide Watch — the application server.
 * ================================================
 *
 * One `node:http` server, no framework, no build step, no dependencies beyond
 * the Node standard library. It serves the whole app:
 *
 *   GET  /                    the dashboard        (ui/index.html)
 *   GET  /review              the review thread    (ui/review.html)
 *   GET  /graph               the graph explorer   (ui/graph.html)
 *   GET  /freshness           the freshness report (ui/freshness.html)
 *   GET  /ui/*                static passthrough
 *
 *   GET  /api/results         out/results.json, or {} if absent
 *   GET  /api/graph           out/graph.json, or {} if absent
 *   GET  /api/freshness       out/freshness.json, or {} if absent
 *   GET  /api/state           recorded decisions + counts
 *   GET  /api/review/current  the open review, or {done:true, summary}
 *   POST /api/review/reply    {action} -> the next review
 *   POST /api/run             {budget} -> starts the audit pipeline
 *   GET  /api/run/stream      SSE, one event per stdout line
 *
 * The review logic lives in notify/session.ts and is shared with the messaging
 * transport (notify/spectrum.ts), so an answer given here and one texted from a
 * phone take the identical code path and write the identical files.
 *
 * READ-ONLY AGAINST PRODUCTION: this server binds to localhost, serves files,
 * and spawns the local audit pipeline. It never contacts sfserviceguide.org.
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import * as fs from "node:fs";
import * as http from "node:http";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  ACTIONS_FOR,
  actionLabel,
  askLine,
  evidenceCard,
  fieldLabel,
  hostOf,
  liveLabel,
  loadDecisions,
  loadQueue,
  parseCommand,
  RESULTS_PATH,
  ReviewQueue,
  storedLabel,
  type Action,
  type ReviewItem,
} from "./session.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");
const UI_DIR = path.join(ROOT, "ui");
const OUT_DIR = path.join(ROOT, "out");

const PORT = Number(process.env.PORT ?? 8787);
const HOST = process.env.HOST ?? "127.0.0.1";

// ---------------------------------------------------------------------------
// The review queue
// ---------------------------------------------------------------------------

/**
 * The queue is rebuilt on demand rather than held for the process lifetime, so
 * that a pipeline run started from the dashboard produces reviews the very next
 * time somebody opens the review page — no restart, no stale cursor.
 */
let lastLoad = loadQueue();
let queue = new ReviewQueue(lastLoad.items, "web-reviewer", "web");

/** Re-read out/results.json and start a fresh queue over it. */
function rebuildQueue(): void {
  lastLoad = loadQueue();
  queue = new ReviewQueue(lastLoad.items, "web-reviewer", "web");
  resultsMtimeMs = resultsMtime();
}

function resultsMtime(): number {
  try {
    return fs.statSync(RESULTS_PATH).mtimeMs;
  } catch {
    return 0;
  }
}

let resultsMtimeMs = resultsMtime();

/**
 * Pick up a results.json written by anything other than `/api/run`.
 *
 * The pipeline is just as often run from a terminal as from the dashboard, and
 * a server that only reloads on its own runs will happily serve an empty queue
 * while the file on disk holds a dozen reviews. Anyone who does that and then
 * refreshes the browser concludes the app is broken.
 *
 * An mtime check on each read is cheap and needs no watcher. Already-recorded
 * decisions survive, because `loadQueue()` reads them back from the outcome
 * files and excludes them.
 */
function ensureFresh(): void {
  const mtime = resultsMtime();
  if (mtime === resultsMtimeMs) return;
  rebuildQueue();
  console.log("[sfsg-watch] out/results.json changed on disk — queue reloaded");
  logQueueBanner();
}

/** Serialise state-changing review calls; two taps must not double-file. */
let chain: Promise<void> = Promise.resolve();
function serialise<T>(fn: () => T): Promise<T> {
  const run = chain.then(fn);
  chain = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

// ---------------------------------------------------------------------------
// Shapes the browser consumes
// ---------------------------------------------------------------------------

/**
 * One review, flattened for the UI.
 *
 * Both sides of the comparison are labelled with their origin, because a
 * reviewer about to change a listing has to see the listing AND the evidence,
 * and must never have to work out which of two URLs is which. `live` is
 * omitted entirely when there is no proposed change — an abstention is not a
 * diff, and rendering it as one is what made the old UI nonsensical.
 */
function itemForClient(item: ReviewItem, position: number) {
  return {
    resource_id: item.resourceId,
    kind: item.kind,
    name: item.name,
    field: fieldLabel(item.field),
    ask: askLine(item),
    stored: item.stored || null,
    stored_label: storedLabel(),
    listing_url: item.listingUrl || null,
    live: item.live || null,
    live_label: item.live ? liveLabel(item) : null,
    // What is wrong with the stored value, for a structural finding. Never a
    // replacement — the UI must not render it as one.
    defect: item.defect || null,
    structural: item.kind === "structural",
    source_url: item.sourceUrl || null,
    source_quote: item.sourceQuote || null,
    org_website: item.orgWebsite || null,
    org_host: hostOf(item.orgWebsite) || null,
    reason: item.reason || null,
    confidence: item.confidence,
    blast_radius: item.blastRadiusSize,
    evidence: evidenceCard(item),
    position,
    total: queue.total,
    actions: ACTIONS_FOR[item.kind].map((a) => ({ action: a, label: actionLabel(a, item.kind) })),
  };
}

function currentPayload(): Record<string, unknown> {
  const open = queue.current();
  if (!open) {
    return {
      done: true,
      summary: {
        text: queue.summary(),
        counts: queue.counts,
        answered: queue.answered,
        total: queue.total,
        // Totals across every session ever recorded, read back from the
        // outcome files. After a pipeline run rebuilds the queue the in-memory
        // counts restart at zero, and showing only those would tell a reviewer
        // their earlier work had vanished.
        recorded: recordedCounts(),
      },
      empty: queue.total === 0,
      empty_reason: lastLoad.emptyReason ?? null,
    };
  }
  return { done: false, item: itemForClient(open.item, open.position) };
}

/**
 * Counts read back from the outcome files rather than from memory, so a reload
 * or a second browser sees the same totals as the first.
 */
function recordedCounts(): Record<string, number> {
  const counts = { confirmed: 0, rejected: 0, skipped: 0, verified: 0, flagged: 0 };
  for (const d of loadDecisions().values()) {
    if (d.action === "confirm") counts.confirmed++;
    else if (d.action === "reject") counts.rejected++;
    else if (d.action === "looks_right") counts.verified++;
    else if (d.action === "needs_fixing") counts.flagged++;
  }
  // Skips are never written to disk — "not me, not now" isn't an outcome — so
  // this is the only count that can come from the live session.
  counts.skipped = queue.counts.skipped;
  return counts;
}

/**
 * How many listings are actually reviewable right now.
 *
 * Counting `results.queue` alone answers "how many discrepancies", which is a
 * different and much smaller number — a run can produce zero discrepancies and
 * eight abstentions, and reporting that as an empty queue is both wrong and
 * alarming. Abstentions are reviews; they just ask a different question.
 */
function queueBreakdown(): Record<string, number> {
  let discrepancies = 0;
  let structural = 0;
  let abstentions = 0;
  for (const item of lastLoad.items) {
    if (item.kind === "discrepancy") discrepancies++;
    else if (item.kind === "structural") structural++;
    else abstentions++;
  }
  return {
    reviewable: queue.total,
    remaining: queue.remaining,
    discrepancies,
    // Counted separately rather than folded in with abstentions. A structural
    // defect is arithmetic on the stored value — no fetch, no model, no false
    // positive — so it is the finding we are MOST sure of. Reporting it as
    // "unverified" understates it in the one direction that matters, because
    // a banner reading "0 proposed changes" says we found nothing.
    structural,
    abstentions,
    already_answered: lastLoad.alreadyDecided,
  };
}

/** One truthful line about the queue, for the server log. */
function logQueueBanner(): void {
  const b = queueBreakdown();
  if (b.reviewable === 0) {
    console.log(`[sfsg-watch] ${lastLoad.emptyReason ?? "nothing to review"}`);
    return;
  }
  const parts: string[] = [];
  if (b.structural) parts.push(`${b.structural} broken`);
  parts.push(`${b.discrepancies} proposed change(s)`);
  parts.push(`${b.abstentions} unverified`);
  console.log(`[sfsg-watch] ${b.reviewable} review(s) ready (${parts.join(", ")})`);
}

function statePayload(): Record<string, unknown> {
  const decisions: Record<string, { action: string; at: string }> = {};
  for (const [id, d] of loadDecisions()) {
    decisions[id] = { action: d.action, at: d.at };
  }
  return { decisions, counts: recordedCounts(), queue: queueBreakdown() };
}

// ---------------------------------------------------------------------------
// The pipeline runner — the demo centrepiece
// ---------------------------------------------------------------------------

/**
 * FalkorDB lives in the project virtualenv. Running the pipeline with a bare
 * `python3` silently degrades to the in-memory graph — the run still succeeds,
 * which is exactly what makes the mistake hard to notice. Prefer the venv.
 */
function pythonBin(): string {
  const venv = path.join(ROOT, ".venv", "bin", "python");
  return fs.existsSync(venv) ? venv : "python3";
}

interface RunState {
  proc: ChildProcessWithoutNullStreams | null;
  lines: string[];
  done: boolean;
  ok: boolean;
  /** Why the last run failed, if it did. Mirrored into the terminal event. */
  error: string | null;
  /** Open SSE responses, fed as the pipeline talks. */
  listeners: Set<http.ServerResponse>;
}

const run: RunState = {
  proc: null,
  lines: [],
  done: true,
  ok: true,
  error: null,
  listeners: new Set(),
};

/**
 * Send one SSE frame as the DEFAULT event type — no `event:` line.
 *
 * A named event only reaches `addEventListener("line", …)`; `onmessage` never
 * fires for it. The dashboard listens on `onmessage`, so naming these events
 * left it showing an empty log and a run that appeared to hang forever.
 * Clients tell frames apart by payload shape: `{line, stream}` vs `{done, ok}`.
 */
function sseFrame(data: unknown): string {
  return `data: ${JSON.stringify(data)}\n\n`;
}

function broadcast(data: unknown): void {
  const payload = sseFrame(data);
  for (const res of run.listeners) {
    try {
      res.write(payload);
    } catch {
      run.listeners.delete(res);
    }
  }
}

/**
 * The audit, then the freshness scoring, as one streamed run.
 *
 * `run.py` answers "is anything wrong"; `freshness.py` answers "has anyone
 * confirmed this recently" — different questions over the same corpus, and the
 * freshness page goes stale the moment results.json is regenerated without it.
 * They are chained rather than parallel because the second reads what the first
 * writes. Failure of either ends the run; freshness takes well under a second,
 * so it costs the demo nothing.
 */
const PIPELINE_STEPS: { script: string; label: string }[] = [
  { script: "run.py", label: "audit" },
  { script: "freshness.py", label: "freshness" },
];

function startRun(budget: number): { started: boolean; error?: string } {
  if (run.proc && !run.done) {
    return { started: false, error: "A pipeline run is already in progress." };
  }

  run.lines = [];
  run.done = false;
  run.ok = true;
  run.error = null;

  const bin = pythonBin();

  const pump = (chunk: Buffer, stream: "stdout" | "stderr") => {
    for (const raw of chunk.toString("utf8").split(/\r?\n/)) {
      const line = raw.replace(/\s+$/, "");
      if (!line) continue;
      run.lines.push(line);
      broadcast({ line, stream });
    }
  };

  const finish = (ok: boolean, note?: string) => {
    if (run.done) return;
    run.done = true;
    run.ok = ok;
    run.error = note ?? null;
    run.proc = null;
    if (note) {
      run.lines.push(note);
      broadcast({ line: note, stream: "stderr" });
    }
    // Fresh results mean a fresh queue, without restarting the server.
    rebuildQueue();
    logQueueBanner();
    broadcast({ done: true, ok, ...(note ? { error: note } : {}) });
    for (const res of run.listeners) {
      try {
        res.end();
      } catch {
        /* already gone */
      }
    }
    run.listeners.clear();
  };

  const runStep = (index: number): void => {
    const step = PIPELINE_STEPS[index];
    if (!step) {
      finish(true);
      return;
    }

    const proc = spawn(bin, [step.script], {
      cwd: ROOT,
      env: {
        ...process.env,
        BUDGET: String(budget),
        // Unbuffered, or stdout arrives in one lump at exit and the live
        // stream — the entire point of this route — shows nothing until it's
        // over.
        PYTHONUNBUFFERED: "1",
      },
    }) as ChildProcessWithoutNullStreams;

    run.proc = proc;
    proc.stdout.on("data", (c: Buffer) => pump(c, "stdout"));
    proc.stderr.on("data", (c: Buffer) => pump(c, "stderr"));

    proc.on("error", (err) => {
      finish(false, `[sfsg-watch] could not start ${bin} ${step.script}: ${err.message}`);
    });

    proc.on("close", (code, signal) => {
      if (code === 0) {
        runStep(index + 1);
        return;
      }
      // A null code means the child was terminated by a signal, not by its own
      // exit. Naming the signal is the difference between "the pipeline failed"
      // and "something killed the pipeline" — they have nothing in common.
      finish(
        false,
        signal
          ? `[sfsg-watch] ${step.script} was terminated by ${signal} ` +
              `(not a pipeline failure — something stopped the process)`
          : `[sfsg-watch] ${step.script} exited with code ${code}`,
      );
    });

    console.log(`[sfsg-watch] ${step.label} started (${bin} ${step.script}, BUDGET=${budget})`);
  };

  runStep(0);
  return { started: true };
}

// ---------------------------------------------------------------------------
// HTTP plumbing
// ---------------------------------------------------------------------------

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
};

function sendJson(res: http.ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    "cache-control": "no-store",
  });
  res.end(payload);
}

/** Read a JSON file defensively: absent or broken both read as `{}`. */
function readJsonFile(file: string): unknown {
  try {
    if (!fs.existsSync(file)) return {};
    const parsed: unknown = JSON.parse(fs.readFileSync(file, "utf8"));
    return parsed ?? {};
  } catch (err) {
    console.warn(`[sfsg-watch] ${path.basename(file)} unreadable: ${String(err)}`);
    return {};
  }
}

function sendFile(res: http.ServerResponse, file: string): boolean {
  if (!fs.existsSync(file) || !fs.statSync(file).isFile()) return false;
  const body = fs.readFileSync(file);
  res.writeHead(200, {
    "content-type": CONTENT_TYPES[path.extname(file).toLowerCase()] ?? "application/octet-stream",
    "content-length": body.byteLength,
    "cache-control": "no-store",
  });
  res.end(body);
  return true;
}

function sendNotFound(res: http.ServerResponse, what: string): void {
  const body = `<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Not found — SF Service Guide Watch</title>
<style>body{background:#0b0d10;color:#e8ecf1;font-family:-apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif;padding:48px 24px;line-height:1.6}
a{color:#6fa8ff}code{color:#9aa5b1}</style></head><body>
<h1>Not found</h1><p><code>${what.replace(/[<&]/g, "")}</code> isn't a page here.</p>
<p><a href="/">Dashboard</a> · <a href="/review">Review queue</a> · <a href="/graph">Graph</a></p>
</body></html>`;
  res.writeHead(404, {
    "content-type": "text/html; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

function readBody(req: http.IncomingMessage, limit = 64 * 1024): Promise<string> {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > limit) {
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

const VALID_ACTIONS = new Set<Action>([
  "confirm",
  "reject",
  "skip",
  "looks_right",
  "needs_fixing",
]);

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

const PAGES: Record<string, string> = {
  "/": "index.html",
  "/index.html": "index.html",
  "/review": "review.html",
  "/graph": "graph.html",
  "/freshness": "freshness.html",
};

const server = http.createServer((req, res) => {
  void (async () => {
    const url = new URL(req.url ?? "/", `http://${req.headers.host ?? HOST}`);
    const pathname = decodeURIComponent(url.pathname);
    const method = req.method ?? "GET";

    try {
      // ---------------- static pages ----------------
      const page = PAGES[pathname];
      if (method === "GET" && page) {
        if (sendFile(res, path.join(UI_DIR, page))) return;
        sendNotFound(res, `ui/${page}`);
        return;
      }

      // ---------------- static passthrough ----------------
      if (method === "GET" && pathname.startsWith("/ui/")) {
        // Resolve, then confirm the result is still inside ui/ — otherwise
        // "/ui/../../etc/passwd" would escape the directory.
        const target = path.resolve(UI_DIR, "." + pathname.slice(3));
        if (!target.startsWith(UI_DIR + path.sep)) {
          sendNotFound(res, pathname);
          return;
        }
        if (sendFile(res, target)) return;
        sendNotFound(res, pathname);
        return;
      }

      // ---------------- data ----------------
      if (method === "GET" && pathname === "/api/results") {
        sendJson(res, 200, readJsonFile(path.join(OUT_DIR, "results.json")));
        return;
      }

      if (method === "GET" && pathname === "/api/graph") {
        sendJson(res, 200, readJsonFile(path.join(OUT_DIR, "graph.json")));
        return;
      }

      // Staleness is a different question from brokenness: not "is this value
      // wrong" but "has anyone confirmed it recently". Scored by freshness.py.
      if (method === "GET" && pathname === "/api/freshness") {
        sendJson(res, 200, readJsonFile(path.join(OUT_DIR, "freshness.json")));
        return;
      }

      if (method === "GET" && pathname === "/api/state") {
        ensureFresh();
        sendJson(res, 200, statePayload());
        return;
      }

      // ---------------- review ----------------
      if (method === "GET" && pathname === "/api/review/current") {
        sendJson(
          res,
          200,
          await serialise(() => {
            ensureFresh();
            return currentPayload();
          }),
        );
        return;
      }

      // `/api/reply` is the older path this server shipped with. Kept as an
      // alias so anything already pointed at it keeps working.
      if (
        method === "POST" &&
        (pathname === "/api/review/reply" || pathname === "/api/reply")
      ) {
        let action: unknown;
        try {
          const parsed = JSON.parse(await readBody(req)) as {
            action?: unknown;
            text?: unknown;
          } | null;
          // The alias originally took free text ("Y"), the contract takes a
          // named action. Accept either, so both callers are served.
          action = parsed?.action ?? parsed?.text;
        } catch {
          sendJson(res, 400, { error: 'expected JSON {"action": "..."}' });
          return;
        }

        // Map the old free-text replies onto named actions.
        if (typeof action === "string" && !VALID_ACTIONS.has(action as Action)) {
          const parsedCommand = parseCommand(action);
          if (parsedCommand.kind === "action") action = parsedCommand.action;
        }

        if (typeof action !== "string" || !VALID_ACTIONS.has(action as Action)) {
          sendJson(res, 400, {
            error: `action must be one of ${[...VALID_ACTIONS].join(", ")}`,
          });
          return;
        }

        const payload = await serialise(() => {
          const result = queue.apply(action as Action);
          return {
            ...currentPayload(),
            result: {
              ok: result.ok,
              message: result.message,
              action: result.action,
              resource_id: result.item?.resourceId ?? null,
              // The follow-through: once they've confirmed a change, the most
              // useful next thing is where to actually make it.
              edit_url:
                result.ok && result.action === "confirm"
                  ? (result.item?.listingEditUrl ?? null)
                  : null,
            },
          };
        });
        sendJson(res, 200, payload);
        return;
      }

      // ---------------- pipeline ----------------
      if (method === "POST" && pathname === "/api/run") {
        let budget = 5;
        try {
          const parsed: unknown = JSON.parse(await readBody(req));
          const raw = (parsed as { budget?: unknown } | null)?.budget;
          if (typeof raw === "number" && Number.isFinite(raw)) budget = raw;
        } catch {
          // No body is fine — the default budget is the demo-sized one.
        }
        budget = Math.max(1, Math.min(25, Math.trunc(budget)));

        const started = startRun(budget);
        sendJson(res, started.started ? 200 : 409, {
          started: started.started,
          budget,
          ...(started.error ? { error: started.error } : {}),
        });
        return;
      }

      if (method === "GET" && pathname === "/api/run/stream") {
        // HEADERS FIRST, before anything that could possibly throw. If the
        // replay or the run state blew up after this point, the client would
        // still have a valid 200 event-stream and would see an error frame
        // rather than a destroyed socket with no status line at all.
        res.writeHead(200, {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-store",
          connection: "keep-alive",
          // Proxies that buffer would defeat the entire point of this route.
          "x-accel-buffering": "no",
        });
        // Push the headers out now rather than waiting for the first body
        // write, so a subscriber attached to an idle run still gets a
        // response immediately.
        res.flushHeaders?.();

        // A comment frame (ignored by EventSource) proves the connection is
        // alive while a run is producing no output yet.
        res.write(": connected\n\n");

        try {
          // Replay what's already happened, so a late subscriber sees the run
          // from the start rather than joining mid-way.
          for (const line of run.lines) {
            res.write(sseFrame({ line, stream: "stdout" }));
          }
        } catch (err) {
          console.error(`[sfsg-watch] SSE replay failed: ${String(err)}`);
          res.write(sseFrame({ done: true, ok: false, error: String(err) }));
          res.end();
          return;
        }

        if (run.done) {
          res.write(
            sseFrame({
              done: true,
              ok: run.ok,
              ...(run.error ? { error: run.error } : {}),
            }),
          );
          res.end();
          return;
        }

        run.listeners.add(res);

        // Keep the connection demonstrably alive during long silent stretches
        // (the agent can spend 20s fetching one site without printing).
        const heartbeat = setInterval(() => {
          try {
            res.write(": ping\n\n");
          } catch {
            clearInterval(heartbeat);
            run.listeners.delete(res);
          }
        }, 15_000);
        heartbeat.unref();

        // A client hanging up only detaches that client. It must never touch
        // the run itself — the pipeline outlives every individual request.
        req.on("close", () => {
          clearInterval(heartbeat);
          run.listeners.delete(res);
        });
        return;
      }

      sendNotFound(res, pathname);
    } catch (err) {
      console.error(`[sfsg-watch] request failed: ${String(err)}`);
      if (!res.headersSent) {
        res.writeHead(500, { "content-type": "application/json; charset=utf-8" });
      }
      res.end(JSON.stringify({ error: "internal error" }));
    }
  })();
});

/**
 * Never die silently.
 *
 * A throw that escaped a request handler used to take the process down with no
 * explanation, which is exactly the situation where the log matters most. The
 * server keeps running: one bad request must not end a demo.
 */
process.on("uncaughtException", (err) => {
  console.error(`[sfsg-watch] UNCAUGHT: ${err instanceof Error ? err.stack : String(err)}`);
});
process.on("unhandledRejection", (reason) => {
  console.error(
    `[sfsg-watch] UNHANDLED REJECTION: ${reason instanceof Error ? reason.stack : String(reason)}`,
  );
});

server.on("clientError", (err, socket) => {
  console.error(`[sfsg-watch] client error: ${err.message}`);
  if (socket.writable) socket.end("HTTP/1.1 400 Bad Request\r\n\r\n");
});

server.on("error", (err) => {
  // The common one is EADDRINUSE — an older server still holding the port.
  // Saying so beats a stack trace nobody reads.
  const e = err as NodeJS.ErrnoException;
  if (e.code === "EADDRINUSE") {
    console.error(
      `[sfsg-watch] port ${PORT} is already in use — another server is still ` +
        `running. Stop it first:  pkill -f notify/web.ts`,
    );
    process.exit(1);
  }
  console.error(`[sfsg-watch] server error: ${err.message}`);
});

server.listen(PORT, HOST, () => {
  const base = `http://${HOST}:${PORT}`;
  logQueueBanner();
  console.log(`[sfsg-watch] dashboard  ${base}/`);
  console.log(`[sfsg-watch] review     ${base}/review`);
  console.log(`[sfsg-watch] graph      ${base}/graph`);
});

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    if (run.proc) {
      try {
        run.proc.kill("SIGTERM");
      } catch {
        /* already gone */
      }
    }
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 500).unref();
  });
}
