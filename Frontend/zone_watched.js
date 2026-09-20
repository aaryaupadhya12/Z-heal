// ZoneHeal watcher
//
// Watches the routers, reads the real numbers out of CloudWatch,
// runs the demo scenarios, answers questions, and mails a receipt
// when the spend crosses the limit.
//
//   node zone_watched.js
//
//   npm install express dotenv @aws-sdk/client-s3
//     @aws-sdk/client-cloudwatch @aws-sdk/client-bedrock-runtime
//     @aws-sdk/client-ses

require("dotenv").config();

const express = require("express");
const fs = require("fs");
const path = require("path");

const { S3Client, PutObjectCommand, GetObjectCommand } = require("@aws-sdk/client-s3");
const { CloudWatchClient, GetMetricDataCommand } = require("@aws-sdk/client-cloudwatch");
const { BedrockRuntimeClient, InvokeModelCommand } = require("@aws-sdk/client-bedrock-runtime");
const { SESClient, SendEmailCommand } = require("@aws-sdk/client-ses");

// ---------------------------------------------------------------
// Config
// ---------------------------------------------------------------

const REGION = "ap-southeast-2";
const BUCKET = "zonerl-819168518877";
const PORT = 3000;

// Public IPs change every time ECS redeploys, so keep them in .env.
// Inside the VPC this becomes http://router:8000 and never changes.
const ROUTERS = [
  process.env.ROUTER_1,
  process.env.ROUTER_2,
  process.env.ROUTER_3,
].filter(Boolean);

// ---------------------------------------------------------------
// Rate limiting and off-topic filtering
// ---------------------------------------------------------------

const hits = {};                 // ip -> [timestamps]
const WINDOW = 3600000;          // one hour

function tooMany(ip, max) {
  const now = Date.now();
  hits[ip] = (hits[ip] || []).filter(t => now - t < WINDOW);

  if (hits[ip].length >= max) return true;

  hits[ip].push(now);
  return false;
}
const ON_TOPIC = /\b(zone|zones|latency|slow|fast|traffic|cost|costs|spend|spending|rupee|bill|route|routing|reroute|health|healthy|request|requests|promise|limit|incident|aws|region|utilisation|utilization|busy|policy|mode|p99|spill)\b/i;
const OFF_TOPIC = /ignore (previous|above|all)|system prompt|your instructions|write.{0,20}(code|script|function|poem|essay)|pretend|roleplay|jailbreak|repeat (after|the)|as an ai/i;
// Nova Micro: cheap, and it needs no use-case form.
const MODEL = "apac.amazon.nova-pro-v1:0";

const SLO_MS = 200;
const USD_PER_GB = 0.02;           // $0.01 out + $0.01 in
const RUPEES_PER_USD = 95.9355;
const POLL_SECONDS = 30;

// Must be verified in SES, or nothing sends.
const MAIL_FROM = process.env.ALERT_FROM_EMAIL;
const MAIL_TO = process.env.ALERT_TO_EMAIL;

const CHAT_LIMIT = 20;             // questions per hour
const HISTORY_POINTS = 60;         // 30 minutes at 30s

// ---------------------------------------------------------------

const s3 = new S3Client({ region: REGION });
const cw = new CloudWatchClient({ region: REGION });
const bedrock = new BedrockRuntimeClient({ region: REGION });
const ses = new SESClient({ region: REGION });

let limitRupees = 1000;

let zones = {};              // az -> whatever that router last said
let history = [];            // recent snapshots, for the chart
let incidents = [];          // finished incidents, newest first
let openIncident = null;
let receipt = null;
let mailSent = false;

let azToRouter = {};         // az -> router url
let chatTimes = [];          // for the rate limit
let chatTurns = [];          // conversation so far

let sim = { running: false, phase: "idle", targetAZ: null, startedAt: null };

// ---------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------

const sleep = ms => new Promise(r => setTimeout(r, ms));
const sum = list => list.reduce((a, b) => a + b, 0);
const rupeesFor = bytes => (bytes / 1e9) * USD_PER_GB * RUPEES_PER_USD;

async function get(url) {
  const r = await fetch(url, { signal: AbortSignal.timeout(3000) });
  return r.json();
}

async function post(url, params) {
  const qs = Object.entries(params || {}).map(([k, v]) => `${k}=${v}`).join("&");
  const r = await fetch(qs ? `${url}?${qs}` : url, {
    method: "POST",
    signal: AbortSignal.timeout(5000),
  });
  return r.json();
}

// ---------------------------------------------------------------
// Talking to the routers
// ---------------------------------------------------------------

async function findRouters() {
  for (const url of ROUTERS) {
    try {
      const health = await get(url + "/health");
      if (health.az) {
        azToRouter[health.az] = url;
        console.log(`found ${health.az} at ${url}`);
      }
    } catch {
      console.log(`could not reach ${url}`);
    }
  }
}

async function setMode(mode) {
  for (const url of ROUTERS) {
    try {
      await post(url + "/mode", { m: mode });
    } catch {}
  }
}

async function setBrownout(az, on) {
  // Any router can pass the request through.
  for (const url of ROUTERS) {
    try {
      return await post(url + "/brownout", { az, on });
    } catch {}
  }
  return null;
}


async function readMetrics(minutes) {
  const end = new Date();
  const start = new Date(end - minutes * 60000);

  const want = (id, name, stat, mode) => ({
    Id: id,
    MetricStat: {
      Metric: {
        Namespace: "ZoneRL",
        MetricName: name,
        Dimensions: [{ Name: "Mode", Value: mode }],
      },
      Period: 60,
      Stat: stat,
    },
  });

  const reply = await cw.send(new GetMetricDataCommand({
    StartTime: start,
    EndTime: end,
    ScanBy: "TimestampAscending",
    MetricDataQueries: [
      want("p99", "LatencyMs", "p99", "policy"),
      want("reqs", "Requests", "Sum", "policy"),
      want("good", "GoodRequests", "Sum", "policy"),
      want("bytes", "CrossAZBytes", "Sum", "policy"),
    ],
  }));

  const series = {};
  for (const r of reply.MetricDataResults) series[r.Id] = r.Values || [];

  const requests = sum(series.reqs || []);
  const bytes = sum(series.bytes || []);
  const p99s = series.p99 || [];

  // If the metric has no datapoints at all, nobody measured it.
  // That is not the same as measuring zero -- saying zero here
  // would tell the model every request failed.
  const measuredGood = (series.good || []).length > 0;
  const good = measuredGood ? sum(series.good) : null;

  return {
    p99Now: p99s.length ? p99s[p99s.length - 1] : null,
    p99Worst: p99s.length ? Math.max(...p99s) : null,
    requests,
    good,
    tooSlow: measuredGood ? requests - good : null,
    goodputPercent: measuredGood && requests ? (good / requests) * 100 : null,
    bytes,
    rupees: rupeesFor(bytes),
    minutes,
  };
}
// The running total, so the spend limit means something.
let spentRupees = 0;
let bytesMoved = 0;
let lastBytesSeen = 0;

async function updateSpend() {
  try {
    const m = await readMetrics(5);

    // Only count what is new since the last look.
    if (m.bytes > lastBytesSeen) {
      const fresh = m.bytes - lastBytesSeen;
      bytesMoved += fresh;
      spentRupees += rupeesFor(fresh);
    }
    lastBytesSeen = m.bytes;

  } catch (err) {
    console.log("could not read metrics: " + (err.message || err));
  }
}


async function poll() {
  const snapshot = { at: new Date().toISOString(), zones: {} };

  let spilling = false;
  let slowest = 0;
  let slowZone = null;
  let fastest = null;

  for (const url of ROUTERS) {
    let answer;
    try {
      answer = await get(url + "/state");
    } catch {
      continue;
    }

    const az = answer.source_az;
    const p99 = (answer.p99 || {})[az] || 0;
    const util = (answer.util || {})[az] || 0;
    const spill = answer.spill || 0;

    zones[az] = { ...answer, p99Own: p99, utilOwn: util };
    snapshot.zones[az] = { p99, util, spill };

    if (spill > 0) spilling = true;
    if (p99 > slowest) { slowest = p99; slowZone = az; }
    if (p99 > 0 && (fastest === null || p99 < fastest)) fastest = p99;
  }

  history.push(snapshot);
  if (history.length > HISTORY_POINTS) history.shift();

  await updateSpend();

  // An incident is any stretch where a zone is sending traffic away.
  if (spilling && !openIncident) {
    openIncident = {
      startedAt: new Date().toISOString(),
      zone: slowZone,
      slowLatencyMs: Math.round(slowest),
      normalLatencyMs: Math.round(fastest || 0),
      spillFraction: (zones[slowZone] || {}).spill || 0,
      rupeesAtStart: spentRupees,
    };
    console.log(`incident started in ${slowZone}`);
  }

  if (!spilling && openIncident) {
    openIncident.endedAt = new Date().toISOString();
    openIncident.rupees = spentRupees - openIncident.rupeesAtStart;
    incidents.unshift(openIncident);
    console.log("incident ended");
    openIncident = null;
    await save();
  }

  if (spentRupees > limitRupees && !mailSent) {
    mailSent = true;
    console.log("over the limit -- writing the receipt");
    await explain();
  }
}

// ---------------------------------------------------------------
// Bedrock
//
// Every number comes from a measurement. The model turns them
// into sentences and does no arithmetic of its own.
// ---------------------------------------------------------------

function readFormat(file) {
  try {
    return fs.readFileSync(path.join(__dirname, file), "utf8");
  } catch {
    return "Explain this to someone who is not an engineer, in plain "
         + "words, using only the numbers given.";
  }
}

async function askModel(prompt, systemText) {
  const body = {
    messages: [{ role: "user", content: [{ text: prompt }] }],
    inferenceConfig: { maxTokens: 500 },
  };

  if (systemText) body.system = [{ text: systemText }];

  const reply = await bedrock.send(new InvokeModelCommand({
    modelId: MODEL,
    contentType: "application/json",
    body: JSON.stringify(body),
  }));

  const parsed = JSON.parse(new TextDecoder().decode(reply.body));
  return parsed.output.message.content[0].text;
}

async function gatherFacts() {
  const m = await readMetrics(60).catch(() => ({}));

  // What is happening right now, per zone.
  const now = {};
  let slowestAZ = null;
  let slowest = 0;
  let fastest = null;

  for (const az in zones) {
    const p99 = zones[az].p99Own || 0;

    now[az] = {
      latencyMs: Math.round(p99),
      busyPercent: Math.round((zones[az].utilOwn || 0) * 100),
      sendingAwayPercent: Math.round((zones[az].spill || 0) * 100),
    };

    if (p99 > slowest) { slowest = p99; slowestAZ = az; }
    if (p99 > 0 && (fastest === null || p99 < fastest)) fastest = p99;
  }

  const live = openIncident !== null;

  return {
    // Right now. This is the only thing that is currently true.
    somethingWrongNow: live,
    zonesNow: now,
    promiseMs: SLO_MS,

    // The zone in trouble, only while there IS trouble.
    slowZone: live ? slowestAZ : null,
    slowLatencyMs: live ? Math.round(slowest) : null,
    normalLatencyMs: fastest === null ? null : Math.round(fastest),
    sentAwayPercent: live && slowestAZ
      ? Math.round((zones[slowestAZ].spill || 0) * 100)
      : 0,

    // Already over. Never describe this in the present tense.
    lastIncident: incidents[0]
      ? {
          zone: incidents[0].zone,
          wasMs: Math.round(incidents[0].slowLatencyMs || 0),
          endedAt: incidents[0].endedAt,
        }
      : null,

    requests: Math.round(m.requests || 0),
    requestsWithinPromise: m.good == null ? null : Math.round(m.good),
    requestsTooSlow: m.tooSlow == null ? null : Math.round(m.tooSlow),

    bytesMoved: Math.round(bytesMoved),
    rupeesSpent: spentRupees.toFixed(4),
    limitRupees: limitRupees,
    incidentsSoFar: incidents.length,
  };
}
async function explain() {
  const facts = await gatherFacts();

  let text = "Could not write the explanation.";

  try {
    text = await askModel(
      readFormat("receipt-format.md")
        + "\n\n---\n\nThe measurements:\n\n"
        + JSON.stringify(facts, null, 2)
    );
  } catch (err) {
    console.log("bedrock failed: " + (err.message || err));
  }

  receipt = { writtenAt: new Date().toISOString(), facts, explanation: text };

  try {
    await s3.send(new PutObjectCommand({
      Bucket: BUCKET,
      Key: "receipts/latest.json",
      Body: JSON.stringify(receipt, null, 2),
      ContentType: "application/json",
    }));
  } catch (err) {
    console.log("could not save the receipt: " + (err.message || err));
  }

  await sendMail(facts, text);
  return receipt;
}

// ---------------------------------------------------------------
// The mail
// ---------------------------------------------------------------

function mailHtml(facts, explanation) {
  const row = (label, value) => `
    <tr>
      <td style="padding:6px 0;color:#8d9ba8;font-size:13px;">${label}</td>
      <td style="padding:6px 0;color:#fff;font-size:13px;text-align:right;">
        <strong>${value}</strong>
      </td>
    </tr>`;

  return `
  <html><body style="font-family:Arial,sans-serif;background:#0f1b2d;padding:24px;">
    <div style="max-width:560px;margin:0 auto;background:#1b2a3b;border-radius:8px;padding:24px;">
      <h2 style="color:#f90;margin:0 0 16px;">Your cross-zone spend passed ₹${facts.limitRupees}</h2>
      <p style="color:#d5dbdb;line-height:1.6;font-size:15px;">${explanation}</p>
      <hr style="border:none;border-top:1px solid #2a3f55;margin:16px 0;">
      <table style="width:100%;border-collapse:collapse;">
              ${row("Zone affected",
              facts.slowZone
                ? "Zone " + facts.slowZone.slice(-1).toUpperCase()
                : "nothing slow right now")}
        ${row("Slowest response",
              facts.slowLatencyMs == null ? "—" : facts.slowLatencyMs + " ms")}
        ${row("Normally",
              facts.normalLatencyMs == null ? "—" : facts.normalLatencyMs + " ms")}
        ${row("Data moved between zones", (facts.bytesMoved / 1e6).toFixed(2) + " MB")}
        ${row("Spent", "₹" + facts.rupeesSpent)}
        ${row("Requests kept within the promise",
              facts.requestsWithinPromise == null
                ? "not measured yet"
                : facts.requestsWithinPromise.toLocaleString()
                  + " of " + facts.requests.toLocaleString())}
      </table>
      <p style="color:#5a7a90;font-size:11px;margin:20px 0 0;">
        Sent by ZoneHeal. Change your limit on the dashboard.
      </p>
    </div>
  </body></html>`;
}

async function sendMail(facts, explanation) {
  if (!MAIL_FROM || !MAIL_TO) {
    console.log("no mail addresses set -- skipping");
    return { sent: false, reason: "not configured" };
  }

  try {
    await ses.send(new SendEmailCommand({
      Source: MAIL_FROM,
      Destination: { ToAddresses: [MAIL_TO] },
      Message: {
        Subject: {
          Data: `Cross-zone spend passed ₹${facts.limitRupees}`,
          Charset: "UTF-8",
        },
        Body: {
          Html: { Data: mailHtml(facts, explanation), Charset: "UTF-8" },
          Text: { Data: explanation, Charset: "UTF-8" },
        },
      },
    }));

    console.log("mail sent to " + MAIL_TO);
    return { sent: true };

  } catch (err) {
    // SES only sends to verified addresses until the account leaves
    // the sandbox. Say so rather than failing silently.
    console.log("mail failed: " + (err.message || err));
    return { sent: false, reason: String(err.message || err) };
  }
}

// ---------------------------------------------------------------
// Saving
// ---------------------------------------------------------------

async function save() {
  try {
    await s3.send(new PutObjectCommand({
      Bucket: BUCKET,
      Key: "watcher/state.json",
      Body: JSON.stringify({ spentRupees, bytesMoved, incidents, mailSent, receipt }, null, 2),
      ContentType: "application/json",
    }));
  } catch {}
}

async function load() {
  try {
    const reply = await s3.send(new GetObjectCommand({
      Bucket: BUCKET,
      Key: "watcher/state.json",
    }));

    const saved = JSON.parse(await reply.Body.transformToString());

    spentRupees = saved.spentRupees || 0;
    bytesMoved = saved.bytesMoved || 0;
    incidents = saved.incidents || [];
    mailSent = saved.mailSent || false;
    receipt = saved.receipt || null;

    console.log("loaded what we knew before");
  } catch {
    console.log("starting fresh");
  }
}

// ---------------------------------------------------------------
// The demo scenarios
//
// The router works out its state once a minute, so every phase
// has to be longer than a minute or it will not have noticed yet.
// ---------------------------------------------------------------

const MINUTE = 60000;

async function runScenario(targetAZ) {
  sim = {
    running: true,
    phase: "baseline",
    targetAZ,
    startedAt: new Date().toISOString(),
  };

  try {
    // 1. Nothing wrong. The policy should keep everything local.
    await setMode("policy");
    await sleep(MINUTE);
    if (!sim.running) return;

    // 2. Break the zone. Still healthy, just slow.
    sim.phase = "fault";
    await setBrownout(targetAZ, true);

    // Long enough for a fresh measurement window to close.
    await sleep(MINUTE + 15000);
    if (!sim.running) return;

    // 3. Watch it react.
    sim.phase = "reacting";
    await sleep(MINUTE);
    if (!sim.running) return;

    // 4. Fix the zone and watch it settle.
    sim.phase = "recovery";
    await setBrownout(targetAZ, false);
    await sleep(MINUTE + 15000);

    sim.phase = "complete";
    sim.running = false;

  } catch (err) {
    console.log("scenario failed: " + (err.message || err));
    sim.phase = "error";
    sim.running = false;
    await setBrownout(targetAZ, false).catch(() => {});
  }
}

// ---------------------------------------------------------------
// The API
// ---------------------------------------------------------------

const app = express();
app.use(express.json());
app.use(express.static(__dirname));

// Everything the dashboard needs, in one call.
app.get("/api/status", async (_req, res) => {
  let metrics = null;

  try {
    metrics = await readMetrics(60);
  } catch (err) {
    console.log("metrics unavailable: " + (err.message || err));
  }

  res.json({
    zones,
    metrics,
    spentRupees,
    bytesMoved,
    limit: limitRupees,
    overLimit: spentRupees > limitRupees,
    incidents: incidents.slice(0, 5),
    openIncident,
    receipt,
    sloMs: SLO_MS,
  });
});

// Just the routers' own view, for the zone cards.
app.get("/api/zones", (_req, res) => {
  const out = {};
  let worst = 0;

  for (const az in zones) {
    const p99 = zones[az].p99Own || 0;
    const util = zones[az].utilOwn || 0;

    out[az] = {
      p99,
      util,
      spill: zones[az].spill || 0,
      tooSlow: p99 > SLO_MS,
    };

    if (p99 > worst) worst = p99;
  }

  res.json({
    zones: out,
    risk: worst > SLO_MS ? "degraded" : "healthy",
    sloMs: SLO_MS,
    at: new Date().toISOString(),
  });
});

app.get("/api/history", (_req, res) => {
  res.json({ history, sloMs: SLO_MS });
});

app.post("/api/limit", (req, res) => {
  limitRupees = Number(req.body.limit) || 1000;
  mailSent = false;                  // a new limit deserves a new warning
  res.json({ ok: true, limit: limitRupees });
});

app.post("/api/mode", async (req, res) => {
  const mode = req.body.mode;

  if (!["policy", "rule", "shadow"].includes(mode)) {
    return res.status(400).json({ error: "mode must be policy, rule or shadow" });
  }

  await setMode(mode);
  res.json({ ok: true, mode });
});

app.post("/api/brownout", async (req, res) => {
  if (!req.body.az) return res.status(400).json({ error: "which zone?" });

  const result = await setBrownout(req.body.az, req.body.on);

  if (result) return res.json(result);
  res.status(502).json({ error: "no router answered" });
});

app.post("/api/explain", async (_req, res) => {
  if (tooMany(req.ip, 5)) {
    return res.status(429).json({ error: "try again in a little while" });
  }

  res.json(await explain());
});

// Her question, answered from her own numbers.
app.post("/api/chat", async (req, res) => {
  const question = String(req.body.message || "").trim().slice(0, 500);
  if (!ON_TOPIC.test(question)) {
  return res.json({
    message: "I can only answer questions about your zones — how they're "
           + "responding, what the routing is doing, and what it's costing.",
  });
}

  if (!question) return res.status(400).json({ error: "ask something" });

  // Not a question about her system.
  if (OFF_TOPIC.test(question)) {
    return res.json({
      message: "I can only answer questions about your zones — how they're "
             + "responding, what the routing is doing, and what it's costing.",
    });
  }

  if (tooMany(req.ip, CHAT_LIMIT)) {
    return res.status(429).json({
      error: "that is a lot of questions -- try again in a little while",
    });
  }

  const facts = await gatherFacts();

  const systemText =
    readFormat("chat-format.md")
    + "\n\nHer measurements right now:\n"
    + JSON.stringify(facts, null, 2);

  try {
    const answer = await askModel(question, systemText);

    chatTurns.push({ q: question, a: answer });
    if (chatTurns.length > 12) chatTurns.shift();

    res.json({ message: answer });

  } catch (err) {
    console.log("chat failed: " + (err.message || err));
    res.status(502).json({ error: "could not answer just now" });
  }
});

app.post("/api/scenario/start", (req, res) => {
  if (tooMany(req.ip, 6)) {
    return res.status(429).json({ error: "too many runs -- give it a rest" });
  }
  if (sim.running) {
    return res.status(400).json({ error: "one is already running" });
  }

  const known = Object.keys(azToRouter);
  const targetAZ = req.body.targetAZ || known[0];

  if (!targetAZ) {
    return res.status(400).json({ error: "no zones found" });
  }

  runScenario(targetAZ).catch(err => {
    console.log("scenario error: " + err);
    sim.phase = "error";
    sim.running = false;
  });

  res.json({ ok: true, targetAZ, phase: sim.phase });
});

app.post("/api/scenario/stop", async (_req, res) => {
  sim.running = false;

  if (sim.targetAZ) await setBrownout(sim.targetAZ, false).catch(() => {});

  await setMode("policy");           // leave it running, not idle
  sim.phase = "idle";

  res.json({ ok: true });
});

app.get("/api/scenario/status", (_req, res) => {
  res.json({ ...sim, zonesFound: Object.keys(azToRouter) });
});

// ---------------------------------------------------------------

load().then(async () => {
  await findRouters();
  await poll();

  setInterval(poll, POLL_SECONDS * 1000);
  setInterval(save, MINUTE);

  app.listen(PORT, () => {
    console.log(`\n  ZoneHeal watcher on http://localhost:${PORT}`);
    console.log(`  Watching ${ROUTERS.length} routers`);
    console.log(`  Mail to ${MAIL_TO || "nobody -- set ALERT_TO_EMAIL"}\n`);
  });
});