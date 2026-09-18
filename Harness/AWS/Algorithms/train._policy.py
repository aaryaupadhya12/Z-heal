"""train_policy.py -- train the policy we actually ship.

Different from run_healing.py on purpose:
    healing run = one window, fault injected once  -> proves detection
    this run    = stratified windows, brownouts in half the iterations
                  -> the policy the router uses

Half the iterations run on QUIET windows with a brownout, because that is the
state the fault actually creates: low busyness, high latency, room elsewhere.
Without it the policy only ever learns to react to busyness.

Run:  python -m Harness.AWS.Algorithms.train_policy
"""

import json
from pathlib import Path

import numpy as np

from zoneheal.core.policy import TabularSoftmaxPolicy
from zoneheal.core.trainer import AdaptiveBuckets, Trainer
from zoneheal.heal.controller import PIDBetaController, StaticBeta, natural_kl_target

from ..fixed import (ACTIONS, LAMBDA, LATENCY_EDGES, N_STATES, PER_POD_RATE,
                     SLO_MS, SPARE_EDGES, TABLE_PATH, UTIL_EDGES)
from ..Environment.Healing_zone import ZoneEnv, bucket_of, load_Arrays

ITERS = 400
BATCH = 8
EPISODE_MIN = 60
PER_STRATUM = 8


def pick_windows(data, env, per_stratum=PER_STRATUM):
    """Windows grouped by how overloaded they get, so the policy sees all regimes."""
    peaks = []
    for start in env.starts[::60]:
        end = start + EPISODE_MIN
        cap = data["servers"][start:end] * PER_POD_RATE
        peak = float((data["arrivals"][start:end] / np.maximum(cap, 1e-9)).max())
        peaks.append((peak, int(start)))

    quiet = [m for p, m in peaks if p < 0.5][:per_stratum]
    busy = [m for p, m in peaks if 0.5 <= p < 1.0][:per_stratum]
    over = [m for _, m in sorted([(p, m) for p, m in peaks if p >= 1.0],
                                 reverse=True)[:per_stratum]]

    print(f"windows: {len(quiet)} quiet, {len(busy)} busy, {len(over)} overloaded")
    return quiet, busy, over


def main():
    Path("runs").mkdir(exist_ok=True)
    Path("policy").mkdir(exist_ok=True)

    data = load_Arrays(TABLE_PATH)
    env = ZoneEnv(data, split="train", seed=0, episode_min=EPISODE_MIN)

    quiet, busy, over = pick_windows(data, env)
    all_windows = quiet + busy + over
    env.window_list = all_windows
    env.set_fault(None)

    # ---- calibrate the KL target on the same windows, no penalty ----
    cal_env = ZoneEnv(data, split="train", seed=0, episode_min=EPISODE_MIN)
    cal_env.window_list = all_windows
    cal = Trainer(cal_env, TabularSoftmaxPolicy(env.n_states, env.n_actions, seed=1),
                  AdaptiveBuckets(bucket_of), StaticBeta(0.0),
                  gamma=0.0, batch=BATCH, heal=False)
    kls = []
    for _ in range(50):
        kls += [r["kl"] for r in cal.step()["buckets"].values()]
    kl_target = natural_kl_target(kls)
    print("kl target:", round(kl_target, 5))

    # ---- train ----
    pol = TabularSoftmaxPolicy(env.n_states, env.n_actions, seed=1)
    ctrl = PIDBetaController(kl_target0=kl_target, heal=True)
    tr = Trainer(env, pol, AdaptiveBuckets(bucket_of), ctrl,
                 gamma=0.0, batch=BATCH, heal=True)

    for it in range(ITERS):
        if it % 2 == 0:
            env.window_list = quiet          # quiet traffic ...
            env.set_fault("brownout", zone="all_but_0", severity=1.0, start=0)  # ... but slow
        else:
            env.window_list = all_windows    # normal operation, every regime
            env.set_fault(None)

        rep = tr.step()

        if it % 50 == 0 or it == ITERS - 1:
            print(f"it {it:3d}  mean_reward={rep['mean_return']:.2f}  "
                  f"buckets={len(rep['buckets'])}")

    np.save("runs/theta_deploy.npy", pol.theta)

    # ---- what did it learn? ----
    best = pol.theta.argmax(axis=1)
    touched = int((np.abs(pol.theta).sum(axis=1) > 1e-6).sum())
    print(f"\nstates touched: {touched} of {N_STATES}")
    print("action per state (rows = busyness bands, columns = latency x spare):")
    print(best.reshape(-1, 9))
    print("action counts:", {a: int((best == a).sum()) for a in range(len(ACTIONS))})
    print("(want non-zero actions in the LAST three columns of the top rows:"
          " not busy, but slow)")

    # ---- export ----
    windows = {"quiet": quiet, "busy": busy, "overloaded": over}
    json.dump({
        "version": 1,
        "actions": ACTIONS,
        "n_states": N_STATES,
        "table": pol.theta.tolist(),
        "edges": {"util": UTIL_EDGES, "latency": LATENCY_EDGES, "spare": SPARE_EDGES},
        "slo_ms": SLO_MS,
        "trained_on": {
            "split": "train",
            "windows": windows,
            "iters": ITERS,
            "batch": BATCH,
            "episode_min": EPISODE_MIN,
            "fault": "brownout all_but_0 severity 1.0, on quiet windows, every other iteration",
            "lambda": LAMBDA,
            "kl_target": kl_target,
        },
    }, open("policy/policy.json", "w"), indent=2)
    json.dump(windows, open("runs/train_windows.json", "w"))
    print("\nsaved policy/policy.json and runs/theta_deploy.npy")


if __name__ == "__main__":
    main()