from dataclasses import dataclass, field

import numpy as np 

@dataclass

class Step:
    obs: int
    action: int
    reward: int
    logprob:int 
    policy_version: int
    meta: dict = field(default_factory = dict)

def rollout(env,policy,max_steps = 100):
    traj =[]
    obs, _ = env.reset()

    for _ in range(max_steps):
        action, logprob = policy.action(obs)
        next_obs , reward , terminated, truncated , _info = env.step(action)

        traj.append(Step(
            obs = int(obs),
            action = int(action),
            reward = float(reward),
            logprob= logprob,
            policy_version=policy.version
        ))

        obs = next_obs

        if terminated or truncated:
            break
    return traj

def episode_return(traj):
    return sum(s.reward for s in traj)



if __name__ == "__main__":
    import gymnasium as gym
 
    from policy import TabularSoftmaxPolicy
 
    env = gym.make("FrozenLake-v1", is_slippery=False)
    env.reset(seed=0)
    pol = TabularSoftmaxPolicy(16, 4, seed=1)
 
    # Step 1 checkpoint: uniform random policy over many episodes.
    n = 10_000
    wins, lengths = 0, []
    for _ in range(n):
        traj = rollout(env, pol)
        wins += int(episode_return(traj) > 0)
        lengths.append(len(traj))
 
    print(f"success rate : {wins / n:.4f}   (expect ~0.01-0.02)")
    print(f"mean length  : {np.mean(lengths):.2f}   (expect well under 10)")
 
    # Step 3 checkpoint: read a successful trajectory and confirm the reward
    # lands only on the final step.
    for _ in range(20_000):
        traj = rollout(env, pol)
        if episode_return(traj) > 0:
            print("\na successful trajectory:")
            for t, s in enumerate(traj):
                print(f"  t={t}  obs={s.obs:2d}  a={s.action}  r={s.reward}")
            break

