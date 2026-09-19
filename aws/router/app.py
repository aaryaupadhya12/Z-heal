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

# --------------------------------------------------
# Configuration
# --------------------------------------------------

REGION = os.getenv("AWS_REGION", "ap-southeast-2")

# Local testing only.
# In ECS, the router discovers its real AZ automatically.
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
ALARM_NAME = os.getenv(
    "ALARM_NAME",
    "zonerl-p99-guardrail",
)
SLO_MS = float(os.getenv("SLO_MS", "200"))

mode = START_MODE
policy = None

s3 = boto3.client("s3", region_name=REGION)
cloudwatch = boto3.client("cloudwatch", region_name=REGION)

# --------------------------------------------------
# Availability Zone
# --------------------------------------------------

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


# --------------------------------------------------
# Policy loading
# --------------------------------------------------

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
# Convert spill fraction into target AZ
# --------------------------------------------------

def choose_target_az(spill_fraction: float):
    if spill_fraction <= 0:
        return SOURCE_AZ

    if random.random() >= spill_fraction:
        return SOURCE_AZ

    other_azs = [
        az for az in AZ_TO_BACKEND
        if az != SOURCE_AZ
    ]

    if not other_azs:
        return SOURCE_AZ

    return random.choice(other_azs)


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


# --------------------------------------------------
# Work
# --------------------------------------------------

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
    }

    print(json.dumps(emf), flush=True)

@app.get("/work")
def work(size: int = Query(1000, ge=0)):
    # Temporary stub state.
    # encoder.py will replace this later.
    state = 0

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
        "Errors": 0 if response.ok else 1,
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

    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get(
            "content-type",
            "application/octet-stream",
        ),
    )