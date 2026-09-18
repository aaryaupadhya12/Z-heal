
"""envoy_zone_aware.py -- Python port of Envoy's zone-aware routing.

Source: envoyproxy/envoy, tag v1.28.0,
        source/common/upstream/load_balancer_impl.cc
        (ZoneAwareLoadBalancerBase: earlyExitNonLocalityRoutingNew,
         calculateLocalityPercentagesNew, regenerateLocalityRoutingStructuresNew,
         tryChooseLocalLocalityHosts, hostSourceToUse, isHostSetInPanic,
         LoadBalancerBase::recalculatePerPriorityState)
License of the original: Apache License 2.0. This file is a translation of that
logic; keep this header when redistributing.

Why this is the baseline: Amazon ECS Service Connect's zone-aware routing is built
on Envoy's zone-aware routing. AWS's exact Envoy version and settings are not
public beyond what the ECS docs state (e.g. a minimum of 2 x number-of-AZs
endpoints), so `aws_defaults()` encodes only that; everything else uses Envoy's
defaults. Newer Envoy versions add options (force_local_zone, locality basis by
weight) that are not modelled here.

Vocabulary (Envoy's words):
    local cluster     = the CALLING service's hosts (client Envoys), per zone
    upstream cluster  = the CALLED service's hosts, per zone
    locality index 0  = the caller's own zone
    percentages       = integers scaled to 10000 (100.00 %), integer division

Two ways to use it:
    routing_fractions(...)  expected share of one zone's requests per upstream zone
                            (for a minute-level / fluid simulator)
    sample_zone(...)        one request's destination zone, exactly like Envoy's
                            per-request random choice (for a request-level router)
"""

from dataclasses import dataclass

import numpy as np

SCALE = 10000                       # Envoy: 10000ULL * count / total


@dataclass
class ZoneAwareConfig:
    min_cluster_size: int = 6              # ZoneAwareLbConfig.min_cluster_size default
    routing_enabled_pct: float = 100.0     # ZoneAwareLbConfig.routing_enabled default
    panic_threshold_pct: int = 50          # healthy_panic_threshold default
    overprovisioning_factor_pct: int = 140 # default overprovisioning factor 1.4
    fail_traffic_on_panic: bool = False
    allow_different_zone_counts: bool = True   # runtime flag in the new logic


def aws_defaults(n_zones):
    """What the ECS docs state: zone-aware routing needs >= 2 x AZs endpoints."""
    return ZoneAwareConfig(min_cluster_size=2 * n_zones)


# ----------------------------------------------------------------------
# helpers that mirror Envoy functions
# ----------------------------------------------------------------------

def _is_panic(healthy, total, cfg):
    """LoadBalancerBase::isHostSetInPanic (no degraded hosts modelled)."""
    healthy_percent = 0.0 if total == 0 else 100.0 * healthy / total
    return healthy_percent < min(100, cfg.panic_threshold_pct)


def _upstream_in_panic(healthy, total, cfg):
    """Priority-level panic (single priority): panic is only considered when the
    normalized availability is below 100 (recalculatePerPriorityPanic)."""
    if total == 0:
        availability = 0
    else:
        availability = min(100, cfg.overprovisioning_factor_pct * healthy // total)
    if availability == 100:
        return False
    return _is_panic(healthy, total, cfg)


def _ordered(zones, local_zone):
    """Envoy keeps the local locality at index 0."""
    return [local_zone] + [z for z in zones if z != local_zone]


def locality_percentages(order, local_healthy, upstream_healthy):
    """calculateLocalityPercentagesNew: list of (local_pct, upstream_pct), SCALE-based."""
    total_local = sum(local_healthy.get(z, 0) for z in order)
    total_up = sum(upstream_healthy.get(z, 0) for z in order)
    return [
        (
            SCALE * local_healthy.get(z, 0) // total_local if total_local else 0,
            SCALE * upstream_healthy.get(z, 0) // total_up if total_up else 0,
        )
        for z in order
    ]


# ----------------------------------------------------------------------
# routing state (regenerateLocalityRoutingStructuresNew)
# ----------------------------------------------------------------------

def build_state(local_zone, zones, local_hosts, local_healthy,
                upstream_hosts, upstream_healthy, cfg=None):
    """Envoy recomputes this whenever host membership or health changes.

    local_hosts / local_healthy        {zone: count} for the calling service
    upstream_hosts / upstream_healthy  {zone: count} for the called service
    """
    cfg = cfg or ZoneAwareConfig()
    order = _ordered(zones, local_zone)
    st = {"order": order, "mode": "none", "local_percent_to_route": 0,
          "residual_capacity": [0] * len(order), "reason": ""}

    up_localities = [z for z in order if upstream_hosts.get(z, 0) > 0]
    local_localities = [z for z in order if local_hosts.get(z, 0) > 0]

    # --- earlyExitNonLocalityRoutingNew ---
    if len(up_localities) < 2:
        st["reason"] = "upstream has fewer than 2 zones"
        return st
    if len(local_localities) < 2:
        st["reason"] = "caller has fewer than 2 zones"
        return st
    if local_hosts.get(local_zone, 0) == 0:
        st["reason"] = "caller has no hosts in its own zone"
        return st
    if not cfg.allow_different_zone_counts and len(up_localities) != len(local_localities):
        st["reason"] = "zone counts differ"
        return st
    if sum(upstream_healthy.get(z, 0) for z in order) < cfg.min_cluster_size:
        st["reason"] = f"fewer than {cfg.min_cluster_size} healthy upstream hosts"
        return st

    pcts = locality_percentages(order, local_healthy, upstream_healthy)
    has_local = upstream_hosts.get(local_zone, 0) > 0
    local_pct0, up_pct0 = pcts[0]

    if has_local and up_pct0 > 0 and up_pct0 >= local_pct0:
        st["mode"] = "direct"
        return st

    st["mode"] = "residual"
    st["local_percent_to_route"] = (up_pct0 * SCALE // local_pct0
                                    if has_local and local_pct0 > 0 else 0)
    residual = []
    for i, (lp, upc) in enumerate(pcts):
        last = residual[i - 1] if i > 0 else 0
        if i == 0 and has_local:
            residual.append(last)
        elif upc > lp:
            residual.append(last + upc - lp)
        else:
            residual.append(last)
    st["residual_capacity"] = residual
    return st


# ----------------------------------------------------------------------
# per-request choice (hostSourceToUse + tryChooseLocalLocalityHosts)
# ----------------------------------------------------------------------

def _source(st, upstream_hosts, upstream_healthy, cfg):
    """hostSourceToUse, up to the routing_enabled draw:
    upstream panic -> all hosts (or fail); no locality routing -> healthy hosts;
    otherwise the request is a candidate for zone-aware routing."""
    total_up = sum(upstream_hosts.values())
    healthy_up = sum(upstream_healthy.values())
    if _upstream_in_panic(healthy_up, total_up, cfg):
        return "fail" if cfg.fail_traffic_on_panic else "all_hosts"
    if st["mode"] == "none":
        return "healthy_hosts"
    return "candidate"


def _proportional(pool, order):
    total = sum(pool.get(z, 0) for z in order)
    return {z: pool.get(z, 0) / total if total else 0.0 for z in order}


def _after_enabled(local_hosts, local_healthy, cfg):
    """hostSourceToUse, after routing_enabled passed: caller-cluster panic check."""
    if _is_panic(sum(local_healthy.values()), sum(local_hosts.values()), cfg):
        return "fail" if cfg.fail_traffic_on_panic else "healthy_hosts"
    return "zone_aware"


def sample_zone(st, rng, local_zone, local_hosts, local_healthy,
                upstream_hosts, upstream_healthy, cfg=None):
    """One request. Returns a zone, or None if Envoy would fail the request."""
    cfg = cfg or ZoneAwareConfig()
    order = st["order"]

    def plain(pool):
        weights = np.array([pool.get(z, 0) for z in order], dtype=float)
        if weights.sum() == 0:
            return None
        return order[rng.choice(len(order), p=weights / weights.sum())]

    src = _source(st, upstream_hosts, upstream_healthy, cfg)
    if src == "fail":
        return None
    if src == "all_hosts":
        return plain(upstream_hosts)
    if src == "healthy_hosts":
        return plain(upstream_healthy)
    # featureEnabled(upstream.zone_routing.enabled, routing_enabled_)
    if rng.random() * 100.0 >= cfg.routing_enabled_pct:
        return plain(upstream_healthy)
    src = _after_enabled(local_hosts, local_healthy, cfg)
    if src == "fail":
        return None
    if src == "healthy_hosts":
        return plain(upstream_healthy)

    if st["mode"] == "direct":
        return order[0]
    if rng.integers(0, SCALE) < st["local_percent_to_route"]:
        return order[0]
    res = st["residual_capacity"]
    if res[-1] == 0:                                   # rounding corner case
        return order[rng.integers(0, len(order))]
    threshold = rng.integers(0, res[-1])
    i = 0
    while threshold >= res[i]:
        i += 1
    return order[i]


# ----------------------------------------------------------------------
# expected fractions (for the fluid simulator)
# ----------------------------------------------------------------------

def routing_fractions(local_zone, zones, local_hosts, local_healthy,
                      upstream_hosts, upstream_healthy, cfg=None):
    """Expected share of `local_zone`'s requests sent to each zone.
    Same decisions as sample_zone, averaged exactly. Rows sum to 1
    (or to 0 if every request would fail)."""
    cfg = cfg or ZoneAwareConfig()
    st = build_state(local_zone, zones, local_hosts, local_healthy,
                     upstream_hosts, upstream_healthy, cfg)
    order = st["order"]

    zero = {z: 0.0 for z in order}
    src = _source(st, upstream_hosts, upstream_healthy, cfg)
    if src == "fail":
        return zero, st
    if src == "all_hosts":
        return _proportional(upstream_hosts, order), st
    if src == "healthy_hosts":
        return _proportional(upstream_healthy, order), st

    plain = _proportional(upstream_healthy, order)
    enabled = min(max(cfg.routing_enabled_pct, 0.0), 100.0) / 100.0
    after = _after_enabled(local_hosts, local_healthy, cfg)
    if after != "zone_aware":
        enabled_part = zero if after == "fail" else plain
        return {z: enabled * enabled_part[z] + (1 - enabled) * plain[z] for z in order}, st

    if st["mode"] == "direct":
        zone_aware = {z: float(z == order[0]) for z in order}
    else:
        p_local = st["local_percent_to_route"] / SCALE
        res = st["residual_capacity"]
        zone_aware = {z: 0.0 for z in order}
        zone_aware[order[0]] += p_local
        if res[-1] == 0:
            for z in order:
                zone_aware[z] += (1 - p_local) / len(order)
        else:
            prev = 0
            for i, z in enumerate(order):
                zone_aware[z] += (1 - p_local) * (res[i] - prev) / res[-1]
                prev = res[i]

    if enabled >= 1.0:
        return zone_aware, st
    return {z: enabled * zone_aware[z] + (1 - enabled) * plain[z] for z in order}, st


def routing_matrix(zones, local_hosts, local_healthy, upstream_hosts, upstream_healthy,
                   cfg=None):
    """{source_zone: {dest_zone: fraction}} for every zone that has callers."""
    rows, states = {}, {}
    for z in zones:
        if local_hosts.get(z, 0) == 0:
            continue
        rows[z], states[z] = routing_fractions(z, zones, local_hosts, local_healthy,
                                               upstream_hosts, upstream_healthy, cfg)
    return rows, states
