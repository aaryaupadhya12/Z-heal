"""Buckets + the training loop that wires everything together.

Layers, deliberately separate:
    signature(state) / bucket fn   env-specific    [CHANGE PER ENV]
    AdaptiveBuckets                state -> bucket [agnostic]
    Trainer.step()                 one iteration   [agnostic]

One code path covers both learners:
    gamma > 0, multi-step episodes  -> REINFORCE (policy gradient)
    one-step episodes               -> contextual bandit (gradient bandit)
A contextual bandit is REINFORCE with a horizon of one.
"""

from collections import defaultdict

import numpy as np

from healing import HealLog, PolicyFlapMonitor, RollbackGuard
from rollout import rollout


# ----------------------------------------------------------------------
# Env-specific: rewrite these for a new environment
# ----------------------------------------------------------------------

def grid_quadrant(state, ncol=4):
    """GridLake 4x4 -> which quadrant."""
    row, col = divmod(int(state), ncol)
    return f"q{row // 2}{col // 2}"


def regime_health(state):
    """RegimeBandit context -> health bucket (ok / degraded / down)."""
    from envs import HEALTH
    return f"h_{HEALTH[int(state) // 2]}"


def single_bucket(state):
    """Everything in one bucket = one global trust region."""
    return "all"


# kept for backwards compatibility with the old scripts
def signature(state):
    row, col = divmod(int(state), 4)
    return (row // 2, col // 2)


def coarse_bucket(sig):
    return f"q{sig[0]}{sig[1]}"


# ----------------------------------------------------------------------
# Agnostic: bucket bookkeeping
# ----------------------------------------------------------------------

class AdaptiveBuckets:
    """state -> bucket id. Splitting is stubbed (fixed buckets for now).

    AdaptiveBuckets(fn) with a single function, or the old
    AdaptiveBuckets(bucket_fn, sig_fn) form.
    """

    def __init__(self, bucket_fn, sig_fn=None, min_samples=10**9):
        if sig_fn is None:
            self._fn = bucket_fn
        else:
            self._fn = lambda s: bucket_fn(sig_fn(s))
        self.min_samples = min_samples
        self._of = {}
        self._states = defaultdict(list)
        self.counts = defaultdict(int)

    def of(self, state):
        state = int(state)
        if state not in self._of:
            b = self._fn(state)
            self._of[state] = b
            self._states[b].append(state)
        return self._of[state]

    def assign(self, state):
        b = self.of(state)
        self.counts[b] += 1
        return b

    def register_all(self, n_states):
        for s in range(n_states):
            self.of(s)

    def states_in(self, bucket):
        return self._states[bucket]

    def buckets(self):
        return self._states.keys()

    def maybe_split(self, bucket, stats):
        """Hook for later (U-Tree style split when sub-regions differ)."""
        return False


# ----------------------------------------------------------------------
# Agnostic: KL for softmax rows
# ----------------------------------------------------------------------

def kl_rows(p, q):
    """Per-row KL(p || q)."""
    return (p * (np.log(p + 1e-12) - np.log(q + 1e-12))).sum(axis=1)


def kl_grad(p, q):
    """d KL(p||q) / d logits of p. Exactly zero when p == q, which is why
    the inner loop needs more than one step for beta to do anything."""
    logr = np.log(p + 1e-12) - np.log(q + 1e-12)
    kl = (p * logr).sum(axis=1, keepdims=True)
    return p * (logr - kl)


def returns_to_go(rewards, gamma):
    g = np.zeros(len(rewards))
    run = 0.0
    for t in range(len(rewards) - 1, -1, -1):
        run = rewards[t] + gamma * run
        g[t] = run
    return g


# ----------------------------------------------------------------------
# Agnostic: the trainer
# ----------------------------------------------------------------------

class Trainer:
    """One `step()` = collect a batch, take `inner_steps` penalised and
    clipped gradient steps per bucket, measure, let the controller and the
    healers react.

    Fixes relative to the first version:
      * importance ratio + clipping in the inner loop (the old loop applied
        the same stale gradient K times, i.e. a K-times larger step)
      * per-state value baseline and return-to-go, so a bucket is judged by
        what happened AFTER the agent was in it, not by the whole episode
      * KL weighted by how often each state was visited
      * controller gets kl_prev with perf (same update), kl_now for the PID
      * KL clipped at 0 (float noise gave -0.00000)
    """

    def __init__(self, env, policy, buckets, ctrl, *, gamma=0.95, batch=64,
                 lr=0.5, inner_steps=5, clip=0.2, v_rate=0.1,
                 max_steps=100, heal=True, reheat=0.5, reheat_cooldown=3,
                 log=None):
        self.env, self.policy, self.buckets, self.ctrl = env, policy, buckets, ctrl
        self.gamma, self.batch, self.lr = gamma, batch, lr
        self.inner_steps, self.clip, self.v_rate = inner_steps, clip, v_rate
        self.max_steps = max_steps

        n_s = policy.n_states
        buckets.register_all(n_s)
        self.V = np.zeros(n_s)
        self.v_seen = np.zeros(n_s, dtype=bool)
        self.prev_kl = defaultdict(float)
        self.it = 0

        self.log = log if log is not None else getattr(ctrl, "hlog", HealLog())
        self.heal = heal
        self.guard = RollbackGuard(self.log) if heal else None
        self.flap = PolicyFlapMonitor() if heal else None
        self.reheat = reheat
        self.reheat_cooldown = reheat_cooldown
        self._cool = defaultdict(int)
        self.heal_kl = 0.0          # policy movement caused by heal actions

    # ------------------------------------------------------------------
    def _reheat(self, b, St):
        """Heal action for a data shift: raise exploration in ONE bucket.

        A converged policy is nearly deterministic. After the world changes,
        the value baseline catches up, advantages go to ~0 and there is no
        gradient left to follow. Shrinking that bucket's logits toward their
        mean makes it try other actions again; every other bucket is left
        exactly as it was.
        """
        th = self.policy.theta
        before = self.policy.snapshot()[St]
        th[St] = (th[St] - th[St].mean(axis=1, keepdims=True)) * self.reheat
        after = self.policy.snapshot()[St]
        moved = float(kl_rows(after, before).mean())
        self.heal_kl += moved
        self.log.add(b, "reheat", kl=moved)

    # ------------------------------------------------------------------
    def _collect(self):
        S, A, LP, G, lengths, returns = [], [], [], [], [], []
        for _ in range(self.batch):
            traj = rollout(self.env, self.policy, self.max_steps)
            r = [st.reward for st in traj]
            g = returns_to_go(r, self.gamma)
            for st, gt in zip(traj, g):
                S.append(st.obs); A.append(st.action)
                LP.append(st.logprob); G.append(gt)
            lengths.append(len(traj))
            returns.append(sum(r))
        return (np.array(S, dtype=int), np.array(A, dtype=int),
                np.array(LP), np.array(G), lengths, returns)

    def _baseline(self, s, g):
        n_s = self.policy.n_states
        cnt = np.bincount(s, minlength=n_s)
        mean_g = np.bincount(s, weights=g, minlength=n_s) / np.maximum(cnt, 1)
        new = (cnt > 0) & ~self.v_seen
        self.V[new] = mean_g[new]
        self.v_seen |= cnt > 0
        adv = g - self.V[s]
        seen = cnt > 0
        self.V[seen] += self.v_rate * (mean_g[seen] - self.V[seen])
        return adv, cnt

    # ------------------------------------------------------------------
    def step(self):
        self.log.it = self.it
        pol = self.policy
        ref = pol.snapshot()

        s, a, lp_old, g, lengths, returns = self._collect()
        adv, visits = self._baseline(s, g)
        b_of = np.array([self.buckets.assign(x) for x in s], dtype=object)
        active_buckets = sorted(set(b_of))
        n_b = {b: int((b_of == b).sum()) for b in active_buckets}
        norm = np.array([n_b[b] for b in b_of], dtype=float)
        p_old = np.exp(lp_old)

        for _ in range(self.inner_steps):
            P = pol.snapshot()
            ratio = P[s, a] / p_old
            clipped = (((adv > 0) & (ratio > 1 + self.clip))
                       | ((adv < 0) & (ratio < 1 - self.clip)))
            coef = np.where(clipped, 0.0, ratio * adv) / norm
            grad = np.zeros_like(pol.theta)
            np.add.at(grad, (s, a), coef)
            np.add.at(grad, s, -coef[:, None] * P[s])
            for b in active_buckets:
                St = self.buckets.states_in(b)
                beta = self.ctrl.beta[b]
                pol.theta[St] += self.lr * (grad[St] - beta * kl_grad(P[St], ref[St]))
        pol.version += 1

        new = pol.snapshot()
        kl_state = kl_rows(new, ref)
        report = {}
        for b in active_buckets:
            St = self.buckets.states_in(b)
            w = visits[St]
            kl_now = float(max((kl_state[St] * w).sum() / max(w.sum(), 1), 0.0))
            mask = b_of == b
            gb = g[mask]
            perf = float(gb.mean())
            se = float(gb.std(ddof=1) / np.sqrt(len(gb))) if len(gb) > 1 else 1.0

            cell, decided = self.ctrl.update(b, kl_now, self.prev_kl[b], perf, se)
            self.prev_kl[b] = kl_now

            if decided and self.heal:
                d = self.ctrl.last_decision[b]
                acted = self.guard.observe(b, d["cell"], d["perf"], d["thr"],
                                           pol.theta, St)
                if self._cool[b] > 0:
                    self._cool[b] -= 1
                elif (not acted and d["cell"] == "data_shift"
                      and self.reheat < 1.0):
                    self._reheat(b, St)
                    acted = "reheat"
                    self._cool[b] = self.reheat_cooldown
                if acted and hasattr(self.ctrl, "reset_baseline"):
                    self.ctrl.reset_baseline(b)

            report[b] = {"kl": kl_now, "perf": perf, "se": se, "n": n_b[b],
                         "cell": cell, "decided": decided,
                         "beta": self.ctrl.beta[b],
                         "target": (self.ctrl.target[b]
                                    if hasattr(self.ctrl, "target") else None)}

        if self.flap is not None and hasattr(self.ctrl, "tighten"):
            for b in self.flap.check(pol.snapshot(), self.buckets):
                self.ctrl.tighten(b, "policy_flap_tightened")

        self.it += 1
        return {"buckets": report,
                "success": float(np.mean(np.array(returns) > 0)),
                "mean_return": float(np.mean(returns)),
                "mean_len": float(np.mean(lengths))}


def iteration(trainer):
    """Old name, kept so existing scripts still read naturally."""
    return trainer.step()