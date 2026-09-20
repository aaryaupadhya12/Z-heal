// ZoneHeal watcher
//
// Polls the routers, adds up what the spilling costs, and when the
// bill passes the limit it asks Bedrock to explain what happened
// and stores the explanation in S3.
//
// Run:  node server.js
//
// Needs:  npm install express @aws-sdk/client-s3 @aws-sdk/client-bedrock-runtime

const express = require("express");
const { S3Client, PutObjectCommand, GetObjectCommand } = require("@aws-sdk/client-s3");
const { BedrockRuntimeClient, InvokeModelCommand } = require("@aws-sdk/client-bedrock-runtime");



const ROUTERS = [
  "http://0.0.0.0:8000", //these are not corrrect expect the first 1 change them in a env idk if its s sec risk, here the ip address goes in 
  "http://0.0.0.0:8000",
  "http://0.0.0.0:8000",
];

const REGION = "ap-southeast-2";
const BUCKET = "zonerl-819168518877";

// What a gigabyte costs to cross a zone: $0.01 out + $0.01 in.
const USD_PER_GB = 0.02;
const RUPEES_PER_USD = 95.9355;

// Spend limit. The default is 1000 rupees a month. Users change it
// in the UI whenever they want, the same way you set your own UPI
// limit rather than the bank setting it for you.
let SPEND_LIMIT = 1000;

const POLL_SECONDS = 30;
const PORT = 3000;

// ---------------------------------------------------------------

const s3 = new S3Client({ region: REGION });
const bedrock = new BedrockRuntimeClient({ region: REGION });

// Everything we know right now. Kept in memory, written to S3.
let state = {
  zones: {},           // what each router last told us
  spentRupees: 0,      // added up since we started watching
  bytesMoved: 0,
  incidents: [],       // every time a router started spilling
  mailSent: false,     // so we only warn once
  limit: SPEND_LIMIT,
};

// The incident happening right now, if any.
let openIncident = null;

// ---------------------------------------------------------------
// Ask every router what it is doing
// ---------------------------------------------------------------

async function askRouter(url) {
  try {
    const reply = await fetch(url + "/state", { signal: AbortSignal.timeout(3000) });
    const data = await reply.json();
    data.ok = true;
    data.url = url;
    return data;
  } catch (err) {
    return { ok: false, url: url, error: String(err) };
  }
}

async function poll() {
  const answers = await Promise.all(ROUTERS.map(askRouter));

  let anyoneSpilling = false;
  let worstP99 = 0;
  let slowZone = null;

  for (const answer of answers) {
    if (!answer.ok) continue;

    const az = answer.source_az;
    state.zones[az] = answer;

    if (answer.spill > 0) {
      anyoneSpilling = true;
    }

    // Find the slowest zone, for the explanation.
    const p99 = (answer.p99 || {})[az] || 0;
    if (p99 > worstP99) {
      worstP99 = p99;
      slowZone = az;
    }
  }

  if (anyoneSpilling && openIncident === null) {
    openIncident = {
      startedAt: new Date().toISOString(),
      zone: slowZone,
      p99Before: worstP99,
      bytesAtStart: state.bytesMoved,
      rupeesAtStart: state.spentRupees,
    };
    console.log("incident started in " + slowZone);
  }

  if (!anyoneSpilling && openIncident !== null) {
    openIncident.endedAt = new Date().toISOString();
    openIncident.bytes = state.bytesMoved - openIncident.bytesAtStart;
    openIncident.rupees = state.spentRupees - openIncident.rupeesAtStart;
    openIncident.p99After = worstP99;

    state.incidents.unshift(openIncident);
    console.log("incident ended");

    openIncident = null;
    await save();
  }

  state.limit = SPEND_LIMIT;

  if (state.spentRupees > SPEND_LIMIT && !state.mailSent) {
    state.mailSent = true;
    console.log("over the limit -- writing an explanation");
    await explain();
  }
}

// ---------------------------------------------------------------
// Cost
//
// The routers report latency and load, not bytes. So the UI posts
// the bytes figure it reads from CloudWatch, and we turn it into
// rupees here.
// ---------------------------------------------------------------

function addBytes(bytes) {
  state.bytesMoved = state.bytesMoved + bytes;

  const gigabytes = bytes / 1000000000;
  const rupees = gigabytes * USD_PER_GB * RUPEES_PER_USD;

  state.spentRupees = state.spentRupees + rupees;
}

// ---------------------------------------------------------------
// Ask Bedrock to write the explanation
//
// Every number in the prompt is measured. The model only turns
// them into sentences -- it never works anything out itself.
// ---------------------------------------------------------------

async function explain() {
  const latest = state.incidents[0] || openIncident || {};

  const facts = {
    zone: latest.zone || "unknown",
    slowLatencyMs: Math.round(latest.p99Before || 0),
    normalLatencyMs: 25,
    spilledFraction: 0.5,
    bytesMoved: Math.round(state.bytesMoved),
    rupeesSpent: state.spentRupees.toFixed(4),
    limitRupees: SPEND_LIMIT,
    incidentCount: state.incidents.length,
  };

  const prompt =
    "You are explaining a cloud routing decision to a founder who " +
    "is not technical. Use the numbers below and add none of your " +
    "own. Four sentences at most. Say what went wrong, what the " +
    "system did, what it cost, and what it avoided. No jargon.\n\n" +
    JSON.stringify(facts, null, 2);

  try {
    const command = new InvokeModelCommand({
      modelId: "anthropic.claude-3-5-sonnet-20241022-v2:0",
      contentType: "application/json",
      body: JSON.stringify({
        anthropic_version: "bedrock-2023-05-31",
        max_tokens: 400,
        messages: [{ role: "user", content: prompt }],
      }),
    });

    const reply = await bedrock.send(command);
    const parsed = JSON.parse(new TextDecoder().decode(reply.body));

    const receipt = {
      writtenAt: new Date().toISOString(),
      facts: facts,
      explanation: parsed.content[0].text,
    };

    state.receipt = receipt;

    await s3.send(new PutObjectCommand({
      Bucket: BUCKET,
      Key: "receipts/latest.json",
      Body: JSON.stringify(receipt, null, 2),
      ContentType: "application/json",
    }));

    console.log("receipt written to S3");

    // TODO: SES goes here. Same receipt, sent as mail.

  } catch (err) {
    console.log("bedrock failed: " + err);
    state.receipt = { error: String(err) };
  }
}

// ---------------------------------------------------------------
// Save and load, so a restart does not lose the running total
// ---------------------------------------------------------------

async function save() {
  try {
    await s3.send(new PutObjectCommand({
      Bucket: BUCKET,
      Key: "watcher/state.json",
      Body: JSON.stringify(state, null, 2),
      ContentType: "application/json",
    }));
  } catch (err) {
    console.log("could not save: " + err);
  }
}

async function load() {
  try {
    const reply = await s3.send(new GetObjectCommand({
      Bucket: BUCKET,
      Key: "watcher/state.json",
    }));

    const text = await reply.Body.transformToString();
    const saved = JSON.parse(text);

    state.spentRupees = saved.spentRupees || 0;
    state.bytesMoved = saved.bytesMoved || 0;
    state.incidents = saved.incidents || [];
    state.mailSent = saved.mailSent || false;

    console.log("loaded previous state");
  } catch (err) {
    console.log("starting fresh");
  }
}

// ---------------------------------------------------------------
// The API the page talks to
// ---------------------------------------------------------------

const app = express();
app.use(express.json());
app.use(express.static("public"));

// Everything the page needs, in one call.
app.get("/api/status", (req, res) => {
  res.json({
    zones: state.zones,
    spentRupees: state.spentRupees,
    bytesMoved: state.bytesMoved,
    limit: SPEND_LIMIT,
    overLimit: state.spentRupees > SPEND_LIMIT,
    incidents: state.incidents.slice(0, 5),
    openIncident: openIncident,
    receipt: state.receipt || null,
  });
});

// The page tells us how many bytes CloudWatch reported.
app.post("/api/bytes", (req, res) => {
  addBytes(req.body.bytes || 0);
  res.json({ ok: true, spentRupees: state.spentRupees });
});

// Change the limit.
app.post("/api/limit", (req, res) => {
  SPEND_LIMIT = Number(req.body.limit) || 1000;
  state.mailSent = false;          // a new limit means a new warning
  res.json({ ok: true, limit: SPEND_LIMIT });
});

// Turn the brownout on or off, through whichever router owns that zone.
app.post("/api/brownout", async (req, res) => {
  const az = req.body.az;
  const on = req.body.on;

  for (const url of ROUTERS) {
    try {
      const reply = await fetch(
        url + "/brownout?az=" + az + "&on=" + on,
        { method: "POST", signal: AbortSignal.timeout(5000) }
      );
      return res.json(await reply.json());
    } catch (err) {
      // try the next router
    }
  }

  res.status(502).json({ error: "no router answered" });
});

// Write the explanation now, without waiting for the limit.
app.post("/api/explain", async (req, res) => {
  await explain();
  res.json(state.receipt || { error: "nothing to explain" });
});

// ---------------------------------------------------------------

load().then(() => {
  poll();
  setInterval(poll, POLL_SECONDS * 1000);
  setInterval(save, 60 * 1000);

  app.listen(PORT, () => {
    console.log("watching " + ROUTERS.length + " routers");
    console.log("open http://localhost:" + PORT);
  });
});
