import pandas as pd
from pathlib import Path
Path("runs").mkdir(exist_ok=True)

from ..Algorithms.Force_local import ForceLocal
from ..Algorithms.Server_Weighted_Round_Robin import Server_Weighted_Round_Robin as RoundRobin
from ..Algorithms.psuedo_envoy import Envoy
from ..fixed import TABLE_PATH
from .run import episode, summarise
from .zone_env import ZoneEnv, load_Arrays

SEEDS = range(5)


def main():
    data = load_Arrays(TABLE_PATH)
    env_train = ZoneEnv(data, split="train", seed=0)
    env_test = ZoneEnv(data, split= "test", seed =0)


    rows = []
    for split_name, env in (("train", env_train), ("test", env_test)):
        for seed in SEEDS:
            for agent in (ForceLocal(), RoundRobin(), Envoy()):
                df = episode(env, agent, seed=seed, minutes=360)
                df.to_parquet(f"runs/{split_name}_{agent.name}_s{seed}.parquet")
                r = summarise(df, agent.name, split_name)
                r["start_minute"] = int(df["minute"].iloc[0]) # Tels us whcih window it used 
                r["seed"] = seed
                rows.append(r)

    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    table.to_csv("first_table.csv", index=False)
    print(table.groupby(["window", "agent"])[
    ["p99_ms", "rupees_per_hour", "slo_miss_min", "local_pct", "mean_reward"]
].median().round(1).to_string())


if __name__ == "__main__":
    main()