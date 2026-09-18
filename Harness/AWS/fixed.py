from pathlib import Path

PER_POD_RATE = 42.26 # p99 for global region 
SLO_MS = 678.7

USD_PER_GB = 0.02 # Netowrk In + Network out cost 
FX = 96.54
RESP_BYTES_RULE = "same as request"

RTT_MS = 1.5 # always below 2 
ACTIONS = [0.0, 0.10, 0.25, 0.50]
AZ_ZONES = 4 

LAMBDA = 1.0 # Cost paramter controller 
SLO_PENALTY = 1.0

EPISODE_MIN = 360
TEST_FROM_DAY = 24
HOLIDAY_DAYS = (9, 15) # This is controlled for abilation not known to algorithm 

CONFIG_VERSION = 1


UTIL_EDGES  = [0.078, 0.135, 0.317, 0.9] # UTIL edges or how much utilization can have 3 bands (total total dges + 1)
LATENCY_EDGES = [0.8, 1.0] # latency can be between below 0.8 and above 0.8 total 
SPARE_EDGES = [0.771, 0.91] # This means bands between 0.1 and post 0.3 what 




DATA_DIR   = Path(r"C:\Users\Aarya-2\Documents\ADOG\MARLOW AI\CPHarn\Z-heal\data\processed")
TABLE_PATH = DATA_DIR / "huawei_region2.parquet"
META_PATH  = DATA_DIR / "huawei_region2.json"
RUNS_DIR    = Path("runs")
RESULTS_DIR = Path("results")

SEEDS = range(5)

N_STATES = (len(UTIL_EDGES) + 1) * (len(LATENCY_EDGES) + 1) * (len(SPARE_EDGES) + 1)