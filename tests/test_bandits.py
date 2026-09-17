import numpy as np

from zoneheal.agents.bandits import BanditRunner, GreedyView, ThompsonGaussian


def test_bandit_smoke():
    runner = BanditRunner(np.array([0.1, 0.4, 0.8]), seed=0)
    acts = runner.run(policy=[0.1, 0.3, 0.6], steps=4)
    assert len(acts) == 4
    assert isinstance(GreedyView([0.2, 0.8]).choose(), int)
    th = ThompsonGaussian(3, seed=7)
    assert th.sample(0) is not None


if __name__ == "__main__":
    test_bandit_smoke()
    print("bandit smoke ok")
