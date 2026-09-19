import os
import time

import requests
from fastapi import FastAPI, Query
from fastapi.responses import Response

app = FastAPI()


def get_availability_zone():
    """
    Local testing:
        FAKE_AZ=ap-southeast-2a

    ECS:
        ECS_CONTAINER_METADATA_URI_V4 is provided automatically.
        We query /task and read AvailabilityZone.
    """
    fake_az = os.getenv("FAKE_AZ")

    if fake_az:
        return fake_az

    metadata_uri = os.getenv("ECS_CONTAINER_METADATA_URI_V4")

    if not metadata_uri:
        raise RuntimeError(
            "Cannot determine Availability Zone: "
            "FAKE_AZ is not set and ECS_CONTAINER_METADATA_URI_V4 is unavailable"
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


FAKE_AZ = get_availability_zone()

BASE_MS = float(os.getenv("BASE_MS", "20"))
EXTRA_MS = float(os.getenv("EXTRA_MS", "0"))

capacity = int(os.getenv("CAPACITY", "50"))
inflight = 0
slow = False


@app.get("/work")
def work(size: int = Query(1000, ge=0)):
    global inflight

    inflight += 1

    try:
        delay_ms = BASE_MS

        if slow:
            delay_ms += EXTRA_MS

        time.sleep(delay_ms / 1000.0)

        return Response(
            content=b"x" * size,
            media_type="application/octet-stream",
        )

    finally:
        inflight -= 1


@app.post("/slow")
def set_slow(on: bool = Query(...)):
    global slow

    slow = on

    return {
        "slow": slow,
        "az": FAKE_AZ,
    }


@app.get("/stats")
def stats():
    return {
        "az": FAKE_AZ,
        "inflight": inflight,
        "capacity": capacity,
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "az": FAKE_AZ,
    }