REGRU_IDENTITY_BASE = "https://api.cloud.reg.ru/identity/v3"
REGRU_NETWORK_BASE = "https://api.cloud.reg.ru/network"
REGRU_REGION = "msk1"  # Moscow — only region available

TOKEN_MAX_AGE = 20 * 3600
ERROR_RETRY_SEC = 30
RATE_LIMIT_RETRY_SEC = 30

# Target subnets for reg.cloud Moscow floating IPs.
# Edit this list to match the subnets you want to capture.
REGRU_WHITELIST_CIDRS = [
    "5.63.152.0/24",
    "5.63.153.0/24",
    "5.63.154.0/24",
    "5.63.155.0/24",
    "91.194.226.0/24",
    "91.194.227.0/24",
    "213.219.212.0/24",
    "213.219.213.0/24",
    "213.219.214.0/24",
    "213.219.215.0/24",
    "185.65.24.0/24",
    "185.65.25.0/24",
    "185.65.26.0/24",
    "185.65.27.0/24",
]
