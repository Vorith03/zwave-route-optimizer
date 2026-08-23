"""Constants for Z-Wave Route Optimizer."""

DOMAIN = "zwave_route_optimizer"
CONF_ZWAVE_ENTRY_ID = "zwave_entry_id"

SERVICE_OPTIMIZE_NODE = "optimize_node"
SERVICE_OPTIMIZE_NETWORK = "optimize_network"

DEFAULT_ROUNDS = 5
DEFAULT_PASSES = 1
DEFAULT_WARMUP = 1
DEFAULT_MAX_REPEATERS = 2
DEFAULT_MAX_CANDIDATES = 12
DEFAULT_MIN_IMPROVEMENT = 15.0
DEFAULT_SETTLE_SECONDS = 0.20
DEFAULT_SAMPLE_INTERVAL = 0.05

# Z-Wave Serial API route-speed values. These are *not* bit rates:
#   1 = 9.6 kbit/s, 2 = 40 kbit/s, 3 = 100 kbit/s
ROUTE_SPEED_TO_BPS = {
    1: 9600,
    2: 40000,
    3: 100000,
}
BPS_TO_ROUTE_SPEED = {bps: speed for speed, bps in ROUTE_SPEED_TO_BPS.items()}
VALID_BPS = tuple(sorted(BPS_TO_ROUTE_SPEED, reverse=True))
