import numpy as np

from zoneheal.envs.probes import GridLake
from zoneheal.core.policy import TabularSoftmaxPolicy
from zoneheal.core.trainer import Trainer, AdaptiveBuckets, grid_quadrant


if __name__ == "__main__":
    env = GridLake()
    policy = TabularSoftmaxPolicy(env.n_states, env.n_actions, seed=123)
    tr = Trainer(env, policy, AdaptiveBuckets(grid_quadrant), ctrl=None)
    for _ in range(2):
        tr.step()
    print("ppo_small smoke ok")
