// ZoneHeal watcher + dashboard server
//
// Polls the routers, aggregates metrics, drives the dashboard,
// runs simulations, chats via Bedrock, and sends SES email alerts.
//
// Run:  node zone_watched.js
//
// Install:
//   npm install express @aws-sdk/client-s3 @aws-sdk/client-bedrock-runtime @aws-sdk/client-ses

const express = require("express");
const path    = require("path");

const { S3Client, PutObjectCommand, GetObjectCommand } = require("@aws-sdk/client-s3");
const { BedrockRuntimeClient, InvokeModelCommand }    = require("@aws-sdk/client-bedrock-runtime");
const { SESClient, SendEmailCommand }                  = require("@aws-sdk/client-ses");

// ---------------------------------------------------------------
// Config
// ---------------------------------------------------------------

const REGION = "ap-southeast-2";
const BUCKET = "zonerl-819168518877";

// All three router public IPs (one per AZ task)
const ROUTER_IPS = [
  "http://3.106.214.17:8000",
  "http://3.107.157.146:8000",
  "http://3.107.233.251:8000",
];

const USD_PER_GB    = 0.02;
const RUPEES_PER_USD = 95.9355;
const SLO_MS        = 200;     // matches router env var

const POLL_SECONDS = 30;
const PORT         = 3000;

// SES – set these in your shell environment:
//   export ALERT_FROM_EMAIL=alerts@yourdomain.com
//   export ALERT_TO_EMAIL=founder@yourdomain.com
const ALERT_FROM_EMAIL = process.env.ALERT_FROM_EMAIL || "alerts@example.com";
const ALERT_TO_EMAIL   = process.env.ALERT_TO_EMAIL   || "founder@example.com";

// ---------------------------------------------------------------
// AWS SDK clients  (all use the ECS task-role, no keys needed)
// ---------------------------------------------------------------

const s3      = new S3Client({ region: REGION });
const bedrock = new BedrockRuntimeClient({ region: REGION });
const ses     = new SESClient({ region: REGION });

// ---------------------------------------------------------------
// Runtime state
// ---------------------------------------------------------------

let SPEND_LIMIT = 1000;

let state = {
  zones:       {},
  spentRupees: 0,
  bytesMoved:  0,
  incidents:   [],
  mailSent:    false,
  limit:       SPEND_LIMIT,
  receipt:     null,
};

let openIncident = null;

// Time-series snapshots for the dashboard chart (last 30 minutes)
let history = [];           // [{timestamp, zones:{az:{util,p99,spill}}, totalSpill}]
const MAX_HISTORY = 30;

// Bedrock conversation history (kept in-memory, per server session)
let chatHistory = [];

// AZ discovery: filled on startup by calling /health on each router
let routerAZMap = {};       // url  -> az  (e.g. "http://3.x.x.x:8000" -> "ap-southeast-2a")
let azRouterMap = {};       // az   -> url

// Simulation state
let simState = {
  running:         false,
  phase:           "idle",   // idle | baseline | fault | policy | recovery | complete | error
  startedAt:       null,
  metrics:         [],       // high-frequency snapshots collected during the sim
  baselineMetrics: null,
  summary:         null,
  targetAZ:        null,
};

// ---------------------------------------------------------------
// Utility
// ---------------------------------------------------------------

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function fmtTime() {
  return new Date().toLocaleTimeString("en-AU");
}

// ---------------------------------------------------------------
// Router helpers
// ---------------------------------------------------------------

async function httpGet(url, timeoutMs = 3000) {
  const res = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  return res.json();
}

async function httpPost(url, params = {}, timeoutMs = 5000) {
  // Build query string for the FastAPI routers (they use query params)
  const qs = Object.entries(params).map(([k, v]) => `${k}=${v}`).join("&");
  const fullUrl = qs ? `${url}?${qs}` : url;
  const res = await fetch(fullUrl, { method: "POST", signal: AbortSignal.timeout(timeoutMs) });
  return res.json();
}

// Discover which AZ each router task is in by calling /health
async function discoverRouters() {
  await Promise.all(ROUTER_IPS.map(async (url) => {
    try {
      const data = await httpGet(url + "/health");
      if (data.az) {
        routerAZMap[url] = data.az;
        azRouterMap[data.az] = url;
        console.log(`[discovery] ${url} -> ${data.az}`);
      }
    } catch (err) {
      console.log(`[discovery] could not reach ${url}: ${err.message || err}`);
    }
  }));
}

// Fetch /state from one router; returns { ok, url, ...state }
async function askRouter(url) {
  try {
    const data = await httpGet(url + "/state");
    return { ok: true, url, ...data };
  } catch (err) {
    return { ok: false, url, error: String(err) };
  }
}

// Fetch routing mode from one router
async function getRouterMode(url) {
  try {
    const data = await httpGet(url + "/mode");
    return data.mode;
  } catch {
    return null;
  }
}

async function setRouterMode(url, mode) {
  try {
    await httpPost(url + "/mode", { m: mode });
  } catch (err) {
    console.log(`[mode] failed on ${url}: ${err.message || err}`);
  }
}

async function triggerBrownout(az, on) {
  // Try every router until one of them accepts the brownout command
  for (const url of ROUTER_IPS) {
    try {
      const result = await httpPost(url + "/brownout", { az, on });
      return result;
    } catch {
      // try next router
    }
  }
  console.log(`[brownout] all routers failed for az=${az} on=${on}`);
}

// ---------------------------------------------------------------
// Polling
// ---------------------------------------------------------------

async function poll() {
  const answers = await Promise.all(ROUTER_IPS.map(askRouter));

  let anyoneSpilling = false;
  let worstP99       = 0;
  let slowZone       = null;

  const snapshot = {
    timestamp: new Date().toISOString(),
    zones:     {},
    totalSpill: 0,
  };

  for (const answer of answers) {
    if (!answer.ok) continue;

    const az    = answer.source_az;
    const spill = answer.spill || 0;
    const p99   = (answer.p99  || {})[az] || 0;
    const util  = (answer.util || {})[az] || 0;

    state.zones[az] = answer;

    snapshot.zones[az] = { util, p99, spill };
    snapshot.totalSpill += spill;

    if (spill > 0) anyoneSpilling = true;

    if (p99 > worstP99) { worstP99 = p99; slowZone = az; }
  }

  // Store snapshot in time-series
  history.push(snapshot);
  if (history.length > MAX_HISTORY) history.shift();

  // Incident lifecycle
  if (anyoneSpilling && openIncident === null) {
    openIncident = {
      startedAt:    new Date().toISOString(),
      zone:         slowZone,
      p99Before:    worstP99,
      bytesAtStart: state.bytesMoved,
      rupeesAtStart: state.spentRupees,
    };
    console.log(`[incident] started in ${slowZone}`);
  }

  if (!anyoneSpilling && openIncident !== null) {
    openIncident.endedAt  = new Date().toISOString();
    openIncident.bytes    = state.bytesMoved  - openIncident.bytesAtStart;
    openIncident.rupees   = state.spentRupees - openIncident.rupeesAtStart;
    openIncident.p99After = worstP99;
    state.incidents.unshift(openIncident);
    console.log(`[incident] ended`);
    openIncident = null;
    await save();
  }

  state.limit = SPEND_LIMIT;

  // Spend-limit alert
  if (state.spentRupees > SPEND_LIMIT && !state.mailSent) {
    state.mailSent = true;
    console.log(`[alert] over limit — generating explanation and sending email`);
    await explain();
  }

  // Feed running simulation
  if (simState.running) {
    simState.metrics.push(snapshot);
  }
}

// ---------------------------------------------------------------
// Bedrock explanation (non-technical receipt)
// ---------------------------------------------------------------

async function explain() {
  const latest = state.incidents[0] || openIncident || {};

  const facts = {
    zone:              latest.zone         || "unknown",
    slowLatencyMs:     Math.round(latest.p99Before || 0),
    normalLatencyMs:   25,
    spilledFraction:   0.5,
    bytesMoved:        Math.round(state.bytesMoved),
    rupeesSpent:       state.spentRupees.toFixed(4),
    limitRupees:       SPEND_LIMIT,
    incidentCount:     state.incidents.length,
  };

  const prompt =
    "You are explaining a cloud routing decision to a founder who " +
    "is not technical. Use the numbers below and add none of your " +
    "own. Four sentences at most. Say what went wrong, what the " +
    "system did, what it cost, and what it avoided. No jargon.\n\n" +
    JSON.stringify(facts, null, 2);

  let explanationText = "Explanation unavailable.";

  try {
    const cmd = new InvokeModelCommand({
      modelId:     "anthropic.claude-haiku-4-5-20251001-v1:0",
      contentType: "application/json",
      body: JSON.stringify({
        anthropic_version: "bedrock-2023-05-31",
        max_tokens: 400,
        messages:   [{ role: "user", content: prompt }],
      }),
    });

    const reply  = await bedrock.send(cmd);
    const parsed = JSON.parse(new TextDecoder().decode(reply.body));
    explanationText = parsed.content[0].text;

    const receipt = {
      writtenAt:   new Date().toISOString(),
      facts,
      explanation: explanationText,
    };
    state.receipt = receipt;

    await s3.send(new PutObjectCommand({
      Bucket:      BUCKET,
      Key:         "receipts/latest.json",
      Body:        JSON.stringify(receipt, null, 2),
      ContentType: "application/json",
    }));
    console.log("[bedrock] receipt written to S3");

  } catch (err) {
    console.log("[bedrock] failed:", err.message || err);
    state.receipt = { error: String(err) };
  }

  // Always attempt the email, even if Bedrock partially failed
  await sendAlertEmail(facts, explanationText);
}

// ---------------------------------------------------------------
// SES email alert
// ---------------------------------------------------------------

async function sendAlertEmail(facts, explanation) {
  const subject =
    `🚨 ZoneHeal Alert: Spend limit exceeded — ₹${Number(facts.rupeesSpent).toFixed(2)} spent`;

  const htmlBody = `
    <html><body style="font-family:Arial,sans-serif;background:#0f1b2d;color:#d5dbdb;padding:24px;">
      <div style="max-width:560px;margin:0 auto;background:#1b2a3b;border-radius:8px;padding:24px;">
        <h2 style="color:#f90;margin:0 0 16px;">⚡ ZoneHeal Spend Alert</h2>
        <p style="font-size:15px;color:#fff;">
          Your cross-zone routing cost has exceeded your limit of
          <strong style="color:#f90;">₹${facts.limitRupees}</strong>.
        </p>
        <hr style="border:none;border-top:1px solid #2a3f55;margin:16px 0;">
        <h3 style="color:#fff;margin:0 0 8px;">What happened</h3>
        <p style="color:#d5dbdb;line-height:1.6;">${explanation}</p>
        <hr style="border:none;border-top:1px solid #2a3f55;margin:16px 0;">
        <table style="width:100%;border-collapse:collapse;">
          <tr>
            <td style="padding:6px 0;color:#8d9ba8;font-size:13px;">Zone affected</td>
            <td style="padding:6px 0;color:#fff;font-size:13px;text-align:right;"><strong>${facts.zone}</strong></td>
          </tr>
          <tr>
            <td style="padding:6px 0;color:#8d9ba8;font-size:13px;">Worst latency</td>
            <td style="padding:6px 0;color:#fff;font-size:13px;text-align:right;"><strong>${facts.slowLatencyMs} ms</strong></td>
          </tr>
          <tr>
            <td style="padding:6px 0;color:#8d9ba8;font-size:13px;">Data moved across zones</td>
            <td style="padding:6px 0;color:#fff;font-size:13px;text-align:right;"><strong>${(facts.bytesMoved / 1e9).toFixed(3)} GB</strong></td>
          </tr>
          <tr>
            <td style="padding:6px 0;color:#8d9ba8;font-size:13px;">Total cost</td>
            <td style="padding:6px 0;color:#f90;font-size:15px;text-align:right;font-weight:700;">₹${facts.rupeesSpent}</td>
          </tr>
          <tr>
            <td style="padding:6px 0;color:#8d9ba8;font-size:13px;">Incidents so far</td>
            <td style="padding:6px 0;color:#fff;font-size:13px;text-align:right;">${facts.incidentCount}</td>
          </tr>
        </table>
        <p style="color:#5a7a90;font-size:11px;margin:20px 0 0;">
          Sent automatically by ZoneHeal monitoring · ${fmtTime()}
        </p>
      </div>
    </body></html>
  `;

  const textBody =
    `ZoneHeal Spend Alert\n\n` +
    `${explanation}\n\n` +
    `Spent: ₹${facts.rupeesSpent}  /  Limit: ₹${facts.limitRupees}\n` +
    `Zone: ${facts.zone}  |  Peak latency: ${facts.slowLatencyMs}ms\n`;

  try {
    await ses.send(new SendEmailCommand({
      Source: ALERT_FROM_EMAIL,
      Destination: { ToAddresses: [ALERT_TO_EMAIL] },
      Message: {
        Subject: { Data: subject, Charset: "UTF-8" },
        Body: {
          Html: { Data: htmlBody, Charset: "UTF-8" },
          Text: { Data: textBody, Charset: "UTF-8" },
        },
      },
    }));
    console.log(`[ses] alert email sent to ${ALERT_TO_EMAIL}`);
  } catch (err) {
    console.log(`[ses] failed: ${err.message || err}`);
  }
}

// ---------------------------------------------------------------
// S3 persistence
// ---------------------------------------------------------------

async function save() {
  try {
    await s3.send(new PutObjectCommand({
      Bucket:      BUCKET,
      Key:         "watcher/state.json",
      Body:        JSON.stringify(state, null, 2),
      ContentType: "application/json",
    }));
  } catch (err) {
    console.log("[s3] save failed:", err.message || err);
  }
}

async function load() {
  try {
    const reply  = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: "watcher/state.json" }));
    const text   = await reply.Body.transformToString();
    const saved  = JSON.parse(text);
    state.spentRupees = saved.spentRupees || 0;
    state.bytesMoved  = saved.bytesMoved  || 0;
    state.incidents   = saved.incidents   || [];
    state.mailSent    = saved.mailSent    || false;
    if (saved.receipt) state.receipt = saved.receipt;
    console.log("[s3] loaded previous state");
  } catch {
    console.log("[s3] starting fresh (no saved state)");
  }
}

// ---------------------------------------------------------------
// Simulation runner (async, fire-and-forget)
// ---------------------------------------------------------------

async function runSimulation(targetAZ) {
  simState.running         = true;
  simState.metrics         = [];
  simState.baselineMetrics = null;
  simState.summary         = null;
  simState.targetAZ        = targetAZ;
  simState.startedAt       = new Date().toISOString();

  try {
    // Phase 1 — Baseline: shadow mode, record normal behaviour
    simState.phase = "baseline";
    console.log(`[sim] phase=baseline — setting shadow mode`);
    await Promise.all(ROUTER_IPS.map(url => setRouterMode(url, "shadow")));

    await sleep(15000);
    if (!simState.running) return;
    simState.baselineMetrics = simState.metrics.slice();

    // Phase 2 — Fault: inject brownout on target AZ
    simState.phase = "fault";
    console.log(`[sim] phase=fault — brownout on ${targetAZ}`);
    await triggerBrownout(targetAZ, true);

    await sleep(5000);
    if (!simState.running) return;

    // Phase 3 — Policy: AI takes over
    simState.phase = "policy";
    console.log(`[sim] phase=policy — switching all routers to policy mode`);
    await Promise.all(ROUTER_IPS.map(url => setRouterMode(url, "policy")));

    await sleep(60000);
    if (!simState.running) return;

    // Phase 4 — Recovery: lift fault, restore shadow
    simState.phase = "recovery";
    console.log(`[sim] phase=recovery — lifting brownout`);
    await triggerBrownout(targetAZ, false);
    await Promise.all(ROUTER_IPS.map(url => setRouterMode(url, "shadow")));

    await sleep(10000);
    if (!simState.running) return;

    // Complete — build summary
    simState.phase   = "complete";
    simState.running = false;

    const durationSeconds = Math.round(
      (Date.now() - new Date(simState.startedAt).getTime()) / 1000
    );

    simState.summary = {
      targetAZ,
      durationSeconds,
      metricsCount: simState.metrics.length,
    };
    console.log(`[sim] complete in ${durationSeconds}s`);

  } catch (err) {
    console.log(`[sim] error: ${err.message || err}`);
    simState.phase   = "error";
    simState.running = false;
    // Restore safe state
    await triggerBrownout(simState.targetAZ, false).catch(() => {});
    await Promise.all(ROUTER_IPS.map(url => setRouterMode(url, "shadow").catch(() => {})));
  }
}

// ---------------------------------------------------------------
// Express app
// ---------------------------------------------------------------

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));

// Serve dashboard
app.get("/", (_req, res) => {
  res.sendFile(path.join(__dirname, "index.html"));
});

// ── Status ─────────────────────────────────────────────────────

app.get("/api/status", (_req, res) => {
  res.json({
    zones:        state.zones,
    spentRupees:  state.spentRupees,
    bytesMoved:   state.bytesMoved,
    limit:        SPEND_LIMIT,
    overLimit:    state.spentRupees > SPEND_LIMIT,
    incidents:    state.incidents.slice(0, 5),
    openIncident,
    receipt:      state.receipt || null,
  });
});

// ── History (time-series for charts) ───────────────────────────

app.get("/api/history", (_req, res) => {
  res.json({ history, sloMs: SLO_MS });
});

// ── Aggregated CloudWatch snapshot ─────────────────────────────

app.get("/api/cloudwatch", async (_req, res) => {
  const answers = await Promise.all(ROUTER_IPS.map(askRouter));
  const modes   = await Promise.all(ROUTER_IPS.map(getRouterMode));

  const zones     = {};
  let   anySpill  = false;

  answers.forEach((answer, i) => {
    if (!answer.ok) return;
    const az    = answer.source_az;
    const p99   = (answer.p99  || {})[az] || 0;
    const util  = (answer.util || {})[az] || 0;
    const spill = answer.spill || 0;
    zones[az] = {
      util,
      p99,
      spill,
      sloMissed: p99 > SLO_MS,
      healthy:   p99 < SLO_MS && util < 0.8,
      mode:      modes[i] || "unknown",
    };
    if (spill > 0) anySpill = true;
  });

  // Compute overall risk level
  const values  = Object.values(zones);
  const maxP99  = values.reduce((m, z) => Math.max(m, z.p99),  0);
  const maxUtil = values.reduce((m, z) => Math.max(m, z.util), 0);
  let risk = "healthy";
  if (maxUtil > 0.9 || maxP99 > SLO_MS)            risk = "critical";
  else if (maxUtil > 0.7 || maxP99 > SLO_MS * 0.8) risk = "high";
  else if (anySpill || maxUtil > 0.5)               risk = "elevated";

  res.json({ zones, anySpilling: anySpill, risk, sloMs: SLO_MS, timestamp: new Date().toISOString() });
});

// ── Bytes / Cost ────────────────────────────────────────────────

app.post("/api/bytes", (req, res) => {
  const bytes  = req.body.bytes || 0;
  state.bytesMoved  += bytes;
  state.spentRupees += (bytes / 1e9) * USD_PER_GB * RUPEES_PER_USD;
  res.json({ ok: true, spentRupees: state.spentRupees });
});

// ── Spend limit ─────────────────────────────────────────────────

app.post("/api/limit", (req, res) => {
  SPEND_LIMIT    = Number(req.body.limit) || 1000;
  state.mailSent = false;   // new limit resets the alert flag
  res.json({ ok: true, limit: SPEND_LIMIT });
});

// ── Routing mode (broadcasts to all routers) ────────────────────

app.post("/api/mode", async (req, res) => {
  const mode = req.body.mode;
  if (!["policy", "rule", "shadow"].includes(mode)) {
    return res.status(400).json({ error: "invalid mode — must be policy | rule | shadow" });
  }
  await Promise.all(ROUTER_IPS.map(url => setRouterMode(url, mode)));
  res.json({ ok: true, mode });
});

// ── Brownout proxy (passes through to the right backend via a router) ──

app.post("/api/brownout", async (req, res) => {
  const { az, on } = req.body;
  if (!az) return res.status(400).json({ error: "az required" });

  const result = await triggerBrownout(az, on);
  if (result) return res.json(result);
  res.status(502).json({ error: "no router answered the brownout request" });
});

// ── Bedrock chat ────────────────────────────────────────────────

app.post("/api/chat", async (req, res) => {
  const userMessage = (req.body.message || "").trim();
  if (!userMessage) return res.status(400).json({ error: "message required" });

  // Build live context for the model
  const latest     = history[history.length - 1];
  const contextStr = latest
    ? `Live system state: ${JSON.stringify(latest.zones)}. ` +
      `Total spill this period: ${latest.totalSpill.toFixed(2)}. ` +
      `Spend so far: ₹${state.spentRupees.toFixed(2)}.`
    : "No live metrics available yet.";

  const systemPrompt =
    "You are ZoneHeal's AI assistant, embedded in a real-time cloud dashboard. " +
    "You monitor three AWS Availability Zones in Sydney (ap-southeast-2). " +
    "Your job is to explain what the system is doing to a non-technical founder in 2-3 sentences — friendly, jargon-free. " +
    "Key terms: 'spill' = traffic rerouted to another zone; 'p99 latency' = slowest 1% of requests; " +
    "'utilization' = how busy a zone is (0-100%); 'SLO' = the 200ms latency promise to users. " +
    `Current system context: ${contextStr}`;

  chatHistory.push({ role: "user", content: userMessage });
  const recentHistory = chatHistory.slice(-12);  // last 6 turns each side

  try {
    const cmd = new InvokeModelCommand({
      modelId:     "anthropic.claude-haiku-4-5-20251001-v1:0",
      contentType: "application/json",
      body: JSON.stringify({
        anthropic_version: "bedrock-2023-05-31",
        max_tokens:        350,
        system:            systemPrompt,
        messages:          recentHistory,
      }),
    });

    const reply   = await bedrock.send(cmd);
    const parsed  = JSON.parse(new TextDecoder().decode(reply.body));
    const aiReply = parsed.content[0].text;

    chatHistory.push({ role: "assistant", content: aiReply });
    res.json({ message: aiReply });

  } catch (err) {
    console.log("[chat] bedrock error:", err.message || err);
    res.status(500).json({ error: String(err) });
  }
});

// ── Manual explain trigger ──────────────────────────────────────

app.post("/api/explain", async (_req, res) => {
  await explain();
  res.json(state.receipt || { error: "nothing to explain yet" });
});

// ── Simulation ──────────────────────────────────────────────────

app.post("/api/simulate/start", async (req, res) => {
  if (simState.running) {
    return res.status(400).json({ error: "simulation already running" });
  }

  // The target AZ can be passed by the client, or we fall back to the second
  // discovered AZ (if only one is known, use that).
  const azList   = Object.keys(azRouterMap);
  const targetAZ = req.body.targetAZ
    || azList[1]
    || azList[0]
    || "ap-southeast-2b";

  // Reset state
  simState = {
    running:         true,
    phase:           "baseline",
    startedAt:       new Date().toISOString(),
    metrics:         [],
    baselineMetrics: null,
    summary:         null,
    targetAZ,
  };

  // Fire async — do not await
  runSimulation(targetAZ).catch(err => {
    console.log("[sim] unhandled error:", err);
    simState.phase   = "error";
    simState.running = false;
  });

  res.json({ ok: true, phase: simState.phase, targetAZ });
});

app.post("/api/simulate/stop", async (_req, res) => {
  simState.running = false;

  // Always restore safe state
  if (simState.targetAZ) {
    await triggerBrownout(simState.targetAZ, false).catch(() => {});
  }
  await Promise.all(ROUTER_IPS.map(url => setRouterMode(url, "shadow").catch(() => {})));

  simState.phase = "idle";
  res.json({ ok: true });
});

app.get("/api/simulate/status", (_req, res) => {
  res.json({
    running:         simState.running,
    phase:           simState.phase,
    startedAt:       simState.startedAt,
    targetAZ:        simState.targetAZ,
    summary:         simState.summary,
    // Only send the last 5 metric snapshots (client builds its own chart arrays)
    latestMetrics:   simState.metrics.slice(-5),
    metricsCount:    simState.metrics.length,
    routerAZMap,
    discoveredAZs:   Object.keys(azRouterMap),
  });
});

// ── Boot ────────────────────────────────────────────────────────

load().then(async () => {
  await discoverRouters();

  // Initial poll so the dashboard has data the moment it opens
  await poll();

  setInterval(poll, POLL_SECONDS * 1000);
  setInterval(save, 60 * 1000);

  app.listen(PORT, () => {
    console.log(`\n  ZoneHeal dashboard → http://localhost:${PORT}`);
    console.log(`  Watching ${ROUTER_IPS.length} router tasks`);
    console.log(`  Alert email → ${ALERT_TO_EMAIL}\n`);
  });
});
