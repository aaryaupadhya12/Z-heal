import json
from pathlib import Path                    

import numpy as np
import pandas as pd

from ..fixed import *
from ..Environment.Healing_zone import *
from ..Environment.run import episode, summarise
from .Force_local import ForceLocal
from .Server_Weighted_Round_Robin import Server_Weighted_Round_Robin as RoundRobin
from .psuedo_envoy import Envoy

theta = np.load(r"C:\Users\Aarya-2\Documents\ADOG\MARLOW AI\CPHarn\Z-heal\runs\theta_deploy.npy")

# 2. one agent, not two. TabularSoftmaxPolicy has no act(state, feats),
#    so it can't be used by episode(). Use a small greedy wrapper:
class OurPolicy:
    name = "zoneheal"
    def __init__(self, table): self.table = table
    def reset(self): pass
    def act(self, state, feats): return int(np.argmax(self.table[state]))
    def observe(self, state, action, reward, info): pass

ours = OurPolicy(theta)

Path("results").mkdir(exist_ok=True)
data = load_Arrays(TABLE_PATH)

rows = []
for condition in ("normal", "brownout"):
    for seed in range(5):
        for agent in (ForceLocal(), RoundRobin(), Envoy(), ours):    
            env = ZoneEnv(data, split="test", seed=seed)
            if condition == "brownout":
                env.set_fault("brownout", zone="all_but_0",          
                              severity=1.0, start=0)
            df = episode(env, agent, seed=seed, minutes=360)
            r = summarise(df, agent.name, condition)                  
            r["seed"] = seed
            rows.append(r)

table = pd.DataFrame(rows)
table.to_csv("results/final_table.csv", index=False)
print(table.groupby(["window", "agent"])[
    ["p99_ms", "rupees_per_hour", "slo_miss_min", "local_pct"]].median().round(1).to_string())


# "Harness.AWS.Algorithms.test_policy"abs

data = load_Arrays(TABLE_PATH)
env = ZoneEnv(data, split="test", seed=0, episode_min=60)

# 1. find the most overloaded window in the test split
best_peak, START = 0.0, None
for s in env.starts[::10]:
    cap = data["servers"][s:s+60] * PER_POD_RATE
    peak = float((data["arrivals"][s:s+60] / np.maximum(cap, 1e-9)).max())
    if peak > best_peak:
        best_peak, START = peak, int(s)
print(f"window {START}, peak util {best_peak:.2f}, day {data['day'][START]}\n")

# 2. reward for each action, same minute, brownout on
print("action  spill  own_p99   rupees   reward")
for a in range(len(ACTIONS)):
    env.reset(seed=0, start_minute=START)
    env.set_fault("brownout", zone="all_but_0", severity=1.0, start=0)
    _, r, _, _, info = env.step(a)
    print(f"  {a}    {info['spill']:.2f}   {info['own_p99_ms']:7.0f}  {info['rupees']:6.3f}  {r:7.3f}")

# 3. where does the spill go, and is that zone any faster?
env.reset(seed=0, start_minute=START)
env.set_fault("brownout", zone="all_but_0", severity=1.0, start=0)
m = env.m
row = env.rows[0]                      # zone 0 is deciding first
L = loads(data, m, env.rows)
print("\nzone  util   p99(with brownout)")
for k in range(4):
    mean_ms, p99 = latency(data, m, k, L[k])
    if k != 0:                         # zones 1-3 are browned out
        p99 *= 5.0
    u = L[k] / capacity(data, m, k)
    print(f"  {k}   {u:.2f}   {p99:8.0f}")

env.reset(seed=0, start_minute=START)
env.set_fault("brownout", zone="all_but_0", severity=1.0, start=0)
env.step(3)                            # 50 % spill from zone 0
print("\nrouting row of zone 0 at 50% spill:", [round(x, 3) for x in env.rows[0]])
print("(if most of the spill goes to zones 1-3, it is going to browned-out zones)")

env = ZoneEnv(data, split="test", seed=0)
env.set_fault("brownout", zone="all_but_0", severity=1.0, start=0)
df = episode(env, ours, seed=0, minutes=360)
print(df["state"].map(lambda s: decode(s)[0]).value_counts().sort_index())   # busyness band
print(df["spill"].value_counts())