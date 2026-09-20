# ZoneHeal

A routing policy that can detect when a zone is slow but still healthy, then explain what it did and why it cost money.

**Live:** http://54.79.89.181:3000/
**Repo:** https://github.com/aaryaupadhya12/Z-heal

Built for First Commit, AWS × WeMakeDevs, 17–20 September 2026.

---

## The problem

Say you sell sweets and pickles online from India. Your customers are mostly Indians abroad, especially in the US and Australia. Your application runs across multiple Availability Zones.

Then Diwali arrives. Traffic increases several times over and one zone starts getting slow.

It is not down. Health checks are still green. Endpoint counts are still balanced. From the console, everything looks fine.

But customers in that zone are waiting several seconds for pages to load, and some leave.

You did not build the infrastructure yourself. A consultancy set it up and now only handles maintenance. If something goes wrong at 3am, there is nobody to call. Later, you still have to understand what happened and why the bill changed.

ZoneHeal watches latency instead of just endpoint counts. When a zone becomes slow, it moves some traffic away and explains what happened, what it cost, and why it made that decision.

---

## What AWS already does

ZoneHeal is not trying to replace AWS's existing routing.

Amazon ECS Service Connect provides zone aware routing using Envoy sidecars. It prefers endpoints in the caller's own Availability Zone and uses endpoint distribution to decide when traffic should cross zones.

The approach works well when endpoint distribution is a good representation of load. Endpoint counts are cheap, stable and widely available.

Our results show the same thing. When everything is normal, ZoneHeal is not meaningfully better than AWS's routing.

The problem appears during a brownout.

A brownout is when a zone is degraded but still passes its health checks. Endpoint counts do not change when a zone simply becomes slow, so the routing policy has no signal telling it to move traffic.

Making health checks more aggressive does not solve the problem either. Health checks are binary. They can remove an entire zone when the real problem only requires moving part of its traffic.

ZoneHeal treats this as a gradual problem and responds gradually.

---

## What we built

ZoneHeal uses a reinforcement learning policy that looks at three signals every minute:

| Signal | Question |
|---|---|
| Utilisation | How full is the zone? |
| Latency ratio | Was the zone slow compared with its SLO? |
| Spare capacity | Can another zone handle more traffic? |

This produces 45 states:

```
5 utilisation bands × 3 latency bands × 3 spare capacity bands
```

Each state has a score for each action. The router chooses the highest scoring action.

The actions determine how much traffic should be moved:

```
0%, 10%, 25%, or 50%
```

### Training data

The policy was trained using the Huawei Public Cloud Trace 2025 dataset, covering 31 days from Region 2.

We treat its four clusters as Availability Zones and use real request arrivals, pod counts, service times and cold starts. The failures are injected into this real workload.

### What the policy learned

```
              latency: FINE      | NEAR SLO   | OVER SLO
              spare:   lo mid hi | lo mid hi  | lo mid hi

u0  quiet         0  0   1  |  0  0   0  |  0  3   3
u1  light         0  0   0  |  0  0   0  |  0  1   3
u2  moderate      0  0   0  |  0  0   0  |  0  2   3
u3  busy          2  0   0  |  2  0   0  |  1  0   0
u4  full          2  0   0  |  2  0   0  |  3  0   0
```

The policy discovered four useful behaviours:

- **Quiet and fast:** stay local. There is no reason to pay for cross zone traffic.
- **Slow but not busy:** move some traffic. This is the brownout case that endpoint based routing cannot see.
- **Busy:** move traffic even if latency has not crossed the SLO yet.
- **No spare capacity elsewhere:** keep traffic where it is. Moving traffic into another full zone does not help.

28 of the 45 states were visited often enough during training to learn a different action. The remaining 17 use the default action of staying local.

---

## Results

Results are from held out test days across five seeds. Cross zone transfer is priced using AWS's published per GB rate.

| Condition | Agent | p99 (ms) | ₹/hr | % local |
|---|---|---|---|---|
| Brownout | **ZoneHeal** | **2221.7** | 69.0 | 72.4 |
| Brownout | Round robin | 2696.1 | 200.6 | 24.4 |
| Brownout | AWS zone aware | 2910.5 | 21.8 | 91.8 |
| Brownout | Force local | 3151.0 | 0.0 | 100.0 |
| Normal | **ZoneHeal** | **728.9** | 9.4 | 95.6 |
| Normal | Round robin | 730.1 | 200.6 | 24.4 |
| Normal | AWS zone aware | 728.8 | 21.8 | 91.8 |
| Normal | Force local | 728.4 | 0.0 | 100.0 |

The important part is what happens in the two conditions.

When everything is normal, all four approaches have almost identical latency. ZoneHeal costs ₹9.4/hr compared with ₹21.8/hr for the AWS zone aware policy.

When a zone becomes slow, endpoint counts stay the same. AWS therefore keeps 91.8% of traffic local.

ZoneHeal moves more traffic away from the degraded zone. Its p99 is 24% lower than the AWS baseline while still keeping 72.4% of traffic local.

The fact that the policies are essentially tied during normal operation is important. ZoneHeal only changes its behaviour when the additional signals actually indicate a problem.

### The baseline

We wanted the AWS comparison to come from the real algorithm rather than a description of it, so we gave an AI agent access to the Envoy repository (envoyproxy/envoy v1.28.0) and had it port the zone aware routing logic into Python. The port covers the integer scaled share calculation, both routing modes, residual capacity spilling, the early exit conditions and panic mode.

We reviewed the output against the worked examples in Envoy's source comments, but we have not run Envoy's own test suite against it.

---

## Deployment

The AWS deployment uses real requests, latency measurements and cross zone traffic.

The workload is generated rather than production traffic, and the failure is an endpoint that can be made slow on command.

```
                    Internet
                       |
                       v
             +----------------------+
             |    Router service    |
             |  3 Fargate tasks     |
             |  45 state policy     |
             +----+------+------+---+
                  |      |      |
                  v      v      v
              backend  backend  backend
                AZ-2a    AZ-2b    AZ-2c
                  \        |       /
                   \       |      /
                  structured logs
                        |
                        v
                   CloudWatch
                        |
              +---------+---------+
              |                   |
          p99 alarm          metrics
              |
          EventBridge
              |
            Lambda
              |
          watcher
              |
           Bedrock
              |
             SES
              |
          dashboard
```

### AWS services

| Service | Role |
|---|---|
| ECS on Fargate | Runs the routers, backends, load generator and dashboard |
| Service Connect | Service discovery between routers and backends |
| S3 | Stores the trained policy, incident receipts and watcher state |
| CloudWatch | Collects latency, request count, cross zone bytes and cost metrics |
| CloudWatch Alarms + EventBridge + Lambda | Provides the runtime fallback mechanism |
| Bedrock | Converts measured results into a plain English explanation |
| SES | Sends incident explanations by email |
| VPC, IGW, security groups, IAM | Provides the network and access control |
| ECR | Stores the container images |

### The guardrail

The learned policy does not run without a fallback.

CloudWatch monitors p99 over one minute periods. The alarm requires three consecutive breaching minutes before triggering.

When it fires, the routers fall back to rule based routing within ten seconds.

If the CloudWatch call itself fails, the router keeps its current mode rather than making a decision using missing control plane data.

### One deployment detail

Envoy's zone aware routing requires at least twice the number of Availability Zones in destination endpoints.

Our AWS deployment is below that threshold, so AWS disables zone aware routing and spreads traffic evenly. The AWS zone aware results in the table come from the trace environment, where the cluster is large enough to satisfy the requirement.

The two environments are kept separate in the comparison.

---

## The receipt

ZoneHeal does more than move traffic.

When it spends money moving traffic between zones, it records why.

For example:

> Zone C slowed to about 745ms while its health checks were still passing. The system moved half of its traffic to the other zones, which were responding normally. The transfer cost ₹0.0005.

The numbers come directly from our measurements. Bedrock only turns those measurements into readable sentences.

CloudWatch can show that cross zone traffic increased, but it does not explain why the routing system chose to spend that money or what the decision achieved.

The dashboard also has a chat box that is limited to the user's metrics. A user can ask questions such as "Why is Zone C slow?" and get an explanation using the same measured data.

`aws/README.md` contains the complete deployment and demo sequence.

---

## What we learned

**Free capacity alone was not enough.**

A broken zone becomes slow and therefore looks less busy. A policy based only on free capacity can actually send more traffic into the broken zone.

Using free capacity together with recent p99 fixed this.

**The router was in shadow mode for two days without us noticing.**

Shadow mode lets the rule act and only logs what the policy would have done. So the policy computed the correct action every minute and discarded it. Cross zone bytes stayed at zero and nothing errored. We assumed the routing logic was broken and debugged everything downstream of it first.

**The policy loader checked shape but not identity.**

A 216 state placeholder file containing random numbers passed every validation, because 216 matched its own table length. The router would have served decisions from noise without any error. It now refuses any file that is not 45 states.

**Change detection needs a stable comparison window.**

When every training iteration used a different random window, the detector produced 43 false alarms before a failure was injected. Keeping the window fixed removed those false alarms.

---

## Limitations

There are several limitations to the current evaluation.

- The evaluation uses one dataset and one region. The AWS deployment demonstrates that the policy works as real software, but it is not a production scale test.
- The AWS workload is generated and the failure is manually controlled.
- Half of the training iterations included a brownout, so the policy was trained on the same type of condition used during evaluation.
- `CAPACITY` is currently a declared backend constant rather than a measured service rate. Latency is measured, while utilisation is nominal.
- The cost numbers are tiny at demo scale. The measurement pipeline is end to end, but the traffic volume is not production scale.
- The latency model uses a single server queue approximation with p99 estimated as 4.6 × mean.
- Overload occurs in roughly 3% of minutes in the dataset, so routing decisions only matter during a small part of the workload.

---

## Prior work

The reinforcement learning harness in `Harness/` existed before the hackathon. It was originally built as a learning exercise around FrozenLake and Taxi v3 and is included as a dependency for the training code.

The work completed during the event includes:

- Routing environment
- State encoding and reward
- Envoy baseline port
- AWS deployment
- Live router state encoder and spill logic
- Observability and guardrail system
- Watcher
- Dashboard

AI coding tools used: Claude and GitHub Copilot. Usage is attributed per commit.

---

## Credits

**Dataset:** Huawei Public Cloud Trace 2025, CC BY 4.0, *Serverless Cold Starts and Where to Find Them*, EuroSys 2025

**Baseline:** Python port of Envoy zone aware routing, `envoyproxy/envoy` v1.28.0, Apache 2.0

**Feature under study:** Amazon ECS Service Connect zone aware routing

---

## Team

**Aarya Upadhya**
RL environment and policy, Envoy port, router state encoder and spill logic, CloudWatch metrics, watcher and Bedrock/SES path

**Anshull M Udyavar**
Dashboard, AWS infrastructure and deployment, CloudWatch dashboard and guardrail wiring, load generation
