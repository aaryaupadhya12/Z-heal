# RL harness architecture: how it is wired, why each metric exists, and how to extend it

This repository is not a generic “RL framework” in the sense of a plug-in ecosystem with arbitrary algorithms. It is a specific research harness built around a trust-region policy update with per-bucket control, self-healing, and fault-injection evaluation.

The important mental model is:

- the environment produces trajectories
- the policy produces action probabilities and log-probs
- the trainer computes rewards-to-go and advantages
- states are assigned to buckets
- KL is measured per bucket against a policy reference
- a controller decides how much a bucket is allowed to move
- a healer reacts when the controller oscillates, flaps, or regresses
- experiments judge success via recovery metrics, not just raw reward

That is the shape of the system. If you want to think of it as a GRPO-style or PPO-like update for continuous control, the same structure still applies, but the concrete API must match the repo’s expectations: discrete or compact state IDs, discrete action sampling, per-state softmax logits, and bucketed KL control.

---

## 1) What this repo is actually doing

The core training logic is not “a random policy gradient implementation.” It is a policy gradient loop with trust regions and per-bucket adaptation.

The high-level loop is:

1. collect trajectories from the environment
2. compute return-to-go or advantage per step
3. compute policy log-probability for each taken action
4. accumulate gradient by state and action
5. update policy parameters with a clipped / trust-region-aware step
6. measure KL between the current policy and the reference policy
7. per bucket, decide whether to loosen or tighten the trust region
8. if the controller behaves badly, heal the bucket

The repo currently uses a tabular softmax policy and grid-like environments, but the same architecture can be generalized to continuous control if you preserve the contract at each boundary.

The current flat implementation in the old folder is kept as legacy code; the actively maintained logic is split into the training classes and helpers that are hooked together by the trainer, controller, and experiment registry.

---

## 2) The execution flow, exact contract by contract

### 2.1 Environment contract

The environment must match the Gym-style reset/step interface used by rollout and trainer code.

Required interface:

```python
class MyEnv:
    def __init__(self):
        self.n_states = ...
        self.n_actions = ...

    def reset(self, seed=None):
        # -> (state, info)
        return state, {}

    def step(self, action):
        # -> (next_state, reward, terminated, truncated, info)
        return next_state, reward, terminated, truncated, {}
```

This repo assumes:

- `reset()` yields a state identifier for the current episode
- `step(action)` returns the next state, scalar reward, and terminal flags
- each environment can inject a fault or regime change via a method like `set_fault(True)`

The concrete environments in this repo are:

- `GridLake` in `Harness/FrozenLake/envs.py`
- `RegimeBandit` in the same file

The reasons they matter:

- `GridLake` is a deterministic routing environment; the policy has to change route choice when a hole appears in the map
- `RegimeBandit` is a contextual bandit where the best action changes under a fault; this is used to test whether the controller reacts only in the damaged regime and leaves healthy regimes alone

The repo’s experiments use `set_fault(True)` at a chosen iteration and evaluate whether the policy recovers without destabilizing everything else.

### 2.2 Trajectory contract

The trajectory is a list of `Step` objects. This is the single most important data structure in the entire repo.

Defined in the rollout logic:

```python
@dataclass
class Step:
    obs: int
    action: int
    reward: float
    logprob: float
    policy_version: int
    meta: dict = field(default_factory=dict)
```

Every later function expects this shape. If you add a new policy or environment, do not break these fields.

Why each field exists:

- `obs`: which state the policy was in when it acted
- `action`: the action actually sampled
- `reward`: immediate reward from this transition
- `logprob`: log probability of the chosen action under the policy at that state
- `policy_version`: tells you which policy snapshot produced this transition, so you can detect stale data or compare policy updates
- `meta`: extra environment or debug information, such as bucket labels or diagnostic info

The rollout function itself must produce this structure exactly:

```python
def rollout(env, policy, max_steps=100):
    traj = []
    obs, _ = env.reset()
    for _ in range(max_steps):
        action, logprob = policy.action(obs)
        next_obs, reward, terminated, truncated, _ = env.step(action)
        traj.append(Step(
            obs=int(obs),
            action=int(action),
            reward=float(reward),
            logprob=logprob,
            policy_version=policy.version,
        ))
        obs = next_obs
        if terminated or truncated:
            break
    return traj
```

If a new policy uses a different sampling API, you must adapt it to return `(action, logprob)` in this exact style.

### 2.3 Return and advantage contract

The repo uses scalar returns and advantage estimates in a few different ways.

#### `episode_return(traj)`

```python
def episode_return(traj):
    return sum(s.reward for s in traj)
```

This is the total reward of one full episode. It is used for:

- evaluating success/failure in training loops
- comparing policy behavior in experiments
- checking if a trajectory was a success or failure

#### `returns_to_go(rewards, gamma)`

```python
def returns_to_go(rewards, gamma):
    g = np.zeros(len(rewards))
    run = 0.0
    for t in range(len(rewards) - 1, -1, -1):
        run = rewards[t] + gamma * run
        g[t] = run
    return g
```

This is the “reward-to-go” signal. It is not just the total episode return; it assigns credit to each step according to future reward, discounted by `gamma`.

Why it matters:

- a step near the goal should get more credit than a step taken far from the goal
- it stabilizes learning by attributing future reward to earlier decisions

#### `naive(traj)`

```python
def naive(traj):
    return np.full(len(traj), episode_return(traj), dtype=float)
```

This is the simplest baseline but noisy. It gives every step in the trajectory the same total reward, even if the bad event happened only at the end.

It is deliberately rough. It is useful as a baseline, but the repo quickly moves beyond it because it causes high variance.

#### `discounted_to_go(traj, gamma)`

This applies a proper discounted reward-to-go to each step. It is the most standard shape for policy gradient signal design.

#### `WithBaseline`

```python
class WithBaseline:
    def __init__(self, fn, momentum=0.99):
        self.fn = fn
        self.momentum = momentum
        self.b = 0
        self._seen = False
```

This subtracts a running baseline from the raw return signal:

```python
raw = self.fn(traj)
advantage = raw - self.b
```

Why subtract a baseline:

- reduces variance
- keeps updates focused on whether a decision was better than average
- avoids crediting all steps equally when the episode return is zero or when some states are naturally easier to reach

This is the same conceptual idea as PPO/GRPO-style advantage normalization: reward signal is centered around a baseline estimate.

---

## 3) Policy contract: the exact API the trainer expects

The repo uses a tabular softmax policy, but the contract is truly broader: a policy is any object that can sample actions, evaluate log-probability, and provide a probability snapshot.

The exact softmax policy is:

```python
class TabularSoftmaxPolicy:
    def __init__(self, n_states, n_action, seed=0):
        self.n_states = n_states
        self.n_action = n_action
        self.theta = np.zeros((n_states, n_action), dtype=float)
        self.rng = np.random.default_rng(seed)
        self.version = 0
```

Required methods:

### `probablity(state)`

This takes a state index and returns a probability vector over actions.

It is implemented as softmax over logits:

```python
logits = self.theta[state]
z = logits - logits.max()
e = np.exp(z)
return e / e.sum()
```

This matters because every trust-region KL calculation compares probability rows between old and new policies.

### `action(state)`

The trainer expects the sampled action and log-probability for that action:

```python
action = int(self.rng.choice(self.n_action, p=p))
return action, float(np.log(p[action]))
```

This is the exact contract used by `rollout()` and by policy-gradient updates.

### `logprob(state, action)`

This is the log probability of a specific action under the current policy:

```python
return float(np.log(self.probablity(state)[action]))
```

This is used in the policy-gradient update to weight each sampled action by its log-probability gradient.

### `grad_logprob(state, action)`

This is the critical term for REINFORCE-style learning:

```python
g = -self.probablity(state)
g[action] += 1.0
return g
```

This derivative is the gradient of `log pi(a|s)` with respect to the logits. It is exactly the score-function term used in policy gradient.

Why this is central:

- the trainer accumulates `a * grad_logprob(...)`
- the update uses the sampled advantage `a` times the policy score gradient
- if this function is wrong, the whole learning loop is wrong even if the rest of the code looks valid

The file includes a finite-difference gradient check for exactly this reason.

### `snapshot()`

```python
def snapshot(self):
    p = np.exp(self.theta - self.theta.max(axis=1, keepdims=True))
    return p / p.sum(axis=1, keepdims=True)
```

This returns the full action distribution for every state in matrix form. It is the reference used for KL calculations between policy versions.

### `mean_entropy()`

This measures how uncertain the policy is:

```python
return float(-np.mean(np.sum(p * np.log(p), axis=1)))
```

Why it matters:

- high entropy means the policy is exploratory
- low entropy means the policy is nearly deterministic
- entropy is frequently used as a training diagnostic and sanity check

If you replace the policy with a continuous Gaussian policy, the equivalent continuous contract still needs:

- a sampling function to generate an action from the current policy
- a log-probability function for the chosen action
- a score-function or Jacobian-like gradient with respect to parameters
- a reference distribution for the trust-region KL computation

The repo’s discrete softmax form is just one concrete instance.

---

## 4) Bucket contract: the trust-region grouping logic

The repo is designed to regulate policy updates by state region, not just globally. That is why it has `AdaptiveBuckets` and a bucket function per environment.

### 4.1 What a bucket function does

A bucket function maps a state ID to a label or bucket name.

Example from the grid environment:

```python
def grid_quadrant(state, ncol=4):
    row, col = divmod(int(state), ncol)
    return f"q{row // 2}{col // 2}"
```

This groups the board into quadrants.

For a continuous-control system, the equivalent would be something like:

```python
def continuous_bucket(state_vec):
    # e.g. map continuous state into region bins
    region = tuple(np.digitize(state_vec, bins) for state_vec, bins in zip(...))
    return region
```

The key requirement is stability: the same state should always map to the same bucket.

### 4.2 `AdaptiveBuckets`

```python
class AdaptiveBuckets:
    def __init__(self, bucket_fn, sig_fn=None, min_samples=10**9):
        self._fn = bucket_fn
        self._of = {}
        self._states = defaultdict(list)
```

It is a deterministic estimator of state-to-bucket assignment.

Important semantics:

- `assign(state)` returns the bucket ID for that state
- `states_in(bucket)` returns all states in that bucket
- `buckets()` enumerates all known buckets
- bucket membership is used later to compute separate KL and controller updates

The bucket logic is not “for aesthetics”; it is how local policy trust regions are regulated.

### 4.3 KL metrics used per bucket

The repo computes KL between policy rows using:

```python
def kl_rows(p, q):
    return (p * (np.log(p + 1e-12) - np.log(q + 1e-12))).sum(axis=1)
```

This returns the KL divergence for each state row.

The gradient of this KL with respect to logits is also needed by the controller:

```python
def kl_grad(p, q):
    logr = np.log(p + 1e-12) - np.log(q + 1e-12)
    kl = (p * logr).sum(axis=1, keepdims=True)
    return p * (logr - kl)
```

Why this matters:

- `kl_rows` tells the trainer how much a bucket moved relative to the reference policy
- `kl_grad` tells the update direction of the trust-region penalty term
- this is the trust-region core that makes the system behave like constrained optimization rather than unconstrained policy gradient

This is conceptually very close to PPO-like or TRPO-like trust-region structure, even though the code is much simpler and more explicit.

---

## 5) Why each training metric is there

These are the metrics the repo tracks, and what they mean.

### `reward` and `episode_return`

- direct measure of whether the decision sequence solved the task
- used in the rollout and evaluation loops

### `return-to-go` / `advantage`

- converts sparse episode rewards into per-step credit assignment
- keeps learning signals aligned with decisions that actually led to future reward

### `entropy`

- measures randomness of the policy
- high entropy means exploratory; low entropy means deterministic policy

### `KL` per bucket

- measures how far a bucket moved relative to the old target policy
- used as the trust-region constraint signal

### `beta`

- the trust-region penalty weight associated with a bucket
- bigger beta => stronger penalty against moving too far from the reference policy

### `target`

- the desired KL target for a bucket
- controller changes this slowly over time to permit only healthy movement

### `perf` and `perf_se`

- `perf` is the mean return-to-go in the bucket
- `perf_se` is the estimate of uncertainty in that mean
- used to decide whether a bucket improved, worsened, or was flat

### `cell` decision

The controller’s `cell` value is a diagnosis tag, not just a label. It tells whether the last update was:

- `healthy`: bucket improved and moved as expected
- `overwrote`: moved too much and got worse
- `data_shift`: world changed or distribution shifted, making old policy assumptions stale
- `easy_drift`: bucket moved a little and improved, no need to tighten
- `flat`: no clear change, likely noise

This is why the repo names its controller a “cascade control” and not just a simple learning-rate schedule.

### `recovery_iters`

This is how many iterations it took after a fault to recover to the desired behavior. It is one of the main experiment outcome metrics.

### `kl_spent`

This measures the total KL movement after the environment fault. It captures how much policy movement occurred during the recovery phase.

### `heal_kl`

This is the policy movement caused specifically by healing interventions, such as reheat or rollback. It tells you whether the system healed itself by making controlled corrective action rather than by broad-blind updates.

### `online_regret` and `collateral_online`

These are especially important for contextual bandit experiments.

- `online_regret` measures how much expected reward was lost by the current policy relative to the best action in each context
- `collateral_online` measures how much untouched contexts were degraded while the system fixed the broken ones

This makes the repo evaluate not just “did the broken regime recover?” but also “did the fix cause damage elsewhere?”

### `stable_after_recovery`

This detects whether the recovered policy stayed stable once recovery happened. It is a real quality measure, not just a speed metric.

---

## 6) Controller contract and why it exists

The core controller interface is:

```python
ctrl.beta[bucket] -> penalty weight
ctrl.update(bucket, kl_now, kl_prev, perf, perf_se) -> (cell, decided)
```

`StaticBeta` is the simple baseline. It keeps a fixed penalty and never adapts. This is the honest baseline used in experiments.

`PIDBetaController` is the main adaptive controller. It keeps track of:

- a KL target per bucket
- a beta value per bucket
- an inner PID loop on log(beta)
- an outer loop that decides how much KL is allowed per bucket

The comment in the code explains the subtle but crucial piece: `perf` is measured on data from before the current update, while `kl_now` corresponds to the update that just happened. That is why the code stores `kl_prev` and compares it to the previous period’s performance.

This avoids blaming the wrong update for a performance change.

The controller is not a random gain scheduler. It is built to answer four questions:

1. Did the bucket move too far?
2. Did it improve or worsen?
3. Is the world changing, or is the bucket simply oscillating?
4. Should the trust region be loosened, tightened, or frozen?

This is exactly the kind of logic you would want in a continuous-control GRPO-like variant: local trust regions and adaptive penalty weights per region of state space.

---

## 7) Healing logic: what the repo is trying to protect against

The healing module is not optional decoration; it is part of the algorithmic story.

### `OscillationDetector`

This detects when a control signal is ringing:

- sign of the PID error keeps flipping
- the error crosses zero repeatedly at high rate
- indicates the controller is overreacting

Heal action:

- damp the gains
- reset the integral term
- restore gradually when the system is calm again

### `FlapDetector`

This detects sequences like A -> B -> A in a value stream.

In the repo it is used in two ways:

- target flapping: the outer loop alternates between looser and tighter trust-region targets
- policy flapping: the greedy action in a state keeps switching back and forth

Heal action:

- freeze the target or tighten the trust region

### `RollbackGuard`

This is a safety mechanism for catastrophic regressions.

If a bucket keeps getting worse after moving as much as allowed, the code stores a last-known-good snapshot of that bucket and restores it when necessary.

This is a real “self-healing” mechanism, not just logging.

The key idea is:

- if the bucket moved too much and was worse, restore the prior row values
- verify after a few periods whether the repair worked
- if the world has truly shifted, drop the stale snapshot instead of forcing an outdated policy forever

This is the self-healing story behind the experiments.

---

## 8) Experiment metrics and what they prove

The experiment runner in `experiments.py` is not just “run training.” It is a fault-recovery benchmark.

The variants are:

- `static`: fixed beta only, no healing
- `global_heal`: one global trust region with healing
- `bucket_pid`: bucketed adaptive controller, no healing
- `self_heal`: bucketed adaptive controller with healing

The claim is that `self_heal` recovers the fastest and most robustly while changing the policy the least.

### Evaluation metrics in `ok_metric`

For `GridLake`:

- `greedy_success`: whether the greedy policy reaches the goal
- `greedy_len`: number of steps until termination

For `RegimeBandit`:

- `greedy_correct`: fraction of contexts where the greedy action is best
- `regret`: expected regret of the greedy policy
- `online_regret`: policy regret weighted by context distribution
- `collateral_online`: how much untouched contexts were harmed while fixing a broken one

This tells you the repo is explicitly measuring trade-off, not just “reward went up for the broken context.”

### Recovery metrics in `run_one`

- `converged_before_fault`: whether the policy was already stable before the fault
- `recovery_iters`: time to recover after the fault
- `stable_after_recovery`: how stable it remained after recovery
- `kl_spent`: how much total policy movement occurred during recovery
- `heal_kl`: how much movement came from healing actions

If you are adding a new environment or policy, this is the benchmark structure that must be matched.

---

## 9) How this repo expects a new policy to match the format

If you add a new policy, do not just build any RL policy object. The repo expects the following behavioral shape.

Minimum required contract:

```python
class MyPolicy:
    def __init__(self, n_states, n_actions, seed=0):
        self.n_states = n_states
        self.n_actions = n_actions
        self.theta = ...
        self.version = 0

    def snapshot(self):
        # return probabilities for each state and action
        ...

    def action(self, state):
        # return (action, logprob)
        ...

    def logprob(self, state, action):
        ...

    def grad_logprob(self, state, action):
        ...

    def mean_entropy(self):
        ...
```

Why this is strict:

- `rollout` calls `policy.action(obs)`
- trainer computes `grad_logprob` for each sampled step
- KL measurement uses `snapshot()`
- bucket logic uses state IDs, not arbitrary observation arrays

This is a strong contract, and the code is built around it.

### Continuous-control adaptation

If you wanted this to be a proper continuous-control GRPO implementation, the adaptation pattern would be:

- keep `action(state)` / `logprob(...)` / `grad_logprob(...)` semantics
- replace the discrete softmax policy with a Gaussian or mixture policy over continuous actions
- make `snapshot()` return a parameterized distribution or a sampled action distribution, not a discrete table
- ensure the bucket function keys states with a compact, deterministic representation
- convert continuous state/action spaces to a stable bucketed domain before applying KL control

The repo’s current implementation is discrete and tabular, so the exact `n_states` and `n_actions` structure is baked in. A truly continuous-control version would still need to respect the same high-level object API, even though the underlying math changes.

---

## 10) How this repo expects a new bucketing method to match the format

Bucket functions are not accidental. They are the place where a state-space region becomes a control region.

The rule is:

```python
def my_bucket_fn(state):
    return some_hashable_bucket_id
```

This bucket should be:

- deterministic
- hashable
- stable across runs
- meaningful to the control problem

The actual bucket logic uses that label to create per-region trust regions.

Examples in this codebase:

- grid bucket: quadrant of the map
- bandit bucket: health/load regime
- single global bucket: `all`

For continuous control, a bucket may be based on:

- radial sectors in a 2D state space
- discretized velocity/position bins
- regime labels from a latent controller or external context

The repo does not support arbitrary bucket shapes without preserving the same `state -> bucket` API.

---

## 11) How this repo expects a new environment to match the format

New environments have to match the same `reset` / `step` interface and define the same summary fields the trainer expects.

Required environment fields:

- `n_states`
- `n_actions`
- `reset(seed=None)` returns `(state, info)`
- `step(action)` returns `(next_state, reward, terminated, truncated, info)`
- `set_fault(on)` when fault injection is needed

The experiment runner expects the environment to support both normal learning and post-fault recovery experiments.

For a continuous-control environment, the same logic still applies, but the environment should expose a state vector and action vector rather than a discrete state index. The repo’s controller machinery is local to state regions, so you must define an appropriate bucket transform for the continuous state space before plugging it into the existing KL controller.

---

## 12) The exact extension recipe if you are adding a new algorithm

For a new policy, new bucketing method, or new environment, the repo expects the following checklist to be satisfied:

### Policy checklist

- `policy.action(state)` returns `(action, logprob)`
- `policy.logprob(state, action)` is defined
- `policy.grad_logprob(state, action)` matches the score-function gradient
- `policy.snapshot()` returns action probabilities over all states
- `policy.theta` is the trainable parameter object in the correct shape

### Bucketing checklist

- define a function `state -> bucket_id`
- bucket IDs are stable and hashable
- all states in the same bucket share a trust-region update

### Environment checklist

- `reset` and `step` follow the Gym contract
- the environment returns a scalar reward and terminal flags
- if the environment is faulted, there is a way to trigger the change mid-training

### Experiment checklist

- add the environment to the `ENVS` registry
- define the correct bucket function for that environment
- define recovery metrics consistent with the runner
- compare against `static`, `bucket_pid`, and `self_heal` variants if you are testing healing logic

---

## 13) Summary: the repo’s design philosophy

The repository is built around the idea that policy updates should not be globally unconstrained. Instead:

- a bucket defines which states are treated as one control region
- a trust-region penalty regulates how much that region can move
- the controller decides when to loosen or tighten the region
- healing intervenes when the controller oscillates or the policy flaps

This is a strong research-harness pattern for policy optimization under changing environments. The grid is only the simplest demonstration. The actual design is “bucketed trust-region policy control with self-healing,” not “just grid RL.”

If you want to convert this to a continuous-function GRPO setup, keep the same layer boundaries and contract semantics, but swap in:

- a Gaussian or mixture policy instead of a softmax table
- continuous state buckets or radial region bins instead of grid quadrants
- continuous action probability densities instead of discrete action logits
- the same KL-trust-region and healing machinery, applied to the bucketed continuous state regions

That is the correct way to think about the repo: it is a pattern for adaptive trust-region policy learning under regime shifts, with a tabular grid environment as the simplest concrete case.
