"""Self-healing: detect -> diagnose -> act -> verify, per bucket.

Three failure modes this file knows how to spot:

1. Controller oscillation  (OscillationDetector)
   beta overshoots, KL swings above/below target, beta swings back.
   Signature: the PID error keeps changing sign.
   Heal: cut that bucket's PID gains, reset its integral. Restore gains
   slowly once it has been calm for a while.

2. Target flapping  (FlapDetector on target moves)
   the outer loop alternates "permit more" / "permit less".
   Heal: freeze that bucket's target for a cooldown.

3. Policy flapping  (FlapDetector on greedy actions)
   the greedy action in a state goes A -> B -> A.
   Heal: tighten that bucket's trust region.

Plus one repair action that isn't about oscillation:

4. Regression after a big move  (RollbackGuard)
   bucket moved as much as allowed and got worse, repeatedly.
   Heal: restore the bucket's last-known-good rows. Then VERIFY: if
   performance doesn't come back, the snapshot is stale (the world
   changed), so drop it instead of rolling back forever.

Everything writes to one HealLog, which is the evidence you show.
"""

from collections import defaultdict, deque

import numpy as np


class HealLog:
    def __init__(self):
        self.events = []
        self.it = 0          # set by the training loop each iteration

    def add(self, bucket, event, **detail):
        self.events.append({"iter": self.it, "bucket": bucket,
                            "event": event, **detail})

    def summary(self):
        out = defaultdict(lambda: defaultdict(int))
        for e in self.events:
            out[e["bucket"]][e["event"]] += 1
        return {b: dict(v) for b, v in out.items()}

    def since(self, it):
        return [e for e in self.events if e["iter"] >= it]


# ----------------------------------------------------------------------
# 1. Oscillation in a scalar error signal
# ----------------------------------------------------------------------

class OscillationDetector:
    """Zero-crossing rate of a signal over a sliding window.

    A well-damped loop crosses zero rarely; pure noise around the target
    crosses about half the time; a loop that is ringing crosses on almost
    every step. The threshold sits between noise and ringing. Tiny errors are ignored (deadband) so noise around the
    target doesn't count as oscillation.
    """

    def __init__(self, window=12, min_len=10, zcr_thresh=0.75, deadband=0.25):
        self.window = window
        self.min_len = min_len
        self.zcr_thresh = zcr_thresh
        self.deadband = deadband
        self._hist = defaultdict(lambda: deque(maxlen=window))

    def push(self, key, x):
        if abs(x) >= self.deadband:
            self._hist[key].append(np.sign(x))

    def zcr(self, key):
        h = self._hist[key]
        if len(h) < 2:
            return 0.0
        s = np.array(h)
        return float((s[1:] != s[:-1]).mean())

    def oscillating(self, key):
        return (len(self._hist[key]) >= self.min_len
                and self.zcr(key) >= self.zcr_thresh)

    def clear(self, key):
        self._hist[key].clear()


# ----------------------------------------------------------------------
# 2/3. A -> B -> A flips in a discrete sequence
# ----------------------------------------------------------------------

class FlapDetector:
    """Counts A->B->A reversals in the last `window` changes of a value."""

    def __init__(self, window=6, flips_thresh=2):
        self.flips_thresh = flips_thresh
        self._changes = defaultdict(lambda: deque(maxlen=window))
        self._last = {}

    def push(self, key, value):
        """Record a value; return True if it is flapping."""
        prev = self._last.get(key)
        self._last[key] = value
        if prev is None or value == prev:
            return False
        self._changes[key].append((prev, value))
        ch = list(self._changes[key])
        flips = sum(1 for a, b in zip(ch, ch[1:]) if b == (a[1], a[0]))
        return flips >= self.flips_thresh

    def clear(self, key):
        self._changes[key].clear()


class PolicyFlapMonitor:
    """Watches the greedy action per state; reports buckets that flap.

    Only CONFIDENT choices count (top prob beats runner-up by `margin`),
    so near-ties while the policy is still learning are not flagged.
    """

    def __init__(self, window=6, flips_thresh=2, margin=0.2):
        self.det = FlapDetector(window, flips_thresh)
        self.margin = margin

    def check(self, probs, buckets):
        flapping = set()
        top2 = np.sort(probs, axis=1)[:, -2:]
        confident = (top2[:, 1] - top2[:, 0]) >= self.margin
        greedy = probs.argmax(axis=1)
        for b in list(buckets.buckets()):
            for s in buckets.states_in(b):
                if confident[s] and self.det.push(s, int(greedy[s])):
                    flapping.add(b)
                    self.det.clear(s)
        return flapping


# ----------------------------------------------------------------------
# 4. Rollback with verification
# ----------------------------------------------------------------------

class RollbackGuard:
    """Per-bucket last-known-good snapshot, used only when a bucket
    regresses after moving as much as it was allowed to.

    Called once per OUTER decision (not every iteration).
    """

    def __init__(self, log, strikes_to_rollback=2, verify_periods=3):
        self.log = log
        self.k = strikes_to_rollback
        self.verify_periods = verify_periods
        self.snap = {}                     # bucket -> (states, rows, perf)
        self.strikes = defaultdict(int)
        self.pending = {}                  # bucket -> periods since rollback

    def observe(self, bucket, cell, perf, margin, theta, states):
        """Returns the action taken: 'rollback' or None."""
        # --- verify a rollback we already did -------------------------
        if bucket in self.pending:
            self.pending[bucket] += 1
            snap_perf = self.snap[bucket][2]
            if perf >= snap_perf - margin:
                self.log.add(bucket, "rollback_verified", perf=perf)
                del self.pending[bucket]
            elif self.pending[bucket] >= self.verify_periods:
                self.log.add(bucket, "snapshot_stale", perf=perf,
                             snap_perf=snap_perf)
                del self.pending[bucket]
                del self.snap[bucket]
            return

        # --- the world changed: old snapshot is no longer "good" ------
        if cell == "data_shift":
            self.strikes[bucket] = 0
            if bucket in self.snap:
                self.log.add(bucket, "snapshot_invalidated", perf=perf)
                del self.snap[bucket]
            return

        # --- moved as allowed and got worse --------------------------
        if cell == "overwrote":
            self.strikes[bucket] += 1
            if self.strikes[bucket] >= self.k and bucket in self.snap:
                snap_states, rows, snap_perf = self.snap[bucket]
                theta[snap_states] = rows
                self.log.add(bucket, "rollback", perf=perf,
                             snap_perf=snap_perf)
                self.strikes[bucket] = 0
                self.pending[bucket] = 0
                return "rollback"
            return

        self.strikes[bucket] = 0
        old = self.snap.get(bucket)
        if old is None or perf >= old[2] - margin:
            st = list(states)
            self.snap[bucket] = (st, theta[st].copy(), perf)
