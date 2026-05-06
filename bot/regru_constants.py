REGRU_IDENTITY_BASE = "https://api.cloud.reg.ru/identity/v3"
REGRU_NETWORK_BASE = "https://api.cloud.reg.ru/network"
REGRU_REGION = "msk1"  # Moscow — only region available

TOKEN_MAX_AGE = 20 * 3600
ERROR_RETRY_SEC = 30
RATE_LIMIT_RETRY_SEC = 30

# Target subnets for reg.cloud Moscow floating IPs.
# Edit this list to match the subnets you want to capture.
REGRU_WHITELIST_CIDRS = [
    "37.140.194.0/24",
    "37.140.195.0/24",
    "37.140.192.0/24",
    "37.140.193.0/24",
    "31.31.198.0/24",
    "31.31.196.0/24",
    "31.31.197.0/24",
    "79.174.92.0/24",
    "79.174.93.0/24",
    "79.174.94.0/24",
    "79.174.95.0/24",
    "95.163.232.0/24",
    "95.163.239.0/24",
    "194.67.98.0/24",
    "213.189.204.0/24",
]
