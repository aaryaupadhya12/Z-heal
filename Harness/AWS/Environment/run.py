import pandas as pd

def episode(env, agent , seed = 0, start_minute = None , minutes = None):
    state , _ = env.reset(seed = seed , start_minute = start_minute)
    agent.reset()
    rows, done = [] , False

    while not done:
        feats = env.features()
        action = agent.act(state,feats)
        # Terminated -> Tge episoded ended for a real rasoin in the world 
        # Truncated  -> The episode was cut short by a time limit eventhogh thw world couve continued (usualy people just sue _)
        state, reward, terminated, truncated, info = env.step(action)
        done = terminated or truncated
        agent.observe(state,action,reward,info)
        rows.append(info)
        if minutes and len(rows) >= minutes *4: # assumption and  hardcoded right now as only 4 zones if icnreses need to change 
            break
    return pd.DataFrame(rows)


def summarise(df,name,window):
    per_min = df.drop_duplicates("minute")
    hours = len(per_min) / 60
    return {
        "window": window, "agent": name,
        "p50_ms": round(per_min["p50_ms"].median(), 1),
        "p99_ms": round(per_min["p99_ms"].quantile(0.99), 1),
        "rupees_per_hour": round(per_min["rupees"].sum() / hours, 1),
        "slo_miss_min": int(per_min["slo_miss"].sum()),
        "local_pct": round(100 * per_min["local_frac"].mean(), 1),
        "mean_reward": round(df["reward"].mean(), 3),
    }