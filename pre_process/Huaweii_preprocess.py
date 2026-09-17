"""build_region2.py -- Huawei Public Cloud Trace 2025 -> env table for the simulator.

Uses only the loading pattern from the dataset's demo notebook
(section 1: cold starts; sections 2.3-2.4: quantiles folders), reduced to what
the routing simulator needs.

Inputs (same layout as the notebook):
    <root>/quantiles/R2/requests/*.csv                  one file per day, WIDE (day, time, one column per funcID)
    <root>/quantiles/R2/num_pods/*.csv
    <root>/quantiles/R2/totalCost_quantile_050/*.csv
    <root>/quantiles/R2/totalCost_quantile_099/*.csv
    <root>/quantiles/R2/requestBodySize_avg/*.csv
    <root>/cold_start/R2/*.csv                          one row per cold start
    <per-request glob>                                  Region 2 part 1 request logs (day 30)
    <root>/runtime_triggerType/df_funcID_runtime_triggerType.csv   (only with --sync-only)

Output:
    data/processed/huawei_region2.parquet   one row per (minute, cluster)
    data/processed/huawei_region2.json      metadata: files, shares, assumptions, checks

Usage (start small):
    python build_region2.py --root ../datasets/cold_start_dataset --days 3 \
        --requests-glob "../datasets/cold_start_dataset/per_request/R2/*.csv"
    python build_region2.py ... --days all
"""

import argparse
import json
import os
import time as _time
from glob import glob

import numpy as np
import pandas as pd

S_TO_MS = 1000.0


def daily_files(folder):
    """Sorted daily CSVs in a folder (notebook: sorted(glob(...)))."""
    files = sorted(glob(os.path.join(folder, "*.csv")))
    if not files:
        parent = os.path.dirname(folder.rstrip("/"))
        options = sorted(os.listdir(parent)) if os.path.isdir(parent) else []
        raise FileNotFoundError(
            f"No CSV files in {folder}\n"
            f"Folders that exist in {parent}:\n  " + "\n  ".join(options[:60]))
    return files


def pick_folder(base, candidates):
    """First existing folder among spelling variants (e.g. quantile_099 vs quantile_99)."""
    for name in candidates:
        path = os.path.join(base, name)
        if os.path.isdir(path):
            return path
    raise FileNotFoundError(
        f"None of {candidates} found in {base}. Available: {sorted(os.listdir(base))[:60]}")


def read_wide(path, keep_funcs=None):
    """One daily time-series file -> (DataFrame indexed by time, day Series).
    Columns are funcIDs."""
    df = pd.read_csv(path)
    df = df.set_index("time")
    day = df.pop("day")
    if keep_funcs is not None:
        df = df[[c for c in df.columns if c in keep_funcs]]
    return df, day


def weighted_by_requests(stat_df, req_df):
    """Per minute: sum(stat * requests) / sum(requests), over functions where the
    stat exists and requests > 0. (Region-level approximation of a per-function stat.)"""
    common = stat_df.columns.intersection(req_df.columns)
    v = stat_df[common].reindex(req_df.index)
    w = req_df[common]
    mask = v.notna() & (w > 0)
    num = (v.fillna(0.0) * w).where(mask, 0.0).sum(axis=1)
    den = w.where(mask, 0.0).sum(axis=1)
    return num / den.replace(0.0, np.nan)




def region_minutes(root, region, n_days, keep_funcs):
    base = os.path.join(root, "quantiles", region)
    f_req = daily_files(os.path.join(base, "requests"))
    f_pods = daily_files(os.path.join(base, "num_pods"))
    f_p50 = daily_files(pick_folder(base, ["totalCost_quantile_050", "totalCost_quantile_50"]))
    f_p99 = daily_files(pick_folder(base, ["totalCost_quantile_099", "totalCost_quantile_99"]))
    f_size = daily_files(pick_folder(base, ["requestBodySize_avg"]))

    n = min(len(f_req), len(f_pods), len(f_p50), len(f_p99), len(f_size))
    if len({len(f_req), len(f_pods), len(f_p50), len(f_p99), len(f_size)}) != 1:
        print(f"WARNING: folders have different numbers of daily files; using first {n}")
    if n_days is not None:
        n = min(n, n_days)

    out = []
    for i in range(n):
        t0 = _time.time()
        req, day = read_wide(f_req[i], keep_funcs)
        req = req.fillna(0.0)                                   # counts: empty = 0
        pods, _ = read_wide(f_pods[i], keep_funcs)
        pods = pods.fillna(0.0).reindex(req.index).fillna(0.0)
        p50, _ = read_wide(f_p50[i], keep_funcs)                # stats: empty = no data
        p99, _ = read_wide(f_p99[i], keep_funcs)
        size, _ = read_wide(f_size[i], keep_funcs)

        day_df = pd.DataFrame({
            "day": day.reindex(req.index).values,
            "total_req_per_min": req.sum(axis=1).values,
            "total_pods": pods.sum(axis=1).values,
            "svc_p50_ms": (weighted_by_requests(p50, req) * S_TO_MS).values,
            "svc_p99_ms": (weighted_by_requests(p99, req) * S_TO_MS).values,
            "req_bytes": weighted_by_requests(size, req).values,
        }, index=req.index)
        out.append(day_df)
        del req, pods, p50, p99, size                           # free the wide tables
        print(f"  quantiles day file {i + 1}/{n} done ({_time.time() - t0:.1f}s)")

    rm = pd.concat(out)
    rm.index.name = "time"
    rm = rm.reset_index()
    rm["minute"] = (rm["time"] // 60).astype(int)

    # minutes with no requests have no stats: carry the nearest value
    filled = int(rm[["svc_p50_ms", "svc_p99_ms", "req_bytes"]].isna().any(axis=1).sum())
    rm[["svc_p50_ms", "svc_p99_ms", "req_bytes"]] = (
        rm[["svc_p50_ms", "svc_p99_ms", "req_bytes"]].ffill().bfill())
    files_used = {"requests": f_req[:n], "num_pods": f_pods[:n], "p50": f_p50[:n],
                  "p99": f_p99[:n], "req_bytes": f_size[:n]}
    return rm, filled, files_used




def cold_per_cluster(root, region, n_days):
    files = daily_files(os.path.join(root, "cold_start", region))
    if n_days is not None:
        files = files[:n_days]
    parts = [pd.read_csv(f, usecols=["day", "time", "clusterName", "totalCost_cold_start"])
             for f in files]
    cold = pd.concat(parts, ignore_index=True)
    cold["minute"] = (cold["time"] // 60).astype(int)
    g = cold.groupby(["minute", "clusterName"])
    per_min = pd.DataFrame({
        "cold_n": g.size(),
        "cold_ms": g["totalCost_cold_start"].mean() * S_TO_MS,
    }).reset_index().rename(columns={"clusterName": "cluster"})
    summary = {
        "events": int(len(cold)),
        "per_cluster": cold["clusterName"].value_counts().sort_index().to_dict(),
        "median_cold_ms": float(cold["totalCost_cold_start"].median() * S_TO_MS),
    }
    return per_min, summary, files




def cluster_shares(requests_glob, sample_rows=200_000, chunksize=1_000_000):
    files = sorted(glob(requests_glob))
    if not files:
        raise FileNotFoundError(f"No request-log files match {requests_glob}")
    cols = ["time_worker", "clusterName", "podID", "totalCost_worker", "requestBodySize"]

    req_counts = pd.Series(dtype="float64")
    pod_cluster_parts = []
    samples = []
    t_min, t_max = np.inf, -np.inf
    for f in files:
        for chunk in pd.read_csv(f, usecols=cols, chunksize=chunksize):
            t_min = min(t_min, chunk["time_worker"].min())
            t_max = max(t_max, chunk["time_worker"].max())
            req_counts = req_counts.add(chunk["clusterName"].value_counts(), fill_value=0)
            pod_cluster_parts.append(chunk.groupby(["podID", "clusterName"]).size())
            if sum(len(s) for s in samples) < sample_rows:
                samples.append(chunk[["totalCost_worker", "requestBodySize"]]
                               .sample(frac=min(1.0, sample_rows / max(len(chunk), 1)),
                                       random_state=0))
        print(f"  request log {os.path.basename(f)} done")

    # each pod belongs to the cluster where it appears most
    pc = pd.concat(pod_cluster_parts).groupby(level=[0, 1]).sum()
    pod_home = pc.groupby(level=0).idxmax().map(lambda x: x[1])
    pod_counts = pod_home.value_counts()

    traffic_share = (req_counts / req_counts.sum()).sort_index()
    pod_share = (pod_counts / pod_counts.sum()).reindex(traffic_share.index).fillna(0.0)
    sample = pd.concat(samples) if samples else pd.DataFrame()
    info = {
        "files": files,
        "time_worker_range_s": [float(t_min), float(t_max)],
        "requests_per_cluster": {int(k): int(v) for k, v in req_counts.items()},
        "pods_per_cluster": {int(k): int(v) for k, v in pod_counts.items()},
        "pods_seen_in_more_than_one_cluster": int((pc.groupby(level=0).size() > 1).sum()),
        "sample_median_worker_ms": (float(sample["totalCost_worker"].median() * S_TO_MS)
                                    if len(sample) else None),
        "sample_median_request_bytes": (float(sample["requestBodySize"].median())
                                        if len(sample) else None),
    }
    return traffic_share, pod_share, info




def sync_functions(root):
    path = os.path.join(root, "runtime_triggerType", "df_funcID_runtime_triggerType.csv")
    rt = pd.read_csv(path)
    col = "triggerType-invocationType"
    is_sync = rt[col].astype(str).str.contains(r"-S\b", regex=True)
    return set(rt.loc[is_sync, "funcID"])


# Hvce changed hte function to be able to understnad candiate detection rather than over classify this as a holiday broad 
def candidate_detector(rm, length_days=7):
    daily = rm.groupby("day")["total_req_per_min"].sum()
    if len(daily) < length_days + 3:
        return None
    best, best_score = None, -np.inf
    for start in daily.index[: len(daily) - length_days + 1]:
        inside = daily.loc[start:start + length_days - 1]
        outside = daily.drop(inside.index)
        score = abs(inside.mean() - outside.mean()) / (outside.std() + 1e-9)
        if score > best_score:
            best, best_score = (int(start), int(start + length_days - 1)), float(score)
    return {"days": best, "score": best_score,
            "note": "candidate only; confirm against the paper"}


def build(args):
    t_start = _time.time()
    n_days = None if args.days == "all" else int(args.days)
    keep = sync_functions(args.root) if args.sync_only else None
    if keep is not None:
        print(f"keeping {len(keep)} synchronous functions")

    print("step 1: region totals per minute")
    rm, filled, ts_files = region_minutes(args.root, args.region, n_days, keep)
    print("step 2: cold starts per cluster")
    cold, cold_summary, cold_files = cold_per_cluster(args.root, args.region, n_days)
    print("step 3: cluster shares from request logs")
    traffic_share, pod_share, share_info = cluster_shares(args.requests_glob)
    clusters = [int(c) for c in traffic_share.index]
    print(f"  traffic share: {traffic_share.round(3).to_dict()}")
    print(f"  pod share:     {pod_share.round(3).to_dict()}")

    print("step 4: combine")
    rows = []
    for c in clusters:
        part = rm[["minute", "day", "total_req_per_min", "total_pods",
                   "svc_p50_ms", "svc_p99_ms", "req_bytes"]].copy()
        part["cluster"] = c
        part["arrivals_ps"] = part["total_req_per_min"] / 60.0 * float(traffic_share[c])
        part["servers"] = part["total_pods"] * float(pod_share[c])
        rows.append(part)
    table = pd.concat(rows, ignore_index=True)
    table = table.merge(cold, on=["minute", "cluster"], how="left")
    table["cold_n"] = table["cold_n"].fillna(0).astype(int)
    table["cold_ms"] = table["cold_ms"].fillna(0.0)

    p99_fixed = int((table["svc_p99_ms"] < table["svc_p50_ms"]).sum())
    table["svc_p99_ms"] = np.maximum(table["svc_p99_ms"], table["svc_p50_ms"])
    table["split"] = np.where(table["day"] >= args.test_from_day, "test", "train")
    table = table.sort_values(["minute", "cluster"]).reset_index(drop=True)

    # ---- checks ----
    per_min = table.groupby("minute")["arrivals_ps"].sum()
    expected = rm.set_index("minute")["total_req_per_min"] / 60.0
    assert np.allclose(per_min.values, expected.reindex(per_min.index).values, rtol=1e-6), \
        "cluster arrivals do not add up to the region total"
    num_cols = ["arrivals_ps", "servers", "svc_p50_ms", "svc_p99_ms", "cold_n", "cold_ms", "req_bytes"]
    assert (table[num_cols] >= 0).all().all(), "negative values found"
    assert not table[num_cols].isna().any().any(), "missing values remain"
    unexpected = set(table["cluster"]) - {1, 2, 3, 4}
    if unexpected:
        print(f"WARNING: unexpected cluster ids {unexpected}")
    if (table["split"] == "test").sum() == 0:
        print("WARNING: no test rows (load more days, or lower --test-from-day)")

    # rough per-pod rate from busy minutes (an assumption, recorded)
    busy = rm["total_req_per_min"] >= rm["total_req_per_min"].quantile(0.9)
    per_pod = (rm.loc[busy, "total_req_per_min"] / 60.0
               / rm.loc[busy, "total_pods"].replace(0, np.nan)).median()

    # ---- save ----
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    cols = ["minute", "day", "cluster", "arrivals_ps", "servers", "svc_p50_ms", "svc_p99_ms",
            "cold_n", "cold_ms", "req_bytes", "split"]
    try:
        table[cols].to_parquet(args.out, index=False)
        out_path = args.out
    except (ImportError, ValueError) as e:
        out_path = args.out.replace(".parquet", ".csv")
        print(f"parquet not available ({e}); writing {out_path}")
        table[cols].to_csv(out_path, index=False)

    meta = {
        "dataset": "Huawei Public Cloud Trace 2025",
        "region": args.region,
        "days_loaded": int(rm["day"].nunique()),
        "rows": int(len(table)),
        "clusters": clusters,
        "traffic_share": {int(k): float(v) for k, v in traffic_share.items()},
        "pod_share": {int(k): float(v) for k, v in pod_share.items()},
        "share_source": share_info,
        "cold_starts": cold_summary,
        "minutes_with_stats_filled_from_neighbours": filled,
        "rows_where_p99_raised_to_p50": p99_fixed,
        "sync_only": bool(args.sync_only),
        "test_from_day": args.test_from_day,
        "candiate_detector": candidate_detector(rm),
        "assumptions": {
            "cluster traffic/pod split": "fixed shares from Region 2 part 1 request logs (day 30)",
            "service time": "request-weighted average of per-function totalCost p50/p99 (approximation)",
            "req_bytes": "request-weighted average of per-function requestBodySize_avg",
            "resp_bytes": "not in data; set in the simulator (e.g. equal to req_bytes)",
            "per_pod_rate_ps_estimate": None if pd.isna(per_pod) else float(per_pod),
            "rtt_ms_between_zones": "set in the simulator (e.g. 1.5)",
            "usd_per_gb": 0.02,
            "fx_inr_per_usd": "set in the simulator, with date",
        },
        "files": {"timeseries": ts_files, "cold_start": cold_files},
        "output": out_path,
        "built_in_seconds": round(_time.time() - t_start, 1),
    }
    with open(args.out.replace(".parquet", ".json"), "w") as f:
        json.dump(meta, f, indent=2, default=str)

    print(f"\nsaved {out_path} ({len(table)} rows) and metadata")
    print(table[cols].head(8).to_string(index=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="../datasets/cold_start_dataset")
    ap.add_argument("--region", default="R2")
    ap.add_argument("--days", default="3", help='number of daily files, or "all"')
    ap.add_argument("--requests-glob",
                    default="../datasets/cold_start_dataset/per_request/R2/*.csv")
    ap.add_argument("--test-from-day", type=int, default=24,
                    help="days >= this are the test split (24-30 = last week)")
    ap.add_argument("--sync-only", action="store_true",
                    help="keep only synchronous functions (needs the runtime/trigger file)")
    ap.add_argument("--out", default="data/processed/huawei_region2.parquet")
    build(ap.parse_args())


if __name__ == "__main__":
    main()
