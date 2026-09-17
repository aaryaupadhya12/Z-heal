"""Bandit utilities kept import-compatible with the refactor layout."""

import numpy as np


class ThompsonGaussian:
    """Minimal Gaussian-thompson sampler used for import compatibility."""

    def __init__(self, n_arms, prior_mean=0.0, prior_var=1.0, seed=0):
        self.n_arms = int(n_arms)
        self.prior_mean = float(prior_mean)
        self.prior_var = float(prior_var)
        self.rng = np.random.default_rng(seed)
        self.eval_rng = np.random.default_rng(seed + 1)
        self.mean = np.full(self.n_arms, self.prior_mean, dtype=float)
        self.var = np.full(self.n_arms, self.prior_var, dtype=float)

    def sample(self, arm):
        return float(self.rng.normal(self.mean[arm], np.sqrt(self.var[arm])))

    def update(self, arm, reward):
        self.mean[arm] = reward
        self.var[arm] = max(self.var[arm] * 0.5, 1e-6)


class GreedyView:
    def __init__(self, means):
        self.means = np.asarray(means, dtype=float)

    def choose(self):
        return int(np.argmax(self.means))


class BanditRunner:
    def __init__(self, arms, seed=0):
        self.arms = np.asarray(arms, dtype=float)
        self.rng = np.random.default_rng(seed)

    def run(self, policy=None, steps=10):
        out = []
        for _ in range(steps):
            if policy is None:
                arm = int(self.rng.integers(len(self.arms)))
            else:
                arm = int(np.argmax(np.asarray(policy)))
            reward = float(self.arms[arm]) + self.rng.normal(0.0, 0.1)
            out.append((arm, reward))
        return out
