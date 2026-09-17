capacity(m, z)        = servers[m, z] × PER_POD_RATE
routing_row(z, spill) = keep (1 − spill) in z; split the rest over other zones by their spare room
loads(m, rows)        = for each zone k: Σ over source zones j of arrivals[m, j] × rows[j][k]
latency(m, k, load)   = (mean_ms, p99_ms) for zone k            ← Part 2
cost(m, rows)         = rupees for the bytes that crossed zones   ← Part 2


state = busy_band × 9 + latency_band × 3 + spare_band      → 0 … 35


rng = np.random.default_rng(seed)
pick a start minute m0 inside the chosen split, with at least EPISODE_MIN minutes left
m = m0; zone = 0
spill = [0, 0, 0, 0]                  every zone starts "all local"
last_p99 = [svc_p99[m0]] × 4          so the first state has a latency value
return state of zone 0

1. action → the deciding zone's row
     if action is a number: rows[zone] = routing_row(zone, ACTIONS[action])
     if action is a dict (the AWS rule): rows[zone] = that dict
2. load on every zone:        loads = loads(m, rows)
3. latency in every zone:     for each k: mean_k, p99_k = latency(m, k, loads[k])
4. what ALL users experience: weight each zone's latency by how much traffic went there
                              (+ RTT_MS for traffic that crossed zones)
                              → sys_mean, sys_p99
5. cost:                      rupees = cost(m, rows)
6. reward = −( sys_p99 / SLO_MS + LAMBDA × rupees / RUPEE_SCALE + SLO_PENALTY × (sys_p99 > SLO_MS) )
7. remember last_p99[k] = p99_k for every zone
8. move on: zone += 1; after zone 3 → zone = 0 and m += 1
9. done = (m == m0 + EPISODE_MIN)
10. return next state, reward, done, info (sys_p99, rupees, local share, …)

for agent in [force_local, round_robin, aws_rule]:
    state = env.reset(seed)
    loop until done: action = agent(env); state, reward, done, info = env.step(action)
    save sys_p99, rupees, slo_miss per step




