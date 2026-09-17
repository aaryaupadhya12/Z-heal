import gymnasium as gym 
import json 

env = gym.make("FrozenLake-v1", is_slippery = False)

observation, info = env.reset()
data = [] 

for _ in range(10000):
    action = env.action_space.sample()
    observation, reward, terminated, truncated , info = env.step(action)

    data.append({
    "state": int(observation),
    "action": int(action),
    "reward": float(reward)
})

    if terminated or truncated:
        observation, info = env.reset()

env.close()

with open("frozenlake_data.json", "w") as f:
    json.dump(data, f, indent=4)