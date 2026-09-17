"""Interactive view of the tabular FrozenLake policy."""

import sys
from pathlib import Path

import gymnasium as gym
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import Advantage
from policy import TabularSoftmaxPolicy
from train import train


ACTION_NAMES = {0: "Left", 1: "Down", 2: "Right", 3: "Up"}
ACTION_SYMBOLS = {0: "←", 1: "↓", 2: "→", 3: "↑"}
MAP = ("S", "F", "F", "F", "F", "H", "F", "H", "F", "F", "F", "H", "H", "F", "F", "G")


st.set_page_config(page_title="FrozenLake Policy Explorer", page_icon="🧭", layout="wide")
st.markdown(
    """
    <style>
    .block-container { max-width: 1200px; padding-top: 2.5rem; }
    .hero { padding: 1.5rem 1.75rem; border-radius: 18px; background: linear-gradient(120deg, #123c4a, #197278); color: white; margin-bottom: 1.5rem; }
    .hero h1 { margin: 0; font-size: 2.35rem; }
    .hero p { margin: .5rem 0 0; color: #d7f4ef; }
    .tile { min-height: 108px; padding: 12px; border-radius: 12px; border: 1px solid #d7e3e5; background: #f7fbfb; text-align: center; margin-bottom: 10px; }
    .tile strong { display: block; font-size: 2rem; color: #123c4a; }
    .tile small { color: #557177; }
    .tile.start { background: #e3f3e8; border-color: #9bcdae; }
    .tile.hole { background: #f8e9e5; border-color: #e3b1a5; }
    .tile.goal { background: #fff2c9; border-color: #e6c96d; }
    </style>
    <div class="hero">
      <h1>FrozenLake Policy Explorer</h1>
      <p>Watch the tabular softmax policy turn experience into action preferences.</p>
    </div>
    """,
    unsafe_allow_html=True,
)


def run_training(seed: int, iterations: int, batch: int, learning_rate: float):
    env = gym.make("FrozenLake-v1", is_slippery=False)
    env.reset(seed=seed)
    policy = TabularSoftmaxPolicy(env.observation_space.n, env.action_space.n, seed=seed + 1)
    advantage = Advantage.WithBaseline(Advantage.naive)
    policy, history = train(
        env, policy, advantage, n_iters=iterations, batch=batch, lr=learning_rate, log_every=max(iterations, 1) + 1
    )
    env.close()
    return policy, history


with st.sidebar:
    st.header("Training controls")
    seed = st.number_input("Random seed", min_value=0, max_value=9999, value=0, step=1)
    iterations = st.slider("Training iterations", 25, 1000, 300, step=25)
    batch = st.slider("Episodes per iteration", 8, 128, 32, step=8)
    learning_rate = st.slider("Learning rate", 0.01, 0.5, 0.1, step=0.01)
    train_clicked = st.button("Train policy", type="primary", use_container_width=True)


if train_clicked or "policy" not in st.session_state:
    with st.spinner("Training the policy..."):
        st.session_state.policy, st.session_state.history = run_training(seed, iterations, batch, learning_rate)

policy = st.session_state.policy
history = st.session_state.history
probabilities = policy.snapshot()
greedy_actions = probabilities.argmax(axis=1)

metric_cols = st.columns(3)
metric_cols[0].metric("Latest success rate", f"{history[-1]['success']:.1%}")
metric_cols[1].metric("Latest mean episode length", f"{history[-1]['mean_len']:.1f}")
metric_cols[2].metric("Policy entropy", f"{history[-1]['entropy']:.3f}")

st.subheader("Policy map")
st.caption("Each arrow is the most likely action. The percentage is the probability of that action.")
grid = st.columns(4, gap="small")
for state in range(16):
    tile_type = {"S": "start", "H": "hole", "G": "goal"}.get(MAP[state], "")
    action = int(greedy_actions[state])
    label = MAP[state]
    grid[state % 4].markdown(
        f'<div class="tile {tile_type}"><small>State {state} · {label}</small>'
        f'<strong>{ACTION_SYMBOLS[action]}</strong><small>{ACTION_NAMES[action]} · {probabilities[state, action]:.1%}</small></div>',
        unsafe_allow_html=True,
    )

left, right = st.columns([1, 1.35])
with left:
    st.subheader("Inspect a state")
    selected_state = st.selectbox("State", range(16), format_func=lambda state: f"State {state} ({MAP[state]})")
    selected = probabilities[selected_state]
    st.write(f"**Greedy action:** {ACTION_SYMBOLS[int(selected.argmax())]} {ACTION_NAMES[int(selected.argmax())]}")
    st.bar_chart({ACTION_NAMES[action]: float(selected[action]) for action in range(4)}, y_label="Probability")

with right:
    st.subheader("What training changed")
    st.line_chart(
        {
            "Success rate": [row["success"] for row in history],
            "Mean episode length": [row["mean_len"] for row in history],
            "Entropy": [row["entropy"] for row in history],
        },
        x_label="Iteration",
        y_label="Metric value",
    )

with st.expander("Show the raw policy table"):
    st.dataframe(
        [
            {
                "state": state,
                "tile": MAP[state],
                "left": round(float(probabilities[state, 0]), 4),
                "down": round(float(probabilities[state, 1]), 4),
                "right": round(float(probabilities[state, 2]), 4),
                "up": round(float(probabilities[state, 3]), 4),
            }
            for state in range(16)
        ],
        hide_index=True,
        use_container_width=True,
    )