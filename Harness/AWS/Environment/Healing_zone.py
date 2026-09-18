import numpy as np
import pandas as pd

from ..fixed import *


# ----------------------------------------------------------------------
# data
# ----------------------------------------------------------------------

def load_Arrays(path):
    """Long table (one row per minute per cluster) -> arrays indexed [minute, zone]."""
    df = pd.read_parquet(path)
    df = df.sort_values(["minute", "cluster"])

    def per_zone(col):
        return df.pivot(index="minute", columns="cluster", values=col).to_numpy()

    one = df[df["cluster"] == 1].sort_values("minute")

    return {
        "arrivals":  per_zone("arrivals_ps"),
        "servers":   per_zone("servers"),
        "cold_n":    per_zone("cold_n"),
        "cold_ms":   per_zone("cold_ms"),
        "svc_p50":   one["svc_p50_ms"].to_numpy(),
        "svc_p99":   one["svc_p99_ms"].to_numpy(),
        "req_bytes": one["req_bytes"].to_numpy(),
        "day":       one["day"].to_numpy(),
        "split":     one["split"].to_numpy(),
        "zones":     [1, 2, 3, 4],
    }


# ----------------------------------------------------------------------
# physics of one minute
# ----------------------------------------------------------------------

def capacity(d, m, zi):
    """Requests/second zone zi can handle at minute m."""
    return d["servers"][m, zi] * PER_POD_RATE


def util(load_ps, cap_ps):
    """Busyness, capped at 0.99 so latency stays finite."""
    if cap_ps <= 0:
        return 0.99
    return min(load_ps / cap_ps, 0.99)


def routing_rows(df, m, zi, spill, last_p99=None):
    """Where zone zi's traffic goes. Destinations are weighted by free capacity,
    divided by their recent p99 so slow zones become unattractive."""
    weight = [0.0] * 4
    for k in range(4):
        if k == zi:
            continue
        free_k = max(0.0, capacity(df, m, k) - df["arrivals"][m, k])
        if last_p99 is not None:
            free_k = free_k / max(last_p99[k], 1.0)
        weight[k] = free_k

    total = sum(weight)

    row = [0.0] * 4
    row[zi] = 1.0 - spill
    for k in range(4):
        if k == zi:
            continue
        row[k] = spill * weight[k] / total if total > 0 else spill / 3.0
    return row


def loads(d, m, rows):
    """How much traffic lands on each zone, in requests/second."""
    load = [0.0] * 4
    for k in range(4):
        for j in range(4):
            load[k] += d["arrivals"][m, j] * rows[j][k]
    return load


def latency(d, m, k, load_ps):
    """(mean_ms, p99_ms) for zone k, given its load."""
    u = util(load_ps, capacity(d, m, k))
    mean_ms = d["svc_p50"][m] / (1 - u)
    p99_ms = max(d["svc_p99"][m], 4.6 * mean_ms)      # 4.6 = p99 of an exponential wait

    cold_frac = d["cold_n"][m, k] / max(d["arrivals"][m, k] * 60, 1)
    mean_ms += cold_frac * d["cold_ms"][m, k]
    p99_ms += cold_frac * d["cold_ms"][m, k]
    return mean_ms, p99_ms


def cost(d, m, rows):
    """Rupees per minute for traffic that crossed zones."""
    crossing_ps = 0.0
    for j in range(4):
        crossing_ps += d["arrivals"][m, j] * (1.0 - rows[j][j])
    bytes_each = 2 * d["req_bytes"][m]                # request + response (assumption)
    gb = crossing_ps * 60 * bytes_each / 1e9
    return gb * USD_PER_GB * FX


# ----------------------------------------------------------------------
# state encoding
# ----------------------------------------------------------------------

def band(x, edges):
    b = 0
    for e in edges:
        if x >= e:
            b += 1
    return b


def encode(util_value, latency_ratio, spare):
    u = band(util_value, UTIL_EDGES)
    l = band(latency_ratio, LATENCY_EDGES)
    s = band(spare, SPARE_EDGES)
    n_l = len(LATENCY_EDGES) + 1
    n_s = len(SPARE_EDGES) + 1
    return (u * n_l + l) * n_s + s


def decode(state):
    n_l = len(LATENCY_EDGES) + 1
    n_s = len(SPARE_EDGES) + 1
    s = state % n_s
    l = (state // n_s) % n_l
    u = state // (n_s * n_l)
    return u, l, s


def bucket_of(state):
    """Region name for the harness: busyness band x latency band.
    A brownout raises latency in particular regimes, so the affected states land
    in a few buckets and the rest stay untouched. That is what the harness localises."""
    u, l, s = decode(state)
    return f"u{u}_l{l}"



class ZoneEnv:
    def __init__(self, d, split="train", seed=0, episode_min=EPISODE_MIN):
        self.d = d
        self.episode_min = episode_min
        self.n_zones = 4
        self.n_states = N_STATES
        self.n_actions = len(ACTIONS)
        self.window_list = None
        self.window_i = 0

        self.fault = None
        self.fault_name = None
        # random windows add noise that looks like a fault; fix the window for healing runs
        self.fixed_start = None

        idx = np.where(d["split"] == split)[0]
        last = idx.max()
        self.starts = idx[idx + episode_min <= last]
        self.rng = np.random.default_rng(seed)

    # -- episode ------------------------------------------------------
    def reset(self, seed=None, start_minute=None):
        if seed is not None:
            self.rng = np.random.default_rng(seed)

        if start_minute is None and self.window_list:
            start_minute = self.window_list[self.window_i % len(self.window_list)]
            self.window_i += 1
        if start_minute is None:
            self.m0 = int(self.rng.choice(self.starts))
        else:
            self.m0 = int(start_minute)

        self.m = self.m0
        self.zi = 0
        self.rows = [[1.0 if k == z else 0.0 for k in range(4)] for z in range(4)]
        self.last_p99 = [self.d["svc_p99"][self.m]] * 4
        return self._state(), {}

    # -- what an agent sees -------------------------------------------
    def features(self):
        d, m, zi = self.d, self.m, self.zi

        load = loads(d, m, self.rows)[zi]
        u = util(load, capacity(d, m, zi))

        free = total = 0.0
        for k in range(4):
            if k == zi:
                continue
            cap_k = capacity(d, m, k)
            free += max(0.0, cap_k - d["arrivals"][m, k])
            total += cap_k
        spare = free / total if total > 0 else 0.0

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

    # -- faults (simulator only; the harness is never told) ------------
    def set_fault(self, kind=None, zone=None, severity=1.0, start=None, length=None):
        if kind is None:
            self.fault = None
            self.fault_name = None
            return
        if start is None:
            start = self.m
        self.fault = {"kind": kind, "zone": zone, "severity": severity,
                      "start": start, "length": length}
        self.fault_name = f"{kind}_z{zone}_s{severity}"

    def _fault_active(self, minute, zone):
        if self.fault is None:
            return False

        target = self.fault["zone"]
        if target == "all_but_0":
            if zone == 0:                 # cluster 1 stays healthy: somewhere to spill to
                return False
        elif target != zone:
            return False

        if minute < self.fault["start"]:
            return False
        if self.fault["length"] is None:
            return True
        return minute < self.fault["start"] + self.fault["length"]

    # -- one decision --------------------------------------------------
    def step(self, action):
        d, m, zi = self.d, self.m, self.zi

        # 1. apply the action
        if isinstance(action, (int, np.integer)):
            self.rows[zi] = routing_rows(d, m, zi, ACTIONS[int(action)], self.last_p99)
        else:
            row = ([float(action[d["zones"][k]]) for k in range(4)]
                   if isinstance(action, dict) else [float(x) for x in action])
            assert abs(sum(row) - 1.0) < 1e-6, f"row must sum to 1: {row}"
            self.rows[zi] = row

        # 2. loads and per-zone latency (the brownout bites here)
        loads_now = loads(d, m, self.rows)
        mean_ms, p99 = [], []
        for zone in range(4):
            mm, pp = latency(d, m, zone, loads_now[zone])
            if self._fault_active(m, zone) and self.fault["kind"] == "brownout":
                factor = 1.0 + 4.0 * self.fault["severity"]
                mm *= factor
                pp *= factor
            mean_ms.append(mm)
            p99.append(pp)

        # 3. what all users experience (reported)
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
                system_p99 += fraction * (p99[destination] + network_delay)

        # 4. score: the DECIDING zone's own users, so a regional fault
        #    produces a regional performance drop for the harness to find
        cost_value = cost(d, m, self.rows)
        own_p99 = sum(self.rows[zi][k] * (p99[k] + (RTT_MS if k != zi else 0.0))
                      for k in range(4))
        slo_missed = own_p99 > SLO_MS
        reward = -(own_p99 / SLO_MS + LAMBDA * cost_value + SLO_PENALTY * float(slo_missed))

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
            "p50_ms": system_mean,
            "p99_ms": system_p99,
            "own_p99_ms": own_p99,
            "own_slo_miss": bool(slo_missed),
            "slo_miss": bool(system_p99 > SLO_MS),
            "rupees": cost_value,
            "local_frac": local_frac,
            "reward": reward,
            "state": next_state,
            "util": [util(loads_now[k], capacity(d, m, k)) for k in range(4)],
            "arrivals": float(d["arrivals"][m].sum()),
            "loads": [float(x) for x in loads_now],
            "fault": self.fault_name,
        }
        return next_state, reward, done, False, info


def main():
    d = load_Arrays(TABLE_PATH)

    print(d["arrivals"].shape, d["svc_p50"].shape)
    print(d["arrivals"][0])
    print(d["servers"][0])
    print(d["cold_n"][0])

    # busyness at a quiet and a busy minute
    for label, m in (("minute 0", 0), ("busiest", int(np.argmax(d["arrivals"].sum(axis=1))))):
        print(f"\n{label} (day {d['day'][m]})")
        for zi in range(4):
            cap, arr = capacity(d, m, zi), d["arrivals"][m, zi]
            print(f"  zone {zi+1}: cap={cap:8.1f} arrivals={arr:7.1f} util={util(arr, cap):.3f}")

    # conservation and cost
    m = 18994
    rows = [routing_rows(d, m, j, 0.0) for j in range(4)]
    rows[1] = routing_rows(d, m, 1, 0.25)
    L = loads(d, m, rows)
    print("\nin:", round(d["arrivals"][m].sum()), " out:", round(sum(L)))
    crossing_ps = 1e9 / (60 * 2 * d["req_bytes"][m])
    one_gb_rows = [routing_rows(d, m, j, 0.0) for j in range(4)]
    print("all local cost:", round(cost(d, m, one_gb_rows), 4),
          " 1 GB should cost:", round(USD_PER_GB * FX, 2))

    # encoder
    print("\nencode(0.01,0.10,0.99) =", encode(0.01, 0.10, 0.99),
          " encode(0.99,1.50,0.01) =", encode(0.99, 1.50, 0.01),
          " N_STATES =", N_STATES)
    print("buckets:", sorted({bucket_of(s) for s in range(N_STATES)}))

    # environment
    env = ZoneEnv(d, split="train", seed=0)
    env.reset(seed=1); a = env.m0
    env.reset(seed=1); b = env.m0
    env.reset(seed=2); c = env.m0
    print("\nsame seed:", a == b, " different seed:", a != c)

    def run(seed, n=50, act=0):
        env.reset(seed=seed)
        return [env.step(act)[1] for _ in range(n)]
    print("deterministic:", run(3) == run(3))

    # the brownout must raise the deciding zone's own p99
    env.fixed_start = 16183
    env.reset()
    _, _, _, _, before = env.step(0)
    env.set_fault("brownout", zone="all_but_0", severity=1.0)
    _, _, _, _, after = env.step(0)
    print("own p99:", round(before["own_p99_ms"], 1), "->", round(after["own_p99_ms"], 1),
          "|", after["fault"])

    env.reset(seed=0, start_minute=43300)
    env.set_fault("brownout", zone="all_but_0", severity=1.0, start=0)
    for _ in range(4):
        env.step(0)                      # one full minute, so last_p99 reflects the brownout
    print("\nlast_p99:", [round(x) for x in env.last_p99])

    env.step(0)                          # zone 0 decides (fast zone)
    env.step(3)                          # zone 1 decides (slow zone), spills 50 %
    print("zone 1 row:", [round(x, 3) for x in env.rows[1]])
    print("(most of the 0.5 should go to zone 0, the fast one)")

    env = ZoneEnv(d, split="test", seed=0, episode_min=60)
    START = 43300

    print("action  spill  own_p99   rupees   reward")
    for a in range(4):
        env.reset(seed=0, start_minute=START)
        env.set_fault("brownout", zone="all_but_0", severity=1.0, start=0)
        for _ in range(4):
            env.step(0)          # let a minute pass so last_p99 reflects the brownout
        env.step(0)              # zone 0 decides (skip it: it is the fast zone)
        _, r, _, _, info = env.step(a)      # zone 1 decides: a slow zone
        print(f"  {a}    {info['spill']:.2f}   {info['own_p99_ms']:7.0f}  "
            f"{info['rupees']:6.3f}  {r:7.3f}")

if __name__ == "__main__":
    main()