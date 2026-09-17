"""
Training loop. 
Deliberately knows nothing about FrozenLake. It takes an env, a policy, and
an advantage_fn. Swapping the environment and the advantage function is the
only change needed to run a different experiment -- that is the whole point
of the exercise.
"""



import numpy as np
 
import Advantage
from policy import TabularSoftmaxPolicy
from rollout import episode_return, rollout

def train(env, policy, advantage_fn, n_iters=3000, batch=32, lr=0.1, log_every=150):
    history = []
    for it in range(n_iters):
        grad = np.zeros_like(policy.theta)
        wins, lengths = 0, []

        for _ in range(batch):
            traj = rollout(env, policy)
            adv = advantage_fn(traj)

            for step, a in zip(traj, adv):
                grad[step.obs] += a * policy.grad_logprob(step.obs, step.action)

            wins += int(episode_return(traj) > 0)
            lengths.append(len(traj))

        policy.theta += lr * grad / batch
        policy.version += 1

        rec = {
            "iter": it,
            "success": wins / batch,
            "mean_len": float(np.mean(lengths)),
            "entropy": policy.mean_entropy(),
        }
        history.append(rec)
 
        if it % log_every == 0:
            print(f"iter {it:5d}  success {rec['success']:.3f}  "
                f"len {rec['mean_len']:5.2f}  entropy {rec['entropy']:.3f}")
 
    return policy, history

def run_frozenlake(seed =0 , n_iters = 3000 , advantage_fn =None):
    import gymnasium as gym 
    env = gym.make("FrozenLake-v1", is_slippery = False)
    env.reset(seed=seed)

    policy = TabularSoftmaxPolicy(env.observation_space.n, env.action_space.n, seed=seed + 1)

    if advantage_fn is None:
        advantage_fn = Advantage.WithBaseline(Advantage.naive)
    
    return train(env,policy,advantage_fn,n_iters=n_iters)


def bucket_of(traj):
    # out functions that helps to bucket the errors 
    R = episode_return(traj)
    if R > 0:
        return "goal_optimal" if len(traj) <= 6 else "goal_slow"
    return "fail_early" if len(traj) <= 3 else "fail_late"


if __name__ == "__main__":
    policy, history = run_frozenlake()
 
    print("\nfinal policy, greedy action per state (0=L 1=D 2=R 3=U):")
    print(policy.theta.argmax(axis=1).reshape(4, 4))
 
    print("\nlast 10 iterations:")
    for rec in history[-10:]:
        print(f"  iter {rec['iter']:5d}  success {rec['success']:.3f}  "
              f"len {rec['mean_len']:5.2f}  entropy {rec['entropy']:.3f}")




