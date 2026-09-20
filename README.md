# ZoneHeal

**A routing policy that can see a zone which is slow but still healthy — and explains itself to the person paying the bill.**

Live: **http://54.79.89.181:3000/**
Repo: https://github.com/aaryaupadhya12/Z-heal

Built for First Commit, AWS × WeMakeDevs, 17–20 September 2026.

---

## The problem

Picture someone in India selling sweets and pickles online. Her customers
are Indians abroad — in the US, in Australia — people who miss home and
order from her around festivals. Her infrastructure sits across a few
Availability Zones so it's close to them.

Then Diwali arrives. Traffic goes up several times over, and one zone
starts getting slow. Not down. Just slow.

Every health check on it is green. Endpoint counts are balanced. Nothing
in the console says anything is wrong. But the customers routed to that
zone are watching pages take seconds to load, and some of them leave. At
our demo scale the money involved is small; on her biggest weekend of the
year, with a few thousand customers, it isn't.

And she can't fix it, because she didn't build this. A consultancy set it
up and handed it over, and now only does maintenance. There is nobody to
call at 3am, and nobody to explain the bill afterwards.

**ZoneHeal watches the latency instead of the endpoint count, moves part
of the traffic out of the slow zone, and then tells her what it did, what
it cost, and what that bought — in plain English, by email.**

---

## What AWS already does, and does well

This matters, because ZoneHeal is not a replacement for it.

Amazon ECS Service Connect gives every task an Envoy sidecar and ships
zone-aware routing on by default. When endpoints are balanced across AZs
it keeps more than 80% of traffic local, which cuts cross-AZ data transfer
charges and takes roughly a quarter off median network latency. It
rebalances automatically as endpoints scale, with no redeployment.

The way it decides is documented and deliberate:

- discover every endpoint of the destination service and which AZ it is in
- prefer endpoints in the caller's own AZ
- compare endpoint distribution percentages between the calling and called
  clusters; where a destination AZ has proportionally more endpoints, that
  surplus absorbs cross-zone traffic
- spill to other AZs when local endpoints are unhealthy or too few
- below 2 × the number of AZs endpoints, switch zone-aware routing off and
  spread evenly

Endpoint counts are cheap, stable and available everywhere. For the common
case this is the right design, and our own results agree: when nothing is
wrong, it is near-optimal and we cannot beat it.

**But the inputs are endpoint counts and health checks. Neither moves when
a zone is merely slow.** A brownout — degraded but passing every health
check — is invisible to it by construction, not by oversight.

You also can't fix this by tightening health checks. A health check is
binary, so it removes the entire zone the moment it trips, and flaps when
the zone sits near the threshold. What you want is to move *some* of the
traffic, which is a graded response to a graded problem.

---

## What we built

A reinforcement learning policy that reads three things every minute:

| Signal | The question it answers |
|---|---|
| Utilisation | How full am I? |
| Latency ratio (recent p99 ÷ SLO) | Was I slow last minute? |
| Spare room elsewhere | Can the other zones help? |

Five busyness bands × 3 latency bands × 3 spare-room bands = **45 states**.
Each state holds a score per action; the router takes the highest. Actions
are the fraction of traffic to send elsewhere: 0, 10%, 25% or 50%.

Trained on the **Huawei Public Cloud Trace 2025** (CC BY 4.0, EuroSys 2025)
— Region 2, 31 days, four clusters treated as Availability Zones. Real
request arrivals, real pod counts, real service times, real cold starts.
We inject the failures; the traffic is real.

### What it learned

```
              latency: FINE     | NEAR SLO   | OVER SLO
              spare:  lo mid hi | lo mid hi  | lo mid hi

u0  quiet        0  0   1   |  0  0   0  |  0  3   3    not busy but SLOW -> spill
u1  light        0  0   0   |  0  0   0  |  0  1   3
u2  moderate     0  0   0   |  0  0   0  |  0  2   3
u3  busy         2  0   0   |  2  0   0  |  1  0   0    BUSY -> spill
u4  full         2  0   0   |  2  0   0  |  3  0   0
```

Four rules, none of which we wrote:

- **Quiet and fast → stay local.** The whole top-left block is zero.
  Moving traffic is pure cost when latency is already fine.
- **Slow even when not busy → spill up to 50%.** This is the brownout
  response, and it is the behaviour endpoint counts cannot express.
- **Busy → spill regardless of latency.** Overload is coming.
- **No room elsewhere → don't pay to move.** Pushing traffic into a full
  zone helps nobody.

28 of the 45 states were visited often enough during training to move at
all. The other 17 keep the default of staying local — a safe fallback for
situations the policy has never seen.

---

## Results

Held-out test days, five seeds. Rupees per hour is cross-AZ transfer
priced at AWS's published per-GB rate.

| Condition | Agent | p99 (ms) | ₹/hr | % local |
|---|---|---|---|---|
| brownout | **ZoneHeal** | **2221.7** | 69.0 | 72.4 |
| brownout | round-robin | 2696.1 | 200.6 | 24.4 |
| brownout | AWS zone-aware (port) | 2910.5 | 21.8 | 91.8 |
| brownout | force-local | 3151.0 | 0.0 | 100.0 |
| normal | **ZoneHeal** | **728.9** | 9.4 | 95.6 |
| normal | round-robin | 730.1 | 200.6 | 24.4 |
| normal | AWS zone-aware (port) | 728.8 | 21.8 | 91.8 |
| normal | force-local | 728.4 | 0.0 | 100.0 |

**Read it in three lines:**

1. **When nothing is wrong, all four agents produce identical latency** —
   within a millisecond of each other. Only the bill differs, and
   ZoneHeal is the cheapest of the ones that stay responsive: ₹9.4/hr
   against zone-aware routing's ₹21.8, at the same speed.
2. **When a zone degrades, endpoint counts don't move**, so the incumbent
   keeps 91.8% of traffic pointed at the slow zones.
3. **ZoneHeal reacts**: 24% lower p99 than the incumbent, 18% lower than
   full load-spreading, at a third of the spreading cost — while still
   keeping 72.4% of traffic local.

The order matters. The tie under normal conditions is what makes the win
under degradation credible.

### The baseline

The comparison is not against a straw man. We ported Envoy's zone-aware
routing from source (`envoyproxy/envoy` v1.28.0, Apache-2.0) into Python:
the integer-scaled share calculation, both routing modes, residual-capacity
spilling, the early-exit conditions and panic mode. It is validated against
the worked examples in Envoy's own source comments — local shares 40/40/20
against upstream 25/50/25 giving 62.5% local — and against a published
issue where a zone with 2/6 of callers and 1/8 of servers keeps 37.5% local.

---

## The deployment

Nothing on AWS is simulated. The requests, the latency and the cross-AZ
traffic are all real. What is artificial is the workload (a load generator
rather than production traffic) and the failure (an endpoint that makes one
backend slow on command).

```
                    Internet
                        │
                        ▼
         ┌──────── router service ────────┐
         │   3 Fargate tasks, one per AZ  │
         │   45-state policy from S3      │
         └────┬───────────┬───────────┬───┘
              ▼           ▼           ▼
          backend-a   backend-b   backend-c
            AZ-2a       AZ-2b       AZ-2c
              │           │           │
              └─────── EMF logs ──────┘
                        │
                        ▼
                   CloudWatch
                   ├── p99, goodput, cross-AZ bytes, cost
                   └── p99 alarm ──► EventBridge ──► Lambda
                        │
                        ▼
                   watcher service ──► Bedrock ──► SES
                        │
                        ▼
                    dashboard
```

### AWS services

| Service | What it does here |
|---|---|
| **ECS on Fargate** | Routers, backends, load generator and dashboard. Tasks pinned to subnets so each zone is a real AZ, not an opaque regional pool. |
| **Service Connect** | Discovery between routers and backends under `zonerl.local`. |
| **S3** | Stores `policy.json`, trained offline, loaded by each router at startup. Also incident receipts and watcher state. |
| **CloudWatch** | Every request emits a structured log line that becomes a metric: latency, cross-AZ bytes, request count, and whether the request kept the 200ms promise. Drives the dashboard, goodput, and cost in rupees via metric math. |
| **CloudWatch Alarms + EventBridge + Lambda** | The runtime guardrail. p99 on the learned policy, three consecutive breaching minutes, missing data treated as not breaching. EventBridge routes the state change to a Lambda; the routers independently poll the alarm and fall back to rule-based routing. |
| **Bedrock** | Turns the measured numbers into plain English. All figures are rendered by our code — the model writes only the prose. |
| **SES** | Emails that explanation when cross-zone spend crosses the user's limit. |
| **VPC, IGW, security groups, IAM** | Three-AZ network, no NAT Gateway, narrowly scoped task roles. |
| **ECR** | Private repositories for all four images. |

### The guardrail

The learned policy never runs unsupervised. A CloudWatch alarm watches p99
for `Mode=policy` over one-minute periods and requires three consecutive
breaching datapoints — one slow minute is noise, three in a row is a
problem. If it fires, every router falls back to the plain rule within ten
seconds.

If the CloudWatch call itself fails, the routers **leave the mode
unchanged** rather than making a decision from missing control-plane
information.

### One thing to know about the deployment

Envoy's zone-aware routing requires at least 2 × the number of AZs
endpoints in the destination service. Our deployment runs below that
threshold, so on AWS it switches itself off and spreads traffic evenly —
the documented behaviour. **The zone-aware comparison in the results table
above is from the trace environment**, where the cluster is large enough.
We've kept those two clearly separate.

---

## The receipt

This is the part that isn't about routing.

When the routing spends money, the controller writes down why. Bedrock
turns the measurements into a few sentences and SES emails them:

> Zone C slowed to about 745ms, while every health check on it kept
> passing — so nothing would have alerted you. The system moved half of
> that zone's traffic to the others, which were responding normally. That
> cost ₹0.0005 in data moving between zones.

Every number in there is measured. The model writes sentences, not figures.

CloudWatch can tell you that cross-zone transfer went up. It can't tell you
that a controller chose to spend it, or what that bought — because that
reasoning only exists inside the thing that made the decision.

There's also a chat box on the dashboard, rate-limited and scoped to her
own metrics, so she can ask "why is Zone C slow" and get an answer in the
same register.

---

## Running it

```bash
# the routing environment and training
cd Harness
python train.py

# the AWS side
cd aws
docker build -t zonerl/router:latest .
# push to ECR, create a task definition revision, deploy

# the dashboard and watcher
cd Frontend
npm install
node zone_watched.js
```

`aws/README.md` has the full deployment and demo sequence, including the
endpoints and what each one does.

---

## What we learned

Every serious bug this weekend failed silently. The code ran and printed
plausible numbers.

**Spilling by free capacity alone sent traffic into the broken zone.** A
degraded zone is slow and therefore idle, so on free capacity it looks like
the most attractive destination. Latency got worse and the policy correctly
learned never to spill. Weighting destinations by free capacity ÷ recent
p99 fixed it. This is the project's own thesis biting us: capacity signals
alone are not enough.

**Our policy loader checked shape but not identity.** A 216-state
placeholder file full of random numbers passed every validation, because
216 matched its own table length. The router would have happily served
decisions from noise. It now refuses anything that isn't 45 states.

**The router sat in shadow mode for two days**, computing the correct
action and discarding it, while we debugged everything downstream of it.

**A change detector can't work if everything else is changing.** Sampling a
different random training window each iteration produced 43 false alarms
before any fault was injected. Holding the window fixed took it to zero.

**The reward's scope decides what can be localised.** With a system-wide
reward, a fault in one zone lowers the score of every decision everywhere,
so nothing stands out as regional. Scoring each decision by its own zone's
users restored the structure.

The habit that caught these: check against values known in advance. One
gigabyte crossing zones must cost exactly the published rate. Action 0 must
cost exactly zero. And always ask whether a number *should* have moved —
identical results across seeds means plumbing, not stability.

---

## Limitations

We'd rather state these than have them found.

- One dataset, one region, in the trace environment. The AWS deployment
  proves the policy runs as real software, not production-scale performance.
- The AWS workload is a load generator, and the failure is an endpoint we
  control. Neither is a naturally occurring AZ degradation.
- Half the training iterations included a brownout, so the policy was
  trained for the condition it is evaluated on. The incumbent cannot be
  trained at all, which is the argument, but it should be said plainly.
- `CAPACITY` on the backends is a declared constant, not a measured service
  rate. The latency signal is measured; the utilisation signal is nominal.
- Cost figures at demo scale are fractions of a rupee. The mechanism is
  measured end to end; the volume is not production.
- The latency model in the environment is a single-server queue
  approximation with p99 estimated as 4.6 × the mean.
- Overload occurs in roughly 3% of minutes, so routing rarely matters. That
  is a finding, not a flaw — it's why a policy that stays quiet is the
  right shape.

---

## Prior work

The reinforcement learning harness in `Harness/` **predates this
hackathon**. It was written for FrozenLake and Taxi-v3 as a learning
exercise before the event, and is included here as a dependency so the
training code runs. It is not claimed as hackathon work.

Built during the event: the routing environment, the state encoding and
reward, the Envoy baseline port, the AWS deployment, the router's live
state encoder and spill logic, the observability and guardrail path, the
watcher, and the dashboard.

AI coding tools used: Claude and GitHub Copilot. Attributed per commit.

---

## Credits

- **Dataset:** Huawei Public Cloud Trace 2025, CC BY 4.0 — *Serverless Cold
  Starts and Where to Find Them*, EuroSys 2025
- **Baseline:** Python port of Envoy zone-aware routing,
  `envoyproxy/envoy` v1.28.0, Apache-2.0
- **Feature under study:** Amazon ECS Service Connect zone-aware routing

## Team

- **Aarya Upadhya** — RL environment and policy, Envoy port, router state
  encoder and spill logic, CloudWatch metrics, watcher and Bedrock/SES path
- **Anshull M Udyavar** — dashboard, AWS infrastructure and deployment,
  CloudWatch dashboard and guardrail wiring, load generation
