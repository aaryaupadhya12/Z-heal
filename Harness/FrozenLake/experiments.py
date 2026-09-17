"""Does the harness heal? Fault-injection experiments.

    python experiments.py                  # both envs, 5 seeds
    python experiments.py --env bandit --seeds 10
    python experiments.py --osc            # controller oscillation test

Variants (same seeds, same fault):
    static       best fixed beta from a sweep, no healing   (honest baseline)
    global_heal  one trust region for the whole policy, with healing
    bucket_pid   per-bucket PID, no healing                  (ablation)
    self_heal    per-bucket PID + healing                    (the claim)

The claim holds if self_heal recovers at least as reliably and as fast as
the others while moving the policy less, and (bandit) without damaging the
regimes that were never broken.

Every run is written to runs/*.json for the UI.
"""

import argparse
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from bucketing import (AdaptiveBuckets, Trainer, grid_quadrant,
                       regime_health, single_bucket)
from envs import GridLake, RegimeBandit
from healing import HealLog
from pid_controller import PIDBetaController, StaticBeta, natural_kl_target
from policy import TabularSoftmaxPolicy

ENVS = {
    "grid": dict(make=lambda seed: GridLake(), bucket_fn=grid_quadrant,
                 trainer=dict(gamma=0.95, batch=32, lr=0.5, max_steps=100),
                 fault_at=200, iters=500),
    "bandit": dict(make=lambda seed: RegimeBandit(seed=seed),
                   bucket_fn=regime_health,
                   trainer=dict(gamma=0.0, batch=128, lr=0.5, max_steps=1),
                   fault_at=200, iters=500),
}
UNTOUCHED_CONTEXTS = [0, 1, 4, 5]      # bandit regimes the fault never changes


def ok_metric(env_name, env, policy):
    if env_name == "grid":
        r, length = env.greedy_eval(policy)
        return r >= 1.0, {"greedy_success": r, "greedy_len": length}
    correct, regret = env.greedy_eval(policy)
    m = env.means
    gap = m.max(axis=1, keepdims=True) - m          # regret of each action
    online = (policy.snapshot() * gap).sum(axis=1)  # sampled policy, per context
    return correct >= 1.0, {
        "greedy_correct": correct, "regret": regret,
        "online_regret": float(online @ env.p),
        "collateral_online": float(online[UNTOUCHED_CONTEXTS].mean())}


def build(env_name, variant, seed, kl_target, static_beta, ctrl_kw=None):
    spec = ENVS[env_name]
    env = spec["make"](seed)
    policy = TabularSoftmaxPolicy(env.n_states, env.n_actions, seed=seed + 1)
    log = HealLog()
    ctrl_kw = ctrl_kw or {}
    fn = single_bucket if variant == "global_heal" else spec["bucket_fn"]
    heal = variant in ("global_heal", "self_heal")
    if variant == "static":
        ctrl = StaticBeta(static_beta)
    else:
        ctrl = PIDBetaController(beta0=max(static_beta, 1e-2),
                                 kl_target0=kl_target, heal=heal, log=log,
                                 **ctrl_kw)
    tr = Trainer(env, policy, AdaptiveBuckets(fn), ctrl, heal=heal, log=log,
                 **spec["trainer"])
    return env, policy, tr


def run_one(job):
    env_name, variant, seed, kl_target, static_beta, iters, fault_at, out = job
    env, policy, tr = build(env_name, variant, seed, kl_target, static_beta)
    series, oks = [], []
    heal_kl_at_fault = 0.0
    for it in range(iters):
        if fault_at is not None and it == fault_at:
            env.set_fault(True)
            heal_kl_at_fault = tr.heal_kl
        rep = tr.step()
        ok, m = ok_metric(env_name, env, policy)
        oks.append(ok)
        series.append({"iter": it, "ok": bool(ok), **m,
                       "success": rep["success"],
                       "buckets": {b: {k: r[k] for k in
                                       ("kl", "beta", "target", "cell", "perf")}
                                   for b, r in rep["buckets"].items()}})

    res = {"env": env_name, "variant": variant, "seed": seed,
           "fault_at": fault_at, "iters": iters}
    if fault_at is not None:
        res["converged_before_fault"] = bool(oks[fault_at - 1])
        rec = None
        for t in range(fault_at, iters - 4):
            if all(oks[t:t + 5]):
                rec = t - fault_at
                break
        res["recovery_iters"] = rec
        if rec is not None:
            after = oks[fault_at + rec:]
            res["stable_after_recovery"] = float(np.mean(after))
        post = series[fault_at:]
        res["kl_spent"] = float(sum(sum(b["kl"] for b in s["buckets"].values())
                                    for s in post))
        res["heal_kl"] = float(tr.heal_kl - heal_kl_at_fault)
        if env_name == "bandit":
            res["regret_area"] = float(sum(s["online_regret"] for s in post))
            res["collateral_area"] = float(sum(s["collateral_online"]
                                               for s in post))
    res["heal_events"] = tr.log.summary()

    if out:
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, f"{env_name}_{variant}_s{seed}.json")
        with open(path, "w") as f:
            json.dump({"summary": res, "series": series,
                       "events": tr.log.events}, f)
    return res


# ----------------------------------------------------------------------
# calibration: KL scale and the best fixed beta
# ----------------------------------------------------------------------

def calibrate(env_name, seeds=(100, 101, 102), betas=(0.01, 0.1, 1.0, 10.0),
              workers=1):
    spec = ENVS[env_name]
    # natural KL: beta=0, no fault, learning phase only
    kls = []
    for seed in seeds:
        _, _, tr = build(env_name, "static", seed, 1e-3, 0.0)
        for _ in range(spec["fault_at"]):
            rep = tr.step()
            kls.extend(r["kl"] for r in rep["buckets"].values())
    kl_target = natural_kl_target(kls)

    # best fixed beta, judged on the same fault, on calibration seeds
    jobs = [(env_name, "static", s, kl_target, b, spec["iters"],
             spec["fault_at"], None) for b in betas for s in seeds]
    results = pmap(run_one, jobs, workers)
    cap = spec["iters"] - spec["fault_at"]
    score = {b: _score([r["recovery_iters"] for j, r in zip(jobs, results)
                        if j[4] == b], cap)
             for b in betas}
    best = min(score, key=score.get)
    return kl_target, best, score


def _score(recs, cap):
    """Mean recovery time, with never-recovered counted as the cap."""
    return float(np.mean([cap if x is None else x for x in recs]))


def pmap(fn, jobs, workers):
    if workers <= 1:
        return [fn(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, jobs))


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------

def summarise(env_name, results, variants):
    cap = ENVS[env_name]["iters"] - ENVS[env_name]["fault_at"]
    print(f"\n=== {env_name}: fault at iter {ENVS[env_name]['fault_at']} ===")
    hdr = (f"{'variant':<12} {'recovered':>9} {'rec med':>8} {'rec mean':>9} "
           f"{'stable':>7} {'KL spent':>9} {'heal KL':>8}")
    if env_name == "bandit":
        hdr += f" {'regret':>8} {'collat.':>8}"   # sampled policy, post-fault
    print(hdr)
    for v in variants:
        rs = [r for r in results if r["variant"] == v]
        recs = [r["recovery_iters"] for r in rs]
        got = [x for x in recs if x is not None]
        stab = [r["stable_after_recovery"] for r in rs
                if "stable_after_recovery" in r]
        line = (f"{v:<12} {len(got):>4}/{len(rs):<4} "
                f"{(np.median(got) if got else float('nan')):>8.0f} "
                f"{_score(recs, cap):>9.1f} "
                f"{(np.mean(stab) if stab else float('nan')):>7.2f} "
                f"{np.median([r['kl_spent'] for r in rs]):>9.4f} "
                f"{np.median([r['heal_kl'] for r in rs]):>8.4f}")
        if env_name == "bandit":
            line += (f" {np.median([r['regret_area'] for r in rs]):>8.2f}"
                     f" {np.median([r['collateral_area'] for r in rs]):>8.3f}")
        print(line)
    conv = [r["converged_before_fault"] for r in results]
    print(f"(converged before fault in {sum(conv)}/{len(conv)} runs; "
          f"'rec mean' counts never-recovered as {cap})")

    print("\nheal events, self_heal, summed over seeds:")
    agg = {}
    for r in results:
        if r["variant"] != "self_heal":
            continue
        for b, ev in r["heal_events"].items():
            for k, n in ev.items():
                agg.setdefault(b, {}).setdefault(k, 0)
                agg[b][k] += n
    for b in sorted(agg):
        print(f"  {b:<12} {agg[b]}")



def oscillation_test(seeds=(0, 1, 2), iters=200):
    """Deliberately over-aggressive PID gains. Does the harness notice and
    damp the ringing? Measured on the learning phase of the bandit."""
    aggressive = dict(kp=3.0, ki=0.5, kd=0.0, max_log_step=3.0)
    print("\n=== oscillation test (bandit, aggressive PID gains) ===")
    print(f"{'heal':<6} {'zero-cross rate':>16} {'std log beta':>13} "
          f"{'damp events':>12} {'final correct':>14}")
    kl_target = 5e-3
    for heal in (False, True):
        zcrs, stds, evs, finals = [], [], [], []
        for seed in seeds:
            env = RegimeBandit(seed=seed)
            pol = TabularSoftmaxPolicy(env.n_states, env.n_actions, seed=seed + 1)
            log = HealLog()
            ctrl = PIDBetaController(beta0=1.0, kl_target0=kl_target,
                                     heal=heal, log=log, **aggressive)
            tr = Trainer(env, pol, AdaptiveBuckets(regime_health), ctrl,
                         heal=heal, log=log, **ENVS["bandit"]["trainer"])
            for _ in range(iters):
                tr.step()
            for b in ctrl.target:
                rows = [x for x in ctrl.log if x["bucket"] == b and not x["idle"]]
                errs = np.sign([x["err"] for x in rows if abs(x["err"]) > 0.25])
                if len(errs) > 2:
                    zcrs.append(float((errs[1:] != errs[:-1]).mean()))
                if rows:
                    stds.append(float(np.std(np.log([x["beta"] for x in rows]))))
            evs.append(sum(1 for e in log.events
                           if e["event"] == "oscillation_damped"))
            finals.append(env.greedy_eval(pol)[0])
        print(f"{str(heal):<6} {np.mean(zcrs):>16.2f} {np.mean(stds):>13.2f} "
              f"{np.mean(evs):>12.1f} {np.mean(finals):>14.2f}")



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["grid", "bandit", "both"], default="both")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--out", default="runs")
    ap.add_argument("--osc", action="store_true")
    args = ap.parse_args()

    if args.osc:
        oscillation_test()
        return

    variants = ["static", "global_heal", "bucket_pid", "self_heal"]
    envs = ["grid", "bandit"] if args.env == "both" else [args.env]
    for env_name in envs:
        t0 = time.time()
        kl_target, best_beta, score = calibrate(env_name, workers=args.workers)
        print(f"\n[{env_name}] kl_target={kl_target:.2e}  best static beta={best_beta}"
              f"  (sweep mean recovery: "
              + ", ".join(f"{b}->{s:.0f}" for b, s in score.items()) + ")")
        spec = ENVS[env_name]
        jobs = [(env_name, v, s, kl_target, best_beta, spec["iters"],
                 spec["fault_at"], args.out)
                for v in variants for s in range(args.seeds)]
        results = pmap(run_one, jobs, args.workers)
        summarise(env_name, results, variants)
        print(f"({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
