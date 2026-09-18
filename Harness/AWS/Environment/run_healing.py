import json
from pathlib import Path

import numpy as np
import pandas as pd

from zoneheal.core.trainer import AdaptiveBuckets, Trainer
from zoneheal.core.policy import TabularSoftmaxPolicy
from zoneheal.heal.controller import PIDBetaController, StaticBeta, natural_kl_target

# your environment
from .Healing_zone import ZoneEnv, load_Arrays, decode
from ..fixed import TABLE_PATH, N_STATES, ACTIONS

FAULT_AT   = 60    # iteration when the brownout starts
ITERS      = 120
BATCH      = 8            # episodes per iteration
EPISODE_MIN = 60          # minutes per episode
FAULT_ZONE = 1            # index 1 = cluster 2 (the busiest)
SEVERITY   = 1.0
TRAIN_START = 16183 


def bucket_of(state):
    u, l, s = decode(state)
    return f"u{u}_l{l}"


def calibrate(data, seed=0, iters=50):
    """How far does the policy move per iteration with no penalty?
    The KL target is set from that."""
    env = ZoneEnv(data, split="train", seed=seed, episode_min=EPISODE_MIN)
    env.fixed_start = TRAIN_START
    pol = TabularSoftmaxPolicy(env.n_states, env.n_actions, seed=seed + 1)
    tr = Trainer(env, pol, AdaptiveBuckets(bucket_of), StaticBeta(0.0),
                 gamma=0.0, batch=BATCH, heal=False)
    kls = []
    for _ in range(iters):
        rep = tr.step()
        kls += [r["kl"] for r in rep["buckets"].values()]
    return natural_kl_target(kls)

# Written by Claude Opus 5 Medium 
def summarise_healing(rows, events, fault_at=FAULT_AT, window=25):
    """Turn per-iteration rows into the numbers that answer:
    did it drop, where, did the harness notice, and did healing help?"""

    before = rows[(rows["iter"] >= fault_at - window) & (rows["iter"] < fault_at)]
    after  = rows[(rows["iter"] >= fault_at) & (rows["iter"] < fault_at + window)]

    # 1. which buckets were hit, and by how much
    b = before.groupby(["heal", "seed", "bucket"])["perf"].mean().rename("before")
    a = after.groupby(["heal", "seed", "bucket"])["perf"].mean().rename("after")
    hit = pd.concat([b, a], axis=1)
    hit["drop"] = hit["before"] - hit["after"]
    print("\n=== 1. performance drop per bucket (mean over seeds) ===")
    print(hit.groupby(["heal", "bucket"])["drop"].mean().round(3).to_string())

    # 2. recovery: iterations until perf is back to 95 % of the pre-fault level
    print("\n=== 2. recovery ===")
    rec = []
    for (heal, seed), g in rows.groupby(["heal", "seed"]):
        base = g[(g["iter"] >= fault_at - window) & (g["iter"] < fault_at)]["perf"].mean()
        per_iter = g[g["iter"] >= fault_at].groupby("iter")["perf"].mean()
        ok = per_iter[per_iter >= 0.95 * base]          # perf is negative: closer to 0 is better
        rec.append({"heal": heal, "seed": seed,
                    "recovery_iters": int(ok.index[0] - fault_at) if len(ok) else None,
                    "lost_reward": float((base - per_iter[:window]).clip(lower=0).sum())})
    rec = pd.DataFrame(rec)
    print(rec.to_string(index=False))
    print(rec.groupby("heal")[["recovery_iters", "lost_reward"]].median().round(2).to_string())

    # 3. what the harness did, and where
    print("\n=== 3. heal actions ===")
    if len(events):
        ev = events[events["iter"] >= fault_at]
        print(ev.groupby(["bucket", "event"]).size().rename("count").to_string())
        print("\nbefore the fault (false alarms):")
        print(events[events["iter"] < fault_at].groupby("event").size().to_string())
    else:
        print("none recorded")

    # 4. collateral: did untouched buckets get worse?
    print("\n=== 4. collateral ===")
    worst = hit.groupby("bucket")["drop"].mean().sort_values(ascending=False)
    affected = worst.index[:2].tolist()
    others = [x for x in worst.index if x not in affected]
    print("most affected:", affected)
    print("others, mean drop:", round(hit.loc[hit.index.get_level_values('bucket').isin(others), "drop"].mean(), 3))
    return hit, rec


def run(data, heal, seed, kl_target):
    env = ZoneEnv(data, split="train", seed=seed, episode_min=EPISODE_MIN)
    env.fixed_start = TRAIN_START
    pol = TabularSoftmaxPolicy(env.n_states, env.n_actions, seed=seed + 1)
    ctrl = PIDBetaController(kl_target0=kl_target, heal=heal)
    tr = Trainer(env, pol, AdaptiveBuckets(bucket_of), ctrl,
                 gamma=0.0, batch=BATCH, heal=heal)

    rows = []
    for it in range(ITERS):
        if it == FAULT_AT:
            env.set_fault("brownout", zone="all_but_0", severity=SEVERITY, start = 0, length = 30)
            print("fault set:", env.fault)

        rep = tr.step()

        for b, r in rep["buckets"].items():
            rows.append({"iter": it, "heal": heal, "seed": seed, "bucket": b,
                         "perf": r["perf"], "kl": r["kl"], "beta": r["beta"],
                         "cell": r["cell"], "n": r["n"]})

        if it % 25 == 0 or FAULT_AT - 5 <= it <= FAULT_AT + 25:
            perf = {b: round(r["perf"], 2) for b, r in sorted(rep["buckets"].items())}
            print(f"it {it:3d} heal={heal} mean_reward={rep['mean_return']:.2f} {perf}")
        
    log = pd.DataFrame(ctrl.log)
    print(f"\nheal={heal} seed={seed}  controller cells:")
    print(log["cell"].value_counts().to_string())

    events = pd.DataFrame(tr.log.events) if tr.log.events else pd.DataFrame()
    return pd.DataFrame(rows), events, pol.theta.copy()


def main():
    Path("runs").mkdir(exist_ok=True)
    data = load_Arrays(TABLE_PATH)

    kl_target = calibrate(data)
    print("kl target:", kl_target)

    all_rows, all_events = [], []
    for seed in range(3):
        for heal in (False, True):
            rows, events, theta = run(data, heal, seed, kl_target)
            all_rows.append(rows)
            if len(events):
                events["seed"] = seed
                events["heal"] = heal
                all_events.append(events)
            np.save(f"runs/theta_heal{int(heal)}_s{seed}.npy", theta)


    pd.concat(all_rows).to_parquet("runs/healing_rows.parquet")
    if all_events:
        pd.concat(all_events).to_parquet("runs/healing_events.parquet")

    with open("runs/healing_meta.json", "w") as f:
        json.dump({"kl_target": kl_target, "fault_at": FAULT_AT, "iters": ITERS,
                   "batch": BATCH, "episode_min": EPISODE_MIN,
                   "fault_zone": FAULT_ZONE, "severity": SEVERITY}, f, indent=2)
    print("saved runs/healing_rows.parquet")



if __name__ == "__main__":
    import sys
    if "--summary" in sys.argv:
        rows = pd.read_parquet("runs/healing_rows.parquet")
        try:
            events = pd.read_parquet("runs/healing_events.parquet")
        except FileNotFoundError:
            events = pd.DataFrame()
        summarise_healing(rows, events)
    else:
        main()