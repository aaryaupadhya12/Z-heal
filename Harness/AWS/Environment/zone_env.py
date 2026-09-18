import pandas as pd 
import numpy as np 
PER_POD_RATE = 42.26
import pandas as pd
import numpy as np

from ..fixed import *


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

def band(x,edges):
    b = 0
    for e in edges:
        if x >= e:
            b+= 1
    return b 

def encode(util_values,latency_ratio,spare):
    u = band(util_values,UTIL_EDGES)
    l = band(latency_ratio,LATENCY_EDGES)
    s = band(spare,SPARE_EDGES)
    n_l = len(LATENCY_EDGES) + 1
    n_s = len(SPARE_EDGES) + 1
    return (u * n_l + l) * n_s + s

def decode(state):
    # Inverse function
    n_l = len(LATENCY_EDGES) + 1
    n_s = len(SPARE_EDGES) + 1
    s = state % n_s
    l = (state // n_s) % n_l
    u = state // (n_s * n_l)
    return u, l, s

class ZoneEnv:
    def __init__(self, d, split="train", seed=0, episode_min=EPISODE_MIN):
        self.d = d
        self.episode_min = episode_min
        self.n_zones = 4

        # which minutes belong to this split
        idx = np.where(d["split"] == split)[0]
        last = idx.max()
        # a start minute needs a full episode after it, still inside the split
        self.starts = idx[idx + episode_min <= last]

        self.rng = np.random.default_rng(seed)
    

    def reset(self, seed=None, start_minute=None):
        if seed is not None:
            self.rng = np.random.default_rng(seed)

        if start_minute is None:
            self.m0 = int(self.rng.choice(self.starts))   # random window
        else:
            self.m0 = int(start_minute)                   # fixed window, for the table

        self.m = self.m0                 # current minute -> we understnad when the episodes end 
        self.zi = 0                      # which zone decides next (0..3)

        # everyone starts fully local
        self.rows = [[1.0 if k == z else 0.0 for k in range(4)] for z in range(4)]

        # no previous minute yet, so use this minute's service p99
        self.last_p99 = [self.d["svc_p99"][self.m]] * 4

        return self._state(), {}
    
    def features(self):
        d = self.d 
        m = self.m
        zi = self.zi


        load = loads(d,m,self.rows)[zi]
        u = util(load,capacity(d,m,zi))

        free = total = 0.0
        for k in range(4):
            if k == zi:
                continue 
            cap_k = capacity(d,m,k)
            free += max(0.0, cap_k -d["arrivals"][m,k])
            total += cap_k
        spare = free /total if total > 0 else 0.0

        return {
            "zone": d["zones"][zi],                
            "util": u,
            "latency_ratio": self.last_p99[zi] / SLO_MS,
            "spare": spare,
            "req_bytes": float(d["req_bytes"][m]),
            "servers": {d["zones"][k]: float(d["servers"][m, k]) for k in range(4)},
            "clients": {d["zones"][k]: 10.0 for k in range(4)},
        }
    
    def _state(self):
        f = self.features()
        return encode(f["util"], f["latency_ratio"], f["spare"])
    
    def step(self, action):
        d, m, zi = self.d, self.m, self.zi

        # 1. apply the action
        if isinstance(action, (int, np.integer)):
            self.rows[zi] = routing_rows(d, m, zi, ACTIONS[int(action)])
        else:
            row = ([float(action[d["zones"][k]]) for k in range(4)]
                   if isinstance(action, dict) else [float(x) for x in action])
            assert abs(sum(row) - 1.0) < 1e-6, f"row must sum to 1: {row}"
            self.rows[zi] = row

        # 2. loads and per-zone latency
        loads_now = loads(d, m, self.rows)
        mean_ms, p99 = [], []
        for zone in range(4):
            mm, pp = latency(d, m, zone, loads_now[zone])
            mean_ms.append(mm)
            p99.append(pp)

        # 3. what all users experience
        total_requests = float(d["arrivals"][m].sum())
        system_mean = system_p99 = 0.0
        for source in range(4):
            for destination in range(4):
                traffic = d["arrivals"][m, source] * self.rows[source][destination]
                if traffic == 0:
                    continue
                fraction = traffic / total_requests
                network_delay = RTT_MS if destination != source else 0.0
                system_mean += fraction * (mean_ms[destination] + network_delay)
                system_p99  += fraction * (p99[destination] + network_delay)

        # 4. score
        cost_value = cost(d, m, self.rows)
        slo_missed = system_p99 > SLO_MS
        reward = -(system_p99 / SLO_MS + LAMBDA * cost_value + SLO_PENALTY * float(slo_missed))

        local_frac = sum(d["arrivals"][m, j] * self.rows[j][j] for j in range(4)) / total_requests

        # 5. remember and advance
        self.last_p99 = p99
        self.zi += 1
        if self.zi == 4:
            self.zi = 0
            self.m += 1
        done = self.m >= self.m0 + self.episode_min

        next_state = self._state()
        info = {
            "minute": m, "day": int(d["day"][m]), "zone": d["zones"][zi],
            "spill": 1.0 - self.rows[zi][zi],
            "p50_ms": system_mean, "p99_ms": system_p99,
            "rupees": cost_value, "slo_miss": bool(slo_missed),
            "local_frac": local_frac, "reward": reward, "state": next_state,
            "util": [util(loads_now[k], capacity(d, m, k)) for k in range(4)],
        }
        return next_state, reward, done, False, info

                
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

    print("LATENCY_EDGES =", LATENCY_EDGES)
    print(band(0.10, LATENCY_EDGES), band(0.90, LATENCY_EDGES), band(1.50, LATENCY_EDGES))
# expect 0, 1, 2
    
    for s in range(N_STATES):
        u, l, sp = decode(s)
        assert 0 <= u <= len(UTIL_EDGES)
        assert 0 <= l <= len(LATENCY_EDGES)
        assert 0 <= sp <= len(SPARE_EDGES)

    # 2. real values land where you expect
    print(encode(0.01, 0.10, 0.99), decode(encode(0.01, 0.10, 0.99)))   # quiet, fast, lots of room
    print(encode(0.99, 1.50, 0.01), decode(encode(0.99, 1.50, 0.01)))   # full, slow, no room elsewhere
    print("encoder ok, N_STATES =", N_STATES)

    env = ZoneEnv(d, split="train", seed=0)
    print(len(env.starts), env.starts[:3], env.starts[-1])
    env.reset(seed=1); print("start:", env.m0)
    env.reset(seed=1); print("same seed:", env.m0)     # must match
    env.reset(seed=2); print("other seed:", env.m0)    # usually different
    print(env.rows[1])               

    env.reset(seed=2, start_minute=0)
    f = env.features()
    for k, v in f.items():
        if k in ("servers", "clients"):
            print(f"{k}: {({kk: round(vv, 1) for kk, vv in v.items()})}")
        else:
            print(f"{k}: {round(v, 4) if isinstance(v, float) else v}")
    print("state:", env._state(), "of", N_STATES)

    # Action tests 

    # A: it runs, and nothing spills with action 0
    env.reset(seed=1, start_minute=0)
    for i in range(10):
        s, r, done, _, info = env.step(0)
        if i < 4:
            print(f"zone {info['zone']}  p99={info['p99_ms']:.1f}  rupees={info['rupees']:.4f}  reward={r:.3f}")

    # B: same seed gives the same run
    def run(seed, n=50, a=0):
        env.reset(seed=seed)
        return [env.step(a)[1] for _ in range(n)]
    print("deterministic:", run(3) == run(3))

    # C: spilling costs money in a busy window
    env.reset(seed=1, start_minute=18900)
    _, _, _, _, info = env.step(3)          # action 3 = 50 % spill
    print("spill rupees:", round(info["rupees"], 3), " local_frac:", round(info["local_frac"], 3))


if __name__ == "__main__":
    main()
