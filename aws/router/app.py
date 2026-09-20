import json
import os
import random
import threading
import time

import boto3
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

app = FastAPI()


REGION = os.getenv("AWS_REGION", "ap-southeast-2")


FAKE_AZ = os.getenv("FAKE_AZ")

AZ_TO_BACKEND = json.loads(
    os.getenv(
        "AZ_TO_BACKEND",
        json.dumps({
            "ap-southeast-2a": "backend-a",
            "ap-southeast-2b": "backend-b",
            "ap-southeast-2c": "backend-c",
        }),
    )
)

BUCKET = os.getenv("BUCKET", "zonerl-819168518877")
POLICY_KEY = os.getenv("POLICY_KEY", "policy/policy.json")
RESULTS_KEY = os.getenv("RESULTS_KEY", "results/results.json")

START_MODE = os.getenv("START_MODE", "shadow")
ALARM_NAME = os.getenv("ALARM_NAME", "zonerl-p99-guardrail")
SLO_MS = float(os.getenv("SLO_MS", "200"))

mode = START_MODE
policy = None

s3 = boto3.client("s3", region_name=REGION)
cloudwatch = boto3.client("cloudwatch", region_name=REGION)


def get_availability_zone():
    """
    Local:
        FAKE_AZ is used.

    ECS:
        AZ is discovered from the ECS task metadata endpoint.
    """

    if FAKE_AZ:
        return FAKE_AZ

    metadata_uri = os.getenv("ECS_CONTAINER_METADATA_URI_V4")

    if not metadata_uri:
        raise RuntimeError(
            "Cannot determine Availability Zone: "
            "FAKE_AZ is not set and ECS_CONTAINER_METADATA_URI_V4 "
            "is unavailable"
        )

    response = requests.get(
        f"{metadata_uri}/task",
        timeout=2,
    )
    response.raise_for_status()

    metadata = response.json()

    az = metadata.get("AvailabilityZone")

    if not az:
        raise RuntimeError(
            "ECS task metadata did not contain AvailabilityZone"
        )

    return az


SOURCE_AZ = None


def load_policy():
    global policy

    if FAKE_AZ:
        # Local development:
        # read the policy from the mounted/local filesystem.
        with open(
            os.getenv("POLICY_PATH", "policy/policy.json"),
            "r",
        ) as f:
            candidate = json.load(f)
    else:
        # ECS:
        # load policy from S3.
        response = s3.get_object(
            Bucket=BUCKET,
            Key=POLICY_KEY,
        )

        candidate = json.loads(
            response["Body"].read().decode("utf-8")
        )

    n_states = candidate["n_states"]
    actions = candidate["actions"]
    table = candidate["table"]

    if len(table) != n_states:
        raise ValueError(
            f"Invalid policy: expected {n_states} states, "
            f"got {len(table)}"
        )

    for i, row in enumerate(table):
        if len(row) != len(actions):
            raise ValueError(
                f"Invalid policy: state {i} has "
                f"{len(row)} scores, expected {len(actions)}"
            )

    # The encoder below makes 45 states. If the file says anything
    # else, it is the wrong file: refuse it instead of serving
    # decisions from a table we do not understand.
    if n_states != 45:
        raise ValueError(
            f"Invalid policy: encoder makes 45 states, "
            f"file has {n_states}"
        )

    policy = candidate

    print(
        json.dumps({
            "event": "policy_loaded",
            "version": policy["version"],
            "n_states": policy["n_states"],
            "actions": policy["actions"],
        }),
        flush=True,
    )



# Numbers for the minute in progress.
now_latencies = {}          # az -> [12.3, 15.1, ...]
now_counts = {}             # az -> 41
minute_started = time.time()

# The finished minute. This is what the policy reads.
last_util = {}              # az -> 0.12
last_p99 = {}               # az -> 380.0

backend_capacity = {}       # az -> requests per second

current_state = 0


def record_request(az, latency_ms):
    """Call this after every request comes back."""

    if az not in now_latencies:
        now_latencies[az] = []
        now_counts[az] = 0

    now_latencies[az].append(latency_ms)
    now_counts[az] = now_counts[az] + 1


def p99_of(numbers):
    """The value that 99% of requests came in under."""

    if len(numbers) == 0:
        return 0.0

    ordered = sorted(numbers)
    position = int(len(ordered) * 0.99)

    if position >= len(ordered):
        position = len(ordered) - 1

    return ordered[position]


def which_band(value, edges):
    """Turn a number into a band number: 0, 1, 2 ..."""

    band = 0

    for edge in edges:
        if value >= edge:
            band = band + 1

    return band


def get_capacity(az):
    """Ask a backend how many requests per second it can take."""

    try:
        reply = requests.get(
            get_backend_url(az) + "/stats",
            timeout=1,
        )
        return float(reply.json()["capacity"])

    except Exception:
        # If we cannot ask, use whatever we knew before.
        return backend_capacity.get(az, 50.0)


def finish_the_minute():
    """Freeze the last 60 seconds and work out the new state."""

    global minute_started, current_state

    seconds = time.time() - minute_started

    if seconds < 1.0:
        seconds = 1.0

    for az in AZ_TO_BACKEND:
        latencies = now_latencies.get(az, [])
        count = now_counts.get(az, 0)

        capacity = get_capacity(az)
        backend_capacity[az] = capacity

        # util = requests arriving per second, out of how many
        # it can take. The environment caps this at 0.99.
        util = (count / seconds) / capacity

        if util > 0.99:
            util = 0.99

        last_util[az] = util
        last_p99[az] = p99_of(latencies)

    now_latencies.clear()
    now_counts.clear()
    minute_started = time.time()

    current_state = work_out_state()

    print(
        json.dumps({
            "event": "minute_closed",
            "state": current_state,
            "util": last_util,
            "p99": last_p99,
        }),
        flush=True,
    )


def work_out_state():
    """Turn the three measurements into one number from 0 to 44."""

    edges = policy["edges"]

    # 1. How busy am I?
    my_util = last_util.get(SOURCE_AZ, 0.0)
    busy_band = which_band(my_util, edges["util"])

    # 2. Was I slow last minute?
    my_p99 = last_p99.get(SOURCE_AZ, 0.0)
    slow_band = which_band(my_p99 / SLO_MS, edges["latency"])

    # 3. Do the other zones have room?
    total_free = 0.0
    how_many = 0

    for az in AZ_TO_BACKEND:
        if az != SOURCE_AZ:
            total_free = total_free + (1.0 - last_util.get(az, 0.0))
            how_many = how_many + 1

    if how_many == 0:
        spare = 0.0
    else:
        spare = total_free / how_many

    spare_band = which_band(spare, edges["spare"])

    # Rows are busyness. Each row holds 3 latency bands x 3 spare
    # bands = 9 cells. Checked against the table: state 2 gives
    # 0.1 spill, states 7 and 8 give 0.5 spill.
    return (busy_band * 9) + (slow_band * 3) + spare_band


def get_state():
    """Called on every request. Recalculates once a minute."""

    if time.time() - minute_started >= 60.0:
        finish_the_minute()

    return current_state


# --------------------------------------------------
# Policy decision
# --------------------------------------------------

def choose_policy_action(state: int):
    scores = policy["table"][state]

    best_index = max(
        range(len(scores)),
        key=lambda i: scores[i],
    )

    return policy["actions"][best_index]


# --------------------------------------------------
# Rule decision
# --------------------------------------------------

def choose_rule_action():
    """
    Temporary stub for envoy_zone_aware.sample_zone.

    The real teammate implementation will replace this.
    """

    return 0.0


# --------------------------------------------------
# Where to send it
# --------------------------------------------------

def choose_target_az(spill_fraction: float):
    """Pick where this request goes."""

    # Most requests stay at home.
    if spill_fraction <= 0:
        return SOURCE_AZ

    if random.random() >= spill_fraction:
        return SOURCE_AZ

    other_zones = []
    scores = []

    for az in AZ_TO_BACKEND:
        if az == SOURCE_AZ:
            continue

        free = 1.0 - last_util.get(az, 0.0)

        if free < 0.0:
            free = 0.0

        p99 = last_p99.get(az, 0.0)

        if p99 <= 0.0:
            # Not measured yet. Do not assume it is fast.
            p99 = SLO_MS

        # Empty AND fast is good. Empty but slow is not.
        # Dividing by p99 is what stops us sending traffic into
        # the broken zone, which is idle because it is broken.
        other_zones.append(az)
        scores.append(free / p99)

    if len(other_zones) == 0:
        return SOURCE_AZ

    total = 0.0

    for s in scores:
        total = total + s

    if total <= 0.0:
        return SOURCE_AZ

    # Pick one, with better zones more likely.
    pick = random.random() * total
    running = 0.0

    for i in range(len(other_zones)):
        running = running + scores[i]

        if pick <= running:
            return other_zones[i]

    return other_zones[-1]


# --------------------------------------------------
# Backend URL
# --------------------------------------------------

def get_backend_url(target_az: str):
    backend = AZ_TO_BACKEND[target_az]

    # Local mode can still use complete URLs.
    if backend.startswith("http://") or backend.startswith("https://"):
        return backend

    # ECS / Service Connect.
    return f"http://{backend}:8080"


# --------------------------------------------------
# Results
# --------------------------------------------------

def load_results():
    if FAKE_AZ:
        with open(
            os.getenv("RESULTS_PATH", "policy/results.json"),
            "r",
        ) as f:
            return json.load(f)

    response = s3.get_object(
        Bucket=BUCKET,
        Key=RESULTS_KEY,
    )

    return json.loads(
        response["Body"].read().decode("utf-8")
    )


# --------------------------------------------------
# CloudWatch kill switch
# --------------------------------------------------

def check_alarm():
    global mode

    try:
        response = cloudwatch.describe_alarms(
            AlarmNames=[ALARM_NAME],
        )

        alarms = response.get("MetricAlarms", [])

        if not alarms:
            return

        state = alarms[0].get("StateValue")

        if state == "ALARM" and mode != "rule":
            mode = "rule"

            print(
                json.dumps({
                    "event": "kill_switch",
                    "reason": "p99 alarm",
                }),
                flush=True,
            )

    except Exception as e:
        # Contract:
        # if the CloudWatch call fails, keep current mode.
        print(
            json.dumps({
                "event": "alarm_check_failed",
                "error": str(e),
            }),
            flush=True,
        )


def alarm_loop():
    while True:
        time.sleep(10)
        check_alarm()


# --------------------------------------------------
# Startup
# --------------------------------------------------

@app.on_event("startup")
def startup():
    global SOURCE_AZ

    SOURCE_AZ = get_availability_zone()

    load_policy()

    print(
        json.dumps({
            "event": "router_started",
            "az": SOURCE_AZ,
            "mode": mode,
        }),
        flush=True,
    )

    thread = threading.Thread(
        target=alarm_loop,
        daemon=True,
    )

    thread.start()


# --------------------------------------------------
# Health
# --------------------------------------------------

@app.get("/health")
def health():
    return {
        "ok": True,
        "az": SOURCE_AZ,
    }


# --------------------------------------------------
# Mode
# --------------------------------------------------

@app.get("/mode")
def get_mode():
    return {
        "mode": mode,
    }


@app.post("/mode")
def set_mode(m: str = Query(...)):
    global mode

    if m not in {"policy", "rule", "shadow"}:
        raise HTTPException(
            status_code=400,
            detail="mode must be policy, rule, or shadow",
        )

    mode = m

    return {
        "mode": mode,
    }


# --------------------------------------------------
# Policy information
# --------------------------------------------------

@app.get("/policy")
def get_policy():
    return {
        "version": policy["version"],
        "n_states": policy["n_states"],
    }


# --------------------------------------------------
# Reload policy
# --------------------------------------------------

@app.post("/reload")
def reload_policy():
    load_policy()

    return {
        "version": policy["version"],
        "n_states": policy["n_states"],
    }


# --------------------------------------------------
# State, for the dashboard and for debugging
# --------------------------------------------------

@app.get("/state")
def state_now():
    return {
        "state": current_state,
        "source_az": SOURCE_AZ,
        "util": last_util,
        "p99": last_p99,
        "mode": mode,
        "spill": choose_policy_action(current_state) if policy else None,
    }


# --------------------------------------------------
# Turn the brownout on and off
#
# The backends are not reachable from outside the VPC,
# so the router passes the request through.
# --------------------------------------------------

@app.post("/brownout")
def brownout(az: str = Query(...), on: bool = Query(...)):
    if az not in AZ_TO_BACKEND:
        raise HTTPException(
            status_code=400,
            detail=f"unknown az: {az}",
        )

    try:
        reply = requests.post(
            get_backend_url(az) + "/slow",
            params={"on": on},
            timeout=5,
        )
        return reply.json()

    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=str(e),
        )


# --------------------------------------------------
# Results
# --------------------------------------------------

@app.get("/results")
def get_results():
    try:
        return load_results()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=str(e),
        )




def emit_metrics(log):
    emf = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "ZoneRL",
                    "Dimensions": [
                        ["Mode", "SourceAZ"],
                        ["Mode"],
                    ],
                    "Metrics": [
                        {
                            "Name": "LatencyMs",
                            "Unit": "Milliseconds",
                        },
                        {
                            "Name": "CrossAZBytes",
                            "Unit": "Bytes",
                        },
                        {
                            "Name": "Requests",
                            "Unit": "Count",
                        },
                        {
                            "Name": "Errors",
                            "Unit": "Count",
                        },
                        {
                            "Name" : "GoodRequests",
                            "Unit": "Count"

                        },
                    ],
                }
            ],
        },
        "Mode": log["Mode"],
        "SourceAZ": log["SourceAZ"],
        "LatencyMs": log["LatencyMs"],
        "CrossAZBytes": log["CrossAZBytes"],
        "Requests": log["Requests"],

        "Errors": log["Errors"],

        "GoodRequests": log["GoodRequests"],
    }

    print(json.dumps(emf), flush=True)


@app.get("/work")
def work(size: int = Query(1000, ge=0)):
    state = get_state()

    policy_spill = choose_policy_action(state)
    rule_spill = choose_rule_action()

    if mode == "policy":
        spill = policy_spill
        shadow_spill = None

    elif mode == "rule":
        spill = rule_spill
        shadow_spill = None

    elif mode == "shadow":
        # Rule acts.
        # Policy action is only logged.
        spill = rule_spill
        shadow_spill = policy_spill

    else:
        raise HTTPException(
            status_code=500,
            detail=f"Unknown mode: {mode}",
        )

    target_az = choose_target_az(spill)

    backend = get_backend_url(target_az)

    start = time.perf_counter()

    try:
        response = requests.get(
            f"{backend}/work",
            params={"size": size},
            timeout=10,
        )

    except requests.RequestException as e:
        latency_ms = (
            time.perf_counter() - start
        ) * 1000

        log = {
            "Mode": mode,
            "SourceAZ": SOURCE_AZ,
            "TargetAZ": target_az,

            "State": state,
            "Spill": spill,
            "LatencyMs": round(latency_ms, 3),
            "CrossAZBytes": 0,
            "Requests": 1,
            
            "GoodRequests": 0,
            "Errors": 1,
            "PolicyVersion": policy["version"],
        }

        if shadow_spill is not None:
            log["ShadowSpill"] = shadow_spill

        # Normal application log
        print(
            json.dumps(log),
            flush=True,
        )

        # CloudWatch EMF metric log
        emit_metrics(log)

        raise HTTPException(
            status_code=502,
            detail=str(e),
        )

    latency_ms = (
        time.perf_counter() - start
    ) * 1000

    record_request(target_az, latency_ms)

    cross_az_bytes = 0

    if target_az != SOURCE_AZ:
        cross_az_bytes = (
            size + len(response.content)
        )

    log = {
        "Mode": mode,
        "SourceAZ": SOURCE_AZ,
        "TargetAZ": target_az,
        "State": state,
        "Spill": spill,
        "LatencyMs": round(latency_ms, 3),
        "CrossAZBytes": cross_az_bytes,
        "Requests": 1,
        "GoodRequests": 1 if latency_ms < SLO_MS else 0,
        "Errors": 0 if response.ok else 1,
        "PolicyVersion": policy["version"],
        # Debug: which band each signal landed in.
        "Util": round(last_util.get(SOURCE_AZ, 0.0), 4),
        "P99Ratio": round(last_p99.get(SOURCE_AZ, 0.0) / SLO_MS, 4),
    }

    if shadow_spill is not None:
        log["ShadowSpill"] = shadow_spill

    # Normal application log
    print(
        json.dumps(log),
        flush=True,
    )

    # CloudWatch EMF metric log
    emit_metrics(log)

    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get(
            "content-type",
            "application/octet-stream",
        ),
    )