import json
import random

N_STATES = 216
ACTIONS = [0.0, 0.10, 0.25, 0.50]

policy = {
    "version": 0,
    "actions": ACTIONS,
    "n_states": N_STATES,
    "table": [
        [random.random() for _ in ACTIONS]
        for _ in range(N_STATES)
    ]
}

with open("policy/policy.json", "w") as f:
    json.dump(policy, f, indent=2)

print(f"Created policy with {N_STATES} states and {len(ACTIONS)} actions")