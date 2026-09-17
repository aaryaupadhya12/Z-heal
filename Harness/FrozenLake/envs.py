"""Small, dependency-free environments with the gymnasium reset/step API.

GridLake     -- deterministic FrozenLake (same action/reward semantics as
                FrozenLake-v1 with is_slippery=False) that accepts any map, so
                we can break a region of the world mid-training.
RegimeBandit -- a contextual bandit shaped like the AZ routing decision:
                context = (load, health) regime, action = spill fraction.
                One step per episode. A "brownout" fault changes the best
                action in the degraded regimes only.

Both expose set_fault(bool) so experiments can inject a fault at iteration T.
"""

import numpy as np

# ----------------------------------------------------------------------
# GridLake
# ----------------------------------------------------------------------

DEFAULT_MAP = ["SFFF", "FHFH", "FFFH", "HFFG"]
# Same map with state 9 turned into a hole: the learned route
# 0->4->8->9->13->14->15 now dies at 9. The only way through is
# 0->1->2->6->10->14->15, so the agent must change some regions and
# should leave the rest alone.
BROKEN_MAP = ["SFFF", "FHFH", "FHFH", "HFFG"]

LEFT, DOWN, RIGHT, UP = 0, 1, 2, 3


class GridLake:
    def __init__(self, desc=DEFAULT_MAP, fault_desc=BROKEN_MAP):
        self._maps = {False: [list(r) for r in desc],
                      True: [list(r) for r in fault_desc]}
        self.nrow = len(desc)
        self.ncol = len(desc[0])
        self.n_states = self.nrow * self.ncol
        self.n_actions = 4
        self.fault = False
        self.s = 0

    @property
    def desc(self):
        return self._maps[self.fault]

    def set_fault(self, on):
        self.fault = bool(on)

    def reset(self, seed=None):
        self.s = 0
        return self.s, {}

    def step(self, a):
        r, c = divmod(self.s, self.ncol)
        if a == LEFT:
            c = max(c - 1, 0)
        elif a == DOWN:
            r = min(r + 1, self.nrow - 1)
        elif a == RIGHT:
            c = min(c + 1, self.ncol - 1)
        elif a == UP:
            r = max(r - 1, 0)
        self.s = r * self.ncol + c
        cell = self.desc[r][c]
        terminated = cell in "HG"
        reward = 1.0 if cell == "G" else 0.0
        return self.s, reward, terminated, False, {}

    def greedy_eval(self, policy, max_steps=100):
        """Deterministic env + greedy policy -> one episode is exact."""
        s, _ = self.reset()
        for t in range(max_steps):
            s, r, term, trunc, _ = self.step(int(policy.theta[s].argmax()))
            if term or trunc:
                return r, t + 1
        return 0.0, max_steps


# ----------------------------------------------------------------------
# RegimeBandit
# ----------------------------------------------------------------------

LOADS = ["low", "high"]
HEALTH = ["ok", "degraded", "down"]
SPILL = [0.0, 0.10, 0.25, 0.50]

# Mean reward for each (context, action). Higher is better. Think of it as
# 1 - normalised(latency_penalty + lambda * cross_AZ_rupees).
_MEANS = np.array([
    # spill:  0%    10%   25%   50%
    [0.90, 0.85, 0.75, 0.60],   # low  / ok        -> stay local
    [0.80, 0.84, 0.74, 0.60],   # high / ok        -> spill a little
    [0.50, 0.65, 0.80, 0.70],   # low  / degraded  -> spill 25%
    [0.40, 0.60, 0.78, 0.72],   # high / degraded  -> spill 25%
    [0.10, 0.40, 0.60, 0.85],   # low  / down      -> spill 50%
    [0.05, 0.35, 0.60, 0.85],   # high / down      -> spill 50%
])
# Brownout gets worse: in the degraded regimes the best action moves to 50%.
# Every other regime is untouched, so a good harness should loosen only the
# "degraded" bucket and keep the others protected.
_FAULT_MEANS = _MEANS.copy()
_FAULT_MEANS[2] = [0.30, 0.45, 0.60, 0.80]
_FAULT_MEANS[3] = [0.20, 0.40, 0.62, 0.82]


def context_id(load, health):
    return HEALTH.index(health) * 2 + LOADS.index(load)


def context_name(c):
    h, l = divmod(int(c), 2)
    return f"{LOADS[l]}/{HEALTH[h]}"


class RegimeBandit:
    def __init__(self, noise=0.15, context_probs=None, seed=0):
        self.n_states = _MEANS.shape[0]
        self.n_actions = _MEANS.shape[1]
        self.noise = noise
        self.p = (np.full(self.n_states, 1.0 / self.n_states)
                  if context_probs is None else np.asarray(context_probs))
        self.rng = np.random.default_rng(seed)
        self.fault = False
        self.ctx = 0

    @property
    def means(self):
        return _FAULT_MEANS if self.fault else _MEANS

    def set_fault(self, on):
        self.fault = bool(on)

    def reset(self, seed=None):
        if seed is not None:
            self.rng = np.random.default_rng(seed)
        self.ctx = int(self.rng.choice(self.n_states, p=self.p))
        return self.ctx, {}

    def step(self, a):
        r = self.means[self.ctx, a] + self.noise * self.rng.standard_normal()
        return self.ctx, float(r), True, False, {}

    def greedy_eval(self, policy):
        """Fraction of contexts where the greedy action is truly best,
        and expected regret of the greedy policy."""
        greedy = policy.theta.argmax(axis=1)
        best = self.means.argmax(axis=1)
        correct = float((greedy == best).mean())
        regret = float((self.means.max(axis=1)
                        - self.means[np.arange(self.n_states), greedy]) @ self.p)
        return correct, regret
