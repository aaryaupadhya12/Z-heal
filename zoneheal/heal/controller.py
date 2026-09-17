"""Per-bucket trust-region controllers.

Interface every controller implements:
    ctrl.beta[bucket]                                     -> penalty weight
    ctrl.update(bucket, kl_now, kl_prev, perf, perf_se)   -> (cell, decided)

    kl_now   KL produced by THIS iteration's update (what beta just did)
    kl_prev  KL of the PREVIOUS update -- the one whose effect `perf` measures
    perf     mean return-to-go of steps in this bucket, under the current policy
    perf_se  standard error of that mean

Why kl_prev: `perf` is measured on data collected before this iteration's
update, so it reflects the previous update. Pairing it with kl_now would
blame the wrong update.
"""

import math
from collections import defaultdict

import numpy as np

from zoneheal.heal.healing import FlapDetector, HealLog, OscillationDetector


class StaticBeta:
    """Fixed beta. The honest baseline -- sweep it and keep the best."""

    def __init__(self, beta):
        self.beta = defaultdict(lambda: beta)
        self.last_decision = {}
        self.log = []

    def update(self, bucket, kl_now, kl_prev, perf, perf_se):
        return "static", False


class PIDBetaController:
    """Cascade control of the per-bucket trust region.

    OUTER loop (slow: every `outer_every` iterations, per bucket)
        Decides how much a bucket is ALLOWED to move (its KL target),
        from how much it moved and whether it got better or worse.
        Uses period averages and a noise-aware threshold, so it does
        not chase sampling noise.

                         moved as allowed      barely moved
        got worse        overwrote -> tighten  data_shift -> loosen
        got better       healthy               easy_drift
        no clear change  flat -> target drifts back toward its default

    INNER loop (fast: every iteration, per bucket)
        PID on log(beta) so the measured KL tracks the target.

    Cascade rule: the outer loop must be several times slower than the
    inner loop. If both run every iteration they fight each other --
    one of the ways this system oscillates.

    Self-healing hooks (per bucket):
      idle gate   converged bucket has KL ~ 0. That is not "below target",
                  there is nothing to control, so beta is held.
      oscillation PID error keeps flipping sign -> halve that bucket's
                  gains, reset its integral; restore slowly when calm.
      target flap outer loop alternates loosen/tighten -> freeze target.
      kick        on data_shift, lower beta immediately (a converged
                  bucket's inner loop is idle-gated and would never react).
    """

    def __init__(self, beta0=1.0, kl_target0=1e-3,
                 kp=0.4, ki=0.05, kd=0.1,
                 beta_lo=1e-2, beta_hi=1e2,
                 target_lo=1e-6, target_hi=1e-1,
                 integral_clamp=3.0, max_log_step=0.5,
                 outer_every=5, outer_gain=1.5, z_thresh=3.0,
                 perf_eps=0.005, hit_frac=0.8, idle_frac=0.05,
                 settle_rate=0.1, heal=True, log=None):
        self.beta = defaultdict(lambda: beta0)
        self.target = defaultdict(lambda: kl_target0)
        self.kl_target0 = kl_target0

        self.kp0, self.ki0, self.kd0 = kp, ki, kd
        self.gain = defaultdict(lambda: 1.0)        # per-bucket gain scale
        self._I = defaultdict(float)
        self._prev_e = defaultdict(float)
        self._calm = defaultdict(int)

        self.beta_lo, self.beta_hi = beta_lo, beta_hi
        self.target_lo, self.target_hi = target_lo, target_hi
        self.integral_clamp = integral_clamp
        self.max_log_step = max_log_step

        self.outer_every = outer_every
        self.outer_gain = outer_gain
        self.z_thresh = z_thresh
        self.perf_eps = perf_eps
        self.hit_frac = hit_frac
        self.idle_frac = idle_frac
        self.settle_rate = settle_rate

        # per-bucket period accumulators
        self._n = defaultdict(int)
        self._kl_sum = defaultdict(float)
        self._perf_sum = defaultdict(float)
        self._se2_sum = defaultdict(float)
        self._last_period = {}             # bucket -> (mean perf, se)
        self._freeze = defaultdict(int)
        self.last_decision = {}            # bucket -> dict, read by the guard

        self.heal = heal
        self.hlog = log if log is not None else HealLog()
        self.osc = OscillationDetector()
        self.tflap = FlapDetector(window=6, flips_thresh=2)
        self.log = []

    # ------------------------------------------------------------------
    # outer loop
    # ------------------------------------------------------------------
    def _outer(self, b):
        n = self._n[b]
        kl_mean = self._kl_sum[b] / n
        perf = self._perf_sum[b] / n
        se = math.sqrt(self._se2_sum[b]) / n
        self._n[b] = 0
        self._kl_sum[b] = self._perf_sum[b] = self._se2_sum[b] = 0.0

        prev = self._last_period.get(b)
        self._last_period[b] = (perf, se)
        if prev is None:
            return "warmup", perf, 0.0, self.perf_eps

        dperf = perf - prev[0]
        thr = max(self.perf_eps, self.z_thresh * math.sqrt(se**2 + prev[1]**2))
        tgt = self.target[b]
        moved = kl_mean >= self.hit_frac * tgt

        if dperf < -thr:
            cell = "overwrote" if moved else "data_shift"
        elif dperf > thr:
            cell = "healthy" if moved else "easy_drift"
        else:
            cell = "flat"

        if self._freeze[b] > 0:
            self._freeze[b] -= 1
            return cell, perf, dperf, thr

        direction = 0
        if cell == "overwrote":
            tgt /= self.outer_gain
            self.beta[b] = min(self.beta[b] * self.outer_gain, self.beta_hi)
            direction = -1
        elif cell == "data_shift":
            tgt *= self.outer_gain
            self.beta[b] = max(self.beta[b] / self.outer_gain, self.beta_lo)
            self._I[b] = 0.0
            direction = +1
        elif cell == "flat":
            tgt *= (self.kl_target0 / tgt) ** self.settle_rate
        self.target[b] = float(np.clip(tgt, self.target_lo, self.target_hi))

        if self.heal and direction != 0 and self.tflap.push(b, direction):
            self._freeze[b] = 3
            self.tflap.clear(b)
            self.hlog.add(b, "target_flap_frozen", target=self.target[b])
        return cell, perf, dperf, thr

    # ------------------------------------------------------------------
    # inner loop
    # ------------------------------------------------------------------
    def _inner(self, b, kl):
        tgt = self.target[b]
        if kl < self.idle_frac * tgt:
            self._prev_e[b] = 0.0
            return 0.0, 0.0, True

        e = float(np.clip((kl - tgt) / max(tgt, 1e-12), -5.0, 5.0))
        d = e - self._prev_e[b]
        self._prev_e[b] = e

        beta = self.beta[b]
        saturated = beta <= self.beta_lo * 1.001 or beta >= self.beta_hi * 0.999
        if not saturated:
            self._I[b] = float(np.clip(self._I[b] + e,
                                       -self.integral_clamp, self.integral_clamp))

        g = self.gain[b]
        u = g * (self.kp0 * e + self.ki0 * self._I[b] + self.kd0 * d)
        u = float(np.clip(u, -self.max_log_step, self.max_log_step))
        self.beta[b] = float(np.clip(beta * math.exp(u),
                                     self.beta_lo, self.beta_hi))

        if self.heal:
            self.osc.push(b, e)
            if self.osc.oscillating(b):
                self.gain[b] = max(self.gain[b] * 0.5, 0.1)
                self._I[b] = 0.0
                self._calm[b] = 0
                self.osc.clear(b)
                self.hlog.add(b, "oscillation_damped", gain=self.gain[b],
                              beta=self.beta[b])
            else:
                self._calm[b] += 1
                if self._calm[b] >= 36 and self.gain[b] < 1.0:
                    self.gain[b] = min(self.gain[b] * 1.25, 1.0)
                    self._calm[b] = 0
                    self.hlog.add(b, "gain_restored", gain=self.gain[b])
        return e, u, False

    # ------------------------------------------------------------------
    def reset_baseline(self, b):
        """Call after any heal action. The intervention itself changes
        performance (e.g. more exploration after a reheat); comparing the
        next period against the pre-heal level would read that as a new
        fault and trigger another heal -- a loop the harness creates itself.
        """
        self._last_period.pop(b, None)
        self._n[b] = 0
        self._kl_sum[b] = self._perf_sum[b] = self._se2_sum[b] = 0.0

    def tighten(self, b, reason):
        """Heal action used from outside (e.g. policy flapping)."""
        self.target[b] = max(self.target[b] / self.outer_gain, self.target_lo)
        self.hlog.add(b, reason, target=self.target[b])

    def update(self, b, kl_now, kl_prev, perf, perf_se):
        kl_now, kl_prev = max(kl_now, 0.0), max(kl_prev, 0.0)
        self._n[b] += 1
        self._kl_sum[b] += kl_prev
        self._perf_sum[b] += perf
        self._se2_sum[b] += perf_se ** 2

        decided = self._n[b] >= self.outer_every
        cell = "-"
        if decided:
            cell, pmean, dperf, thr = self._outer(b)
            self.last_decision[b] = {"cell": cell, "perf": pmean,
                                     "dperf": dperf, "thr": thr}

        e, u, idle = self._inner(b, kl_now)
        self.log.append({"bucket": b, "cell": cell, "kl": kl_now,
                         "target": self.target[b], "beta": self.beta[b],
                         "err": e, "ctrl": u, "I": self._I[b],
                         "gain": self.gain[b], "idle": idle})
        return cell, decided


def natural_kl_target(kls, q=90, fallback=1e-3):
    """KL target from a beta=0 run: how far the policy moves per iteration
    WHILE IT IS LEARNING.

    Most iterations of a run are after convergence, where KL is ~0, so the
    median mostly measures "nothing is happening". A target set there
    strangles learning: the PID pushes beta to its ceiling and the policy
    freezes. The 90th percentile of non-zero KL tracks the learning phase.
    """
    kls = np.asarray(kls, dtype=float)
    nz = kls[kls > 1e-8]
    return float(np.percentile(nz, q)) if len(nz) else fallback
