import pandas as pd 
import numpy as np 
PER_POD_RATE = 42.26
import pandas as pd
import numpy as np

from ..fixed import (
    PER_POD_RATE,
    SLO_MS,
    USD_PER_GB,
    FX,
    RESP_BYTES_RULE,
    RTT_MS,
    LAMBDA,
    SLO_PENALTY,
)


def load_Arrays(path):
    # Print and see how each works 
    df = pd.read_parquet(path)

    df = df.sort_values(["minute","cluster"])

    arrivals = df.pivot(
        index = "minute",
        columns = "cluster",
        values = "arrivals_ps"
    ).to_numpy()

    servers = df.pivot(
        index = "minute",
        columns = "cluster",
        values = "servers"
    ).to_numpy()

    cold_n = df.pivot(
        index = "minute",
        columns = "cluster",
        values = "cold_n"
    ).to_numpy()

    cold_ms = df.pivot(
        index="minute",
        columns="cluster",
        values="cold_ms"
    ).to_numpy()

    one = df[df["cluster"] == 1].sort_values("minute")

    svc_p50 = one["svc_p50_ms"].to_numpy()
    svc_p99 = one["svc_p99_ms"].to_numpy()

    req_bytes = one["req_bytes"].to_numpy()

    day = one["day"].to_numpy()

    split = one["split"].to_numpy()

    return {
        "arrivals": arrivals,
        "servers": servers,
        "cold_n": cold_n,
        "cold_ms": cold_ms,
        "svc_p50": svc_p50,
        "svc_p99": svc_p99,
        "req_bytes": req_bytes,
        "day": day,
        "split": split,
        "zones": [1, 2, 3, 4],
    }

# Next Function get the capacity and the bussyness 
def capacity(df,m,zi):
    return df["servers"][m, zi] * PER_POD_RATE
    # we returna  dictonary that answershow many servers are there at zone z at minute m , -> howmnay requests they are handling , if 600 requests arrive then utility is 1.2 -> 120% overladed
    # h 

def util(load_ps, cap_ps):
    if cap_ps == 0:
        return 0.99
    util = min(load_ps / cap_ps,0.99) # we capp it at 0.99 max utilization of any pod in a zone 
    return util
    # Why is it capped at 1 is because iif anyhting greater than 11 becoems negetive 

def routing_rows(df,m , zi , spill):
    free = [0.0] * 4 
    for k in range(4):
        if k == zi:
            continue
        free[k] = max(0.0, capacity(df,m,k) - df["arrivals"][m,k])
    total_free = sum(free)

    row = [0.0] * 4
    row[zi] = 1.0 - spill

    for k in range(4):
        if k == zi:
            continue
        if total_free > 0:
            row[k] = spill * free[k] / total_free
        else:
            row[k] = spill / 3
    return row

def loads(df,m,rows):
    load = [0.0] * 4 
    for k in range(4):
        for j in range(4):
            load[k] += df["arrivals"][m, j] * rows[j][k]
    return load 


def latency(df, m, k, load_ps):
    u = util(load_ps, capacity(df, m, k))
    mean_ms = df["svc_p50"][m] / (1 - u)
    p99_ms = max(df["svc_p99"][m], 4.6 * mean_ms)   # 4.6 = p99 of an exponential wait

    # a share of requests also pay a cold-start delay
    cold_frac = df["cold_n"][m, k] / max(df["arrivals"][m, k] * 60, 1)
    mean_ms += cold_frac * df["cold_ms"][m, k]
    p99_ms  += cold_frac * df["cold_ms"][m, k]

    return mean_ms, p99_ms

def cost(d, m, rows):
    crossing_ps = 0.0
    for j in range(4):
        crossing_ps += d["arrivals"][m, j] * (1.0 - rows[j][j])

    bytes_each = 2 * d["req_bytes"][m]              # request + response (assumption)
    gb = crossing_ps * 60 * bytes_each / 1e9        # per minute
    return gb * USD_PER_GB * FX



def main():
    d = load_Arrays(r"C:\Users\Aarya-2\Documents\ADOG\MARLOW AI\CPHarn\Z-heal\data\processed\huawei_region2.parquet")
    print(d["arrivals"].shape, d["svc_p50"].shape)     # (44640, 4) (44640,)
    print(d["arrivals"][0])                            # [474.4, 930.9, 824.0, 593.2]
    print(d["servers"][0])                             # [166.05, 113.45, 118.50, 113.00]
    print(d["cold_n"][0])                              # [46, 59, 37, 37]  <- not the server numbers
    print(d["svc_p50"][0], d["req_bytes"][0])          # 48.57  3806.99
    print(d["day"][:3], d["split"][0]) 

    import numpy as np
    

    # check 1: minute 0, no routing (each zone serves only its own traffic)
    for zi in range(4):
        cap = capacity(d, 0, zi)
        arr = d["arrivals"][0, zi]
        print(f"zone {zi+1}: capacity={cap:8.1f}  arrivals={arr:7.1f}  util={util(arr, cap):.3f}")

    # check 2: the busiest minute in the whole dataset
    m = int(np.argmax(d["arrivals"].sum(axis=1)))
    print(f"\nbusiest minute = {m} (day {d['day'][m]})")
    for zi in range(4):
        cap = capacity(d, m, zi)
        arr = d["arrivals"][m, zi]
        print(f"zone {zi+1}: capacity={cap:8.1f}  arrivals={arr:7.1f}  util={util(arr, cap):.3f}")

    m = 18994
    print(routing_rows(d, m, 1, 0.00))   # zone 2, no spill  -> [0, 1, 0, 0]
    print(routing_rows(d, m, 1, 0.25))   # zone 2, 25% spill
    print(sum(routing_rows(d, m, 1, 0.25)))   # 1.0

    m = 18994

    # everyone local
    rows = [routing_rows(d, m, j, 0.0) for j in range(4)]
    L = loads(d, m, rows)
    print("all local:", [round(x) for x in L])
    print("in:", round(d["arrivals"][m].sum()), " out:", round(sum(L)))

    # zones 2 and 3 spill 25%
    rows = [routing_rows(d, m, j, 0.0) for j in range(4)]
    rows[1] = routing_rows(d, m, 1, 0.25)
    rows[2] = routing_rows(d, m, 2, 0.25)
    L = loads(d, m, rows)
    print("\nwith spill:", [round(x) for x in L])
    print("in:", round(d["arrivals"][m].sum()), " out:", round(sum(L)))

    for zi in range(4):
        print(f"zone {zi+1}: util {util(L[zi], capacity(d, m, zi)):.3f}")

    m = 18994
    print("service time this minute:", round(d["svc_p50"][m], 1), "ms")

    for k in range(4):
        L_local = d["arrivals"][m, k]
        mean, p99 = latency(d, m, k, L_local)
        print(f"zone {k+1}: load={L_local:7.0f} mean={mean:8.1f} p99={p99:9.1f}")

    # quiet minute for comparison
    print()
    for k in range(4):
        mean, p99 = latency(d, 0, k, d["arrivals"][0, k])
        print(f"minute 0, zone {k+1}: mean={mean:6.1f} p99={p99:8.1f}")

    
    m = 18994

    rows = [routing_rows(d, m, j, 0.0) for j in range(4)]
    print("all local:", round(cost(d, m, rows), 4), "rupees/min")     # 0.0

    rows[1] = routing_rows(d, m, 1, 0.25)
    rows[2] = routing_rows(d, m, 2, 0.25)
    c = cost(d, m, rows)
    print("zones 2,3 spill 25%:", round(c, 2), "rupees/min =", round(c * 60, 1), "rupees/hour")

    # the 1 GB test: 1 GB must cost 0.02 x FX
    crossing_ps = 1e9 / (60 * 2 * d["req_bytes"][m])     # crossing rate that makes exactly 1 GB/min
    print("1 GB check:", round(crossing_ps * 60 * 2 * d["req_bytes"][m] / 1e9, 3), "GB ->",
        round(crossing_ps * 60 * 2 * d["req_bytes"][m] / 1e9 * 0.02 * 96.54, 2), "rupees  (expect 1.93)")

if __name__ == "__main__":
    main()
