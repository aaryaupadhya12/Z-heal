# Huawei Dataset and Simulator Contracts

This document defines how the Huawei serverless dataset is interpreted by the simulator. It is intentionally separate from the main project README so the architecture overview remains concise.

The dataset supplies real traces. The simulator supplies the missing distributed-system behavior needed for the hackathon scenario: AWS-style zones, traffic movement, network cost, failures, customer cost, and fallback control.

The simulator must never present assumed values as measured Huawei values.

---

## 1. Source of truth and scope

The source dataset contains four logical data products:

1. cold-start events for all five regions over 31 days
2. request-level tables for day 30
3. per-function, per-minute time series for all five regions and 31 days
4. runtime and trigger metadata for Region 2

The first simulator version should use the first three. Runtime and trigger metadata can be added later for policy features or stratified evaluation.

The dataset describes serverless execution. In this project, a `clusterName` is treated as the closest available analogue to a zone. This is a simulator mapping, not a claim that Huawei clusters and AWS Availability Zones are identical.

---

## 2. Units and time contract

### 2.1 Duration units

Dataset duration fields are in **seconds**.

The simulator may expose latency metrics in milliseconds because the SLO is expressed in milliseconds, but conversion must happen explicitly at the adapter boundary:

```python
latency_ms = latency_seconds * 1000.0
```

Never compare a raw dataset duration directly with a millisecond SLO.

For example:

```python
SLO_MS = 200.0
observed_ms = row["totalCost_frontend"] * 1000.0
slo_breached = observed_ms > SLO_MS
```

### 2.2 Timestamp units

`time` and related timestamps begin at zero and are measured from the start of the dataset. They are not calendar timestamps.

The adapter must preserve:

- `time_s`: original dataset time in seconds
- `day`: dataset-relative day, normally 0 through 30
- `minute`: integer minute derived from the dataset time

A simulator must not invent dates, geography, or calendar labels unless they are explicitly stored as assumptions.

Recommended conversion:

```python
minute = int(time_s // 60)
day = int(time_s // 86400)
```

The dataset's holiday period must be identified from traffic behavior and checked against the paper. It must not be hard-coded as a calendar date when no calendar date exists in the files.

---

## 3. Identity contract

### 3.1 Cluster identity

`clusterName` identifies the cluster associated with an event or request. The expected values are cluster identifiers 1 through 4 in the request data, subject to the exact source encoding.

The loader must preserve the original value and create a normalized value only when needed:

```python
cluster_id = str(row["clusterName"])
```

Do not silently renumber clusters. If a file contains a fifth region but no cluster-level identity, it must remain region-level data.

### 3.2 Function identity

A function is identified by:

```text
funcID = funcName---userID---poolName
```

Example:

```text
400---418---pool22-300-128
```

The `poolName` is obtained from the pod identifier or source metadata according to the dataset's exact naming convention.

The adapter must keep both forms:

- original columns: `funcName`, `userID`, `podID`
- normalized key: `funcID`

Never use `funcName` alone as the function identity because different users or pools may have the same function name.

### 3.3 Pod identity and capacity metadata

`podID` identifies a worker pod. The pod name includes size information, for example:

```text
pool24-600-512
```

The simulator should parse the declared capacity where possible:

- CPU: 600 millicores
- memory: 512 MB

Parsing must be validated against the real naming convention before relying on it. If parsing fails, retain the raw `podID` and mark capacity as unavailable rather than guessing.

---

## 4. File contracts

## 4.1 Cold-start event contract

### Meaning

One row represents one cold start: a new worker had to be started before serving work.

### Coverage

- all five regions
- all 31 days
- cluster identity on every row

### Required fields

| Field | Meaning | Unit | Simulator use |
|---|---|---:|---|
| `day` | dataset-relative day | day | time grouping and replay |
| `time` | event timestamp from dataset start | seconds | ordering and minute aggregation |
| `clusterName` | cluster / simulated zone | identifier | zone state and bucket |
| `funcName` | function name | identifier | feature and grouping |
| `userID` | customer/user identity | identifier | function identity and customer cost |
| `totalCost_cold_start` | total cold-start delay | seconds | cold-start latency |
| `podAllocationCost` | pod allocation portion | seconds | latency attribution |
| `deployCodeCost` | code deployment portion | seconds | latency attribution |
| `deployDependencyCost` | dependency deployment portion | seconds | latency attribution |
| `schedulingCost` | scheduling portion | seconds | latency attribution |
| `podID` | worker pod identifier | identifier | capacity and pool parsing |

### Required checks

The loader must check that:

```text
all component costs are non-negative
all component costs are in seconds
sum(component costs) is approximately totalCost_cold_start
clusterName is present
funcName and userID are present
```

The sum check should use a documented tolerance because source rounding may exist:

```python
components = (
    row["podAllocationCost"]
    + row["deployCodeCost"]
    + row["deployDependencyCost"]
    + row["schedulingCost"]
)
consistent = abs(components - row["totalCost_cold_start"]) <= tolerance_s
```

### Simulator use

This file is the main source for:

- cold starts per cluster per minute
- cold-start delay distributions
- cluster-specific warm/cold behavior
- cold-start cost components
- realistic failure or delay injection around worker creation

It is the only supplied file with cluster identity on every row for all 31 days.

---

## 4.2 Request-level contract

### Meaning

One row represents one request and contains an end-to-end timing breakdown.

### Coverage limitations

- day 30 only
- Region 1 includes only the top 100 pods per function
- Region 3 is a subsample

These limitations must be written into the simulation metadata and evaluation report. Request-level counts must not be described as complete 31-day traffic.

### Required fields

| Field | Meaning | Unit | Simulator use |
|---|---|---:|---|
| `time_worker` | worker-side timestamp | seconds | ordering / correlation |
| `time_frontend` | frontend-side timestamp | seconds | ordering / correlation |
| `clusterName` | serving cluster | identifier | traffic distribution |
| `funcName` | function name | identifier | function grouping |
| `userID` | customer/user identity | identifier | customer grouping |
| `podID` | serving worker pod | identifier | pod capacity grouping |
| `totalCost_frontend` | end-to-end request time | seconds | customer-visible latency |
| `totalCost_worker` | worker processing time | seconds | worker latency |
| `runtimeCost` | runtime portion | seconds | cost decomposition |
| `workerCost` | worker portion | seconds | cost decomposition |
| `busCost` | bus or transport portion | seconds | cost decomposition |
| `frontendCost` | frontend portion | seconds | cost decomposition |
| `readBodyCost` | request-body read time | seconds | cost decomposition |
| `writeRspCost` | response-write time | seconds | cost decomposition |
| `cpu_usage` | CPU used by pod/request | cores | CPU cost and state |
| `memory_usage` | memory used by pod/request | MB | memory cost and state |
| `requestBodySize` | request body size | bytes | data-transfer cost |

### Simulator use

This file is the main source for:

- day-30 traffic split across clusters
- request-level latency distributions
- request-size distributions
- CPU and memory usage observations
- cost attribution features

### Response-size assumption

The dataset records request body size but not response size. The simulator must expose response size as an assumption:

```python
response_size_bytes = requestBodySize * RESPONSE_SIZE_RATIO
```

The default ratio may be 1.0 for a neutral baseline, but it must be configurable and reported in every run.

---

## 4.3 Per-minute time-series contract

### Meaning

One row represents a function and minute summary for a region.

### Coverage

- all five regions
- all 31 days
- function-level, per-minute summaries
- region-level rather than cluster-level

### Expected metrics

The files contain summary statistics, including average, standard deviation, and quantiles from 0 to 1.00. The exact column spelling must be taken from the source demo notebook before writing a final loader.

Important logical metrics include:

| Metric | Meaning | Simulator use |
|---|---|---|
| `requests` | requests per minute | arrivals |
| `num_pods` | running pod count | capacity |
| `num_cold_starts` | cold starts in the minute | warmness and startup pressure |
| `totalCost` | execution time | worker performance |
| `totalCost_frontend` | end-to-end time | customer-visible SLO |
| `requestBodySize` | request size | transfer-cost estimate |
| `cpu_usage` | CPU usage | CPU pressure and cost |
| `memory_usage` | memory usage | memory pressure and cost |
| cost breakdowns | component timing/cost summaries | accounting and diagnosis |

### Quantile contract

Quantile columns must retain their quantile identity. For example, p50, p90, p95, and p99 must not be loaded as generic unnamed values.

The adapter should normalize them to explicit names such as:

```text
requests_mean
requests_std
requests_p50
requests_p90
requests_p95
requests_p99
```

The same naming scheme should be used for latency, CPU, memory, and cost metrics where present.

### Cluster limitation

This file is region-level. It cannot directly produce cluster-level traffic for all 31 days.

The simulator must derive cluster traffic using day-30 cluster shares:

```text
simulated_cluster_requests(region, function, minute)
    = regional_requests(region, function, minute)
    * day30_cluster_share(region, function, cluster)
```

The share calculation must be documented and must handle zero-request groups deterministically.

---

## 4.4 Runtime and trigger metadata contract

This file is available for Region 2 and contains function metadata such as:

- runtime language, for example Python3
- trigger type
- synchronous/asynchronous call behavior
- requested CPU

It is not required for the first simulator version. When added, it should be joined by the normalized `funcID`, not only by `funcName`.

Potential uses:

- policy features
- workload stratification
- runtime-specific cold-start behavior
- synchronous customer-impact weighting
- requested CPU versus observed CPU analysis

---

## 5. Adapter contract

The dataset adapter is responsible for converting source files into a stable simulator representation. It must not contain policy logic, fallback logic, or AWS API calls.

Recommended interface:

```python
class HuaweiDatasetAdapter:
    def load_cold_starts(self, path):
        """Return validated cold-start event records."""

    def load_requests(self, path):
        """Return validated day-30 request records."""

    def load_timeseries(self, path):
        """Return validated per-region minute records."""

    def load_runtime_metadata(self, path):
        """Return optional runtime and trigger metadata."""

    def build_cluster_shares(self, requests):
        """Return day-30 cluster shares by region/function."""

    def build_simulation_frame(self, cold_starts, requests, timeseries):
        """Return the normalized state/features consumed by the environment."""
```

Each loader should return records with:

- original source values
- normalized IDs
- explicit units
- source coverage metadata
- validation warnings

A normalized record should make units obvious. For example:

```python
{
    "time_s": 123.0,
    "latency_s": 0.150,
    "latency_ms": 150.0,
    "cluster_id": "1",
    "region_id": "3",
    "func_id": "400---418---pool22-300-128",
    "cpu_cores": 0.25,
    "memory_mb": 128.0,
    "request_body_bytes": 1024,
    "source": "request_table_day30",
}
```

The adapter should preserve both `latency_s` and `latency_ms` so unit mistakes are visible during debugging.

---

## 6. Simulator environment contract

The simulator may be exposed as a Gymnasium environment so existing training code can use it. It must represent a simulated AWS-style execution system, not claim to observe live AWS.

Recommended state features:

```text
region or simulated zone
function identity or function bucket
minute/day position
request rate
running pod count
cold-start rate
p50/p95/p99 latency
CPU usage
memory usage
queue or backlog estimate
current failure flag
current policy/bucket state
```

Recommended action space:

```text
traffic spill percentage
target cluster
worker/pod capacity adjustment
retry limit
fallback selection
```

The first implementation may use a discrete action table even if the underlying production decision is continuous. If continuous actions are added later, preserve the action-to-cost and action-to-latency accounting contract.

Required Gymnasium methods:

```python
class HuaweiAwsSimulationEnv:
    def reset(self, seed=None, options=None):
        return observation, info

    def step(self, action):
        return observation, reward, terminated, truncated, info
```

The environment should also expose:

```python
def set_fault(self, fault):
    """Enable or disable a deterministic simulation fault."""
```

Faults should be scenario-controlled and reproducible, for example:

- a zone becomes slow
- cold-start delay increases
- available CPU decreases
- request volume spikes
- a target cluster becomes unavailable
- p99 crosses the SLO threshold

---

## 7. P99 and failure-detection contract

The control loop must distinguish an ordinary noisy observation from a failure.

A minimum p99 rule is:

```python
p99_breached = observed_p99_ms > slo_ms
```

A more stable detector can require a consecutive breach window:

```python
failure = consecutive_p99_breaches >= breach_window
```

The simulator should record:

- observed p99
- SLO threshold
- breach count
- first breach time
- detection time
- action time
- recovery time
- whether the breach was caused by the learned action or by the injected fault

The consensus or average-steps check should be explicit. For example:

```python
step_excess = observed_steps - consensus_steps
failed_progress = step_excess > allowed_step_excess
```

Do not use a hidden threshold. Store the values that caused the decision in the transition info.

---

## 8. Cost contract

The simulator must calculate cost as a transparent estimate, not as a real AWS bill.

At minimum, separate:

```text
compute cost
memory cost
request cost
data-transfer cost
cold-start cost
retry cost
failure or wasted-transition cost
customer-impact penalty
```

A generic transition accounting record should look like:

```python
{
    "compute_cost": ...,
    "memory_cost": ...,
    "request_cost": ...,
    "network_cost": ...,
    "cold_start_cost": ...,
    "retry_cost": ...,
    "wasted_transition_cost": ...,
    "customer_impact_penalty": ...,
    "total_estimated_cost": ...,
    "pricing_source": "configured_assumption",
}
```

AWS pricing must be versioned or stored in configuration. The simulator should record:

- pricing region
- pricing date/version
- CPU or compute rate
- memory rate
- request rate
- data-transfer rate
- any free-tier assumption

If the policy fails to reach the expected result, calculate the cost of the failed transition and pass that result to the fallback decision.

---

## 9. Fallback and safety contract

The learned policy must not be the only controller. The simulator needs a bounded fallback path that protects customer experience.

Fallback is triggered when one or more of the following is true:

- p99 remains above the SLO for the configured breach window
- progress does not reach the consensus step target
- estimated failed-transition cost exceeds the allowed budget
- policy confidence is below the configured threshold
- the environment reports a target-zone failure
- the action would exceed a CPU, memory, retry, or traffic-spill limit

Fallback actions may include:

- a cost-aware heuristic routing decision
- bounded traffic spill
- reduced retry count
- selecting a known healthy zone
- stopping a failing transition
- restoring the last-known-good policy or bucket snapshot

The fallback must be described as an application baseline or AWS-style heuristic unless a specific AWS service algorithm is actually being invoked.

The simulator should record:

```python
{
    "controller": "learned_policy" or "fallback_heuristic",
    "fallback_reason": ...,
    "fallback_action": ...,
    "customer_experience_protected": True or False,
}
```

The design goal is not to maximize learning reward at any cost. It is to keep customer impact bounded while allowing the learned controller to improve cost and latency when its actions are working.

---

## 10. Missing-data assumptions

The following values are not directly supplied and must be configured, logged, and varied in sensitivity tests.

| Missing value | Required simulator assumption |
|---|---|
| per-cluster traffic for all 31 days | apply day-30 cluster shares to region-level time series |
| response size | use a configured ratio to request body size |
| inter-cluster latency | assume a configured value, such as 1-2 ms, and vary it |
| network pricing | use an AWS published per-GB rate stored in configuration |
| requests per pod capacity | estimate from requests divided by pods during busy periods |
| health and failure status | inject deterministic scenario faults |
| calendar dates and geography | leave unknown; do not fabricate them |
| complete request coverage | preserve day-30 sampling/top-pod limitations |

Every experiment result should include the assumption configuration used to produce it.

---

## 11. Dataset validation checklist

Before training or simulation:

- confirm all expected files exist
- inspect the demo notebook or source schema before finalizing column names
- validate required columns
- verify duration units are seconds
- verify timestamps start at zero
- check cluster and region coverage
- check function IDs are unique after normalization
- check cost components are non-negative
- check timing component sums against totals
- check quantile columns are correctly identified
- report missing values and duplicate rows
- record request-table coverage limitations
- write a validation summary to the run output

A run should fail early if required columns are absent or if unit validation fails. Silent schema coercion is dangerous because it can create plausible but incorrect latency and cost results.

---

## 12. Training and evaluation split

The model may be trained on the Huawei-derived dataset and then evaluated in a separate simulated AWS scenario.

The result must be described as:

```text
trained from Huawei traces
simulated using AWS-style zones, pricing, failures, and control actions
```

It must not be described as measured AWS production performance.

Recommended evaluation splits:

- training days/functions
- validation days/functions
- held-out functions
- held-out failure scenarios
- held-out cost and network assumptions

Useful comparisons:

1. learned policy
2. static routing or allocation baseline
3. cost-aware heuristic baseline
4. learned policy with fallback
5. learned policy without fallback

The last comparison demonstrates whether the safety layer reduces failed-transition cost and customer impact.

---

## 13. AWS deployment mapping for the hackathon

The AWS components are execution and observability infrastructure around the simulator:

| AWS component | Role |
|---|---|
| S3 | dataset artifacts, normalized traces, trained policy, run results, assumptions |
| Fargate | isolated simulation workers and replay jobs |
| CloudWatch | p99, error, CPU, memory, cost, fallback, and recovery metrics |
| Lambda | failure detector, fallback trigger, run coordinator, or policy safety action |

The deployment should label data clearly:

- `source=huawei_trace`
- `mode=simulation`
- `pricing=assumption`
- `controller=learned` or `controller=fallback`

This prevents the demo from implying that the system is receiving live AWS production telemetry when it is replaying and simulating known traces.

---

## 14. Minimal end-to-end record

A single simulated transition should be explainable from one record containing:

```python
{
    "trace_time_s": 86400.0,
    "day": 1,
    "minute": 1440,
    "region_id": "3",
    "zone_id": "cluster2",
    "func_id": "400---418---pool22-300-128",
    "requests": 120,
    "cold_starts": 8,
    "p99_ms": 245.0,
    "slo_ms": 200.0,
    "cpu_cores": 0.6,
    "memory_mb": 512.0,
    "action": "spill_25_percent_to_cluster3",
    "consensus_steps": 4,
    "observed_steps": 7,
    "failure_detected": True,
    "estimated_cost": 0.0,
    "failed_transition_cost": 0.0,
    "controller": "fallback_heuristic",
    "assumptions": {
        "response_size_ratio": 1.0,
        "inter_cluster_latency_ms": 2.0,
        "network_price_per_gb": 0.0,
    },
}
```

The exact monetary values will depend on the configured AWS pricing assumptions. The important contract is that latency, failure, action, steps, cost, controller choice, and assumptions are recorded together.

---

## 15. Dataset citation

The Huawei dataset is licensed under CC BY 4.0. The final hackathon presentation and repository documentation should cite the dataset and its paper. The simulator must distinguish:

- values measured in the Huawei dataset
- values derived from Huawei data
- values assumed for the AWS simulation
- values produced by the learned policy
- values produced by the fallback heuristic

That separation is part of the technical credibility of the demo.
